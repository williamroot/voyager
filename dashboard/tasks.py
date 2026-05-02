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


_WARM_TTL = 7200  # 2h — TTL longo evita "cache vazio" se warm falhar
# em sequência (DB lento, OOM, restart). Cron renova a cada 5min.


@job('default', timeout=600)
def warm_chart_cache():
    """Pré-aquece todos os charts da home (TTL 30min). Hot-path NUNCA computa.

    Lock Redis impede pile-up se DB lento prolongar a execução.
    """
    lock_key = 'lock:warm_chart_cache'
    if not cache.add(lock_key, '1', timeout=660):
        logger.info('warm_chart_cache: skip (lock held)')
        return
    try:
        from .views import _CHART_HANDLERS, _chart_cache_key
        warmed = errors = 0
        for dias in _PERIODOS:
            for chart_key, handler in _CHART_HANDLERS.items():
                if chart_key == 'ingestao-por-hora':
                    continue
                try:
                    data = handler(dias, [], None)
                    cache.set(_chart_cache_key(chart_key, dias, []), data, timeout=_WARM_TTL)
                    warmed += 1
                except Exception as e:
                    logger.warning('warm_chart_cache %s/d=%s: %s', chart_key, dias, e)
                    errors += 1
        for horas in _HORAS:
            try:
                data = queries.ingestion_rate_por_hora(horas=horas)
                cache.set(f'chart:ingestao-por-hora:h={horas}', data, timeout=_WARM_TTL)
                warmed += 1
            except Exception as e:
                logger.warning('warm_chart_cache ingestao-por-hora/h=%s: %s', horas, e)
                errors += 1
        logger.info('warm_chart_cache: %d aquecidos, %d erros', warmed, errors)
    finally:
        cache.delete(lock_key)


@job('default', timeout=600)
def warm_kpis_cache():
    """Pré-aquece KPIs da home no cache (TTL 30min). Hot-path NUNCA computa.

    Executado a cada 5 min. Lock Redis impede pile-up — se uma execução
    ainda roda, a próxima pula. Sem isso, em DB lento o RQ enfileiraria
    múltiplas instâncias e amplificaria a carga.
    """
    from django.core.cache import cache
    lock_key = 'lock:warm_kpis_cache'
    if not cache.add(lock_key, '1', timeout=660):  # 9min — < 5min interval × 2
        logger.info('warm_kpis_cache: skip (lock held)')
        return
    try:
        errors = 0
        for dias in _PERIODOS:
            try:
                queries.compute_kpis_globais(dias=dias, tribunais=None)
            except Exception as e:
                logger.warning('warm_kpis_cache dias=%s: %s', dias, e)
                errors += 1
        logger.info('warm_kpis_cache: concluído, %d erros', errors)
    finally:
        cache.delete(lock_key)


@job('default', timeout=1200)
def warm_estatisticas_tribunal():
    """Pré-aquece /dashboard/tribunais/. Query GROUP BY em ~30M movs.

    Observado: compute leva ~270s sob carga normal (workers Datajud
    inserindo). Timeout 1200s + lock 1100s dão margem de 4× pra picos.
    """
    lock_key = 'lock:warm_estatisticas_tribunal'
    if not cache.add(lock_key, '1', timeout=1100):
        logger.info('warm_estatisticas_tribunal: skip (lock held)')
        return
    try:
        queries.compute_estatisticas_por_tribunal()
        logger.info('warm_estatisticas_tribunal: ok')
    except Exception as e:
        logger.warning('warm_estatisticas_tribunal: %s', e)
    finally:
        cache.delete(lock_key)


@job('default', timeout=120)
def warm_partes_cache():
    """Pré-aquece /dashboard/partes/. Lock impede pile-up em DB lento."""
    lock_key = 'lock:warm_partes_cache'
    if not cache.add(lock_key, '1', timeout=660):
        logger.info('warm_partes_cache: skip (lock held)')
        return
    try:
        queries.distribuicao_tipos_partes()
    except Exception as e:
        logger.warning('warm_partes_cache: %s', e)
    finally:
        cache.delete(lock_key)


@job('default', timeout=600)
def refresh_materialized_views():
    """REFRESH MATERIALIZED VIEW CONCURRENTLY pras MVs do dashboard.

    Executa a cada 5min. CONCURRENTLY garante que reads não bloqueiam.
    Lock impede pile-up se REFRESH demorar (75M rows na origem da MV).
    """
    from django.db import connection
    lock_key = 'lock:refresh_mv'
    if not cache.add(lock_key, '1', timeout=540):
        logger.info('refresh_materialized_views: skip (lock held)')
        return
    try:
        with connection.cursor() as cur:
            for mv in ('mv_volume_diario', 'mv_ingestion_rate_hora'):
                try:
                    cur.execute(f'REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}')
                    logger.info('refresh MV %s ok', mv)
                except Exception as e:
                    logger.warning('refresh MV %s falhou: %s', mv, e)
    finally:
        cache.delete(lock_key)


def warm_workers_cache_inline():
    """Snapshot de workers/filas — versão inline (rodada DIRETO no thread
    do scheduler, sem RQ). É leve (só lê Redis) e fazia pile-up na fila
    default quando workers ficavam presos no warm_dashboard_all pesado.
    APScheduler `max_instances=1 + coalesce=True` já garante uma execução
    por vez — sem necessidade de lock Redis.
    """
    try:
        queries.compute_workers_snapshot()
    except Exception as e:
        logger.warning('warm_workers_cache_inline: %s', e)


@job('default', timeout=3600)
def warm_dashboard_all():
    """Executa TODOS os warms do dashboard em sequência sob 1 lock global.

    Bloqueante: refresh MV → kpis → charts → partes → estatísticas. Sem
    paralelismo entre eles — evita 5 jobs concorrendo no mesmo PG e
    inflando contention. Cron único de 5min substitui os schedules
    individuais antigos. Workers snapshot fica fora (cron 30s).
    """
    lock_key = 'lock:warm_dashboard_all'
    if not cache.add(lock_key, '1', timeout=3500):
        logger.info('warm_dashboard_all: skip (lock held)')
        return
    import time
    from django.db import connection
    started = time.time()
    try:
        for mv in ('mv_volume_diario', 'mv_ingestion_rate_hora'):
            try:
                with connection.cursor() as cur:
                    cur.execute(f'REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}')
                logger.info('warm_dashboard_all: MV %s ok', mv)
            except Exception as e:
                logger.warning('warm_dashboard_all: MV %s falhou: %s', mv, e)

        for dias in _PERIODOS:
            try:
                queries.compute_kpis_globais(dias=dias, tribunais=None)
            except Exception as e:
                logger.warning('warm_dashboard_all: kpis dias=%s: %s', dias, e)

        from .views import _CHART_HANDLERS, _chart_cache_key
        for dias in _PERIODOS:
            for chart_key, handler in _CHART_HANDLERS.items():
                if chart_key == 'ingestao-por-hora':
                    continue
                try:
                    data = handler(dias, [], None)
                    cache.set(_chart_cache_key(chart_key, dias, []), data, timeout=_WARM_TTL)
                except Exception as e:
                    logger.warning('warm_dashboard_all: chart %s/d=%s: %s', chart_key, dias, e)
        for horas in _HORAS:
            try:
                data = queries.ingestion_rate_por_hora(horas=horas)
                cache.set(f'chart:ingestao-por-hora:h={horas}', data, timeout=_WARM_TTL)
            except Exception as e:
                logger.warning('warm_dashboard_all: ingestao-por-hora h=%s: %s', horas, e)

        try:
            queries.distribuicao_tipos_partes()
        except Exception as e:
            logger.warning('warm_dashboard_all: partes: %s', e)

        try:
            queries.compute_estatisticas_por_tribunal()
        except Exception as e:
            logger.warning('warm_dashboard_all: estatisticas: %s', e)

        try:
            queries.compute_filtros_movimentacoes()
        except Exception as e:
            logger.warning('warm_dashboard_all: filtros_movs: %s', e)

        logger.info('warm_dashboard_all: concluído em %.1fs', time.time() - started)
    finally:
        cache.delete(lock_key)
