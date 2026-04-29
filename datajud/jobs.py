"""Jobs RQ pra ingestão Datajud."""
import logging

from django_rq import job

from tribunals.models import Process

from .client import DatajudClient
from .ingestion import sync_processo

logger = logging.getLogger('voyager.datajud.jobs')


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
