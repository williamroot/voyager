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
     cnj                 CNJ exato (term em `proc`; aceita com/sem máscara)
     documento           CPF/CNPJ da parte (nested) — normaliza máscara nos DOIS
                         sentidos e valida DV. Ver "DOCUMENTO" abaixo.
     documento_raiz      bool: expande CNPJ pra raiz (matriz + filiais)
     documento_parciais  bool: inclui os documentos MASCARADOS compatíveis
     parte_polo          keyword ativo|passivo|outros (nested)
     parte_papel         keyword AUTOR|EXECUTADO|… (nested)
     oab                 nº OAB do advogado (nested, normalizado p/ `UF12345`)
     vara                texto livre da vara/juízo — regexp acento/caixa-
                         insensível em `orgao_julgador` OU `juizo`
     vara_exata          valor exato de `orgao_julgador`/`juizo` (do autocomplete)
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
DOCUMENTO (CPF/CNPJ) — a máscara é do ÍNDICE, não do usuário
--------------------------------------------------------------------------------
Medido no ES de prod em 13/08/2026, sobre as 302.609 participações que têm
`participacoes.documento` preenchido:

    108.892  CNPJ canônico mascarado    29.979.036/0001-40
    108.826  CPF  canônico mascarado    038.499.054-11
     32.468  CPF  LGPD                  040.***.***-**
     30.348  CPF  LGPD                  136.XXX.XXX-XX
     14.023  CPF  LGPD com DV           872.***.***-97   ← revela 3 dígitos + DV
      7.500  CNPJ LGPD                  29.9**.***/****-**
        436  CNPJ LGPD                  00.3XX.XXX/XXXX-XX
        116  lixo (18-20 dígitos colados, ex. CNPJ+sufixo)

`regexp` de 11 e de 14 dígitos puros devolveu **ZERO**: NENHUM documento está
gravado sem máscara. Quem digita `29979036000140` acha 0 e quem digita
`29.979.036/0001-40` acha 23.217 — daí `normalizar_documento`, que joga a
entrada pra dígitos, valida o DV e GERA a forma mascarada canônica (mais os
dígitos crus, pelos 116 do lixo).

Expansões (nunca ligadas por padrão — abster > chutar):
- `documento_raiz`: CNPJ vira `prefix` na raiz de 8 dígitos (`29.979.036/`),
  unindo matriz e filiais. Medido: 23.217 → 27.044 processos (+16,5%), 11 ms.
- `documento_parciais`: inclui as formas LGPD compatíveis. Elas NÃO provam
  identidade — `038.***.***-**` casa 1 CPF em 1.000. A de DV
  (`872.***.***-97`) casa ~1 em 100.000, mas ainda é indício, não prova.

DV inválido é ERRO DE ENTRADA (400 `cpf_dv_invalido`/`cnpj_dv_invalido`), não
"não encontrado" — a tela precisa dizer coisas diferentes. Medido em 8.000
documentos distintos do índice: 1 (0,014%) tem DV inválido, então recusar não
esconde acervo. Pra esse resíduo existe `documento_forcar=1`.

--------------------------------------------------------------------------------
LIMITAÇÕES HONESTAS (estado ago/2026)
--------------------------------------------------------------------------------
- **Cobertura dos campos estruturados é baixa e a resposta tem que dizer isso.**
  Medido em 13/08/2026 sobre 71.308.806 processos:
  `partes`/`advs` (texto) 100% · `participacoes.nome` 1,90% ·
  `orgao_julgador` não-vazio 25,09% · `juizo` não-vazio 1,82% ·
  `participacoes.documento` 0,14% · `participacoes.oab` 0,067%.
  Buscar por CPF e achar 0 NÃO significa "essa pessoa não tem processo" —
  significa que 99,86% da base não tem documento indexado. Quem serve tela usa
  `search.busca_ui.buscar_processos_ui`, que carrega essa cobertura no payload.
- `parte`/`advogado` são MATCH TEXTUAL num campo concatenado ("Fulano, Sicrano"):
  100% de cobertura, zero estrutura (sem polo, sem pessoa exata). O caminho
  estruturado (`participacoes`) existe e é aceito, mas vale 1,9% da base — por
  isso `parte` sozinho continua indo pro campo texto, e só migra pro nested
  quando o cliente pede polo/papel (aí a resposta avisa a troca de escopo).
- `vara`/`vara_exata` batem em `orgao_julgador` OU `juizo`. `orgao_julgador`
  EXISTE em 100% dos docs mas é STRING VAZIA em 74,9% deles (53.417.123);
  `juizo` existe em 6,3% e 70,9% desses são vazios. Vazio nunca casa uma regexp
  com conteúdo, então o filtro se auto-exclui — mas o universo pesquisável é
  17,9M (orgão) + 1,3M (juízo), não 71M.
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

#: janela de plausibilidade pro ano_cnj (mesma do agg_overview)
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
# Documento da parte (CPF/CNPJ) — máscara nos dois sentidos + DV
# --------------------------------------------------------------------------- #
_RE_NAO_DIGITO = re.compile(r'\D')

#: pesos do 1º e do 2º dígito verificador do CNPJ
_PESOS_CNPJ_1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_PESOS_CNPJ_2 = (6,) + _PESOS_CNPJ_1


def _dv_cpf_ok(d: str) -> bool:
    """Dígito verificador do CPF. Repetido (111.111.111-11) é inválido de fato."""
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for n in (9, 10):
        soma = sum(int(d[i]) * ((n + 1) - i) for i in range(n))
        if (soma * 10) % 11 % 10 != int(d[n]):
            return False
    return True


def _dv_cnpj_ok(d: str) -> bool:
    if len(d) != 14 or len(set(d)) == 1:
        return False
    for pesos, n in ((_PESOS_CNPJ_1, 12), (_PESOS_CNPJ_2, 13)):
        soma = sum(int(d[i]) * pesos[i] for i in range(n))
        resto = soma % 11
        if (0 if resto < 2 else 11 - resto) != int(d[n]):
            return False
    return True


def mascarar_cpf(d: str) -> str:
    return f'{d[0:3]}.{d[3:6]}.{d[6:9]}-{d[9:11]}'


def mascarar_cnpj(d: str) -> str:
    return f'{d[0:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}'


def normalizar_documento(bruto, forcar=False) -> dict:
    """Normaliza CPF/CNPJ digitado de QUALQUER jeito pras formas do índice.

    O índice guarda SÓ a forma mascarada (medido: 0 documentos com 11/14 dígitos
    puros), então a normalização é digitar→dígitos→máscara canônica. Devolve
    também a raiz do CNPJ e as formas LGPD compatíveis, pro chamador decidir
    quanto quer esticar (nenhuma delas entra por padrão).

    `forcar=True` aceita DV inválido (0,014% do índice tem — ver docstring do
    módulo). Comprimento errado NUNCA passa: não é CPF nem CNPJ, ponto.

    Levanta BuscaParamError: 'documento_invalido' | 'cpf_dv_invalido' |
    'cnpj_dv_invalido'.
    """
    s = (bruto or '').strip()
    if not s:
        raise BuscaParamError('documento_invalido')
    digitos = _RE_NAO_DIGITO.sub('', s)

    if len(digitos) == 8:                    # raiz de CNPJ: só matriz+filiais
        raiz = digitos
        return {
            'entrada': s, 'digitos': digitos, 'tipo': 'raiz_cnpj',
            'mascarado': f'{raiz[0:2]}.{raiz[2:5]}.{raiz[5:8]}',
            'dv_valido': None, 'raiz': raiz,
            'raiz_prefixo': f'{raiz[0:2]}.{raiz[2:5]}.{raiz[5:8]}/',
            'termos_exatos': [],
            'termos_parciais': [f'{raiz[0:2]}.{raiz[2]}**.***/****-**',
                                f'{raiz[0:2]}.{raiz[2]}XX.XXX/XXXX-XX'],
        }

    if len(digitos) == 11:
        valido = _dv_cpf_ok(digitos)
        if not valido and not forcar:
            raise BuscaParamError('cpf_dv_invalido')
        mascarado = mascarar_cpf(digitos)
        return {
            'entrada': s, 'digitos': digitos, 'tipo': 'cpf',
            'mascarado': mascarado, 'dv_valido': valido,
            'raiz': None, 'raiz_prefixo': None,
            'termos_exatos': [mascarado, digitos],
            # ordem = da mais informativa pra menos: a de DV revela 3 dígitos +
            # os 2 verificadores; as outras, só os 3 primeiros
            'termos_parciais': [f'{digitos[0:3]}.***.***-{digitos[9:11]}',
                                f'{digitos[0:3]}.***.***-**',
                                f'{digitos[0:3]}.XXX.XXX-XX'],
        }

    if len(digitos) == 14:
        valido = _dv_cnpj_ok(digitos)
        if not valido and not forcar:
            raise BuscaParamError('cnpj_dv_invalido')
        mascarado = mascarar_cnpj(digitos)
        raiz = digitos[0:8]
        return {
            'entrada': s, 'digitos': digitos, 'tipo': 'cnpj',
            'mascarado': mascarado, 'dv_valido': valido,
            'raiz': raiz, 'raiz_prefixo': f'{raiz[0:2]}.{raiz[2:5]}.{raiz[5:8]}/',
            'termos_exatos': [mascarado, digitos],
            'termos_parciais': [f'{digitos[0:2]}.{digitos[2]}**.***/****-**',
                                f'{digitos[0:2]}.{digitos[2]}XX.XXX/XXXX-XX'],
        }

    raise BuscaParamError('documento_invalido')


#: como o documento é ecoado no payload (o dict inteiro tem termos de query)
_DOC_ECO = ('entrada', 'tipo', 'digitos', 'mascarado', 'dv_valido', 'raiz')


# --------------------------------------------------------------------------- #
# OAB — o índice grava `PE23255` (UF colada, sem separador, caixa alta)
# --------------------------------------------------------------------------- #
_RE_OAB_UF_DEPOIS = re.compile(r'^(\d+[A-Z]?)([A-Z]{2})$')


def normalizar_oab(bruto):
    """'sp 123.456' | '123456/SP' | 'SP123456' → 'SP123456'. Inválido ⇒ None.

    Formato medido no índice: sigla da UF + número, sem separador, às vezes com
    sufixo de letra (`CE5864A`). Zeros à esquerda NÃO são removidos — o índice
    tem os dois jeitos e inventar normalização aqui perderia acervo.
    """
    s = re.sub(r'[^0-9A-Za-z]', '', (bruto or '')).upper()
    if not s or not any(c.isdigit() for c in s):
        return None
    m = _RE_OAB_UF_DEPOIS.match(s)           # '123456SP' → 'SP123456'
    if m:
        return f'{m.group(2)}{m.group(1)}'
    return s


# --------------------------------------------------------------------------- #
# Vara / juízo — keyword cru, então regexp acento- e caixa-insensível
# --------------------------------------------------------------------------- #
#: `orgao_julgador` e `juizo` são keyword SEM analyzer: convivem "VARA DE
#: EXECUCAO FISCAL" (TRF1, caixa alta sem acento) e "1ª Vara de Execução Fiscal
#: do DF". Uma classe de caracteres por letra acentuável resolve os dois de uma
#: vez — medido: `.*[eéèêẽ]x[eéèêẽ][cç]…` devolve 341.697 = 313.105 (com acento)
#: + 28.592 (sem), em 28 ms frio / 8 ms morno.
_CLASSES_ACENTO = {
    'a': 'aáàâãäªAÁÀÂÃÄ', 'e': 'eéèêëEÉÈÊË', 'i': 'iíìîïIÍÌÎÏ',
    'o': 'oóòôõöºOÓÒÔÕÖ', 'u': 'uúùûüUÚÙÛÜ', 'c': 'cçCÇ', 'n': 'nñNÑ',
}
#: metacaracteres da regexp do Lucene (a sintaxe é a dele, não a do Python)
_REGEXP_ESCAPAR = set('.?+*|{}[]()"\\#@&<>~')

VARA_MIN_CARACTERES = 3
VARA_CAMPOS = ('orgao_julgador', 'juizo')


def regexp_contendo(termo: str) -> str:
    """Texto livre → regexp Lucene que casa a SUBSTRING, ignorando acento e caixa.

    Cada token vira um trecho e os tokens são unidos por `.*` — digitar mais
    palavras ESTREITA ("vara federal campinas" casa "5ª Vara Federal de
    Campinas"). Sem tokens úteis ⇒ None (o chamador não filtra).
    """
    tokens = [t for t in re.split(r'\s+', (termo or '').strip()) if t]
    if not tokens:
        return None
    partes = []
    for tok in tokens:
        buf = []
        for ch in tok:
            classe = _CLASSES_ACENTO.get(ch.lower())
            if classe:
                buf.append(f'[{classe}]')
            elif ch in _REGEXP_ESCAPAR:
                buf.append('\\' + ch)
            else:
                buf.append(ch)
        partes.append(''.join(buf))
    return '.*' + '.*'.join(partes) + '.*'


def clausula_vara(termo: str, campos=VARA_CAMPOS):
    """`vara=` (texto livre) → should sobre `orgao_julgador` e `juizo`.

    String vazia se auto-exclui: a regexp sempre tem conteúdo literal, e o
    documento com `juizo: ""` (3.169.792 deles) nunca casa. Não precisa de
    `must_not term ""` — que custaria uma cláusula a mais em toda busca.
    """
    rx = regexp_contendo(termo)
    if not rx:
        return None
    return {'bool': {'should': [
        {'regexp': {c: {'value': rx, 'case_insensitive': True}}} for c in campos
    ], 'minimum_should_match': 1}}


def clausula_vara_exata(valor: str, campos=VARA_CAMPOS):
    """Valor vindo do autocomplete → term exato. Vazio é recusado (casaria 74,9%)."""
    v = (valor or '').strip()
    if not v:
        return None
    return {'bool': {'should': [{'term': {c: v}} for c in campos],
                     'minimum_should_match': 1}}


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

    Recuperável é clampado/descartado (ano fora da janela, valor negativo, data
    lixo). Levanta BuscaParamError só no que é ERRO DE ENTRADA e não tem
    degradação honesta: CNJ malformado, CPF/CNPJ com DV inválido ou comprimento
    impossível — devolver "0 resultados" nesses casos seria mentir sobre o
    acervo. As views mapeiam pra 400.
    """
    g = qd.get
    filtros: dict = {}

    cnj = (g('cnj') or '').strip()
    if cnj:
        filtros['cnj'] = normalizar_cnj(cnj)          # inválido ⇒ 400

    documento = (g('documento') or '').strip()
    if documento:
        forcar = _to_bool(g('documento_forcar')) is True
        filtros['documento'] = normalizar_documento(documento, forcar=forcar)
        if _to_bool(g('documento_raiz')) is True:
            filtros['documento_raiz'] = True
        if _to_bool(g('documento_parciais')) is True:
            filtros['documento_parciais'] = True

    polo = (g('parte_polo') or '').strip().lower()
    if polo in ('ativo', 'passivo', 'outros'):
        filtros['parte_polo'] = polo

    papel = (g('parte_papel') or '').strip().upper()
    if papel:
        filtros['parte_papel'] = papel

    oab = normalizar_oab(g('oab'))
    if oab:
        filtros['oab'] = oab

    vara_exata = (g('vara_exata') or '').strip()
    if vara_exata:
        filtros['vara_exata'] = vara_exata
    else:
        vara = ' '.join((g('vara') or '').split())[:120]
        if len(vara) >= VARA_MIN_CARACTERES:
            filtros['vara'] = vara

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


def clausula_nested_parte(filtros: dict, nome=None):
    """Cláusula nested da PARTE (documento + polo + papel [+ nome]).

    Tudo num nested SÓ de propósito: "documento X no polo passivo" tem que ser a
    MESMA participação. Dois nested separados casariam o documento numa parte e
    o polo em outra do mesmo processo — falso positivo silencioso.
    """
    inner: list = []

    doc = filtros.get('documento')
    if doc:
        should = []
        if doc['termos_exatos']:
            should.append({'terms': {'participacoes.documento': doc['termos_exatos']}})
        if doc.get('raiz_prefixo') and (filtros.get('documento_raiz')
                                        or doc['tipo'] == 'raiz_cnpj'):
            should.append({'prefix': {'participacoes.documento': doc['raiz_prefixo']}})
        if filtros.get('documento_parciais') and doc['termos_parciais']:
            should.append({'terms': {'participacoes.documento': doc['termos_parciais']}})
        if not should:                    # raiz sem expansão: nada pesquisável
            return None
        inner.append({'bool': {'should': should, 'minimum_should_match': 1}})

    if filtros.get('parte_polo'):
        inner.append({'term': {'participacoes.polo': filtros['parte_polo']}})
    if filtros.get('parte_papel'):
        inner.append({'term': {'participacoes.papel': filtros['parte_papel']}})
    if nome:
        inner.append({'match': {'participacoes.nome': {'query': nome,
                                                       'operator': 'and'}}})
    if not inner:
        return None
    return {'nested': {'path': 'participacoes',
                       'query': {'bool': {'filter': inner}}}}


def clausula_nested_advogado(filtros: dict):
    """OAB é de OUTRA pessoa que não a parte — nested próprio (ver acima)."""
    if not filtros.get('oab'):
        return None
    return {'nested': {'path': 'participacoes', 'query': {'bool': {'filter': [
        {'term': {'participacoes.oab': filtros['oab']}},
    ]}}}}


def build_clauses_processos(filtros: dict, nome_parte=None) -> list:
    """Traduz filtros saneados em cláusulas `filter` (sem scoring — cacheável)."""
    clauses: list = []

    if filtros.get('cnj'):
        clauses.append({'term': {'proc': filtros['cnj']}})

    nested_parte = clausula_nested_parte(filtros, nome=nome_parte)
    if nested_parte:
        clauses.append(nested_parte)
    nested_adv = clausula_nested_advogado(filtros)
    if nested_adv:
        clauses.append(nested_adv)

    if filtros.get('vara_exata'):
        c = clausula_vara_exata(filtros['vara_exata'])
        if c:
            clauses.append(c)
    elif filtros.get('vara'):
        c = clausula_vara(filtros['vara'])
        if c:
            clauses.append(c)

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
def entre_aspas(texto: str):
    """Devolve o conteúdo se o termo VEIO entre aspas, senão `None`.

    Aceita aspas retas ("x") e as curvas que o Word/iOS produzem ("x" e ‟x„) —
    quem cola de um documento não digitou aspa reta, e falhar aí seria falhar
    exatamente com quem tentou ser preciso.
    """
    t = (texto or '').strip()
    pares = (('"', '"'), ('\u201c', '\u201d'), ('\u2018', '\u2019'), ("'", "'"))
    for a, b in pares:
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            miolo = t[1:-1].strip()
            if miolo:
                return miolo
    return None


_RE_TERMOS = re.compile(r'"([^"]+)"|\u201c([^\u201d]+)\u201d|(\S+)')


def separar_termos(texto: str) -> list:
    """Quebra a busca em termos, respeitando aspas.

        '"William Alves de Souza" Santander'
        → [('frase', 'William Alves de Souza'), ('termos', 'Santander')]

    É o que permite achar UM processo específico: "sou eu E é contra o banco X".
    Sem isso, a busca inteira vira uma frase só e não acha nada — o usuário
    tenta a coisa mais natural do mundo e recebe zero.

    As palavras SOLTAS entre as frases são agrupadas num termo só, porque
    "Santander S.A." solto deve ser um AND de duas palavras, não duas
    condições independentes.
    """
    frases, soltas = [], []
    for m in _RE_TERMOS.finditer(texto or ''):
        aspas = m.group(1) or m.group(2)
        if aspas:
            if aspas.strip():
                frases.append(('frase', aspas.strip()))
        elif m.group(3):
            soltas.append(m.group(3))
    if soltas:
        frases.append(('termos', ' '.join(soltas)))
    return frases


def clausulas_texto(campo: str, texto: str) -> list:
    """Uma cláusula por termo — todas obrigatórias (E, não OU).

    Cada frase entre aspas vira `match_phrase`; o resto vira `match` AND. Como
    todas entram em `must`, o processo precisa satisfazer TODAS: é assim que
    "eu E o banco" acha o processo certo em vez de todo processo meu ou todo
    processo do banco.
    """
    termos = separar_termos(texto)
    if not termos:
        return []
    if len(termos) == 1 and termos[0][0] == 'termos':
        return [{'match': {campo: {'query': termos[0][1], 'operator': 'and'}}}]
    out = []
    for tipo, valor in termos:
        if tipo == 'frase':
            out.append({'match_phrase': {campo: valor}})
        else:
            out.append({'match': {campo: {'query': valor, 'operator': 'and'}}})
    return out


def _match_and(campo: str, texto: str) -> dict:
    """Frase exata quando vem entre aspas; senão, match com AND.

    O AND casa os termos em QUALQUER lugar do campo — e `partes` é a lista
    inteira do processo, então "William Alves de Souza" casava "TIAGO ALVES DE
    SOUZA, JONATHAN WILLIAM RODRIGUES": três pessoas diferentes emprestando uma
    palavra cada. Medido em prod (14/08/2026): 923 processos no AND contra 20
    na frase exata — 97,8% de falso positivo.

    As aspas dão ao usuário o controle que só ele tem: se ele SABE o nome
    inteiro, frase exata; se está garimpando, AND. Adivinhar por ele erra dos
    dois lados.
    """
    frase = entre_aspas(texto)
    if frase:
        return {'match_phrase': {campo: frase}}
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


def get_esqueleto(cnj: str):
    """O processo no ACERVO DECLARADO ao CNJ (`voyager-acervo`), pra quando ele
    não está no nosso acervo rico.

    Existe porque "não achei" tinha dois significados colados num só: *não
    existe* e *existe, mas nunca passou pela nossa porta de entrada*. A ingestão
    é DJEN — publicação em diário —, e medimos que 81% a 96% do acervo declarado
    ao CNJ nunca teve publicação (processo parado, físico, em sistema que publica
    em diário próprio, ou com intimação pessoal/portal). Devolver o esqueleto
    troca um beco sem saída por "existe, e dá pra ir buscar".

    O que volta aqui é ESQUELETO de verdade: sem parte, sem advogado, sem valor
    — o Datajud não expõe esses campos. Quem exibir tem que dizer isso; por isso
    o `_so_esqueleto` vem no próprio documento, e não como convenção implícita.
    """
    cnj = normalizar_cnj(cnj)
    body = {
        'size': 1,
        'query': {'bool': {'filter': [{'term': {'proc': cnj}}]}},
        'sort': [{'atualizado_em': {'order': 'desc'}}],
        'timeout': ES_QUERY_TIMEOUT,
    }
    try:
        resp = _executar('acervo', body)
    except Exception:  # noqa: BLE001 — índice ausente num ambiente não pode derrubar a busca
        return None
    hits = resp.get('hits', {}).get('hits', [])
    if not hits:
        return None
    doc = dict(hits[0].get('_source') or {})
    doc['_fonte'] = 'acervo_cnj'
    doc['_so_esqueleto'] = True
    return doc


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


def montar_query_processos(parte=None, advogado=None, filtros=None):
    """Monta a `query` ES da busca de processos. Reusada pela v1 e pela tela.

    Devolve (query, tem_texto, parte_escopo):
    - `parte` sozinho vai pro campo TEXTO `partes` (100% de cobertura);
    - `parte` COM `parte_polo`/`parte_papel` migra pro nested `participacoes.nome`
      — polo/papel só existem lá — e o escopo cai pra 1,9% da base. Quem chama
      recebe `parte_escopo` justamente pra avisar o usuário da troca.
    """
    filtros = filtros or {}
    nested_parte = bool(parte) and bool(filtros.get('parte_polo')
                                        or filtros.get('parte_papel'))
    must = []
    if parte and not nested_parte:
        must.extend(clausulas_texto('partes', parte))
    if advogado:
        must.extend(clausulas_texto('advs', advogado))

    bool_q: dict = {}
    if must:
        bool_q['must'] = must
    clauses = build_clauses_processos(filtros,
                                      nome_parte=parte if nested_parte else None)
    if clauses:
        bool_q['filter'] = clauses

    query = {'bool': bool_q} if bool_q else {'match_all': {}}
    escopo = 'nested' if nested_parte else ('texto' if parte else None)
    return query, bool(must), escopo


def montar_body_processos(parte=None, advogado=None, filtros=None, ordenar=None,
                          size=SIZE_DEFAULT, cursor=None, total_exato=True):
    """Corpo da busca de processos. Devolve (body, ordenar_efetivo, parte_escopo).

    `total_exato=False` é pra quem vai contar numa sub-busca separada: MEDIDO no
    ES de prod (13/08) na busca do INSS (4,4M hits), `size=20 + sort +
    track_total_hits` custa 1.053-1.094 ms; a MESMA página sem a flag custa
    68-76 ms e a contagem `size:0` em filter context custa 170 ms frio / 1 ms
    morno. Num `_msearch` as duas viram 281 ms frio / ~110 ms morno, com o total
    exato preservado. Contar 4,4M docs é barato; contar E ordenar no mesmo
    request é que não é (a contagem exaustiva desliga o block-max WAND).
    """
    size = clamp_size(size)
    query, tem_texto, escopo = montar_query_processos(parte, advogado, filtros)
    ordenar_efetivo, sort = sort_processos(ordenar, tem_texto=tem_texto)

    body = {
        'size': size,
        'sort': sort,
        'query': query,
        'timeout': ES_QUERY_TIMEOUT,
    }
    if total_exato:
        # Sem isto o ES para de contar em 10.000 e devolve
        # {"value": 10000, "relation": "gte"} — a busca por "Instituto Nacional
        # do Seguro Social" dizia "10.000+" quando são 4.403.363 processos.
        # "10 mil ou mais" e "4,4 milhões" levam a decisões comerciais opostas.
        body['track_total_hits'] = True
    # NÃO mandar `track_scores` aqui. Ele era "explícito por clareza" e custava
    # 17×: medido em 13/08/2026 na busca do INSS (página de 20, sort por
    # _score), took = 68-76 ms sem flag nenhuma, 1.245-1.293 ms com
    # `track_scores`, 1.053-1.094 ms com `track_total_hits`, 1.293-1.376 ms com
    # as duas. Qualquer uma das duas desliga o block-max WAND (o ES passa a
    # pontuar TODOS os 4,4M em vez de pular o não-competitivo). Os 5 primeiros
    # hits e seus scores são idênticos com e sem a flag — ela não trazia nada.
    after = decode_cursor(cursor)
    if after:
        body['search_after'] = after
    return body, ordenar_efetivo, escopo


def body_contagem_processos(parte=None, advogado=None, filtros=None) -> dict:
    """Sub-busca de CONTAGEM (size 0, filter context, sem sort): total exato barato."""
    query, _tem_texto, _escopo = montar_query_processos(parte, advogado, filtros)
    # tudo em filter: contagem não precisa de score, e sem scoring o ES conta
    # por bitset (medido: 195 ms na busca do INSS, 1 ms morno)
    if 'bool' in query and query['bool'].get('must'):
        b = dict(query['bool'])
        b['filter'] = list(b.get('filter') or []) + list(b.pop('must'))
        query = {'bool': b}
    return {'size': 0, 'track_total_hits': True, 'query': query,
            'timeout': ES_QUERY_TIMEOUT}


def buscar_processos(parte=None, advogado=None, filtros=None, ordenar=None,
                     size=SIZE_DEFAULT, cursor=None) -> dict:
    """Busca de processos: texto (partes/advs) + filtros combinados + ordenação.

    Sem critério nenhum ainda é válido (browse ordenado por recência).
    """
    filtros = filtros or {}
    size = clamp_size(size)
    body, ordenar_efetivo, _escopo = montar_body_processos(
        parte, advogado, filtros, ordenar, size, cursor)

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
    """Ecoa filtros saneados + critérios textuais efetivamente aplicados.

    O `documento` sai enxuto: o dict interno carrega os termos de query (formas
    mascaradas, raiz, parciais LGPD) que são detalhe de implementação — o
    cliente quer saber o que ENTENDEMOS do que ele digitou.
    """
    out = dict(filtros)
    doc = out.get('documento')
    if isinstance(doc, dict):
        out['documento'] = {k: doc.get(k) for k in _DOC_ECO}
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
