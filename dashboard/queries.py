"""Queries agregadas usadas pelo dashboard.

Convenções:
- `dias=None` ou `dias=0` significa "todo o período" (sem filtro de data).
- `tribunais` é uma lista de siglas; `None` ou `[]` significa "todos os tribunais".

Toda função relevante aceita ambos para que o dashboard possa aplicá-los uniformemente.
"""
from datetime import date, timedelta

from django.db.models import Avg, Count, ExpressionWrapper, DurationField, F, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from tribunals.models import IngestionRun, Movimentacao, Process, SchemaDriftAlert, Tribunal


def _aplicar_filtros(qs, dias=None, tribunais=None, date_field='data_disponibilizacao'):
    """Aplica filtros de período e tribunais comuns em qualquer queryset de Movimentacao/Process.

    Quando `tribunais` é vazio/None, restringe a Tribunal.ativo=True por
    default — evita que tribunais desativados apareçam fantasma em donuts/tops.
    """
    if dias:
        cutoff = date.today() - timedelta(days=dias)
        qs = qs.filter(**{f'{date_field}__date__gte': cutoff})
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    else:
        qs = qs.filter(tribunal__ativo=True)
    return qs


_KPIS_TTL = 604800  # 7 dias
# evita "cache vazio" (que mata a UX); warm renova bem antes de expirar.


def _kpis_cache_key(dias, tribunais):
    return f'kpis_globais:dias={dias}:tribunais={",".join(sorted(tribunais or []))}'


def _kpis_placeholder():
    """Retornado quando o cache está frio. View renderiza '—' (format_int aceita None)."""
    return {
        'total_processos': None,
        'total_movimentacoes': None,
        'movs_24h': None,
        'movs_24h_delta_pct': None,
        'ins_24h': None,
        'cancelados': None,
        'ultima_atualizacao': None,
        'drift_abertos': 0,
        'pending': True,
    }


def kpis_globais(dias=None, tribunais=None):
    """Lê APENAS do cache. Sem hit, retorna placeholder com `pending=True`.

    Aquecimento via cron `warm_kpis_cache` (5 min). Read-only no caminho da
    request — query DB nunca roda em hot-path.
    """
    from django.core.cache import cache
    from redis.exceptions import RedisError

    try:
        cached = cache.get(_kpis_cache_key(dias, tribunais))
    except RedisError:
        return _kpis_placeholder()
    return cached if cached is not None else _kpis_placeholder()


def compute_kpis_globais(dias=None, tribunais=None):
    """Computa KPIs e grava no cache (TTL 30min). Chamado pelo cron, NUNCA pela view."""
    from django.core.cache import cache
    from redis.exceptions import RedisError

    cache_key = _kpis_cache_key(dias, tribunais)

    agora = timezone.now()
    cutoff_24h = agora - timedelta(hours=24)
    cutoff_48h = agora - timedelta(hours=48)

    movs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais)
    procs = Process.objects.all()
    if tribunais:
        procs = procs.filter(tribunal_id__in=tribunais)
    else:
        procs = procs.filter(tribunal__ativo=True)
    if dias:
        # Quando há filtro de período, "Processos" passa a significar
        # "processos com pelo menos 1 mov no período" — semântica
        # consistente com total_movimentacoes que já filtra.
        cutoff = date.today() - timedelta(days=dias)
        procs = procs.filter(
            movimentacoes__data_disponibilizacao__date__gte=cutoff,
        ).distinct()

    # Usa data_disponibilizacao (publicação real) e NÃO inserido_em — durante
    # backfill, inserido_em explode com movs antigas reingeridas, distorcendo
    # o KPI de "atividade do dia" pra centenas de M.
    movs_24h_qs = Movimentacao.objects.filter(data_disponibilizacao__gte=cutoff_24h)
    movs_24_48h_qs = Movimentacao.objects.filter(
        data_disponibilizacao__gte=cutoff_48h, data_disponibilizacao__lt=cutoff_24h,
    )
    if tribunais:
        movs_24h_qs = movs_24h_qs.filter(tribunal_id__in=tribunais)
        movs_24_48h_qs = movs_24_48h_qs.filter(tribunal_id__in=tribunais)

    movs_24h = movs_24h_qs.count()
    movs_24_48h = movs_24_48h_qs.count()
    delta_pct = None
    if movs_24_48h:
        delta_pct = round((movs_24h - movs_24_48h) / movs_24_48h * 100, 1)

    # Velocidade de ingestão (DB INSERT) — métrica operacional separada.
    ins_24h_qs = Movimentacao.objects.filter(inserido_em__gte=cutoff_24h)
    if tribunais:
        ins_24h_qs = ins_24h_qs.filter(tribunal_id__in=tribunais)
    ins_24h = ins_24h_qs.count()

    drift_qs = SchemaDriftAlert.objects.filter(resolvido=False)
    if tribunais:
        drift_qs = drift_qs.filter(tribunal_id__in=tribunais)

    result = {
        'total_processos': procs.count(),
        'total_movimentacoes': movs.count(),
        'movs_24h': movs_24h,
        'movs_24h_delta_pct': delta_pct,
        'ins_24h': ins_24h,
        'cancelados': movs.filter(ativo=False).count(),
        'ultima_atualizacao': IngestionRun.objects
            .filter(status=IngestionRun.STATUS_SUCCESS).order_by('-finished_at')
            .values_list('finished_at', flat=True).first(),
        'drift_abertos': drift_qs.count(),
    }
    try:
        cache.set(cache_key, result, timeout=_KPIS_TTL)
    except RedisError:
        pass
    return result


def ingestion_rate_por_hora(horas=24, tribunais=None):
    """Movimentações inseridas por hora nas últimas N horas, por tribunal.

    Lê de `mv_ingestion_rate_hora` (refresh por cron 5min). Sem MV a query
    direta com TruncHour em ~30M+ rows leva >60s e estoura o warm.
    """
    from django.db import connection

    cutoff_sql = f"NOW() - INTERVAL '{int(horas)} hours'"
    where = ['hora >= ' + cutoff_sql]
    params: list = []
    if tribunais:
        where.append('tribunal_id = ANY(%s)')
        params.append(list(tribunais))
    sql = (
        'SELECT hora, tribunal_id, total FROM mv_ingestion_rate_hora '
        f'WHERE {" AND ".join(where)} ORDER BY hora, tribunal_id'
    )
    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {'hora': hora.isoformat(), 'tribunal': trib, 'total': total}
        for hora, trib, total in rows if hora
    ]


def sparkline_24h(tribunais=None):
    """Conta movs disponibilizadas por hora nas últimas 24h. Usa
    data_disponibilizacao (publicação real) — espelha o KPI movs_24h.
    """
    from django.db.models.functions import TruncHour

    agora = timezone.now()
    cutoff = agora - timedelta(hours=24)
    qs = Movimentacao.objects.filter(data_disponibilizacao__gte=cutoff)
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    rows = (
        qs.annotate(hora=TruncHour('data_disponibilizacao'))
        .values('hora').annotate(n=Count('id'))
    )
    by_hora = {r['hora']: r['n'] for r in rows}

    base = agora.replace(minute=0, second=0, microsecond=0)
    pontos = []
    for i in range(24):
        h = base - timedelta(hours=23 - i)
        pontos.append(by_hora.get(h, 0))
    return pontos


_VOLUME_TEMPORAL_MIN_DATE = date(2020, 1, 1)


def volume_temporal(dias=None, tribunais=None):
    """Série temporal por tribunal. Auto-bucket: dia se janela <=365d, mês caso contrário.

    Bucket parcial (dia/mês corrente que ainda não acabou) recebe `parcial: True`
    pro frontend renderizar tracejado — sem flag, o gráfico sugeria queda
    artificial sempre no fim.

    Floor em 2020-01-01: DJEN só foi criado no fim de 2020. Movs com
    `data_disponibilizacao` anterior vêm via Datajud (processos legados,
    publicados décadas atrás) e distorcem visualmente o eixo X — a curva
    real começa em 2020+.
    """
    qs = Movimentacao.objects.filter(data_disponibilizacao__date__gte=_VOLUME_TEMPORAL_MIN_DATE)
    if dias:
        qs = qs.filter(data_disponibilizacao__date__gte=date.today() - timedelta(days=dias))
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    else:
        qs = qs.filter(tribunal__ativo=True)

    is_daily = dias and dias <= 365
    bucket_func = TruncDate if is_daily else TruncMonth
    rows = (
        qs.annotate(periodo=bucket_func('data_disponibilizacao'))
        .values('periodo', 'tribunal_id')
        .annotate(total=Count('id'))
        .order_by('periodo', 'tribunal_id')
    )

    hoje = date.today()
    if is_daily:
        bucket_corrente = hoje
    else:
        bucket_corrente = hoje.replace(day=1)

    # Serializa só `YYYY-MM-DD` (sem hora) — eixo X do chart precisa só
    # da data, e `datetime.isoformat()` mete `T00:00:00-03:00` que polui.
    def _bucket_iso(periodo):
        if hasattr(periodo, 'date'):
            return periodo.date().isoformat()
        return periodo.isoformat()

    return [
        {
            'dia': _bucket_iso(r['periodo']),
            'tribunal': r['tribunal_id'],
            'total': r['total'],
            'parcial': (r['periodo'].date() if hasattr(r['periodo'], 'date') else r['periodo']) == bucket_corrente,
        }
        for r in rows if r['periodo']
    ]


# Mantido como alias retrocompatível.
volume_diario = volume_temporal


def distribuicao_por_tribunal(dias=None, tribunais=None):
    qs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais)
    rows = qs.values('tribunal_id').annotate(total=Count('id')).order_by('-total')
    return [{'tribunal': r['tribunal_id'], 'total': r['total']} for r in rows]


def distribuicao_por_meio(dias=None, tribunais=None):
    qs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais).exclude(meio_completo='')
    rows = qs.values('meio_completo').annotate(total=Count('id')).order_by('-total')[:8]
    return [{'meio': r['meio_completo'] or 'Não informado', 'total': r['total']} for r in rows]


def distribuicao_enriquecimento(tribunais=None):
    """Status de enriquecimento dos processos (não respeita período — é estado atual)."""
    qs = Process.objects.all()
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    rows = qs.values('enriquecimento_status').annotate(total=Count('id')).order_by('-total')
    rotulos = {
        'ok': 'Enriquecidos',
        'pendente': 'Pendentes',
        'nao_encontrado': 'Não encontrados',
        'erro': 'Com erro',
    }
    return [{'status': rotulos.get(r['enriquecimento_status'], r['enriquecimento_status']),
             'status_key': r['enriquecimento_status'],
             'total': r['total']} for r in rows]


def top_tipos_comunicacao(limit=15, dias=None, tribunais=None):
    qs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais).exclude(tipo_comunicacao='')
    rows = qs.values('tipo_comunicacao').annotate(total=Count('id')).order_by('-total')[:limit]
    return [{'tipo': r['tipo_comunicacao'], 'total': r['total']} for r in rows]


def top_classes(limit=10, dias=None, tribunais=None):
    """Top N classes processuais por número de PROCESSOS (não movs).

    Filtros de data e tribunal aplicados no Process via última atividade.
    Usa `classe_nome` populado pelo Datajud em Process — uma linha por proc,
    em vez de N linhas (uma por mov) inflando os totais.
    """
    qs = Process.objects.exclude(classe_nome='')
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    else:
        qs = qs.filter(tribunal__ativo=True)
    if dias:
        cutoff = date.today() - timedelta(days=dias)
        qs = qs.filter(ultima_movimentacao_em__date__gte=cutoff)
    rows = qs.values('classe_nome').annotate(total=Count('id')).order_by('-total')[:limit]
    return [{'classe': r['classe_nome'], 'total': r['total']} for r in rows]


def top_orgaos(limit=10, dias=None, tribunais=None):
    qs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais).exclude(nome_orgao='')
    rows = qs.values('nome_orgao').annotate(total=Count('id')).order_by('-total')[:limit]
    return [{'orgao': r['nome_orgao'], 'total': r['total']} for r in rows]


def runs_recentes(limit=30):
    return list(
        IngestionRun.objects.select_related('tribunal').order_by('-started_at')[:limit]
    )


def cobertura_temporal(tribunal):
    runs = (
        IngestionRun.objects.filter(tribunal=tribunal, status=IngestionRun.STATUS_SUCCESS)
        .order_by('janela_inicio').values('janela_inicio', 'janela_fim')
    )
    return {
        'inicio': tribunal.data_inicio_disponivel.isoformat() if tribunal.data_inicio_disponivel else None,
        'fim': date.today().isoformat(),
        'janelas_ok': [{'inicio': r['janela_inicio'].isoformat(), 'fim': r['janela_fim'].isoformat()} for r in runs],
    }


FILTROS_MOVIMENTACOES_CACHE_KEY = 'filtros_movimentacoes'


def filtros_movimentacoes():
    """Top tipos/meios/classes pra facetas. APENAS lê do cache —
    aquecido pelo warm_dashboard_all. Hot-path nunca computa: a query é
    seq scan em ~30M rows e estoura o timeout do gunicorn (observado 500
    em /movimentacoes/ com cache frio).
    """
    from django.core.cache import cache
    return cache.get(FILTROS_MOVIMENTACOES_CACHE_KEY) or {
        'tipos': [], 'meios': [], 'classes': [],
    }


def compute_filtros_movimentacoes():
    """Calcula e armazena no cache. Chamado APENAS pelo warm task.

    1 query SQL com 3 GROUP BY paralelos (UNION ALL) — antes eram 3
    seq scans em ~30M rows. Tribunal__ativo=True trocado por subquery
    cacheada de `tribunal_id IN (...)` pra evitar JOIN.

    Roteia pra replica via thread-local (ReplicaRouter) — caller usa `with
    use_replica():` e este SELECT vai pra alias `replica` automaticamente.
    """
    from django.core.cache import cache
    from django.db import connections
    from core.db_router import _local
    alias = getattr(_local, 'reads_alias', None) or 'default'
    sql = """
        WITH ativos AS (
            SELECT sigla FROM tribunals_tribunal WHERE ativo = true
        ),
        movs AS (
            SELECT tipo_comunicacao, meio_completo, nome_classe
            FROM tribunals_movimentacao
            WHERE tribunal_id IN (SELECT sigla FROM ativos)
        ),
        tipos AS (
            SELECT 'tipos' AS k, tipo_comunicacao AS v, COUNT(*) AS n
            FROM movs WHERE tipo_comunicacao <> ''
            GROUP BY tipo_comunicacao ORDER BY n DESC LIMIT 8
        ),
        meios AS (
            SELECT 'meios' AS k, meio_completo AS v, COUNT(*) AS n
            FROM movs WHERE meio_completo <> ''
            GROUP BY meio_completo ORDER BY n DESC LIMIT 6
        ),
        classes AS (
            SELECT 'classes' AS k, nome_classe AS v, COUNT(*) AS n
            FROM movs WHERE nome_classe <> ''
            GROUP BY nome_classe ORDER BY n DESC LIMIT 6
        )
        SELECT k, v FROM tipos UNION ALL
        SELECT k, v FROM meios UNION ALL
        SELECT k, v FROM classes
    """
    result = {'tipos': [], 'meios': [], 'classes': []}
    with connections[alias].cursor() as cur:
        cur.execute(sql)
        for k, v in cur.fetchall():
            result[k].append(v)
    cache.set(FILTROS_MOVIMENTACOES_CACHE_KEY, result, timeout=604800)
    return result


PARTES_DISTRIBUICAO_CACHE_KEY = 'partes:distribuicao_tipos'


def distribuicao_tipos_partes():
    """Conta Partes por tipo (advogado/pj/pf/desconhecido). Usado pra
    donut na página /dashboard/partes/.

    A query faz seq scan em ~1M rows (~5s a frio) — cacheada no Redis
    por 10min. Pré-aquecida pelo job `warm_partes_cache` (5min).
    """
    from django.core.cache import cache
    from tribunals.models import Parte

    cached = cache.get(PARTES_DISTRIBUICAO_CACHE_KEY)
    if cached is not None:
        return cached

    LABELS = {
        'advogado': 'Advogado',
        'pj': 'Pessoa Jurídica',
        'pf': 'Pessoa Física',
        'desconhecido': 'Sem Identificação',
    }
    rows = Parte.objects.values('tipo').annotate(n=Count('id')).order_by('-n')
    result = [
        {'name': LABELS.get(r['tipo'], r['tipo'] or '—'), 'value': r['n'], 'tipo': r['tipo']}
        for r in rows
    ]
    cache.set(PARTES_DISTRIBUICAO_CACHE_KEY, result, timeout=604800)  # 2h — warm a cada 5 min
    return result


def status_workers():
    """Lê o snapshot de filas/workers do cache Redis.

    A computação real fica em `compute_workers_snapshot()`, chamada apenas
    pelo job de background `warm_workers_cache` (a cada 30s). Requisições
    web NUNCA computam diretamente — com 1400+ conexões Redis o pipeline
    demora 20-30s e estoura o timeout do gunicorn.

    Retorna esqueleto vazio enquanto o cache ainda não foi aquecido.
    """
    from django.core.cache import cache
    return cache.get('status_workers_snapshot') or {
        'queues': [],
        'workers': [],
        'workers_by_queue': {},
    }


def compute_workers_snapshot():
    """Calcula o snapshot de filas/workers via pipelines Redis e armazena no cache.

    Chamado apenas pelo worker RQ (warm_workers_cache). Nunca direto de
    requisições web — pode demorar 20-30s com Redis saturado.
    """
    from django.core.cache import cache
    import django_rq

    conn = django_rq.get_connection('default')
    queue_names = list(__import__('django.conf', fromlist=['settings']).settings.RQ_QUEUES.keys())

    pipe = conn.pipeline()
    for qname in queue_names:
        pipe.llen(f'rq:queue:{qname}')
        pipe.zcard(f'rq:wip:{qname}')
        pipe.zcard(f'rq:finished:{qname}')
        pipe.zcard(f'rq:failed:{qname}')
        pipe.zcard(f'rq:scheduled:{qname}')
        pipe.zcard(f'rq:deferred:{qname}')
    pipe.smembers('rq:workers')
    pipe_results = pipe.execute()

    queues = []
    for i, qname in enumerate(queue_names):
        base = i * 6
        queues.append({
            'name': qname,
            'pending': pipe_results[base],
            'started': pipe_results[base + 1],
            'finished': pipe_results[base + 2],
            'failed': pipe_results[base + 3],
            'scheduled': pipe_results[base + 4],
            'deferred': pipe_results[base + 5],
        })

    worker_keys = [k.decode() if isinstance(k, bytes) else k for k in pipe_results[-1]]

    from collections import Counter
    workers_by_queue = Counter()
    if worker_keys:
        pipe2 = conn.pipeline(transaction=False)
        for k in worker_keys:
            pipe2.hget(k, 'queues')
        for raw in pipe2.execute():
            if not raw:
                continue
            queues_str = raw.decode() if isinstance(raw, bytes) else raw
            for q in queues_str.split(','):
                if q:
                    workers_by_queue[q] += 1

    result = {
        'queues': queues,
        'workers': worker_keys,
        'workers_by_queue': dict(workers_by_queue),
    }
    # TTL 2h garante que a página continua exibindo o último snapshot
    # mesmo se vários warms falharem em sequência. Warm cron roda
    # frequente — TTL é só rede de segurança contra "cache vazio" matar UX.
    cache.set('status_workers_snapshot', result, timeout=7200)
    return result


_ESTATISTICAS_TRIBUNAL_CACHE_KEY = 'estatisticas_por_tribunal:v2'
_ESTATISTICAS_TRIBUNAL_TTL = 7200  # 2h — cron warm a cada 5 min, TTL longo
# evita "cache vazio" se warm falhar (Redis lento, OOM, etc).


def estatisticas_por_tribunal():
    """Lê APENAS do cache. Aquecido por `warm_estatisticas_tribunal` (5min).

    Hot-path nunca computa: Movimentacao.values('tribunal_id').annotate(Count)
    em ~30M rows toma >60s sob carga e tomba o gunicorn worker.
    Em cache miss, retorna placeholder com flag `_pending` por tribunal.
    """
    from django.core.cache import cache
    from redis.exceptions import RedisError
    try:
        cached = cache.get(_ESTATISTICAS_TRIBUNAL_CACHE_KEY)
    except RedisError:
        cached = None
    if cached is not None:
        # Reidrata Tribunal pra cada entry (não serializável bem em cache).
        siglas_in_cache = {row['tribunal_sigla'] for row in cached}
        tribs = {t.sigla: t for t in Tribunal.objects.filter(sigla__in=siglas_in_cache)}
        return [
            {**{k: v for k, v in row.items() if k != 'tribunal_sigla'},
             'tribunal': tribs.get(row['tribunal_sigla'])}
            for row in cached if row['tribunal_sigla'] in tribs
        ]
    # Cache miss — placeholder pra todos os ativos
    return [
        {
            'tribunal': t,
            'processos': None,
            'movimentacoes': None,
            'movs_30d': None,
            'enriquecimento': {},
            'primeira_mov': None,
            'ultima_mov': None,
            'data_inicio': t.data_inicio_disponivel,
            'backfill_concluido_em': t.backfill_concluido_em,
            'pending': True,
        }
        for t in Tribunal.objects.filter(ativo=True).order_by('sigla')
    ]


def compute_estatisticas_por_tribunal():
    """Computa e grava no cache (TTL 30min). Chamado pelo cron, NUNCA pela view.

    Query única por métrica usando GROUP BY tribunal_id em vez de N queries
    por tribunal. Total ~5 queries pesadas em tabelas grandes.
    """
    from django.core.cache import cache
    from django.db.models import Max, Min
    from redis.exceptions import RedisError
    agora = timezone.now()
    cutoff_30d = agora - timedelta(days=30)

    procs_por_trib = dict(
        Process.objects.values('tribunal_id').annotate(n=Count('id'))
        .values_list('tribunal_id', 'n')
    )
    movs_por_trib = dict(
        Movimentacao.objects.values('tribunal_id').annotate(n=Count('id'))
        .values_list('tribunal_id', 'n')
    )
    movs_30d_por_trib = dict(
        Movimentacao.objects.filter(data_disponibilizacao__gte=cutoff_30d)
        .values('tribunal_id').annotate(n=Count('id'))
        .values_list('tribunal_id', 'n')
    )

    enriq_status: dict[str, dict] = {}
    rows = (
        Process.objects.values('tribunal_id', 'enriquecimento_status')
        .annotate(n=Count('id'))
    )
    for r in rows:
        enriq_status.setdefault(r['tribunal_id'], {})[r['enriquecimento_status']] = r['n']

    range_trib = {
        r['tribunal_id']: (r['primeira'], r['ultima'])
        for r in Movimentacao.objects.values('tribunal_id')
        .annotate(primeira=Min('data_disponibilizacao'), ultima=Max('data_disponibilizacao'))
    }

    payload = []
    for t in Tribunal.objects.filter(ativo=True).order_by('sigla'):
        primeira, ultima = range_trib.get(t.sigla, (None, None))
        payload.append({
            'tribunal_sigla': t.sigla,  # serialização — Tribunal hidratado no read
            'processos': procs_por_trib.get(t.sigla, 0),
            'movimentacoes': movs_por_trib.get(t.sigla, 0),
            'movs_30d': movs_30d_por_trib.get(t.sigla, 0),
            'enriquecimento': enriq_status.get(t.sigla, {}),
            'primeira_mov': primeira,
            'ultima_mov': ultima,
            'data_inicio': t.data_inicio_disponivel,
            'backfill_concluido_em': t.backfill_concluido_em,
        })
    try:
        cache.set(_ESTATISTICAS_TRIBUNAL_CACHE_KEY, payload, timeout=_ESTATISTICAS_TRIBUNAL_TTL)
    except RedisError:
        pass
    return payload


def ingestao_por_dia(dias=None, tribunal=None):
    """Agrega IngestionRuns por janela_inicio: status, throughput e duração.

    Retorna lista de {dia, tribunal, ok, falha, movs, duracao_s} ordenada por dia.
    Usado pelos charts da página de ingestão.
    """
    _dur = ExpressionWrapper(F('finished_at') - F('started_at'), output_field=DurationField())

    qs = IngestionRun.objects.filter(finished_at__isnull=False)
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    if dias:
        cutoff = date.today() - timedelta(days=dias)
        qs = qs.filter(janela_inicio__gte=cutoff)

    rows = list(
        qs.annotate(duration=_dur)
        .values('janela_inicio', 'tribunal_id')
        .annotate(
            ok=Count('id', filter=Q(status=IngestionRun.STATUS_SUCCESS)),
            falha=Count('id', filter=Q(status=IngestionRun.STATUS_FAILED)),
            movs=Sum('movimentacoes_novas', filter=Q(status=IngestionRun.STATUS_SUCCESS)),
            duracao_avg=Avg('duration', filter=Q(status=IngestionRun.STATUS_SUCCESS)),
        )
        .order_by('janela_inicio')
    )

    result = []
    for r in rows:
        avg_s = r['duracao_avg'].total_seconds() if r['duracao_avg'] else None
        result.append({
            'dia': r['janela_inicio'].isoformat(),
            'tribunal': r['tribunal_id'],
            'ok': r['ok'],
            'falha': r['falha'],
            'movs': r['movs'] or 0,
            'duracao_s': round(avg_s) if avg_s is not None else None,
        })
    return result


def ingestao_kpis(tribunal=None):
    """KPIs agregados para a página de ingestão."""
    qs = IngestionRun.objects.filter(finished_at__isnull=False)
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)

    total = qs.count()
    n_ok = qs.filter(status=IngestionRun.STATUS_SUCCESS).count()
    dias_cobertos = (
        qs.filter(status=IngestionRun.STATUS_SUCCESS)
        .values('janela_inicio').distinct().count()
    )
    movs = (
        qs.filter(status=IngestionRun.STATUS_SUCCESS)
        .aggregate(t=Sum('movimentacoes_novas'))['t'] or 0
    )
    _dur = ExpressionWrapper(F('finished_at') - F('started_at'), output_field=DurationField())
    duracao = (
        qs.filter(status=IngestionRun.STATUS_SUCCESS)
        .annotate(duration=_dur)
        .aggregate(avg=Avg('duration'))['avg']
    )

    dur_s = round(duracao.total_seconds()) if duracao else None
    if dur_s is not None:
        duracao_fmt = f'{dur_s // 60}m{dur_s % 60:02d}s' if dur_s >= 60 else f'{dur_s}s'
    else:
        duracao_fmt = None

    return {
        'total_runs': total,
        'ok_runs': n_ok,
        'taxa_sucesso': round(n_ok / total * 100, 1) if total else 0,
        'dias_cobertos': dias_cobertos,
        'movimentacoes_novas': movs,
        'duracao_avg_s': dur_s,
        'duracao_avg_fmt': duracao_fmt,
    }
