from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from djen.jobs import sincronizar_movimentacoes
from djen.proxies import ProxyScrapePool
from enrichers.jobs import enriquecer_processo
from tribunals.models import Movimentacao, Parte, Process, ProcessoParte, SchemaDriftAlert, Tribunal

from . import queries


def _periodo_dias(request, default=90) -> int | None:
    """Retorna número de dias do filtro de período. None = todo o período."""
    raw = request.GET.get('dias')
    if raw is None:
        return default
    raw = raw.strip()
    if raw in ('all', '0', ''):
        return None
    try:
        return min(max(int(raw), 1), 3650)
    except ValueError:
        return default


def _backfill_em_curso() -> tuple[bool, object]:
    """Indica se há backfill ativo + última data coberta com sucesso."""
    em_curso = Tribunal.objects.filter(ativo=True, backfill_concluido_em__isnull=True).exists()
    cobertura = (
        Tribunal.objects.filter(ativo=True)
        .aggregate(m=Max('runs__janela_fim', filter=Q(runs__status='success')))
        ['m']
    ) if em_curso else None
    return em_curso, cobertura


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v for v in (x.strip() for x in value.split(',')) if v]


@login_required
@require_GET
def overview(request):
    backfill_em_curso, cobertura_ate = _backfill_em_curso()
    # Enquanto o backfill ainda não fechou, default vira "todo período" — caso contrário
    # janelas curtas (90d) ficam vazias até a ingestão alcançar a data atual.
    dias = _periodo_dias(request, default=None if backfill_em_curso else 90)
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    ctx = {
        'kpis': queries.kpis_globais(dias=dias, tribunais=tribunais_filtro),
        'sparkline_24h': queries.sparkline_24h(tribunais=tribunais_filtro),
        'volume_diario': queries.volume_temporal(dias=dias, tribunais=tribunais_filtro),
        'distribuicao': queries.distribuicao_por_tribunal(dias=dias, tribunais=tribunais_filtro),
        'tipos': queries.top_tipos_comunicacao(limit=10, dias=dias, tribunais=tribunais_filtro),
        'orgaos': queries.top_orgaos(limit=10, dias=dias, tribunais=tribunais_filtro),
        'classes': queries.top_classes(limit=8, dias=dias, tribunais=tribunais_filtro),
        'enriq_dist': queries.distribuicao_enriquecimento(tribunais=tribunais_filtro),
        'periodo_dias': dias,
        'tribunais': Tribunal.objects.filter(ativo=True),
        'tribunal_filtro': ','.join(tribunais_filtro),
        'backfill_em_curso': backfill_em_curso,
        'cobertura_ate': cobertura_ate,
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
        'volume_diario': queries.volume_temporal(dias=dias, tribunais=[t.sigla]),
        'tipos': queries.top_tipos_comunicacao(dias=dias, tribunais=[t.sigla]),
        'orgaos': queries.top_orgaos(dias=dias, tribunais=[t.sigla]),
        'classes': queries.top_classes(dias=dias, tribunais=[t.sigla]),
        'meios': queries.distribuicao_por_meio(dias=dias, tribunais=[t.sigla]),
        'cobertura': queries.cobertura_temporal(t),
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

    enriq = request.GET.get('enriq')
    if enriq in ('ok', 'pendente', 'nao_encontrado', 'erro'):
        qs = qs.filter(enriquecimento_status=enriq)

    ano = request.GET.get('ano')
    if ano:
        try:
            qs = qs.filter(ano_cnj=int(ano))
        except ValueError:
            pass
    ano_de = request.GET.get('ano_de')
    ano_ate = request.GET.get('ano_ate')
    if ano_de:
        try: qs = qs.filter(ano_cnj__gte=int(ano_de))
        except ValueError: pass
    if ano_ate:
        try: qs = qs.filter(ano_cnj__lte=int(ano_ate))
        except ValueError: pass

    backfill_em_curso, cobertura_ate = _backfill_em_curso()
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
        'enriq_filtro': enriq or '',
        'ano_filtro': ano or '',
        'ano_de_filtro': ano_de or '',
        'ano_ate_filtro': ano_ate or '',
        'total_resultados': qs.count() if (cnj or tribunais_filtro or com_movs or enriq or ano or ano_de or ano_ate) else None,
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

    participacoes = (
        ProcessoParte.objects.filter(processo=proc)
        .select_related('parte', 'representa__parte')
        .order_by('polo', 'papel')
    )
    polos = {'ativo': [], 'passivo': [], 'outros': []}
    for pp in participacoes:
        polos.setdefault(pp.polo, []).append(pp)

    return render(request, 'dashboard/processo_detail.html', {
        'processo': proc,
        'movimentacoes': list(movs_qs[:200]),
        'polos': polos,
        'tipos_disponiveis': tipos_disponiveis,
        'meios_disponiveis': meios_disponiveis,
        'tipo_filtro': ','.join(tipos_filtro),
        'meio_filtro': ','.join(meios_filtro),
        'so_ativos': so_ativos,
    })


@login_required
@require_POST
def processo_enriquecer(request, pk):
    proc = get_object_or_404(Process, pk=pk)
    if proc.tribunal_id != 'TRF1':
        messages.error(request, f'Enriquecimento ainda não suportado para {proc.tribunal_id}.')
        return redirect('dashboard:processo-detail', pk=pk)
    j = enriquecer_processo.delay(proc.pk)
    messages.success(request, f'Atualização enfileirada (job {j.id[:8]}). Recarregue em alguns segundos.')
    return redirect('dashboard:processo-detail', pk=pk)


@login_required
@require_POST
def processo_sincronizar(request, pk):
    """Dispara sincronização DJEN de movimentações desse processo."""
    proc = get_object_or_404(Process, pk=pk)
    j = sincronizar_movimentacoes.delay(proc.pk)
    messages.success(request, f'Sincronização DJEN enfileirada (job {j.id[:8]}). Recarregue em alguns segundos.')
    return redirect('dashboard:processo-detail', pk=pk)


@login_required
@require_GET
def partes(request):
    tipo = request.GET.get('tipo', '').strip()
    q = (request.GET.get('q') or '').strip()
    qs = Parte.objects.all().order_by('-total_processos', 'nome')
    if tipo:
        qs = qs.filter(tipo=tipo)
    if q and len(q) >= 2:
        qs = qs.filter(Q(nome__icontains=q) | Q(documento__icontains=q) | Q(oab__icontains=q))
    return render(request, 'dashboard/partes.html', {
        'partes': qs[:300],
        'tipo_filtro': tipo,
        'q': q,
        'total': qs.count() if (tipo or q) else None,
    })


@login_required
@require_GET
def parte_detail(request, pk):
    parte = get_object_or_404(Parte, pk=pk)

    base_qs = ProcessoParte.objects.filter(parte=parte)
    # Conta por polo (sempre, independente do filtro — pra exibir nos chips)
    counts = {row['polo']: row['n'] for row in base_qs.values('polo').annotate(n=Count('id'))}

    polo_filtro = request.GET.get('polo', '')
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    papel_filtro = (request.GET.get('papel') or '').strip()

    qs = base_qs.select_related('processo', 'processo__tribunal')
    if polo_filtro in ('ativo', 'passivo', 'outros'):
        qs = qs.filter(polo=polo_filtro)
    if tribunais_filtro:
        qs = qs.filter(processo__tribunal_id__in=tribunais_filtro)
    if papel_filtro:
        qs = qs.filter(papel__iexact=papel_filtro)
    qs = qs.order_by('-processo__ultima_movimentacao_em')

    # Papéis disponíveis pra chip bar (somente os que essa parte tem)
    papeis_disponiveis = list(
        base_qs.exclude(papel='').values_list('papel', flat=True).distinct()[:10]
    )
    tribunais_da_parte = list(
        base_qs.values_list('processo__tribunal_id', flat=True).distinct()
    )

    return render(request, 'dashboard/parte_detail.html', {
        'parte': parte,
        'participacoes': qs[:200],
        'counts': counts,
        'polo_filtro': polo_filtro,
        'tribunal_filtro': ','.join(tribunais_filtro),
        'papel_filtro': papel_filtro,
        'papeis_disponiveis': papeis_disponiveis,
        'tribunais_da_parte': tribunais_da_parte,
        'total_filtrado': qs.count() if (polo_filtro or tribunais_filtro or papel_filtro) else None,
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
