"""Jobs RQ pra ingestão Datajud."""
import logging

import django_rq
from django_rq import job
from rq import Retry

from tribunals.models import Process

from .client import DatajudClient
from .ingestion import sync_processo

logger = logging.getLogger('voyager.datajud.jobs')

# Auto-retry imediato de falhas transientes (Redis/PG drop) — idempotente
# (bulk_create ignore_conflicts). Sem interval: não exige --with-scheduler.
DATAJUD_RETRY = Retry(max=3)

# Análogo ao reabastecer_filas_enriquecimento (PJe). Drena backlog histórico
# de Process com data_enriquecimento_datajud IS NULL.
DATAJUD_REFILL_BATCH = 10_000
DATAJUD_REFILL_HIGH_WATER = 100_000


@job('manual', timeout=300)
def datajud_sincronizar_processo(process_id: int, prefer_cortex: bool = True) -> dict:
    """Sincroniza um processo via Datajud — usado pelo botão UI.

    `prefer_cortex=True` por default: cliente vai pra Cortex primeiro,
    porque é click manual com user esperando. Backfill em massa que
    chamar isso direto pode passar False.
    """
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    logger.info('datajud_sincronizar_processo %s %s', p.tribunal.sigla, p.numero_cnj)
    client = DatajudClient(prefer_cortex=prefer_cortex)
    return sync_processo(p, client=client)


@job('datajud', timeout=600)
def datajud_sync_bulk(process_id: int) -> dict:
    """Versão bulk auto-enfileirada quando processos novos aparecem na
    ingestão DJEN. Roda na fila `datajud` (workers dedicados) pra não
    disputar com `enrich_trf*` (PJe scraping) nem `djen_backfill`
    (data-based)."""
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    logger.info('datajud_sync_bulk %s %s', p.tribunal.sigla, p.numero_cnj)
    client = DatajudClient(prefer_cortex=False)
    return sync_processo(p, client=client)


@job('default', timeout=300)
def reabastecer_fila_datajud() -> dict:
    """Cron: enfileira até DATAJUD_REFILL_BATCH Process pendentes de Datajud,
    desde que a fila não esteja cheia (DATAJUD_REFILL_HIGH_WATER).

    Análogo ao reabastecer_filas_enriquecimento (PJe). Sem este job, o
    backlog histórico de Process com data_enriquecimento_datajud=NULL
    nunca é drenado — só processos tocados na ingestão diária pegam
    Datajud, deixando processos antigos sem features Datajud na
    classificação.
    """
    from django.conf import settings
    if not getattr(settings, 'DATAJUD_ENQUEUE_ENABLED', True):
        logger.info('reabastecer_fila_datajud: desativado (DATAJUD_ENQUEUE_ENABLED=False)')
        return {'skipped': 'disabled'}
    queue = django_rq.get_queue('datajud')
    if len(queue) >= DATAJUD_REFILL_HIGH_WATER:
        msg = f'skip (fila com {len(queue):,} jobs ≥ {DATAJUD_REFILL_HIGH_WATER:,})'
        logger.info('reabastecer_fila_datajud: %s', msg)
        return {'skipped': msg}

    capacidade = DATAJUD_REFILL_HIGH_WATER - len(queue)
    a_enfileirar = min(capacidade, DATAJUD_REFILL_BATCH)
    # `order_by('-inserido_em')`: sem ordenação, o Postgres devolve por
    # heap order — que clustera por tribunal_id e faz cada refill enfileirar
    # 10k do MESMO tribunal. Resultado: drenagem por blocos (TRF5 dias,
    # depois TJMG dias, etc.) e tribunais menores como TRF3 ficam esperando
    # seu "lote" ser sorteado. Ordenando por inserido_em DESC, refills
    # naturalmente intercalam tribunais (DJEN diária toca todos).
    # Escopo: só tribunais SEM enricher (onde há enricher, classe/assunto vem dele
    # → Datajud redundante). Evita reafogar a API pública compartilhada do CNJ.
    from djen.ingestion import TRIBUNAIS_COM_ENRICHER
    pids = list(
        Process.objects.filter(
            tribunal__ativo=True,
            data_enriquecimento_datajud__isnull=True,
        ).exclude(
            tribunal__sigla__in=TRIBUNAIS_COM_ENRICHER,
        ).order_by('-inserido_em').values_list('pk', flat=True)[:a_enfileirar]
    )
    # Enqueue explícito na queue 'datajud' (não usa .delay()) — mesmo padrão
    # do reabastecer_filas_enriquecimento, defensivo contra alguém mudar o
    # decorator do datajud_sync_bulk no futuro.
    enfileirados = 0
    for pid in pids:
        try:
            queue.enqueue(datajud_sync_bulk, pid, job_timeout=600, retry=DATAJUD_RETRY)
            enfileirados += 1
        except Exception as exc:
            logger.warning('falha ao enfileirar datajud bulk pid=%d: %s', pid, exc)
    logger.info('reabastecer_fila_datajud: %d enfileirados', enfileirados)
    return {'enfileirados': enfileirados}


@job('default', timeout=60)
def datajud_api_healthcheck() -> dict:
    """Sonda a API pública do Datajud (_count direto, sem proxy/rate-limit) e
    registra o estado em cache (`datajud:api_health`). Loga WARNING na transição
    down→up pra sinalizar que dá pra religar (DATAJUD_ENQUEUE_ENABLED=true).

    1 req a cada 15min é desprezível pra quota — é só um probe.
    """
    import time

    import requests
    from django.core.cache import cache
    from django.utils import timezone

    from .client import DEFAULT_API_KEY, index_for

    url = f'https://api-publica.datajud.cnj.jus.br/{index_for("TJSP")}/_count'
    hdr = {'Authorization': DEFAULT_API_KEY, 'Content-Type': 'application/json'}
    prev = cache.get('datajud:api_health') or {}
    t0 = time.monotonic()
    try:
        r = requests.post(url, json={'query': {'match_all': {}}}, headers=hdr, timeout=15)
        ok = r.status_code == 200
        status = r.status_code
    except Exception as exc:  # noqa: BLE001
        ok = False
        status = type(exc).__name__
    latency = round(time.monotonic() - t0, 2)
    state = {'ok': ok, 'status': status, 'latency': latency,
             'checked_at': timezone.now().isoformat()}
    cache.set('datajud:api_health', state, 3600)
    if ok and not prev.get('ok'):
        logger.warning(
            '🟢 Datajud API RECUPEROU (_count HTTP %s em %ss). '
            'Dá pra religar: DATAJUD_ENQUEUE_ENABLED=true + force-recreate.',
            status, latency,
        )
    elif not ok:
        logger.info('Datajud API ainda fora (%s, %ss)', status, latency)
    return state
