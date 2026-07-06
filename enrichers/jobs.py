import logging

import django_rq
from django_rq import job
from rq import Retry

from tribunals.models import Process

from .esaj import TjacEnricher, TjalEnricher, TjspEnricher
from .tjap import TjapEnricher
from .tjce import TjceEnricher
from .tjdft import TjdftEnricher
from .tjma import TjmaEnricher
from .tjmg import TjmgEnricher
from .tjmt import TjmtEnricher
from .tjpa import TjpaEnricher
from .tjpe import TjpeEnricher
from .tjrj import TjrjEnricher
from .tjro import TjroEnricher
from .trf1 import Trf1Enricher
from .trf3 import Trf3Enricher
from .trf5 import Trf5Enricher

logger = logging.getLogger('voyager.enrichers.jobs')


_ENRICHERS = {
    'TRF1': Trf1Enricher,
    'TRF3': Trf3Enricher,
    'TRF5': Trf5Enricher,
    'TJMG': TjmgEnricher,
    'TJMA': TjmaEnricher,
    'TJSP': TjspEnricher,
    'TJAL': TjalEnricher,
    'TJDFT': TjdftEnricher,
    # Viáveis (recon 2026-06-29): PJe clássico sem captcha + e-SAJ.
    'TJCE': TjceEnricher,
    'TJAP': TjapEnricher,
    'TJPE': TjpeEnricher,
    'TJRJ': TjrjEnricher,
    'TJRO': TjroEnricher,
    'TJAC': TjacEnricher,
    'TJPA': TjpaEnricher,
    'TJMT': TjmtEnricher,
}

ENRICH_TIMEOUT = 300

# Auto-retry de falhas TRANSIENTES (Redis/PG connection drop, timeout). Sem
# interval = retry imediato (não exige rqworker --with-scheduler). Jobs são
# idempotentes (drainer dedupe por scraped_at; upsert ignore_conflicts), então
# re-tentar é seguro. nao_encontrado/erro de scrape NÃO são exceção → não
# disparam retry; só os erros de infra (que viravam failed à toa) re-tentam.
ENRICH_RETRY = Retry(max=3)


def queue_for(tribunal_sigla: str) -> str:
    """Mapeia sigla → nome da fila por tribunal (enrich_trf1, enrich_trf3...)."""
    return f'enrich_{tribunal_sigla.lower()}'


# @job('default') é mantido pra trabalhar como fallback se algo enfileirar
# direto via .delay() sem passar pela queue per-tribunal.
@job('default', timeout=ENRICH_TIMEOUT)
def enriquecer_processo(process_id: int, prefer_cortex: bool | None = None,
                         direct_apply: bool = False, seguir_incidentes: bool = False) -> dict:
    # prefer_cortex=None → resolve do setting (default True: Cortex-first pra
    # passar o WAF; datacenter fica de fallback). Cobre TODOS os paths de enqueue.
    if prefer_cortex is None:
        from django.conf import settings
        prefer_cortex = getattr(settings, 'ENRICH_PREFER_CORTEX', True)
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    logger.info('enriquecer_processo inicio %s %s', p.tribunal_id, p.numero_cnj)
    enricher = cls(prefer_cortex=prefer_cortex)
    # seguir_incidentes só faz sentido no e-SAJ (cada parte tem um incidente).
    from enrichers.esaj import BaseEsajEnricher
    if seguir_incidentes and isinstance(enricher, BaseEsajEnricher):
        result = enricher.enriquecer(p, direct_apply=direct_apply, seguir_incidentes=True)
    else:
        result = enricher.enriquecer(p, direct_apply=direct_apply)
    logger.info('enriquecer_processo fim %s %s status=%s', p.tribunal_id, p.numero_cnj, result.get('status', 'ok'))
    return result


def enqueue_enriquecimento(process_id: int, tribunal_sigla: str):
    """Enfileira na queue do tribunal — paraleliza coletas sem misturar pools.

    prefer_cortex do setting (default True): residencial passa o WAF dos tribunais;
    o pool datacenter fica só como fallback. Evita rotacionar 40 IPs queimados.
    """
    queue = django_rq.get_queue(queue_for(tribunal_sigla))
    return queue.enqueue(enriquecer_processo, process_id, job_timeout=ENRICH_TIMEOUT,
                         retry=ENRICH_RETRY)


def enqueue_enriquecimento_manual(process_id: int):
    """Enfileira na queue 'manual' — prioritária pra cliques na UI.

    Bypassa as filas per-tribunal (que podem ter centenas de milhares de
    jobs). `prefer_cortex=False`: pool-first (ProxyScrape). O e-SAJ (TJSP/TJAL/
    TJAC) funciona bem com o pool datacenter, e o Cortex tem resetado conexão
    (Connection reset) — Cortex-first fazia o enrich manual FALHAR e o dossiê
    vir vazio. Com pool-first, o Cortex vira fallback no _next_proxy. (2026-07-06)
    """
    from django.conf import settings
    queue = django_rq.get_queue('manual')
    return queue.enqueue(
        enriquecer_processo,
        args=(process_id,),
        kwargs={'prefer_cortex': False, 'direct_apply': True,
                'seguir_incidentes': getattr(settings, 'ESAJ_SEGUIR_INCIDENTES', False)},
        job_timeout=ENRICH_TIMEOUT,
        retry=ENRICH_RETRY,
    )


# Buffer pra workers nunca esperarem: com 600+ workers (300 .30 + 300 .177)
# por tribunal a ~5-10s/job, queima ~60-120/s = ~7-14k/min. 100k high-water
# garante 7-14min de folga entre refills do scheduler (que roda a cada 2min).
ENQUEUE_BATCH_SIZE = 10_000
QUEUE_HIGH_WATER = 100_000  # se já tem isso na fila, não re-enfileira

# --- Recuperação de falsos-negativos e-SAJ (nao_encontrado legado) -----------
# Os enrichers e-SAJ marcavam nao_encontrado TERMINAL em falha transitória (200
# ambíguo) — corrigido em 2026-07-06 (esaj 8bfd9f7). Os ~3,25M presos são legado.
# Este tick os devolve a 'pendente' pra re-enriquecer com a lógica nova, de forma
# AUTO-LIMITANTE (só quando a pipeline tem folga) e SEM loop: reseta apenas
# nao_encontrado com enriquecido_em ANTES do fix; os re-enriquecidos pós-fix
# (genuínos) ganham timestamp recente e nunca mais são resetados.
import datetime as _dt  # noqa: E402
from django.utils import timezone as _tz  # noqa: E402

REENRICH_ESAJ_TRIBUNAIS = ('TJSP', 'TJAL', 'TJAC')
REENRICH_LEGACY_CUTOFF = _tz.make_aware(_dt.datetime(2026, 7, 6))
REENRICH_PENDENTE_FLOOR = 20_000   # só reabastece nao_encontrado se pendente < isso
REENRICH_RESET_BATCH = 50_000      # quantos nao_encontrado→pendente por tribunal/tick


@job('default', timeout=600)
def tick_reenrich_esaj_legacy() -> dict:
    """Devolve nao_encontrado LEGADO (pré-fix) a 'pendente', auto-limitante."""
    relatorio: dict = {}
    for sig in REENRICH_ESAJ_TRIBUNAIS:
        pend = Process.objects.filter(
            tribunal_id=sig, enriquecimento_status='pendente').count()
        if pend >= REENRICH_PENDENTE_FLOOR:
            relatorio[sig] = f'skip (pendente {pend:,} ≥ {REENRICH_PENDENTE_FLOOR:,})'
            continue
        ids = list(Process.objects.filter(
            tribunal_id=sig, enriquecimento_status='nao_encontrado',
            enriquecido_em__lt=REENRICH_LEGACY_CUTOFF,
        ).values_list('pk', flat=True)[:REENRICH_RESET_BATCH])
        if not ids:
            relatorio[sig] = 'sem legado restante'
            continue
        n = Process.objects.filter(pk__in=ids).update(enriquecimento_status='pendente')
        relatorio[sig] = f'reset {n:,}'
        logger.info('tick_reenrich_esaj_legacy %s: reset %d', sig, n)
    return relatorio


@job('default', timeout=600)
def reabastecer_filas_enriquecimento() -> dict:
    """Cron: enfileira Process pendentes por tribunal, sem duplicar in-flight.

    Idempotente e seguro contra os dois modos de falha que derrubaram o DB em
    2026-07-01 (enrich_tjmt chegou a 387k jobs p/ high-water 100k):
    - **concorrência**: lock Redis. O scan de pendentes é lento em tribunal com
      milhões pendentes; sem lock, runs do scheduler (2min) sobrepunham e cada um
      passava o teste `len(queue) < high_water` → estouro.
    - **re-enfileiramento**: o `enriquecimento_status` só vira OK quando o drainer
      (async) aplica; filtrar `status=pendente` sem mais nada re-selecionava os
      MESMOS processos a cada ciclo. Agora paginamos por pk via cursor Redis
      (`enr:cursor:<sigla>`): cada processo é enfileirado uma vez por passada;
      ao esgotar o backlog o cursor volta a 0 (pega os que falharam/voltaram a
      pendente e os novos da ingestão diária).
    """
    from django.core.cache import cache
    lock = 'lock:reabastecer_enriquecimento'
    if not cache.add(lock, '1', timeout=600):
        logger.info('reabastecer: skip (lock held)')
        return {'skip': 'lock held'}
    try:
        return _reabastecer_impl()
    finally:
        cache.delete(lock)


def _reabastecer_impl() -> dict:
    from tribunals.models import Process

    relatorio = {}
    for sigla in _ENRICHERS.keys():
        queue = django_rq.get_queue(queue_for(sigla))
        qlen = len(queue)
        if qlen >= QUEUE_HIGH_WATER:
            relatorio[sigla] = f'skip (fila {qlen:,} ≥ {QUEUE_HIGH_WATER})'
            continue
        capacidade = min(QUEUE_HIGH_WATER - qlen, ENQUEUE_BATCH_SIZE)
        # Query simples e sargável (usa índice de tribunal_id / status); NÃO
        # ordena por pk (ORDER BY pk varre o espaço GLOBAL de pk filtrando
        # tribunal_id → scan de minutos, incidente 2026-07-01). Re-seleção de
        # PENDENTE já-em-fila é tolerada: o teto QUEUE_HIGH_WATER + o lock
        # bounded a duplicação (workers drenam, drainer marca OK, some do filtro);
        # o out-of-order guard do drainer descarta re-scrapes de processo já OK.
        ids = list(
            Process.objects.filter(
                tribunal_id=sigla,
                enriquecimento_status=Process.ENRIQ_PENDENTE,
            ).values_list('pk', flat=True)[:capacidade]
        )
        for pid in ids:
            try:
                queue.enqueue(enriquecer_processo, pid, job_timeout=ENRICH_TIMEOUT,
                              retry=ENRICH_RETRY)
            except Exception as exc:
                logger.warning('falha ao enfileirar', extra={'pid': pid, 'erro': str(exc)})
        relatorio[sigla] = f'+{len(ids)} (fila {qlen}->{qlen+len(ids)})'
    logger.info('reabastecer_filas_enriquecimento', extra={'r': relatorio})
    return relatorio
