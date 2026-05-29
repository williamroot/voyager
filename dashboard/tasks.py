"""Jobs RQ do dashboard — executados pelos workers, enfileirados pelo scheduler.

Arquitetura: 6 jobs warm independentes na fila `warm` (worker dedicado em .30).
Cada job tem lock próprio + statement_timeout SQL — falha de um não bloqueia
os outros, e queries pesadas cancelam ao invés de travar o pipeline.
"""
import logging
import time

from django.core.cache import cache
from django.db import close_old_connections, connection, connections, transaction
from django_rq import job

from . import queries

logger = logging.getLogger('voyager.dashboard.tasks')

# Períodos pré-aquecidos. Apenas [None, 7] na home — outros computam on-demand.
_PERIODOS = [None, 7]
# Janelas da velocidade de ingestão (horas)
_HORAS = [24, 48, 72]

_WARM_TTL = 604800  # 7 dias - charts pesados podem timeoutar; dados stale e melhor que MISS


def _reset_connection(using: str = 'default'):
    """Garante cursor limpo: query anterior cancelada deixa cursor 'busy'."""
    close_old_connections()
    try:
        conn = connections[using]
        conn.connection and conn.connection.cancel()
    except Exception:
        pass


def _with_timeout(timeout_s: int, fn, using: str = 'default'):
    """Executa fn() dentro de transação com SET LOCAL statement_timeout.

    pgbouncer transaction-mode descarta SET statement_timeout entre queries
    (cada cursor.execute pode ir pra conexão diferente). SET LOCAL dentro
    de transaction.atomic() vincula o timeout a TODA query da transação,
    garantindo que pesadas (GROUP BY em 30M rows) abortem em vez de travar.

    `using`: roteia pra outro database alias (ex: 'replica' pra read-only).
    """
    with transaction.atomic(using=using):
        with connections[using].cursor() as cur:
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
    except BaseException as e:
        logger.warning('%s: abortado (%s: %s)', lock_key, type(e).__name__, e)
        try:
            connection.close()
        except Exception:
            pass
        raise
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
    187M+ rows (Movimentacao.count() é o mais caro). Roteado pra replica.

    Timeout 1800s/period: empiricamente kpis_None levou 22min em cold cache
    da replica; 1800s = 30min cobre folga. Statement_timeout do PG aborta
    individuais — total job pode levar até 60min.
    """
    def _run():
        for dias in _PERIODOS:
            try:
                _with_timeout(1800,
                    lambda d=dias: queries.compute_kpis_globais(dias=d, tribunais=None))
            except Exception as e:
                logger.warning('warm_kpis dias=%s: %s', dias, e)
                _reset_connection()
    _with_lock('lock:warm_kpis', 2700, _run)


# Charts leves (filtros temporais que limitam IO).
_CHARTS_LEVES = ('classes', 'enriquecimento', 'sparkline-24h')
# Charts pesados (GROUP BY em 187M+ rows tribunals_movimentacao).
_CHARTS_PESADOS = ('volume-temporal', 'distribuicao', 'tipos', 'orgaos', 'meios')


@job('warm', timeout=2400)
def warm_charts_leves():
    """Charts rápidos (filtros temporais). 3 charts × 2 períodos = 6 queries;
    timeout 300s/each. Esses populam de forma confiável a cada cycle.
    """
    def _run():
        from .views import _CHART_HANDLERS, _chart_cache_key
        for dias in _PERIODOS:
            for chart_key in _CHARTS_LEVES:
                handler = _CHART_HANDLERS.get(chart_key)
                if not handler:
                    continue
                try:
                    def _go(c=chart_key, d=dias, h=handler):
                        data = h(d, [], None)
                        cache.set(_chart_cache_key(c, d, []), data, timeout=_WARM_TTL)
                    _with_timeout(300, _go)
                except Exception as e:
                    logger.warning('warm_charts_leves %s/d=%s: %s', chart_key, dias, e)
                    _reset_connection()
    _with_lock('lock:warm_charts_leves', 2700, _run)


@job('warm', timeout=14400)
def warm_charts_pesados():
    """Charts com GROUP BY pesado em 187M+ rows (volume-temporal, distribuicao,
    tipos, orgaos, meios). Cada um leva 5-30min sem MV. timeout 1800s/each.
    Roda em job separado pra não bloquear charts leves.
    """
    def _run():
        from .views import _CHART_HANDLERS, _chart_cache_key
        for dias in _PERIODOS:
            for chart_key in _CHARTS_PESADOS:
                handler = _CHART_HANDLERS.get(chart_key)
                if not handler:
                    continue
                try:
                    def _go(c=chart_key, d=dias, h=handler):
                        data = h(d, [], None)
                        cache.set(_chart_cache_key(c, d, []), data, timeout=_WARM_TTL)
                    _with_timeout(1800, _go)
                except Exception as e:
                    logger.warning('warm_charts_pesados %s/d=%s: %s', chart_key, dias, e)
                    _reset_connection()
    _with_lock('lock:warm_charts_pesados', 14700, _run)


# Períodos do period-picker da /dashboard/leads/ (7d/30d/90d/1ano).
_LEADS_PERIODOS = [7, 30, 90, 365]


@job('warm', timeout=2400)
def warm_leads_charts():
    """Pré-aquece os widgets da /dashboard/leads/ no filtro default
    (sem tribunal, cliente 'juriscope') × períodos do picker.

    Antes só havia cache lazy de 5min sem warm: a cada expiração a
    próxima visita pagava queries pesadas (Count em Process,
    ClassificacaoLog, anti-join Exists de LeadConsumption) e a página
    ficava presa em 'ACQUIRING SIGNAL'. Mesmo padrão de warm_charts_pesados.
    """
    def _run():
        from .views import LEADS_CHART_KEYS, compute_leads_chart, leads_cache_key
        for dias in _LEADS_PERIODOS:
            for ck in LEADS_CHART_KEYS:
                try:
                    def _go(c=ck, d=dias):
                        data = compute_leads_chart(c, None, None, d, 'juriscope')
                        cache.set(leads_cache_key(c, None, None, d, 'juriscope'),
                                  data, timeout=_WARM_TTL)
                    _with_timeout(1800, _go)
                except Exception as e:
                    logger.warning('warm_leads_charts %s/d=%s: %s', ck, dias, e)
                    _reset_connection()
    _with_lock('lock:warm_leads_charts', 2700, _run)


@job('warm', timeout=180)
def warm_ingestao_por_hora():
    """Velocidade de ingestão (lê da MV mv_ingestion_rate_hora pro cache).

    Só LÊ a MV (rápido — tabela de ~poucas centenas de linhas) e cacheia por
    janela. Quem dá REFRESH na MV é `refresh_ingestion_rate_hora` (dedicado).
    """
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


@job('warm', timeout=2400)
def refresh_ingestion_rate_hora():
    """REFRESH dedicado da MV mv_ingestion_rate_hora (janela 4d).

    Separado do `refresh_materialized_views` diário: o gráfico "Velocidade de
    ingestão" é janela rolante de 24-72h, então a MV precisa de refresh
    frequente (~30min), não 1x/dia — senão fica vazia perto do horário do
    refresh e some por dias quando o scan estoura o timeout (incidente
    2026-05-28). Roda com lock próprio pra não competir com os 3 MVs pesados
    do job diário.

    CONCURRENTLY exige MV já populada; logo após o DROP/CREATE WITH NO DATA
    da migration 0034 o 1º refresh cai pro modo não-concorrente (toma
    ACCESS EXCLUSIVE só nessa primeira vez).
    """
    def _run():
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '10s'")
                cur.execute("SET statement_timeout = '1800s'")
                cur.execute(
                    "SELECT relispopulated FROM pg_class "
                    "WHERE relname = 'mv_ingestion_rate_hora'")
                row = cur.fetchone()
                populated = bool(row[0]) if row else False
                concurrently = 'CONCURRENTLY ' if populated else ''
                cur.execute(
                    f'REFRESH MATERIALIZED VIEW {concurrently}mv_ingestion_rate_hora')
            logger.info('refresh MV mv_ingestion_rate_hora ok (concurrently=%s)', populated)
        except Exception as e:
            logger.warning('refresh_ingestion_rate_hora: %s', e)
            _reset_connection()
    _with_lock('lock:refresh_ingestion_rate_hora', 1800, _run)


@job('warm', timeout=900)
def warm_pipeline_diario():
    """REFRESH CONCURRENTLY mv_pipeline_diario — intraday, hoje/ontem fresco."""
    def _run():
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("SET statement_timeout = '600s'")
                cur.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario')
            logger.info('refresh MV mv_pipeline_diario ok (warm)')
        except Exception as e:
            logger.warning('warm_pipeline_diario: %s', e)
            _reset_connection()
    _with_lock('lock:warm_pipeline_diario', 900, _run)


@job('warm', timeout=1200)
def warm_partes():
    """Distribuição de tipos de partes (/dashboard/partes/)."""
    _with_lock('lock:warm_partes', 900,
               lambda: _with_timeout(600, queries.compute_distribuicao_tipos_partes))


@job('warm', timeout=7200)
def warm_estatisticas_tribunal():
    """Estatísticas por tribunal (/dashboard/tribunais/). GROUP BY em 30M+ movs."""
    def _run():
        _with_timeout(3600, queries.compute_estatisticas_por_tribunal)
    _with_lock('lock:warm_estatisticas_tribunal', 7500, _run)


@job('warm', timeout=3600)
def warm_tribunal_status():
    """Status/linha do tempo por tribunal (/dashboard/tribunais/status/).

    GROUP BY TruncMonth em ~30M+ movs + split_part(numero_cnj) em Process,
    cobrindo todos os tribunais ativos numa passada. Roda só no warm.
    """
    def _run():
        _with_timeout(1800, queries.compute_tribunal_status)
    _with_lock('lock:warm_tribunal_status', 3900, _run)


@job('warm', timeout=7200)
def warm_filtros_movimentacoes():
    """Top tipos/meios/classes pra facetas de /movimentacoes/."""
    def _run():
        _with_timeout(3600, queries.compute_filtros_movimentacoes)
    _with_lock('lock:warm_filtros_movimentacoes', 7500, _run)


@job('warm', timeout=7200)
def refresh_materialized_views():
    """REFRESH MATERIALIZED VIEW CONCURRENTLY. Cron diário, NÃO no warm path.

    `lock_timeout` PG aborta se outro REFRESH segura lock — evita empilhar
    (observado 11 REFRESH bloqueados crashou o postmaster).

    `mv_ingestion_rate_hora` saiu daqui (2026-05-28): tem refresh dedicado e
    frequente em `refresh_ingestion_rate_hora` — um gráfico rolante de 24h
    não pode depender de refresh diário.
    """
    def _run():
        for mv in ('mv_volume_diario', 'mv_pipeline_diario', 'mv_tribunal_kpis'):
            try:
                with connection.cursor() as cur:
                    cur.execute("SET lock_timeout = '5s'")
                    cur.execute("SET statement_timeout = '3600s'")
                    cur.execute(f'REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}')
                logger.info('refresh MV %s ok', mv)
            except Exception as e:
                logger.warning('refresh MV %s: %s', mv, e)
                _reset_connection()
    _with_lock('lock:refresh_mv', 7200, _run)
