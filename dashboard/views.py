from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from djen.jobs import sincronizar_movimentacoes
from djen.proxies import ProxyScrapePool
from enrichers.jobs import _ENRICHERS, enqueue_enriquecimento, enqueue_enriquecimento_manual
from tribunals.models import IngestionRun, Movimentacao, Parte, Process, ProcessoParte, SchemaDriftAlert, Tribunal

from . import queries


def _safe_cache_get(key, default=None):
    """Wrapper de cache.get() que retorna `default` ao invés de propagar exceções Redis."""
    try:
        return cache.get(key, default)
    except Exception:
        return default


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


def _is_htmx(request) -> bool:
    return request.headers.get('HX-Request') == 'true'


class _CachedCountPaginator(Paginator):
    """Paginator que aceita um `count` pré-computado.

    Usado quando o total da tabela já foi calculado por outra via
    (ex: soma do donut cacheado em `distribuicao_tipos_partes`),
    evitando um `SELECT COUNT(*)` extra que faria seq scan na hot path.

    Implementação: gravamos o valor diretamente em `self.__dict__['count']`,
    que é o mesmo slot usado por `Paginator.count` (`cached_property`).
    Assim `count`, `num_pages`, `page_range` e os derivados em `Page`
    leem o cache normalmente — sem reissue de COUNT(*) por acesso.
    """

    def __init__(self, *args, count_override=None, **kwargs):
        super().__init__(*args, **kwargs)
        if count_override is not None:
            self.__dict__['count'] = count_override


def _paginar(qs, request, default_size=50, max_size=200, count_override=None):
    try:
        size = max(1, min(int(request.GET.get('page_size', default_size)), max_size))
    except (TypeError, ValueError):
        size = default_size
    paginator = _CachedCountPaginator(qs, size, count_override=count_override)
    try:
        page_num = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page_num = 1
    page = paginator.get_page(page_num)
    return page


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
    dias = _periodo_dias(request, default=None)
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    ctx = {
        'periodo_dias': dias,
        'tribunais': Tribunal.objects.filter(ativo=True),
        'tribunal_filtro': ','.join(tribunais_filtro),
        'backfill_em_curso': backfill_em_curso,
        'cobertura_ate': cobertura_ate,
    }
    return render(request, 'dashboard/overview.html', ctx)


@login_required
@require_GET
def overview_kpis(request):
    """Carregamento lazy dos KPIs da overview — count() em tabelões pesados
    pode demorar; isolar do shell evita gunicorn timeout no caminho da home."""
    dias = _periodo_dias(request, default=None)
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    return render(request, 'dashboard/_partials/_kpis.html', {
        'kpis': queries.kpis_globais(dias=dias, tribunais=tribunais_filtro),
    })


# Mapa de chaves de chart → callable que retorna lista/dict serializável.
# Cada callable recebe (dias, tribunais, sigla_tribunal_unica). Endpoints são read-only,
# leem filtros do request, retornam JSON.
def _chart_volume_temporal(dias, tribunais, sigla):
    if sigla:
        return queries.volume_temporal(dias=dias, tribunais=[sigla])
    return queries.volume_temporal(dias=dias, tribunais=tribunais)


def _chart_distribuicao(dias, tribunais, sigla):
    if sigla:
        return queries.distribuicao_por_tribunal(dias=dias, tribunais=[sigla])
    return queries.distribuicao_por_tribunal(dias=dias, tribunais=tribunais)


def _chart_tipos(dias, tribunais, sigla):
    return queries.top_tipos_comunicacao(limit=10, dias=dias, tribunais=[sigla] if sigla else tribunais)


def _chart_orgaos(dias, tribunais, sigla):
    return queries.top_orgaos(limit=10, dias=dias, tribunais=[sigla] if sigla else tribunais)


def _chart_classes(dias, tribunais, sigla):
    return queries.top_classes(limit=8, dias=dias, tribunais=[sigla] if sigla else tribunais)


def _chart_meios(dias, tribunais, sigla):
    return queries.distribuicao_por_meio(dias=dias, tribunais=[sigla] if sigla else tribunais)


def _chart_enriq(dias, tribunais, sigla):
    return queries.distribuicao_enriquecimento(tribunais=[sigla] if sigla else tribunais)


def _chart_sparkline_24h(dias, tribunais, sigla):
    return queries.sparkline_24h(tribunais=[sigla] if sigla else tribunais)


def _chart_ingestao_por_hora(dias, tribunais, sigla):
    return queries.ingestion_rate_por_hora(horas=24, tribunais=[sigla] if sigla else tribunais)


def _chart_ingestao_run_stats(dias, tribunais, sigla):
    tribunal = sigla or (tribunais[0] if tribunais else None)
    return queries.ingestao_por_dia(dias=dias, tribunal=tribunal)


def _chart_cache_key(key: str, dias, tribunais: list) -> str:
    trib = ','.join(sorted(tribunais)) if tribunais else ''
    dias_str = str(dias) if dias is not None else 'all'
    return f'chart:{key}:d={dias_str}:t={trib}'


_CHART_HANDLERS = {
    'volume-temporal': _chart_volume_temporal,
    'distribuicao': _chart_distribuicao,
    'tipos': _chart_tipos,
    'orgaos': _chart_orgaos,
    'classes': _chart_classes,
    'meios': _chart_meios,
    'enriquecimento': _chart_enriq,
    'sparkline-24h': _chart_sparkline_24h,
    'ingestao-por-hora': _chart_ingestao_por_hora,
    'ingestao-run-stats': _chart_ingestao_run_stats,
}


@login_required
@require_GET
def chart_data(request, key):
    """Endpoint AJAX pros charts. Lazy load do dashboard."""
    handler = _CHART_HANDLERS.get(key)
    if not handler:
        raise Http404(f'chart "{key}" não existe')

    # Default None (todo período) = mesmo que overview/KPIs.
    # Sem isso, donut/charts mostravam 90d enquanto KPIs mostravam todo
    # período — números visivelmente divergentes na mesma página.
    dias = _periodo_dias(request, default=None)
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    sigla = request.GET.get('sigla') or None

    # Cache só quando não há filtros específicos (homepage sem tribunal/sigla)
    use_cache = sigla is None and not tribunais_filtro

    # ingestao-por-hora usa parâmetro `horas` em vez de `dias`
    if key == 'ingestao-por-hora':
        try:
            horas = max(1, min(int(request.GET.get('horas', 24)), 168))
        except (ValueError, TypeError):
            horas = 24
        if use_cache:
            cached = _safe_cache_get(f'chart:ingestao-por-hora:h={horas}')
            return JsonResponse(
                {'data': cached if cached is not None else [], 'pending': cached is None},
                json_dumps_params={'default': str},
            )
        # Filtro custom (por sigla/tribunal): computa on-demand — não pré-aquecido.
        data = queries.ingestion_rate_por_hora(horas=horas, tribunais=[sigla] if sigla else tribunais_filtro or None)
        return JsonResponse({'data': data}, json_dumps_params={'default': str})

    if use_cache:
        cached = _safe_cache_get(_chart_cache_key(key, dias, tribunais_filtro))
        return JsonResponse(
            {'data': cached if cached is not None else [], 'pending': cached is None},
            json_dumps_params={'default': str},
        )

    # Filtro custom: computa on-demand, sem persistir em cache (cron só aquece o caminho default).
    data = handler(dias, tribunais_filtro, sigla)
    return JsonResponse({'data': data}, json_dumps_params={'default': str})


@login_required
@require_GET
def workers(request):
    """Visão das filas RQ e workers conectados (auto-refresh via HTMX)."""
    return render(request, 'dashboard/workers.html', queries.status_workers())


@login_required
@require_GET
def tribunais(request):
    """Lista tribunais ativos com KPIs agregados (cards). Lê só do cache;
    cron `warm_estatisticas_tribunal` (5min) computa o GROUP BY pesado."""
    stats = queries.estatisticas_por_tribunal()
    return render(request, 'dashboard/tribunais.html', {
        'stats': stats,
        'has_pending': any(s.get('pending') for s in stats),
    })


@login_required
@require_GET
def tribunal_detail(request, sigla):
    t = get_object_or_404(Tribunal, sigla=sigla)
    dias = _periodo_dias(request)
    # KPIs server-side. Charts via lazy load (chart_data).
    # Cacheia por 5min — 5 counts em Movimentacao com filtro só por tribunal
    # faz seq scan em ~10M-30M rows (2-5s a frio).
    kpi_key = f'tribunal_kpis:{t.sigla}'
    kpis_t = _safe_cache_get(kpi_key)
    if kpis_t is None:
        kpis_t = {
            'total_processos': Process.objects.filter(tribunal=t).count(),
            'total_movs': Movimentacao.objects.filter(tribunal=t).count(),
            'cancelados': Movimentacao.objects.filter(tribunal=t, ativo=False).count(),
            'orgaos_unicos': Movimentacao.objects.filter(tribunal=t).exclude(nome_orgao='')
                              .values('nome_orgao').distinct().count(),
            'classes_unicas': Movimentacao.objects.filter(tribunal=t).exclude(nome_classe='')
                               .values('nome_classe').distinct().count(),
        }
        cache.set(kpi_key, kpis_t, timeout=300)
    ctx = {
        'tribunal': t,
        **kpis_t,
        'periodo_dias': dias,
    }
    return render(request, 'dashboard/tribunal_detail.html', ctx)


@login_required
@require_GET
def processos(request):
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    enriq = request.GET.get('enriq')

    base_ctx = {
        'tribunais': Tribunal.objects.all(),
        'tribunal_filtro': ','.join(tribunais_filtro),
        'enriq_filtro': enriq or '',
    }

    # Shell-only quando NÃO é HTMX — sem queryset, página renderiza instantâneo
    # e a lista vem via hx-trigger="load".
    if not _is_htmx(request):
        return render(request, 'dashboard/processos.html', base_ctx)

    # HTMX: roda queryset + paginação + retorna só o partial.
    # Ordenação fixa por -id (PK reverse scan, mesma ordem cronológica de inserção).
    # only(): template usa 9 campos — buscar 36 colunas (Process + JOIN tribunal) era desperdício.
    # select_related removido: template só usa tribunal_id (FK column), nunca p.tribunal.attr.
    qs = Process.objects.only(
        'numero_cnj', 'enriquecimento_status', 'tribunal_id',
        'ano_cnj', 'classe_nome', 'classe_codigo', 'ultima_movimentacao_em', 'inserido_em',
    ).order_by('-id')
    has_filter = False
    if tribunais_filtro:
        qs = qs.filter(tribunal_id__in=tribunais_filtro)
        has_filter = True
    if enriq in ('ok', 'pendente', 'nao_encontrado', 'erro'):
        qs = qs.filter(enriquecimento_status=enriq)
        has_filter = True

    # Sem filtro: 500k+ rows. Reusa total_processos do kpis_globais (cache).
    # Se cache frio, usa estimativa reltuples — evita seq-scan no COUNT(*).
    count_override = None
    if not has_filter:
        kpis = queries.kpis_globais()
        count_override = kpis.get('total_processos')
        if count_override is None:
            from django.db import connection
            with connection.cursor() as cur:
                cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname='tribunals_process'")
                row = cur.fetchone()
                count_override = int(row[0]) if row and row[0] else 0

    page = _paginar(qs, request, default_size=50, count_override=count_override)
    return render(request, 'dashboard/_partials/_processos_list.html', {
        **base_ctx,
        'page': page,
        'processos': page.object_list,
        'total_resultados': page.paginator.count,
    })


@login_required
@require_GET
def processo_detail(request, pk):
    """Shell do detalhe — render rápido (só Process + ProcessoParte). A
    timeline de movimentações vem via HTMX no endpoint separado pra não
    bloquear o first paint quando o processo tem muitas movs (DISTINCT
    + ORDER BY no Postgres pode levar 1s+ por query)."""
    proc = get_object_or_404(Process.objects.select_related('tribunal'), pk=pk)
    participacoes = (
        ProcessoParte.objects.filter(processo=proc)
        .select_related('parte', 'representa__parte')
        .order_by('polo', 'papel')
    )
    polos = {'ativo': [], 'passivo': [], 'outros': []}
    for pp in participacoes:
        polos.setdefault(pp.polo, []).append(pp)

    # Explicação da classificação: pega último ClassificacaoLog + computa
    # contribuições por feature (peso × valor) pra explainability.
    classif_explicacao = None
    if proc.classificacao_em:
        from tribunals.classificador import WEIGHTS, THRESHOLD_PRECATORIO, THRESHOLD_PRE_PRECATORIO, THRESHOLD_DIREITO_CREDITORIO
        from tribunals.models import ClassificacaoLog

        # Catálogo human-friendly de cada feature (emoji + label curto + descrição)
        FEATURE_META = {
            'F1_cumprim': {
                'emoji': '⚖️', 'label': 'Classe Cumprimento contra Fazenda',
                'desc': 'Processo está classificado como uma das variantes de "Cumprimento de Sentença contra a Fazenda Pública" (códigos CNJ 12078, 156, 15160, 15215). É o caminho processual padrão pra emissão de precatório/RPV.',
            },
            'F10_juizado_ANTI': {
                'emoji': '🚫', 'label': 'Juizado Especial / Recurso (anti)',
                'desc': 'Processo é de Juizado Especial, Recurso Inominado ou Procedimento Comum — esses NUNCA terminam em precatório. Sinal forte de que NÃO é lead.',
            },
            'F2_precat_tc': {
                'emoji': '📜', 'label': 'Expedição de precatório/RPV (tipo_comunicacao)',
                'desc': 'Existe movimentação com tipo de comunicação "Expedição de precatório/rpv" ou "Precatório" — sinal direto e estruturado.',
            },
            'F7_envTrib_tc': {
                'emoji': '📤', 'label': 'Enviado ao Tribunal',
                'desc': 'Tem movimentação de "Enviada ao Tribunal" ou "Preparada para Envio" — geralmente o ofício/precatório sendo encaminhado pro TRF.',
            },
            'F11_precat_text': {
                'emoji': '🔎', 'label': 'Texto contém "precatório"',
                'desc': 'Alguma movimentação tem a palavra "precatório" no texto. Captura sinais que não estão nos campos estruturados.',
            },
            'F12_rpv_text': {
                'emoji': '🔎', 'label': 'Texto contém "rpv"',
                'desc': 'Alguma movimentação menciona "RPV" (Requisição de Pequeno Valor) no texto.',
            },
            'F13_reqPag_text': {
                'emoji': '⚠️', 'label': '"Requisição de pagamento" no texto (anti)',
                'desc': 'Aparece muito em processos NÃO-leads (ex: requerimento administrativo). Teve peso negativo no treino.',
            },
            'F14_oficio_text': {
                'emoji': '⚠️', 'label': '"Ofício requisitório" no texto (anti)',
                'desc': 'Apareceu mais em não-leads que em leads no treino. Quando ele aparece sozinho (sem outros sinais), tende a empurrar pra fora.',
            },
            'F15_logMovs': {
                'emoji': '📈', 'label': 'Volume de movimentações (log)',
                'desc': 'log(total movs + 1) normalizado. Leads tipicamente acumulam histórico longo — quanto mais movs, mais provável.',
            },
            'F16_logTipos': {
                'emoji': '🌀', 'label': 'Variedade de tipos de mov (log, anti)',
                'desc': 'Processos "diversificados" (muitos tipos diferentes de mov) tendem a NÃO ser leads — é mais sinal de processo de conhecimento ativo do que cumprimento focado.',
            },
            'F17_logN1count': {
                'emoji': '🎯', 'label': 'Quantidade de palavras N1 no texto',
                'desc': 'Soma das ocorrências de precatório+rpv+req. pagamento+ofício no texto, em log. Muitas menções = sinal forte.',
            },
            'F18_anoZ': {
                'emoji': '📅', 'label': 'Ano do CNJ (z-score)',
                'desc': 'Ano de autuação do processo, normalizado. Modelo tem leve preferência por processos mais recentes.',
            },
            'F19_cancelado_ANTI': {
                'emoji': '❌', 'label': 'Cancelamento/revogação (anti)',
                'desc': 'Tem movimentação de cancelamento ou revogação de precatório/RPV. Anula o lead. Peso treinado próximo de zero porque é raro nas movs públicas (vive nos autos).',
            },
            'F20_exp_juriscope': {
                'emoji': '✅', 'label': 'Termos exatos do filtro Juriscope',
                'desc': 'Texto contém os termos exatos que o Juriscope usa pra confirmar precatório expedido (ex: "precatório expedido", "rpv expedida"). Raro nas movs DJEN/Datajud.',
            },
            'F21_diasUltMovZ': {
                'emoji': '🕰️', 'label': 'Dias desde última mov (z-score)',
                'desc': 'Quantos dias atrás foi a última mov, normalizado. Leads tendem a ter histórico longo, então mov antiga (z>0) ainda contribui positivo (já completou o ciclo).',
            },
            'F23_logPartes': {
                'emoji': '👥', 'label': 'Número de partes (log, anti)',
                'desc': 'Processos com muitas partes (ações coletivas) tendem a NÃO ser leads de precatório individual.',
            },
            'F1xF11': {
                'emoji': '🔗', 'label': 'Cumprimento × precatório no texto',
                'desc': 'Interação: ajusta peso quando ambos F1 e F11 estão ativos juntos (evita dupla contagem).',
            },
            'F1xF15': {
                'emoji': '🔗', 'label': 'Cumprimento × volume movs',
                'desc': 'Sinergia forte: muitas movs DENTRO de classe Cumprimento é sinal extra de lead avançado.',
            },
            'F1xF20': {
                'emoji': '🔗', 'label': 'Cumprimento × termos Juriscope',
                'desc': 'Ajuste fino quando termos Juriscope aparecem em processo de Cumprimento.',
            },
        }

        ultimo_log = ClassificacaoLog.objects.filter(processo=proc).order_by('-criada_em').first()
        feats = (ultimo_log.features_snapshot if ultimo_log else None) or {}
        if feats:
            contribs = []
            for fname, val in feats.items():
                w = WEIGHTS.get(fname, 0.0)
                contrib = w * val
                if abs(contrib) > 0.001:
                    meta = FEATURE_META.get(fname, {'emoji': '•', 'label': fname, 'desc': ''})
                    contribs.append({
                        'feature': fname, 'peso': round(w, 3),
                        'valor': round(val, 3), 'contribuicao': round(contrib, 3),
                        'emoji': meta['emoji'], 'label': meta['label'], 'desc': meta['desc'],
                    })
            contribs.sort(key=lambda x: -abs(x['contribuicao']))

            # Para o "porque dessa categoria" — sumário humano
            classif_resumo = None
            cat = proc.classificacao
            score = proc.classificacao_score or 0
            if cat == 'PRECATORIO':
                classif_resumo = (
                    f'Score {score:.2f} ≥ {THRESHOLD_PRECATORIO} '
                    f'E tem precatório/RPV explícito (F2 ou F11) → 💎 PRECATÓRIO.'
                )
            elif cat == 'PRE_PRECATORIO':
                classif_resumo = (
                    f'Score {score:.2f} ≥ {THRESHOLD_PRE_PRECATORIO} '
                    f'E classe é Cumprimento contra Fazenda, mas SEM expedição explícita → ⏳ PRÉ-PRECATÓRIO.'
                )
            elif cat == 'DIREITO_CREDITORIO':
                classif_resumo = (
                    f'Score {score:.2f} ≥ {THRESHOLD_DIREITO_CREDITORIO} '
                    f'E classe é Cumprimento → 🌱 DIREITO CREDITÓRIO.'
                )
            else:
                classif_resumo = f'Score {score:.2f} abaixo do threshold mínimo OU não bate critério de classe → não-lead.'

            classif_explicacao = {
                'log': ultimo_log,
                'features': feats,
                'contribuicoes': contribs[:12],
                'resumo': classif_resumo,
                'thresholds': {
                    'precatorio': THRESHOLD_PRECATORIO,
                    'pre': THRESHOLD_PRE_PRECATORIO,
                    'direito': THRESHOLD_DIREITO_CREDITORIO,
                },
            }

    return render(request, 'dashboard/processo_detail.html', {
        'processo': proc,
        'polos': polos,
        'classif_explicacao': classif_explicacao,
    })


@login_required
@require_GET
def processo_movs(request, pk):
    """Partial HTMX com timeline de movs + facetas. Carregado lazy pelo
    shell do detalhe (`hx-trigger="load"`) e pelo próprio chip bar
    (filtros aplicados via querystring + hx-get)."""
    proc = get_object_or_404(Process, pk=pk)

    tipos_filtro = _split_csv(request.GET.get('tipo'))
    meios_filtro = _split_csv(request.GET.get('meio'))
    so_ativos = request.GET.get('ativos', '1') == '1'

    movs_qs = Movimentacao.objects.filter(processo=proc).order_by('-data_disponibilizacao', '-id')
    if tipos_filtro:
        movs_qs = movs_qs.filter(tipo_comunicacao__in=tipos_filtro)
    if meios_filtro:
        movs_qs = movs_qs.filter(meio_completo__in=meios_filtro)
    if so_ativos:
        movs_qs = movs_qs.filter(ativo=True)

    tipos_disponiveis = list(
        Movimentacao.objects.filter(processo=proc).exclude(tipo_comunicacao='')
        .values_list('tipo_comunicacao', flat=True).distinct()[:10]
    )
    meios_disponiveis = list(
        Movimentacao.objects.filter(processo=proc).exclude(meio_completo='')
        .values_list('meio_completo', flat=True).distinct()[:6]
    )

    return render(request, 'dashboard/_partials/_processo_movs.html', {
        'processo': proc,
        'movimentacoes': list(movs_qs[:200]),
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
    if proc.tribunal_id not in _ENRICHERS:
        if _is_htmx(request):
            return HttpResponse('Tribunal não suportado.', status=400)
        messages.error(request, f'Enriquecimento ainda não suportado para {proc.tribunal_id}.')
        return redirect('dashboard:processo-detail', pk=pk)
    j = enqueue_enriquecimento_manual(proc.pk)
    return _resposta_job_enfileirado(request, j.id, pk)


@login_required
@require_POST
def processo_sincronizar(request, pk):
    """Dispara sincronização DJEN + Datajud em paralelo na fila manual."""
    from datajud.jobs import datajud_sincronizar_processo
    proc = get_object_or_404(Process, pk=pk)
    sincronizar_movimentacoes.delay(proc.pk)
    j = datajud_sincronizar_processo.delay(proc.pk)
    return _resposta_job_enfileirado(request, j.id, pk)


def _resposta_job_enfileirado(request, job_id: str, processo_pk: int):
    """Resposta unificada pra cliques de update on-demand:
    - HTMX: devolve fragmento com polling pra status do job
    - normal POST: redirect com flash
    """
    if _is_htmx(request):
        return render(request, 'dashboard/_partials/_job_status.html', {
            'job_id': job_id, 'state': 'queued',
        })
    messages.success(request, f'Atualização enfileirada (job {job_id[:8]}). Recarregue em alguns segundos.')
    return redirect('dashboard:processo-detail', pk=processo_pk)


@login_required
@require_GET
def job_status(request, job_id):
    """Polling do status de um job RQ. HTMX swap-fora-do-target.

    Quando o job termina (finished/failed), retorna response com
    HX-Refresh: true — browser recarrega a página inteira pra mostrar
    os dados atualizados.
    """
    import django_rq
    job = None
    for qname in ('manual', 'enrich_trf1', 'enrich_trf3', 'default'):
        q = django_rq.get_queue(qname)
        job = q.fetch_job(job_id)
        if job is not None:
            break
    if job is None:
        # Job sumiu (provável: já terminou e foi limpo do registry).
        # Sinaliza terminado pra recarregar a página.
        resp = HttpResponse(status=204)
        resp['HX-Refresh'] = 'true'
        return resp

    state = job.get_status()
    if state in ('finished', 'failed'):
        resp = HttpResponse(status=204)
        resp['HX-Refresh'] = 'true'
        return resp

    return render(request, 'dashboard/_partials/_job_status.html', {
        'job_id': job_id, 'state': state,
    })


@login_required
@require_GET
def partes(request):
    tipo = request.GET.get('tipo', '').strip()
    q = (request.GET.get('q') or '').strip()
    min_procs = request.GET.get('min_procs', '').strip()
    sort = request.GET.get('sort', '-total_processos').strip()

    SORT_VALIDO = {
        '-total_processos': ('-total_processos', 'nome'),
        'total_processos': ('total_processos', 'nome'),
        '-ultima_aparicao_em': ('-ultima_aparicao_em',),
        'ultima_aparicao_em': ('ultima_aparicao_em',),
        '-primeira_aparicao_em': ('-primeira_aparicao_em',),
        'nome': ('nome',),
        '-nome': ('-nome',),
    }
    order_by = SORT_VALIDO.get(sort, SORT_VALIDO['-total_processos'])

    distribuicao = queries.distribuicao_tipos_partes()
    base_ctx = {
        'tipo_filtro': tipo, 'q': q, 'min_procs': min_procs, 'sort': sort,
        'distribuicao_tipos': distribuicao,
        # `total_partes` é a soma dos buckets por tipo (já consulta GROUP BY
        # tipo) — evita um count() global extra.
        'total_partes': sum(d.get('value', 0) for d in distribuicao),
    }

    if not _is_htmx(request):
        return render(request, 'dashboard/partes.html', base_ctx)

    qs = Parte.objects.all().order_by(*order_by)
    has_filter = False
    if tipo:
        qs = qs.filter(tipo=tipo)
        has_filter = True
    if q and len(q) >= 2:
        qs = qs.filter(Q(nome__icontains=q) | Q(documento__icontains=q) | Q(oab__icontains=q))
        has_filter = True
    if min_procs.isdigit() and int(min_procs) > 0:
        qs = qs.filter(total_processos__gte=int(min_procs))
        has_filter = True
    # Sem filtro a tabela inteira é paginada — reusa a soma do donut
    # (já cacheada) em vez de pagar um SELECT COUNT(*) que faz seq scan.
    count_override = base_ctx['total_partes'] if not has_filter else None
    page = _paginar(qs, request, default_size=50, count_override=count_override)
    return render(request, 'dashboard/_partials/_partes_list.html', {
        **base_ctx,
        'page': page,
        'partes': page.object_list,
        'total': page.paginator.count,
    })


@login_required
@require_GET
def parte_detail(request, pk):
    parte = get_object_or_404(Parte, pk=pk)

    base_qs = ProcessoParte.objects.filter(parte=parte)
    # Conta por polo (sempre, independente do filtro — pra exibir nos chips).
    # Normaliza polo='' → 'outros' pra bater com chart_polo (mesma chave nos
    # dois caminhos; senão template `counts.outros` ficava 0 enquanto donut
    # mostrava N).
    counts = {}
    for row in base_qs.values('polo').annotate(n=Count('id')):
        key = row['polo'] or 'outros'
        counts[key] = counts.get(key, 0) + row['n']

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
        base_qs.values_list('processo__tribunal_id', flat=True).distinct()[:50]
    )

    # Distribuições pros 3 donuts (Tribunal / Papel / Polo). Sempre
    # baseadas em base_qs — independem dos filtros aplicados na lista.
    chart_tribunal = [
        {'name': r['processo__tribunal_id'], 'value': r['n']}
        for r in base_qs.values('processo__tribunal_id').annotate(n=Count('id')).order_by('-n')
    ]
    chart_papel = [
        {'name': r['papel'] or '(sem papel)', 'value': r['n']}
        for r in base_qs.values('papel').annotate(n=Count('id')).order_by('-n')[:10]
    ]
    chart_polo = [
        {'name': r['polo'] or 'outros', 'value': r['n']}
        for r in base_qs.values('polo').annotate(n=Count('id')).order_by('-n')
    ]

    return render(request, 'dashboard/parte_detail.html', {
        'parte': parte,
        'participacoes': qs[:200],
        'counts': counts,
        'polo_filtro': polo_filtro,
        'tribunal_filtro': ','.join(tribunais_filtro),
        'papel_filtro': papel_filtro,
        'papeis_disponiveis': papeis_disponiveis,
        'tribunais_da_parte': tribunais_da_parte,
        'total_filtrado': qs.order_by().count() if (polo_filtro or tribunais_filtro or papel_filtro) else None,
        'chart_tribunal': chart_tribunal,
        'chart_papel': chart_papel,
        'chart_polo': chart_polo,
    })


@login_required
@require_GET
def movimentacoes(request):
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    tipos_filtro = _split_csv(request.GET.get('tipo'))
    meios_filtro = _split_csv(request.GET.get('meio'))
    classes_filtro = _split_csv(request.GET.get('classe'))
    so_ativos = request.GET.get('ativos', '1') == '1'
    q = (request.GET.get('q') or '').strip()
    com_link = request.GET.get('com_link')

    base_ctx = {
        'tribunais': Tribunal.objects.all(),
        'facetas': queries.filtros_movimentacoes(),
        'q': q,
        'tribunal_filtro': ','.join(tribunais_filtro),
        'tipo_filtro': ','.join(tipos_filtro),
        'meio_filtro': ','.join(meios_filtro),
        'classe_filtro': ','.join(classes_filtro),
        'so_ativos': so_ativos,
        'com_link': com_link or '',
    }

    if not _is_htmx(request):
        return render(request, 'dashboard/movimentacoes.html', base_ctx)

    # Mesma estratégia do /processos (commit 55a19a3): order_by('-id')
    # = PK reverse scan, ~aproximadamente cronológico, sem custo do
    # ORDER BY (data_disponibilizacao, id) em 26M rows. Diferença prática:
    # poucas movs têm data_disponibilizacao fora de ordem com id.
    qs = Movimentacao.objects.select_related('tribunal', 'processo').order_by('-id')
    has_filter = False
    if tribunais_filtro:
        qs = qs.filter(tribunal_id__in=tribunais_filtro); has_filter = True
    if tipos_filtro:
        qs = qs.filter(tipo_comunicacao__in=tipos_filtro); has_filter = True
    if meios_filtro:
        qs = qs.filter(meio_completo__in=meios_filtro); has_filter = True
    if classes_filtro:
        qs = qs.filter(nome_classe__in=classes_filtro); has_filter = True
    if so_ativos:
        qs = qs.filter(ativo=True); has_filter = True
    if q and len(q) >= 3:
        qs = qs.filter(texto__icontains=q); has_filter = True
    if com_link == 'sim':
        qs = qs.exclude(link='')
        has_filter = True

    # Sem filtro: 26M rows. Reusa total_movimentacoes do kpis_globais (cache).
    # Se cache frio (kpis pending → None), usa estimativa rápida do reltuples
    # — paginar caindo num count() seq-scan dispara o mesmo timeout que motivou
    # a refatoração da home.
    count_override = None
    if not has_filter:
        kpis = queries.kpis_globais()
        count_override = kpis.get('total_movimentacoes')
        if count_override is None:
            from django.db import connection
            with connection.cursor() as cur:
                cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname='tribunals_movimentacao'")
                row = cur.fetchone()
                count_override = int(row[0]) if row and row[0] else 0

    page = _paginar(qs, request, default_size=50, count_override=count_override)
    return render(request, 'dashboard/_partials/_movimentacoes_list.html', {
        **base_ctx,
        'page': page,
        'movimentacoes': page.object_list,
    })


@login_required
@require_GET
def ingestao(request):
    from datetime import date as date_type
    periodo_dias = _periodo_dias(request, default=90)
    tribunal_filtro = request.GET.get('tribunal', '')

    # Filtros da tabela de runs
    run_tribunal = request.GET.get('run_tribunal', '')
    run_status   = request.GET.get('run_status', '')
    run_de       = request.GET.get('run_de', '')
    run_ate      = request.GET.get('run_ate', '')

    runs_qs = IngestionRun.objects.select_related('tribunal').order_by('-started_at')
    if run_tribunal:
        runs_qs = runs_qs.filter(tribunal_id=run_tribunal)
    if run_status:
        runs_qs = runs_qs.filter(status=run_status)
    if run_de:
        try:
            runs_qs = runs_qs.filter(janela_inicio__gte=date_type.fromisoformat(run_de))
        except ValueError:
            pass
    if run_ate:
        try:
            runs_qs = runs_qs.filter(janela_fim__lte=date_type.fromisoformat(run_ate))
        except ValueError:
            pass
    runs = list(runs_qs[:100])

    return render(request, 'dashboard/ingestao.html', {
        'runs': runs,
        'kpis': queries.ingestao_kpis(tribunal=tribunal_filtro or None),
        'drift_alerts': SchemaDriftAlert.objects.filter(resolvido=False)
                        .select_related('tribunal', 'ingestion_run'),
        'proxies': ProxyScrapePool.singleton().status(),
        'tribunais': Tribunal.objects.filter(ativo=True),
        'periodo_dias': periodo_dias,
        'tribunal_filtro': tribunal_filtro,
        'run_tribunal': run_tribunal,
        'run_status': run_status,
        'run_de': run_de,
        'run_ate': run_ate,
    })


@login_required
@require_GET
def root(request):
    return redirect('dashboard:overview')


# ---------- Consulta rápida (debug Datajud/DJEN, sem persistir) ----------

@login_required
@require_GET
def leads_overview(request):
    """Shell da dashboard de leads — só renderiza filtros + skeletons.
    Cards/charts/tabela carregam lazy via fetch/HTMX.
    """
    return render(request, 'dashboard/leads.html', {
        'tribunais': Tribunal.objects.filter(ativo=True),
        'niveis': [
            ('PRECATORIO', '💎 Precatório'),
            ('PRE_PRECATORIO', '⏳ Pré-precatório'),
            ('DIREITO_CREDITORIO', '🌱 Direito creditório'),
        ],
        'tribunal_filtro': request.GET.get('tribunal', ''),
        'nivel_filtro': request.GET.get('nivel', 'PRECATORIO'),
        'periodo_dias': _periodo_dias(request, default=30),
    })


@login_required
@require_GET
def leads_lista(request):
    """HTMX partial — tabela paginada de leads pendentes (não consumidos).

    Default: top N1 não-consumidos pelo cliente Juriscope.
    """
    from tribunals.models import ApiClient, LeadConsumption

    nivel = (request.GET.get('nivel') or 'PRECATORIO').upper()
    tribunal = (request.GET.get('tribunal') or '').upper()
    cliente_nome = request.GET.get('cliente') or 'juriscope'
    incluir_consumidos = request.GET.get('incluir_consumidos') == '1'

    qs = (
        Process.objects.filter(classificacao=nivel)
        .select_related('tribunal')
        .order_by('-classificacao_score', '-id')
    )
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    cliente = ApiClient.objects.filter(nome=cliente_nome).first()
    if cliente and not incluir_consumidos:
        consumidos_subq = LeadConsumption.objects.filter(cliente=cliente, processo_id=OuterRef('pk'))
        qs = qs.annotate(_consumido=Exists(consumidos_subq)).filter(_consumido=False)

    page = _paginar(qs, request, default_size=50)
    return render(request, 'dashboard/_partials/_leads_lista.html', {
        'page': page, 'leads': page.object_list,
        'nivel': nivel, 'tribunal': tribunal, 'cliente_nome': cliente_nome,
        'incluir_consumidos': incluir_consumidos,
    })


@login_required
@require_GET
def leads_export_csv(request):
    """Exporta CSV dos top N leads pendentes — pra colar na fila do Juriscope."""
    import csv
    from tribunals.models import ApiClient, LeadConsumption

    nivel = (request.GET.get('nivel') or 'PRECATORIO').upper()
    tribunal = (request.GET.get('tribunal') or '').upper()
    try:
        limit = max(100, min(int(request.GET.get('limit', 5000)), 50000))
    except ValueError:
        limit = 5000
    cliente_nome = request.GET.get('cliente') or 'juriscope'

    qs = Process.objects.filter(classificacao=nivel).order_by('-classificacao_score', '-id')
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    cliente = ApiClient.objects.filter(nome=cliente_nome).first()
    if cliente:
        consumidos_subq = LeadConsumption.objects.filter(cliente=cliente, processo_id=OuterRef('pk'))
        qs = qs.annotate(_consumido=Exists(consumidos_subq)).filter(_consumido=False)
    qs = qs[:limit]

    import logging
    logger_ = logging.getLogger('voyager.dashboard.leads_export')
    logger_.info('export CSV: user=%s nivel=%s tribunal=%s limit=%d',
                 request.user.username, nivel, tribunal or '*', limit)

    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="leads_{nivel.lower()}_{tribunal or "todos"}.csv"'
    # BOM pra Excel pt-BR não quebrar acentos
    resp.write('﻿')
    w = csv.writer(resp)
    w.writerow(['rank', 'numero_cnj', 'pid', 'classificacao', 'score', 'classe_codigo', 'classe_nome', 'ano_cnj', 'tribunal'])
    for rank, p in enumerate(qs.iterator(chunk_size=500), 1):
        w.writerow([
            rank, p.numero_cnj, p.pk, p.classificacao,
            f'{p.classificacao_score or 0:.4f}',
            p.classe_codigo or '', p.classe_nome or '', p.ano_cnj or '', p.tribunal_id,
        ])
    return resp


# ===== API endpoints lazy pros widgets da dashboard de leads =====

def _leads_filtros(request):
    """Lê tribunal/nivel/dias/cliente do request com defaults sensatos."""
    tribunal = (request.GET.get('tribunal') or '').upper() or None
    nivel = (request.GET.get('nivel') or '').upper() or None
    cliente_nome = (request.GET.get('cliente') or 'juriscope').strip()
    try:
        dias = max(1, min(int(request.GET.get('dias', 30)), 365))
    except (TypeError, ValueError):
        dias = 30
    return tribunal, nivel, dias, cliente_nome


@login_required
@require_GET
def leads_chart_data(request, key):
    """Endpoint genérico — cada `key` retorna JSON pro respectivo widget.

    Cache 5min agressivo por key + (tribunal, nivel, dias, cliente).

    Caveats:
    - ClassificacaoLog é gravado apenas em TRANSIÇÃO de categoria
      (classificador.classificar_e_persistir). "Descobertos N1" no
      timeseries = transições para PRECATORIO, não primeira ingestão.
    - LeadConsumption permite re-consumo (sem unique). Funil deduplica
      por processo_id + último resultado.
    """
    from datetime import timedelta
    from django.core.cache import cache
    from django.db.models import Count, Exists, OuterRef, Q
    from django.utils import timezone as djtz
    from tribunals.models import ApiClient, ClassificacaoLog, LeadConsumption

    tribunal, nivel, dias, cliente_nome = _leads_filtros(request)
    cache_key = f'dashleads:{key}:t={tribunal or ""}:n={nivel or ""}:d={dias}:c={cliente_nome}'
    cached = _safe_cache_get(cache_key)
    if cached is not None:
        return JsonResponse({'data': cached}, json_dumps_params={'default': str})

    cliente = ApiClient.objects.filter(nome=cliente_nome, ativo=True).first()

    # Anti-join via Exists() pra evitar NOT IN (subquery) caro
    def _excluir_consumidos(qs):
        if not cliente:
            return qs
        return qs.annotate(
            _consumido=Exists(LeadConsumption.objects.filter(
                cliente=cliente, processo_id=OuterRef('pk'),
            )),
        ).filter(_consumido=False)

    base = Process.objects.exclude(classificacao__isnull=True)
    if tribunal:
        base = base.filter(tribunal_id=tribunal)

    data = None

    if key == 'kpis':
        # Counts em uma única query via aggregate(filter=Q(...))
        agg = base.aggregate(
            n1=Count('id', filter=Q(classificacao='PRECATORIO')),
            n2=Count('id', filter=Q(classificacao='PRE_PRECATORIO')),
            n3=Count('id', filter=Q(classificacao='DIREITO_CREDITORIO')),
        )
        n1, n2, n3 = agg['n1'], agg['n2'], agg['n3']

        # n1_pendente: anti-join ao invés de NOT IN
        if cliente:
            n1_qs = _excluir_consumidos(base.filter(classificacao='PRECATORIO'))
            n1_pendente = n1_qs.count()
        else:
            n1_pendente = n1

        # Throughput descobertos/dia (rolling 7d via ClassificacaoLog)
        cutoff7 = djtz.now() - timedelta(days=7)
        descob_qs = ClassificacaoLog.objects.filter(
            classificacao='PRECATORIO', criada_em__gte=cutoff7,
        )
        if tribunal:
            descob_qs = descob_qs.filter(processo__tribunal_id=tribunal)
        descobertos_7d = descob_qs.count()
        throughput_descob = descobertos_7d / 7

        # Throughput consumidos/dia (últimos 7d) — APLICA filtro tribunal pra consistência
        if cliente:
            cons7 = LeadConsumption.objects.filter(cliente=cliente, consumido_em__gte=cutoff7)
            cons_total_qs = LeadConsumption.objects.filter(cliente=cliente)
            if tribunal:
                cons7 = cons7.filter(processo__tribunal_id=tribunal)
                cons_total_qs = cons_total_qs.filter(processo__tribunal_id=tribunal)
            consumidos_7d = cons7.count()
            consumidos_total = cons_total_qs.count()
        else:
            consumidos_7d = 0; consumidos_total = 0
        throughput_cons = consumidos_7d / 7

        # Taxa de validação 30d
        cutoff30 = djtz.now() - timedelta(days=30)
        cons30_total = 0; validados30 = 0; taxa_val = None
        if cliente:
            cons30 = LeadConsumption.objects.filter(cliente=cliente, consumido_em__gte=cutoff30)
            if tribunal:
                cons30 = cons30.filter(processo__tribunal_id=tribunal)
            # Filtra só resultados conclusivos (não conta 'pendente' nem 'erro')
            cons30_conclusivo = cons30.filter(resultado__in=[
                'validado', 'pago', 'sem_expedicao', 'arquivado', 'cedido',
            ])
            cons30_total = cons30_conclusivo.count()
            validados30 = cons30_conclusivo.filter(resultado__in=['validado', 'pago']).count()
            taxa_val = (validados30 / cons30_total) if cons30_total else None

        runway = (n1_pendente / throughput_cons) if throughput_cons > 0 else None
        # Clamp absurdo — backlog gigante não é informação útil em "dias"
        runway_label = None
        if runway is not None:
            if runway > 180:
                runway_label = '180+'
            else:
                runway_label = round(runway, 1)

        data = {
            'juriscope_ativo': cliente is not None,
            'n1_total': n1, 'n2_total': n2, 'n3_total': n3,
            'n1_pendente': n1_pendente,
            'consumidos_total': consumidos_total,
            'throughput_descobertos_dia': round(throughput_descob, 1),
            'throughput_consumidos_dia': round(throughput_cons, 1),
            'taxa_validacao_30d': round(taxa_val, 3) if taxa_val is not None else None,
            'taxa_validacao_treino': 0.939,
            'runway_dias': runway_label,
            'cons30_total': cons30_total, 'validados30': validados30,
        }

    elif key == 'timeseries':
        from django.db.models.functions import TruncDate
        cutoff = djtz.now() - timedelta(days=dias)
        # Descobertos por dia — atenção: ClassificacaoLog só registra
        # TRANSIÇÃO de categoria, então mede "novos N1" não "ingestões".
        desc_qs = ClassificacaoLog.objects.filter(
            classificacao='PRECATORIO', criada_em__gte=cutoff,
        )
        if tribunal:
            desc_qs = desc_qs.filter(processo__tribunal_id=tribunal)
        descobertos = (
            desc_qs.annotate(d=TruncDate('criada_em')).values('d')
            .annotate(n=Count('id')).order_by('d')
        )
        cons_qs = LeadConsumption.objects.filter(consumido_em__gte=cutoff)
        if cliente:
            cons_qs = cons_qs.filter(cliente=cliente)
        if tribunal:
            cons_qs = cons_qs.filter(processo__tribunal_id=tribunal)
        # COUNT(DISTINCT processo_id) já deduplica re-consumos por dia.
        consumidos = (
            cons_qs.annotate(d=TruncDate('consumido_em'))
            .values('d').annotate(n=Count('processo_id', distinct=True))
            .order_by('d')
        )
        data = {
            'descobertos': [{'dia': r['d'].isoformat(), 'n': r['n']} for r in descobertos if r['d']],
            'consumidos': [{'dia': r['d'].isoformat(), 'n': r['n']} for r in consumidos if r['d']],
        }

    elif key == 'calibration':
        # Calibração: agrupa processos consumidos COM resultado conclusivo
        # em decis de score, e mede taxa de validação real por bucket.
        # Conclusivo = exclui 'pendente' e 'erro' (esses não dão sinal de label).
        if not cliente:
            data = {'rows': [], 'sample_size': 0}
        else:
            CONCLUSIVOS = ('validado', 'pago', 'sem_expedicao', 'arquivado', 'cedido')
            POSITIVOS = ('validado', 'pago')
            # Para cada (processo, último resultado conclusivo) — deduplica re-consumos
            from collections import defaultdict
            ultimo_resultado = {}
            cons_qs = (
                LeadConsumption.objects.filter(cliente=cliente, resultado__in=CONCLUSIVOS)
                .order_by('processo_id', '-consumido_em')
                .values('processo_id', 'resultado')
            )
            for c in cons_qs.iterator(chunk_size=5000):
                # primeiro registro de cada processo == último consumido (DESC)
                pid = c['processo_id']
                if pid not in ultimo_resultado:
                    ultimo_resultado[pid] = c['resultado']

            if not ultimo_resultado:
                data = {'rows': [], 'sample_size': 0}
            else:
                # Pega scores em chunks pra evitar IN gigante
                pids = list(ultimo_resultado.keys())
                scores = {}
                for i in range(0, len(pids), 5000):
                    chunk = pids[i:i+5000]
                    for r in (Process.objects.filter(pk__in=chunk,
                                                      classificacao_score__isnull=False)
                              .values('pk', 'classificacao_score')):
                        scores[r['pk']] = r['classificacao_score']

                buckets = []
                for pid, score in scores.items():
                    res = ultimo_resultado.get(pid)
                    if res is None: continue
                    validado = res in POSITIVOS
                    buckets.append((score, validado))

                if not buckets:
                    data = {'rows': [], 'sample_size': 0}
                else:
                    buckets.sort(key=lambda x: x[0])
                    n = len(buckets)
                    rows = []
                    for d in range(10):
                        lo = int(d * n / 10); hi = int((d+1) * n / 10)
                        sl = buckets[lo:hi]
                        if not sl: continue
                        score_med = sum(s for s, _ in sl) / len(sl)
                        taxa = sum(1 for _, v in sl if v) / len(sl)
                        rows.append({
                            'decil': d + 1, 'score_med': round(score_med, 3),
                            'taxa_real': round(taxa, 3), 'n': len(sl),
                        })
                    data = {'rows': rows, 'sample_size': n}

    elif key == 'funnel':
        # Funil ÚLTIMOS N DIAS: descobertos N1 → consumidos (deduplicados
        # por processo, último resultado) → buckets por resultado.
        cutoff = djtz.now() - timedelta(days=dias)
        desc_qs = ClassificacaoLog.objects.filter(
            classificacao='PRECATORIO', criada_em__gte=cutoff,
        )
        if tribunal:
            desc_qs = desc_qs.filter(processo__tribunal_id=tribunal)
        descobertos = desc_qs.count()

        cons_qs = LeadConsumption.objects.filter(consumido_em__gte=cutoff)
        if cliente:
            cons_qs = cons_qs.filter(cliente=cliente)
        if tribunal:
            cons_qs = cons_qs.filter(processo__tribunal_id=tribunal)

        # Deduplica: pega o ÚLTIMO resultado por processo (re-consumo permitido,
        # mas no funil cada processo conta 1x com o estado mais recente).
        ultimo_por_proc = {}
        for c in (cons_qs.order_by('processo_id', '-consumido_em')
                         .values('processo_id', 'resultado')
                         .iterator(chunk_size=5000)):
            pid = c['processo_id']
            if pid not in ultimo_por_proc:
                ultimo_por_proc[pid] = c['resultado']
        from collections import Counter
        por_resultado = dict(Counter(ultimo_por_proc.values()))
        consumidos_total = len(ultimo_por_proc)

        data = {
            'descobertos': descobertos,
            'consumidos_total': consumidos_total,
            'por_resultado': por_resultado,
        }

    elif key == 'by-tribunal':
        rows = list(
            base.values('tribunal_id', 'classificacao')
            .annotate(n=Count('id'))
        )
        agg = {}
        for r in rows:
            t = r['tribunal_id']; c = r['classificacao']
            agg.setdefault(t, {'tribunal': t, 'n1': 0, 'n2': 0, 'n3': 0, 'nao_lead': 0})
            if c == 'PRECATORIO': agg[t]['n1'] = r['n']
            elif c == 'PRE_PRECATORIO': agg[t]['n2'] = r['n']
            elif c == 'DIREITO_CREDITORIO': agg[t]['n3'] = r['n']
            else: agg[t]['nao_lead'] = r['n']
        data = sorted(agg.values(), key=lambda x: -(x['n1'] + x['n2'] + x['n3']))

    elif key == 'distribuicao-score':
        # Histograma de scores N1
        from django.db.models import F
        scores = list(
            base.filter(classificacao='PRECATORIO',
                        classificacao_score__isnull=False)
            .values_list('classificacao_score', flat=True)[:50000]
        )
        # Bins de 0.05
        bins = [0]*20
        for s in scores:
            idx = min(int(s * 20), 19)
            bins[idx] += 1
        data = {'bins': [{'lo': i*0.05, 'hi': (i+1)*0.05, 'n': bins[i]} for i in range(20)]}

    else:
        return JsonResponse({'erro': f'key inválida: {key}'}, status=400)

    cache.set(cache_key, data, timeout=300)
    return JsonResponse({'data': data}, json_dumps_params={'default': str})


@login_required
@require_GET
def api_docs(request):
    """Tela de documentação da API de leads — endpoints, exemplos, etc."""
    from tribunals.models import ApiClient, ClassificadorVersao, LeadConsumption, Process
    from django.db.models import Count

    versao_ativa = ClassificadorVersao.objects.filter(ativa=True).first()
    clientes = ApiClient.objects.filter(ativo=True).only('nome', 'criado_em')

    # Stats globais (sem filtrar por cliente)
    classif_counts = dict(
        Process.objects.exclude(classificacao__isnull=True)
        .values_list('classificacao').annotate(n=Count('id'))
    )
    consumos_total = LeadConsumption.objects.count()
    consumos_resultado = dict(
        LeadConsumption.objects.values_list('resultado').annotate(n=Count('id'))
    )

    return render(request, 'dashboard/api_docs.html', {
        'versao_ativa': versao_ativa,
        'clientes': clientes,
        'classif_counts': classif_counts,
        'consumos_total': consumos_total,
        'consumos_resultado': consumos_resultado,
    })


@login_required
@require_GET
def consulta_rapida(request):
    """Tela de debug — consulta CNJ ao vivo no DJEN e Datajud sem salvar nada."""
    return render(request, 'dashboard/consulta_rapida.html', {
        'tribunais': Tribunal.objects.filter(ativo=True),
    })


@login_required
@require_GET
def consulta_rapida_api(request):
    """API JSON: chama DJEN + Datajud em paralelo e retorna raw + resumo.

    Não usa cache, não persiste — pra debug. Aceita ?cnj=...&tribunal=TRF1&fontes=djen,datajud.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    cnj = (request.GET.get('cnj') or '').strip()
    sigla = (request.GET.get('tribunal') or 'TRF1').strip()
    fontes = set((request.GET.get('fontes') or 'djen,datajud').split(','))

    if not cnj:
        return JsonResponse({'erro': 'cnj obrigatório'}, status=400)

    try:
        tribunal = Tribunal.objects.get(sigla=sigla)
    except Tribunal.DoesNotExist:
        return JsonResponse({'erro': f'tribunal {sigla} não existe'}, status=400)

    def consulta_djen():
        from djen.client import DJENClient
        t0 = time.monotonic()
        try:
            cli = DJENClient(prefer_cortex=True)
            paginas = []
            for items in cli.iter_pages_processo(tribunal.sigla_djen, cnj):
                paginas.append(items)
                if len(paginas) >= 5:
                    break
            flat = [it for pg in paginas for it in pg]
            return {
                'fonte': 'djen',
                'ms': int((time.monotonic() - t0) * 1000),
                'paginas': len(paginas),
                'itens': len(flat),
                'amostra': flat[:5],
            }
        except Exception as e:
            return {'fonte': 'djen', 'erro': str(e)[:300], 'ms': int((time.monotonic() - t0) * 1000)}

    def consulta_datajud():
        from datajud.client import DatajudClient
        from datajud.parser import parse_movimentos
        t0 = time.monotonic()
        try:
            cli = DatajudClient(prefer_cortex=True)
            source = cli.fetch_processo(sigla, cnj)
            if source is None:
                return {'fonte': 'datajud', 'ms': int((time.monotonic()-t0)*1000),
                        'encontrado': False, 'movimentos': 0}
            parsed = parse_movimentos(source)
            return {
                'fonte': 'datajud',
                'ms': int((time.monotonic()-t0)*1000),
                'encontrado': True,
                'numero_processo': source.get('numeroProcesso'),
                'classe': source.get('classe'),
                'sistema': source.get('sistema'),
                'orgao_julgador': source.get('orgaoJulgador'),
                'data_ajuizamento': source.get('dataAjuizamento'),
                'assuntos': source.get('assuntos', []),
                'movimentos_total': len(source.get('movimentos', [])),
                'movimentos_parsed': len(parsed),
                'amostra_raw': source.get('movimentos', [])[:5],
                'amostra_parsed': parsed[:5],
                'source_keys': list(source.keys()),
            }
        except Exception as e:
            return {'fonte': 'datajud', 'erro': str(e)[:300], 'ms': int((time.monotonic()-t0)*1000)}

    resultados = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {}
        if 'djen' in fontes:
            futs['djen'] = ex.submit(consulta_djen)
        if 'datajud' in fontes:
            futs['datajud'] = ex.submit(consulta_datajud)
        for k, fut in futs.items():
            resultados[k] = fut.result()

    return JsonResponse({'cnj': cnj, 'tribunal': sigla, 'resultados': resultados},
                        json_dumps_params={'default': str, 'ensure_ascii': False, 'indent': 2})


# ---------- Wizard de exportação ----------

import csv
import io

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import OuterRef, Subquery
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView

from tribunals.models import Assunto, ClasseJudicial


class _WizardFiltersMixin:
    """Reusa parsing dos filtros (classes/tribunais/assuntos) entre Count/Export."""

    def filtered_queryset(self):
        classes = _split_csv(self.request.GET.get('classes'))
        tribs = _split_csv(self.request.GET.get('tribunais'))
        assuntos = _split_csv(self.request.GET.get('assuntos'))
        qs = Process.objects.all()
        if classes:
            qs = qs.filter(classe_codigo__in=classes)
        if tribs:
            qs = qs.filter(tribunal_id__in=tribs)
        if assuntos:
            qs = qs.filter(assunto_codigo__in=assuntos)
        return qs


@method_decorator(require_GET, name='dispatch')
class WizardView(LoginRequiredMixin, TemplateView):
    """Renderiza o shell do wizard. Toda a interação acontece client-side via
    Alpine; count e export são endpoints separados (CBVs abaixo)."""
    template_name = 'dashboard/wizard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['classes'] = list(
            ClasseJudicial.objects.all()
            .order_by('-total_processos', 'nome')
            .values('codigo', 'nome', 'total_processos')
        )
        ctx['tribunais'] = list(
            Tribunal.objects.filter(ativo=True).order_by('sigla')
            .values('sigla', 'nome')
        )
        ctx['assuntos'] = list(
            Assunto.objects.all()
            .order_by('-total_processos', 'nome')
            .values('codigo', 'nome', 'total_processos')[:500]
        )
        return ctx


@method_decorator(require_GET, name='dispatch')
class WizardCountView(LoginRequiredMixin, _WizardFiltersMixin, View):
    """Devolve fragmento HTMX com a contagem do filtro corrente."""

    def get(self, request, *args, **kwargs):
        cache_key = f'wizard_count:{request.GET.urlencode()}'
        count = _safe_cache_get(cache_key)
        if count is None:
            count = self.filtered_queryset().count()
            try:
                cache.set(cache_key, count, timeout=60)
            except Exception:
                pass
        return render(request, 'dashboard/_partials/_wizard_count.html', {'count': count})


@method_decorator(require_GET, name='dispatch')
class WizardExportView(LoginRequiredMixin, _WizardFiltersMixin, View):
    """Streama CSV ou XLSX com dados básicos do processo + última movimentação.

    Campos: tribunal, cnj, ano, classe, assunto, órgão julgador, data autuação,
    valor causa, total movs, primeira/última movimentação, status enriquecimento,
    último texto da movimentação (truncado a 500 chars).
    """

    HEADER = [
        'tribunal', 'numero_cnj', 'ano_cnj', 'classe_codigo', 'classe_nome',
        'assunto_codigo', 'assunto_nome', 'orgao_julgador', 'data_autuacao',
        'valor_causa', 'total_movimentacoes', 'primeira_movimentacao_em',
        'ultima_movimentacao_em', 'enriquecimento_status',
        'ultima_mov_data', 'ultima_mov_tipo', 'ultima_mov_orgao', 'ultima_mov_texto',
    ]

    def get(self, request, *args, **kwargs):
        fmt = (request.GET.get('format') or 'csv').lower()
        qs = (self.filtered_queryset()
              .select_related('tribunal', 'classe', 'assunto')
              .order_by('-ultima_movimentacao_em'))
        if fmt == 'xlsx':
            return self._render_xlsx(qs)
        return self._render_csv(qs)

    def _annotate_ultima_mov(self, qs):
        """Subquery única evita N+1 — pega a última mov por processo."""
        latest = Movimentacao.objects.filter(processo=OuterRef('pk')).order_by('-data_disponibilizacao')
        return qs.annotate(
            ultima_mov_data=Subquery(latest.values('data_disponibilizacao')[:1]),
            ultima_mov_tipo=Subquery(latest.values('tipo_comunicacao')[:1]),
            ultima_mov_orgao=Subquery(latest.values('nome_orgao')[:1]),
            ultima_mov_texto=Subquery(latest.values('texto')[:1]),
        )

    def _row_for(self, p):
        return [
            p.tribunal_id, p.numero_cnj, p.ano_cnj or '',
            p.classe_codigo, p.classe_nome,
            p.assunto_codigo, p.assunto_nome,
            p.orgao_julgador_nome,
            p.data_autuacao.isoformat() if p.data_autuacao else '',
            str(p.valor_causa) if p.valor_causa is not None else '',
            p.total_movimentacoes,
            p.primeira_movimentacao_em.isoformat() if p.primeira_movimentacao_em else '',
            p.ultima_movimentacao_em.isoformat() if p.ultima_movimentacao_em else '',
            p.enriquecimento_status,
            p.ultima_mov_data.isoformat() if p.ultima_mov_data else '',
            p.ultima_mov_tipo or '',
            p.ultima_mov_orgao or '',
            (p.ultima_mov_texto or '')[:500],
        ]

    def _render_csv(self, qs):
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename="voyager-processos.csv"'
        writer = csv.writer(resp)
        writer.writerow(self.HEADER)
        for p in self._annotate_ultima_mov(qs).iterator(chunk_size=500):
            writer.writerow(self._row_for(p))
        return resp

    def _render_xlsx(self, qs):
        from openpyxl import Workbook
        wb = Workbook(write_only=True)
        ws = wb.create_sheet('processos')
        ws.append(self.HEADER)
        for p in self._annotate_ultima_mov(qs).iterator(chunk_size=500):
            ws.append(self._row_for(p))
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = 'attachment; filename="voyager-processos.xlsx"'
        return resp
