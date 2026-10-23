"""Job RQ da fila oficial de precatórios (cron da madrugada)."""
from __future__ import annotations

from django.conf import settings
from django_rq import job

from filas_oficiais.execucao import executar


# Fila `diarios`: a das portas além do DJEN (2 réplicas, quase ociosa — o
# diário próprio roda com orçamento de 8 cadernos/24 h). Na `default` (2
# workers) a coleta de ~2 h seguraria metade dos watchdogs; na `djen_backfill`
# esperaria atrás de até 8.000 dias de backfill.
@job('diarios', timeout=6 * 3600)
def coletar_fila_oficial(sigla: str = 'TJAL') -> dict:
    """Coleta a fila oficial e descobre os precatórios pendentes (escreve).

    `retomar=True`: se o job morrer no meio, o reenvio do mesmo dia pula as
    entidades já gravadas.
    """
    return executar(sigla, escrever=True, retomar=True,
                    com_enricher=getattr(settings, 'FILA_OFICIAL_COM_ENRICHER', True))
