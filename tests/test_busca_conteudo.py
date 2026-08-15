"""Busca NO TEXTO das publicações — o acervo que a tela não alcançava.

Medido em 15/08/2026 no índice de 1,16 bilhão de publicações: 94.065.570 citam
"poder judiciário", 61.747.787 citam "sentença", 3.086.016 citam "precatório".
Tudo isso estava indexado e servido pela API externa — e nenhuma tela consultava,
porque a busca só olhava o índice de PROCESSOS.

Estes testes travam as duas coisas que fazem a diferença: o endpoint existir com
o contrato certo, e a tela mandar a consulta pro índice CERTO conforme o escopo.
"""
from unittest.mock import patch

import pytest
from django.urls import reverse

from search import busca_ui as ui
from search.busca_api import BuscaParamError

URL = 'dashboard:busca-conteudo'


@pytest.fixture(autouse=True)
def cache_limpo():
    """O endpoint cacheia por querystring. Sem limpar entre testes, um teste que
    guardou 200 faz o seguinte (que espera erro) ler o cache e passar/falhar por
    motivo errado — foi o que aconteceu ao rodar a suíte inteira."""
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def logado(client, django_user_model):
    u = django_user_model.objects.create_user('conteudo@t.local', password='x' * 12)
    client.force_login(u)
    return client


def test_url_resolve():
    assert reverse(URL) == '/dashboard/api/busca/conteudo/'


@pytest.mark.django_db
def test_exige_login(client):
    r = client.get(reverse(URL), {'q': 'precatório'})
    assert r.status_code in (302, 403)


@pytest.mark.django_db
def test_busca_no_texto_devolve_publicacao_com_trecho(logado):
    """A unidade do resultado é a PUBLICAÇÃO, com highlight — é isso que
    responde "onde apareceu esta palavra", diferente da busca de processos."""
    falso = {
        'versao': 'v1',
        'total': {'value': 3_086_016, 'relation': 'eq'},
        'results': [{'proc': '5229078-89.2022.8.13.0024', 'tribunal': 'TJMG',
                     'highlight': ['…expedição de <em>precatório</em>…']}],
        'size': 20, 'next_cursor': None,
    }
    with patch.object(ui.ba, 'buscar_movimentacoes', return_value=dict(falso)) as m:
        r = logado.get(reverse(URL), {'q': 'precatório'})
    assert r.status_code == 200
    d = r.json()
    assert d['total']['value'] == 3_086_016
    assert 'highlight' in d['results'][0]
    assert m.call_args.kwargs['q'] == 'precatório'


@pytest.mark.django_db
def test_sem_criterio_e_recusado_com_400(logado):
    """1,16 bilhão de docs sem filtro é varredura cega. Recusar é a resposta
    certa — e tem que ser 400 (critério), não 503 (infra)."""
    with patch.object(ui.ba, 'buscar_movimentacoes',
                      side_effect=BuscaParamError('informe_q_proc_ou_tribunal')):
        r = logado.get(reverse(URL))
    assert r.status_code == 400
    assert r.json()['erro'] == 'informe_q_proc_ou_tribunal'


@pytest.mark.django_db
def test_es_fora_vira_503_e_nao_500(logado):
    with patch.object(ui.ba, 'buscar_movimentacoes', side_effect=RuntimeError('ES fora')):
        r = logado.get(reverse(URL), {'q': 'sentença'})
    assert r.status_code == 503
    assert r.json()['erro'] == 'elasticsearch_indisponivel'


@pytest.mark.django_db
def test_resposta_avisa_a_composicao_do_acervo(logado):
    """82,9% do acervo são rótulos curtos de andamento ("Conclusão · para
    despacho"), não peça integral. A tela precisa dizer isso, senão quem busca
    "sentença" acha que está lendo a sentença."""
    with patch.object(ui.ba, 'buscar_movimentacoes',
                      return_value={'total': {'value': 1}, 'results': []}):
        r = logado.get(reverse(URL), {'q': 'sentença'})
    assert 'aviso' in r.json()['cobertura']


@pytest.mark.django_db
def test_pagina_publica_a_url_dos_dois_escopos(logado):
    """Sem a URL no shell, o botão "No texto" cairia no default hardcoded e
    quebraria calado se a rota mudasse."""
    r = logado.get(reverse('dashboard:busca'))
    corpo = r.content.decode()
    assert '/dashboard/api/busca/conteudo/' in corpo
    assert '/dashboard/api/busca/processos/' in corpo
    assert "escopo: 'processos'" in corpo
