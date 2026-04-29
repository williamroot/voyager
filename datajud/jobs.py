"""Jobs RQ pra ingestão Datajud."""
from django_rq import job

from tribunals.models import Process

from .client import DatajudClient
from .ingestion import sync_processo


@job('manual', timeout=300)
def datajud_sincronizar_processo(process_id: int, prefer_cortex: bool = True) -> dict:
    """Sincroniza um processo via Datajud — usado pelo botão UI.

    `prefer_cortex=True` por default: cliente vai pra Cortex primeiro,
    porque é click manual com user esperando. Backfill em massa que
    chamar isso direto pode passar False.
    """
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    client = DatajudClient(prefer_cortex=prefer_cortex)
    return sync_processo(p, client=client)


@job('djen_backfill', timeout=600)
def datajud_sync_bulk(process_id: int) -> dict:
    """Versão bulk pra auto-enqueue durante backfill (não usado por
    default, deixa pendurado pra ativar quando quiser cobertura total
    de movs via Datajud em background)."""
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    client = DatajudClient(prefer_cortex=False)
    return sync_processo(p, client=client)
