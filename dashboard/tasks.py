"""Jobs RQ do dashboard — executados pelos workers, enfileirados pelo scheduler."""
import logging

from django.core.cache import cache
from django_rq import job

from . import queries

logger = logging.getLogger('voyager.dashboard.tasks')

# Períodos pré-aquecidos. Reduzido de [None, 7, 30, 90, 365] pra [None, 7]:
# os outros multiplicavam queries pesadas em 30M+ rows e travavam o warm.
# Períodos fora desses são computados on-demand pelo handler quando o filtro
# é aplicado — caminho frio aceitável pra clicks raros.
_PERIODOS = [None, 7]
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
    # Lock TTL 600s (10min) — antes era 3500s (58min) e órfão de worker
    # morto deixava warm parado por quase 1h. Cron 5min, então 10min cobre
    # 2 ciclos. Se warm precisar mais que isso, a query estourou timeout
    # e queremos que próximo ciclo tente de novo.
    if not cache.add(lock_key, '1', timeout=600):
        logger.info('warm_dashboard_all: skip (lock held)')
        return
    import time
    from django.db import connection, close_old_connections

    def _safe(label, fn, timeout_s=60):
        # Reseta a conexão antes de cada step. Se uma query anterior foi
        # cancelada (timeout, OOM, kill), o cursor fica em estado "busy" e
        # a próxima erra com "another command is already in progress",
        # poisonando o resto do warm. close_old_connections + cancel garante
        # cursor limpo a cada step.
        # statement_timeout SQL aborta a query se demorar — sem ele uma
        # única query pesada (COUNT em 30M rows) trava 5+ min e o lock
        # bloqueia o próximo ciclo do cron.
        try:
            close_old_connections()
            try:
                connection.connection and connection.connection.cancel()
            except Exception:
                pass
            with connection.cursor() as cur:
                cur.execute(f"SET statement_timeout = '{int(timeout_s)}s'")
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
            _safe(f'kpis d={dias}',
                  lambda d=dias: queries.compute_kpis_globais(dias=d, tribunais=None),
                  timeout_s=90)

        from .views import _CHART_HANDLERS, _chart_cache_key
        for dias in _PERIODOS:
            for chart_key, handler in _CHART_HANDLERS.items():
                if chart_key == 'ingestao-por-hora':
                    continue
                def _chart(c=chart_key, d=dias, h=handler):
                    data = h(d, [], None)
                    cache.set(_chart_cache_key(c, d, []), data, timeout=_WARM_TTL)
                _safe(f'chart {chart_key}/d={dias}', _chart, timeout_s=90)
        for horas in _HORAS:
            def _ingest(h=horas):
                data = queries.ingestion_rate_por_hora(horas=h)
                cache.set(f'chart:ingestao-por-hora:h={h}', data, timeout=_WARM_TTL)
            _safe(f'ingestao-por-hora h={horas}', _ingest, timeout_s=60)

        _safe('partes', queries.distribuicao_tipos_partes, timeout_s=60)
        _safe('estatisticas', queries.compute_estatisticas_por_tribunal, timeout_s=180)
        _safe('filtros_movs', queries.compute_filtros_movimentacoes, timeout_s=60)

        logger.info('warm_dashboard_all: concluído em %.1fs', time.time() - started)
    finally:
        cache.delete(lock_key)
