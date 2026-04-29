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
def enriquecer_processo(process_id: int, prefer_cortex: bool = False,
                         direct_apply: bool = False) -> dict:
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    enricher = cls(prefer_cortex=prefer_cortex)
    return enricher.enriquecer(p, direct_apply=direct_apply)


def enqueue_enriquecimento(process_id: int, tribunal_sigla: str):
    """Enfileira na queue do tribunal — paraleliza coletas sem misturar pools."""
    queue = django_rq.get_queue(queue_for(tribunal_sigla))
    return queue.enqueue(enriquecer_processo, process_id, job_timeout=ENRICH_TIMEOUT)


def enqueue_enriquecimento_manual(process_id: int):
    """Enfileira na queue 'manual' — prioritária pra cliques na UI.

    Bypassa as filas per-tribunal (que podem ter centenas de milhares de
    jobs). Passa `prefer_cortex=True` pro enricher tentar o proxy
    residencial primeiro — click do user retorna em ~1-3s em vez de
    rotacionar proxies queimados por 30s+.
    """
    queue = django_rq.get_queue('manual')
    return queue.enqueue(
        enriquecer_processo, process_id,
        kwargs={'prefer_cortex': True, 'direct_apply': True},
        job_timeout=ENRICH_TIMEOUT,
    )


# Buffer pra workers nunca esperarem: com 600+ workers (300 .30 + 300 .177)
# por tribunal a ~5-10s/job, queima ~60-120/s = ~7-14k/min. 100k high-water
# garante 7-14min de folga entre refills do scheduler (que roda a cada 2min).
ENQUEUE_BATCH_SIZE = 10_000
QUEUE_HIGH_WATER = 100_000  # se já tem isso na fila, não re-enfileira


@job('default', timeout=300)
def reabastecer_filas_enriquecimento() -> dict:
    """Cron: pra cada tribunal com enricher, enfileira até ENQUEUE_BATCH_SIZE
    Process pendentes — desde que a fila não esteja já cheia.

    Resolve o problema de o `enriquecer_pendentes` ficar dependente de
    sessão/tty (morre em restart). Aqui é stateless: roda, enfileira o
    que cabe, termina. Se o scheduler restart, na próxima invocação
    retoma. Idempotente — sempre filtra `status=pendente`.
    """
    from tribunals.models import Process

    relatorio = {}
    for sigla in _ENRICHERS.keys():
        queue = django_rq.get_queue(queue_for(sigla))
        if len(queue) >= QUEUE_HIGH_WATER:
            relatorio[sigla] = f'skip (fila com {len(queue):,} jobs ≥ {QUEUE_HIGH_WATER})'
            continue
        capacidade = QUEUE_HIGH_WATER - len(queue)
        a_enfileirar = min(capacidade, ENQUEUE_BATCH_SIZE)
        ids = list(
            Process.objects.filter(
                tribunal_id=sigla,
                enriquecimento_status=Process.ENRIQ_PENDENTE,
            ).values_list('pk', flat=True)[:a_enfileirar]
        )
        for pid in ids:
            try:
                queue.enqueue(enriquecer_processo, pid, job_timeout=ENRICH_TIMEOUT)
            except Exception as exc:
                logger.warning('falha ao enfileirar', extra={'pid': pid, 'erro': str(exc)})
        relatorio[sigla] = len(ids)
    logger.info('reabastecer_filas_enriquecimento', extra=relatorio)
    return relatorio
