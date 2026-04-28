"""Jobs RQ do dashboard — executados pelos workers, enfileirados pelo scheduler."""
import logging

from django.core.cache import cache
from django_rq import job

from . import queries

logger = logging.getLogger('voyager.dashboard.tasks')

# Períodos pré-aquecidos (dias=None = todo o período)
_PERIODOS = [None, 7, 30, 90, 365]
# Janelas da velocidade de ingestão (horas)
_HORAS = [24, 48, 72]


@job('default', timeout=300)
def warm_chart_cache():
    """Pré-aquece o cache Redis de todos os charts da home para cada período.

    Executado a cada 5 min pelo APScheduler — garante que o usuário sempre
    bate no cache (TTL 1h), nunca em query fria.
    """
    # Import aqui para evitar circular (views importa queries, jobs importa views)
    from .views import _CHART_HANDLERS, _chart_cache_key

    warmed = errors = 0

    for dias in _PERIODOS:
        for chart_key, handler in _CHART_HANDLERS.items():
            if chart_key == 'ingestao-por-hora':
                continue  # aquecido separadamente abaixo
            try:
                data = handler(dias, [], None)
                cache.set(_chart_cache_key(chart_key, dias, []), data, timeout=3600)
                warmed += 1
            except Exception as e:
                logger.warning('warm_chart_cache %s/d=%s: %s', chart_key, dias, e)
                errors += 1

    for horas in _HORAS:
        try:
            data = queries.ingestion_rate_por_hora(horas=horas)
            cache.set(f'chart:ingestao-por-hora:h={horas}', data, timeout=3600)
            warmed += 1
        except Exception as e:
            logger.warning('warm_chart_cache ingestao-por-hora/h=%s: %s', horas, e)
            errors += 1

    logger.info('warm_chart_cache: %d aquecidos, %d erros', warmed, errors)
