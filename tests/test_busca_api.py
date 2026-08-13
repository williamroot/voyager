"""Testes da API de Busca v1 (search/busca_api.py + api/busca_views.py).

Não dependem de ES real: mockam o cliente (`search.busca_api.get_es`), padrão
de `test_agg_overview.py`. Cobrem: saneamento de params, montagem do corpo ES
(asserts no body), paginação search_after (cursor opaco), cada endpoint REST,
filtros combinados e degradação limpa (400/404/503, nunca 500).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from search import busca_api as ba
from search.busca_api import BuscaParamError

CNJ = '1105916-36.2026.8.26.0053'
CNJ_DIGITOS = '11059163620268260053'


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _qd(**kwargs):
    """Simula um request.GET (dict-like com .get)."""
    return SimpleNamespace(get=lambda k, default=None: kwargs.get(k, default))


def _es_resp(hits, total=None, relation='eq'):
    """Envelope ES de resposta search. `hits` = [(source, sort_values), ...]."""
    return {
        'hits': {
            'total': {'value': total if total is not None else len(hits),
                      'relation': relation},
            'hits': [
                {'_id': str(i), '_source': src, 'sort': sort,
                 **({'highlight': hl} if hl else {})}
                for i, (src, sort, *rest) in enumerate(hits)
                for hl in [rest[0] if rest else None]
            ],
        }
    }


def _mock_es(*responses):
    es = MagicMock()
    es.search.side_effect = list(responses)
    return es


def _body_da_chamada(es, n=0):
    return es.search.call_args_list[n].kwargs['body']


# --------------------------------------------------------------------------- #
# normalizar_cnj / cursor / clamps
# --------------------------------------------------------------------------- #
def test_normalizar_cnj_mascarado_passa():
    assert ba.normalizar_cnj(CNJ) == CNJ


def test_normalizar_cnj_digitos_formata():
    assert ba.normalizar_cnj(CNJ_DIGITOS) == CNJ


def test_normalizar_cnj_com_espacos():
    assert ba.normalizar_cnj(f'  {CNJ}  ') == CNJ


def test_normalizar_cnj_invalido_levanta():
    for ruim in ('', None, '123', 'abc', '1105916-36.2026'):
        with pytest.raises(BuscaParamError):
            ba.normalizar_cnj(ruim)


def test_cursor_roundtrip():
    valores = [1784084400000, 1271133182]
    assert ba.decode_cursor(ba.encode_cursor(valores)) == valores


def test_cursor_none_e_vazio():
    assert ba.decode_cursor(None) is None
    assert ba.decode_cursor('') is None


def test_cursor_corrompido_levanta():
    for ruim in ('!!!nao-base64!!!', 'YWJj', ba.encode_cursor([])[:1] or 'e30'):
        with pytest.raises(BuscaParamError):
            ba.decode_cursor(ruim)


def test_clamp_size():
    assert ba.clamp_size(None) == ba.SIZE_DEFAULT
    assert ba.clamp_size('abc') == ba.SIZE_DEFAULT
    assert ba.clamp_size('5') == 5
    assert ba.clamp_size(0) == 1
    assert ba.clamp_size(9999) == ba.SIZE_MAX


# --------------------------------------------------------------------------- #
# parse_filtros_processos / build_clauses_processos
# --------------------------------------------------------------------------- #
def test_parse_filtros_processos_vazio():
    assert ba.parse_filtros_processos(_qd()) == {}


def test_parse_filtros_processos_combinados():
    f = ba.parse_filtros_processos(_qd(
        tribunal='tjsp,tjrj', uf='sp', classificacao='precatorio',
        tem_sinal_precatorio='1', tem_ente_publico_passivo='true',
        segredo_justica='false', enriquecido='0',
        codigo_classe='1116', assunto_codigo='10433',
        ano_min='2020', ano_max='3000',
        valor_min='-5', valor_max='1000.5',
        ultima_mov_gte='2026-01-01', autuacao_lte='lixo',
    ))
    assert f['tribunal'] == ['TJSP', 'TJRJ']
    assert f['uf'] == 'SP'
    assert f['classificacao'] == 'PRECATORIO'
    assert f['tem_sinal_precatorio'] is True
    assert f['tem_ente_publico_passivo'] is True
    assert f['segredo_justica'] is False
    assert f['enriquecido'] is False
    assert f['codigo_classe'] == '1116'
    assert f['assunto_codigo'] == '10433'
    assert f['ano_min'] == 2020
    assert f['ano_max'] >= 2026            # 3000 clampado pro ano atual
    assert 'valor_min' not in f            # negativo ignorado
    assert f['valor_max'] == 1000.5
    assert f['ultima_mov_gte'] == '2026-01-01'
    assert 'autuacao_lte' not in f         # data lixo descartada


def test_parse_filtros_processos_faixas_invertidas_ordenam():
    f = ba.parse_filtros_processos(_qd(ano_min='2024', ano_max='2010',
                                       valor_min='900', valor_max='10'))
    assert (f['ano_min'], f['ano_max']) == (2010, 2024)
    assert (f['valor_min'], f['valor_max']) == (10.0, 900.0)


def test_build_clauses_processos_todos():
    filtros = ba.parse_filtros_processos(_qd(
        tribunal='TJSP', uf='SP', classificacao='PRECATORIO',
        tem_sinal_precatorio='true', codigo_classe='1116',
        ano_min='2020', ano_max='2024', valor_min='100', valor_max='5000',
        ultima_mov_gte='2026-01-01', autuacao_gte='2020-01-01',
    ))
    clauses = ba.build_clauses_processos(filtros)
    assert {'term': {'tribunal': 'TJSP'}} in clauses            # 1 sigla ⇒ term
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'term': {'classificacao': 'PRECATORIO'}} in clauses
    assert {'term': {'tem_sinal_precatorio': True}} in clauses
    assert {'term': {'codigo_classe': '1116'}} in clauses
    assert {'range': {'ano_cnj': {'gte': 2020, 'lte': 2024}}} in clauses
    assert {'range': {'valor_causa': {'gte': 100.0, 'lte': 5000.0}}} in clauses
    assert {'range': {'ultima_movimentacao_em': {'gte': '2026-01-01'}}} in clauses
    assert {'range': {'data_autuacao': {'gte': '2020-01-01'}}} in clauses


def test_build_clauses_processos_tribunal_csv_vira_terms():
    clauses = ba.build_clauses_processos({'tribunal': ['TJSP', 'TJRJ']})
    assert {'terms': {'tribunal': ['TJSP', 'TJRJ']}} in clauses


# --------------------------------------------------------------------------- #
# parse_filtros_movs / build_clauses_movs
# --------------------------------------------------------------------------- #
def test_parse_filtros_movs_normaliza_proc():
    f = ba.parse_filtros_movs(_qd(proc=CNJ_DIGITOS))
    assert f['proc'] == CNJ


def test_parse_filtros_movs_proc_invalido_levanta():
    with pytest.raises(BuscaParamError):
        ba.parse_filtros_movs(_qd(proc='123'))


def test_build_clauses_movs_combinados():
    f = ba.parse_filtros_movs(_qd(
        tribunal='TJSP', proc=CNJ, tipo_comunicacao='Intimação',
        codigo_classe='1116', publicado_gte='2026-01-01', publicado_lte='2026-06-30'))
    clauses = ba.build_clauses_movs(f)
    assert {'term': {'tribunal': 'TJSP'}} in clauses
    assert {'term': {'proc': CNJ}} in clauses
    assert {'term': {'tipo_comunicacao': 'Intimação'}} in clauses
    assert {'term': {'codigo_classe': '1116'}} in clauses
    assert {'range': {'publish_date': {'gte': '2026-01-01', 'lte': '2026-06-30'}}} in clauses


# --------------------------------------------------------------------------- #
# ordenação
# --------------------------------------------------------------------------- #
def test_sort_processos_defaults():
    ordenar, sort = ba.sort_processos(None, tem_texto=True)
    assert ordenar == 'relevancia'
    assert sort[0] == {'_score': 'desc'}
    ordenar, sort = ba.sort_processos(None, tem_texto=False)
    assert ordenar == 'recente'
    assert 'ultima_movimentacao_em' in sort[0]


def test_sort_processos_relevancia_sem_texto_vira_recente():
    ordenar, _ = ba.sort_processos('relevancia', tem_texto=False)
    assert ordenar == 'recente'


def test_sort_processos_valor_com_tiebreaker():
    ordenar, sort = ba.sort_processos('valor_desc', tem_texto=False)
    assert ordenar == 'valor_desc'
    assert sort[0] == {'valor_causa': {'order': 'desc', 'missing': '_last'}}
    assert sort[-1] == {'id': 'desc'}     # tiebreaker obrigatório (search_after)


def test_sort_processos_enum_invalido_cai_no_default():
    ordenar, _ = ba.sort_processos('xablau', tem_texto=False)
    assert ordenar == 'recente'


def test_sort_movs():
    assert ba.sort_movs(None, tem_texto=True)[0] == 'relevancia'
    assert ba.sort_movs(None, tem_texto=False)[0] == 'recente'
    assert ba.sort_movs('relevancia', tem_texto=False)[0] == 'recente'


# --------------------------------------------------------------------------- #
# get_processo (ES mockado)
# --------------------------------------------------------------------------- #
@patch('search.busca_api.get_es')
def test_get_processo_encontrado(mock_get_es):
    es = _mock_es(_es_resp([({'proc': CNJ, 'tribunal': 'TJSP'}, [1])]))
    mock_get_es.return_value = es
    doc = ba.get_processo(CNJ_DIGITOS)   # aceita dígitos puros
    assert doc == {'proc': CNJ, 'tribunal': 'TJSP'}
    body = _body_da_chamada(es)
    assert body['size'] == 1
    assert {'term': {'proc': CNJ}} in body['query']['bool']['filter']
    assert body['timeout'] == ba.ES_QUERY_TIMEOUT
    # índice certo
    assert 'processos' in es.search.call_args.kwargs['index']


@patch('search.busca_api.get_es')
def test_get_processo_nao_encontrado(mock_get_es):
    mock_get_es.return_value = _mock_es(_es_resp([]))
    assert ba.get_processo(CNJ) is None


# --------------------------------------------------------------------------- #
# movimentacoes_por_cnj — paginação search_after
# --------------------------------------------------------------------------- #
@patch('search.busca_api.get_es')
def test_movimentacoes_pagina_cheia_gera_cursor(mock_get_es):
    hits = [({'id': i, 'proc': CNJ}, [1000 - i, i]) for i in range(2)]
    es = _mock_es(_es_resp(hits, total=10, relation='eq'))
    mock_get_es.return_value = es
    out = ba.movimentacoes_por_cnj(CNJ, size=2)
    assert out['versao'] == 'v1'
    assert out['total'] == {'value': 10, 'relation': 'eq'}
    assert len(out['results']) == 2
    # página cheia ⇒ next_cursor com os sort values do último hit
    assert ba.decode_cursor(out['next_cursor']) == [999, 1]
    body = _body_da_chamada(es)
    assert body['sort'][0] == {'publish_date': {'order': 'desc', 'missing': '_last'}}
    assert 'search_after' not in body
    assert 'movimentacoes' in es.search.call_args.kwargs['index']


@patch('search.busca_api.get_es')
def test_movimentacoes_ultima_pagina_sem_cursor(mock_get_es):
    mock_get_es.return_value = _mock_es(
        _es_resp([({'id': 1}, [500, 1])], total=1))
    out = ba.movimentacoes_por_cnj(CNJ, size=50)
    assert out['next_cursor'] is None


@patch('search.busca_api.get_es')
def test_movimentacoes_cursor_vira_search_after(mock_get_es):
    es = _mock_es(_es_resp([]))
    mock_get_es.return_value = es
    cursor = ba.encode_cursor([999, 1])
    ba.movimentacoes_por_cnj(CNJ, size=2, cursor=cursor)
    assert _body_da_chamada(es)['search_after'] == [999, 1]


def test_movimentacoes_cnj_invalido_levanta():
    with pytest.raises(BuscaParamError):
        ba.movimentacoes_por_cnj('nao-e-cnj')


# --------------------------------------------------------------------------- #
# buscar_processos
# --------------------------------------------------------------------------- #
@patch('search.busca_api.get_es')
def test_buscar_processos_parte_e_advogado_combinados(mock_get_es):
    es = _mock_es(_es_resp([({'proc': CNJ, 'partes': 'Estado de São Paulo'}, [1.5, 9])]))
    mock_get_es.return_value = es
    filtros = {'tribunal': ['TJSP'], 'tem_sinal_precatorio': True}
    out = ba.buscar_processos(parte='estado de são paulo', advogado='silva',
                              filtros=filtros, size=20)
    body = _body_da_chamada(es)
    must = body['query']['bool']['must']
    assert {'match': {'partes': {'query': 'estado de são paulo', 'operator': 'and'}}} in must
    assert {'match': {'advs': {'query': 'silva', 'operator': 'and'}}} in must
    fil = body['query']['bool']['filter']
    assert {'term': {'tribunal': 'TJSP'}} in fil
    assert {'term': {'tem_sinal_precatorio': True}} in fil
    # com texto e sem `ordenar` ⇒ relevância
    assert out['ordenar'] == 'relevancia'
    assert body['sort'][0] == {'_score': 'desc'}
    # ecoa critérios aplicados
    assert out['filtros']['parte'] == 'estado de são paulo'
    assert out['filtros']['advogado'] == 'silva'


@patch('search.busca_api.get_es')
def test_buscar_processos_por_valor_ordenado(mock_get_es):
    es = _mock_es(_es_resp([({'proc': CNJ, 'valor_causa': 5000.0}, [5000.0, 9])]))
    mock_get_es.return_value = es
    out = ba.buscar_processos(filtros={'valor_min': 1000.0}, ordenar='valor_desc')
    body = _body_da_chamada(es)
    assert body['sort'][0] == {'valor_causa': {'order': 'desc', 'missing': '_last'}}
    assert {'range': {'valor_causa': {'gte': 1000.0}}} in body['query']['bool']['filter']
    assert out['ordenar'] == 'valor_desc'


@patch('search.busca_api.get_es')
def test_buscar_processos_sem_criterio_match_all(mock_get_es):
    es = _mock_es(_es_resp([]))
    mock_get_es.return_value = es
    out = ba.buscar_processos()
    body = _body_da_chamada(es)
    assert body['query'] == {'match_all': {}}
    assert out['ordenar'] == 'recente'
    assert out['results'] == []
    assert out['next_cursor'] is None


@patch('search.busca_api.get_es')
def test_buscar_processos_cursor_e_size_clampado(mock_get_es):
    es = _mock_es(_es_resp([]))
    mock_get_es.return_value = es
    out = ba.buscar_processos(size='9999', cursor=ba.encode_cursor([123, 4]))
    assert out['size'] == ba.SIZE_MAX
    body = _body_da_chamada(es)
    assert body['size'] == ba.SIZE_MAX
    assert body['search_after'] == [123, 4]


def test_buscar_processos_cursor_invalido_levanta():
    with pytest.raises(BuscaParamError):
        ba.buscar_processos(cursor='###')


# --------------------------------------------------------------------------- #
# buscar_movimentacoes (full-text)
# --------------------------------------------------------------------------- #
def test_buscar_movimentacoes_sem_criterio_levanta():
    with pytest.raises(BuscaParamError):
        ba.buscar_movimentacoes()
    with pytest.raises(BuscaParamError):
        ba.buscar_movimentacoes(q='   ', filtros={})


@patch('search.busca_api.get_es')
def test_buscar_movimentacoes_fulltext_com_highlight(mock_get_es):
    hit = ({'id': 1, 'proc': CNJ, 'tribunal': 'TJSP'}, [2.0, 1],
           {'body': ['ordem do <em>precatório</em>']})
    es = _mock_es(_es_resp([hit], total=10000, relation='gte'))
    mock_get_es.return_value = es
    filtros = ba.parse_filtros_movs(_qd(tribunal='TJSP', publicado_gte='2026-01-01'))
    out = ba.buscar_movimentacoes(q='precatório', filtros=filtros, size=20)

    body = _body_da_chamada(es)
    assert {'match': {'body': {'query': 'precatório', 'operator': 'and'}}} in body['query']['bool']['must']
    assert {'term': {'tribunal': 'TJSP'}} in body['query']['bool']['filter']
    assert {'range': {'publish_date': {'gte': '2026-01-01'}}} in body['query']['bool']['filter']
    assert body['highlight']['fields']['body']['fragment_size'] == 200
    assert body['_source'] == ba._MOV_LIST_SOURCE   # lista NÃO devolve body inteiro

    assert out['total'] == {'value': 10000, 'relation': 'gte'}
    assert out['results'][0]['highlight'] == ['ordem do <em>precatório</em>']
    assert out['ordenar'] == 'relevancia'
    assert out['filtros']['q'] == 'precatório'


@patch('search.busca_api.get_es')
def test_buscar_movimentacoes_so_filtro_sem_highlight(mock_get_es):
    es = _mock_es(_es_resp([({'id': 1}, [1000, 1])]))
    mock_get_es.return_value = es
    out = ba.buscar_movimentacoes(filtros={'proc': CNJ})
    body = _body_da_chamada(es)
    assert 'highlight' not in body
    assert 'must' not in body['query']['bool']
    assert out['ordenar'] == 'recente'
    assert 'highlight' not in out['results'][0]


@patch('search.busca_api.get_es')
def test_buscar_movimentacoes_search_after(mock_get_es):
    es = _mock_es(_es_resp([]))
    mock_get_es.return_value = es
    ba.buscar_movimentacoes(q='precatório', cursor=ba.encode_cursor([2.0, 55]))
    assert _body_da_chamada(es)['search_after'] == [2.0, 55]


# --------------------------------------------------------------------------- #
# Endpoints REST (client Django + ApiClient + ES mockado)
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_key(db):
    from tribunals.models import ApiClient
    ApiClient.objects.create(nome='busca-test', api_key='k-busca', ativo=True)
    return {'HTTP_X_API_KEY': 'k-busca'}


def test_urls_resolvem():
    from django.urls import reverse
    assert reverse('busca-processos') == '/api/v1/busca/processos/'
    assert reverse('busca-processo', args=[CNJ]) == f'/api/v1/busca/processos/{CNJ}/'
    assert reverse('busca-processo-movs', args=[CNJ]).endswith('/movimentacoes/')
    assert reverse('busca-movimentacoes') == '/api/v1/busca/movimentacoes/'


@pytest.mark.django_db
def test_endpoint_sem_auth_403(client):
    assert client.get('/api/v1/busca/processos/').status_code == 403


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_processo_detalhe(mock_get_es, client, api_key):
    mock_get_es.return_value = _mock_es(
        _es_resp([({'proc': CNJ, 'tribunal': 'TJSP', 'valor_causa': 1.0}, [1])]))
    resp = client.get(f'/api/v1/busca/processos/{CNJ}/', **api_key)
    assert resp.status_code == 200
    data = resp.json()
    assert data['versao'] == 'v1'
    assert data['processo']['proc'] == CNJ
    assert data['movimentacoes_url'].endswith(f'/busca/processos/{CNJ}/movimentacoes/')


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_processo_404(mock_get_es, client, api_key):
    mock_get_es.return_value = _mock_es(_es_resp([]))
    resp = client.get(f'/api/v1/busca/processos/{CNJ}/', **api_key)
    assert resp.status_code == 404
    assert resp.json() == {'erro': 'processo_nao_encontrado'}


@pytest.mark.django_db
def test_endpoint_processo_cnj_invalido_400(client, api_key):
    resp = client.get('/api/v1/busca/processos/nao-e-cnj/', **api_key)
    assert resp.status_code == 400
    assert resp.json() == {'erro': 'cnj_invalido'}


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_movimentacoes_do_processo(mock_get_es, client, api_key):
    hits = [({'id': i, 'proc': CNJ, 'body': 'texto'}, [900 - i, i]) for i in range(2)]
    mock_get_es.return_value = _mock_es(_es_resp(hits, total=5))
    resp = client.get(f'/api/v1/busca/processos/{CNJ}/movimentacoes/?size=2', **api_key)
    assert resp.status_code == 200
    data = resp.json()
    assert data['proc'] == CNJ
    assert len(data['results']) == 2
    assert data['next_cursor']   # página cheia


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_movimentacoes_processo_inexistente_404(mock_get_es, client, api_key):
    # 1ª chamada: movs vazias; 2ª chamada: processo não existe
    mock_get_es.return_value = _mock_es(_es_resp([]), _es_resp([]))
    resp = client.get(f'/api/v1/busca/processos/{CNJ}/movimentacoes/', **api_key)
    assert resp.status_code == 404


@pytest.mark.django_db
def test_endpoint_movimentacoes_cursor_invalido_400(client, api_key):
    resp = client.get(
        f'/api/v1/busca/processos/{CNJ}/movimentacoes/?cursor=%23%23', **api_key)
    assert resp.status_code == 400
    assert resp.json() == {'erro': 'cursor_invalido'}


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_busca_processos_filtros_combinados(mock_get_es, client, api_key):
    es = _mock_es(_es_resp([({'proc': CNJ, 'uf': 'SP'}, [123, 9])]))
    mock_get_es.return_value = es
    resp = client.get(
        '/api/v1/busca/processos/?parte=estado&uf=SP&ano_min=2020'
        '&tem_sinal_precatorio=1&ordenar=valor_desc&size=10', **api_key)
    assert resp.status_code == 200
    data = resp.json()
    assert data['ordenar'] == 'valor_desc'
    assert data['filtros']['uf'] == 'SP'
    assert data['filtros']['parte'] == 'estado'
    body = _body_da_chamada(es)
    assert {'term': {'uf': 'SP'}} in body['query']['bool']['filter']
    assert {'range': {'ano_cnj': {'gte': 2020}}} in body['query']['bool']['filter']


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_busca_processos_es_fora_503(mock_get_es, client, api_key):
    es = MagicMock()
    es.search.side_effect = ConnectionError('boom')
    mock_get_es.return_value = es
    resp = client.get('/api/v1/busca/processos/', **api_key)
    assert resp.status_code == 503
    assert resp.json()['erro'] == 'elasticsearch_indisponivel'


@pytest.mark.django_db
def test_endpoint_busca_movimentacoes_sem_criterio_400(client, api_key):
    resp = client.get('/api/v1/busca/movimentacoes/', **api_key)
    assert resp.status_code == 400
    assert resp.json() == {'erro': 'informe_q_proc_ou_tribunal'}


@pytest.mark.django_db
@patch('search.busca_api.get_es')
def test_endpoint_busca_movimentacoes_fulltext(mock_get_es, client, api_key):
    hit = ({'id': 1, 'proc': CNJ, 'tribunal': 'TJSP'}, [2.0, 1],
           {'body': ['<em>precatório</em>']})
    mock_get_es.return_value = _mock_es(_es_resp([hit], total=10000, relation='gte'))
    resp = client.get(
        '/api/v1/busca/movimentacoes/?q=precat%C3%B3rio&tribunal=TJSP'
        '&publicado_gte=2026-01-01', **api_key)
    assert resp.status_code == 200
    data = resp.json()
    assert data['total'] == {'value': 10000, 'relation': 'gte'}
    assert data['results'][0]['highlight'] == ['<em>precatório</em>']
    assert data['filtros']['tribunal'] == ['TJSP']
