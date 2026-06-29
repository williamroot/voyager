"""Testes da view acervo_teor e das funções gordon_client.extrair/chunks.

Cobertura:
- acervo_teor: renderiza campos quando extração OK
- acervo_teor: exibe aviso "não indexado" quando erro="sem_contexto"
- acervo_teor: exibe erro amigável quando Gordon offline (erro genérico)
- acervo_teor: redireciona para login se não autenticado
- acervo_teor: não chama chunks quando extração falha
- gordon_client.extrair: retorna campos em sucesso
- gordon_client.extrair: degrada em ConnectionError
- gordon_client.extrair: mapeia 404 HTTP para sem_contexto
- gordon_client.extrair: degrada em Timeout
- gordon_client.extrair: retorna erro quando GORDON_URL vazio
- gordon_client.chunks: retorna lista em sucesso
- gordon_client.chunks: degrada em ConnectionError

gordon_client.extrair e gordon_client.chunks são mockados nos testes de view
— não precisa do Gordon no ar.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests as req_lib
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from tribunals.models import Process, Tribunal

pytestmark = pytest.mark.django_db

User = get_user_model()

CNJ_MOCK = '0001234-56.2023.8.26.0000'

_EXTRACAO_MOCK = {
    'natureza': 'Precatório',
    'valor_principal': 150000.00,
    'valor_juros_mora': 12500.50,
    'data_oficio': '2024-03-15',
    'numero_parcelas_rra': 3,
    'fundamento_resumo': 'Benefício previdenciário LOAS, art. 203 CF.',
    'confianca': 0.91,
    'erro': None,
}

_CHUNKS_MOCK = {
    'chunks': [
        {'id': 'abc1', 'texto': 'Trecho inicial do ofício...', 'pagina': 1},
        {'id': 'abc2', 'texto': 'Continuação do documento...', 'pagina': 2},
    ],
    'erro': None,
}


# ---------- fixtures ----------

@pytest.fixture
def tribunal(db):
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP',
        defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP', 'ativo': True},
    )
    return t


@pytest.fixture
def processo(tribunal):
    return Process.objects.create(
        tribunal=tribunal,
        numero_cnj=CNJ_MOCK,
    )


@pytest.fixture
def usuario(db):
    return User.objects.create_user(username='tester_teor', password='senha123')


@pytest.fixture
def client_logado(usuario):
    c = Client()
    c.login(username='tester_teor', password='senha123')
    return c


def _teor_url(cnj=CNJ_MOCK):
    return reverse('dashboard:acervo-teor', kwargs={'cnj': cnj})


# ---------- testes de view ----------

class TestAcervoTeorView:
    """Testes funcionais da view acervo_teor."""

    def test_redireciona_se_nao_autenticado(self):
        c = Client()
        resp = c.get(_teor_url())
        assert resp.status_code == 302
        assert '/login/' in resp['Location']

    def test_renderiza_campos_quando_extracao_ok(self, client_logado):
        extr = dict(_EXTRACAO_MOCK)
        with (
            patch('dashboard.gordon_client.extrair', return_value=extr),
            patch('dashboard.gordon_client.chunks', return_value=_CHUNKS_MOCK),
        ):
            resp = client_logado.get(_teor_url())

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'gordon-teor' in content
        assert 'Precatório' in content
        assert '150000' in content or '150,000' in content or '150.000' in content
        assert '0.91' in content
        assert 'Benefício previdenciário' in content
        assert 'dashboard/_partials/_acervo_teor.html' in [t.name for t in resp.templates]

    def test_exibe_chunks_quando_presentes(self, client_logado):
        extr = dict(_EXTRACAO_MOCK)
        with (
            patch('dashboard.gordon_client.extrair', return_value=extr),
            patch('dashboard.gordon_client.chunks', return_value=_CHUNKS_MOCK),
        ):
            resp = client_logado.get(_teor_url())

        content = resp.content.decode()
        assert 'Trecho inicial do ofício' in content
        assert 'Fragmentos do auto' in content

    def test_sem_contexto_exibe_aviso_nao_indexado(self, client_logado):
        with (
            patch('dashboard.gordon_client.extrair', return_value={'erro': 'sem_contexto'}),
        ):
            resp = client_logado.get(_teor_url())

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'não indexado' in content.lower()
        assert 'gordon-teor' in content
        # Não deve mostrar campos de extração
        assert 'Precatório' not in content

    def test_erro_generico_exibe_mensagem_amigavel(self, client_logado):
        payload = {'erro': 'Serviço Gordon indisponível (falha de conexão)'}
        with (
            patch('dashboard.gordon_client.extrair', return_value=payload),
        ):
            resp = client_logado.get(_teor_url())

        assert resp.status_code == 200
        content = resp.content.decode()
        assert 'Gordon indisponível' in content or 'falha de conexão' in content.lower()
        assert 'gordon-teor' in content

    def test_chunks_nao_e_chamado_quando_extracao_falha(self, client_logado):
        payload = {'erro': 'sem_contexto'}
        with (
            patch('dashboard.gordon_client.extrair', return_value=payload),
            patch('dashboard.gordon_client.chunks') as mock_chunks,
        ):
            client_logado.get(_teor_url())

        mock_chunks.assert_not_called()

    def test_chunks_nao_e_chamado_quando_gordon_offline(self, client_logado):
        payload = {'erro': 'Serviço Gordon indisponível (falha de conexão)'}
        with (
            patch('dashboard.gordon_client.extrair', return_value=payload),
            patch('dashboard.gordon_client.chunks') as mock_chunks,
        ):
            client_logado.get(_teor_url())

        mock_chunks.assert_not_called()

    def test_usa_template_partial_correto(self, client_logado):
        extr = dict(_EXTRACAO_MOCK)
        with (
            patch('dashboard.gordon_client.extrair', return_value=extr),
            patch('dashboard.gordon_client.chunks', return_value=_CHUNKS_MOCK),
        ):
            resp = client_logado.get(_teor_url())

        templates = [t.name for t in resp.templates]
        assert 'dashboard/_partials/_acervo_teor.html' in templates
        # Não deve renderizar o layout completo
        assert 'dashboard/base.html' not in templates

    def test_renderiza_sem_chunks_quando_chunks_vazio(self, client_logado):
        extr = dict(_EXTRACAO_MOCK)
        chunks_vazio = {'chunks': [], 'erro': None}
        with (
            patch('dashboard.gordon_client.extrair', return_value=extr),
            patch('dashboard.gordon_client.chunks', return_value=chunks_vazio),
        ):
            resp = client_logado.get(_teor_url())

        assert resp.status_code == 200
        content = resp.content.decode()
        # Campos presentes, mas sem seção de chunks
        assert 'Precatório' in content
        assert 'Fragmentos do auto' not in content


# ---------- testes unitários do gordon_client ----------

class TestGordonClientExtrair:
    """Testes unitários de gordon_client.extrair."""

    def test_extrair_retorna_campos_em_sucesso(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = 'http://gordon:8011'
        settings.GORDON_API_KEY = ''

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = (
            '{"natureza": "Precatório", "valor_principal": 150000.0,'
            ' "confianca": 0.91}'
        ).encode('utf-8')
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.gordon_client.requests.get', return_value=mock_resp):
            resultado = extrair(CNJ_MOCK)

        assert resultado['erro'] is None
        assert resultado['natureza'] == 'Precatório'
        assert resultado['confianca'] == 0.91

    def test_extrair_degrada_em_connection_error(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = 'http://gordon:8011'

        with patch('dashboard.gordon_client.requests.get',
                   side_effect=req_lib.exceptions.ConnectionError('refused')):
            resultado = extrair(CNJ_MOCK)

        assert resultado.get('erro') is not None
        assert 'conexão' in resultado['erro'].lower() or 'indisponível' in resultado['erro'].lower()

    def test_extrair_mapeia_404_para_sem_contexto(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = 'http://gordon:8011'

        mock_resp = req_lib.Response()
        mock_resp.status_code = 404
        mock_resp._content = b'{"detail": "not found"}'
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.gordon_client.requests.get', return_value=mock_resp):
            resultado = extrair(CNJ_MOCK)

        assert resultado['erro'] == 'sem_contexto'

    def test_extrair_propaga_sem_contexto_do_payload(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = 'http://gordon:8011'

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = b'{"erro": "sem_contexto"}'
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.gordon_client.requests.get', return_value=mock_resp):
            resultado = extrair(CNJ_MOCK)

        assert resultado['erro'] == 'sem_contexto'

    def test_extrair_degrada_em_timeout(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = 'http://gordon:8011'

        with patch('dashboard.gordon_client.requests.get',
                   side_effect=req_lib.exceptions.Timeout('timed out')):
            resultado = extrair(CNJ_MOCK)

        assert resultado.get('erro') is not None
        assert 'tempo' in resultado['erro'].lower() or 'timeout' in resultado['erro'].lower()

    def test_extrair_retorna_erro_quando_gordon_url_vazio(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = ''
        resultado = extrair(CNJ_MOCK)

        assert 'GORDON_URL' in resultado['erro']

    def test_extrair_passa_api_key_no_header(self, settings):
        from dashboard.gordon_client import extrair

        settings.GORDON_URL = 'http://gordon:8011'
        settings.GORDON_API_KEY = 'chave-secreta'

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = '{"natureza": "Precatório"}'.encode('utf-8')
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.gordon_client.requests.get', return_value=mock_resp) as mock_get:
            extrair(CNJ_MOCK)

        call_kwargs = mock_get.call_args[1]
        assert call_kwargs['headers']['Authorization'] == 'Api-Key chave-secreta'


class TestGordonClientChunks:
    """Testes unitários de gordon_client.chunks."""

    def test_chunks_retorna_lista_em_sucesso(self, settings):
        from dashboard.gordon_client import chunks

        settings.GORDON_URL = 'http://gordon:8011'
        settings.GORDON_API_KEY = ''

        mock_resp = req_lib.Response()
        mock_resp.status_code = 200
        mock_resp._content = (
            b'{"chunks": [{"id": "c1", "texto": "Trecho...", "pagina": 1}]}'
        )
        mock_resp.encoding = 'utf-8'

        with patch('dashboard.gordon_client.requests.get', return_value=mock_resp):
            resultado = chunks(CNJ_MOCK)

        assert resultado['erro'] is None
        assert len(resultado['chunks']) == 1
        assert resultado['chunks'][0]['id'] == 'c1'

    def test_chunks_degrada_em_connection_error(self, settings):
        from dashboard.gordon_client import chunks

        settings.GORDON_URL = 'http://gordon:8011'

        with patch('dashboard.gordon_client.requests.get',
                   side_effect=req_lib.exceptions.ConnectionError('refused')):
            resultado = chunks(CNJ_MOCK)

        assert resultado['chunks'] == []
        assert resultado.get('erro') is not None

    def test_chunks_retorna_erro_quando_gordon_url_vazio(self, settings):
        from dashboard.gordon_client import chunks

        settings.GORDON_URL = ''
        resultado = chunks(CNJ_MOCK)

        assert resultado['chunks'] == []
        assert 'GORDON_URL' in resultado['erro']
