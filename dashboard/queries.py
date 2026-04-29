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
    """Aplica filtros de período e tribunais comuns em qualquer queryset de Movimentacao/Process."""
    if dias:
        cutoff = date.today() - timedelta(days=dias)
        qs = qs.filter(**{f'{date_field}__date__gte': cutoff})
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    return qs


def kpis_globais(dias=None, tribunais=None):
    from django.core.cache import cache

    cache_key = f'kpis_globais:dias={dias}:tribunais={",".join(sorted(tribunais or []))}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    agora = timezone.now()
    cutoff_24h = agora - timedelta(hours=24)
    cutoff_48h = agora - timedelta(hours=48)

    movs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais)
    procs = Process.objects.all()
    if tribunais:
        procs = procs.filter(tribunal_id__in=tribunais)

    movs_24h_qs = Movimentacao.objects.filter(inserido_em__gte=cutoff_24h)
    movs_24_48h_qs = Movimentacao.objects.filter(inserido_em__gte=cutoff_48h, inserido_em__lt=cutoff_24h)
    if tribunais:
        movs_24h_qs = movs_24h_qs.filter(tribunal_id__in=tribunais)
        movs_24_48h_qs = movs_24_48h_qs.filter(tribunal_id__in=tribunais)

    movs_24h = movs_24h_qs.count()
    movs_24_48h = movs_24_48h_qs.count()
    delta_pct = None
    if movs_24_48h:
        delta_pct = round((movs_24h - movs_24_48h) / movs_24_48h * 100, 1)

    drift_qs = SchemaDriftAlert.objects.filter(resolvido=False)
    if tribunais:
        drift_qs = drift_qs.filter(tribunal_id__in=tribunais)

    result = {
        'total_processos': procs.count(),
        'total_movimentacoes': movs.count(),
        'movs_24h': movs_24h,
        'movs_24h_delta_pct': delta_pct,
        'cancelados': movs.filter(ativo=False).count(),
        'ultima_atualizacao': IngestionRun.objects
            .filter(status=IngestionRun.STATUS_SUCCESS).order_by('-finished_at')
            .values_list('finished_at', flat=True).first(),
        'drift_abertos': drift_qs.count(),
    }
    cache.set(cache_key, result, timeout=300)
    return result


def ingestion_rate_por_hora(horas=24, tribunais=None):
    """Movimentações inseridas por hora nas últimas N horas, por tribunal.

    Baseado em `inserido_em` (timestamp do INSERT no banco), não em
    `data_disponibilizacao` — reflete a velocidade real de ingestão.
    """
    from django.db.models.functions import TruncHour

    agora = timezone.now()
    cutoff = agora - timedelta(hours=horas)
    qs = Movimentacao.objects.filter(inserido_em__gte=cutoff)
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)

    rows = (
        qs.annotate(hora=TruncHour('inserido_em'))
        .values('hora', 'tribunal_id')
        .annotate(total=Count('id'))
        .order_by('hora', 'tribunal_id')
    )
    return [
        {'hora': r['hora'].isoformat(), 'tribunal': r['tribunal_id'], 'total': r['total']}
        for r in rows if r['hora']
    ]


def sparkline_24h(tribunais=None):
    """Conta movs inseridas por hora nas últimas 24h. Agregação SQL via TruncHour."""
    from django.db.models.functions import TruncHour

    agora = timezone.now()
    cutoff = agora - timedelta(hours=24)
    qs = Movimentacao.objects.filter(inserido_em__gte=cutoff)
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)
    rows = (
        qs.annotate(hora=TruncHour('inserido_em'))
        .values('hora').annotate(n=Count('id'))
    )
    by_hora = {r['hora']: r['n'] for r in rows}

    base = agora.replace(minute=0, second=0, microsecond=0)
    pontos = []
    for i in range(24):
        h = base - timedelta(hours=23 - i)
        pontos.append(by_hora.get(h, 0))
    return pontos


def volume_temporal(dias=None, tribunais=None):
    """Série temporal por tribunal. Auto-bucket: dia se janela <=365d, mês caso contrário."""
    qs = Movimentacao.objects.all()
    if dias:
        qs = qs.filter(data_disponibilizacao__date__gte=date.today() - timedelta(days=dias))
    if tribunais:
        qs = qs.filter(tribunal_id__in=tribunais)

    bucket_func = TruncDate if (dias and dias <= 365) else TruncMonth
    rows = (
        qs.annotate(periodo=bucket_func('data_disponibilizacao'))
        .values('periodo', 'tribunal_id')
        .annotate(total=Count('id'))
        .order_by('periodo', 'tribunal_id')
    )
    return [
        {'dia': r['periodo'].isoformat(), 'tribunal': r['tribunal_id'], 'total': r['total']}
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
    qs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais).exclude(nome_classe='')
    rows = qs.values('nome_classe').annotate(total=Count('id')).order_by('-total')[:limit]
    return [{'classe': r['nome_classe'], 'total': r['total']} for r in rows]


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


def filtros_movimentacoes():
    return {
        'tipos': [r['tipo_comunicacao'] for r in
                  Movimentacao.objects.exclude(tipo_comunicacao='')
                  .values('tipo_comunicacao').annotate(n=Count('id')).order_by('-n')[:8]],
        'meios': [r['meio_completo'] for r in
                  Movimentacao.objects.exclude(meio_completo='')
                  .values('meio_completo').annotate(n=Count('id')).order_by('-n')[:6]],
        'classes': [r['nome_classe'] for r in
                    Movimentacao.objects.exclude(nome_classe='')
                    .values('nome_classe').annotate(n=Count('id')).order_by('-n')[:6]],
    }


def distribuicao_tipos_partes():
    """Conta Partes por tipo (advogado/pj/pf/desconhecido). Usado pra
    donut na página /dashboard/partes/."""
    from tribunals.models import Parte
    LABELS = {
        'advogado': 'Advogado',
        'pj': 'Pessoa Jurídica',
        'pf': 'Pessoa Física',
        'desconhecido': 'Sem Identificação',
    }
    rows = Parte.objects.values('tipo').annotate(n=Count('id')).order_by('-n')
    return [
        {'name': LABELS.get(r['tipo'], r['tipo'] or '—'), 'value': r['n'], 'tipo': r['tipo']}
        for r in rows
    ]


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
    cache.set('status_workers_snapshot', result, timeout=60)
    return result


def estatisticas_por_tribunal():
    """Retorna list de dicts — um por tribunal ativo — com métricas
    agregadas pra a página `/dashboard/tribunais/`. Query única por
    métrica usando agregações condicionais ou GROUP BY tribunal_id."""
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

    # Enriquecimento status (só faz sentido nos que tem enricher).
    enriq_status = {}
    rows = (
        Process.objects.values('tribunal_id', 'enriquecimento_status')
        .annotate(n=Count('id'))
    )
    for r in rows:
        enriq_status.setdefault(r['tribunal_id'], {})[r['enriquecimento_status']] = r['n']

    # Range temporal: primeira / última movimentação por tribunal.
    from django.db.models import Max, Min
    range_trib = {
        r['tribunal_id']: (r['primeira'], r['ultima'])
        for r in Movimentacao.objects.values('tribunal_id')
        .annotate(primeira=Min('data_disponibilizacao'), ultima=Max('data_disponibilizacao'))
    }

    out = []
    for t in Tribunal.objects.filter(ativo=True).order_by('sigla'):
        primeira, ultima = range_trib.get(t.sigla, (None, None))
        out.append({
            'tribunal': t,
            'processos': procs_por_trib.get(t.sigla, 0),
            'movimentacoes': movs_por_trib.get(t.sigla, 0),
            'movs_30d': movs_30d_por_trib.get(t.sigla, 0),
            'enriquecimento': enriq_status.get(t.sigla, {}),
            'primeira_mov': primeira,
            'ultima_mov': ultima,
            'data_inicio': t.data_inicio_disponivel,
            'backfill_concluido_em': t.backfill_concluido_em,
        })
    return out


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
