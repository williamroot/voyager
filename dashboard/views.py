import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from djen.jobs import sincronizar_movimentacoes
from djen.proxies import ProxyScrapePool
from enrichers.jobs import _ENRICHERS, enqueue_enriquecimento, enqueue_enriquecimento_manual
from tribunals.models import (
    IngestionRun, Movimentacao, Parte, PartePapel, ParteTribunal, Process,
    ProcessoParte, SchemaDriftAlert, Tribunal,
)

from . import queries

logger = logging.getLogger(__name__)


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
# Com 27 TJs + 6 TRFs ativos, o donut/stacked "por tribunal" viram espaguete
# (33 séries, labels sobrepostos). Mantém os top-N por volume e agrega o resto
# em "Outros" — legível sem perder o total. Não colapsa quando há filtro de
# tribunal explícito (o usuário escolheu o recorte) nem na visão de 1 tribunal.
TOP_N_TRIBUNAIS = 12
OUTROS_LABEL = 'Outros'


def _colapsar_donut(rows, n=TOP_N_TRIBUNAIS):
    """rows: [{'tribunal','total'}] já ordenado por -total."""
    if len(rows) <= n + 1:
        return rows
    outros = sum(r['total'] for r in rows[n:])
    top = list(rows[:n])
    if outros:
        top.append({'tribunal': OUTROS_LABEL, 'total': outros})
    return top


def _colapsar_temporal(rows, n=TOP_N_TRIBUNAIS):
    """rows: [{'dia','tribunal','total','parcial'}]. Mantém os top-N tribunais
    por volume total no período; soma o resto em 'Outros' por dia."""
    from collections import defaultdict
    totais = defaultdict(int)
    for r in rows:
        totais[r['tribunal']] += r['total']
    if len(totais) <= n + 1:
        return rows
    top = {t for t, _ in sorted(totais.items(), key=lambda kv: -kv[1])[:n]}
    out = []
    outros = defaultdict(lambda: [0, False])  # dia -> [total, parcial]
    for r in rows:
        if r['tribunal'] in top:
            out.append(r)
        else:
            acc = outros[r['dia']]
            acc[0] += r['total']
            acc[1] = acc[1] or r.get('parcial', False)
    for dia, (total, parcial) in outros.items():
        out.append({'dia': dia, 'tribunal': OUTROS_LABEL, 'total': total, 'parcial': parcial})
    return out


def _chart_volume_temporal(dias, tribunais, sigla):
    if sigla:
        return queries.volume_temporal(dias=dias, tribunais=[sigla])
    rows = queries.volume_temporal(dias=dias, tribunais=tribunais)
    return rows if tribunais else _colapsar_temporal(rows)


def _chart_distribuicao(dias, tribunais, sigla):
    if sigla:
        return queries.distribuicao_por_tribunal(dias=dias, tribunais=[sigla])
    rows = queries.distribuicao_por_tribunal(dias=dias, tribunais=tribunais)
    return rows if tribunais else _colapsar_donut(rows)


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


def _chart_pipeline_grid(dias, tribunais, sigla):
    return queries.pipeline_saude_grid(dias=dias, tribunais=[sigla] if sigla else tribunais)


def _chart_pipeline_temporal(dias, tribunais, sigla):
    return queries.pipeline_volume_temporal(dias=dias, tribunais=[sigla] if sigla else tribunais)


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
    'pipeline-grid': _chart_pipeline_grid,
    'pipeline-temporal': _chart_pipeline_temporal,
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
            if cached is None:
                return JsonResponse({'data': [], 'pending': True},
                                    json_dumps_params={'default': str})
            # Payload legado (lista pura) pode estar no cache logo após deploy.
            if isinstance(cached, list):
                return JsonResponse({'data': cached, 'pending': False},
                                    json_dumps_params={'default': str})
            return JsonResponse({
                'data': cached.get('rows', []),
                'stale': cached.get('stale', False),
                'mv_max_hora': cached.get('mv_max_hora'),
                'idade_horas': cached.get('idade_horas'),
                'pending': False,
            }, json_dumps_params={'default': str})
        # Filtro custom (por sigla/tribunal): computa on-demand — não pré-aquecido.
        payload = queries.ingestion_rate_por_hora(horas=horas, tribunais=[sigla] if sigla else tribunais_filtro or None)
        return JsonResponse({
            'data': payload['rows'],
            'stale': payload['stale'],
            'mv_max_hora': payload['mv_max_hora'],
            'idade_horas': payload['idade_horas'],
        }, json_dumps_params={'default': str})

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
def jurimetria(request):
    """Jurimetria de acórdãos — consome a API de agregação do Zordon (M2).

    Determinístico: os números vêm do SQL do Zordon; a página só apresenta +
    mostra a proveniência (_meta). Fonte selecionável (STJ por ora).
    """
    from . import zordon_client
    fonte = (request.GET.get('fonte') or 'STJ').upper()
    ctx = {
        'fonte': fonte,
        'resumo': zordon_client.jurimetria('resumo', fonte=fonte),
        'orgaos': zordon_client.jurimetria('orgaos', fonte=fonte),
        'relatores': zordon_client.jurimetria('relatores', fonte=fonte, limit=15),
        'temas': zordon_client.jurimetria('temas', fonte=fonte, limit=15),
    }
    # Templates do Django não acessam chaves com underscore → remapeia _meta→meta.
    for k in ('resumo', 'orgaos', 'relatores', 'temas'):
        d = ctx[k]
        if isinstance(d, dict) and '_meta' in d:
            d['meta'] = d['_meta']
    return render(request, 'dashboard/jurimetria.html', ctx)


@login_required
def jurimetria_dossie(request):
    """Dossiê de jurimetria por CNJ (M3): Voyager + Juriscope + Zordon numa página."""
    from .jurimetria_dossie import montar_dossie, fontes_e_pesos, score_oportunidade
    cnj = (request.GET.get('cnj') or '').strip()
    dossie, fontes, score, teses = None, None, None, None
    if cnj:
        # Guard definitivo: qualquer falha (survival, Juriscope, Zordon, template)
        # vira um card de erro — o dossiê NUNCA responde 500.
        try:
            dossie = montar_dossie(cnj)
            if dossie and dossie.get('cabecalho'):
                fontes = fontes_e_pesos(dossie)
                score = score_oportunidade(dossie)
                assunto = (dossie.get('cabecalho') or {}).get('assunto_nome') or ''
                if assunto and assunto != '—':
                    try:
                        from . import fontes_publicas
                        t = fontes_publicas.stj_temas_repetitivos(assunto, limit=3)
                        teses = t.get('temas') if t and not t.get('erro') else None
                    except Exception:  # noqa: BLE001
                        teses = None
        except Exception as exc:  # noqa: BLE001
            logger.exception('montar_dossie 500 p/ %s', cnj)
            dossie = {'cnj': cnj, 'erro': f'Falha ao montar o dossiê ({type(exc).__name__}). '
                      f'O time foi notificado; tente novamente em instantes.'}
    return render(request, 'dashboard/jurimetria_dossie.html',
                  {'cnj_input': cnj, 'dossie': dossie, 'fontes': fontes,
                   'score': score, 'teses_stj': teses})


@login_required
def jurimetria_prompt(request):
    """Ver/editar o prompt do sistema da narrativa de jurimetria (pra transparência/tuning).
    GET → JSON {prompt, is_override}; POST (prompt) → salva override; POST vazio → reseta."""
    from django.http import JsonResponse
    from django.utils import timezone
    from .jurimetria_narrativa import (get_system_prompt, set_system_prompt, _SYSTEM_DEFAULT,
                                       get_prompt_history, append_prompt_history)
    if request.method == 'POST':
        novo = request.POST.get('prompt', '')
        antes = get_system_prompt()
        set_system_prompt(novo)
        depois = get_system_prompt()
        if depois != antes:  # audita só mudanças reais
            append_prompt_history({
                'ts': timezone.now().strftime('%d/%m/%Y %H:%M'),
                'user': request.user.get_username(),
                'acao': 'restaurado o padrão' if depois == _SYSTEM_DEFAULT else 'editado',
                'chars': len(depois), 'prompt': depois, 'preview': depois[:90]})
        return JsonResponse({'ok': True, 'prompt': depois, 'is_override': depois != _SYSTEM_DEFAULT,
                             'history': get_prompt_history()})
    return JsonResponse({'prompt': get_system_prompt(), 'default': _SYSTEM_DEFAULT,
                         'is_override': get_system_prompt() != _SYSTEM_DEFAULT,
                         'history': get_prompt_history()})


def jurimetria_dossie_narrativa(request):
    """Poll da análise jurimétrica por IA. Gera em background (thread do web, que tem
    LLM+Zordon) e cacheia; o front faz poll até 'pronto'. Sem conexão HTTP longa (o LLM
    leva ~60-90s e gunicorn/nginx/cloudflare cortavam → 500/'indisponível')."""
    from django.http import JsonResponse
    from django.core.cache import caches
    cnj = (request.GET.get('cnj') or '').strip()
    if not cnj:
        return JsonResponse({'estado': 'erro', 'html': None})
    from .jurimetria_narrativa import iniciar_ou_obter
    try:
        html, estado = iniciar_ou_obter(cnj)
    except Exception:  # noqa: BLE001
        logger.exception('narrativa poll falhou %s', cnj)
        html, estado = None, 'erro'
    return JsonResponse({'estado': estado, 'html': html})


def jurimetria_dossie_narrativa_stream(request):
    """SSE: streaming da análise jurimétrica — mostra o 'pensando' (reasoning) e vai
    preenchendo o HTML conforme gera. Heartbeat via thread+queue pra não estourar o
    timeout do gunicorn durante a latência do modelo de reasoning."""
    import json
    import queue
    import threading
    from django.http import StreamingHttpResponse
    from .jurimetria_narrativa import gerar_stream

    cnj = (request.GET.get('cnj') or '').strip()
    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def _produce():
        try:
            for ev in gerar_stream(cnj):
                q.put(ev)
        except Exception:  # noqa: BLE001
            logger.exception('gerar_stream falhou')
            q.put({'type': 'error', 'text': 'Falha ao gerar a análise.'})
        finally:
            q.put(_SENTINEL)

    def _sse():
        threading.Thread(target=_produce, daemon=True).start()
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                yield ': keepalive\n\n'  # comentário SSE — mantém a conexão viva
                continue
            if ev is _SENTINEL:
                yield 'event: end\ndata: {}\n\n'
                return
            yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'

    resp = StreamingHttpResponse(_sse(), content_type='text/event-stream; charset=utf-8')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'  # desliga buffering do nginx
    return resp


# ---------------- Chat de jurimetria (agente conversacional) ----------------

@login_required
@never_cache
def jurimetria_chat(request):
    """Página do chat de jurimetria — sidebar de conversas + pane. ?cnj= abre uma
    conversa nova ancorada no processo; ?sessao=<uuid> reabre uma existente.
    never_cache: página com JS inline que evolui — browser não pode reusar HTML velho."""
    return render(request, 'dashboard/jurimetria_chat.html', {
        'cnj_input': (request.GET.get('cnj') or '').strip(),
        'sessao_input': (request.GET.get('sessao') or '').strip(),
    })


@login_required
@require_POST
def jurimetria_chat_stream(request):
    """SSE do turno do chat. POST JSON {session|null, message, cnj?, regenerate?}.
    session=null cria a sessão on-the-fly (1º evento devolve o uuid). Lock por
    sessão contra turnos concorrentes. Mesmo esqueleto Queue+thread+heartbeat da
    narrativa (o turno de agente com tools passa fácil de 90s)."""
    import json
    import queue
    import threading
    from django.http import StreamingHttpResponse
    from . import jurimetria_chat as jc
    from .models import ChatSession

    try:
        body = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'erro': 'JSON inválido'}, status=400)
    message = (body.get('message') or '').strip()
    regenerate = bool(body.get('regenerate'))
    cnj = (body.get('cnj') or '').strip()[:30]
    sess_uuid = (body.get('session') or '').strip() or None

    nova_sessao = False
    if sess_uuid:
        try:
            session = ChatSession.objects.get(uuid=sess_uuid, user=request.user)
        except Exception:  # noqa: BLE001 — uuid inválido ou inexistente
            return JsonResponse({'erro': 'sessão não encontrada'}, status=404)
    else:
        if not message:
            return JsonResponse({'erro': 'mensagem vazia'}, status=400)
        session = ChatSession.objects.create(user=request.user, cnj_contexto=cnj)
        nova_sessao = True

    lock_key = f'jurchat:lock:{session.uuid}'
    if not cache.add(lock_key, '1', timeout=300):
        def _busy():
            yield ('data: ' + json.dumps({'type': 'error', 'code': 'turno_em_andamento',
                   'text': 'Já há uma resposta sendo gerada nesta conversa.'}) + '\n\n')
            yield 'event: end\ndata: {}\n\n'
        resp = StreamingHttpResponse(_busy(), content_type='text/event-stream; charset=utf-8')
        resp['Cache-Control'] = 'no-cache'
        resp['X-Accel-Buffering'] = 'no'
        return resp

    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def _produce():
        try:
            for ev in jc.responder_stream(session, message, regenerate=regenerate):
                q.put(ev)
        except Exception:  # noqa: BLE001
            logger.exception('chat responder_stream falhou (sessao %s)', session.uuid)
            q.put({'type': 'error', 'code': 'interno',
                   'text': 'Falha interna ao gerar a resposta.'})
        finally:
            cache.delete(lock_key)
            q.put(_SENTINEL)

    def _sse():
        if nova_sessao:
            yield ('data: ' + json.dumps({'type': 'session', 'session': str(session.uuid),
                   'title': session.title}, ensure_ascii=False) + '\n\n')
        threading.Thread(target=_produce, daemon=True).start()
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                yield ': keepalive\n\n'
                continue
            if ev is _SENTINEL:
                yield 'event: end\ndata: {}\n\n'
                return
            yield f'data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n'

    resp = StreamingHttpResponse(_sse(), content_type='text/event-stream; charset=utf-8')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'
    return resp


@login_required
def jurimetria_chat_sessoes(request):
    """GET → lista de conversas do usuário. POST → cria conversa vazia."""
    from .models import ChatSession
    if request.method == 'POST':
        cnj = (request.POST.get('cnj') or '').strip()[:30]
        s = ChatSession.objects.create(user=request.user, cnj_contexto=cnj)
        return JsonResponse({'session': str(s.uuid), 'title': s.title})
    sessoes = ChatSession.objects.filter(user=request.user)[:50]
    return JsonResponse({'sessoes': [
        {'uuid': str(s.uuid), 'title': s.title, 'cnj': s.cnj_contexto,
         'ultima': s.last_message_at.strftime('%d/%m %H:%M') if s.last_message_at else ''}
        for s in sessoes]})


@login_required
def jurimetria_chat_sessao(request, sess_uuid):
    """GET → mensagens da conversa (blocks p/ reidratar a UI). PATCH → renomeia.
    DELETE → apaga. Sempre restrito ao dono."""
    import json
    from .models import ChatSession
    session = get_object_or_404(ChatSession, uuid=sess_uuid, user=request.user)
    if request.method == 'DELETE':
        session.delete()
        return JsonResponse({'ok': True})
    if request.method == 'PATCH':
        try:
            title = (json.loads(request.body or b'{}').get('title') or '').strip()[:255]
        except (ValueError, TypeError):
            title = ''
        if title:
            session.title = title
            session.save(update_fields=['title'])
        return JsonResponse({'ok': True, 'title': session.title})
    from .jurimetria_chat import fmt_chat
    msgs = []
    for m in session.messages.all():
        blocks = (m.content_json or {}).get('blocks') or []
        html = ''
        if m.role == 'assistant':
            txt = m.texto()
            if txt:
                html = fmt_chat(txt)
        msgs.append({'id': m.pk, 'role': m.role, 'blocks': blocks, 'html': html,
                     'texto': m.texto(), 'model': m.model})
    return JsonResponse({'uuid': str(session.uuid), 'title': session.title,
                         'cnj': session.cnj_contexto, 'mensagens': msgs})


def _chat_redis():
    import django_rq
    return django_rq.get_connection('classificacao')  # qualquer fila → mesmo REDIS_URL


@login_required
@require_POST
def jurimetria_chat_enviar(request):
    """Dispara um turno do chat SEM conexão longa (o padrão que sobrevive a
    gunicorn/nginx/cloudflare nesta infra — igual à narrativa do dossiê):
    valida, inicia a thread produtora que grava os eventos numa lista Redis e
    retorna NA HORA {turno, session}. A UI consome via jurimetria_chat_eventos."""
    import json
    import threading
    import uuid as _uuid
    from . import jurimetria_chat as jc
    from .models import ChatSession

    try:
        body = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'erro': 'JSON inválido'}, status=400)
    message = (body.get('message') or '').strip()
    regenerate = bool(body.get('regenerate'))
    cnj = (body.get('cnj') or '').strip()[:30]
    sess_uuid = (body.get('session') or '').strip() or None

    if sess_uuid:
        try:
            session = ChatSession.objects.get(uuid=sess_uuid, user=request.user)
        except Exception:  # noqa: BLE001
            return JsonResponse({'erro': 'sessão não encontrada'}, status=404)
    else:
        if not message:
            return JsonResponse({'erro': 'mensagem vazia'}, status=400)
        session = ChatSession.objects.create(user=request.user, cnj_contexto=cnj)

    lock_key = f'jurchat:lock:{session.uuid}'
    if not cache.add(lock_key, '1', timeout=600):
        return JsonResponse({'erro': 'turno_em_andamento'}, status=409)

    turno = str(_uuid.uuid4())
    ev_key = f'jurchat:ev:{turno}'

    def _produce():
        import json as _json
        r = _chat_redis()
        try:
            for ev in jc.responder_stream(session, message, regenerate=regenerate):
                r.rpush(ev_key, _json.dumps(ev, ensure_ascii=False, default=str))
                r.expire(ev_key, 900)
        except Exception:  # noqa: BLE001
            logger.exception('chat turno falhou (sessao %s)', session.uuid)
            r.rpush(ev_key, _json.dumps({'type': 'error', 'code': 'interno',
                    'text': 'Falha interna ao gerar a resposta.'}))
            r.expire(ev_key, 900)
        finally:
            cache.delete(lock_key)

    threading.Thread(target=_produce, daemon=True).start()
    return JsonResponse({'turno': turno, 'session': str(session.uuid),
                         'title': session.title})


@login_required
@require_GET
def jurimetria_chat_eventos(request):
    """Poll dos eventos de um turno: ?turno=<uuid>&desde=<n> → {eventos, proximo,
    fim}. Requisições curtas — imunes a timeout de worker/proxy."""
    import json
    turno = (request.GET.get('turno') or '').strip()
    try:
        desde = max(int(request.GET.get('desde') or 0), 0)
    except ValueError:
        desde = 0
    if not turno or len(turno) > 40:
        return JsonResponse({'erro': 'turno inválido'}, status=400)
    r = _chat_redis()
    raw = r.lrange(f'jurchat:ev:{turno}', desde, desde + 199)
    eventos = []
    fim = False
    for item in raw:
        try:
            ev = json.loads(item)
        except (ValueError, TypeError):
            continue
        eventos.append(ev)
        if ev.get('type') in ('done', 'error'):
            fim = True
    return JsonResponse({'eventos': eventos, 'proximo': desde + len(raw), 'fim': fim})


@login_required
@require_POST
def jurimetria_chat_upload(request):
    """Upload de anexo do chat (multipart, campo 'arquivo'). Extrai o texto na hora
    (pdf/xlsx/txt...) e devolve {file_id, filename, chars} — a UI insere o marcador
    [arquivo: nome #id] na mensagem e o agente lê via tool `ler_arquivo`."""
    from . import chat_arquivos
    from .models import ChatFile
    f = request.FILES.get('arquivo')
    if not f:
        return JsonResponse({'erro': 'nenhum arquivo enviado'}, status=400)
    if f.size > chat_arquivos.MAX_UPLOAD_MB * 1024 * 1024:
        return JsonResponse({'erro': f'arquivo maior que {chat_arquivos.MAX_UPLOAD_MB}MB'}, status=400)
    texto, erro = chat_arquivos.extrair_texto(f.name, f.read())
    if erro:
        return JsonResponse({'erro': erro}, status=422)
    cf = ChatFile.objects.create(user=request.user, filename=f.name[:255],
                                 mime=(f.content_type or '')[:100],
                                 texto=texto, chars=len(texto))
    return JsonResponse({'file_id': str(cf.uuid), 'filename': cf.filename, 'chars': cf.chars})


@login_required
def jurimetria_chat_prompt(request):
    """Ver/editar o prompt do sistema do CHAT (transparência/tuning) — espelho do
    jurimetria_prompt da narrativa, com chaves próprias."""
    from django.utils import timezone
    from . import jurimetria_chat as jc
    if request.method == 'POST':
        novo = request.POST.get('prompt', '')
        antes = jc.get_system_prompt()
        jc.set_system_prompt(novo)
        depois = jc.get_system_prompt()
        if depois != antes:
            jc.append_prompt_history({
                'ts': timezone.now().strftime('%d/%m/%Y %H:%M'),
                'user': request.user.get_username(),
                'acao': 'editado' if jc.is_override() else 'restaurado o padrão',
                'chars': len(depois), 'prompt': depois, 'preview': depois[:90]})
        return JsonResponse({'ok': True, 'prompt': depois, 'is_override': jc.is_override(),
                             'history': jc.get_prompt_history()})
    return JsonResponse({'prompt': jc.get_system_prompt(), 'default': jc.get_default_prompt(),
                         'is_override': jc.is_override(), 'history': jc.get_prompt_history()})


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
    # KPIs lidos da MV `mv_tribunal_kpis` (refresh diário CONCURRENTLY no cron
    # `refresh_materialized_views`). Inline morria por timeout em tribunais
    # grandes — TJMG tem 212M movs e DISTINCT órgão/classe leva ~225s cada
    # (>> 30s gunicorn). Ver migration tribunals/0031.
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute(
            'SELECT total_processos, total_movs, cancelados, orgaos_unicos, classes_unicas '
            'FROM mv_tribunal_kpis WHERE sigla = %s', [t.sigla])
        row = cur.fetchone()
    if row:
        kpis_t = {
            'total_processos': row[0],
            'total_movs':      row[1],
            'cancelados':      row[2],
            'orgaos_unicos':   row[3],
            'classes_unicas':  row[4],
        }
    else:
        # MV ainda sem dados (pré-primeiro refresh) ou tribunal novo sem rows.
        kpis_t = {
            'total_processos': None, 'total_movs': None, 'cancelados': None,
            'orgaos_unicos': None, 'classes_unicas': None, 'pending': True,
        }
    ctx = {
        'tribunal': t,
        **kpis_t,
        'periodo_dias': dias,
    }
    return render(request, 'dashboard/tribunal_detail.html', ctx)


def _timeline_geometry(d):
    """Posições % dos marcos da linha do tempo de cobertura de um tribunal.

    Eixo: de min(início disponível, 1ª mov) até hoje. Retorna None se o
    tribunal ainda não tem movimentação mapeada.
    """
    from datetime import date as _date

    def _as_date(x):
        if x is None:
            return None
        return x.date() if hasattr(x, 'date') else x

    inicio = _as_date(d.get('data_inicio'))
    primeira = _as_date(d.get('primeira_mov'))
    ultima = _as_date(d.get('ultima_mov'))
    if not primeira or not ultima:
        return None
    hoje = _date.today()
    t0 = min(x for x in (inicio, primeira) if x)
    span = max((hoje - t0).days, 1)

    def pct(x):
        return round(100 * (x - t0).days / span, 2)

    inicio_pct = pct(inicio) if inicio else 0.0
    primeira_pct = pct(primeira)
    ultima_pct = pct(ultima)
    return {
        'inicio': inicio, 'primeira': primeira, 'ultima': ultima, 'hoje': hoje,
        'inicio_pct': inicio_pct,
        'primeira_pct': primeira_pct,
        'ultima_pct': ultima_pct,
        'gap_w': round(max(primeira_pct - inicio_pct, 0), 2),   # gap início→1ª mov
        'fill_w': round(max(ultima_pct - primeira_pct, 0), 2),  # janela coberta
        'lag_w': round(max(100 - ultima_pct, 0), 2),            # lag última mov→hoje
        'dias_desde_ultima': (hoje - ultima).days,
    }


@login_required
@require_GET
def tribunal_status(request):
    """Status / linha do tempo por tribunal: visão geral de todos +
    detalhe (drill-down) do tribunal selecionado. Lê só do warm cache."""
    sigla = request.GET.get('tribunal') or None
    overview, detalhe, pending = queries.tribunal_status_data(sigla)

    for row in overview:
        if row.get('pending'):
            row['mini'] = None
            row['lag_max'] = None
            continue
        row['mini'] = _timeline_geometry(row)
        lags = [v for v in (row.get('lag_datajud'), row.get('lag_classificacao'))
                if v is not None]
        row['lag_max'] = max(lags) if lags else None

    timeline = None
    volume_rows: list = []
    ano_rows: list = []
    if detalhe and not detalhe.get('pending'):
        timeline = _timeline_geometry(detalhe)
        sig = detalhe['sigla']
        volume_rows = [
            {'dia': f'{mes}-01', 'tribunal': sig, 'total': n}
            for mes, n in detalhe.get('volume_mensal', [])
        ]
        ano_rows = [
            {'ano': ano, 'total': n} for ano, n in detalhe.get('ano_cnj', [])
        ]

    return render(request, 'dashboard/tribunal_status.html', {
        'overview': overview,
        'detalhe': detalhe,
        'timeline': timeline,
        'volume_rows': volume_rows,
        'ano_rows': ano_rows,
        'pending': pending,
        'sigla_sel': detalhe.get('sigla') if detalhe else sigla,
    })


@login_required
@require_GET
def processos(request):
    tribunais_filtro = _split_csv(request.GET.get('tribunal'))
    enriq = request.GET.get('enriq')
    extraido = request.GET.get('extraido') == '1'

    base_ctx = {
        'tribunais': Tribunal.objects.all(),
        'tribunal_filtro': ','.join(tribunais_filtro),
        'enriq_filtro': enriq or '',
        'extraido_filtro': extraido,
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
    if extraido:
        # Filtra pelos CNJs com metadados extraídos (lista vem do Zordon, cache 60s
        # pois cresce durante o backfill). Conjunto pequeno → IN em numero_cnj (index).
        from django.core.cache import cache

        from . import zordon_client
        cnjs = cache.get('metadados_extraidos_cnjs')
        if cnjs is None:
            cnjs = zordon_client.metadados_extraidos().get('cnjs', [])
            cache.set('metadados_extraidos_cnjs', cnjs, 60)
        qs = qs.filter(numero_cnj__in=cnjs)
        has_filter = True

    # Sem filtro: 500k+ rows. Reusa total_processos do kpis_globais (cache).
    # Se cache frio, usa estimativa reltuples — evita seq-scan no COUNT(*).
    # Com filtro (1 tribunal +/- status): consome cache `estatisticas_por_tribunal`
    # (warm 30min) — sem ele, COUNT(*) em TJSP+ok leva ~14s e a página trava.
    count_override = None
    if extraido:
        pass  # conjunto pequeno (só extraídos) → paginator conta direto (index numero_cnj)
    elif not has_filter:
        kpis = queries.kpis_globais()
        count_override = kpis.get('total_processos')
        if count_override is None:
            from django.db import connection
            with connection.cursor() as cur:
                cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname='tribunals_process'")
                row = cur.fetchone()
                count_override = int(row[0]) if row and row[0] else 0
    else:
        # Com filtro (tribunal e/ou enriq): deriva o total do cache
        # `estatisticas_por_tribunal` (warm 30min) somando o bucket relevante
        # por tribunal — NUNCA um COUNT(*). Crítico: `enriq=ok` sem tribunal
        # fazia COUNT(*) sobre ~11M (medido 59s, 2026-06-28) → estourava o
        # timeout do gunicorn e a /processos/?enriq=ok nunca carregava.
        stats = {
            s['tribunal'].sigla: s
            for s in queries.estatisticas_por_tribunal()
            if s.get('tribunal')
        }
        siglas = tribunais_filtro or list(stats.keys())
        total = 0
        completo = bool(stats)
        for sg in siglas:
            entry = stats.get(sg)
            if not entry or entry.get('pending'):
                completo = False
                break
            if enriq in ('ok', 'pendente', 'nao_encontrado', 'erro'):
                total += entry.get('enriquecimento', {}).get(enriq, 0)
            else:
                total += entry.get('processos') or 0
        if completo:
            count_override = total

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
    # contribuições por feature (peso × valor) pra explainability. Metadados
    # human-friendly vêm de `tribunals.explicacao.FEATURE_META` (fonte única).
    classif_explicacao = None
    if proc.classificacao_em:
        from tribunals.classificador import WEIGHTS, THRESHOLD_PRECATORIO, THRESHOLD_PRE_PRECATORIO, THRESHOLD_DIREITO_CREDITORIO
        from tribunals.explicacao import FEATURE_META, resumir_decisao
        from tribunals.models import ClassificacaoLog

        ultimo_log = ClassificacaoLog.objects.filter(processo=proc).order_by('-criada_em').first()
        feats = (ultimo_log.features_snapshot if ultimo_log else None) or {}
        if feats:
            contribs = []
            for fname, val in feats.items():
                w = WEIGHTS.get(fname, 0.0)
                contrib = w * val
                if abs(contrib) > 0.001:
                    meta = FEATURE_META.get(fname, {'emoji': 'mission-tag', 'label': fname, 'desc': ''})
                    contribs.append({
                        'feature': fname, 'peso': round(w, 3),
                        'valor': round(val, 3), 'contribuicao': round(contrib, 3),
                        'emoji': meta['emoji'], 'label': meta['label'], 'desc': meta['desc'],
                    })
            contribs.sort(key=lambda x: -abs(x['contribuicao']))

            thresholds = {
                'precatorio': THRESHOLD_PRECATORIO,
                'pre': THRESHOLD_PRE_PRECATORIO,
                'direito': THRESHOLD_DIREITO_CREDITORIO,
            }
            classif_explicacao = {
                'log': ultimo_log,
                'features': feats,
                'contribuicoes': contribs[:12],
                'resumo': resumir_decisao(
                    proc.classificacao or 'NAO_LEAD',
                    proc.classificacao_score or 0,
                    thresholds,
                ),
                'thresholds': thresholds,
            }

    return render(request, 'dashboard/processo_detail.html', {
        'processo': proc,
        'polos': polos,
        'classif_explicacao': classif_explicacao,
        # Botão "Atualizar dados públicos" aparece sse há enricher pro tribunal.
        # Fonte única = registry _ENRICHERS (mesmo guard de processo_enriquecer),
        # evita o or-chain hardcoded que desincronizava do registry.
        'pode_enriquecer': proc.tribunal_id in _ENRICHERS,
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
    from enrichers.jobs import _ENRICHERS, queue_for
    qnames = ('manual', *(queue_for(s) for s in _ENRICHERS), 'default')
    for qname in qnames:
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
    # Filtros multi-valor (CSV): tipo/tribunal/papel. Tribunal e papel filtram
    # via pontes denormalizadas (ParteTribunal/PartePapel) — EXISTS sobre a
    # tribunals_processoparte (bilhões) custaria ~43s; a ponte (índice por
    # tribunal/papel) é instantânea. Pontes populadas por `rebuild_parte_bridges`.
    tipos = _split_csv(request.GET.get('tipo'))
    tribunais_sel = _split_csv(request.GET.get('tribunal'))
    papeis_sel = _split_csv(request.GET.get('papel'))
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
        'tipos_filtro': tipos, 'tribunais_filtro': tribunais_sel, 'papeis_filtro': papeis_sel,
        'q': q, 'min_procs': min_procs, 'sort': sort,
        'distribuicao_tipos': distribuicao,
        # `total_partes` é a soma dos buckets por tipo (já consulta GROUP BY
        # tipo) — evita um count() global extra.
        'total_partes': sum(d.get('value', 0) for d in distribuicao),
        # Opções pros autocompletes multi-select.
        'tribunais_opcoes': queries.tribunais_opcoes(),
        'papeis_opcoes': queries.papeis_opcoes(),
        'tipos_opcoes': Parte.TIPO_CHOICES,
    }

    if not _is_htmx(request):
        return render(request, 'dashboard/partes.html', base_ctx)

    qs = Parte.objects.all().order_by(*order_by)
    has_filter = False
    if tipos:
        qs = qs.filter(tipo__in=tipos)
        has_filter = True
    if tribunais_sel:
        qs = qs.filter(Exists(ParteTribunal.objects.filter(
            parte_id=OuterRef('pk'), tribunal_id__in=tribunais_sel)))
        has_filter = True
    if papeis_sel:
        qs = qs.filter(Exists(PartePapel.objects.filter(
            parte_id=OuterRef('pk'), papel__in=papeis_sel)))
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


# Acima deste nº de participações, a página de detalhe da parte omite as
# agregações GROUP BY (donuts/counts/chips). Partes mega-agregadas (INSS,
# União, Fazenda, bancos) aparecem em milhões de processos; o GROUP BY faz
# full-scan de tribunals_processoparte (10-26s p/ o INSS, 3.4M linhas) e
# estoura o `--timeout 60` do gunicorn → worker morto (SystemExit) → 500.
PARTE_DETAIL_AGG_LIMIT = 50_000


@login_required
@require_GET
def parte_detail(request, pk):
    parte = get_object_or_404(Parte, pk=pk)

    base_qs = ProcessoParte.objects.filter(parte=parte)

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

    # Mega-partes: pula agregações e ordena por PK (instantâneo via índice).
    # ORDER BY processo__ultima_movimentacao_em faz join-sort de milhões de
    # linhas (~11s) — só vale pras partes de cardinalidade normal.
    agregacoes_omitidas = (parte.total_processos or 0) > PARTE_DETAIL_AGG_LIMIT

    if agregacoes_omitidas:
        qs = qs.order_by('-id')
        counts = {}
        papeis_disponiveis = []
        tribunais_da_parte = []
        chart_tribunal = chart_papel = chart_polo = []
        total_filtrado = None
    else:
        qs = qs.order_by('-processo__ultima_movimentacao_em')

        # Conta por polo (sempre, independente do filtro — pra exibir nos chips).
        # Normaliza polo='' → 'outros' pra bater com chart_polo (mesma chave nos
        # dois caminhos; senão template `counts.outros` ficava 0 enquanto donut
        # mostrava N).
        counts = {}
        for row in base_qs.values('polo').annotate(n=Count('id')):
            key = row['polo'] or 'outros'
            counts[key] = counts.get(key, 0) + row['n']

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
        total_filtrado = (
            qs.order_by().count() if (polo_filtro or tribunais_filtro or papel_filtro) else None
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
        'total_filtrado': total_filtrado,
        'chart_tribunal': chart_tribunal,
        'chart_papel': chart_papel,
        'chart_polo': chart_polo,
        'agregacoes_omitidas': agregacoes_omitidas,
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
def ingestao_saude(request):
    periodo_dias = _periodo_dias(request, default=30)
    tribunal_filtro = request.GET.get('tribunal', '')
    return render(request, 'dashboard/ingestao_saude.html', {
        'periodo_dias': periodo_dias,
        'tribunal_filtro': tribunal_filtro,
        'tribunais': Tribunal.objects.filter(ativo=True).order_by('sigla'),
        'kpis': queries.pipeline_kpis(
            tribunais=[tribunal_filtro] if tribunal_filtro else None),
    })


@login_required
@require_GET
def root(request):
    return redirect('dashboard:overview')


# ---------- Consulta rápida (debug Datajud/DJEN, sem persistir) ----------

@login_required
@require_GET
def algoritmo(request):
    """Página didática explicando o classificador (advogado-first).

    Renderiza:
      - As 19 features agrupadas em 5 famílias, com peso v6 e v7 (se disponível)
      - 4 exemplos curados (PROCESSO/CNJ por categoria)
      - Sandbox: input pra colar qualquer CNJ que já esteja no Voyager

    Lê `settings.ALGORITMO_EXEMPLOS_CNJS` (dict {N1, N2, N3, NL} -> CNJ).
    Se ausente, tenta top-1 por categoria no DB. Sem dependência hard.
    """
    from django.conf import settings as _settings

    from tribunals.classificador import (
        HARDCODED_WEIGHTS,
        METRICAS,
        THRESHOLD_DIREITO_CREDITORIO,
        THRESHOLD_PRE_PRECATORIO,
        THRESHOLD_PRECATORIO,
        _current_weights,
        get_versao_ativa,
    )
    from tribunals.explicacao import FEATURE_META, FAMILIAS, explicar_processo, features_por_familia
    from tribunals.models import ClassificadorVersao

    pesos_v6 = dict(_current_weights() or HARDCODED_WEIGHTS)
    pesos_v7: dict = {}
    v7_status = 'não treinada ainda'
    try:
        v7 = ClassificadorVersao.objects.filter(versao='v7').only('pesos', 'ativa', 'shadow', 'criada_em').first()
        if v7 and isinstance(v7.pesos, dict):
            pesos_v7 = v7.pesos
            if v7.ativa:
                v7_status = 'ativa em produção'
            elif v7.shadow:
                v7_status = 'em shadow (sendo comparada com v6)'
            else:
                v7_status = 'treinada, ainda não promovida'
    except Exception:
        pass

    # Constroi catálogo agrupado, com pesos v6+v7 anexados
    familias_view = []
    for key, icone, titulo, descricao, feature_names in features_por_familia():
        items = []
        for fname in feature_names:
            meta = FEATURE_META[fname]
            items.append({
                'feature': fname,
                'emoji': meta['emoji'],
                'label': meta['label'],
                'desc': meta['desc'],
                'criterio': meta['criterio'],
                'peso_v6': round(pesos_v6.get(fname, 0.0), 3) if fname in pesos_v6 else None,
                'peso_v7': round(pesos_v7.get(fname, 0.0), 3) if fname in pesos_v7 else None,
            })
        familias_view.append({
            'key': key, 'icone': icone, 'titulo': titulo, 'descricao': descricao, 'features': items,
        })

    # Exemplos curados — pode vir de settings ou ser auto-resolvido
    exemplos_cfg = getattr(_settings, 'ALGORITMO_EXEMPLOS_CNJS', None) or {}
    exemplos = []
    if exemplos_cfg:
        for rotulo, cnj in exemplos_cfg.items():
            proc = Process.objects.filter(numero_cnj=cnj).first()
            if proc:
                try:
                    exemplos.append({
                        'rotulo': rotulo,
                        'explicacao': explicar_processo(proc, top_n=8),
                    })
                except Exception:
                    continue
    else:
        # Fallback: top-1 por categoria
        for rotulo, icone, categoria in [
            ('Lead N1 típico (PRECATÓRIO)',                  'gem',       'PRECATORIO'),
            ('Pré-precatório (cumprimento sem expedição)',   'hourglass', 'PRE_PRECATORIO'),
            ('Direito creditório (sinal fraco)',             'sprout',    'DIREITO_CREDITORIO'),
            ('Não-lead clássico',                            'ban',       'NAO_LEAD'),
        ]:
            proc = (
                Process.objects.filter(classificacao=categoria)
                .order_by('-classificacao_score' if categoria != 'NAO_LEAD' else 'classificacao_score', '-id')
                .first()
            )
            if proc:
                try:
                    exemplos.append({
                        'rotulo': rotulo,
                        'icone': icone,
                        'explicacao': explicar_processo(proc, top_n=8),
                    })
                except Exception:
                    continue

    return render(request, 'dashboard/algoritmo.html', {
        'familias': familias_view,
        'familias_meta': FAMILIAS,
        'pesos_v6': pesos_v6,
        'pesos_v7': pesos_v7,
        'v7_status': v7_status,
        'versao_ativa': get_versao_ativa(),
        'metricas_v6': METRICAS,
        'thresholds': {
            'precatorio': THRESHOLD_PRECATORIO,
            'pre': THRESHOLD_PRE_PRECATORIO,
            'direito': THRESHOLD_DIREITO_CREDITORIO,
        },
        'exemplos': exemplos,
        'intercept_v6': round(pesos_v6.get('_intercept_', 0.0), 3),
    })


@login_required
@require_POST
def algoritmo_explicar(request):
    """Sandbox: recebe CNJ, devolve partial HTML com a explicação completa.

    Limitação: só processos já presentes no Voyager. Se não achar, retorna
    partial com mensagem amigável (200) — UX htmx-friendly, sem 404.
    """
    from tribunals.explicacao import explicar_processo

    cnj = (request.POST.get('cnj') or '').strip()
    cnj_normalizado = ''.join(ch for ch in cnj if ch.isdigit() or ch in '.-')

    if not cnj_normalizado:
        return render(request, 'dashboard/_partials/_algoritmo_explicacao.html', {
            'erro': 'Informe um CNJ.',
        })

    proc = Process.objects.filter(numero_cnj=cnj_normalizado).first()
    if proc is None:
        return render(request, 'dashboard/_partials/_algoritmo_explicacao.html', {
            'erro': f'Processo {cnj_normalizado} ainda não está no Voyager.',
            'erro_dica': 'O robô só consegue explicar processos que já foram ingeridos pelo DJEN/Datajud.',
        })

    try:
        explicacao = explicar_processo(proc, top_n=12)
    except Exception as e:  # noqa: BLE001
        return render(request, 'dashboard/_partials/_algoritmo_explicacao.html', {
            'erro': f'Erro inesperado: {e}',
        })

    return render(request, 'dashboard/_partials/_algoritmo_explicacao.html', {
        'explicacao': explicacao,
        'sandbox': True,
    })


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


# Keys de widget servidas por compute_leads_chart / pré-aquecidas pelo warm.
LEADS_CHART_KEYS = (
    'kpis', 'timeseries', 'calibration', 'funnel',
    'by-tribunal', 'distribuicao-score',
)


def leads_cache_key(key, tribunal, nivel, dias, cliente_nome):
    """Chave de cache canônica de um widget de leads. Usada pelo endpoint
    lazy e pelo warm job — TÊM que bater pra o warm popular o que a view lê.
    """
    return f'dashleads:{key}:t={tribunal or ""}:n={nivel or ""}:d={dias}:c={cliente_nome}'


def compute_leads_chart(key, tribunal, nivel, dias, cliente_nome):
    """Computa o payload JSON de um widget da dashboard de leads.

    Pura: sem request, sem cache, sem HttpResponse — chamável tanto pelo
    endpoint lazy quanto pelo warm job em background. `key` inválida levanta
    ValueError.

    Caveats:
    - ClassificacaoLog é gravado apenas em TRANSIÇÃO de categoria
      (classificador.classificar_e_persistir). "Descobertos N1" no
      timeseries = transições para PRECATORIO, não primeira ingestão.
    - LeadConsumption permite re-consumo (sem unique). Funil deduplica
      por processo_id + último resultado.
    """
    from datetime import timedelta
    from django.db.models import Count, Exists, OuterRef, Q
    from django.utils import timezone as djtz
    from tribunals.models import ApiClient, ClassificacaoLog, LeadConsumption

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
                    # lower() defensivo — ver nota no bucket do funil.
                    ultimo_resultado[pid] = (c['resultado'] or '').lower()

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
                # Normaliza casing: histórico tinha 'VALIDADO' (path legado,
                # já limpo) — lower() impede que um valor torto rache bucket.
                ultimo_por_proc[pid] = (c['resultado'] or '').lower()
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
        raise ValueError(f'key inválida: {key}')

    return data


@login_required
@require_GET
def leads_chart_data(request, key):
    """Endpoint lazy — JSON por widget, cache 5min por (key, filtros).

    Computação delegada a `compute_leads_chart` (compartilhada com o warm
    job `warm_leads_charts`, que pré-aquece o filtro default).
    """
    from django.core.cache import cache

    tribunal, nivel, dias, cliente_nome = _leads_filtros(request)
    cache_key = leads_cache_key(key, tribunal, nivel, dias, cliente_nome)
    cached = _safe_cache_get(cache_key)
    if cached is not None:
        return JsonResponse({'data': cached}, json_dumps_params={'default': str})

    try:
        data = compute_leads_chart(key, tribunal, nivel, dias, cliente_nome)
    except ValueError as e:
        return JsonResponse({'erro': str(e)}, status=400)

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


# ===========================================================================
# Validação humana de leads (T8)
# ===========================================================================
#
# Views adicionadas:
# - leads_visibilidade            shell de observabilidade (charts lazy)
# - chart_*                       5 endpoints JSON cacheados (5min) p/ charts
# - leads_validacao_overview      lista de lotes do usuário
# - leads_validacao_lote          fila do lote, redireciona pro item 1
# - leads_validacao_item          partial do card (HTMX swap)
# - leads_validacao_salvar        POST JSON, cria ProcessoValidacao
# - leads_validacao_criar_lote    POST form-data, chama sampling.criar_lote
#
# Templates referenciados (T12/T13 vão criar):
#   dashboard/leads/visibilidade.html
#   dashboard/leads/validacao_overview.html
#   dashboard/leads/validacao_lote.html
#   dashboard/leads/_partials/_validacao_card.html
#   dashboard/leads/_partials/_lote_concluido.html
#
# Segurança:
# - Todas as views exigem login + permission específica
# - leads_validacao_salvar valida que processo_id ∈ lote_id (proteção IDOR)
# - @require_POST + CSRF padrão Django em todas as mutações

import hashlib  # noqa: E402
import json  # noqa: E402

from django.contrib.auth.decorators import permission_required  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.views.decorators.csrf import csrf_protect  # noqa: E402

_CHART_VALIDACAO_TTL = 300  # 5 min
_MOTIVO_MAX_CHARS = 5000
_TEMPO_MAX_SEGUNDOS = 3600

_logger_validacao = __import__('logging').getLogger('voyager.dashboard.validacao')


def _validacao_filtros(request):
    """Lê filtros comuns de tribunal/classificacao/periodo."""
    tribunais = _split_csv(request.GET.get('tribunal'))
    classificacao = (request.GET.get('classificacao') or '').upper() or None
    try:
        dias = max(1, min(int(request.GET.get('dias', 90)), 365))
    except (TypeError, ValueError):
        dias = 90
    return tribunais, classificacao, dias


_CHART_VERSION_KEY = 'voyager:chart_version'


def _chart_validacao_cache_version() -> int:
    """Versão atual do namespace de cache dos charts. Increment-only.
    Permite invalidação O(1) sem depender de delete_pattern (django-redis only)."""
    ver = cache.get(_CHART_VERSION_KEY)
    if ver is None:
        cache.set(_CHART_VERSION_KEY, 0, None)
        return 0
    return int(ver)


def _bump_chart_version():
    """Invalida todos os caches de chart de validação atomicamente."""
    try:
        cache.incr(_CHART_VERSION_KEY)
    except ValueError:
        # key não existe ainda; cria.
        cache.set(_CHART_VERSION_KEY, 1, None)


def _chart_validacao_cache_key(nome: str, request) -> str:
    """Cache key versionada — incrementar version invalida tudo de uma vez."""
    raw = (
        request.GET.get('tribunal', '') + '|'
        + request.GET.get('classificacao', '') + '|'
        + str(request.GET.get('dias', ''))
    )
    h = hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]
    ver = _chart_validacao_cache_version()
    return f'voyager:chart:{nome}:{h}:v{ver}'


def _chart_validacao_endpoint(nome: str, builder):
    """Wrapper comum: cache + JSON + tratamento de erro."""
    def _resolve(request):
        key = _chart_validacao_cache_key(nome, request)
        cached = _safe_cache_get(key)
        if cached is not None:
            return JsonResponse({'data': cached}, json_dumps_params={'default': str})
        try:
            data = builder(request)
        except Exception as exc:
            _logger_validacao.exception(
                'chart_falhou', extra={'nome': nome, 'erro': str(exc)[:300]},
            )
            return JsonResponse(
                {'data': None, 'error': f'{nome}: {str(exc)[:120]}'},
                status=503,
            )
        try:
            cache.set(key, data, timeout=_CHART_VALIDACAO_TTL)
        except Exception:
            pass
        return JsonResponse({'data': data}, json_dumps_params={'default': str})
    return _resolve


# ---------- /dashboard/leads/visibilidade/ ----------

@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def leads_visibilidade(request):
    """Shell — instantâneo. Charts e listas carregam lazy."""
    kpis = queries.kpis_validacao(usuario=request.user)
    return render(request, 'dashboard/leads/visibilidade.html', {
        'tribunais': Tribunal.objects.filter(ativo=True),
        'tribunal_filtro': request.GET.get('tribunal', ''),
        'classificacao_filtro': request.GET.get('classificacao', ''),
        'periodo_dias': _periodo_dias(request, default=90),
        'kpis_validacao': kpis,
    })


@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def chart_histograma_score(request):
    return _chart_validacao_endpoint(
        'histograma_score',
        lambda r: queries.histograma_score_por_tribunal(
            tribunais=_split_csv(r.GET.get('tribunal')) or None,
            classificacao=(r.GET.get('classificacao') or '').upper() or None,
        ),
    )(request)


@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def chart_calibracao_por_tribunal(request):
    tribunais, _classif, dias = _validacao_filtros(request)
    return _chart_validacao_endpoint(
        'calibracao',
        lambda r: queries.calibracao_por_tribunal(
            tribunais=tribunais or None, periodo_dias=dias,
        ),
    )(request)


@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def chart_heatmap_tribunal_ano(request):
    return _chart_validacao_endpoint(
        'heatmap_tribunal_ano',
        lambda r: queries.heatmap_tribunal_ano(),
    )(request)


@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def chart_funil_ampliado(request):
    _t, _c, dias = _validacao_filtros(request)
    return _chart_validacao_endpoint(
        'funil_ampliado',
        lambda r: queries.funil_ampliado(periodo_dias=dias),
    )(request)


@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def chart_top_fn_semana(request):
    return _chart_validacao_endpoint(
        'top_fn_semana',
        lambda r: queries.top_fn_semana(limit=10),
    )(request)


@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def chart_shadow_status(request):
    """Status do shadow mode (T19). Sem cache — estado leve e mutável."""
    try:
        data = queries.shadow_status()
    except Exception as exc:
        _logger_validacao.exception(
            'chart_falhou', extra={'nome': 'shadow_status', 'erro': str(exc)[:300]},
        )
        return JsonResponse(
            {'data': None, 'error': f'shadow_status: {str(exc)[:120]}'}, status=503,
        )
    return JsonResponse({'data': data}, json_dumps_params={'default': str})


# ---------- /dashboard/leads/validacao/ ----------

@login_required
@require_GET
@permission_required('tribunals.can_view_validacao_dashboard', raise_exception=True)
def leads_validacao_overview(request):
    """Lista de lotes ativos + KPIs do usuário."""
    from tribunals.models import AmostraValidacao

    kpis = queries.kpis_validacao(usuario=request.user)
    lotes = list(
        AmostraValidacao.objects.select_related('tribunal')
        .order_by('-criada_em')[:50]
        .values('id', 'estrategia', 'tribunal_id', 'tamanho_alvo', 'criada_em',
                'versao_modelo')
    )
    return render(request, 'dashboard/leads/validacao_overview.html', {
        'kpis_validacao': kpis,
        'lotes': lotes,
        'estrategias': [
            ('top_score', 'Top score (≥ 0.7)'),
            ('borderline', 'Borderline (0.30-0.70)'),
            ('low_score', 'Low score (0.05-0.30)'),
            ('falsos_consumidos', 'Falsos consumidos'),
            ('recuperados', 'Recuperados'),
            ('on_demand', 'Sob demanda'),
            ('fn_candidatos', 'Candidatos a falso negativo'),
        ],
        'tribunais': Tribunal.objects.filter(ativo=True),
    })


@login_required
@require_GET
@permission_required('tribunals.can_validate_lead', raise_exception=True)
def leads_validacao_lote(request, lote_id: int):
    """Render fila + carrega item 1 via HTMX."""
    from tribunals.models import AmostraProcesso, AmostraValidacao, ProcessoValidacao

    lote = get_object_or_404(AmostraValidacao, pk=lote_id)
    total = AmostraProcesso.objects.filter(amostra=lote).count()
    anotados = ProcessoValidacao.objects.filter(
        amostra=lote, usuario=request.user,
    ).count()
    return render(request, 'dashboard/leads/validacao_lote.html', {
        'lote': lote,
        'total': total,
        'anotados': anotados,
        'progresso_pct': round(100 * anotados / total, 1) if total else 0,
    })


@login_required
@require_GET
@permission_required('tribunals.can_validate_lead', raise_exception=True)
def leads_validacao_item(request, lote_id: int, posicao: int):
    """Partial _validacao_card.html. `posicao` é 1-based.

    Pula processos já anotados pelo usuário até encontrar não anotado.
    Se passar do total, redirect pra concluido.
    """
    from tribunals.models import (
        AmostraProcesso, AmostraValidacao, Movimentacao,
        ProcessoParte, ProcessoValidacao,
    )

    lote = get_object_or_404(AmostraValidacao, pk=lote_id)
    itens = list(
        AmostraProcesso.objects.filter(amostra=lote)
        .select_related('processo', 'processo__tribunal')
        .order_by('ordem')
    )
    total = len(itens)
    if total == 0 or posicao > total:
        return redirect('dashboard:leads_validacao_lote_concluido', lote_id=lote_id)
    posicao = max(posicao, 1)

    # Pula anotados pelo usuário corrente — avança até não anotado ou fim.
    anotados_ids = set(
        ProcessoValidacao.objects.filter(amostra=lote, usuario=request.user)
        .values_list('processo_id', flat=True)
    )
    while posicao <= total and itens[posicao - 1].processo_id in anotados_ids:
        posicao += 1
    if posicao > total:
        return redirect('dashboard:leads_validacao_lote_concluido', lote_id=lote_id)

    item = itens[posicao - 1]
    processo = item.processo

    ultimas_movs = list(
        Movimentacao.objects.filter(processo=processo)
        .order_by('-data_disponibilizacao', '-id')[:5]
    )
    partes = list(
        ProcessoParte.objects.filter(processo=processo)
        .select_related('parte').order_by('polo', 'papel')[:20]
    )

    suspeita = None
    if item.suspeita_score is not None:
        suspeita = {
            'score': round(item.suspeita_score, 3),
            'motivos': item.motivos_suspeita or [],
        }

    score_breakdown = queries.compute_score_breakdown(processo, top_n=5)

    return render(
        request,
        'dashboard/leads/_partials/_validacao_card.html',
        {
            'lote': lote,
            'lote_id': lote_id,
            'posicao': posicao,
            'total': total,
            'processo': processo,
            'item': item,
            'suspeita': suspeita,
            'score_breakdown': score_breakdown,
            'ultimas_movs': ultimas_movs,
            'partes': partes,
        },
    )


@login_required
@require_GET
@permission_required('tribunals.can_validate_lead', raise_exception=True)
def leads_validacao_lote_concluido(request, lote_id: int):
    """Tela de "mission complete" pós-fim do lote."""
    from tribunals.models import AmostraProcesso, AmostraValidacao, ProcessoValidacao

    lote = get_object_or_404(AmostraValidacao, pk=lote_id)
    total = AmostraProcesso.objects.filter(amostra=lote).count()
    minhas = ProcessoValidacao.objects.filter(amostra=lote, usuario=request.user)
    por_resultado = dict(
        minhas.values_list('resultado').annotate(n=Count('id'))
    )
    return render(request, 'dashboard/leads/_partials/_lote_concluido.html', {
        'lote': lote,
        'total': total,
        'minhas_total': minhas.count(),
        'por_resultado': por_resultado,
    })


@login_required
@require_POST
@csrf_protect
@permission_required('tribunals.can_validate_lead', raise_exception=True)
def leads_validacao_salvar(request):  # noqa: PLR0911, PLR0912
    """POST JSON {processo_id, lote_id, resultado, confianca, motivo, tempo_segundos}.

    Validações:
    - processo_id ∈ lote_id (anti-IDOR via AmostraProcesso)
    - resultado em ProcessoValidacao.RESULTADO_CHOICES
    - confianca em CONFIANCA_CHOICES (default 'media' se omitida)
    - tempo_segundos > 0 e < 3600 se informado
    - motivo truncado a 5000 chars
    - UniqueConstraint(processo, usuario): segundo save = 409
    """
    from django.db import IntegrityError

    from tribunals.models import AmostraProcesso, ProcessoValidacao

    # Aceita JSON ou form-data.
    if request.content_type and 'application/json' in request.content_type:
        try:
            payload = json.loads(request.body or b'{}')
        except json.JSONDecodeError:
            return JsonResponse({'error': 'json inválido'}, status=400)
    else:
        payload = request.POST.dict()

    try:
        processo_id = int(payload.get('processo_id') or 0)
        lote_id = int(payload.get('lote_id') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'processo_id/lote_id inválidos'}, status=400)
    if not processo_id or not lote_id:
        return JsonResponse({'error': 'processo_id e lote_id obrigatórios'}, status=400)

    resultado = (payload.get('resultado') or '').strip()
    valid_resultados = {c[0] for c in ProcessoValidacao.RESULTADO_CHOICES}
    if resultado not in valid_resultados:
        return JsonResponse(
            {'error': f'resultado inválido (opções: {sorted(valid_resultados)})'},
            status=400,
        )

    confianca = (payload.get('confianca') or ProcessoValidacao.CONFIANCA_MEDIA).strip()
    valid_confiancas = {c[0] for c in ProcessoValidacao.CONFIANCA_CHOICES}
    if confianca not in valid_confiancas:
        return JsonResponse({'error': 'confianca inválida'}, status=400)

    tempo_segundos = payload.get('tempo_segundos')
    if tempo_segundos is not None and tempo_segundos != '':
        try:
            tempo_segundos = int(tempo_segundos)
        except (TypeError, ValueError):
            return JsonResponse({'error': 'tempo_segundos inválido'}, status=400)
        if tempo_segundos <= 0 or tempo_segundos >= _TEMPO_MAX_SEGUNDOS:
            return JsonResponse({'error': 'tempo_segundos fora de faixa'}, status=400)
    else:
        tempo_segundos = None

    motivo = (payload.get('motivo') or '')[:_MOTIVO_MAX_CHARS]

    # Anti-IDOR: processo_id deve pertencer ao lote_id.
    item = AmostraProcesso.objects.filter(
        amostra_id=lote_id, processo_id=processo_id,
    ).select_related('amostra').first()
    if item is None:
        _logger_validacao.warning(
            'idor_attempt',
            extra={
                'action': 'validacao_salvar',
                'usuario_id': request.user.pk,
                'lote_id': lote_id,
                'processo_id': processo_id,
            },
        )
        return JsonResponse(
            {'error': 'processo não pertence ao lote'}, status=403,
        )

    # Snapshot do score/classificação vem do MOMENTO DO SORTEIO (auditoria).
    score_snap = float(item.score_no_sorteio or 0.0)
    classif_snap = item.classificacao_no_sorteio or ''
    versao = item.amostra.versao_modelo

    # Features snapshot — best-effort.
    features_snap = {}
    try:
        from tribunals.classificador import compute_features
        feats = compute_features(item.processo)
        features_snap = {k: round(float(v), 4) for k, v in feats.items()}
    except Exception as exc:
        _logger_validacao.warning(
            'features_snapshot_falhou',
            extra={'processo_id': processo_id, 'erro': str(exc)[:120]},
        )
        features_snap = {}

    # LGPD anonimização: descomissionada — campo `usuario_hash` permanece
    # no schema mas não é populado. Reativar futuramente via setting.
    try:
        ProcessoValidacao.objects.create(
            processo_id=processo_id,
            amostra_id=lote_id,
            usuario=request.user,
            resultado=resultado,
            confianca=confianca,
            motivo=motivo,
            tempo_segundos=tempo_segundos,
            versao_modelo=versao,
            classificacao_no_momento=classif_snap,
            score_no_momento=score_snap,
            features_snapshot=features_snap,
        )
    except IntegrityError:
        return JsonResponse(
            {'error': 'você já anotou esse processo (re-anotação proibida)'},
            status=409,
        )

    # Invalida charts via versionamento (1 INCR — funciona em qualquer
    # backend Redis, sem depender de delete_pattern do django-redis).
    _bump_chart_version()

    # Métricas pós-save pro toast/UI.
    minhas = ProcessoValidacao.objects.filter(amostra_id=lote_id, usuario=request.user)
    total_anotados = minhas.count()
    tempos = list(
        minhas.exclude(tempo_segundos__isnull=True).values_list('tempo_segundos', flat=True)
    )
    tempo_medio = round(sum(tempos) / len(tempos), 1) if tempos else None
    total_lote = (
        AmostraProcesso.objects.filter(amostra_id=lote_id).count()
    )
    progresso_pct = round(100 * total_anotados / total_lote, 1) if total_lote else 0

    # Próxima URL: aponta pro próximo item.
    next_url = reverse(
        'dashboard:leads_validacao_item',
        kwargs={'lote_id': lote_id, 'posicao': item.ordem + 1},
    )

    _logger_validacao.info(
        'validacao_salva',
        extra={
            'action': 'validacao_salva',
            'lote_id': lote_id,
            'usuario_id': request.user.pk,
            'processo_id': processo_id,
            'resultado': resultado,
        },
    )

    return JsonResponse({
        'ok': True,
        'next_url': next_url,
        'total_anotados': total_anotados,
        'tempo_medio': tempo_medio,
        'progresso_pct': progresso_pct,
    })


_ESTRATEGIAS_PERMITIDAS = {
    'top_score', 'borderline', 'low_score',
    'falsos_consumidos', 'recuperados', 'on_demand',
    'fn_candidatos',
}


@login_required
@require_POST
@csrf_protect
@permission_required('tribunals.can_validate_lead', raise_exception=True)
def leads_validacao_criar_lote(request):
    """POST form-data {estrategia, tribunal_sigla, tamanho, parametros_json}.

    Despacha pra sampling.<estrategia>() + sampling.criar_lote(...).
    """
    from tribunals import sampling
    from tribunals.models import AmostraValidacao, Tribunal as _T

    estrategia = (request.POST.get('estrategia') or '').strip()
    if estrategia not in _ESTRATEGIAS_PERMITIDAS:
        return JsonResponse(
            {'error': f'estrategia inválida (opções: {sorted(_ESTRATEGIAS_PERMITIDAS)})'},
            status=400,
        )
    tribunal_sigla = (request.POST.get('tribunal_sigla') or '').strip().upper() or None
    try:
        tamanho = int(request.POST.get('tamanho') or 300)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'tamanho inválido'}, status=400)
    if tamanho <= 0 or tamanho > 5000:
        return JsonResponse({'error': 'tamanho fora de faixa (1-5000)'}, status=400)

    tribunal_obj = None
    if tribunal_sigla:
        try:
            tribunal_obj = _T.objects.get(pk=tribunal_sigla)
        except _T.DoesNotExist:
            return JsonResponse({'error': f'tribunal {tribunal_sigla} não existe'}, status=400)

    parametros = {}
    raw_params = request.POST.get('parametros_json', '')
    if raw_params:
        try:
            parametros = json.loads(raw_params)
            if not isinstance(parametros, dict):
                return JsonResponse({'error': 'parametros_json deve ser objeto'}, status=400)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'parametros_json inválido'}, status=400)

    # Despacho de estratégia.
    try:
        if estrategia == 'borderline':
            faixa = parametros.get('faixa') or [0.30, 0.70]
            qs = sampling.sample_borderline(
                faixa=(float(faixa[0]), float(faixa[1])),
                tribunal=tribunal_obj, limit=tamanho, usuario=request.user,
            )
        elif estrategia == 'fn_candidatos':
            qs = sampling.sample_fn_candidatos(
                tribunal=tribunal_obj, limit=tamanho,
                csv_path=parametros.get('csv_path'),
                usuario=request.user,
            )
        elif estrategia == 'top_score':
            qs = sampling.sample_n1_alto(
                min_score=float(parametros.get('min_score', 0.85)),
                tribunal=tribunal_obj, limit=tamanho, usuario=request.user,
            )
        elif estrategia == 'low_score':
            qs = sampling.sample_nao_lead_top(
                min_score=float(parametros.get('min_score', 0.05)),
                tribunal=tribunal_obj, limit=tamanho, usuario=request.user,
            )
        elif estrategia == 'falsos_consumidos':
            qs = sampling.sample_falsos_consumidos(
                tribunal=tribunal_obj, limit=tamanho,
                csv_path=parametros.get('csv_path',
                                        'leads_trf1_falsos_consumidos_1327.csv'),
                usuario=request.user,
            )
        elif estrategia == 'recuperados':
            qs = sampling.sample_recuperados(
                tribunal=tribunal_obj, limit=tamanho,
                csv_path=parametros.get('csv_path',
                                        'leads_trf1_recuperados_1327.csv'),
                usuario=request.user,
            )
        elif estrategia == 'on_demand':
            if tribunal_obj is None:
                return JsonResponse(
                    {'error': 'on_demand exige tribunal_sigla'}, status=400,
                )
            qs = sampling.sample_random_tribunal(
                tribunal=tribunal_obj, limit=tamanho, usuario=request.user,
            )
        else:
            return JsonResponse({'error': 'estrategia não implementada'}, status=400)

        lote = sampling.criar_lote(
            estrategia=estrategia,
            queryset=qs,
            criada_por=request.user,
            tribunal=tribunal_obj,
            tamanho_alvo=tamanho,
            parametros=parametros,
        )
    except FileNotFoundError as exc:
        return JsonResponse({'error': f'CSV não encontrado: {exc}'}, status=400)
    except Exception as exc:
        _logger_validacao.exception(
            'criar_lote_falhou',
            extra={'estrategia': estrategia, 'erro': str(exc)[:300]},
        )
        return JsonResponse({'error': f'falha ao criar lote: {str(exc)[:200]}'}, status=500)

    _ = AmostraValidacao  # silenced lint
    return JsonResponse({
        'ok': True,
        'lote_id': lote.pk,
        'tamanho_real': lote.itens.count(),
        'redirect_url': reverse('dashboard:leads_validacao_lote', kwargs={'lote_id': lote.pk}),
    })


# ---------------------------------------------------------------------------
# Acervo — busca semântica (Zordon)
# ---------------------------------------------------------------------------

@login_required
@require_GET
def acervo_busca(request):
    """Página de busca semântica no acervo de autos via Zordon.

    GET sem HX-Request → shell completo (caixa de busca + estado vazio).
    GET com HX-Request → apenas o partial de resultados (swap HTMX).
    """
    from .zordon_client import buscar as zordon_buscar

    q = request.GET.get('q', '').strip()

    if not _is_htmx(request):
        return render(request, 'dashboard/acervo_busca.html', {'q': q})

    # HTMX: retorna apenas o partial de resultados
    if not q:
        return render(request, 'dashboard/_partials/_acervo_resultados.html', {
            'q': '',
            'resultados': None,
            'erro': None,
        })

    # Pede mais que o teto de exibição: o Zordon indexa por documento
    # (petição/movimentação/...), então vários hits caem no mesmo processo —
    # deduplicamos por CNJ pra listar PROCESSOS, não documentos.
    dados = zordon_buscar(q, limit=20)
    erro = dados.get('erro')

    melhor_por_cnj = {}
    for it in (dados.get('results') or []):
        cnj = (it.get('numero_cnj') or '').strip()
        if not cnj:
            continue
        atual = melhor_por_cnj.get(cnj)
        if atual is None or (it.get('score') or 0) > (atual.get('score') or 0):
            melhor_por_cnj[cnj] = it

    # Resolve CNJ -> Process do nosso acervo (só processos que existem aqui, pra
    # o clique sempre abrir a página do processo). Guard de statement_timeout:
    # nunca travar o worker (proteção extra além do índice proc_numero_cnj_idx).
    from django.db import transaction, connection
    pk_por_cnj = {}
    if melhor_por_cnj:
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '5000'")
                pk_por_cnj = dict(
                    Process.objects.filter(numero_cnj__in=list(melhor_por_cnj))
                    .values_list('numero_cnj', 'pk')
                )
        except Exception:
            import logging
            logging.getLogger('voyager.dashboard').warning(
                'acervo_busca: lookup CNJ->pk degradado (timeout/erro)')
            pk_por_cnj = {}

    resultados = []
    for cnj, it in sorted(melhor_por_cnj.items(),
                          key=lambda kv: -(kv[1].get('score') or 0)):
        pk = pk_por_cnj.get(cnj)
        if not pk:
            continue  # só processos do acervo
        resultados.append({
            'numero_cnj': cnj,
            'process_id': pk,
            'score': it.get('score'),
            'snippet': it.get('snippet'),
        })
        if len(resultados) >= 15:
            break

    return render(request, 'dashboard/_partials/_acervo_resultados.html', {
        'q': q,
        'resultados': resultados,
        'erro': erro,
    })


@login_required
@require_GET
def acervo_teor(request, cnj):
    """Partial HTMX com campos extraídos pelo Zordon para um processo específico.

    Carregado lazily no detalhe do processo (hx-trigger="revealed").
    Chama zordon_client.extrair(cnj) para obter os campos estruturados e
    zordon_client.chunks(cnj) para os fragmentos do auto (opcional).

    Casos de resposta:
    - Extração OK → tabela de campos + (se disponível) chunks
    - erro="sem_contexto" → aviso "processo não indexado no Zordon"
    - Qualquer outro erro → mensagem amigável (Zordon offline / falha de rede)
    """
    from django.core.cache import cache
    from . import zordon_client
    from .zordon_client import extrair as zordon_extrair, chunks as zordon_chunks

    # Extração é cara (RAG + LLM 20b, ~dezenas de s): cacheia por processo (autos
    # imutáveis → TTL longo; o warm reescreve). Só estados estáveis; erro re-tenta.
    ck = zordon_client.extract_cache_key(cnj)
    dados_extracao = cache.get(ck)
    if dados_extracao is None:
        dados_extracao = zordon_extrair(cnj)
        if dados_extracao.get('erro') in (None, 'sem_contexto'):
            cache.set(ck, dados_extracao, zordon_client.EXTRACT_CACHE_TTL)
    sem_contexto = dados_extracao.get('erro') == 'sem_contexto'
    erro_generico = dados_extracao.get('erro') if not sem_contexto else None

    dados_chunks = None
    if not dados_extracao.get('erro'):
        dados_chunks = zordon_chunks(cnj)

    # Showcase: junta o extract do Zordon (natureza/valores/pagamento dos autos)
    # com o enrichment do nosso acervo (juízo, partes, advogados, valor da causa,
    # situação) — cards ricos mesmo quando o Zordon ainda não indexou.
    proc = (Process.objects.select_related('tribunal')
            .filter(numero_cnj=cnj).order_by('-enriquecido_em').first())
    polos = {'ativo': [], 'passivo': [], 'outros': []}
    advogados = []
    if proc:
        for pp in (ProcessoParte.objects.filter(processo=proc)
                   .select_related('parte', 'representa__parte')
                   .order_by('polo', 'papel')):
            eh_adv = (pp.representa_id is not None
                      or (pp.parte.oab or '')
                      or (pp.parte.tipo == 'advogado'))
            if eh_adv:
                advogados.append(pp)
            else:
                polos.setdefault(pp.polo, []).append(pp)

    return render(request, 'dashboard/_partials/_acervo_teor.html', {
        'cnj': cnj,
        'extracao': dados_extracao if not dados_extracao.get('erro') else None,
        'sem_contexto': sem_contexto,
        'erro': erro_generico,
        'chunks': dados_chunks.get('chunks', []) if dados_chunks else [],
        'processo': proc,
        'polo_ativo': polos['ativo'],
        'polo_passivo': polos['passivo'],
        'polo_outros': polos['outros'],
        'advogados': advogados,
    })


def processo_metadados(request, pk):
    """Partial HTMX: metadados EXTRAÍDOS dos autos pelo Zordon.

    Lista de documentos (por classe, sem conteúdo), partes classificadas por papel,
    eventos do ciclo de vida, e campos com proveniência/abstenção. Lazy-load
    (hx-trigger="revealed"). Degrada: sem acervo / extração pendente / Zordon offline.
    """
    from . import zordon_client

    processo = get_object_or_404(Process, pk=pk)
    dados = zordon_client.metadados(processo.numero_cnj)
    erro = dados.get('erro')

    # Partes agrupadas por papel (ordem de relevância no precatório).
    _ORDEM_PAPEL = ['EXEQUENTE', 'BENEFICIARIO', 'HERDEIRO', 'ESPOLIO', 'INVENTARIANTE',
                    'CESSIONARIO', 'REPRESENTANTE', 'ADVOGADO', 'EXECUTADO', 'TERCEIRO',
                    'DESCONHECIDO']
    grupos = {}
    for p in (dados.get('partes') or []):
        grupos.setdefault(p.get('papel') or 'DESCONHECIDO', []).append(p)
    partes_por_papel = [(papel, grupos[papel]) for papel in _ORDEM_PAPEL if papel in grupos]

    # Documentos agrupados por classe (ordena classes por contagem desc).
    docs = dados.get('documentos') or []
    docs_por_classe = {}
    for d in docs:
        docs_por_classe.setdefault(d.get('doc_classe') or '?', []).append(d)
    docs_por_classe = sorted(docs_por_classe.items(), key=lambda kv: -len(kv[1]))

    # Campos-chave em ordem de leitura, com seu MetaField (valor + proveniência).
    _CAMPOS = [('Natureza', 'natureza'), ('Valor total', 'valor_total'),
               ('Ente devedor', 'ente_devedor'), ('Beneficiário', 'beneficiario'),
               ('Nº precatório', 'numero_precatorio'), ('Exercício', 'exercicio'),
               ('Data do ofício', 'data_oficio'), ('Estágio', 'estagio_sinal')]
    campos = dados.get('campos') or {}
    campos_exib = [(label, campos[k]) for label, k in _CAMPOS if k in campos]

    return render(request, 'dashboard/_partials/_processo_metadados.html', {
        'processo': processo,
        'dados': dados if not erro else None,
        'erro': erro,
        'partes_por_papel': partes_por_papel,
        'docs_por_classe': docs_por_classe,
        'campos_exib': campos_exib,
    })
