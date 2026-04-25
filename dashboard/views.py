import json

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from tribunals.models import Movimentacao, Process, SchemaDriftAlert, Tribunal

from . import queries
from djen.proxies import ProxyScrapePool


def _periodo_dias(request) -> int:
    try:
        d = int(request.GET.get('dias', '90'))
    except (TypeError, ValueError):
        d = 90
    return min(max(d, 1), 365)


@login_required
@require_GET
def overview(request):
    dias = _periodo_dias(request)
    ctx = {
        'kpis': queries.kpis_globais(),
        'volume_diario_json': json.dumps(queries.volume_diario(dias=dias), default=str),
        'distribuicao_json': json.dumps(queries.distribuicao_por_tribunal()),
        'tipos_json': json.dumps(queries.top_tipos_comunicacao()),
        'orgaos_json': json.dumps(queries.top_orgaos()),
        'periodo_dias': dias,
        'tribunais': Tribunal.objects.filter(ativo=True),
    }
    return render(request, 'dashboard/overview.html', ctx)


@login_required
@require_GET
def tribunal_detail(request, sigla):
    t = get_object_or_404(Tribunal, sigla=sigla)
    dias = _periodo_dias(request)
    ctx = {
        'tribunal': t,
        'volume_diario_json': json.dumps(queries.volume_diario(dias=dias, tribunal_sigla=t.sigla), default=str),
        'tipos_json': json.dumps(queries.top_tipos_comunicacao(tribunal_sigla=t.sigla)),
        'orgaos_json': json.dumps(queries.top_orgaos(tribunal_sigla=t.sigla)),
        'cobertura_json': json.dumps(queries.cobertura_temporal(t), default=str),
        'periodo_dias': dias,
    }
    return render(request, 'dashboard/tribunal_detail.html', ctx)


@login_required
@require_GET
def processos(request):
    qs = Process.objects.select_related('tribunal').order_by('-ultima_movimentacao_em')
    tribunal = request.GET.get('tribunal')
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    cnj = request.GET.get('cnj')
    if cnj:
        qs = qs.filter(numero_cnj__icontains=cnj.strip())
    return render(request, 'dashboard/processos.html', {
        'processos': qs[:200],
        'tribunais': Tribunal.objects.all(),
        'tribunal_filtro': tribunal,
        'cnj_filtro': cnj or '',
    })


@login_required
@require_GET
def processo_detail(request, pk):
    proc = get_object_or_404(Process.objects.select_related('tribunal'), pk=pk)
    movs = (
        Movimentacao.objects.filter(processo=proc)
        .order_by('-data_disponibilizacao', '-id')[:200]
    )
    return render(request, 'dashboard/processo_detail.html', {
        'processo': proc,
        'movimentacoes': movs,
    })


@login_required
@require_GET
def movimentacoes(request):
    from api.filters import MovimentacaoFilter
    qs = Movimentacao.objects.select_related('tribunal', 'processo').order_by('-data_disponibilizacao', '-id')
    f = MovimentacaoFilter(request.GET, queryset=qs)
    rows = f.qs[:200]
    return render(request, 'dashboard/movimentacoes.html', {
        'movimentacoes': rows,
        'tribunais': Tribunal.objects.all(),
        'q': request.GET.get('q', ''),
        'tribunal_filtro': request.GET.get('tribunal', ''),
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
