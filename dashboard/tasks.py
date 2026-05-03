"""Jobs RQ do dashboard — executados pelos workers, enfileirados pelo scheduler.

Arquitetura: 6 jobs warm independentes na fila `warm` (worker dedicado em .30).
Cada job tem lock próprio + statement_timeout SQL — falha de um não bloqueia
os outros, e queries pesadas cancelam ao invés de travar o pipeline.
"""
import logging
import time

from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django_rq import job

from . import queries

logger = logging.getLogger('voyager.dashboard.tasks')

# Períodos pré-aquecidos. Apenas [None, 7] na home — outros computam on-demand.
_PERIODOS = [None, 7]
# Janelas da velocidade de ingestão (horas)
_HORAS = [24, 48, 72]

_WARM_TTL = 7200  # 2h


def _reset_connection():
    """Garante cursor limpo: query anterior cancelada deixa cursor 'busy'."""
    close_old_connections()
    try:
        connection.connection and connection.connection.cancel()
    except Exception:
        pass


def _with_timeout(timeout_s: int, fn):
    """Executa fn() dentro de transação com SET LOCAL statement_timeout.

    pgbouncer transaction-mode descarta SET statement_timeout entre queries
    (cada cursor.execute pode ir pra conexão diferente). SET LOCAL dentro
    de transaction.atomic() vincula o timeout a TODA query da transação,
    garantindo que pesadas (GROUP BY em 30M rows) abortem em vez de travar.
    """
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
        fn()


def _with_lock(lock_key: str, ttl: int, fn):
    """Executa fn() sob lock Redis + reset de conexão. Idempotente."""
    if not cache.add(lock_key, '1', timeout=ttl):
        logger.info('%s: skip (lock held)', lock_key)
        return
    try:
        _reset_connection()
        fn()
    except Exception as e:
        logger.warning('%s: %s', lock_key, e)
        try:
            connection.close()
        except Exception:
            pass
    finally:
        cache.delete(lock_key)


# Workers snapshot — INLINE no scheduler thread, sem RQ. É leve (só lê Redis)
# e fazia pile-up na fila default quando workers ficavam ocupados.
def warm_workers_cache_inline():
    try:
        queries.compute_workers_snapshot()
    except Exception as e:
        logger.warning('warm_workers_cache_inline: %s', e)


@job('warm', timeout=2400)
def warm_kpis():
    """KPIs globais (None + 7d). compute_kpis_globais faz vários COUNT em
    30M+ rows. timeout 600s/período: queries tomam 1-3min em DB lento."""
    def _run():
        for dias in _PERIODOS:
            try:
                _with_timeout(600, lambda d=dias: queries.compute_kpis_globais(dias=d, tribunais=None))
            except Exception as e:
                logger.warning('warm_kpis dias=%s: %s', dias, e)
                _reset_connection()
    _with_lock('lock:warm_kpis', 2700, _run)


@job('warm', timeout=5400)
def warm_charts():
    """Pré-aquece charts da home (volume-temporal, top_*, etc).

    NÃO inclui ingestao-por-hora (job próprio, lê de MV). 7 charts × 2
    períodos = 14 queries; timeout 300s/each ⇒ 4200s pior caso. RQ
    horse 5400s (90min) cobre. Falha de uma não bloqueia outras.
    """
    def _run():
        from .views import _CHART_HANDLERS, _chart_cache_key
        for dias in _PERIODOS:
            for chart_key, handler in _CHART_HANDLERS.items():
                if chart_key == 'ingestao-por-hora':
                    continue
                try:
                    def _go(c=chart_key, d=dias, h=handler):
                        data = h(d, [], None)
                        cache.set(_chart_cache_key(c, d, []), data, timeout=_WARM_TTL)
                    _with_timeout(300, _go)
                except Exception as e:
                    logger.warning('warm_charts %s/d=%s: %s', chart_key, dias, e)
                    _reset_connection()
    _with_lock('lock:warm_charts', 5700, _run)


@job('warm', timeout=180)
def warm_ingestao_por_hora():
    """Velocidade de ingestão (lê da MV mv_ingestion_rate_hora)."""
    def _run():
        for horas in _HORAS:
            try:
                def _go(h=horas):
                    data = queries.ingestion_rate_por_hora(horas=h)
                    cache.set(f'chart:ingestao-por-hora:h={h}', data, timeout=_WARM_TTL)
                _with_timeout(60, _go)
            except Exception as e:
                logger.warning('warm_ingestao_por_hora h=%s: %s', horas, e)
                _reset_connection()
    _with_lock('lock:warm_ingestao_por_hora', 300, _run)


@job('warm', timeout=120)
def warm_partes():
    """Distribuição de tipos de partes (/dashboard/partes/)."""
    _with_lock('lock:warm_partes', 300,
               lambda: _with_timeout(60, queries.distribuicao_tipos_partes))


@job('warm', timeout=1500)
def warm_estatisticas_tribunal():
    """Estatísticas por tribunal (/dashboard/tribunais/). GROUP BY em 30M+ movs."""
    _with_lock('lock:warm_estatisticas_tribunal', 1800,
               lambda: _with_timeout(900, queries.compute_estatisticas_por_tribunal))


@job('warm', timeout=900)
def warm_filtros_movimentacoes():
    """Top tipos/meios/classes pra facetas de /movimentacoes/."""
    _with_lock('lock:warm_filtros_movimentacoes', 1200,
               lambda: _with_timeout(600, queries.compute_filtros_movimentacoes))


@job('warm', timeout=600)
def refresh_materialized_views():
    """REFRESH MATERIALIZED VIEW CONCURRENTLY. Cron diário, NÃO no warm path.

    `lock_timeout` PG aborta se outro REFRESH segura lock — evita empilhar
    (observado 11 REFRESH bloqueados crashou o postmaster).
    """
    def _run():
        with connection.cursor() as cur:
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("SET statement_timeout = '600s'")
            for mv in ('mv_volume_diario', 'mv_ingestion_rate_hora'):
                try:
                    cur.execute(f'REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}')
                    logger.info('refresh MV %s ok', mv)
                except Exception as e:
                    logger.warning('refresh MV %s: %s', mv, e)
                    _reset_connection()
                    _set_statement_timeout(600)
                    cur.execute("SET lock_timeout = '5s'")
    _with_lock('lock:refresh_mv', 1800, _run)
