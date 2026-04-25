import json

from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from djen.proxies import ProxyScrapePool
from tribunals.models import Movimentacao, Process, SchemaDriftAlert, Tribunal

from . import queries


def _periodo_dias(request) -> int:
    try:
        d = int(request.GET.get('dias', '90'))
    except (TypeError, ValueError):
        d = 90
    return min(max(d, 1), 365)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v for v in (x.strip() for x in value.split(',')) if v]


@login_required
@require_GET
def overview(request):
    dias = _periodo_dias(request)
    ctx = {
        'kpis': queries.kpis_globais(),
        'sparkline_24h': queries.sparkline_24h(),
        'volume_diario_json': json.dumps(queries.volume_diario(dias=dias), default=str),
        'distribuicao_json': json.dumps(queries.distribuicao_por_tribunal()),
        'tipos_json': json.dumps(queries.top_tipos_comunicacao(limit=10)),
        'orgaos_json': json.dumps(queries.top_orgaos(limit=10)),
        'classes_json': json.dumps(queries.top_classes(limit=8)),
        'meios_json': json.dumps(queries.distribuicao_por_meio()),
        'periodo_dias': dias,
        'tribunais': Tribunal.objects.filter(ativo=True),
    }
    return render(request, 'dashboard/overview.html', ctx)


@login_required
@require_GET
def tribunal_detail(request, sigla):
    t = get_object_or_404(Tribunal, sigla=sigla)
    dias = _periodo_dias(request)
    total_processos = Process.objects.filter(tribunal=t).count()
    total_movs = Movimentacao.objects.filter(tribunal=t).count()
    ctx = {
        'tribunal': t,
        'total_processos': total_processos,
        'total_movs': total_movs,
        'cancelados': Movimentacao.objects.filter(tribunal=t, ativo=False).count(),
        'orgaos_unicos': Movimentacao.objects.filter(tribunal=t).exclude(nome_orgao='')
                          .values('nome_orgao').distinct().count(),
        'classes_unicas': Movimentacao.objects.filter(tribunal=t).exclude(nome_classe='')
                           .values('nome_classe').distinct().count(),
        'volume_diario_json': json.dumps(queries.volume_diario(dias=dias, tribunal_sigla=t.sigla), default=str),
        'tipos_json': json.dumps(queries.top_tipos_comunicacao(tribunal_sigla=t.sigla)),
        'orgaos_json': json.dumps(queries.top_orgaos(tribunal_sigla=t.sigla)),
        'classes_json': json.dumps(queries.top_classes(tribunal_sigla=t.sigla)),
        'meios_json': json.dumps(queries.distribuicao_por_meio(tribunal_sigla=t.sigla)),
        'cobertura_json': json.dumps(queries.cobertura_temporal(t), default=str),
        'periodo_dias': dias,
    }
    return render(request, 'dashboard/tribunal_detail.html', ctx)


@login_required
@require_GET
def processos(request):
    qs = Process.objects.select_related('tribunal')

    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    if tribunais_filtro:
        qs = qs.filter(tribunal_id__in=tribunais_filtro)

    cnj = (request.GET.get('cnj') or '').strip()
    if cnj:
        qs = qs.filter(numero_cnj__icontains=cnj)

    com_movs = request.GET.get('com_movs')
    if com_movs == 'sim':
        qs = qs.filter(total_movimentacoes__gt=0)
    elif com_movs == 'nao':
        qs = qs.filter(total_movimentacoes=0)

    # backfill em curso → ordena por inserido_em (mais útil que ultima_mov, que está
    # presa ao chunk mais recente já processado).
    backfill_em_curso = Tribunal.objects.filter(ativo=True, backfill_concluido_em__isnull=True).exists()
    cobertura_ate = (
        Tribunal.objects.filter(ativo=True)
        .aggregate(m=Max('runs__janela_fim', filter=Q(runs__status='success')))
        ['m']
    ) if backfill_em_curso else None

    sort = request.GET.get('sort', 'inserido' if backfill_em_curso else 'recente')
    if sort == 'recente':
        qs = qs.order_by(F_NULL := '-ultima_movimentacao_em', '-inserido_em').extra(
            select={'ult_null': 'ultima_movimentacao_em IS NULL'},
            order_by=['ult_null', '-ultima_movimentacao_em', '-inserido_em'],
        )
    elif sort == 'antigo':
        qs = qs.order_by('ultima_movimentacao_em')
    elif sort == 'maismovs':
        qs = qs.order_by('-total_movimentacoes', '-ultima_movimentacao_em')
    elif sort == 'menosmovs':
        qs = qs.order_by('total_movimentacoes')
    elif sort == 'inserido':
        qs = qs.order_by('-inserido_em')

    processos_page = list(qs[:200])
    return render(request, 'dashboard/processos.html', {
        'processos': processos_page,
        'tribunais': Tribunal.objects.all(),
        'tribunal_filtro': ','.join(tribunais_filtro),
        'cnj_filtro': cnj,
        'com_movs': com_movs or '',
        'sort': sort,
        'total_resultados': qs.count() if cnj or tribunais_filtro or com_movs else None,
        'backfill_em_curso': backfill_em_curso,
        'cobertura_ate': cobertura_ate,
    })


@login_required
@require_GET
def processo_detail(request, pk):
    proc = get_object_or_404(Process.objects.select_related('tribunal'), pk=pk)
    movs_qs = Movimentacao.objects.filter(processo=proc).order_by('-data_disponibilizacao', '-id')

    # filtros inline na timeline
    tipos_filtro = _split_csv(request.GET.get('tipo'))
    meios_filtro = _split_csv(request.GET.get('meio'))
    if tipos_filtro:
        movs_qs = movs_qs.filter(tipo_comunicacao__in=tipos_filtro)
    if meios_filtro:
        movs_qs = movs_qs.filter(meio_completo__in=meios_filtro)
    so_ativos = request.GET.get('ativos', '1') == '1'
    if so_ativos:
        movs_qs = movs_qs.filter(ativo=True)

    # facetas pro chip bar
    tipos_disponiveis = list(
        Movimentacao.objects.filter(processo=proc).exclude(tipo_comunicacao='')
        .values_list('tipo_comunicacao', flat=True).distinct()[:10]
    )
    meios_disponiveis = list(
        Movimentacao.objects.filter(processo=proc).exclude(meio_completo='')
        .values_list('meio_completo', flat=True).distinct()[:6]
    )

    return render(request, 'dashboard/processo_detail.html', {
        'processo': proc,
        'movimentacoes': list(movs_qs[:200]),
        'tipos_disponiveis': tipos_disponiveis,
        'meios_disponiveis': meios_disponiveis,
        'tipo_filtro': ','.join(tipos_filtro),
        'meio_filtro': ','.join(meios_filtro),
        'so_ativos': so_ativos,
    })


@login_required
@require_GET
def movimentacoes(request):
    qs = Movimentacao.objects.select_related('tribunal', 'processo').order_by('-data_disponibilizacao', '-id')

    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    if tribunais_filtro:
        qs = qs.filter(tribunal_id__in=tribunais_filtro)

    tipos_filtro = _split_csv(request.GET.get('tipo'))
    if tipos_filtro:
        qs = qs.filter(tipo_comunicacao__in=tipos_filtro)

    meios_filtro = _split_csv(request.GET.get('meio'))
    if meios_filtro:
        qs = qs.filter(meio_completo__in=meios_filtro)

    classes_filtro = _split_csv(request.GET.get('classe'))
    if classes_filtro:
        qs = qs.filter(nome_classe__in=classes_filtro)

    so_ativos = request.GET.get('ativos', '1') == '1'
    if so_ativos:
        qs = qs.filter(ativo=True)

    q = (request.GET.get('q') or '').strip()
    if q and len(q) >= 3:
        qs = qs.filter(texto__icontains=q)

    com_link = request.GET.get('com_link')
    if com_link == 'sim':
        qs = qs.exclude(link='')

    rows = list(qs[:200])
    return render(request, 'dashboard/movimentacoes.html', {
        'movimentacoes': rows,
        'tribunais': Tribunal.objects.all(),
        'facetas': queries.filtros_movimentacoes(),
        'q': q,
        'tribunal_filtro': ','.join(tribunais_filtro),
        'tipo_filtro': ','.join(tipos_filtro),
        'meio_filtro': ','.join(meios_filtro),
        'classe_filtro': ','.join(classes_filtro),
        'so_ativos': so_ativos,
        'com_link': com_link or '',
    })


@login_required
@require_GET
def ingestao(request):
    return render(request, 'dashboard/ingestao.html', {
        'runs': queries.runs_recentes(50),
        'drift_alerts': SchemaDriftAlert.objects.filter(resolvido=False)
                        .select_related('tribunal', 'ingestion_run'),
        'proxies': ProxyScrapePool.singleton().status(),
        'tribunais': Tribunal.objects.all(),
    })


@login_required
@require_GET
def root(request):
    return redirect('dashboard:overview')
