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


@job('default', timeout=600)
def warm_kpis_cache():
    """Pré-aquece os KPIs da home (kpis_globais) no cache Redis.

    Executado a cada 5 min. Sem isso, a home faz COUNT(*) em 3.5M rows a
    frio — mesmo com índices pode levar alguns segundos; com cache é < 1ms.
    """
    from tribunals.models import Tribunal
    periodos = [None, 7, 30, 90, 365]
    try:
        siglas = list(Tribunal.objects.filter(ativo=True).values_list('sigla', flat=True))
    except Exception:
        siglas = []

    errors = 0
    for dias in periodos:
        try:
            queries.kpis_globais(dias=dias, tribunais=None)
        except Exception as e:
            logger.warning('warm_kpis_cache dias=%s: %s', dias, e)
            errors += 1
    logger.info('warm_kpis_cache: concluído, %d erros', errors)


@job('default', timeout=120)
def warm_partes_cache():
    """Pré-aquece o cache da página /dashboard/partes/.

    A query `distribuicao_tipos_partes` faz GROUP BY em ~1M rows e
    leva ~5s a frio. Sem warm, o primeiro hit após expiração paga o
    custo. TTL de 600s no `cache.set` casa com o intervalo desse job.
    """
    try:
        queries.distribuicao_tipos_partes()
    except Exception as e:
        logger.warning('warm_partes_cache: %s', e)


@job('default', timeout=120)
def warm_workers_cache():
    """Computa e armazena o snapshot de workers/filas no cache Redis.

    Executado a cada 30s pelo APScheduler. O job roda num worker RQ
    (sem timeout de gunicorn), então pode demorar 20-30s com Redis saturado.
    A view status_workers() só lê do cache — nunca computa diretamente.
    """
    queries.compute_workers_snapshot()
