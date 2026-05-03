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
    from django.db import connection, close_old_connections

    def _safe(label, fn):
        # Reseta a conexão antes de cada step. Se uma query anterior foi
        # cancelada (timeout, OOM, kill), o cursor fica em estado "busy" e
        # a próxima erra com "another command is already in progress",
        # poisonando o resto do warm. close_old_connections + cancel garante
        # cursor limpo a cada step.
        try:
            close_old_connections()
            try:
                connection.connection and connection.connection.cancel()
            except Exception:
                pass
            fn()
        except Exception as e:
            logger.warning('warm_dashboard_all: %s: %s', label, e)
            try:
                connection.close()
            except Exception:
                pass

    started = time.time()
    try:
        # REFRESH MV está DESABILITADO temporariamente — empilhava locks no
        # PG e derrubava o postmaster ao tentar matar (observado 2x crash).
        # Os caches dependentes (volume_temporal, ingestao_por_hora) lerão
        # da MV com dados levemente atrasados — preferível a derrubar tudo.
        # Deve ser feito por job separado com nice scheduling/timeout.

        for dias in _PERIODOS:
            _safe(f'kpis d={dias}', lambda d=dias: queries.compute_kpis_globais(dias=d, tribunais=None))

        from .views import _CHART_HANDLERS, _chart_cache_key
        for dias in _PERIODOS:
            for chart_key, handler in _CHART_HANDLERS.items():
                if chart_key == 'ingestao-por-hora':
                    continue
                def _chart(c=chart_key, d=dias, h=handler):
                    data = h(d, [], None)
                    cache.set(_chart_cache_key(c, d, []), data, timeout=_WARM_TTL)
                _safe(f'chart {chart_key}/d={dias}', _chart)
        for horas in _HORAS:
            def _ingest(h=horas):
                data = queries.ingestion_rate_por_hora(horas=h)
                cache.set(f'chart:ingestao-por-hora:h={h}', data, timeout=_WARM_TTL)
            _safe(f'ingestao-por-hora h={horas}', _ingest)

        _safe('partes', queries.distribuicao_tipos_partes)
        _safe('estatisticas', queries.compute_estatisticas_por_tribunal)
        _safe('filtros_movs', queries.compute_filtros_movimentacoes)

        logger.info('warm_dashboard_all: concluído em %.1fs', time.time() - started)
    finally:
        cache.delete(lock_key)
