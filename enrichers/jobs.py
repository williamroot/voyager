import logging

from django_rq import job

from tribunals.models import Process

from .trf1 import Trf1Enricher

logger = logging.getLogger('voyager.enrichers.jobs')


_ENRICHERS = {
    'TRF1': Trf1Enricher,
}


@job('default', timeout=300)
def enriquecer_processo(process_id: int) -> dict:
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    enricher = cls()
    return enricher.enriquecer(p)
