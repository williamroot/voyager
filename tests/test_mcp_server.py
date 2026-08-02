import json
import uuid

import pytest

from tribunals.models import ApiClient


@pytest.mark.django_db
def test_mcp_descriptor(client):
    resp = client.get('/mcp/.well-known/mcp.json')
    assert resp.status_code == 200
    data = resp.json()
    assert data['name'] == 'voyager'
    assert 'tools' in data
    assert len(data['tools']) == 12


@pytest.mark.django_db
def test_mcp_descriptor_lista_todas_tools(client):
    resp = client.get('/mcp/.well-known/mcp.json')
    data = resp.json()
    tool_names = [t['name'] for t in data['tools']]
    expected = {
        'buscar_diarios', 'get_documento', 'get_processo', 'list_movimentacoes',
        'get_partes', 'listar_fontes', 'status_cobertura', 'monitorar_termo',
        'monitorar_processo', 'listar_detections', 'get_pdf', 'classificacao_lead',
    }
    assert expected.issubset(set(tool_names))


@pytest.mark.django_db
def test_mcp_messages_sem_token_403(client):
    resp = client.post('/mcp/messages', data=json.dumps({'method': 'tools/list', 'id': 1}),
                        content_type='application/json')
    assert resp.status_code == 403


@pytest.mark.django_db
def test_mcp_messages_token_invalido_403(client):
    resp = client.post('/mcp/messages',
                        data=json.dumps({'method': 'tools/list', 'id': 1}),
                        content_type='application/json',
                        HTTP_AUTHORIZATION='Bearer token-invalido')
    assert resp.status_code == 403


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
    assert len(data['result']['tools']) == 12


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
    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    FonteDiario.objects.create(
        source_id=1, tribunal=t, diario_slug='dje-trf1',
        orgao_slug='trf1', caderno_slug='', nome='TRF - 1ª Reg.',
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
    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
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