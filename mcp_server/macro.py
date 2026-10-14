"""Tools MACRO do MCP: panorama do acervo e perguntas sobre a base.

As tools de `delegates.py` respondem sobre UM processo. Estas respondem sobre a
BASE: quanto temos, de onde, contra o que o CNJ declara, e qualquer contagem/soma
que caiba numa agregação do Elasticsearch.

Tudo lê do ES (nunca do Postgres, que é o gargalo de prod) e tudo tem teto de
espera (`timeout` no corpo + `request_timeout` no client): uma tool de agente
roda no MESMO gunicorn do site.

Contrato de honestidade, herdado do CLAUDE.md:
- `total` vem com `track_total_hits` — nunca o piso de 10.000 do ES;
- `parcial: true` quando o ES estourou o próprio timeout (resultado incompleto);
- `outros` = documentos fora do top-N do agrupamento, pra a resposta não
  parecer o universo quando é só o topo.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('voyager.mcp.macro')

#: teto de espera de uma agregação (lado ES) e da requisição (lado client).
ES_TIMEOUT = '20s'
REQUEST_TIMEOUT = 30

#: `visao_geral` custa ~15s a frio (acervo CNJ: 350M docs) — medido 14/10/2026.
VISAO_GERAL_TTL = 3600
VISAO_GERAL_CACHE = 'mcp:visao_geral:v1'

TOP_DEFAULT = 20
TOP_MAX = 200
TOP_MAX_SUB = 50
HISTOGRAMA_MAX = 400

#: Campos que `agregar` aceita, por índice. Lista FECHADA: campo `text` não
#: agrega (e o `exists` dele mente — ver CLAUDE.md), então só keyword/número/
#: booleano/data entram aqui. `texto` filtra pelos campos de texto à parte.
INDICES = {
    'processos': {
        'sufixo': 'processos',
        'descricao': 'Processos que conhecemos em detalhe (DJEN + enriquecimento nos '
                     'tribunais): partes, valor da causa, classificação de lead.',
        'keyword': {
            'tribunal', 'uf', 'classificacao', 'classificacao_versao', 'classe_nome',
            'classe_cnj_nome', 'classe_cnj_codigo', 'codigo_classe', 'assunto_codigo',
            'grau', 'fase_nome', 'fase_codigo', 'orgao_julgador', 'juizo',
            'enriquecimento_status', 'segredo_justica_estado', 'proc',
        },
        'numero': {'ano_cnj', 'valor_causa', 'total_movimentacoes', 'classificacao_score', 'source'},
        'booleano': {'tem_sinal_precatorio', 'tem_ente_publico_passivo', 'segredo_justica', 'enriquecido'},
        'data': {'data_autuacao', 'primeira_movimentacao_em', 'ultima_movimentacao_em',
                 'inserido_em', 'classificacao_em', 'enriquecido_em', 'fase_em'},
        'texto': ('partes', 'advs', 'assunto'),
        'valor': 'valor_causa',
    },
    'acervo': {
        'sufixo': 'acervo',
        'descricao': 'Acervo DECLARADO ao CNJ (varredura do Datajud): o esqueleto nacional. '
                     'Classe, órgão, datas — SEM partes e SEM valor (o Datajud não expõe).',
        'keyword': {'tribunal', 'uf', 'classe_codigo', 'classe_nome', 'grau', 'formato',
                    'sistema', 'orgao_codigo', 'orgao_nome', 'municipio_ibge',
                    'assunto_codigos', 'proc'},
        'numero': {'ano_cnj', 'sigilo'},
        'booleano': {'no_acervo'},
        'data': {'ajuizado_em', 'atualizado_em', 'varrido_em'},
        'texto': ('assunto_nomes',),
        'valor': None,
    },
    'publicacoes': {
        'sufixo': 'movimentacoes',
        'descricao': 'Publicações em diários (DJEN, DJE próprios, DEJT, STF...): ~1,8 bilhão. '
                     'Agregar sem filtro de tribunal/data leva 5-15s.',
        'keyword': {'tribunal', 'tipo_comunicacao', 'tipo_documento', 'classe_nome',
                    'codigo_classe', 'nome_orgao', 'secao_diario', 'periodico_diario_slug',
                    'periodico_orgao_slug', 'periodico_caderno_slug', 'proc', 'oabs',
                    'cnjs_citados'},
        'numero': {'source', 'valores_citados'},
        'booleano': {'ativo'},
        'data': {'publish_date', 'available_at', 'detected_at'},
        'texto': ('body',),
        'valor': 'valores_citados',
    },
}

METRICAS = ('contagem', 'soma', 'media', 'mediana', 'min', 'max')
INTERVALOS = {'ano': 'year', 'mes': 'month', 'dia': 'day'}
ORDENACOES = ('n', 'valor', 'chave')

CLASSIFICACOES = {
    'PRECATORIO': 'precatório expedido/confirmado',
    'PRE_PRECATORIO': 'a caminho de virar precatório',
    'DIREITO_CREDITORIO': 'crédito contra ente público ainda sem requisitório',
    'NAO_LEAD': 'sem crédito aproveitável',
}


class MacroParamError(Exception):
    """Pedido inválido — vira `{'error': ...}` com a lista do que é aceito."""


# --------------------------------------------------------------------------- #
# ES
# --------------------------------------------------------------------------- #
def _es():
    from search.client import get_es
    return get_es()


def _indice(nome: str) -> str:
    from search.client import index_name
    return index_name(INDICES[nome]['sufixo'])


def _search(nome_ou_indice: str, body: dict) -> dict:
    indice = _indice(nome_ou_indice) if nome_ou_indice in INDICES else nome_ou_indice
    resp = _es().search(index=indice, body=body, request_timeout=REQUEST_TIMEOUT)
    return dict(resp.body) if hasattr(resp, 'body') else dict(resp)


def _total(resp: dict) -> int:
    t = (resp.get('hits') or {}).get('total') or {}
    return t.get('value', 0) if isinstance(t, dict) else int(t or 0)


# --------------------------------------------------------------------------- #
# agregar — a tool de perguntas
# --------------------------------------------------------------------------- #
def _campos(cfg: dict) -> set:
    return cfg['keyword'] | cfg['numero'] | cfg['booleano'] | cfg['data']


def _valida_campo(cfg: dict, campo: str, papel: str) -> str:
    if campo not in _campos(cfg):
        raise MacroParamError(
            f'{papel}: campo "{campo}" não existe ou não agrega neste índice. '
            f'Aceitos: {sorted(_campos(cfg))}')
    return campo


def _clausulas_filtro(cfg: dict, filtros: dict | None, faixas: dict | None) -> list:
    out = []
    for campo, valor in (filtros or {}).items():
        _valida_campo(cfg, campo, 'filtros')
        if isinstance(valor, list):
            out.append({'terms': {campo: valor}})
        elif valor is None:
            out.append({'bool': {'must_not': {'exists': {'field': campo}}}})
        elif campo in ('tribunal', 'uf', 'classificacao') and isinstance(valor, str):
            out.append({'term': {campo: valor.strip().upper()}})
        else:
            out.append({'term': {campo: valor}})
    for campo, faixa in (faixas or {}).items():
        _valida_campo(cfg, campo, 'faixas')
        if not isinstance(faixa, dict) or not set(faixa) <= {'gte', 'lte', 'gt', 'lt'}:
            raise MacroParamError(f'faixas.{campo}: use {{"gte": ..., "lte": ...}}')
        out.append({'range': {campo: faixa}})
    return out


def _clausulas_texto(cfg: dict, texto: str | None) -> list:
    texto = (texto or '').strip()
    if not texto:
        return []
    from search.busca_api import clausulas_texto
    campos = cfg['texto']
    if len(campos) == 1:
        return clausulas_texto(campos[0], texto)
    # Vários campos de texto: o termo pode estar em qualquer um deles.
    return [{'bool': {'should': [{'bool': {'must': clausulas_texto(c, texto)}} for c in campos],
                      'minimum_should_match': 1}}]


def _agg_metrica(cfg: dict, metrica: str) -> dict | None:
    if metrica == 'contagem':
        return None
    campo = cfg['valor']
    if not campo:
        raise MacroParamError('este índice não tem campo de valor — só metrica="contagem"')
    if metrica == 'mediana':
        return {'percentiles': {'field': campo, 'percents': [50]}}
    return {{'soma': 'sum', 'media': 'avg', 'min': 'min', 'max': 'max'}[metrica]: {'field': campo}}


def _valor_metrica(no: dict, metrica: str):
    m = no.get('m')
    if not m:
        return None
    if metrica == 'mediana':
        return (m.get('values') or {}).get('50.0')
    return m.get('value')


def _agg_grupo(cfg: dict, campo: str, intervalo: str | None, top: int, ordenar: str,
               metrica: str) -> dict:
    if campo in cfg['data']:
        intervalo = intervalo or 'ano'
        if intervalo not in INTERVALOS:
            raise MacroParamError(f'intervalo deve ser um de {list(INTERVALOS)}')
        return {'date_histogram': {'field': campo, 'calendar_interval': INTERVALOS[intervalo],
                                   'min_doc_count': 1, 'format': 'yyyy-MM-dd'}}
    ordem = {'n': {'_count': 'desc'}, 'chave': {'_key': 'asc'}}.get(ordenar)
    if ordenar == 'valor':
        if metrica == 'contagem':
            raise MacroParamError('ordenar="valor" exige uma metrica de valor')
        ordem = {'m.50' if metrica == 'mediana' else 'm': 'desc'}
    terms = {'field': campo, 'size': top, 'order': ordem}
    if campo in cfg['keyword']:
        # documentos SEM o campo viram um grupo próprio (chave null), em vez de sumir
        terms['missing'] = '__vazio__'
    return {'terms': terms}


def _ler_buckets(agg: dict, metrica: str, nivel2: bool, avisos: list, campo: str) -> list:
    buckets = agg.get('buckets') or []
    if len(buckets) > HISTOGRAMA_MAX:
        avisos.append(f'{campo}: {len(buckets)} intervalos — devolvendo os {HISTOGRAMA_MAX} '
                      'mais recentes; use faixas ou um intervalo maior pra ver o resto')
        buckets = buckets[-HISTOGRAMA_MAX:]
    out = []
    for b in buckets:
        chave = b.get('key_as_string', b.get('key'))
        item = {'chave': None if chave == '__vazio__' else chave, 'n': b.get('doc_count', 0)}
        v = _valor_metrica(b, metrica)
        if v is not None:
            item['valor'] = v
        if nivel2 and 'g2' in b:
            item['sub'] = _ler_buckets(b['g2'], metrica, False, avisos, campo)
            if b['g2'].get('sum_other_doc_count'):
                item['sub_outros'] = b['g2']['sum_other_doc_count']
        out.append(item)
    return out


def agregar(indice='processos', agrupar_por=None, filtros=None, faixas=None, texto=None,
            metrica='contagem', intervalo=None, top=TOP_DEFAULT, ordenar='n'):
    """Contagem/soma/média agrupada com filtros — responde "quantos/quanto ... por ...".

    `agrupar_por`: 0 a 2 campos (o 2º vira sub-agrupamento de cada grupo do 1º).
    Campo de data vira histograma (`intervalo` = ano|mes|dia).
    """
    try:
        if indice not in INDICES:
            raise MacroParamError(f'indice deve ser um de {list(INDICES)}')
        cfg = INDICES[indice]
        if metrica not in METRICAS:
            raise MacroParamError(f'metrica deve ser uma de {list(METRICAS)}')
        if ordenar not in ORDENACOES:
            raise MacroParamError(f'ordenar deve ser um de {list(ORDENACOES)}')
        if isinstance(agrupar_por, str):
            agrupar_por = [agrupar_por]
        agrupar_por = list(agrupar_por or [])
        if len(agrupar_por) > 2:
            raise MacroParamError('agrupar_por aceita no máximo 2 campos')
        for c in agrupar_por:
            _valida_campo(cfg, c, 'agrupar_por')
        try:
            top = max(1, min(int(top), TOP_MAX))
        except (TypeError, ValueError):
            top = TOP_DEFAULT

        clausulas = _clausulas_filtro(cfg, filtros, faixas)
        must = _clausulas_texto(cfg, texto)
        query = {'bool': {'filter': clausulas, 'must': must}} if (clausulas or must) else {'match_all': {}}

        body = {'size': 0, 'track_total_hits': True, 'timeout': ES_TIMEOUT, 'query': query, 'aggs': {}}
        m = _agg_metrica(cfg, metrica)
        if m:
            body['aggs']['m'] = m
        if agrupar_por:
            g1 = _agg_grupo(cfg, agrupar_por[0], intervalo, top, ordenar, metrica)
            sub = {}
            if m:
                sub['m'] = m
            if len(agrupar_por) == 2:
                g2 = _agg_grupo(cfg, agrupar_por[1], intervalo, min(top, TOP_MAX_SUB), ordenar, metrica)
                if m:
                    g2['aggs'] = {'m': m}
                sub['g2'] = g2
            if sub:
                g1['aggs'] = sub
            body['aggs']['g1'] = g1
        if not body['aggs']:
            del body['aggs']

        resp = _search(indice, body)
    except MacroParamError as e:
        return {'error': str(e)}
    except Exception as e:
        logger.error('MCP agregar erro: %s', e)
        return {'error': f'{type(e).__name__}: {e}'}

    aggs = resp.get('aggregations') or {}
    avisos: list = []
    out = {
        'indice': indice,
        'total': _total(resp),
        'parcial': bool(resp.get('timed_out')),
        'took_ms': resp.get('took'),
        'metrica': metrica,
    }
    if 'm' in aggs:
        out['valor'] = _valor_metrica(aggs, metrica)
    if 'g1' in aggs:
        out['agrupado_por'] = agrupar_por
        out['grupos'] = _ler_buckets(aggs['g1'], metrica, len(agrupar_por) == 2, avisos, agrupar_por[0])
        outros = aggs['g1'].get('sum_other_doc_count')
        if outros:
            out['outros'] = outros
            avisos.append(f'{outros} documentos ficaram fora do top {top} de "{agrupar_por[0]}"')
        if aggs['g1'].get('doc_count_error_upper_bound'):
            out['erro_max_contagem'] = aggs['g1']['doc_count_error_upper_bound']
    if out['parcial']:
        avisos.append(f'o ES estourou o timeout de {ES_TIMEOUT}: números INCOMPLETOS — '
                      'estreite os filtros (tribunal, faixa de data) e repita')
    if avisos:
        out['avisos'] = avisos
    return out


# --------------------------------------------------------------------------- #
# visao_geral — o panorama
# --------------------------------------------------------------------------- #
def _por_tribunal(resp: dict) -> dict:
    return {b['key']: b['doc_count']
            for b in ((resp.get('aggregations') or {}).get('t') or {}).get('buckets', [])}


def _calcular_visao_geral() -> dict:
    from search.agg_overview import _indice_entidades

    consultas = {
        'processos': ('processos', {
            'size': 0, 'track_total_hits': True, 'timeout': ES_TIMEOUT,
            'aggs': {
                't': {'terms': {'field': 'tribunal', 'size': 200}},
                'classif': {'terms': {'field': 'classificacao', 'size': 10, 'missing': '__sem__'}},
                'enriquecido': {'filter': {'term': {'enriquecido': True}}},
                'sinal': {'filter': {'term': {'tem_sinal_precatorio': True}}},
                'valor': {'sum': {'field': 'valor_causa'}},
                'com_valor': {'value_count': {'field': 'valor_causa'}},
            },
        }),
        'acervo': ('acervo', {
            'size': 0, 'track_total_hits': True, 'timeout': ES_TIMEOUT,
            'aggs': {'t': {'terms': {'field': 'tribunal', 'size': 200}}},
        }),
        'publicacoes': ('publicacoes', {
            'size': 0, 'track_total_hits': True, 'timeout': ES_TIMEOUT,
            'aggs': {
                # `max` puro devolve 8026-09-01: existem datas inválidas no índice.
                'ate_hoje': {'filter': {'range': {'publish_date': {'lte': 'now'}}},
                             'aggs': {'ultima': {'max': {'field': 'publish_date'}}}},
                'futuras': {'filter': {'range': {'publish_date': {'gt': 'now'}}}},
                'd7': {'filter': {'range': {'publish_date': {'gte': 'now-7d/d', 'lte': 'now'}}}},
            },
        }),
        'entidades': (_indice_entidades(), {'size': 0, 'track_total_hits': True, 'timeout': ES_TIMEOUT}),
    }

    def rodar(item):
        nome, (indice, body) = item
        try:
            return nome, _search(indice, body), None
        except Exception as e:
            logger.error('MCP visao_geral %s erro: %s', nome, e)
            return nome, None, f'{type(e).__name__}: {e}'

    with ThreadPoolExecutor(max_workers=len(consultas)) as pool:
        resultados = {n: (r, err) for n, r, err in pool.map(rodar, consultas.items())}

    out: dict = {'gerado_em': timezone.now().isoformat(), 'erros': {}, 'parcial': False}
    for nome, (resp, err) in resultados.items():
        if err:
            out['erros'][nome] = err
        elif resp.get('timed_out'):
            out['parcial'] = True

    proc, _ = resultados['processos']
    acervo, _ = resultados['acervo']
    pubs, _ = resultados['publicacoes']
    ents, _ = resultados['entidades']

    if proc:
        a = proc.get('aggregations') or {}
        out['processos'] = {
            'total': _total(proc),
            'enriquecidos': (a.get('enriquecido') or {}).get('doc_count'),
            'com_sinal_precatorio': (a.get('sinal') or {}).get('doc_count'),
            'com_valor_causa': (a.get('com_valor') or {}).get('value'),
            'soma_valor_causa': (a.get('valor') or {}).get('value'),
            'por_classificacao': {
                ('SEM_CLASSIFICACAO' if b['key'] == '__sem__' else b['key']): b['doc_count']
                for b in (a.get('classif') or {}).get('buckets', [])},
        }
    if acervo:
        out['acervo_cnj'] = {'total': _total(acervo)}
    if pubs:
        a = pubs.get('aggregations') or {}
        out['publicacoes'] = {
            'total': _total(pubs),
            'ultimos_7_dias': (a.get('d7') or {}).get('doc_count'),
            'ultima_data': ((a.get('ate_hoje') or {}).get('ultima') or {}).get('value_as_string'),
            'com_data_no_futuro': (a.get('futuras') or {}).get('doc_count'),
        }
    if ents:
        out['entidades'] = {'total': _total(ents)}

    # Completude dos DOIS lados: o que temos em detalhe vs. o que o tribunal declarou ao CNJ.
    if proc and acervo:
        p, c = _por_tribunal(proc), _por_tribunal(acervo)
        linhas = []
        for trib in sorted(set(p) | set(c), key=lambda t: -(c.get(t) or p.get(t) or 0)):
            linha = {'tribunal': trib, 'processos_indexados': p.get(trib, 0),
                     'acervo_cnj': c.get(trib, 0)}
            if c.get(trib):
                linha['razao_pct'] = round(100 * p.get(trib, 0) / c[trib], 1)
            linhas.append(linha)
        out['por_tribunal'] = linhas
        out['nota_por_tribunal'] = (
            '`razao_pct` = processos indexados ÷ acervo declarado ao CNJ. É uma razão, '
            'não uma interseção: passa de 100% quando temos processos baixados/arquivados '
            'que já saíram do acervo ativo. Abaixo de 100% = processos que existem e '
            'ainda não chegaram em detalhe.')
    if not out['erros']:
        del out['erros']
    return out


def visao_geral(atualizar=False):
    """Panorama do acervo inteiro. Cache de 1h (a frio custa ~15s)."""
    if not atualizar:
        cached = cache.get(VISAO_GERAL_CACHE)
        if cached is not None:
            return {**cached, 'cache': True}
    dado = _calcular_visao_geral()
    if not dado.get('erros') and not dado.get('parcial'):
        cache.set(VISAO_GERAL_CACHE, dado, VISAO_GERAL_TTL)
    return {**dado, 'cache': False}


# --------------------------------------------------------------------------- #
# descrever_base — o que dá pra perguntar
# --------------------------------------------------------------------------- #
def descrever_base():
    indices = {}
    for nome, cfg in INDICES.items():
        indices[nome] = {
            'descricao': cfg['descricao'],
            'campos_categoricos': sorted(cfg['keyword']),
            'campos_numericos': sorted(cfg['numero']),
            'campos_booleanos': sorted(cfg['booleano']),
            'campos_data': sorted(cfg['data']),
            'busca_textual_em': list(cfg['texto']),
            'campo_de_valor': cfg['valor'],
        }
    return {
        'indices': indices,
        'classificacao': CLASSIFICACOES,
        'como_perguntar': [
            'Panorama geral → visao_geral.',
            'Contagens e somas → agregar(indice, agrupar_por, filtros, faixas, texto, metrica).',
            'Ex.: precatórios por tribunal → agregar(indice="processos", agrupar_por=["tribunal"], '
            'filtros={"classificacao": "PRECATORIO"}).',
            'Ex.: valor de causa por ano no TJSP → agregar(indice="processos", agrupar_por=["ano_cnj"], '
            'filtros={"tribunal": "TJSP"}, metrica="soma").',
            'Ex.: processos de um devedor → ranking_entidades(q="INSS") e depois ficha_entidade(id).',
            'Ex.: publicações por mês com um termo → agregar(indice="publicacoes", '
            'agrupar_por=["publish_date"], intervalo="mes", texto="\\"requisição de pequeno valor\\"", '
            'faixas={"publish_date": {"gte": "2026-01-01"}}).',
            'Mapa de precatórios por UF/tribunal → mapa_precatorios; um estado em detalhe → panorama_estado.',
        ],
        'ressalvas': [
            'processos ≠ acervo: `processos` é o que conhecemos em detalhe; `acervo` é o que o '
            'tribunal declarou ao CNJ (sem partes nem valor). Compare os dois antes de dizer "não existe".',
            'tem_sinal_precatorio=null significa "não computado", não "não tem".',
            'valor_causa ausente é comum: soma/média valem só sobre quem TEM valor. Diga isso.',
            'classificacao é de um modelo de ML e só existe pra processos classificados — '
            'ausência não é NAO_LEAD.',
            'Campos de texto (partes, advs, assunto, body) não agregam; use `texto` pra filtrar.',
            'Há publish_date inválidas (ano 8026) no índice de publicações: filtre por faixa de data.',
            'Se a resposta vier com `parcial: true` ou `outros`, o número NÃO é o universo — diga.',
        ],
    }


# --------------------------------------------------------------------------- #
# Wrappers sobre os serviços que já alimentam as telas
# --------------------------------------------------------------------------- #
def _sem_vazios(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, '', [])}


def mapa_precatorios(uf=None, tribunal=None, classificacao=None, tipo=None, parte=None,
                     entidade_id=None, ano_min=None, ano_max=None, valor_min=None, valor_max=None):
    """Mapa de precatórios: por UF (sem `uf`) ou tribunais de uma UF (com `uf`)."""
    from search import agg_overview
    try:
        filtros = agg_overview.parse_filtros(_sem_vazios({
            'tribunal': tribunal, 'classificacao': classificacao, 'tipo': tipo, 'parte': parte,
            'entidade_id': entidade_id, 'ano_min': ano_min, 'ano_max': ano_max,
            'valor_min': valor_min, 'valor_max': valor_max,
        }))
        if uf:
            return agg_overview.agg_tribunais(uf, filtros)
        return agg_overview.agg_por_uf(filtros)
    except Exception as e:
        logger.error('MCP mapa_precatorios erro: %s', e)
        return {'error': f'{type(e).__name__}: {e}'}


def panorama_estado(uf, metrica='todos', tribunal=None, classificacao=None, parte=None,
                    entidade_id=None, ano_min=None, ano_max=None):
    """Um estado explodido: tipo de processo, ano, tribunal, entidade devedora, classificação."""
    from search import agg_estado, agg_overview
    try:
        filtros = agg_overview.parse_filtros(_sem_vazios({
            'tribunal': tribunal, 'classificacao': classificacao, 'parte': parte,
            'entidade_id': entidade_id, 'ano_min': ano_min, 'ano_max': ano_max,
        }))
        return agg_estado.agg_estado(uf, filtros, metrica)
    except Exception as e:
        logger.error('MCP panorama_estado erro: %s', e)
        return {'error': f'{type(e).__name__}: {e}'}


def ranking_entidades(q=None, ente=None, ordenar=None, tamanho=20, min_partes=None, cursor=None):
    """Quem mais litiga / quem deve: o cadastro canônico de entidades."""
    from search import agg_entidade
    try:
        opcoes = agg_entidade.parse_ranking(_sem_vazios({
            'q': q, 'ente': '1' if ente else None, 'ordenar': ordenar, 'tamanho': tamanho,
            'min_partes': min_partes, 'cursor': cursor,
        }))
        return agg_entidade.ranking_entidades(opcoes)
    except Exception as e:
        logger.error('MCP ranking_entidades erro: %s', e)
        return {'error': f'{type(e).__name__}: {e}'}


def ficha_entidade(entidade_id, uf=None, tribunal=None, classificacao=None, ano_min=None, ano_max=None):
    """Os processos de UMA entidade, por UF/tribunal/ano/classificação."""
    from search import agg_entidade, agg_overview
    try:
        filtros = agg_overview.parse_filtros(_sem_vazios({
            'uf': uf, 'tribunal': tribunal, 'classificacao': classificacao,
            'ano_min': ano_min, 'ano_max': ano_max,
        }))
        return agg_entidade.ficha_entidade(entidade_id, filtros)
    except Exception as e:
        logger.error('MCP ficha_entidade erro: %s', e)
        return {'error': f'{type(e).__name__}: {e}'}


def buscar_processos(parte=None, advogado=None, filtros=None, ordenar=None, size=20, cursor=None):
    """Lista de processos com os mesmos filtros da tela de busca, com total exato e cursor."""
    from search import busca_api
    try:
        f = busca_api.parse_filtros_processos({k: str(v) for k, v in (filtros or {}).items()
                                               if v not in (None, '')})
        pagina = busca_api.buscar_processos(parte=parte, advogado=advogado, filtros=f,
                                            ordenar=ordenar, size=size, cursor=cursor)
        # A página sai sem contagem exata (custa 15x no ES) e o total dela para
        # em 10.000 "gte" — um piso, não o total. Contagem à parte, em filter context.
        if (pagina.get('total') or {}).get('relation') != 'eq':
            contagem = busca_api.body_contagem_processos(parte, advogado, f)
            pagina['total'] = {'value': _total(_search('processos', contagem)), 'relation': 'eq'}
        return pagina
    except busca_api.BuscaParamError as e:
        return {'error': str(e)}
    except Exception as e:
        logger.error('MCP buscar_processos erro: %s', e)
        return {'error': f'{type(e).__name__}: {e}'}
