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
def enriquecer_processo(process_id: int, prefer_cortex: bool = False,
                         direct_apply: bool = False) -> dict:
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    logger.info('enriquecer_processo inicio %s %s', p.tribunal_id, p.numero_cnj)
    enricher = cls(prefer_cortex=prefer_cortex)
    result = enricher.enriquecer(p, direct_apply=direct_apply)
    logger.info('enriquecer_processo fim %s %s status=%s', p.tribunal_id, p.numero_cnj, result.get('status', 'ok'))
    return result


def enqueue_enriquecimento(process_id: int, tribunal_sigla: str):
    """Enfileira na queue do tribunal — paraleliza coletas sem misturar pools."""
    queue = django_rq.get_queue(queue_for(tribunal_sigla))
    return queue.enqueue(enriquecer_processo, process_id, job_timeout=ENRICH_TIMEOUT,
                         retry=ENRICH_RETRY)


def enqueue_enriquecimento_manual(process_id: int):
    """Enfileira na queue 'manual' — prioritária pra cliques na UI.

    Bypassa as filas per-tribunal (que podem ter centenas de milhares de
    jobs). Passa `prefer_cortex=True` pro enricher tentar o proxy
    residencial primeiro — click do user retorna em ~1-3s em vez de
    rotacionar proxies queimados por 30s+.
    """
    queue = django_rq.get_queue('manual')
    return queue.enqueue(
        enriquecer_processo,
        args=(process_id,),
        kwargs={'prefer_cortex': True, 'direct_apply': True},
        job_timeout=ENRICH_TIMEOUT,
        retry=ENRICH_RETRY,
    )


# Buffer pra workers nunca esperarem: com 600+ workers (300 .30 + 300 .177)
# por tribunal a ~5-10s/job, queima ~60-120/s = ~7-14k/min. 100k high-water
# garante 7-14min de folga entre refills do scheduler (que roda a cada 2min).
ENQUEUE_BATCH_SIZE = 10_000
QUEUE_HIGH_WATER = 100_000  # se já tem isso na fila, não re-enfileira


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

    conn = django_rq.get_connection()
    relatorio = {}
    for sigla in _ENRICHERS.keys():
        queue = django_rq.get_queue(queue_for(sigla))
        qlen = len(queue)
        if qlen >= QUEUE_HIGH_WATER:
            relatorio[sigla] = f'skip (fila {qlen:,} ≥ {QUEUE_HIGH_WATER})'
            continue
        capacidade = min(QUEUE_HIGH_WATER - qlen, ENQUEUE_BATCH_SIZE)
        ckey = f'enr:cursor:{sigla}'
        cursor = int(conn.get(ckey) or 0)
        ids = list(
            Process.objects.filter(
                tribunal_id=sigla,
                enriquecimento_status=Process.ENRIQ_PENDENTE,
                pk__gt=cursor,
            ).order_by('pk').values_list('pk', flat=True)[:capacidade]
        )
        if not ids:
            conn.set(ckey, 0)  # fim do backlog → volta pro começo
            relatorio[sigla] = f'wrap (cursor {cursor}->0, fila {qlen})'
            continue
        for pid in ids:
            try:
                queue.enqueue(enriquecer_processo, pid, job_timeout=ENRICH_TIMEOUT,
                              retry=ENRICH_RETRY)
            except Exception as exc:
                logger.warning('falha ao enfileirar', extra={'pid': pid, 'erro': str(exc)})
        conn.set(ckey, ids[-1])
        relatorio[sigla] = f'+{len(ids)} (cursor->{ids[-1]}, fila {qlen})'
    logger.info('reabastecer_filas_enriquecimento', extra={'r': relatorio})
    return relatorio
