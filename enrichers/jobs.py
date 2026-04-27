import logging

import django_rq
from django_rq import job

from tribunals.models import Process

from .trf1 import Trf1Enricher
from .trf3 import Trf3Enricher

logger = logging.getLogger('voyager.enrichers.jobs')


_ENRICHERS = {
    'TRF1': Trf1Enricher,
    'TRF3': Trf3Enricher,
}

ENRICH_TIMEOUT = 300


def queue_for(tribunal_sigla: str) -> str:
    """Mapeia sigla → nome da fila por tribunal (enrich_trf1, enrich_trf3...)."""
    return f'enrich_{tribunal_sigla.lower()}'


# @job('default') é mantido pra trabalhar como fallback se algo enfileirar
# direto via .delay() sem passar pela queue per-tribunal.
@job('default', timeout=ENRICH_TIMEOUT)
def enriquecer_processo(process_id: int) -> dict:
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    enricher = cls()
    return enricher.enriquecer(p)


def enqueue_enriquecimento(process_id: int, tribunal_sigla: str):
    """Enfileira na queue do tribunal — paraleliza coletas sem misturar pools."""
    queue = django_rq.get_queue(queue_for(tribunal_sigla))
    return queue.enqueue(enriquecer_processo, process_id, job_timeout=ENRICH_TIMEOUT)


def enqueue_enriquecimento_manual(process_id: int):
    """Enfileira na queue 'manual' — prioritária pra cliques na UI.

    Bypassa as filas per-tribunal (que podem ter centenas de milhares de
    jobs). Workers dedicados consomem essa fila, garantindo latência baixa
    pra atualização sob demanda mesmo durante backfill.
    """
    queue = django_rq.get_queue('manual')
    return queue.enqueue(enriquecer_processo, process_id, job_timeout=ENRICH_TIMEOUT)
