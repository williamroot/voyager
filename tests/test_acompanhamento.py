"""Acompanhamento — o diário de bordo do produto.

O que estes testes protegem não é CRUD: é o contrato de honestidade da tela.

  - só quem está logado vê (tem número de acervo e relato de incidente lá dentro);
  - o filtro por período usa `data_evento` (quando a descoberta aconteceu), não
    `criado_em` (quando alguém teve tempo de escrever) — ordenar pela segunda
    contaria a história errada;
  - a contagem dos chips por tipo é feita ANTES do filtro de tipo, senão
    "Incidente (3)" mostraria 3 só porque o filtro de incidente está ligado, que
    é contagem circular;
  - nota sem número medido é marcada como tal na tela — descoberta sem prova é
    opinião com data.
"""
import datetime

import pytest
from django.urls import reverse

from dashboard.models import NotaAcompanhamento as N

URL = 'dashboard:acompanhamento'


@pytest.fixture
def logado(client, django_user_model):
    u = django_user_model.objects.create_user('acomp@t.local', password='x' * 12)
    client.force_login(u)
    return client


@pytest.fixture
def notas(db):
    hoje = datetime.date.today()
    return [
        N.objects.create(titulo='Perda medida no TJSP', tipo=N.TIPO_DESCOBERTA,
                         impacto=N.IMPACTO_ALTO, area='ingestão',
                         resumo='teto de páginas decapitava 43,6%',
                         data_evento=hoje - datetime.timedelta(days=2),
                         numeros=[{'rotulo': 'dia', 'antes': '40.427', 'depois': '257.832'}]),
        N.objects.create(titulo='Site fora do ar', tipo=N.TIPO_INCIDENTE,
                         resumo='migration esperando transação longa', area='deploy',
                         data_evento=hoje - datetime.timedelta(days=5)),
        N.objects.create(titulo='Coisa antiga', tipo=N.TIPO_FEATURE,
                         resumo='fora de qualquer janela curta', area='busca',
                         data_evento=hoje - datetime.timedelta(days=400)),
    ]


def test_urls_resolvem():
    assert reverse(URL) == '/dashboard/acompanhamento/'
    assert reverse('dashboard:acompanhamento-nota', args=[7]) == '/dashboard/acompanhamento/7/'


@pytest.mark.django_db
def test_exige_login(client, notas):
    r = client.get(reverse(URL))
    assert r.status_code in (302, 403)
    r2 = client.get(reverse('dashboard:acompanhamento-nota', args=[notas[0].pk]))
    assert r2.status_code in (302, 403)


@pytest.mark.django_db
def test_lista_com_periodo_padrao(logado, notas):
    r = logado.get(reverse(URL))
    assert r.status_code == 200
    titulos = [n.titulo for n in r.context['notas']]
    assert 'Perda medida no TJSP' in titulos
    assert 'Coisa antiga' not in titulos, 'nota de 400 dias entrou na janela de 90'


@pytest.mark.django_db
def test_periodo_tudo_traz_a_serie_inteira(logado, notas):
    """"Tudo" existe porque o valor da tela é ver a série completa — as
    descobertas antigas explicam as recentes."""
    r = logado.get(reverse(URL), {'periodo': 'tudo'})
    assert len(r.context['notas']) == 3


@pytest.mark.django_db
def test_filtro_por_periodo_usa_data_do_evento(logado, db):
    """A nota pode ser escrita hoje sobre algo de meses atrás. O eixo é o
    evento — senão a linha do tempo vira ordem de digitação."""
    N.objects.create(titulo='Escrita hoje, descoberta em janeiro',
                     resumo='x', data_evento=datetime.date.today() - datetime.timedelta(days=200))
    r = logado.get(reverse(URL), {'periodo': '30'})
    assert len(r.context['notas']) == 0


@pytest.mark.django_db
def test_contagem_por_tipo_nao_e_circular(logado, notas):
    """Com o filtro de incidente ligado, o chip de descoberta tem que continuar
    mostrando quantas descobertas existem no período."""
    r = logado.get(reverse(URL), {'tipo': N.TIPO_INCIDENTE})
    assert [n.titulo for n in r.context['notas']] == ['Site fora do ar']
    assert r.context['por_tipo'].get(N.TIPO_DESCOBERTA) == 1


@pytest.mark.django_db
def test_filtro_por_area_e_busca(logado, notas):
    assert len(logado.get(reverse(URL), {'area': 'deploy'}).context['notas']) == 1
    r = logado.get(reverse(URL), {'q': 'TJSP'})
    assert len(r.context['notas']) == 1


@pytest.mark.django_db
def test_conta_quantas_tem_prova(logado, notas):
    """Descoberta sem número medido é opinião com data — a tela separa as duas."""
    r = logado.get(reverse(URL))
    assert r.context['com_prova'] == 1
    assert notas[0].tem_prova is True
    assert notas[1].tem_prova is False


@pytest.mark.django_db
def test_pagina_da_nota_mostra_numeros_e_relato(logado, notas):
    n = notas[0]
    n.corpo = 'o relato inteiro da medição'
    n.referencias = ['djen/ingestion.py']
    n.save()
    r = logado.get(reverse('dashboard:acompanhamento-nota', args=[n.pk]))
    assert r.status_code == 200
    corpo = r.content.decode()
    assert '257.832' in corpo and 'o relato inteiro da medição' in corpo
    assert 'djen/ingestion.py' in corpo


@pytest.mark.django_db
def test_seed_traz_as_descobertas_reais_e_e_idempotente(logado):
    from django.core.management import call_command
    call_command('acompanhamento_semear')
    n1 = N.objects.count()
    call_command('acompanhamento_semear')
    assert N.objects.count() == n1, 'semear duas vezes duplicou'
    assert n1 >= 9
    assert N.objects.filter(titulo__icontains='43,6% do TJSP').exists()
    # toda nota semeada de descoberta tem que carregar prova
    for nota in N.objects.filter(tipo=N.TIPO_DESCOBERTA):
        assert nota.numeros, f'descoberta sem número medido: {nota.titulo}'


def test_bloco_vazio_nao_vira_coluna_fantasma():
    """Coluna com cabeçalho e nada embaixo parece falha de carregamento — o
    leitor não distingue "não medimos" de "quebrou"."""
    from dashboard.acompanhamento_views import _resumo_blocos

    assert _resumo_blocos(None) == []
    assert _resumo_blocos({}) == []
    assert _resumo_blocos({'resumo': {'cobertura': [], 'pesquisa': []}}) == []
    so_cob = _resumo_blocos({'resumo': {'cobertura': [{'k': 'x'}], 'pesquisa': []}})
    assert len(so_cob) == 1 and so_cob[0]['titulo'] == 'Cobertura'


def test_resumo_entra_no_mesmo_payload_dos_marcos():
    """Uma medição a mais não justifica um segundo cache — e um segundo lugar
    onde o número pode envelhecer sozinho."""
    import inspect

    from dashboard import marcos_semana

    fonte = inspect.getsource(marcos_semana.calcular)
    assert 'resumo' in fonte, 'o resumo tem que sair no payload do `calcular()`'
