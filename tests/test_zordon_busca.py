"""Testes da view de busca semântica no acervo (Zordon).

Cobertura:
- GET shell sem HX-Request → renderiza a página com caixa de busca
- GET com HX-Request + q → chama zordon_client.buscar e renderiza cards
- GET com HX-Request + q vazia → renderiza estado vazio (sem query)
- GET com HX-Request + q + lista vazia → estado "nenhum resultado"
- GET com HX-Request + q + erro → renderiza mensagem amigável de falha
- Redireciona para login se não autenticado

zordon_client.buscar é mockado em todos os casos — não precisa do Zordon
no ar.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()

BUSCA_URL = reverse('dashboard:acervo-busca')

_RESULTADO_MOCK = {
    'doc_tipo': 'movimentacao',
    'numero_cnj': '0001234-56.2023.5.00.0000',
    'score': 0.91,
    'snippet': 'Precatório federal expedido contra a União.',
}


@pytest.fixture
def usuario(db):
    return User.objects.create_user(username='tester_zordon', password='senha123')


@pytest.fixture
def client_logado(usuario):
    c = Client()
    c.login(username='tester_zordon', password='senha123')
    return c


# ---------- helper ----------

def _htmx(client, url, **params):
    """GET HTMX: adiciona o header HX-Request."""
    return client.get(url, params, HTTP_HX_REQUEST='true')


# ---------- testes ----------

class TestAcervoBuscaShell:
    """GET sem HX-Request → página shell completa."""

    def test_redireciona_se_nao_autenticado(self):
        c = Client()
        resp = c.get(BUSCA_URL)
        assert resp.status_code == 302
        assert '/login/' in resp['Location']

    def test_shell_renderiza_caixa_de_busca(self, client_logado):
        resp = client_logado.get(BUSCA_URL)
        assert resp.status_code == 200
        content = resp.content.decode()
        # Formulário presente
        assert 'hx-get' in content
        assert 'acervo-resultados' in content
        # Campo de busca
        assert 'name="q"' in content

    def test_shell_usa_template_base(self, client_logado):
        resp = client_logado.get(BUSCA_URL)
        assert resp.status_code == 200
        templates = [t.name for t in resp.templates]
        assert 'dashboard/acervo_busca.html' in templates
        assert 'dashboard/base.html' in templates

    def test_shell_sem_htmx_nao_chama_zordon(self, client_logado):
        with patch('dashboard.zordon_client.buscar') as mock_buscar:
            client_logado.get(BUSCA_URL, {'q': 'precatório'})
            mock_buscar.assert_not_called()


class TestAcervoBuscaHTMX:
    """GET com HX-Request → partial de resultados."""

    def test_query_vazia_exibe_estado_inicial(self, client_logado):
        resp = _htmx(client_logado, BUSCA_URL)
        assert resp.status_code == 200
        templates = [t.name for t in resp.templates]
        assert 'dashboard/_partials/_acervo_resultados.html' in templates
        # Estado vazio: sem resultados, sem erro
        content = resp.content.decode()
        assert 'acervo-resultados' in content
        assert 'Falha ao consultar' not in content

    def test_resultados_renderiza_cards(self, client_logado):
        payload = {'results': [_RESULTADO_MOCK], 'erro': None}
        with patch('dashboard.zordon_client.buscar', return_value=payload):
            resp = _htmx(client_logado, BUSCA_URL, q='precatório federal')

        assert resp.status_code == 200
        content = resp.content.decode()
        assert '0001234-56.2023.5.00.0000' in content
        assert 'Precatório federal expedido contra a União.' in content
        assert '0.91' in content

    def test_multiplos_resultados(self, client_logado):
        r2 = dict(_RESULTADO_MOCK, numero_cnj='0009999-99.2024.5.00.0000', score=0.75)
        payload = {'results': [_RESULTADO_MOCK, r2], 'erro': None}
        with patch('dashboard.zordon_client.buscar', return_value=payload):
            resp = _htmx(client_logado, BUSCA_URL, q='precatório')

        content = resp.content.decode()
        assert '0001234-56.2023.5.00.0000' in content
        assert '0009999-99.2024.5.00.0000' in content
        assert '2 resultado' in content

    def test_lista_vazia_exibe_estado_sem_resultado(self, client_logado):
        payload = {'results': [], 'erro': None}
        with patch('dashboard.zordon_client.buscar', return_value=payload):
            resp = _htmx(client_logado, BUSCA_URL, q='xyzzy irrelevante')

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'Nenhum resultado encontrado' in content

    def test_erro_zordon_exibe_mensagem_amigavel(self, client_logado):
        payload = {'results': [], 'erro': 'Serviço de busca indisponível (falha de conexão)'}
        with patch('dashboard.zordon_client.buscar', return_value=payload):
            resp = _htmx(client_logado, BUSCA_URL, q='precatório')

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'Falha ao consultar o acervo' in content
        assert 'Serviço de busca indisponível' in content

    def test_partial_usa_template_correto(self, client_logado):
        payload = {'results': [], 'erro': None}
        with patch('dashboard.zordon_client.buscar', return_value=payload):
            resp = _htmx(client_logado, BUSCA_URL, q='teste')

        templates = [t.name for t in resp.templates]
        assert 'dashboard/_partials/_acervo_resultados.html' in templates
        # Não deve renderizar o base inteiro
        assert 'dashboard/base.html' not in templates


class TestZordonClient:
    """Testes unitários do zordon_client (sem Django DB necessário)."""

    def test_buscar_retorna_resultados_em_sucesso(self, settings):
        import requests as req_lib
        from dashboard.zordon_client import buscar

        settings.ZORDON_URL = 'http://zordon:8011'
        settings.ZORDON_API_KEY = ''

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = (
            b'{"results": [{"doc_tipo": "movimentacao",'
            b' "numero_cnj": "0000001-00.2023.5.00.0000",'
            b' "score": 0.9, "snippet": "texto"}]}'
        )
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.zordon_client.requests.get', return_value=mock_resp):
            resultado = buscar('precatório')

        assert resultado['erro'] is None
        assert len(resultado['results']) == 1
        assert resultado['results'][0]['score'] == 0.9

    def test_buscar_degrada_em_connection_error(self, settings):
        import requests as req_lib
        from dashboard.zordon_client import buscar

        settings.ZORDON_URL = 'http://zordon:8011'

        with patch('dashboard.zordon_client.requests.get',
                   side_effect=req_lib.exceptions.ConnectionError('refused')):
            resultado = buscar('qualquer coisa')

        assert resultado['results'] == []
        assert resultado['erro'] is not None
        assert 'conexão' in resultado['erro'].lower() or 'indisponível' in resultado['erro'].lower()

    def test_buscar_degrada_em_timeout(self, settings):
        import requests as req_lib
        from dashboard.zordon_client import buscar

        settings.ZORDON_URL = 'http://zordon:8011'

        with patch('dashboard.zordon_client.requests.get',
                   side_effect=req_lib.exceptions.Timeout('timed out')):
            resultado = buscar('qualquer coisa')

        assert resultado['results'] == []
        assert 'tempo' in resultado['erro'].lower() or 'timeout' in resultado['erro'].lower()

    def test_buscar_retorna_erro_quando_zordon_url_vazio(self, settings):
        from dashboard.zordon_client import buscar

        settings.ZORDON_URL = ''
        resultado = buscar('qualquer coisa')

        assert resultado['results'] == []
        assert 'ZORDON_URL' in resultado['erro']

    def test_buscar_passa_api_key_no_header(self, settings):
        import requests as req_lib
        from dashboard.zordon_client import buscar

        settings.ZORDON_URL = 'http://zordon:8011'
        settings.ZORDON_API_KEY = 'minha-chave-secreta'

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = b'{"results": []}'
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.zordon_client.requests.get', return_value=mock_resp) as mock_get:
            buscar('teste')

        call_kwargs = mock_get.call_args[1]
        assert call_kwargs['headers']['Authorization'] == 'Api-Key minha-chave-secreta'

    def test_buscar_sem_api_key_nao_envia_header(self, settings):
        import requests as req_lib
        from dashboard.zordon_client import buscar

        settings.ZORDON_URL = 'http://zordon:8011'
        settings.ZORDON_API_KEY = ''

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = b'{"results": []}'
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.zordon_client.requests.get', return_value=mock_resp) as mock_get:
            buscar('teste')

        call_kwargs = mock_get.call_args[1]
        assert 'Authorization' not in call_kwargs['headers']
