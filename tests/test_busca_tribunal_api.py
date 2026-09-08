"""Contrato da API de busca por parte (`/api/v1/busca/tribunal/`).

O que estes testes protegem não é o caminho feliz — é a promessa da feature:
que nenhuma das quatro respostas que PARECEM "nenhum processo" seja servida
como se fosse "nenhum processo".

Sem rede: o fan-out é interceptado (`enrichers.busca.jobs.iniciar`), porque o
que se testa aqui é o contrato HTTP, não o scraping — esse tem os testes de
parser sobre fixture real (`test_busca_parte_parsers.py`).
"""
from unittest.mock import patch

import pytest
from django.urls import reverse

from enrichers.busca.entrada import EntradaInvalida, validar
from tribunals.models import ApiClient, BuscaTribunalRun

CPF_VALIDO = '111.444.777-35'


@pytest.fixture
def cliente_api(db):
    return ApiClient.objects.create(nome='teste-busca', api_key='chave-de-teste')


@pytest.fixture
def http(client, cliente_api):
    class _C:
        def post(self, url, corpo):
            return client.post(url, corpo, content_type='application/json',
                               HTTP_X_API_KEY=cliente_api.api_key)

        def get(self, url):
            return client.get(url, HTTP_X_API_KEY=cliente_api.api_key)
    return _C()


# ── entrada: recusar antes de gastar rede ────────────────────────────────────

def test_cpf_com_dv_quebrado_e_400_com_mensagem():
    """Não é "0 resultados": é entrada inválida, e a fonte nem é chamada."""
    with pytest.raises(EntradaInvalida) as erro:
        validar('documento', '111.444.777-00')
    assert erro.value.codigo == 'cpf_dv_invalido'
    assert 'dígito verificador' in erro.value.mensagem


def test_documento_vai_mascarado_para_a_fonte_e_normalizado_para_o_cache():
    saida = validar('documento', '11144477735')
    assert saida['valor'] == '111.444.777-35'      # é como o formulário escreve
    assert saida['normalizado'] == '11144477735'   # é como o cache compara


def test_raiz_de_cnpj_e_recusada_com_explicacao():
    """8 dígitos varrem o índice; nenhum formulário de tribunal os aceita."""
    with pytest.raises(EntradaInvalida) as erro:
        validar('documento', '29979036')
    assert erro.value.codigo == 'documento_incompleto'


def test_nome_curto_nao_vira_requisicao():
    with pytest.raises(EntradaInvalida) as erro:
        validar('nome', 'ana')
    assert erro.value.codigo == 'nome_curto'


def test_oab_aceita_as_tres_grafias():
    for bruto in ('123456/SP', 'SP123456', 'sp 123.456'):
        assert validar('oab', bruto)['normalizado'] == 'SP123456'


# ── POST: cria o run e recusa por critério na porta ──────────────────────────

@pytest.mark.django_db
def test_post_cria_run_e_devolve_202(http):
    url = reverse('busca-tribunal-criar')
    with patch('api.busca_tribunal_views.iniciar',
               return_value={'enfileirados': ['TJSP'], 'recusados': []}) as fan:
        resp = http.post(url, {'criterio': 'documento', 'valor': CPF_VALIDO,
                               'tribunais': ['TJSP']})
    assert resp.status_code == 202
    corpo = resp.json()
    assert corpo['status'] == 'running'
    assert corpo['tribunais'] == ['TJSP']
    assert fan.called
    assert BuscaTribunalRun.objects.filter(pk=corpo['run_id']).exists()


@pytest.mark.django_db
def test_tribunal_sem_busca_e_400_que_diz_quais_existem(http):
    resp = http.post(reverse('busca-tribunal-criar'),
                     {'criterio': 'nome', 'valor': 'MARIA DA SILVA',
                      'tribunais': ['TJRS']})
    assert resp.status_code == 400
    assert resp.json()['erro'] == 'tribunal_sem_busca'
    assert 'TJSP' in resp.json()['mensagem']


@pytest.mark.django_db
def test_criterio_que_a_fonte_nao_tem_vira_recusa_declarada(http):
    """TJPA não busca por nome de advogado — e isso é dito, não silenciado."""
    from enrichers.busca.jobs import iniciar

    run = BuscaTribunalRun.objects.create(
        criterio='advogado', valor='JOAO', valor_normalizado='JOAO',
        tribunais=['TJPA', 'TJSP'])
    with patch('django_rq.get_queue'):
        saida = iniciar(run)
    run.refresh_from_db()
    assert saida['enfileirados'] == ['TJSP']
    assert run.por_tribunal['TJPA']['status'] == 'criterio_indisponivel'
    assert 'não oferece' in run.por_tribunal['TJPA']['mensagem']


# ── GET: parciais, avisos e cache ────────────────────────────────────────────

@pytest.mark.django_db
def test_get_devolve_parciais_enquanto_roda(http):
    run = BuscaTribunalRun.objects.create(
        criterio='nome', valor='MARIA DA SILVA', valor_normalizado='MARIA DA SILVA',
        tribunais=['TJSP', 'TJMG'],
        por_tribunal={'TJSP': {'status': 'ok', 'encontrados': 25},
                      'TJMG': {'status': 'buscando', 'encontrados': 0}},
        resultados=[{'numero_cnj': '1', 'tribunal': 'TJSP'}], encontrados=1)
    resp = http.get(reverse('busca-tribunal-ler', args=[str(run.pk)]))
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo['status'] == 'running'
    assert corpo['encontrados'] == 1
    assert corpo['por_tribunal']['TJMG']['status'] == 'buscando'


@pytest.mark.django_db
def test_avisos_separam_os_quatro_tipos_de_vazio(http):
    run = BuscaTribunalRun.objects.create(
        criterio='documento', valor=CPF_VALIDO, valor_normalizado='11144477735',
        tribunais=['TJSP', 'TJMG', 'TJPA', 'TRF3'],
        status=BuscaTribunalRun.STATUS_CONCLUIDO,
        por_tribunal={
            'TJSP': {'status': 'ok', 'encontrados': 250, 'truncado': True,
                     'motivo_truncagem': 'a fonte limita a resposta a 1000 processos',
                     'total_declarado': 1000},
            'TJMG': {'status': 'refinar', 'mensagem': 'refine sua busca'},
            'TJPA': {'status': 'fonte_indisponivel', 'mensagem': 'timeout'},
            'TRF3': {'status': 'vazio', 'encontrados': 0},
        })
    corpo = http.get(reverse('busca-tribunal-ler', args=[str(run.pk)])).json()
    codigos = {(a['codigo'], a['tribunal']) for a in corpo['avisos']}
    assert ('truncado', 'TJSP') in codigos
    assert ('refinar', 'TJMG') in codigos
    assert ('fonte_indisponivel', 'TJPA') in codigos
    # TRF3 nunca foi conferido ao vivo: o catálogo avisa, mesmo tendo respondido.
    assert ('nao_verificado', 'TRF3') in codigos


@pytest.mark.django_db
def test_mesma_pergunta_em_6h_volta_do_cache_sem_novo_scraping(http):
    anterior = BuscaTribunalRun.objects.create(
        criterio='documento', valor=CPF_VALIDO, valor_normalizado='11144477735',
        tribunais=['TJSP'], status=BuscaTribunalRun.STATUS_CONCLUIDO)
    with patch('api.busca_tribunal_views.iniciar') as fan:
        resp = http.post(reverse('busca-tribunal-criar'),
                         {'criterio': 'documento', 'valor': '11144477735',
                          'tribunais': ['TJSP']})
    assert resp.status_code == 200
    assert resp.json()['run_id'] == str(anterior.pk)
    assert resp.json()['em_cache'] is True
    assert not fan.called


@pytest.mark.django_db
def test_forcar_ignora_o_cache(http):
    BuscaTribunalRun.objects.create(
        criterio='documento', valor=CPF_VALIDO, valor_normalizado='11144477735',
        tribunais=['TJSP'], status=BuscaTribunalRun.STATUS_CONCLUIDO)
    with patch('api.busca_tribunal_views.iniciar',
               return_value={'enfileirados': ['TJSP'], 'recusados': []}):
        resp = http.post(reverse('busca-tribunal-criar'),
                         {'criterio': 'documento', 'valor': CPF_VALIDO,
                          'tribunais': ['TJSP'], 'forcar': True})
    assert resp.status_code == 202
    assert resp.json()['em_cache'] is False


@pytest.mark.django_db
def test_run_inexistente_e_404_limpo(http):
    resp = http.get(reverse('busca-tribunal-ler',
                            args=['00000000-0000-0000-0000-000000000000']))
    assert resp.status_code == 404


# ── catálogo: quem decide o seletor é o servidor ─────────────────────────────

@pytest.mark.django_db
def test_catalogo_diz_o_que_cada_fonte_aceita_e_desde_quando(http):
    corpo = http.get(reverse('busca-tribunal-catalogo')).json()
    por_sigla = {t['tribunal']: t for t in corpo['tribunais']}
    assert por_sigla['TJSP']['teto_da_fonte'] == 1000
    assert por_sigla['TJMG']['teto_da_fonte'] == 30
    assert por_sigla['TJMG']['pagina'] is False
    assert por_sigla['TJMT']['teto_da_fonte'] is None
    # TJPA não tem busca por nome de advogado na consulta pública.
    assert 'advogado' not in por_sigla['TJPA']['criterios']
    # TRF3 entra pelo motor, mas nunca foi medido — e o catálogo não finge.
    assert por_sigla['TRF3']['verificado_em'] is None
    assert por_sigla['TJSP']['verificado_em'] == '2026-09-04'
