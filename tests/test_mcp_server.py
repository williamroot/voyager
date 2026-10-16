import json
import uuid

import pytest

from tribunals.models import ApiClient


def _token_legado():
    token = uuid.uuid4()
    ApiClient.objects.create(nome=f'desc-{token.hex[:8]}', api_key=token.hex, ativo=True, mcp_token=token)
    return str(token)


@pytest.mark.django_db
def test_mcp_descriptor_exige_sessao_ou_token(client):
    """Lista tools com descrição e schema: era público até 16/10/2026."""
    resp = client.get('/mcp/.well-known/mcp.json')
    assert resp.status_code == 401
    assert 'tools' not in resp.json()
    assert client.get('/mcp/.well-known/mcp.json', HTTP_AUTHORIZATION='Bearer lixo').status_code == 401

    from django.contrib.auth import get_user_model
    client.force_login(get_user_model().objects.create_user('d@x.com', 'd@x.com', 'senha-forte-123'))
    resp = client.get('/mcp/.well-known/mcp.json')
    assert resp.status_code == 200 and resp['Cache-Control'] == 'private, no-store'


@pytest.mark.django_db
def test_mcp_descriptor(client):
    resp = client.get('/mcp/.well-known/mcp.json', HTTP_AUTHORIZATION=f'Bearer {_token_legado()}')
    assert resp.status_code == 200
    data = resp.json()
    assert data['name'] == 'voyager'
    assert 'tools' in data
    assert len(data['tools']) == 43


@pytest.mark.django_db
def test_mcp_descriptor_lista_todas_tools(client):
    resp = client.get('/mcp/.well-known/mcp.json', HTTP_AUTHORIZATION=f'Bearer {_token_legado()}')
    data = resp.json()
    tool_names = [t['name'] for t in data['tools']]
    expected = {
        'buscar_diarios', 'get_documento', 'get_processo', 'list_movimentacoes',
        'get_partes', 'listar_fontes', 'status_cobertura', 'monitorar_termo',
        'monitorar_processo', 'listar_detections', 'get_pdf', 'classificacao_lead',
    }
    assert expected.issubset(set(tool_names))


@pytest.mark.django_db
def test_mcp_messages_sem_token_401(client):
    resp = client.post('/mcp/messages', data=json.dumps({'method': 'tools/list', 'id': 1}),
                        content_type='application/json')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_mcp_messages_token_invalido_401(client):
    resp = client.post('/mcp/messages',
                        data=json.dumps({'method': 'tools/list', 'id': 1}),
                        content_type='application/json',
                        HTTP_AUTHORIZATION='Bearer token-invalido')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_mcp_messages_token_valido_lista_tools(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({'method': 'tools/list', 'id': 1, 'params': {}}),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    assert data['jsonrpc'] == '2.0'
    assert 'result' in data
    assert 'tools' in data['result']
    assert len(data['result']['tools']) == 43


@pytest.mark.django_db
def test_mcp_messages_call_buscar_diarios_sem_query_erro_schema(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({
                            'method': 'tools/call',
                            'id': 1,
                            'params': {'name': 'buscar_diarios', 'arguments': {}},
                        }),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 400
    data = resp.json()
    assert 'error' in data


@pytest.mark.django_db
def test_mcp_messages_call_tool_inexistente(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({
                            'method': 'tools/call',
                            'id': 1,
                            'params': {'name': 'tool_inexistente', 'arguments': {}},
                        }),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 400


@pytest.mark.django_db
def test_mcp_messages_call_listar_fontes(client):
    from tribunals.models import FonteDiario, Tribunal
    t, _ = Tribunal.objects.get_or_create(sigla='TRF1', defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1'})
    FonteDiario.objects.update_or_create(
        source_id=1, defaults={'tribunal': t, 'diario_slug': 'dje-trf1',
                               'orgao_slug': 'trf1', 'caderno_slug': '', 'nome': 'TRF - 1ª Reg.'},
    )
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({
                            'method': 'tools/call',
                            'id': 1,
                            'params': {'name': 'listar_fontes', 'arguments': {}},
                        }),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    content_text = data['result']['content'][0]['text']
    fontes = json.loads(content_text)
    assert '1' in fontes
    assert fontes['1'] == 'TRF - 1ª Reg.'


@pytest.mark.django_db
def test_mcp_messages_call_get_processo(client):
    from tribunals.models import Process, Tribunal
    t, _ = Tribunal.objects.get_or_create(sigla='TRF1', defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1'})
    Process.objects.create(
        numero_cnj='0001234-56.2025.4.01.0000', tribunal=t,
        classe_nome='Execução Fiscal',
    )
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({
                            'method': 'tools/call',
                            'id': 1,
                            'params': {'name': 'get_processo', 'arguments': {'cnj': '0001234-56.2025.4.01.0000'}},
                        }),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    content = json.loads(data['result']['content'][0]['text'])
    assert content['numero_cnj'] == '0001234-56.2025.4.01.0000'
    assert content['classe_nome'] == 'Execução Fiscal'


@pytest.mark.django_db
def test_mcp_messages_call_get_processo_inexistente(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({
                            'method': 'tools/call',
                            'id': 1,
                            'params': {'name': 'get_processo', 'arguments': {'cnj': '9999999-99.9999.9.99.9999'}},
                        }),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    content = json.loads(data['result']['content'][0]['text'])
    assert content.get('available') is False


@pytest.mark.django_db
def test_mcp_messages_call_classificacao_lead_desativada(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/messages',
                        data=json.dumps({
                            'method': 'tools/call',
                            'id': 1,
                            'params': {'name': 'classificacao_lead', 'arguments': {'cnj': '0001234-56.2025.4.01.0000'}},
                        }),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    content = json.loads(data['result']['content'][0]['text'])
    assert 'error' in content
    assert 'desativada' in content['error']


@pytest.mark.django_db
def test_mcp_resources_list(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/resources',
                        data=json.dumps({'method': 'resources/list', 'id': 1}),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    assert 'resources' in data['result']


@pytest.mark.django_db
def test_mcp_initialize(client):
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    token = uuid.uuid4()
    cliente.mcp_token = token
    cliente.save()

    resp = client.post('/mcp/',
                        data=json.dumps({'method': 'initialize', 'id': 1}),
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    data = resp.json()
    assert data['result']['serverInfo']['name'] == 'voyager'


@pytest.mark.django_db
def test_mcp_sse_endpoint(client):
    resp = client.get('/mcp/sse')
    assert resp.status_code == 200
    assert resp['Content-Type'] == 'text/event-stream'

def _cliente_mcp():
    cliente = ApiClient.objects.create(nome='teste', api_key='key123', ativo=True)
    cliente.mcp_token = uuid.uuid4()
    cliente.save()
    return cliente.mcp_token


def _post_mcp(client, token, payload):
    return client.post('/mcp/', data=json.dumps(payload), content_type='application/json',
                       HTTP_ACCEPT='application/json, text/event-stream',
                       HTTP_AUTHORIZATION=f'Bearer {token}')


@pytest.mark.django_db
def test_mcp_streamable_http_fluxo_completo(client):
    """O fluxo que o Claude Code faz: tudo por POST /mcp/. Antes, /mcp/ devolvia
    o resultado do initialize pra qualquer método e o cliente nunca via tool."""
    token = _cliente_mcp()

    init = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                                     'params': {'protocolVersion': '2025-06-18'}})
    assert init.status_code == 200
    assert init.json()['result']['protocolVersion'] == '2025-06-18'

    notif = _post_mcp(client, token, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert notif.status_code == 202

    lista = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert lista.status_code == 200
    assert lista.json()['id'] == 1
    nomes = {t['name'] for t in lista.json()['result']['tools']}
    assert 'buscar_diarios' in nomes

    fontes = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                       'params': {'name': 'listar_fontes', 'arguments': {}}})
    assert fontes.status_code == 200
    assert fontes.json()['result']['content'][0]['type'] == 'text'

    ping = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
    assert ping.json()['result'] == {}

    rec = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list'})
    assert 'resources' in rec.json()['result']


@pytest.mark.django_db
def test_mcp_streamable_http_versao_desconhecida_cai_na_mais_nova(client):
    token = _cliente_mcp()
    resp = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                                     'params': {'protocolVersion': '1999-01-01'}})
    assert resp.json()['result']['protocolVersion'] == '2025-06-18'


@pytest.mark.django_db
def test_mcp_streamable_http_erro_jsonrpc_no_corpo(client):
    token = _cliente_mcp()
    resp = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                                     'params': {'name': 'nao_existe', 'arguments': {}}})
    assert resp.status_code == 200
    assert resp.json()['error']['code'] == -32601

    resp = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 6, 'method': 'metodo/inventado'})
    assert resp.json()['error']['code'] == -32601


@pytest.mark.django_db
def test_mcp_endpoint_get_405_e_sem_token_401(client):
    assert client.get('/mcp/').status_code == 405
    resp = client.post('/mcp/', data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}),
                       content_type='application/json')
    assert resp.status_code == 401


def test_buscar_diarios_usa_servico_da_busca(monkeypatch):
    """Antes era query_string cru sem timeout no ES (estourava 30s em prod)."""
    from mcp_server import delegates
    from search import busca_api

    chamado = {}

    def fake(q=None, filtros=None, ordenar=None, size=None, cursor=None):
        chamado.update(q=q, filtros=filtros, ordenar=ordenar, size=size)
        return {'total': {'value': 1, 'relation': 'eq'}, 'parcial': False, 'results': [{
            'id': 7, 'tribunal': 'TJSP', 'publish_date': '2026-10-01', 'proc': '0000001-00.2026.8.26.0001',
            'highlight': ['<em>precatório</em> expedido'], 'docurl': 'u',
        }]}

    monkeypatch.setattr(busca_api, 'buscar_movimentacoes', fake)
    out = delegates.buscar_diarios('precatório', tribunal='tjsp, trf3', data_inicio='2026-01-01', size=5)

    assert chamado == {'q': 'precatório', 'ordenar': 'relevancia', 'size': 5,
                       'filtros': {'tribunal': ['TJSP', 'TRF3'], 'publicado_gte': '2026-01-01'}}
    assert out['total']['value'] == 1
    assert out['parcial'] is False
    assert out['resultados'][0]['trechos'] == ['<em>precatório</em> expedido']


def test_buscar_diarios_erro_do_es_vira_error(monkeypatch):
    from mcp_server import delegates
    from search import busca_api

    def explode(**kw):
        raise ConnectionError('Connection timed out')

    monkeypatch.setattr(busca_api, 'buscar_movimentacoes', explode)
    assert delegates.buscar_diarios('x') == {'error': 'Connection timed out'}


# --------------------------------------------------------------------------- #
# Tools macro, prompts, resources
# --------------------------------------------------------------------------- #
def test_tools_nomes_unicos_e_anotadas():
    from mcp_server import server
    nomes = [t['name'] for t in server.TOOLS]
    assert len(nomes) == len(set(nomes))
    assert set(nomes) == set(server.TOOL_MAP)
    assert {'visao_geral', 'agregar', 'descrever_base', 'dossie_jurimetrico', 'ente_fiscal'} <= set(nomes)
    # anexo do chat do dashboard não tem dono conferível fora do chat
    assert 'ler_arquivo' not in nomes
    anot = {t['name']: t['annotations'] for t in server.TOOLS}
    assert anot['agregar'] == {'readOnlyHint': True, 'openWorldHint': False}
    assert anot['monitorar_termo']['readOnlyHint'] is False
    assert anot['capag_rating']['openWorldHint'] is True


@pytest.mark.django_db
def test_mcp_prompts_e_templates(client):
    token = _cliente_mcp()
    init = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
    assert 'prompts' in init.json()['result']['capabilities']
    assert 'agregar' in init.json()['result']['instructions']

    lista = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 1, 'method': 'prompts/list'})
    nomes = {p['name'] for p in lista.json()['result']['prompts']}
    assert {'panorama_do_acervo', 'responder_pergunta', 'analisar_processo', 'perfil_devedor'} <= nomes

    get = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 2, 'method': 'prompts/get',
                                    'params': {'name': 'analisar_processo',
                                               'arguments': {'cnj': '0000001-00.2026.8.26.0001'}}})
    texto = get.json()['result']['messages'][0]['content']['text']
    assert '0000001-00.2026.8.26.0001' in texto

    falta = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 3, 'method': 'prompts/get',
                                      'params': {'name': 'analisar_processo', 'arguments': {}}})
    assert falta.json()['error']['code'] == -32602

    tpl = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 4, 'method': 'resources/templates/list'})
    uris = {t['uriTemplate'] for t in tpl.json()['result']['resourceTemplates']}
    assert 'voyager://processo/{cnj}' in uris

    rec = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 5, 'method': 'resources/list'})
    assert all('{' not in r['uri'] for r in rec.json()['result']['resources'])

    base = _post_mcp(client, token, {'jsonrpc': '2.0', 'id': 6, 'method': 'resources/read',
                                     'params': {'uri': 'voyager://base'}})
    assert 'agregar' in base.json()['result']['contents'][0]['text']


def _resp_es(total=0, aggs=None, timed_out=False):
    return {'took': 3, 'timed_out': timed_out, 'hits': {'total': {'value': total, 'relation': 'eq'}, 'hits': []},
            'aggregations': aggs or {}}


def test_agregar_monta_query_e_le_grupos(monkeypatch):
    from mcp_server import macro
    visto = {}

    def fake(indice, body, **kw):
        visto.update(indice=indice, body=body)
        return _resp_es(1000, {
            'm': {'value': 5e6},
            'g1': {'sum_other_doc_count': 40, 'doc_count_error_upper_bound': 0, 'buckets': [
                {'key': 'TJSP', 'doc_count': 700, 'm': {'value': 4e6}},
                {'key': '__vazio__', 'doc_count': 260, 'm': {'value': 1e6}},
            ]},
        })

    monkeypatch.setattr(macro, '_search', fake)
    out = macro.agregar(indice='processos', agrupar_por=['tribunal'], filtros={'classificacao': 'precatorio'},
                        faixas={'valor_causa': {'gte': 1000}}, metrica='soma', top=2)

    body = visto['body']
    assert body['track_total_hits'] is True
    assert body['timeout'] == macro.ES_TIMEOUT
    assert {'term': {'classificacao': 'PRECATORIO'}} in body['query']['bool']['filter']
    assert {'range': {'valor_causa': {'gte': 1000}}} in body['query']['bool']['filter']
    assert body['aggs']['g1']['terms']['size'] == 2
    assert out['total'] == 1000
    assert out['valor'] == 5e6
    assert out['grupos'][0] == {'chave': 'TJSP', 'n': 700, 'valor': 4e6}
    assert out['grupos'][1]['chave'] is None
    # o que ficou fora do top-N aparece, em vez de a resposta parecer o universo
    assert out['outros'] == 40
    assert any('fora do top' in a for a in out['avisos'])


def test_agregar_campo_numerico_nao_usa_missing_string(monkeypatch):
    from mcp_server import macro
    visto = {}
    monkeypatch.setattr(macro, '_search', lambda i, b, **kw: visto.update(b=b) or _resp_es(1, {'g1': {'buckets': []}}))
    macro.agregar(agrupar_por=['ano_cnj'])
    assert 'missing' not in visto['b']['aggs']['g1']['terms']


def test_agregar_data_vira_histograma_e_parcial_avisa(monkeypatch):
    from mcp_server import macro
    visto = {}

    def fake(indice, body, **kw):
        visto.update(indice=indice, body=body)
        return _resp_es(5, {'g1': {'buckets': [{'key_as_string': '2026-01-01', 'key': 1, 'doc_count': 5}]}},
                        timed_out=True)

    monkeypatch.setattr(macro, '_search', fake)
    out = macro.agregar(indice='publicacoes', agrupar_por=['publish_date'], intervalo='mes', texto='"rpv"')
    assert visto['indice'] == 'publicacoes'
    assert visto['body']['aggs']['g1']['date_histogram']['calendar_interval'] == 'month'
    assert out['grupos'] == [{'chave': '2026-01-01', 'n': 5}]
    assert out['parcial'] is True
    assert any('INCOMPLETOS' in a for a in out['avisos'])


def test_agregar_recusa_campo_fora_da_lista(monkeypatch):
    from mcp_server import macro
    monkeypatch.setattr(macro, '_search', lambda *a: pytest.fail('não deveria consultar o ES'))
    assert 'partes' in macro.agregar(agrupar_por=['partes'])['error']
    assert 'error' in macro.agregar(indice='acervo', metrica='soma')
    assert 'error' in macro.agregar(indice='inexistente')
    assert 'error' in macro.agregar(agrupar_por=['tribunal', 'uf', 'grau'])


def test_visao_geral_cruza_processos_com_acervo(monkeypatch):
    from mcp_server import macro
    from search import agg_overview

    monkeypatch.setattr(agg_overview, '_indice_entidades', lambda: 'voyager-entidades')

    def fake(indice, body, **kw):
        if indice == 'processos':
            return _resp_es(150, {'t': {'buckets': [{'key': 'TJSP', 'doc_count': 100},
                                                     {'key': 'TRF3', 'doc_count': 50}]},
                                  'classif': {'buckets': [{'key': '__sem__', 'doc_count': 90},
                                                          {'key': '', 'doc_count': 10},
                                                          {'key': 'PRECATORIO', 'doc_count': 5}]},
                                  'enriquecido': {'doc_count': 40}, 'sinal': {'doc_count': 7},
                                  'valor': {'value': 1.5e6}, 'com_valor': {'value': 60}})
        if indice == 'acervo':
            return _resp_es(400, {'t': {'buckets': [{'key': 'TJSP', 'doc_count': 400}]}})
        if indice == 'publicacoes':
            return _resp_es(9, {'ate_hoje': {'ultima': {'value_as_string': '2026-10-14'}},
                                'futuras': {'doc_count': 2}, 'd7': {'doc_count': 3}})
        return _resp_es(11)

    esperas = set()

    def fake_com_espera(indice, body, request_timeout=None):
        esperas.add(request_timeout)
        return fake(indice, body)

    monkeypatch.setattr(macro, '_search', fake_com_espera)
    monkeypatch.setattr(macro.cache, 'get', lambda k: None)
    monkeypatch.setattr(macro.cache, 'set', lambda *a, **k: None)
    out = macro.visao_geral()
    assert esperas == {macro.VISAO_GERAL_REQUEST_TIMEOUT}

    assert out['processos']['total'] == 150
    # string vazia e campo ausente são o mesmo "não classificado"
    assert out['processos']['por_classificacao'] == {'SEM_CLASSIFICACAO': 100, 'PRECATORIO': 5}
    assert out['publicacoes']['com_data_no_futuro'] == 2
    assert out['entidades']['total'] == 11
    linhas = {ln['tribunal']: ln for ln in out['por_tribunal']}
    assert linhas['TJSP'] == {'tribunal': 'TJSP', 'processos_indexados': 100, 'acervo_cnj': 400, 'razao_pct': 25.0}
    assert 'razao_pct' not in linhas['TRF3']
    assert 'erros' not in out


def test_visao_geral_nao_cacheia_resultado_com_erro(monkeypatch):
    from mcp_server import macro
    from search import agg_overview

    monkeypatch.setattr(agg_overview, '_indice_entidades', lambda: 'voyager-entidades')
    salvos = []
    monkeypatch.setattr(macro.cache, 'get', lambda k: None)
    monkeypatch.setattr(macro.cache, 'set', lambda *a, **k: salvos.append(a))

    def fake(indice, body, **kw):
        if indice == 'acervo':
            raise ConnectionError('timeout')
        return _resp_es(1)

    monkeypatch.setattr(macro, '_search', fake)
    out = macro.visao_geral()
    assert 'acervo' in out['erros']
    assert salvos == []


@pytest.mark.django_db
def test_mcp_sem_barra_nao_redireciona(client):
    token = _cliente_mcp()
    resp = client.post('/mcp', data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}),
                       content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {token}')
    assert resp.status_code == 200
    assert 'tools' in resp.json()['result']


def test_agregar_ordenado_por_valor_avisa_top_aproximado(monkeypatch):
    from mcp_server import macro
    monkeypatch.setattr(macro, '_search', lambda i, b, **kw: _resp_es(3, {
        'm': {'value': 9.0},
        'g1': {'doc_count_error_upper_bound': -1, 'buckets': [{'key': 2020, 'doc_count': 3, 'm': {'value': 9.0}}]}}))
    out = macro.agregar(agrupar_por=['ano_cnj'], metrica='soma', ordenar='valor')
    assert 'erro_max_contagem' not in out
    assert any('aproximado' in a for a in out['avisos'])


def test_agregar_timeout_vira_mensagem_util_e_texto_em_publicacoes_espera_mais(monkeypatch):
    from elastic_transport import ConnectionTimeout

    from mcp_server import macro
    esperas = []

    def lento(indice, body, request_timeout=None):
        esperas.append(request_timeout)
        raise ConnectionTimeout('Connection timed out')

    monkeypatch.setattr(macro, '_search', lento)
    out = macro.agregar(indice='publicacoes', texto='"rpv"', agrupar_por=['tribunal'])
    assert 'Estreite' in out['error']
    macro.agregar(indice='processos', agrupar_por=['tribunal'])
    assert esperas == [macro.REQUEST_TIMEOUT_TEXTO_PUBLICACOES, macro.REQUEST_TIMEOUT]


def test_buscar_diarios_total_piso_ganha_nota(monkeypatch):
    from mcp_server import delegates
    from search import busca_api
    monkeypatch.setattr(busca_api, 'buscar_movimentacoes', lambda **kw: {
        'total': {'value': 10000, 'relation': 'gte'}, 'parcial': False, 'results': []})
    out = delegates.buscar_diarios('precatório')
    assert 'piso' in out['nota_total']
