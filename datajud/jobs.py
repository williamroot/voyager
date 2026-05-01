"""Jobs RQ pra ingestão Datajud."""
import logging

import django_rq
from django_rq import job

from tribunals.models import Process

from .client import DatajudClient
from .ingestion import sync_processo

logger = logging.getLogger('voyager.datajud.jobs')

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
    queue = django_rq.get_queue('datajud')
    if len(queue) >= DATAJUD_REFILL_HIGH_WATER:
        msg = f'skip (fila com {len(queue):,} jobs ≥ {DATAJUD_REFILL_HIGH_WATER:,})'
        logger.info('reabastecer_fila_datajud: %s', msg)
        return {'skipped': msg}

    capacidade = DATAJUD_REFILL_HIGH_WATER - len(queue)
    a_enfileirar = min(capacidade, DATAJUD_REFILL_BATCH)
    pids = list(
        Process.objects.filter(
            tribunal__ativo=True,
            data_enriquecimento_datajud__isnull=True,
        ).values_list('pk', flat=True)[:a_enfileirar]
    )
    # Enqueue explícito na queue 'datajud' (não usa .delay()) — mesmo padrão
    # do reabastecer_filas_enriquecimento, defensivo contra alguém mudar o
    # decorator do datajud_sync_bulk no futuro.
    enfileirados = 0
    for pid in pids:
        try:
            queue.enqueue(datajud_sync_bulk, pid, job_timeout=600)
            enfileirados += 1
        except Exception as exc:
            logger.warning('falha ao enfileirar datajud bulk pid=%d: %s', pid, exc)
    logger.info('reabastecer_fila_datajud: %d enfileirados', enfileirados)
    return {'enfileirados': enfileirados}
