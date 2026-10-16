"""OAuth 2.1 do conector do Claude (mcp_server/oauth.py): o fluxo inteiro,
do 401 com desafio até o refresh, e as recusas que importam."""
import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.contrib.auth import get_user_model

from mcp_server import tokens
from mcp_server.models import MCPAcesso

BASE = 'https://voyager.test'
CALLBACK = 'https://claude.ai/api/mcp/auth_callback'


@pytest.fixture(autouse=True)
def _oauth(settings):
    settings.MCP_PUBLIC_BASE_URL = BASE
    settings.MCP_RATE_LIMIT_RPM = 0
    for k in tokens.r().scan_iter('mcp:oauth:*'):
        tokens.r().delete(k)
    yield


@pytest.fixture
def usuario(db):
    user = get_user_model().objects.create_user('ana@x.com', 'ana@x.com', 'senha-forte-123')
    MCPAcesso.objects.create(usuario=user)
    return user


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def _registrar(client, redirect=CALLBACK):
    resp = client.post('/oauth/register', data=json.dumps({'client_name': 'Claude', 'redirect_uris': [redirect]}),
                       content_type='application/json')
    assert resp.status_code == 201, resp.content
    return resp.json()['client_id']


def _params(client_id, challenge, redirect=CALLBACK):
    return {'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect, 'state': 'st-1',
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'resource': f'{BASE}/mcp'}


def _autorizar(client, usuario, client_id, challenge):
    client.force_login(usuario)
    resp = client.get('/oauth/authorize', _params(client_id, challenge))
    assert resp.status_code == 200
    ticket = resp.context['ticket']
    resp = client.post('/oauth/authorize', {'ticket': ticket, 'decisao': 'autorizar'})
    assert resp.status_code == 302
    q = parse_qs(urlparse(resp['Location']).query)
    assert resp['Location'].startswith(CALLBACK)
    assert q['state'] == ['st-1'] and q['iss'] == [BASE]
    return q['code'][0]


def _token(client, dados):
    return client.post('/oauth/token', urlencode(dados), content_type='application/x-www-form-urlencoded')


def _trocar(client, client_id, code, verifier):
    return _token(client, {'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier,
                           'client_id': client_id, 'redirect_uri': CALLBACK})


def _mcp(client, access):
    return client.post('/mcp', data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}),
                       content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {access}')


def _conectar(client, usuario):
    client_id = _registrar(client)
    verifier, challenge = _pkce()
    code = _autorizar(client, usuario, client_id, challenge)
    resp = _trocar(client, client_id, code, verifier)
    assert resp.status_code == 200, resp.content
    return client_id, resp.json()


@pytest.mark.django_db
def test_sem_token_401_com_desafio_e_metadata(client):
    resp = client.post('/mcp', data='{}', content_type='application/json')
    assert resp.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in resp['WWW-Authenticate']

    prm = client.get('/.well-known/oauth-protected-resource/mcp').json()
    assert prm['resource'] == f'{BASE}/mcp' and prm['authorization_servers'] == [BASE]
    asm = client.get('/.well-known/oauth-authorization-server').json()
    assert asm['issuer'] == BASE
    assert asm['code_challenge_methods_supported'] == ['S256']
    assert asm['registration_endpoint'] == f'{BASE}/oauth/register'


@pytest.mark.django_db
def test_oauth_desligado_sem_base_url(client, settings):
    settings.MCP_PUBLIC_BASE_URL = ''
    assert client.get('/.well-known/oauth-authorization-server').status_code == 503
    resp = client.post('/mcp', data='{}', content_type='application/json')
    assert resp.status_code == 401 and 'WWW-Authenticate' not in resp


@pytest.mark.django_db
def test_fluxo_completo_e_refresh_rotativo(client, usuario):
    client_id, par = _conectar(client, usuario)
    assert par['token_type'] == 'Bearer' and par['expires_in'] == 3600

    resp = _mcp(client, par['access_token'])
    assert resp.status_code == 200
    assert any(t['name'] == 'visao_geral' for t in resp.json()['result']['tools'])

    novo = _token(client, {'grant_type': 'refresh_token', 'refresh_token': par['refresh_token'],
                                        'client_id': client_id})
    assert novo.status_code == 200
    assert _mcp(client, novo.json()['access_token']).status_code == 200

    # Refresh antigo reapresentado = roubo: a família inteira cai, inclusive o par novo.
    reuso = _token(client, {'grant_type': 'refresh_token', 'refresh_token': par['refresh_token']})
    assert reuso.json()['error'] == 'invalid_grant'
    assert _mcp(client, novo.json()['access_token']).status_code == 401


@pytest.mark.django_db
def test_code_de_uso_unico_e_pkce(client, usuario):
    client_id = _registrar(client)
    verifier, challenge = _pkce()
    code = _autorizar(client, usuario, client_id, challenge)
    assert _trocar(client, client_id, code, 'verifier-errado').json()['error'] == 'invalid_grant'
    # O code com PKCE errado já foi consumido (GETDEL).
    assert _trocar(client, client_id, code, verifier).json()['error'] == 'invalid_grant'

    code = _autorizar(client, usuario, client_id, challenge)
    primeiro = _trocar(client, client_id, code, verifier).json()
    assert _trocar(client, client_id, code, verifier).json()['error'] == 'invalid_grant'
    # Code reapresentado derruba o que a primeira troca emitiu.
    assert _mcp(client, primeiro['access_token']).status_code == 401


@pytest.mark.django_db
def test_authorize_sem_sessao_manda_pro_login(client):
    client_id = _registrar(client)
    _, challenge = _pkce()
    resp = client.get('/oauth/authorize', _params(client_id, challenge))
    assert resp.status_code == 302
    assert resp['Location'].startswith('/dashboard/login/?next=/oauth/authorize')


@pytest.mark.django_db
def test_sem_mcpacesso_nao_autoriza(client, db):
    outro = get_user_model().objects.create_user('bob@x.com', 'bob@x.com', 'senha-forte-123')
    client.force_login(outro)
    client_id = _registrar(client)
    _, challenge = _pkce()
    resp = client.get('/oauth/authorize', _params(client_id, challenge))
    assert resp.status_code == 403
    assert b'/oauth/trocar-conta' in resp.content
    assert resp['X-Frame-Options'] == 'DENY'


@pytest.mark.django_db
def test_pedido_invalido_nao_redireciona(client, usuario):
    client.force_login(usuario)
    client_id = _registrar(client)
    _, challenge = _pkce()
    params = _params(client_id, challenge)
    for ruim in ({'redirect_uri': 'https://evil.com/cb'}, {'code_challenge_method': 'plain'},
                 {'resource': 'https://outro.com/mcp'}, {'client_id': 'nao-existe'}):
        resp = client.get('/oauth/authorize', {**params, **ruim})
        assert resp.status_code == 400, ruim


@pytest.mark.django_db
def test_cancelar_volta_access_denied(client, usuario):
    client.force_login(usuario)
    client_id = _registrar(client)
    _, challenge = _pkce()
    ticket = client.get('/oauth/authorize', _params(client_id, challenge)).context['ticket']
    resp = client.post('/oauth/authorize', {'ticket': ticket, 'decisao': 'cancelar'})
    assert 'error=access_denied' in resp['Location']


@pytest.mark.django_db
def test_dcr_recusa_redirect_desconhecido_e_cliente_confidencial(client):
    resp = client.post('/oauth/register', data=json.dumps({'redirect_uris': ['https://evil.com/cb']}),
                       content_type='application/json')
    assert resp.json()['error'] == 'invalid_redirect_uri'
    resp = client.post('/oauth/register', data=json.dumps({'redirect_uris': [CALLBACK + '\n']}),
                       content_type='application/json')
    assert resp.status_code == 400
    resp = client.post('/oauth/register', data=json.dumps({
        'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': 'client_secret_basic'}),
        content_type='application/json')
    assert resp.status_code == 400
    assert _registrar(client, 'http://localhost:53682/callback')


@pytest.mark.django_db
def test_revogar_acesso_derruba_sessao_e_refresh(client, usuario):
    _, par = _conectar(client, usuario)
    MCPAcesso.objects.filter(usuario=usuario).update(ativo=False)
    # Cache do "permitido" ainda vale; o refresh confere sem cache.
    resp = _token(client, {'grant_type': 'refresh_token', 'refresh_token': par['refresh_token']})
    assert resp.json()['error'] == 'invalid_grant'
    assert _mcp(client, par['access_token']).status_code == 401


@pytest.mark.django_db
def test_origin_estrangeira_403(client, usuario):
    _, par = _conectar(client, usuario)
    resp = client.post('/mcp', data='{}', content_type='application/json',
                       HTTP_AUTHORIZATION=f'Bearer {par["access_token"]}', HTTP_ORIGIN='https://evil.com')
    assert resp.status_code == 403
    resp = client.post('/mcp', data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}),
                       content_type='application/json',
                       HTTP_AUTHORIZATION=f'Bearer {par["access_token"]}', HTTP_ORIGIN='https://claude.ai')
    assert resp.status_code == 200


@pytest.mark.django_db
def test_token_legado_de_apiclient_continua_valendo(client):
    import uuid

    from tribunals.models import ApiClient
    token = uuid.uuid4()
    ApiClient.objects.create(nome='legado', api_key='k-legado', ativo=True, mcp_token=token)
    assert _mcp(client, str(token)).status_code == 200


@pytest.mark.django_db
def test_trocar_conta_desloga_e_volta_ao_login(client, usuario):
    client.force_login(usuario)
    volta = '/oauth/authorize?client_id=x&state=y'
    resp = client.post('/oauth/trocar-conta', {'volta': volta})
    assert resp.status_code == 302 and resp['Location'].startswith('/dashboard/login/?next=')
    assert '_auth_user_id' not in client.session
    assert client.post('/oauth/trocar-conta', {'volta': 'https://evil.com'}).status_code == 400


@pytest.mark.django_db
def test_consentimento_passa_no_csrf_de_verdade(usuario, settings):
    """O POST do "Autorizar" sai da nossa tela com o CSRF do Django ligado."""
    from django.test import Client
    c = Client(enforce_csrf_checks=True)
    client_id = _registrar(c)
    _, challenge = _pkce()
    c.force_login(usuario)
    resp = c.get('/oauth/authorize', _params(client_id, challenge))
    assert resp['Referrer-Policy'] == 'same-origin'
    csrf = resp.context['csrf_token']
    sem = c.post('/oauth/authorize', {'ticket': resp.context['ticket'], 'decisao': 'autorizar'})
    assert sem.status_code == 403
    resp = c.get('/oauth/authorize', _params(client_id, challenge))
    ok = c.post('/oauth/authorize', {'ticket': resp.context['ticket'], 'decisao': 'autorizar',
                                     'csrfmiddlewaretoken': str(csrf)})
    assert ok.status_code == 302 and 'code=' in ok['Location']
