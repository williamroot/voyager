"""Busca de processos para a TELA — envelope honesto sobre `search.busca_api`.

A v1 (`search/busca_api.py` + `api/busca_views.py`) é contrato de integração:
devolve o que casou e cala sobre o resto. Uma TELA não pode fazer isso. Buscar
por CPF e receber "nenhum resultado" faz o usuário concluir "essa pessoa não
tem processo" quando a verdade medida é "**0,14%** dos 71.308.806 processos têm
CPF/CNPJ indexado". Este módulo é a mesma busca — reusa parse/clauses/sort/
cursor de `busca_api`, sem duplicar uma linha de query — mais três coisas que
só a tela precisa:

1. **cobertura** de cada campo que a consulta usou, medida no índice e cacheada;
2. **contexto do documento**: quantos processos existem na raiz do CNPJ e
   quantos batem nas formas MASCARADAS por LGPD (`872.***.***-97`) — o que
   permite dizer "achei 0 exatos, mas 196 têm CPF mascarado começando em 038";
3. **avisos** legíveis, um por armadilha ativa na consulta.

--------------------------------------------------------------------------------
CONTRATO — GET /dashboard/api/busca/processos/   (login-gated)
--------------------------------------------------------------------------------
Parâmetros (todos opcionais; sem nenhum = browse por recência):

  q                 CAIXA ÚNICA. Detecta sozinho o que foi digitado (CNJ /
                    CPF / CNPJ / OAB / nome) e roteia. `interpretacao` no
                    payload diz o que entendeu. Um param explícito vence o `q`.
  cnj               CNJ exato, com ou sem máscara
  documento         CPF/CNPJ da parte (com máscara, sem, com espaço — tanto faz)
  documento_raiz    1 ⇒ CNPJ expande pra raiz (matriz + filiais)
  documento_parciais 1 ⇒ inclui documentos mascarados por LGPD compatíveis
  documento_forcar  1 ⇒ aceita DV inválido (0,014% do índice tem)
  parte             nome da parte (texto, 100% da base)
  parte_polo        ativo|passivo|outros  → move `parte` pro nested (1,9%)
  parte_papel       AUTOR|EXECUTADO|…     → idem
  advogado          nome do advogado (texto)
  oab               nº da OAB (`SP123456`, `123456/SP`, `sp 123.456` — tanto faz)
  vara              texto livre da vara/juízo (≥3 chars, acento/caixa-insensível)
  vara_exata        valor exato vindo do autocomplete (vence `vara`)
  tribunal uf ano_min ano_max classificacao codigo_classe assunto_codigo
  valor_min valor_max ultima_mov_gte/lte autuacao_gte/lte
  tem_sinal_precatorio tem_ente_publico_passivo segredo_justica enriquecido
  ordenar           relevancia|recente|valor_desc|valor_asc
  size              1..100 (default 20)      cursor  opaco (search_after)

Resposta 200:
{
  "versao": "ui-1",
  "total": {"value": 4403363, "relation": "eq"},
  "results": [ {...doc ES do processo...} ],
  "size": 20, "next_cursor": "..."|null, "ordenar": "relevancia",
  "filtros": {...ecoa o que foi de fato aplicado...},
  "interpretacao": {"origem":"q","campo":"documento","tipo":"cnpj",
                    "valor":"29.979.036/0001-40"} | null,
  "cobertura": {
     "total_processos": 71308806,
     "medido_em": "2026-08-13T12:00:00+00:00",
     "campos": {"<campo>": {"rotulo": "...", "docs": 102622, "pct": 0.14,
                            "usado": true}},
     "limitante": {"campo": "participacoes.documento", "rotulo": "...",
                   "docs": 102622, "pct": 0.14} | null
  },
  "documento": {"entrada": "...", "tipo": "cnpj", "mascarado": "...",
                "digitos": "...", "dv_valido": true, "raiz": "29979036",
                "aplicado": ["exato"],
                "alternativas": {"raiz": 27044, "parciais": 0}} | null,
  "avisos": [ {"codigo": "cobertura_documento", "mensagem": "..."} ],
  "levou_ms": 118
}

Erros (nunca 500 cru): 400 `{"erro": "<codigo>", "mensagem": "..."}` —
`cnj_invalido`, `documento_invalido`, `cpf_dv_invalido`, `cnpj_dv_invalido`,
`cursor_invalido`; 503 `{"erro": "elasticsearch_indisponivel"}`.

--------------------------------------------------------------------------------
DECISÕES (todas medidas no ES de prod em 13/08/2026)
--------------------------------------------------------------------------------
- **Página e contagem em `_msearch`, não numa query só.** Medido na busca do
  INSS (4,4M hits, página de 20 ordenada por _score):

      sem flag nenhuma ........................  68-76 ms
      + track_total_hits ...................... 1.053-1.094 ms
      + track_scores .......................... 1.245-1.293 ms
      as duas ................................. 1.293-1.376 ms

  Qualquer contagem/pontuação exaustiva desliga o block-max WAND e o ES pontua
  os 4,4M em vez de pular o não-competitivo. Então a página vai SEM flag e o
  total exato vem de uma 2ª sub-busca `size:0` em filter context (170 ms frio,
  1 ms morno). No `_msearch` isso dá **281 ms frio / ~110 ms morno** com o
  total ainda exato (4.403.363) — contra 1,4 s da query fundida.
- **Cobertura é cacheada (6 h), não recalculada por busca.** O `_msearch` das 6
  contagens custa 1.690 ms frio / 25 ms morno; o número muda em escala de dias
  (reindex), não de segundos. Se o ES falhar na hora de medir, devolvemos as
  constantes de `COBERTURA_FALLBACK` marcadas com `medido_em` da medição
  manual — número velho identificado vale mais que campo ausente.
- **Nada de expansão automática de documento.** Norma da casa: abster > chutar.
  Raiz de CNPJ e formas LGPD entram só se o cliente pedir; o que a resposta faz
  por conta própria é CONTAR as alternativas e oferecer ("achei 23.217 no CNPJ
  exato; 27.044 se contar as filiais").
"""
import datetime
import logging
import re

from search import busca_api as ba
from search.busca_api import BuscaParamError   # noqa: F401  (reexport p/ as views)

logger = logging.getLogger('voyager.busca_ui')

VERSAO_UI = 'ui-1'

#: TTL do cache de cobertura (segundos). O número anda com reindex, não com uso.
COBERTURA_TTL = 6 * 3600
COBERTURA_CACHE_KEY = 'busca_ui:cobertura:v1'

#: TTL do cache de resposta da busca (segundos) — janela curta só pra absorver
#: repique de F5/paginação de ida-e-volta sem envelhecer o acervo.
BUSCA_TTL = 60

#: TTL do autocomplete de vara: a lista de varas é praticamente estática.
VARAS_TTL = 15 * 60
VARAS_N_DEFAULT = 12
VARAS_N_MAX = 50

#: Cobertura medida à mão no ES de prod em 13/08/2026 — fallback quando a
#: medição ao vivo falha. Mentir por omissão é pior que mostrar número velho.
COBERTURA_FALLBACK = {
    'total': 71_441_064,
    'participacoes.documento': 102_622,
    'participacoes.nome': 1_553_522,
    'participacoes.oab': 183_531,
    'orgao_julgador': 17_891_683,
    'juizo': 1_299_995,
    # medidos por amostra de 1.000 docs em 15/08/2026 (±2,5pp)
    'partes': 14_573_977,      # 20,4%
    'advs': 13_288_038,        # 18,6%
    'amostrado': ['partes', 'advs'],
    'medido_em': '2026-08-15',
    'ao_vivo': False,
}

ROTULOS_CAMPO = {
    'participacoes.documento': 'documento da parte (CPF/CNPJ)',
    'participacoes.nome': 'nome estruturado da parte (com polo/papel)',
    'participacoes.oab': 'OAB do advogado',
    'orgao_julgador': 'órgão julgador / vara',
    'juizo': 'juízo',
    'partes': 'nomes das partes (texto)',
    'advs': 'nomes dos advogados (texto)',
    'proc': 'número CNJ',
}

#: campos com cobertura TOTAL — entram no payload pra tela poder dizer
#: "este critério vale pra base inteira" em vez de omitir.
#:
#: ⚠️ `partes` e `advs` SAÍRAM daqui em 15/08/2026. Eles eram dados como 100%
#: porque a medição usava `exists`, e o Elasticsearch trata **string vazia como
#: valor presente** — o mesmo erro que o `_nao_vazio` abaixo já corrigia pro
#: `orgao_julgador`, mas que não dá pra aplicar em campo `text` (`term: ''` não
#: casa nada num campo analisado, então o `must_not` não remove ninguém).
#: Medido por amostragem aleatória de 1.000 docs: `advs` 18,6% · `partes` 20,4%.
#: A consequência era concreta: quem buscava advogado via OAB ou nome recebia
#: uma fração dos processos com a tela afirmando cobertura total.
COBERTURA_TOTAL = ('proc',)

#: Campos `text` cuja cobertura só dá pra medir por AMOSTRA (ver acima).
#: 1.000 docs ⇒ margem de ~±2,5pp, que é irrelevante pra decisão que a tela
#: informa ("dá pra confiar neste critério?").
COBERTURA_AMOSTRADA = ('partes', 'advs')
COBERTURA_AMOSTRA_N = 250          # por semente
COBERTURA_AMOSTRA_SEEDS = (11, 22, 33, 44)
#: teto de espera da amostragem. Curto de propósito: é estimativa exibida num
#: rodapé, e o custo de errar pra mais é o site fora do ar (aconteceu).
AMOSTRA_TIMEOUT = 5


# --------------------------------------------------------------------------- #
# Cobertura do índice
# --------------------------------------------------------------------------- #
def _bodies_cobertura() -> list:
    def _nested(campo):
        return {'size': 0, 'track_total_hits': True,
                'query': {'nested': {'path': 'participacoes',
                                     'query': {'exists': {'field': campo}}}}}

    def _nao_vazio(campo):
        # `orgao_julgador` EXISTE em 100% dos docs e é "" em 74,9% deles: contar
        # `exists` mediria 100% e mentiria por construção.
        return {'size': 0, 'track_total_hits': True,
                'query': {'bool': {'must': [{'exists': {'field': campo}}],
                                   'must_not': [{'term': {campo: ''}}]}}}

    return [
        {'size': 0, 'track_total_hits': True, 'query': {'match_all': {}}},
        _nested('participacoes.documento'),
        _nested('participacoes.nome'),
        _nested('participacoes.oab'),
        _nao_vazio('orgao_julgador'),
        _nao_vazio('juizo'),
    ]


def _amostrar_texto(campos, total) -> dict:
    """Cobertura de campo `text` por amostra aleatória.

    ⚠️ ISTO DERRUBOU O SITE em 17/08/2026 (502 em loop). `function_score` com
    `random_score` sobre 71M docs custa segundos QUANDO O CLUSTER ESTÁ FOLGADO;
    com o ES em forcemerge de um índice de 985 GB, passou do timeout do gunicorn
    e o worker foi morto — repetidamente, porque a medição roda no caminho da
    requisição quando o cache está frio.

    Duas defesas agora: `request_timeout` curto (o dado é ESTIMATIVA — não vale
    um worker) e falha silenciosa que devolve o fallback. Cobertura é enfeite
    honesto na resposta; nunca pode ser o que derruba a busca.

    Não existe contagem exata barata: `exists` conta string vazia como valor, e
    `regexp: .+` num campo analisado percorre o dicionário de termos inteiro de
    71M docs. Amostrar 1.000 documentos custa 4 requisições e responde a mesma
    pergunta com margem de ±2,5pp.
    """
    cheios = {c: 0 for c in campos}
    vistos = 0
    for seed in COBERTURA_AMOSTRA_SEEDS:
        corpo = {
            'size': COBERTURA_AMOSTRA_N, '_source': list(campos),
            'query': {'function_score': {
                'query': {'match_all': {}},
                'random_score': {'seed': seed, 'field': '_seq_no'}}},
        }
        resp = _msearch([corpo], request_timeout=AMOSTRA_TIMEOUT)[0]
        for h in resp.get('hits', {}).get('hits', []):
            vistos += 1
            for c in campos:
                if (h.get('_source', {}).get(c) or '').strip():
                    cheios[c] += 1
    if not vistos:
        return {}
    return {c: int(total * cheios[c] / vistos) for c in campos}


def medir_cobertura() -> dict:
    """Mede a cobertura no índice: contagem exata onde dá, amostra onde não dá."""
    respostas = _msearch(_bodies_cobertura())
    valores = [r.get('hits', {}).get('total', {}).get('value', 0) for r in respostas]
    total, doc, nome, oab, orgao, juizo = valores
    medida = {
        'total': total,
        'participacoes.documento': doc,
        'participacoes.nome': nome,
        'participacoes.oab': oab,
        'orgao_julgador': orgao,
        'juizo': juizo,
        'medido_em': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'ao_vivo': True,
    }
    try:
        medida.update(_amostrar_texto(COBERTURA_AMOSTRADA, total))
        medida['amostrado'] = list(COBERTURA_AMOSTRADA)
    except Exception:  # noqa: BLE001 — cobertura NUNCA pode derrubar a busca
        # Cai no número medido à mão em vez de omitir o campo: a tela precisa
        # dizer alguma coisa, e "número velho identificado" vale mais que
        # silêncio (mesma regra do COBERTURA_FALLBACK).
        logger.warning('cobertura: amostragem de campo texto falhou — usando fallback')
        for c in COBERTURA_AMOSTRADA:
            medida.setdefault(c, COBERTURA_FALLBACK.get(c))
        medida['amostrado'] = list(COBERTURA_AMOSTRADA)
        medida['amostra_degradada'] = True
    return medida


def cobertura_indice(forcar=False) -> dict:
    """Cobertura cacheada por `COBERTURA_TTL`. ES fora ⇒ fallback medido à mão."""
    cache = _cache()
    if cache is not None and not forcar:
        try:
            em_cache = cache.get(COBERTURA_CACHE_KEY)
        except Exception:                       # Redis fora não derruba a busca
            em_cache = None
        if em_cache:
            return em_cache
    try:
        cob = medir_cobertura()
    except Exception:
        logger.warning('busca_ui: falha ao medir cobertura, usando fallback',
                       exc_info=True)
        return dict(COBERTURA_FALLBACK)
    if cache is not None:
        try:
            cache.set(COBERTURA_CACHE_KEY, cob, COBERTURA_TTL)
        except Exception:
            pass
    return cob


def _pct(parte, total):
    return round(100.0 * parte / total, 3) if total else None


def montar_cobertura(campos_usados) -> dict:
    """Bloco `cobertura` da resposta: só os campos que a consulta de fato usou.

    `limitante` é o campo de MENOR cobertura entre os usados — é ele que define
    o universo real da busca, e é a frase que a tela precisa escrever.
    """
    cob = cobertura_indice()
    total = cob.get('total') or 0
    campos = {}
    for campo in campos_usados:
        if campo in COBERTURA_TOTAL:
            docs = total
        elif campo in cob:
            docs = cob[campo]
        else:
            continue
        campos[campo] = {
            'rotulo': ROTULOS_CAMPO.get(campo, campo),
            'docs': docs,
            'pct': _pct(docs, total),
            'usado': True,
            # número de amostra ≠ contagem: a tela pode escrever "≈" e não
            # prometer precisão que a medição não tem
            'estimado': campo in (cob.get('amostrado') or ()),
        }
    limitante = None
    if campos:
        nome, dados = min(campos.items(), key=lambda kv: kv[1]['docs'])
        if dados['docs'] < total:
            limitante = {'campo': nome, **{k: dados[k]
                                           for k in ('rotulo', 'docs', 'pct')}}
    return {
        'total_processos': total,
        'medido_em': cob.get('medido_em'),
        'ao_vivo': cob.get('ao_vivo', False),
        'campos': campos,
        'limitante': limitante,
    }


# --------------------------------------------------------------------------- #
# Interpretação da caixa única (`q`)
# --------------------------------------------------------------------------- #
_RE_SO_DIGITOS = re.compile(r'^[\d\s.\-/]+$')
_RE_OAB = re.compile(r'^\s*(?:oab[\s:/-]*)?([A-Za-z]{2})[\s.\-/]*(\d{2,6}[A-Za-z]?)\s*$'
                     r'|^\s*(?:oab[\s:/-]*)?(\d{2,6}[A-Za-z]?)[\s.\-/]*([A-Za-z]{2})\s*$',
                     re.IGNORECASE)


def classificar_termo(q: str) -> dict:
    """Caixa única → (campo, valor). Só decide pelo FORMATO, nunca por palpite.

    20 dígitos = CNJ. 11 ou 14 = CPF/CNPJ. 8 = raiz de CNPJ. "OAB SP 123456" ou
    "123456/SP" = OAB. Qualquer outra coisa = nome de parte (o campo texto, que
    cobre 100% da base — o default seguro).
    """
    s = (q or '').strip()
    if not s:
        return {'campo': None, 'valor': None}
    if _RE_SO_DIGITOS.match(s):
        n = len(re.sub(r'\D', '', s))
        if n == 20:
            return {'campo': 'cnj', 'valor': s, 'tipo': 'cnj'}
        if n in (11, 14, 8):
            return {'campo': 'documento', 'valor': s,
                    'tipo': {11: 'cpf', 14: 'cnpj', 8: 'raiz_cnpj'}[n]}
        return {'campo': 'parte', 'valor': s, 'tipo': 'numero_desconhecido'}
    m = _RE_OAB.match(s)
    if m:
        oab = ba.normalizar_oab(s.lower().replace('oab', ''))
        if oab:
            return {'campo': 'oab', 'valor': oab, 'tipo': 'oab'}
    return {'campo': 'parte', 'valor': s, 'tipo': 'nome'}


def aplicar_q(qd) -> dict:
    """Explode `q` num dict de params-equivalentes. Param explícito vence o `q`."""
    q = (qd.get('q') or '').strip()
    if not q:
        return {}
    interp = classificar_termo(q)
    campo = interp.get('campo')
    if not campo or (qd.get(campo) or '').strip():
        return {}
    return {'campo': campo, 'valor': interp['valor'], 'tipo': interp.get('tipo'),
            'origem': 'q', 'termo': q}


class _GetComQ:
    """`request.GET` + o que o `q` inferiu, sem mutar o QueryDict do Django."""

    def __init__(self, qd, extra):
        self._qd = qd
        self._extra = extra

    def get(self, chave, default=None):
        v = self._qd.get(chave, None)
        if v not in (None, ''):
            return v
        if chave in self._extra:
            return self._extra[chave]
        return default


# --------------------------------------------------------------------------- #
# Contexto do documento — o que existe ALÉM do casamento exato
# --------------------------------------------------------------------------- #
def contexto_documento(doc: dict, filtros: dict) -> dict:
    """Conta as ALTERNATIVAS do documento buscado (raiz do CNPJ, formas LGPD).

    Não muda o resultado — informa. `1 _msearch`, 2 contagens `size:0` (medido:
    27 ms frio). Só roda quando a alternativa NÃO está aplicada: se o cliente já
    pediu `documento_raiz`, contar a raiz de novo seria repetir o próprio total.
    """
    bodies, chaves = [], []

    def _conta(inner):
        return {'size': 0, 'track_total_hits': True,
                'query': {'nested': {'path': 'participacoes', 'query': inner}},
                'timeout': ba.ES_QUERY_TIMEOUT}

    if doc.get('raiz_prefixo') and not filtros.get('documento_raiz') \
            and doc['tipo'] != 'raiz_cnpj':
        chaves.append('raiz')
        bodies.append(_conta({'prefix': {'participacoes.documento':
                                         doc['raiz_prefixo']}}))
    if doc.get('termos_parciais') and not filtros.get('documento_parciais'):
        chaves.append('parciais')
        bodies.append(_conta({'terms': {'participacoes.documento':
                                        doc['termos_parciais']}}))
    if not bodies:
        return {}
    try:
        respostas = _msearch(bodies)
    except Exception:
        logger.warning('busca_ui: contexto do documento indisponível', exc_info=True)
        return {}
    return {k: r.get('hits', {}).get('total', {}).get('value', 0)
            for k, r in zip(chaves, respostas)}


# --------------------------------------------------------------------------- #
# Autocomplete de vara
# --------------------------------------------------------------------------- #
def sugerir_varas(termo: str, n=VARAS_N_DEFAULT) -> dict:
    """Varas existentes que casam `termo`, com nº de processos em cada uma.

    Por que autocomplete e não campo livre solto: `orgao_julgador` é keyword
    crua com 29.488 valores distintos e grafias incompatíveis entre tribunais
    ("VARA DE EXECUCAO FISCAL…" no TRF1, "1ª Vara de Execução Fiscal do DF" no
    TJDFT). Quem digita escolhe UMA e filtra por `vara_exata`, sem loteria.

    Custo medido: o `terms` agg SOLTO (sem filtro) sobre os 17,9M docs com
    órgão custa **5,1 s** — inviável por tecla. Com o `regexp` na frente cai pra
    **73 ms frio / 13 ms morno**, porque o agg só visita o que casou.
    """
    termo = ' '.join((termo or '').split())[:120]
    n = max(1, min(int(n or VARAS_N_DEFAULT), VARAS_N_MAX))
    base = {'termo': termo, 'min_caracteres': ba.VARA_MIN_CARACTERES,
            'itens': [], 'buscou': False, 'motivo': None}
    if len(termo) < ba.VARA_MIN_CARACTERES:
        return {**base, 'motivo': 'termo_curto' if termo else 'sem_termo'}

    clausula = ba.clausula_vara(termo)
    if not clausula:
        return {**base, 'motivo': 'sem_termo'}

    body = {
        'size': 0,
        'query': {'bool': {'filter': [clausula]}},
        'aggs': {campo: {'terms': {'field': campo, 'size': n}}
                 for campo in ba.VARA_CAMPOS},
        'timeout': ba.ES_QUERY_TIMEOUT,
    }
    resp = ba._executar('processos', body)
    itens = []
    for campo in ba.VARA_CAMPOS:
        for b in resp.get('aggregations', {}).get(campo, {}).get('buckets', []):
            if not b['key']:                 # a string vazia nunca é uma vara
                continue
            itens.append({'valor': b['key'], 'campo': campo,
                          'processos': b['doc_count']})
    itens.sort(key=lambda i: -i['processos'])
    cob = cobertura_indice()
    return {
        **base,
        'buscou': True,
        'itens': itens[:n],
        'levou_ms': resp.get('took'),
        # a tela precisa saber que escolher uma vara não varre a base inteira
        'cobertura': {campo: {'rotulo': ROTULOS_CAMPO[campo],
                              'docs': cob.get(campo),
                              'pct': _pct(cob.get(campo), cob.get('total'))}
                      for campo in ba.VARA_CAMPOS},
    }


# --------------------------------------------------------------------------- #
# Avisos — a leitura humana da cobertura
# --------------------------------------------------------------------------- #
def _fmt(n):
    return f'{n:,}'.replace(',', '.') if isinstance(n, int) else str(n)


def montar_avisos(filtros, cobertura, doc_info, parte_escopo, total) -> list:
    """Um aviso por armadilha ATIVA. Vazio quando não há nada a ressalvar."""
    avisos = []
    campos = cobertura.get('campos') or {}
    zero = (total or 0) == 0

    doc_cob = campos.get('participacoes.documento')
    if doc_cob:
        avisos.append({
            'codigo': 'cobertura_documento',
            'mensagem': (
                f'Busca por CPF/CNPJ varre os {_fmt(doc_cob["docs"])} processos '
                f'({doc_cob["pct"]}% da base) em que o documento da parte está '
                f'indexado.' + (' Zero resultados aqui NÃO significa que a pessoa '
                                'não tem processo.' if zero else '')),
        })
    oab_cob = campos.get('participacoes.oab')
    if oab_cob:
        avisos.append({
            'codigo': 'cobertura_oab',
            'mensagem': (
                f'A OAB estruturada existe em {_fmt(oab_cob["docs"])} processos '
                f'({oab_cob["pct"]}%). Buscar o advogado pelo NOME cobre 100% '
                f'da base.'),
        })
    if filtros.get('vara') or filtros.get('vara_exata'):
        orgao = campos.get('orgao_julgador') or {}
        juizo = campos.get('juizo') or {}
        avisos.append({
            'codigo': 'cobertura_vara',
            'mensagem': (
                f'Filtrar por vara alcança {_fmt(orgao.get("docs", 0))} processos '
                f'({orgao.get("pct")}%) pelo órgão julgador e '
                f'{_fmt(juizo.get("docs", 0))} ({juizo.get("pct")}%) pelo juízo. '
                f'Nos demais o campo veio vazio do tribunal.'),
        })
    if parte_escopo == 'nested':
        nome = campos.get('participacoes.nome') or {}
        avisos.append({
            'codigo': 'parte_estruturada',
            'mensagem': (
                f'Pedir polo/papel move a busca do nome pro cadastro estruturado, '
                f'que cobre {_fmt(nome.get("docs", 0))} processos '
                f'({nome.get("pct")}%). Sem polo, o nome é buscado no texto das '
                f'partes, que cobre 100%.'),
        })
    if doc_info:
        alt = doc_info.get('alternativas') or {}
        if alt.get('raiz'):
            avisos.append({
                'codigo': 'documento_raiz_disponivel',
                'mensagem': (
                    f'Somando matriz e filiais (raiz {doc_info["raiz"]}) são '
                    f'{_fmt(alt["raiz"])} processos. Use documento_raiz=1.'),
            })
        if alt.get('parciais'):
            avisos.append({
                'codigo': 'documento_parciais_disponiveis',
                'mensagem': (
                    f'Há {_fmt(alt["parciais"])} processos com documento '
                    f'MASCARADO por sigilo compatível com o que você digitou — '
                    f'indício, não prova de identidade. Use documento_parciais=1.'),
            })
        if doc_info.get('dv_valido') is False:
            avisos.append({
                'codigo': 'documento_dv_invalido',
                'mensagem': 'O dígito verificador não fecha; busca feita mesmo '
                            'assim porque documento_forcar=1 foi pedido.',
            })
    return avisos


# --------------------------------------------------------------------------- #
# A busca da tela
# --------------------------------------------------------------------------- #
def _campos_usados(parte, advogado, filtros, parte_escopo) -> list:
    campos = []
    if filtros.get('cnj'):
        campos.append('proc')
    if filtros.get('documento'):
        campos.append('participacoes.documento')
    if parte:
        campos.append('participacoes.nome' if parte_escopo == 'nested' else 'partes')
    elif filtros.get('parte_polo') or filtros.get('parte_papel'):
        campos.append('participacoes.nome')
    if advogado:
        campos.append('advs')
    if filtros.get('oab'):
        campos.append('participacoes.oab')
    if filtros.get('vara') or filtros.get('vara_exata'):
        campos.extend(('orgao_julgador', 'juizo'))
    return campos


def buscar_processos_ui(parte=None, advogado=None, filtros=None, ordenar=None,
                        size=None, cursor=None, interpretacao=None) -> dict:
    """Busca da tela: página + total exato num `_msearch` + cobertura + avisos."""
    filtros = filtros or {}
    size = ba.clamp_size(size)

    body_pagina, ordenar_efetivo, parte_escopo = ba.montar_body_processos(
        parte, advogado, filtros, ordenar, size, cursor, total_exato=False)
    body_total = ba.body_contagem_processos(parte, advogado, filtros)

    respostas = _msearch([body_pagina, body_total])
    pagina = ba._montar_pagina(respostas[0], size)
    total = respostas[1].get('hits', {}).get('total', {}) or {}
    pagina['total'] = {'value': total.get('value', 0),
                       'relation': total.get('relation', 'eq')}
    levou = sum(r.get('took') or 0 for r in respostas)

    doc = filtros.get('documento')
    doc_info = None
    if doc:
        doc_info = {k: doc.get(k) for k in ba._DOC_ECO}
        doc_info['aplicado'] = ['exato'] if doc['termos_exatos'] else []
        if filtros.get('documento_raiz') or doc['tipo'] == 'raiz_cnpj':
            doc_info['aplicado'].append('raiz')
        if filtros.get('documento_parciais'):
            doc_info['aplicado'].append('parciais')
        doc_info['alternativas'] = contexto_documento(doc, filtros)

    cobertura = montar_cobertura(
        _campos_usados(parte, advogado, filtros, parte_escopo))

    pagina.update({
        'versao': VERSAO_UI,
        'size': size,
        'ordenar': ordenar_efetivo,
        'filtros': ba._ecoar_filtros(filtros, parte=parte, advogado=advogado),
        'interpretacao': interpretacao,
        'cobertura': cobertura,
        'documento': doc_info,
        'avisos': montar_avisos(filtros, cobertura, doc_info, parte_escopo,
                                pagina['total']['value']),
        'levou_ms': levou,
    })
    return pagina


def buscar_da_querystring(qd) -> dict:
    """`request.GET` → busca completa. Levanta BuscaParamError (⇒ 400) na entrada."""
    inferido = aplicar_q(qd)
    g = _GetComQ(qd, {inferido['campo']: inferido['valor']} if inferido else {})
    filtros = ba.parse_filtros_processos(g)
    interpretacao = None
    if inferido:
        interpretacao = {'origem': 'q', 'termo': inferido['termo'],
                         'campo': inferido['campo'], 'tipo': inferido.get('tipo'),
                         'valor': inferido['valor']}
    return buscar_processos_ui(
        parte=(g.get('parte') or '').strip() or None,
        advogado=(g.get('advogado') or '').strip() or None,
        filtros=filtros,
        ordenar=g.get('ordenar'),
        size=g.get('size'),
        cursor=g.get('cursor'),
        interpretacao=interpretacao,
    )


# --------------------------------------------------------------------------- #
# Wrappers lazy (mockáveis nos testes, mesmo padrão de busca_api)
# --------------------------------------------------------------------------- #
def _msearch(bodies: list, request_timeout=None) -> list:
    """1 round-trip HTTP, N sub-buscas em paralelo no ES (padrão do agg_estado).

    `request_timeout` existe pra quem pode DESISTIR: a amostragem de cobertura
    é estimativa de rodapé e não vale segurar um worker de gunicorn até ele ser
    morto — foi assim que o site caiu em 17/08/2026."""
    es = ba.get_es()
    payload: list = []
    for b in bodies:
        payload.append({})
        payload.append(b)
    kw = {'request_timeout': request_timeout} if request_timeout else {}
    resp = es.msearch(index=ba.index_name('processos'), body=payload, **kw)
    if hasattr(resp, 'body'):
        resp = dict(resp.body)
    respostas = resp.get('responses') or []
    for r in respostas:
        if r.get('error'):
            raise RuntimeError(f'ES msearch: {r["error"]}')
    if len(respostas) != len(bodies):
        raise RuntimeError('ES msearch: resposta incompleta')
    return respostas


def _cache():
    try:
        from django.core.cache import cache
        return cache
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Busca NO CONTEÚDO das publicações
# --------------------------------------------------------------------------- #
#: O texto sempre esteve indexado (`body`, analisado com português+asciifolding)
#: e servido pela API externa — mas a TELA nunca o consultou: ela só olha o
#: índice de processos. Medido em 15/08/2026 no índice de 1,16 bilhão de
#: publicações:
#:
#:     "poder judiciário"  94.065.570 docs
#:     "sentença"          61.747.787
#:     "precatório"         3.086.016
#:
#: Ou seja, havia um acervo de texto inteiro fora do alcance de quem usa o
#: sistema. Isto aqui só abre a porta que já existia.
#:
#: A composição do que está lá dentro (amostra de 1.000 publicações) importa pra
#: tela não prometer o que não tem:
#:     82,9%  rótulo de andamento (≤120 caracteres: "Conclusão · para despacho")
#:     13,0%  publicação de verdade (>600)
#:      3,6%  peça longa (>3.000)
#: Buscar "sentença" acha o texto da sentença publicada — não o PDF dela, que
#: temos como LINK em 15,5% dos casos e nunca baixamos.
CONTEUDO_TTL = 60


def buscar_conteudo_ui(q=None, filtros=None, ordenar=None, size=None, cursor=None):
    """Full-text nas publicações, com o mesmo envelope honesto da busca de processos.

    Diferente de `buscar_processos_ui`, aqui a unidade do resultado é a
    PUBLICAÇÃO (com trecho destacado), não o processo — é o que permite
    responder "onde apareceu esta palavra".
    """
    filtros = filtros or {}
    pagina = ba.buscar_movimentacoes(q=q, filtros=filtros, ordenar=ordenar,
                                     size=size or 20, cursor=cursor)
    pagina['cobertura'] = {
        'aviso': ('A maior parte do acervo são rótulos de andamento curtos; '
                  'o texto integral de peças aparece em parte das publicações.'),
    }
    return pagina


def buscar_conteudo_da_querystring(qs):
    """Params: q (texto), tribunal, proc, publicado_gte/lte, ordenar, size, cursor."""
    filtros = ba.parse_filtros_movs(qs)
    return buscar_conteudo_ui(
        q=(qs.get('q') or '').strip(),
        filtros=filtros,
        ordenar=qs.get('ordenar'),
        size=qs.get('size'),
        cursor=qs.get('cursor'),
    )
