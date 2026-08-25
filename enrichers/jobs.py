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


# --- Kill-switch de enrichment por tribunal ---------------------------------
# Quando um tribunal bloqueia o método de acesso (ex.: TJRO/TJAP devolvendo 403
# pra todos os proxies em 2026-07), continuar tentando só queima proxy/CPU e
# enche o Process.enriquecimento_erro. A pausa vive num set no cache Redis
# (sem TTL — sobrevive a restart) e é honrada em TRÊS pontos: refill, enqueue
# da ingestão e o próprio job (drena fila residual sem tocar a fonte).
_PAUSA_KEY = 'enrich:pausados'


# Tribunais que bloqueiam datacenter POR COMPLETO (403 em todo IP do pool —
# ex.: TJRO/TJAP em 2026-07). Pra esses, tentar datacenter é desperdício: vão
# DIRETO e SÓ pro Cortex (residencial). Granular por tribunal, em cache Redis.
_CORTEX_ONLY_KEY = 'enrich:cortex_only'


def enrich_cortex_only() -> set[str]:
    from django.core.cache import cache
    return set(cache.get(_CORTEX_ONLY_KEY) or [])


def is_cortex_only(sigla: str) -> bool:
    return (sigla or '').upper() in enrich_cortex_only()


def set_cortex_only(siglas: set[str]) -> None:
    from django.core.cache import cache
    cache.set(_CORTEX_ONLY_KEY, sorted(s.upper() for s in siglas), timeout=None)


def enrich_pausados() -> set[str]:
    from django.core.cache import cache
    return set(cache.get(_PAUSA_KEY) or [])


def enrich_pausado(sigla: str) -> bool:
    return (sigla or '').upper() in enrich_pausados()


def set_enrich_pausados(siglas: set[str]) -> None:
    from django.core.cache import cache
    cache.set(_PAUSA_KEY, sorted(s.upper() for s in siglas), timeout=None)


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
    # Pausado: no-op SEM tocar no status (continua pendente) — a fila residual
    # drena rápido sem queimar proxy; ao despausar, o refill repõe e processa.
    if enrich_pausado(p.tribunal_id):
        return {'skip': 'pausado', 'tribunal': p.tribunal_id}
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    # e-SAJ (TJSP/TJAL/TJAC) funciona com o pool ProxyScrape; o Cortex tem resetado
    # conexão e faz o enrich falhar (dossiê vazio). Força pool-first pro e-SAJ,
    # independente do que o enqueue passou (garante o fix mesmo se o web ainda não
    # atualizou). Cortex vira fallback no _next_proxy. (2026-07-06)
    from enrichers.esaj import BaseEsajEnricher
    if issubclass(cls, BaseEsajEnricher):
        prefer_cortex = False
    logger.info('enriquecer_processo inicio %s %s', p.tribunal_id, p.numero_cnj)
    enricher = cls(prefer_cortex=prefer_cortex)
    # seguir_incidentes só faz sentido no e-SAJ (cada parte tem um incidente).
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
    if enrich_pausado(tribunal_sigla):
        return None
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
QUEUE_HIGH_WATER = 100_000  # TETO ABSOLUTO; o teto EFETIVO é medido (ver abaixo)

# --- Por que o teto de 100k virou o gargalo (censo de 25/08/2026) -------------
# A frota tem 146 workers de enrich, não os 600 da conta acima. A 100k jobs por
# fila, o tempo de espera na fila (enqueued_at do job na frente) foi medido em
# 20 a 74 HORAS. E um Process só sai de `pendente` quando o drainer aplica o
# resultado — ou seja, ele fica elegível pro refill durante essas 20-74h. Como
# `_reabastecer_impl` re-seleciona a MESMA cabeça do índice a cada 2 min, a fila
# se enche de cópias do mesmo processo. Censo COMPLETO (não amostra) das 16
# filas em 25/08/2026 00:20 UTC — `ids` é o comprimento da lista, `distintos`
# são os process_id únicos:
#
#   fila            ids    distintos  cópias   max  desperdício
#   enrich_tjmt   99.818       1.418    70,4   136       98,6%
#   enrich_tjac   99.976       1.929    51,8   607       98,1%
#   enrich_tjap   99.968       3.426    29,2   515       96,6%
#   enrich_tjal   99.993       6.433    15,5   849       93,6%
#   enrich_tjro   99.979       8.486    11,8 1.926       91,5%
#   enrich_tjpa   99.952      10.130     9,9   832       89,9%
#   enrich_tjma   99.927      10.711     9,3   564       89,3%
#   enrich_tjce   99.954      12.008     8,3   336       88,0%
#   enrich_tjpe   99.942      12.895     7,8   499       87,1%
#   enrich_trf5   99.948      31.546     3,2   535       68,4%
#   enrich_trf3   99.939      35.265     2,8   672       64,7%
#   enrich_trf1   99.935      35.675     2,8   484       64,3%
#   enrich_tjdft 100.767      54.277     1,9    32       46,1%
#   enrich_tjmg   99.947      74.931     1,3   192       25,0%
#   enrich_tjrj  251.534     251.534     1,0     1        0,0%   ← refill PULA
#   enrich_tjsp  227.678     227.503     1,0     2        0,1%   ← refill PULA
#
# 1.400.007 jobs nas 14 filas que o refill administra para 321.130 processos
# distintos: **77,1% das raspagens repetem trabalho já enfileirado**. As duas
# filas limpas são justamente as que estão ACIMA do teto — o refill não as
# toca, e a duplicação some. O teto redondo não era o total: era a fábrica.
#
# Duas correções, ambas aqui:
#  (a) TETO MEDIDO — o alvo de profundidade é ENRICH_QUEUE_ALVO_S segundos de
#      trabalho, calculado com a vazão real da fila (o FinishedJobRegistry do RQ
#      guarda 500 s de jobs terminados; len(reg)/500 = jobs/s). Fila rasa = o
#      processo é raspado antes de o refill ter chance de re-selecioná-lo.
#  (b) TRAVA DE VOO — ZSET por tribunal com os process_id já enfileirados. Um
#      pid só volta pra fila depois de ENRICH_INFLIGHT_TTL_S. Sem isso, tribunal
#      com backlog quase esgotado (TJMT tinha 1.418 pendentes) re-enfileira o
#      resto inteiro a cada 2 min, para sempre.
# 30 min de trabalho na fila. O refill roda a cada 2 min, então isto é folga
# de 15 passadas — a fila não seca se o `worker_default` estiver ocupado com um
# tick longo. E é sempre menor que ENRICH_INFLIGHT_TTL_S, senão um pid sairia da
# trava de voo antes de ser raspado e voltaria pra fila (a duplicação de volta).
ENRICH_QUEUE_ALVO_S = 1_800
ENRICH_QUEUE_MIN = 2_000         # piso: fila lenta/nova não pode secar
ENRICH_INFLIGHT_TTL_S = 3_600    # 1 h — maior que a espera-alvo, com folga
_INFLIGHT_KEY = 'enr:inflight:%s'


def _profundidade_alvo(conn, nome_fila: str) -> int:
    """Teto EFETIVO da fila: ~ENRICH_QUEUE_ALVO_S segundos de trabalho.

    A régua é o próprio RQ: `FinishedJobRegistry` guarda os jobs terminados por
    `result_ttl` (500 s em prod), então `len(registry)/500` é a vazão medida da
    fila, sem instrumentação nova. Registry vazio (fila parada ou recém-criada)
    cai no piso — nunca em zero, senão o worker fica sem trabalho.
    """
    from rq.registry import FinishedJobRegistry
    try:
        terminados = len(FinishedJobRegistry(nome_fila, connection=conn))
    except Exception:
        terminados = 0
    por_segundo = terminados / 500.0
    return max(ENRICH_QUEUE_MIN,
               min(QUEUE_HIGH_WATER, int(por_segundo * ENRICH_QUEUE_ALVO_S)))


def _filtrar_em_voo(conn, sigla: str, ids: list[int], quantos: int) -> list[int]:
    """Devolve até `quantos` pids que NÃO estão em voo, e os marca em voo.

    ZSET por tribunal (`enr:inflight:<sigla>`), score = epoch do enqueue. A poda
    é por score no começo de cada passada, então a chave não cresce sem fim:
    ela guarda no máximo o que foi enfileirado na última ENRICH_INFLIGHT_TTL_S.
    Duas viagens ao Redis (ler scores, depois marcar só os escolhidos) — marcar
    antes de escolher marcaria como em-voo pid que não foi enfileirado.
    """
    import time as _time
    chave = _INFLIGHT_KEY % sigla.lower()
    agora = _time.time()
    conn.zremrangebyscore(chave, '-inf', agora - ENRICH_INFLIGHT_TTL_S)
    if not ids:
        return []
    try:
        scores = conn.zmscore(chave, [str(i) for i in ids])
    except Exception:                      # Redis < 6.2 / redis-py antigo
        pipe = conn.pipeline()
        for i in ids:
            pipe.zscore(chave, str(i))
        scores = pipe.execute()
    livres = [pid for pid, sc in zip(ids, scores, strict=False) if sc is None][:quantos]
    if livres:
        conn.zadd(chave, {str(p): agora for p in livres})
    return livres

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
REENRICH_MAX_TENTATIVAS = 12       # teto de raspagens por processo no requeue de erro


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
def tick_requeue_erros_enricher() -> dict:
    """Devolve status='erro' → 'pendente' nos tribunais de VALOR (Juriscope ∩
    enricher), bounded pelo pendente floor. Fase 2 do plano de cobertura: o
    `reabastecer_filas_enriquecimento` só re-enfileira 'pendente' → sem isto os
    ~1,1M em 'erro' (transitórios: proxy/WAF, comentário do ENRIQ_ERRO) ficavam
    presos pra sempre. Gêmeo do tick_reenrich_esaj_legacy. Ver .ia/ENRICHMENT.md.
    """
    from djen.ingestion import TRIBUNAIS_JURISCOPE
    alvo = [s for s in _ENRICHERS if s in TRIBUNAIS_JURISCOPE]
    relatorio: dict = {}
    pausados = enrich_pausados()
    for sig in alvo:
        if sig in pausados:
            relatorio[sig] = 'pausado'
            continue
        # SEM floor de pendente: os 'erro' estão PERMANENTEMENTE presos (o
        # reabastecer só pega 'pendente'); resetá-los é o ganho mesmo com pendente
        # já alto — a FILA é bounded pela profundidade-alvo, não o status no DB. O
        # batch por tick é o único limitador de ritmo (churn bounded).
        #
        # TETO DE TENTATIVAS (25/08/2026): sem ele o tick é uma esteira infinita.
        # Medido em prod: o processo 24179591 (TJAL) estava em
        # `enriquecimento_tentativas=360` — 360 raspagens, todas terminando em
        # erro, porque a cada 10 min este tick devolvia o registro a `pendente` e
        # o refill o enfileirava de novo. Cada tentativa dessas gasta até 8 IPs
        # do pool (que é compartilhado com TODOS os tribunais). Acima do teto o
        # processo fica ESTACIONADO em `erro` — e o teto é ALERTA, não corte
        # mudo: sai no log em nível ERROR com a contagem por tribunal.
        linhas = list(Process.objects.filter(
            tribunal_id=sig, enriquecimento_status=Process.ENRIQ_ERRO,
        ).values_list('pk', 'enriquecimento_tentativas')[:REENRICH_RESET_BATCH])
        ids = [pk for pk, t in linhas if (t or 0) < REENRICH_MAX_TENTATIVAS]
        estacionados = len(linhas) - len(ids)
        if estacionados:
            logger.error(
                'requeue_erros: %d de %d processos %s ESTACIONADOS em erro '
                '(≥ %d tentativas) — a fonte não responde pra eles, requeue não resolve',
                estacionados, len(linhas), sig, REENRICH_MAX_TENTATIVAS)
        if not ids:
            relatorio[sig] = ('sem erro' if not linhas
                              else f'{estacionados:,} estacionados (≥{REENRICH_MAX_TENTATIVAS} tent.)')
            continue
        n = Process.objects.filter(pk__in=ids).update(
            enriquecimento_status=Process.ENRIQ_PENDENTE)
        relatorio[sig] = f'reset {n:,}' + (f' / {estacionados:,} estacionados' if estacionados else '')
        logger.info('tick_requeue_erros_enricher %s: reset %d erro->pendente', sig, n)
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
      MESMOS processos a cada ciclo. O remédio prometido aqui era um cursor por
      pk (`enr:cursor:<sigla>`) que NUNCA existiu no código — e o censo de
      25/08/2026 mediu o preço: 77,1% dos jobos das 14 filas administradas por
      este refill eram cópia. Quem resolve hoje são as duas travas descritas
      junto de `ENRICH_QUEUE_ALVO_S`: profundidade-alvo medida pela vazão real
      da fila e ZSET de processos em voo.
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
    pausados = enrich_pausados()
    for sigla in _ENRICHERS.keys():
        if sigla in pausados:
            relatorio[sigla] = 'pausado'
            continue
        queue = django_rq.get_queue(queue_for(sigla))
        conn = queue.connection
        qlen = len(queue)
        alvo = _profundidade_alvo(conn, queue.name)
        if qlen >= alvo:
            relatorio[sigla] = f'skip (fila {qlen:,} ≥ alvo {alvo:,})'
            continue
        capacidade = min(alvo - qlen, ENQUEUE_BATCH_SIZE)
        # Query simples e sargável (usa índice de tribunal_id / status); NÃO
        # ordena por pk (ORDER BY pk varre o espaço GLOBAL de pk filtrando
        # tribunal_id → scan de minutos, incidente 2026-07-01).
        #
        # A re-seleção de PENDENTE já-em-fila NÃO é mais tolerada: era ela que
        # produzia 77,1% de cópias (censo acima). Quem barra é `_filtrar_em_voo`.
        # O out-of-order guard do drainer só descartava o efeito NO BANCO — a
        # requisição ao tribunal e o IP do pool já tinham sido gastos.
        # Lê MAIS candidatos que a capacidade: a cabeça do índice pode estar
        # inteira em voo (é o caso normal — foi ela que a passada anterior
        # enfileirou). Sem essa folga a fila secava em vez de avançar. O teto
        # de 60k mantém o scan barato (values_list de int no índice composto
        # proc_trib_enriq_idx; o incidente de 2026-07-01 foi por ORDER BY pk,
        # que continua fora).
        em_voo = conn.zcard(_INFLIGHT_KEY % sigla.lower())
        limite_scan = min(capacidade + em_voo + 1_000, 60_000)
        candidatos = list(
            Process.objects.filter(
                tribunal_id=sigla,
                enriquecimento_status=Process.ENRIQ_PENDENTE,
            ).values_list('pk', flat=True)[:limite_scan]
        )
        ids = _filtrar_em_voo(conn, sigla, candidatos, capacidade)
        for pid in ids:
            try:
                queue.enqueue(enriquecer_processo, pid, job_timeout=ENRICH_TIMEOUT,
                              retry=ENRICH_RETRY)
            except Exception as exc:
                logger.warning('falha ao enfileirar', extra={'pid': pid, 'erro': str(exc)})
        relatorio[sigla] = (f'+{len(ids)} (fila {qlen}->{qlen+len(ids)}, alvo {alvo:,}, '
                            f'em voo {em_voo:,}, candidatos {len(candidatos):,})')
    logger.info('reabastecer_filas_enriquecimento', extra={'r': relatorio})
    return relatorio
