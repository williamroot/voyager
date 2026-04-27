"""Queries agregadas usadas pelo dashboard.

Convenções:
- `dias=None` ou `dias=0` significa "todo o período" (sem filtro de data).
- `tribunais` é uma lista de siglas; `None` ou `[]` significa "todos os tribunais".

Toda função relevante aceita ambos para que o dashboard possa aplicá-los uniformemente.
"""
from datetime import date, timedelta

from django.db.models import Count, Q
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
    agora = timezone.now()
    cutoff_24h = agora - timedelta(hours=24)
    cutoff_48h = agora - timedelta(hours=48)

    movs = _aplicar_filtros(Movimentacao.objects.all(), dias=dias, tribunais=tribunais)
    procs = Process.objects.all()
    if tribunais:
        procs = procs.filter(tribunal_id__in=tribunais)

    # 24h é sempre 24h reais — período não aplica, mas tribunal sim.
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

    return {
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
    """Snapshot das filas RQ e workers conectados.

    Inclui counters de cada queue (pending/started/finished/failed) e
    a lista de workers vivos com fila atendida + idle time. Usado pela
    página `/dashboard/workers/`.

    Reusa UMA conexão Redis pra todas as queues — antes cada `get_queue`
    podia abrir conexão nova, com 5+ filas e polling de 15s isso lotava
    TIME_WAIT no container web (28k+ sockets, port exhaustion).
    """
    import django_rq
    from rq import Queue, Worker

    conn = django_rq.get_connection('default')
    queue_names = list(__import__('django.conf', fromlist=['settings']).settings.RQ_QUEUES.keys())

    queues = []
    for qname in queue_names:
        q = Queue(qname, connection=conn)
        queues.append({
            'name': qname,
            'pending': len(q),
            'started': q.started_job_registry.count,
            'finished': q.finished_job_registry.count,
            'failed': q.failed_job_registry.count,
            'scheduled': q.scheduled_job_registry.count,
            'deferred': q.deferred_job_registry.count,
        })
    workers_raw = Worker.all(connection=conn)
    # RQ grava last_heartbeat como datetime naive em UTC. Se a TZ do
    # processo Django for ≠ UTC, `timezone.now().replace(tzinfo=None)` mente
    # — daí a comparação direta em UTC.
    from datetime import datetime, timezone as dt_timezone
    now_utc_naive = datetime.now(dt_timezone.utc).replace(tzinfo=None)
    workers = []
    for w in workers_raw:
        last_heartbeat = w.last_heartbeat
        idle_seconds = None
        if last_heartbeat:
            idle_seconds = int((now_utc_naive - last_heartbeat).total_seconds())
        workers.append({
            'name': w.name,
            'state': w.get_state(),
            'queues': [q.name for q in w.queues],
            'current_job_id': w.get_current_job_id(),
            'last_heartbeat': last_heartbeat,
            'idle_seconds': idle_seconds,
            'successful_jobs': w.successful_job_count,
            'failed_jobs': w.failed_job_count,
            'total_working_time': int(w.total_working_time or 0),
        })
    workers.sort(key=lambda w: (','.join(w['queues']), w['name']))
    return {'queues': queues, 'workers': workers}


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
