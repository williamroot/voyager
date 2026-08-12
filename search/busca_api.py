"""API de Busca v1 — serviço de consulta rica sobre o Elasticsearch.

Serviço que alimenta os endpoints REST `/api/v1/busca/*` (ver `api/busca_views.py`).
TODA leitura vem dos índices ES `voyager-processos` (~71M docs) e
`voyager-movimentacoes` (~1.16B docs) — NUNCA do Postgres (contido em prod).

--------------------------------------------------------------------------------
CONTRATO v1 (estável; mudanças breaking ⇒ v2 em paths novos)
--------------------------------------------------------------------------------
Auth: igual aos demais endpoints externos — `HasAPIKeyOrBearer`
(`Authorization: Bearer <token>` | `Authorization: Api-Key <key>` | `X-API-Key`).

1) GET /api/v1/busca/processos/<cnj>/
   Lookup exato por CNJ (term no campo keyword `proc`). Aceita CNJ com máscara
   (`1105916-36.2026.8.26.0053`) ou só dígitos (20). 404 se não existir.
   → { "versao": "v1", "processo": { ...doc ES completo... },
       "movimentacoes_url": "/api/v1/busca/processos/<cnj>/movimentacoes/" }

2) GET /api/v1/busca/processos/<cnj>/movimentacoes/?size=&cursor=
   Movimentações do processo (índice movimentacoes, filter term `proc`),
   ordenadas por publish_date desc + id desc. Paginação por cursor
   (search_after opaco, base64) — NUNCA from/size (índice com 1.16B docs).
   → { "versao": "v1", "total": {"value": N, "relation": "eq|gte"},
       "results": [ {id, proc, tribunal, publish_date, available_at,
                     tipo_comunicacao, nome_orgao, classe_nome, codigo_classe,
                     body, docurl}, ... ],
       "size": 50, "next_cursor": "..."|null }

3) GET /api/v1/busca/processos/?parte=&advogado=&...filtros&ordenar=&size=&cursor=
   Busca de processos. Critérios textuais (ambos opcionais, combináveis):
     parte      match AND no campo text `partes`  (nomes concatenados)
     advogado   match AND no campo text `advs`    (aceita nome ou nº OAB)
   Filtros combinados (todos opcionais):
     tribunal            keyword; CSV pra vários (TJSP,TJRJ ⇒ terms)
     uf                  keyword (SP)
     ano_min/ano_max     int, ano_cnj (clamp [1990, ano atual])
     classificacao       keyword (PRECATORIO|PRE_PRECATORIO|...)
     tem_sinal_precatorio     bool
     tem_ente_publico_passivo bool
     segredo_justica          bool
     enriquecido              bool
     codigo_classe       keyword
     assunto_codigo      keyword (TPU)
     valor_min/valor_max float, valor_causa (negativo é ignorado)
     ultima_mov_gte/lte  date ISO (ultima_movimentacao_em)
     autuacao_gte/lte    date ISO (data_autuacao — ver Limitações)
   Ordenação (`ordenar`):
     relevancia   _score desc (default quando há parte/advogado)
     recente      ultima_movimentacao_em desc (default sem texto)
     valor_desc / valor_asc   valor_causa (docs sem valor vão pro fim)
   → { "versao": "v1", "total": {...}, "results": [ ...docs ES... ],
       "size": 20, "next_cursor": ...,
       "ordenar": "...", "filtros": {...ecoa saneados...} }

4) GET /api/v1/busca/movimentacoes/?q=&...filtros&ordenar=&size=&cursor=
   Full-text no `body` das publicações (match AND) com highlight
   (`<em>...</em>`, até 3 fragmentos de 200 chars). Exige pelo menos um de:
   q, proc, tribunal (400 caso contrário — índice de 1.16B docs).
   Filtros: tribunal (CSV), proc (CNJ), tipo_comunicacao, codigo_classe,
   publicado_gte/lte (publish_date, ISO).
   Ordenação: relevancia (default com q) | recente (publish_date desc).
   → { "versao": "v1", "total": {...},
       "results": [ {id, proc, tribunal, publish_date, tipo_comunicacao,
                     nome_orgao, classe_nome, codigo_classe, docurl,
                     highlight: ["...<em>precatório</em>..."]}, ... ],
       "size": 20, "next_cursor": ..., "ordenar": ..., "filtros": {...} }
   (não devolve `body` inteiro na lista — use o endpoint 2 ou
    /api/v1/diarios-oficiais/doc/get/<id> pro texto completo)

Paginação: `next_cursor` é OPACO (base64 dos sort values do último hit —
search_after). Repassar intocado; null ⇒ acabou. Cursor inválido ⇒ 400.
`total` segue o envelope ES: cap em 10.000 (`relation: "gte"`) — barato e
suficiente pra UI/integração.

Erros (nunca 500 cru): 400 `{"erro": "..."}` param inválido;
404 `{"erro": "processo_nao_encontrado"}`; 503 `{"erro": "elasticsearch_indisponivel"}`.
Entradas recuperáveis degradam limpo: ano clampado, valor negativo ignorado,
enum desconhecido cai no default, size clampado em [1, 100].

--------------------------------------------------------------------------------
LIMITAÇÕES HONESTAS (estado ago/2026)
--------------------------------------------------------------------------------
- `parte`/`advogado` são MATCH TEXTUAL num campo concatenado ("Fulano, Sicrano").
  Sem polo, sem CPF/CNPJ, sem match exato de pessoa. Upgrade planejado: o mapping
  já tem o nested `participacoes` {nome, nome.raw, documento, oab, tipo, polo,
  papel, eh_advogado} (agente ES-SCHEMA), mas a cobertura em prod é 0 até o
  reindex dos 71M docs. Quando coberto, os params ganham `parte_polo=`,
  `parte_documento=` e `oab=` via nested query — sem breaking change nos atuais.
- Campos novos do mapping (data_autuacao, segredo_justica, enriquecido,
  assunto_codigo, tem_ente_publico_passivo) só existem em docs REINDEXADOS após
  o commit 7d03bab — em prod hoje `data_autuacao` tem cobertura 0 e
  tem_sinal_precatorio ~590k docs. Filtrar por eles restringe ao subset coberto.
- `valor_causa` inclui outliers de digitação (ex.: R$ 595 bi num TJAL) — o
  serviço não higieniza; clamp fica a cargo do cliente (`valor_max`).
- Sem rate limit (deliberado, igual ao resto da API).
--------------------------------------------------------------------------------
"""
import base64
import binascii
import datetime
import json
import logging
import re

logger = logging.getLogger('voyager.busca_api')

VERSAO = 'v1'

#: clamps de paginação
SIZE_DEFAULT = 20
SIZE_MAX = 100
MOVS_SIZE_DEFAULT = 50

#: janela de plausibilidade pro ano_cnj (mesma do agg_comercial)
ANO_MIN_PLAUSIVEL = 1990

#: timeout da query no lado do ES (o client já tem request_timeout do settings)
ES_QUERY_TIMEOUT = '15s'

#: ordenações aceitas
ORDENACOES_PROCESSOS = {'relevancia', 'recente', 'valor_desc', 'valor_asc'}
ORDENACOES_MOVS = {'relevancia', 'recente'}

#: campos devolvidos na LISTA de movimentações do full-text (sem body inteiro)
_MOV_LIST_SOURCE = [
    'id', 'proc', 'tribunal', 'publish_date', 'available_at',
    'tipo_comunicacao', 'nome_orgao', 'classe_nome', 'codigo_classe', 'docurl',
]

_RE_CNJ_MASCARA = re.compile(r'^\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}$')

_FLAGS_BOOL_PROC = (
    ('tem_sinal_precatorio', 'tem_sinal_precatorio'),
    ('tem_ente_publico_passivo', 'tem_ente_publico_passivo'),
    ('segredo_justica', 'segredo_justica'),
    ('enriquecido', 'enriquecido'),
)


class BuscaParamError(Exception):
    """Parâmetro irrecuperável (cursor corrompido, CNJ inválido, critério ausente).

    As views mapeiam pra HTTP 400 com {'erro': str(e)}.
    """


# --------------------------------------------------------------------------- #
# Helpers de saneamento
# --------------------------------------------------------------------------- #
def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ('true', '1', 'sim', 'yes'):
        return True
    if s in ('false', '0', 'nao', 'não', 'no'):
        return False
    return None


def _to_date_iso(v):
    """Aceita YYYY-MM-DD ou ISO datetime; devolve a string validada ou None."""
    if not v:
        return None
    s = str(v).strip()
    for parser in (datetime.date.fromisoformat, datetime.datetime.fromisoformat):
        try:
            parser(s.replace('Z', '+00:00') if parser is datetime.datetime.fromisoformat else s)
            return s
        except ValueError:
            continue
    return None


def clamp_size(v, default=SIZE_DEFAULT):
    n = _to_int(v, default)
    if n is None:
        n = default
    return max(1, min(n, SIZE_MAX))


def normalizar_cnj(cnj):
    """Normaliza um CNJ pro formato mascarado do índice (NNNNNNN-DD.AAAA.J.TR.OOOO).

    Aceita já-mascarado ou 20 dígitos puros. Inválido ⇒ BuscaParamError.
    """
    s = (cnj or '').strip()
    if _RE_CNJ_MASCARA.match(s):
        return s
    digitos = re.sub(r'\D', '', s)
    if len(digitos) == 20:
        return (f'{digitos[0:7]}-{digitos[7:9]}.{digitos[9:13]}'
                f'.{digitos[13]}.{digitos[14:16]}.{digitos[16:20]}')
    raise BuscaParamError('cnj_invalido')


# --------------------------------------------------------------------------- #
# Cursor opaco (search_after)
# --------------------------------------------------------------------------- #
def encode_cursor(sort_values) -> str:
    raw = json.dumps(list(sort_values), separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_cursor(cursor):
    """Decodifica o cursor opaco. Corrompido ⇒ BuscaParamError ('cursor_invalido')."""
    if not cursor:
        return None
    pad = '=' * (-len(cursor) % 4)
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise BuscaParamError('cursor_invalido')
    if not isinstance(values, list) or not values:
        raise BuscaParamError('cursor_invalido')
    return values


# --------------------------------------------------------------------------- #
# Filtros — processos
# --------------------------------------------------------------------------- #
def parse_filtros_processos(qd) -> dict:
    """Extrai e SANEIA filtros de processos de um dict-like (request.GET).

    Nunca levanta: inválido é clampado/descartado. Devolve só o efetivamente usado.
    """
    g = qd.get
    filtros: dict = {}

    tribunal = (g('tribunal') or '').strip().upper()
    if tribunal:
        siglas = [t.strip() for t in tribunal.split(',') if t.strip()]
        if siglas:
            filtros['tribunal'] = siglas

    uf = (g('uf') or '').strip().upper()
    if uf:
        filtros['uf'] = uf

    classificacao = (g('classificacao') or '').strip().upper()
    if classificacao:
        filtros['classificacao'] = classificacao

    for param, _campo in _FLAGS_BOOL_PROC:
        b = _to_bool(g(param))
        if b is not None:
            filtros[param] = b

    codigo_classe = (g('codigo_classe') or '').strip()
    if codigo_classe:
        filtros['codigo_classe'] = codigo_classe

    assunto_codigo = (g('assunto_codigo') or '').strip()
    if assunto_codigo:
        filtros['assunto_codigo'] = assunto_codigo

    ano_atual = datetime.date.today().year
    ano_min = _to_int(g('ano_min'))
    ano_max = _to_int(g('ano_max'))
    if ano_min is not None:
        filtros['ano_min'] = max(ANO_MIN_PLAUSIVEL, min(ano_min, ano_atual))
    if ano_max is not None:
        filtros['ano_max'] = max(ANO_MIN_PLAUSIVEL, min(ano_max, ano_atual))
    if 'ano_min' in filtros and 'ano_max' in filtros and filtros['ano_min'] > filtros['ano_max']:
        filtros['ano_min'], filtros['ano_max'] = filtros['ano_max'], filtros['ano_min']

    valor_min = _to_float(g('valor_min'))
    valor_max = _to_float(g('valor_max'))
    if valor_min is not None and valor_min >= 0:
        filtros['valor_min'] = valor_min
    if valor_max is not None and valor_max >= 0:
        filtros['valor_max'] = valor_max
    if 'valor_min' in filtros and 'valor_max' in filtros and filtros['valor_min'] > filtros['valor_max']:
        filtros['valor_min'], filtros['valor_max'] = filtros['valor_max'], filtros['valor_min']

    for param in ('ultima_mov_gte', 'ultima_mov_lte', 'autuacao_gte', 'autuacao_lte'):
        d = _to_date_iso(g(param))
        if d:
            filtros[param] = d

    return filtros


def build_clauses_processos(filtros: dict) -> list:
    """Traduz filtros saneados em cláusulas `filter` (sem scoring — cacheável)."""
    clauses: list = []

    trib = filtros.get('tribunal')
    if trib:
        if len(trib) == 1:
            clauses.append({'term': {'tribunal': trib[0]}})
        else:
            clauses.append({'terms': {'tribunal': trib}})
    if filtros.get('uf'):
        clauses.append({'term': {'uf': filtros['uf']}})
    if filtros.get('classificacao'):
        clauses.append({'term': {'classificacao': filtros['classificacao']}})
    if filtros.get('codigo_classe'):
        clauses.append({'term': {'codigo_classe': filtros['codigo_classe']}})
    if filtros.get('assunto_codigo'):
        clauses.append({'term': {'assunto_codigo': filtros['assunto_codigo']}})

    for param, campo in _FLAGS_BOOL_PROC:
        if param in filtros:
            clauses.append({'term': {campo: filtros[param]}})

    ano_range = {}
    if 'ano_min' in filtros:
        ano_range['gte'] = filtros['ano_min']
    if 'ano_max' in filtros:
        ano_range['lte'] = filtros['ano_max']
    if ano_range:
        clauses.append({'range': {'ano_cnj': ano_range}})

    valor_range = {}
    if 'valor_min' in filtros:
        valor_range['gte'] = filtros['valor_min']
    if 'valor_max' in filtros:
        valor_range['lte'] = filtros['valor_max']
    if valor_range:
        clauses.append({'range': {'valor_causa': valor_range}})

    mov_range = {}
    if 'ultima_mov_gte' in filtros:
        mov_range['gte'] = filtros['ultima_mov_gte']
    if 'ultima_mov_lte' in filtros:
        mov_range['lte'] = filtros['ultima_mov_lte']
    if mov_range:
        clauses.append({'range': {'ultima_movimentacao_em': mov_range}})

    aut_range = {}
    if 'autuacao_gte' in filtros:
        aut_range['gte'] = filtros['autuacao_gte']
    if 'autuacao_lte' in filtros:
        aut_range['lte'] = filtros['autuacao_lte']
    if aut_range:
        clauses.append({'range': {'data_autuacao': aut_range}})

    return clauses


# --------------------------------------------------------------------------- #
# Filtros — movimentações
# --------------------------------------------------------------------------- #
def parse_filtros_movs(qd) -> dict:
    g = qd.get
    filtros: dict = {}

    tribunal = (g('tribunal') or '').strip().upper()
    if tribunal:
        siglas = [t.strip() for t in tribunal.split(',') if t.strip()]
        if siglas:
            filtros['tribunal'] = siglas

    proc = (g('proc') or '').strip()
    if proc:
        filtros['proc'] = normalizar_cnj(proc)   # inválido ⇒ BuscaParamError (400)

    tipo = (g('tipo_comunicacao') or '').strip()
    if tipo:
        filtros['tipo_comunicacao'] = tipo

    codigo_classe = (g('codigo_classe') or '').strip()
    if codigo_classe:
        filtros['codigo_classe'] = codigo_classe

    for param in ('publicado_gte', 'publicado_lte'):
        d = _to_date_iso(g(param))
        if d:
            filtros[param] = d

    return filtros


def build_clauses_movs(filtros: dict) -> list:
    clauses: list = []
    trib = filtros.get('tribunal')
    if trib:
        if len(trib) == 1:
            clauses.append({'term': {'tribunal': trib[0]}})
        else:
            clauses.append({'terms': {'tribunal': trib}})
    if filtros.get('proc'):
        clauses.append({'term': {'proc': filtros['proc']}})
    if filtros.get('tipo_comunicacao'):
        clauses.append({'term': {'tipo_comunicacao': filtros['tipo_comunicacao']}})
    if filtros.get('codigo_classe'):
        clauses.append({'term': {'codigo_classe': filtros['codigo_classe']}})
    pub_range = {}
    if 'publicado_gte' in filtros:
        pub_range['gte'] = filtros['publicado_gte']
    if 'publicado_lte' in filtros:
        pub_range['lte'] = filtros['publicado_lte']
    if pub_range:
        clauses.append({'range': {'publish_date': pub_range}})
    return clauses


# --------------------------------------------------------------------------- #
# Ordenação (sort estável: sempre com tiebreaker `id` — exigência do search_after)
# --------------------------------------------------------------------------- #
def sort_processos(ordenar: str, tem_texto: bool) -> tuple[str, list]:
    """Resolve (ordenar_efetivo, sort ES). Enum desconhecido cai no default."""
    ordenar = (ordenar or '').strip().lower()
    if ordenar not in ORDENACOES_PROCESSOS:
        ordenar = 'relevancia' if tem_texto else 'recente'
    if ordenar == 'relevancia' and not tem_texto:
        ordenar = 'recente'   # sem texto não há score — relevância vira recente

    if ordenar == 'relevancia':
        sort = [{'_score': 'desc'}, {'id': 'desc'}]
    elif ordenar == 'valor_desc':
        sort = [{'valor_causa': {'order': 'desc', 'missing': '_last'}}, {'id': 'desc'}]
    elif ordenar == 'valor_asc':
        sort = [{'valor_causa': {'order': 'asc', 'missing': '_last'}}, {'id': 'desc'}]
    else:  # recente
        sort = [{'ultima_movimentacao_em': {'order': 'desc', 'missing': '_last'}},
                {'id': 'desc'}]
    return ordenar, sort


def sort_movs(ordenar: str, tem_texto: bool) -> tuple[str, list]:
    ordenar = (ordenar or '').strip().lower()
    if ordenar not in ORDENACOES_MOVS:
        ordenar = 'relevancia' if tem_texto else 'recente'
    if ordenar == 'relevancia' and not tem_texto:
        ordenar = 'recente'
    if ordenar == 'relevancia':
        sort = [{'_score': 'desc'}, {'id': 'desc'}]
    else:
        sort = [{'publish_date': {'order': 'desc', 'missing': '_last'}}, {'id': 'desc'}]
    return ordenar, sort


# --------------------------------------------------------------------------- #
# Montagem de corpo + execução
# --------------------------------------------------------------------------- #
def _match_and(campo: str, texto: str) -> dict:
    """match com operator AND — todos os termos precisam aparecer (precisão >
    recall, norma da casa)."""
    return {'match': {campo: {'query': texto, 'operator': 'and'}}}


def _executar(indice: str, body: dict) -> dict:
    es = get_es()
    resp = es.search(index=index_name(indice), body=body)
    if hasattr(resp, 'body'):          # ES 8 devolve AttrDict
        resp = dict(resp.body)
    elif not isinstance(resp, dict):
        resp = dict(resp)
    return resp


def _montar_pagina(resp: dict, size: int, com_highlight: bool = False,
                   source_keys=None) -> dict:
    """Extrai hits do envelope ES → {'total', 'results', 'next_cursor'}.

    next_cursor = sort values do último hit SE a página veio cheia (senão null).
    """
    hits = resp.get('hits', {}).get('hits', [])
    total = resp.get('hits', {}).get('total') or {}
    results = []
    for h in hits:
        item = h.get('_source', {}) or {}
        if source_keys:
            item = {k: item.get(k) for k in source_keys}
        if com_highlight:
            item['highlight'] = (h.get('highlight') or {}).get('body', [])
        results.append(item)
    next_cursor = None
    if len(hits) == size and hits[-1].get('sort'):
        next_cursor = encode_cursor(hits[-1]['sort'])
    return {
        'total': {'value': total.get('value', 0), 'relation': total.get('relation', 'eq')},
        'results': results,
        'next_cursor': next_cursor,
    }


# --------------------------------------------------------------------------- #
# API pública do serviço
# --------------------------------------------------------------------------- #
def get_processo(cnj: str):
    """Doc completo do processo por CNJ exato (term em `proc`). None se não existe."""
    cnj = normalizar_cnj(cnj)
    body = {
        'size': 1,
        'query': {'bool': {'filter': [{'term': {'proc': cnj}}]}},
        'timeout': ES_QUERY_TIMEOUT,
    }
    resp = _executar('processos', body)
    hits = resp.get('hits', {}).get('hits', [])
    return hits[0].get('_source') if hits else None


def movimentacoes_por_cnj(cnj: str, size=MOVS_SIZE_DEFAULT, cursor=None) -> dict:
    """Movimentações de um processo, paginadas por search_after (publish_date desc)."""
    cnj = normalizar_cnj(cnj)
    size = clamp_size(size, MOVS_SIZE_DEFAULT)
    body = {
        'size': size,
        'query': {'bool': {'filter': [{'term': {'proc': cnj}}]}},
        'sort': [{'publish_date': {'order': 'desc', 'missing': '_last'}}, {'id': 'desc'}],
        'timeout': ES_QUERY_TIMEOUT,
    }
    after = decode_cursor(cursor)
    if after:
        body['search_after'] = after
    resp = _executar('movimentacoes', body)
    pagina = _montar_pagina(resp, size)
    pagina.update({'versao': VERSAO, 'proc': cnj, 'size': size})
    return pagina


def buscar_processos(parte=None, advogado=None, filtros=None, ordenar=None,
                     size=SIZE_DEFAULT, cursor=None) -> dict:
    """Busca de processos: texto (partes/advs) + filtros combinados + ordenação.

    Sem critério nenhum ainda é válido (browse ordenado por recência).
    """
    filtros = filtros or {}
    size = clamp_size(size)
    must = []
    if parte:
        must.append(_match_and('partes', parte))
    if advogado:
        must.append(_match_and('advs', advogado))

    ordenar_efetivo, sort = sort_processos(ordenar, tem_texto=bool(must))

    bool_q: dict = {}
    if must:
        bool_q['must'] = must
    clauses = build_clauses_processos(filtros)
    if clauses:
        bool_q['filter'] = clauses

    body = {
        'size': size,
        'sort': sort,
        'timeout': ES_QUERY_TIMEOUT,
        # Sem isto o ES para de contar em 10.000 e devolve
        # {"value": 10000, "relation": "gte"} — a busca por "Instituto Nacional
        # do Seguro Social" dizia "10.000+" quando são 4.402.239 processos.
        # "10 mil ou mais" e "4,4 milhões" levam a decisões comerciais opostas.
        # Custa uma contagem exata por busca; num índice de 71M com filtro por
        # termo o ES resolve por bitset, não por varredura.
        'track_total_hits': True,
    }
    if bool_q:
        body['query'] = {'bool': bool_q}
    else:
        body['query'] = {'match_all': {}}
    if ordenar_efetivo == 'relevancia':
        body['track_scores'] = True   # sort por _score já traz; explícito por clareza
    after = decode_cursor(cursor)
    if after:
        body['search_after'] = after

    resp = _executar('processos', body)
    pagina = _montar_pagina(resp, size)
    pagina.update({
        'versao': VERSAO,
        'size': size,
        'ordenar': ordenar_efetivo,
        'filtros': _ecoar_filtros(filtros, parte=parte, advogado=advogado),
    })
    return pagina


def buscar_movimentacoes(q=None, filtros=None, ordenar=None,
                         size=SIZE_DEFAULT, cursor=None) -> dict:
    """Full-text no body das publicações + highlight + filtros. search_after only.

    Exige pelo menos um critério (q, proc ou tribunal) — 1.16B docs sem filtro
    é varredura cega; BuscaParamError ⇒ 400.
    """
    filtros = filtros or {}
    q = (q or '').strip()
    if not q and not filtros.get('proc') and not filtros.get('tribunal'):
        raise BuscaParamError('informe_q_proc_ou_tribunal')
    size = clamp_size(size)

    ordenar_efetivo, sort = sort_movs(ordenar, tem_texto=bool(q))

    bool_q: dict = {}
    if q:
        bool_q['must'] = [_match_and('body', q)]
    clauses = build_clauses_movs(filtros)
    if clauses:
        bool_q['filter'] = clauses

    body = {
        'size': size,
        'query': {'bool': bool_q},
        'sort': sort,
        '_source': _MOV_LIST_SOURCE,
        'timeout': ES_QUERY_TIMEOUT,
    }
    if q:
        body['highlight'] = {
            'fields': {'body': {'fragment_size': 200, 'number_of_fragments': 3}},
        }
    after = decode_cursor(cursor)
    if after:
        body['search_after'] = after

    resp = _executar('movimentacoes', body)
    pagina = _montar_pagina(resp, size, com_highlight=bool(q))
    pagina.update({
        'versao': VERSAO,
        'size': size,
        'ordenar': ordenar_efetivo,
        'filtros': _ecoar_filtros(filtros, q=q or None),
    })
    return pagina


def _ecoar_filtros(filtros: dict, **extras) -> dict:
    """Ecoa filtros saneados + critérios textuais efetivamente aplicados."""
    out = dict(filtros)
    for k, v in extras.items():
        if v:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Wrappers lazy (mockáveis nos testes sem exigir o pacote elasticsearch)
# --------------------------------------------------------------------------- #
def get_es():
    """Atributo de módulo mockável (`@patch('search.busca_api.get_es')`)."""
    from search.client import get_es as _get_es
    return _get_es()


def index_name(suffix: str) -> str:
    from django.conf import settings
    return f'{settings.ELASTICSEARCH_INDEX_PREFIX}-{suffix}'
