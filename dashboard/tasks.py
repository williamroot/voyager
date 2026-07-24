"""Jobs RQ do dashboard — executados pelos workers, enfileirados pelo scheduler.

Arquitetura: 6 jobs warm independentes na fila `warm` (worker dedicado em .30).
Cada job tem lock próprio + statement_timeout SQL — falha de um não bloqueia
os outros, e queries pesadas cancelam ao invés de travar o pipeline.
"""
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection, connections, transaction
from django_rq import job

from . import queries

logger = logging.getLogger('voyager.dashboard.tasks')

# Cache da telemetria da frota de vetorização (Zordon). TTL ~22min: sobrevive a
# 1 falha do warm de 10min sem cair pra MISS na página.
VETOR_FLEET_CACHE_KEY = 'vetor:fleet:v1'
_VETOR_FLEET_TTL = 1300

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
        for mv in ('mv_volume_diario', 'mv_volume_mensal', 'mv_pipeline_diario', 'mv_tribunal_kpis'):
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


def warm_vetorizacao_fleet():
    """Busca a telemetria da frota de vetorização no Zordon e grava no cache.

    Roda INLINE no thread pool do scheduler (a cada 5min) — só faz 1 GET HTTP
    e 1 cache.set, leve. A página /dashboard/vetorizacao/ lê do cache (fast
    path). TTL 11min sobrevive a 1 falha do warm. Nunca propaga exceção:
    numa falha, mantém o valor stale anterior no cache.

    Retorna o dict gravado (ou None em falha) — usado pelo fallback inline da
    view quando o cache está frio.
    """
    base = getattr(settings, 'ZORDON_URL', '').rstrip('/')
    if not base:
        logger.warning('warm_vetorizacao_fleet: ZORDON_URL não configurado')
        return None
    try:
        resp = requests.get(
            f'{base}/api/vetorizacao/fleet',
            timeout=(5, 20),
            headers={'User-Agent': 'voyager-dashboard/vetor-fleet'},
        )
        resp.raise_for_status()
        data = resp.json()
        cache.set(VETOR_FLEET_CACHE_KEY, data, timeout=_VETOR_FLEET_TTL)
        return data
    except Exception as e:
        logger.warning('warm_vetorizacao_fleet: %s: %s', type(e).__name__, e)
        return None


# ---------------------------------------------------------------------------
# Command Center — custo (QuickPod) + histórico 24h de throughput/fila/GPU.
# Roda INLINE no scheduler (leve: 1-2 GETs à API QuickPod + read-modify-write
# do ring de snapshots no cache). NUNCA no request do browser.
# ---------------------------------------------------------------------------

# Custo QuickPod — crédito, burn/h, runway, nº pods.
COMMAND_CUSTO_CACHE_KEY = 'command:custo:v1'
_COMMAND_CUSTO_TTL = 600  # sobrevive a ~10 falhas do warm de 60s

# Ring de snapshots 24h (throughput embed/extração/classif, fila, GPU util média).
COMMAND_HIST_CACHE_KEY = 'command:hist:v1'
_COMMAND_HIST_TTL = 172800   # 48h de folga
_COMMAND_HIST_WINDOW = 86400  # janela exibida: 24h
_COMMAND_HIST_MAX = 600       # teto de pontos (24h @ ~1 ponto/2,4min)

# Infra / vector store (Prometheus da Fase B). Fonte + PromQL exatos em
# .ia/OPS.md "Observability stack (Fase B)". Prometheus em zordon:9490.
COMMAND_INFRA_CACHE_KEY = 'command:infra:v1'
_COMMAND_INFRA_TTL = 600  # sobrevive a ~10 falhas do warm de 60s
# O índice HNSW que morde é o halfvec (38GB > shared_buffers 24GB).
_HNSW_IDX = 'chunk_emb_hnsw_half'


def _quickpod_custo():
    """Consulta a API QuickPod: crédito + pods ativos → burn/h + runway.

    Retorna dict (nunca lança). Deriva $/1k chunks se der (via throughput
    do fleet cacheado). burn/h = soma de hourly_cost dos pods em execução.
    """
    base = getattr(settings, 'QUICKPOD_API_URL', 'https://api.quickpod.org/update/api').rstrip('/')
    key = getattr(settings, 'QUICKPOD_API_KEY', '')
    if not key:
        return {'ok': False, 'motivo': 'sem_api_key'}
    headers = {'X-API-Key': key, 'User-Agent': 'voyager-dashboard/command-custo'}
    out = {'ok': False, 'ts': int(time.time())}
    try:
        me = requests.get(f'{base}/me', headers=headers, timeout=(5, 15))
        me.raise_for_status()
        me = me.json()
        credito = float(me.get('credit') or 0.0)
        out['credito'] = round(credito, 4)
        out['total_billed'] = round(float(me.get('total_billed') or 0.0), 2)

        pods = requests.get(f'{base}/gpu_pods', headers=headers, timeout=(5, 15))
        pods.raise_for_status()
        pods = pods.json() or []

        def _running(p):
            st = (p.get('State') or p.get('intended_state') or '').lower()
            return 'run' in st and not p.get('destroyed')

        ativos = [p for p in pods if _running(p)]
        burn_h = sum(float(p.get('hourly_cost') or p.get('pod_cost') or 0.0) for p in ativos)
        gpus = sum(int(p.get('gpu_count') or 1) for p in ativos)
        out['pods_ativos'] = len(ativos)
        out['pods_total'] = len(pods)
        out['gpus'] = gpus
        out['burn_h'] = round(burn_h, 5)
        # Runway em horas (crédito / burn). None se sem burn (nada rodando).
        out['runway_h'] = round(credito / burn_h, 2) if burn_h > 0 else None
        out['ok'] = True

        # $/1k chunks: burn/h ÷ (chunks/h). Aproxima chunks/h por docs embedados/h
        # (rate_1h do fleet) × ~fator chunks/doc. Sem fator confiável, expõe
        # $/1k docs embedados (honesto) — o front rotula a fonte.
        fleet = _safe_fleet()
        docs_h = float((fleet or {}).get('rate_1h') or 0.0)
        if burn_h > 0 and docs_h > 0:
            out['usd_por_1k_docs'] = round((burn_h / docs_h) * 1000, 4)
        else:
            out['usd_por_1k_docs'] = None
    except Exception as e:
        logger.warning('_quickpod_custo: %s: %s', type(e).__name__, e)
        out['motivo'] = f'{type(e).__name__}'
    return out


def _safe_fleet():
    try:
        return cache.get(VETOR_FLEET_CACHE_KEY)
    except Exception:
        return None


def _append_hist(custo):
    """Read-modify-write do ring de snapshots 24h. Só o scheduler escreve
    (max_instances=1), então não há corrida. Cada ponto captura throughput
    embed/extração/classif, fila e GPU util média — pros gráficos 24h.
    """
    fleet = _safe_fleet() or {}
    now = int(time.time())
    gpus = fleet.get('gpus') or []
    utils = [g.get('util') for g in gpus if g.get('util') is not None]
    gpu_util_avg = round(sum(utils) / len(utils), 1) if utils else None
    pipe = fleet.get('pipelines') or {}
    ponto = {
        'ts': now,
        'embed_h': fleet.get('rate_1h') or 0,           # docs embedados/h
        'extr_h': (fleet.get('extracao') or {}).get('rate_h') or 0,
        'clas_min': (pipe.get('classificacao') or {}).get('rate_min') or 0,
        'fila': fleet.get('queue_len') or 0,
        'gpu_util': gpu_util_avg,
        'burn_h': (custo or {}).get('burn_h'),
    }
    try:
        hist = cache.get(COMMAND_HIST_CACHE_KEY) or []
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    hist.append(ponto)
    corte = now - _COMMAND_HIST_WINDOW
    hist = [p for p in hist if p.get('ts', 0) >= corte][-_COMMAND_HIST_MAX:]
    try:
        cache.set(COMMAND_HIST_CACHE_KEY, hist, timeout=_COMMAND_HIST_TTL)
    except Exception as e:
        logger.warning('_append_hist: cache.set falhou: %s', e)


def _prom_scalar(base, promql, timeout, selector=None):
    """1 query instantânea no Prometheus → float | None (fail-soft).

    selector: se dado, escolhe a série cujo metric bate os pares dados
    (ex: {'index_name': 'chunk_emb_hnsw_half'}); senão pega a 1ª série.
    Nunca lança — retorna None em qualquer erro/ausência.
    """
    try:
        r = requests.get(f'{base}/api/v1/query', params={'query': promql}, timeout=timeout)
        r.raise_for_status()
        res = (r.json().get('data') or {}).get('result') or []
        if not res:
            return None
        chosen = None
        if selector:
            for item in res:
                m = item.get('metric') or {}
                if all(m.get(k) == v for k, v in selector.items()):
                    chosen = item
                    break
        else:
            chosen = res[0]
        if not chosen:
            return None
        val = chosen.get('value')  # [ts, "num"]
        return float(val[1]) if val and val[1] not in (None, 'NaN') else None
    except Exception:
        return None


def _prometheus_infra():
    """Consulta o Prometheus (Fase B) pros números do bloco INFRA do Command
    Center. Fonte + PromQL exatos em .ia/OPS.md "Observability stack (Fase B)".

    Retorna dict (nunca lança). Fail-soft: se o Prometheus estiver inacessível
    ou uma métrica faltar, os campos ficam None e a página degrada gracioso
    (mostra '—' / último cache), igual o card de custo.

    p95 de busca é PENDENTE (pg_stat_statements desligado no acervo DB — exige
    janela de restart do Postgres, ver OPS.md); expõe pendente=True.
    """
    base = getattr(settings, 'PROMETHEUS_URL', 'http://zordon:9490').rstrip('/')
    if not base:
        return {'ok': False, 'motivo': 'sem_prometheus_url'}
    to = (4, 12)
    idx_sel = {'index_name': _HNSW_IDX}
    out = {'ok': False, 'ts': int(time.time()), 'idx_name': _HNSW_IDX,
           'p95_pendente': True}
    try:
        # 1 · cache-hit ratio do índice HNSW (leading indicator do disk-cliff).
        cache_hit = _prom_scalar(base,
            'rate(hnsw_index_io_blks_hit[15m]) / clamp_min(rate(hnsw_index_io_blks_hit[15m]) '
            '+ rate(hnsw_index_io_blks_read[15m]), 1)', to, selector=idx_sel)
        # 2 · tamanho do índice HNSW vs shared_buffers.
        idx_bytes = _prom_scalar(base, 'hnsw_index_io_size_bytes', to, selector=idx_sel)
        sb_bytes = _prom_scalar(base, 'db_shared_buffers_shared_buffers_bytes', to)
        # 3 · dead tuples (bloat) da acervo_chunk.
        dead_tup = _prom_scalar(base,
            'acervo_table_bloat_n_dead_tup{table_name="acervo_chunk"}', to)
        # 4 · autovacuum ativo (segundos rodando; >0 = ativo).
        autovac_s = _prom_scalar(base, 'autovacuum_progress_running_seconds', to)
        # 5 · conexões / xact longa.
        conns_active = _prom_scalar(base, 'connections_summary_active', to)
        xact_max_s = _prom_scalar(base, 'connections_summary_max_xact_seconds', to)
        idle_in_txn = _prom_scalar(base, 'connections_summary_idle_in_txn', to)

        # Sinal de que o Prometheus respondeu (mesmo que alguma métrica falte):
        # considera OK se pelo menos o cache-hit OU o tamanho do índice vieram.
        if cache_hit is None and idx_bytes is None:
            out['motivo'] = 'sem_dados'
            return out

        out['cache_hit'] = round(cache_hit, 4) if cache_hit is not None else None
        out['cache_hit_pct'] = round(cache_hit * 100, 1) if cache_hit is not None else None
        out['idx_bytes'] = int(idx_bytes) if idx_bytes is not None else None
        out['sb_bytes'] = int(sb_bytes) if sb_bytes is not None else None
        if idx_bytes:
            out['idx_gb'] = round(idx_bytes / 1e9, 1)
        if sb_bytes:
            out['sb_gb'] = round(sb_bytes / 1e9, 1)
        # Razão índice/shared_buffers (>1 = índice maior que a RAM de buffer).
        if idx_bytes and sb_bytes:
            out['idx_vs_sb'] = round(idx_bytes / sb_bytes, 2)
        out['dead_tup'] = int(dead_tup) if dead_tup is not None else None
        out['autovac_seg'] = round(autovac_s, 0) if autovac_s is not None else None
        out['autovac_ativo'] = bool(autovac_s and autovac_s > 0)
        out['conns_active'] = int(conns_active) if conns_active is not None else None
        out['xact_max_seg'] = round(xact_max_s, 0) if xact_max_s is not None else None
        out['idle_in_txn'] = int(idle_in_txn) if idle_in_txn is not None else None
        out['ok'] = True
    except Exception as e:
        logger.warning('_prometheus_infra: %s: %s', type(e).__name__, e)
        out['motivo'] = f'{type(e).__name__}'
    return out


def warm_command_center():
    """Warm INLINE do Command Center: custo QuickPod + infra (Prometheus) +
    ponto no ring 24h.

    Roda no thread pool do scheduler (~60s). Leve (poucos GETs HTTP + cache).
    Nunca propaga exceção — degrade gracioso (mantém stale). Retorna o custo.
    """
    custo = _quickpod_custo()
    try:
        cache.set(COMMAND_CUSTO_CACHE_KEY, custo, timeout=_COMMAND_CUSTO_TTL)
    except Exception as e:
        logger.warning('warm_command_center: cache.set custo falhou: %s', e)

    # Infra (Prometheus Fase B). Só sobrescreve o cache se veio algo útil —
    # senão preserva o último valor bom (fail-soft, não zera a página).
    infra = _prometheus_infra()
    if infra.get('ok'):
        try:
            cache.set(COMMAND_INFRA_CACHE_KEY, infra, timeout=_COMMAND_INFRA_TTL)
        except Exception as e:
            logger.warning('warm_command_center: cache.set infra falhou: %s', e)

    _append_hist(custo)
    return custo
