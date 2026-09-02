"""Endpoints REST do Mapa Comercial de Precatórios.

Views JSON que servem o mapa (choropleth + drill-down + ranking) a partir do
serviço de agregação `search.agg_overview` (100% Elasticsearch). Todos os
endpoints exigem sessão logada — é dado sensível de negócio.

O CONTRATO JSON completo (request/response) está documentado no topo de
`search/agg_overview.py`. Estas views só fazem: parse dos filtros → chamada do
serviço → `JsonResponse`. Erros de infra (ES fora) viram 503 (nunca 500 cru);
`uf` ausente no drill-down vira 400.
"""
import hashlib
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from search import (
    agg_overview,
    agg_entidade as agg_entidade_svc,
    agg_estado as agg_estado_svc,
    entidades as entidades_svc,
)
from search.client import get_es, index_name

logger = logging.getLogger('voyager.comercial.views')


def _json(payload, status=200):
    # default=str garante serialização de qualquer resíduo (datas etc.)
    return JsonResponse(payload, status=status, json_dumps_params={'default': str})


@never_cache
@login_required
@require_GET
def comercial_mapa_page(request):
    """GET /dashboard/overview/mapa/ — página HTML do Mapa Comercial.

    Só renderiza o shell; TODOS os dados vêm dos 3 endpoints JSON
    (`overview-mapa`/`overview-tribunais`/`overview-top`) via fetch no browser.
    Nenhuma query pesada aqui — o Postgres não é tocado.

    `@never_cache` (padrão já usado em dashboard/views.py): sem ele o browser
    servia o HTML do disco e o usuário continuava vendo a versão ANTIGA depois do
    deploy — aconteceu de verdade (12/08/2026: o print reclamando de layout
    mostrava textos já removidos há 3 deploys). A página é toda dinâmica e o
    payload é pequeno; cachear HTML aqui não economiza nada e esconde correção.
    """
    return render(request, 'dashboard/overview_mapa.html')


@never_cache
@login_required
@require_GET
def comercial_estado_page(request, uf):
    """GET /dashboard/overview/estado/<uf>/ — página dedicada de UM estado.

    Mesmo padrão do `comercial_mapa_page`: só renderiza o shell, ZERO query
    (nem Postgres nem ES). Todos os números vêm do endpoint JSON
    `comercial-estado` via fetch no browser, com os filtros que já vieram na
    querystring do mapa (a página é aberta em aba nova pelo drill-down).

    A UF NÃO é validada aqui de propósito: quem valida é o serviço
    (`agg_estado.normalizar_uf` → 400 no endpoint), e a página trata esse 400
    com uma tela humana ("não conhecemos este estado") em vez de um 404 seco no
    meio de uma navegação que veio de um clique no mapa. Só normalizamos a
    caixa e resolvemos o nome por extenso pro `<title>`/cabeçalho — dicionário
    em memória, sem I/O.
    """
    sigla = (uf or '').strip().upper()
    return render(request, 'dashboard/overview_estado.html', {
        'uf': sigla,
        'uf_nome': agg_estado_svc.UF_NOME.get(sigla, ''),
    })


@login_required
@require_GET
def comercial_mapa(request):
    """GET /dashboard/api/overview/mapa — agregado por UF + total + score de foco."""
    filtros = agg_overview.parse_filtros(request.GET)
    try:
        payload = agg_overview.agg_por_uf(filtros)
    except Exception:
        logger.exception('comercial_mapa: falha na agregação ES', extra={'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


@login_required
@require_GET
def comercial_tribunais(request):
    """GET /dashboard/api/overview/tribunais?uf=SP — drill-down por tribunal."""
    filtros = agg_overview.parse_filtros(request.GET)
    uf = filtros.get('uf')
    if not uf:
        return _json({'erro': 'parâmetro uf é obrigatório'}, status=400)
    # `uf` já está nos filtros; agg_tribunais reforça — remove p/ não duplicar semântica
    filtros_sem_uf = {k: v for k, v in filtros.items() if k != 'uf'}
    try:
        payload = agg_overview.agg_tribunais(uf, filtros_sem_uf)
    except Exception:
        logger.exception('comercial_tribunais: falha na agregação ES',
                         extra={'uf': uf, 'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


@login_required
@require_GET
def comercial_estado(request, uf):
    """GET /dashboard/api/overview/estado/<uf>/ — página dedicada de um estado.

    Mesmos filtros do mapa (`agg_overview.parse_filtros`) + `metrica`
    (possiveis|confirmados|todos). O `uf` da querystring é ignorado — manda o da
    rota. Contrato JSON completo no topo de `search/agg_estado.py`.
    UF inválida → 400. Timeout do ES → **`pending`**, não 503 (ver abaixo).
    ES genuinamente fora → 503 (nunca 500 cru).

    ⚠️ **Timeout não é o mesmo que "fora do ar", e tratar os dois como 503 é o
    que deixou a tela morta.** Medido em 02/09/2026: a agregação custa ~13-26 s
    a frio (BA 26,5 s, SP 14,5 s) contra um teto de cliente de 30 s. Basta o
    disco do nó estar disputado — e ele está, com backfills — para o
    `_msearch` estourar. O usuário via **página quebrada** num caso em que o
    dado existe e só demorou.

    Pior: o warm cobre 28 UFs **só na métrica `todos`** (aquecer as três
    triplicaria a passada para ~16 min de ES/hora, medido). Então `?metrica=
    possiveis` — que é uma lente do próprio seletor da tela — caía sempre
    aqui, e virava 503 toda vez.

    `pending` faz a tela segurar o skeleton e repetir, como o `lazyChart` já
    faz nos gráficos de leads (#64). Na segunda tentativa o cache costuma estar
    quente. Devolver 200 com `pending` é honesto: não é dado errado nem
    ausência de dado — é "ainda não".
    """
    filtros = agg_overview.parse_filtros(request.GET)
    metrica = request.GET.get('metrica', agg_estado_svc.METRICA_DEFAULT)
    try:
        payload = agg_estado_svc.agg_estado(uf, filtros, metrica)
    except agg_estado_svc.UfInvalida:
        return _json({'erro': f'UF inválida: {uf}'}, status=400)
    except Exception as e:
        demorou = 'timeout' in type(e).__name__.lower() or 'timed out' in str(e).lower()
        logger.exception(
            'comercial_estado: falha na agregação ES (%s)',
            'TIMEOUT — devolvendo pending' if demorou else 'erro — devolvendo 503',
            extra={'uf': uf, 'metrica': metrica, 'filtros': filtros},
        )
        if demorou:
            return _json({'pending': True, 'uf': uf, 'metrica': metrica,
                          'motivo': 'a agregação passou do teto de espera; '
                                    'o aquecimento vai popular o cache'})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


@login_required
@require_GET
def comercial_top(request):
    """GET /dashboard/api/overview/top?metric=score&n=10 — ranking Top-N por UF."""
    filtros = agg_overview.parse_filtros(request.GET)
    metric = request.GET.get('metric', 'score')
    n = request.GET.get('n', 10)
    try:
        payload = agg_overview.ranking_top(metric, n, filtros)
    except Exception:
        logger.exception('comercial_top: falha na agregação ES',
                         extra={'metric': metric, 'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


# --------------------------------------------------------------------------- #
# AUTOCOMPLETE DE ENTIDADE CANÔNICA
# --------------------------------------------------------------------------- #
# Substitui o texto livre do filtro "Parte / entidade": em vez de o usuário
# adivinhar a grafia que o tribunal usou, ele escolhe UMA entidade do índice
# canônico (`search/entidades.py`), que já fundiu as grafias por raiz de CNPJ /
# nome normalizado. Esta view só CONSOME `entidades.query_autocomplete` — a
# construção do índice e o ranking vivem em `search/`.
#
# Contrato:
#   GET /dashboard/api/entidades/autocomplete?q=inss&n=8&ente=1
#   200 {"termo", "min_caracteres", "buscou": bool, "motivo": str|None,
#        "indice": str, "contagem_processos_disponivel": bool,
#        "itens": [{"entidade_id", "nome_canonico", "chave", "n_variantes",
#                   "grafias_exemplo": [...], "raiz_cnpj", "documentos": [...],
#                   "n_documentos", "eh_ente_publico", "n_partes",
#                   "n_processos": int|null}]}
#   503 {"erro": ...}  — ES fora (nunca 500 cru)
#
# `n_processos` é `null` quando o índice ainda NÃO tem a contagem. `null` =
# DESCONHECIDO, e a UI escreve "não contamos ainda" — nunca "0". Zero é uma
# afirmação ("esta entidade não tem processo"); ausência não é.

#: mínimo de caracteres pra tocar o ES. Abaixo disso devolvemos lista vazia sem
#: requisição nenhuma: 1-2 letras casam com centenas de milhares das 1,14M
#: entidades e ninguém escolhe nada nessa lista — é custo sem produto.
ENTIDADES_MIN_CARACTERES = 3
ENTIDADES_N_DEFAULT = 8
#: teto de itens. O dropdown é pra ESCOLHER, não pra navegar catálogo; e o
#: `_source` de cada entidade carrega listas (grafias, CNPJs).
ENTIDADES_N_MAX = 20
#: TTL do cache por termo. Curto de propósito: o índice é reconstruído com
#: frequência nesta fase e um TTL longo mascararia o rebuild (o mesmo erro que
#: o cache de 2min do dashboard já causou — ver OPS).
ENTIDADES_CACHE_TTL = 60
#: quanto do `_source` volta pro browser. O INSS tem 650 CNPJs e 104 grafias —
#: mandar tudo por item × 8 itens a cada tecla é payload que ninguém lê. O
#: NÚMERO total continua indo (`n_documentos`/`n_variantes`); a lista é amostra.
ENTIDADES_MAX_DOCUMENTOS = 3
ENTIDADES_MAX_GRAFIAS = 3


def _indice_entidades() -> str:
    """Nome do índice ES de entidades — configurável por setting/env.

    Hoje aponta pro índice de TESTE (`voyager-entidades-teste`, 1.141.630
    entidades construídas em 12/08/2026). A promoção pro nome final
    (`voyager-entidades`) é trocar `ENTIDADES_INDICE_SUFIXO` no ambiente, sem
    caçar string hardcoded no código.
    """
    sufixo = getattr(settings, 'ENTIDADES_INDICE_SUFIXO', entidades_svc.INDICE)
    return index_name(sufixo)


def _int_no_intervalo(valor, default: int, minimo: int, maximo: int) -> int:
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return default
    return max(minimo, min(n, maximo))


def _flag(valor) -> bool:
    return str(valor or '').strip().lower() in ('1', 'true', 'sim', 'yes', 'on')


def _item_entidade(fonte: dict) -> dict:
    """Documento do índice → item do dropdown (só o que a UI mostra/usa)."""
    documentos = [d for d in (fonte.get('documentos') or []) if d]
    variantes = fonte.get('variantes') or []
    # `variantes_busca` são as grafias com peso (≥2 linhas / ≥1% da entidade) —
    # é a amostra honesta pra mostrar "o que este item unifica"; grafia de 1
    # linha é typo de cartório e daria uma impressão errada da entidade.
    grafias = fonte.get('variantes_busca') or variantes

    # `n_processos` PODE não existir (o índice está sendo populado). Ausente,
    # None, ou não-numérico ⇒ None = DESCONHECIDO. Nunca 0.
    bruto = fonte.get('n_processos')
    n_processos = int(bruto) if isinstance(bruto, (int, float)) and not isinstance(bruto, bool) else None

    return {
        'entidade_id': fonte.get('entidade_id'),
        'nome_canonico': fonte.get('nome_canonico') or '',
        # 'cnpj' = identidade PROVADA por documento; 'nome' = heurística de
        # grafia. Quem consome precisa saber que confiança tem (decisão 3 do
        # módulo de entidades) — por isso vai no payload, não fica escondido.
        'chave': fonte.get('chave') or '',
        'n_variantes': fonte.get('n_variantes') if fonte.get('n_variantes') is not None else len(variantes),
        'grafias_exemplo': grafias[:ENTIDADES_MAX_GRAFIAS],
        'raiz_cnpj': fonte.get('raiz_cnpj'),
        'documentos': documentos[:ENTIDADES_MAX_DOCUMENTOS],
        'n_documentos': len(documentos),
        'eh_ente_publico': bool(fonte.get('eh_ente_publico')),
        # proxy de PREVALÊNCIA (linhas de `Parte` fundidas) — não é contagem de
        # processos. Vai com este nome pra ninguém confundir os dois.
        'n_partes': fonte.get('n_partes'),
        'n_processos': n_processos,
    }


@login_required
@require_GET
def entidades_autocomplete(request):
    """GET /dashboard/api/entidades/autocomplete?q=&n=&ente=1 — sugestões de entidade.

    Login-gated como todo o módulo comercial. `q` com menos de
    `ENTIDADES_MIN_CARACTERES` devolve lista vazia SEM tocar no ES (o motivo vai
    no payload, pra UI dizer por que não buscou). ES fora → 503, nunca 500 cru.
    Cache de `ENTIDADES_CACHE_TTL`s por (termo, n, ente): quem digita "inss"
    passa pelas mesmas 4 requisições que o vizinho digitou há 10 segundos.
    """
    termo = ' '.join((request.GET.get('q') or '').split())[:120]
    tamanho = _int_no_intervalo(request.GET.get('n'), ENTIDADES_N_DEFAULT, 1, ENTIDADES_N_MAX)
    somente_ente = _flag(request.GET.get('ente'))

    base = {
        'termo': termo,
        'min_caracteres': ENTIDADES_MIN_CARACTERES,
        'itens': [],
        'buscou': False,
        'motivo': None,
    }
    if not termo:
        return _json({**base, 'motivo': 'sem_termo'})
    if len(termo) < ENTIDADES_MIN_CARACTERES:
        return _json({**base, 'motivo': 'termo_curto'})

    chave_cache = 'ac_ent:' + hashlib.sha1(
        f'{_indice_entidades()}|{termo.lower()}|{tamanho}|{int(somente_ente)}'.encode('utf-8')
    ).hexdigest()[:20]
    try:
        cacheado = cache.get(chave_cache)
    except Exception:                       # Redis fora não pode derrubar a busca
        cacheado = None
    if cacheado is not None:
        return _json({**cacheado, 'cache': True})

    body = entidades_svc.query_autocomplete(termo, tamanho, somente_ente)
    try:
        resp = get_es().search(index=_indice_entidades(), **body)
    except Exception:
        logger.exception('entidades_autocomplete: falha ao consultar o índice de entidades',
                         extra={'termo': termo, 'n': tamanho, 'ente': somente_ente})
        return _json({'erro': 'falha ao consultar o índice de entidades'}, status=503)

    itens = [_item_entidade(h.get('_source') or {}) for h in resp.get('hits', {}).get('hits', [])]
    payload = {
        **base,
        'buscou': True,
        'itens': itens,
        'indice': _indice_entidades(),
        # a UI precisa saber se a contagem de processos EXISTE pra escrever
        # "não contamos ainda" em vez de inventar zero
        'contagem_processos_disponivel': any(i['n_processos'] is not None for i in itens),
        'levou_ms': resp.get('took'),
    }
    try:
        cache.set(chave_cache, payload, ENTIDADES_CACHE_TTL)
    except Exception:
        pass                                 # cache é otimização, não requisito
    return _json(payload)


# --------------------------------------------------------------------------- #
# TELAS DE ENTIDADES — ranking ("quem mais litiga") e ficha de UMA entidade
# --------------------------------------------------------------------------- #
# Mesmo padrão do resto do módulo: a view só faz parse → serviço → JsonResponse.
# TODA a lógica (query, honestidade, cache, contrato) mora em
# `search/agg_entidade.py`, e o CONTRATO JSON completo está documentado lá.


@never_cache
@login_required
@require_GET
def entidades_page(request):
    """GET /dashboard/overview/entidades/ — ranking de entidades ("quem deve").

    Só o shell; os dados vêm de `entidades-ranking` via fetch. `@never_cache`
    pelo mesmo motivo das outras telas: sem ele o browser serve o HTML do disco
    e o usuário continua vendo a versão anterior depois do deploy.
    """
    return render(request, 'dashboard/entidades.html')


@never_cache
@login_required
@require_GET
def entidade_page(request, entidade_id):
    """GET /dashboard/overview/entidade/<entidade_id>/ — ficha de UMA entidade.

    `entidade_id` vai pro contexto porque o template monta a URL da API com ele
    (o template tem fallback lendo a própria URL, mas depender disso deixaria a
    página quebrada em qualquer mudança de rota).
    """
    return render(request, 'dashboard/entidade.html', {'entidade_id': entidade_id})


@login_required
@require_GET
def entidades_ranking(request):
    """GET /dashboard/api/entidades/ — ranking de entidades ("quem deve").

    Querystring: `q, ente, min_partes, contadas, ordenar, n, cursor, offset`
    (ver contrato no topo de `search/agg_entidade.py`). NÃO aceita `uf` — o
    porquê, medido, está documentado no mesmo lugar.
    Cursor forjado/corrompido → 400; ES fora → 503. Nunca 500 cru.
    """
    opcoes = agg_entidade_svc.parse_ranking(request.GET)
    try:
        payload = agg_entidade_svc.ranking_entidades(opcoes)
    except agg_entidade_svc.CursorInvalido as exc:
        return _json({'erro': str(exc) or 'cursor inválido'}, status=400)
    except Exception:
        logger.exception('entidades_ranking: falha ao consultar o índice de entidades',
                         extra={'opcoes': opcoes})
        return _json({'erro': 'falha ao consultar o índice de entidades'}, status=503)
    return _json(payload)


@login_required
@require_GET
def entidades_ficha(request, entidade_id):
    """GET /dashboard/api/entidades/<entidade_id>/ — ficha de UMA entidade.

    `<entidade_id>` é o `_id` do índice (`cnpj:29979036` | `nome:<sha1>`).
    Aceita os MESMOS filtros do mapa (`agg_overview.parse_filtros`); `parte` e
    `entidade_id` da querystring são ignorados (quem manda é a rota).
    Entidade inexistente → 404; ES fora → 503. Nunca 500 cru.
    """
    filtros = agg_overview.parse_filtros(request.GET)
    try:
        payload = agg_entidade_svc.ficha_entidade(entidade_id, filtros)
    except agg_entidade_svc.EntidadeNaoEncontrada:
        return _json({'erro': f'entidade não encontrada: {entidade_id}'}, status=404)
    except Exception:
        logger.exception('entidades_ficha: falha na agregação ES',
                         extra={'entidade_id': entidade_id, 'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)
