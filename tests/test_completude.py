"""A tela de completude não pode inventar denominador.

CONTEXTO. Toda outra tela deste projeto mede contagem PRÓPRIA — quantos runs,
quantas movimentações. Isso responde "quanto trabalhamos", não "quanto do acervo
temos", e a diferença entre as duas perguntas custou três perdas medidas (a
tabela do CLAUDE.md).

O que estes testes protegem é o CONTRATO DE HONESTIDADE da tela:

  · porta sem gabarito externo mostra "sem total declarado", nunca uma
    porcentagem inventada — abster > chutar;
  · a recuperação mostra as DUAS réguas, porque nenhuma sozinha é honesta (a
    razão itens/página tem falso positivo do downshift de 5xx; a data do run
    subestima). Mostrar uma só seria escolher a que soa melhor;
  · a idade da medição externa é visível — número declarado envelhece e passa a
    mentir em silêncio;
  · a tela NUNCA computa no caminho da requisição (o 502 de 10/08 nasceu disso).
"""
import datetime
from unittest.mock import patch

import pytest
from django.urls import reverse

from dashboard import completude_medicoes as M

URL = 'dashboard:completude'


@pytest.fixture
def logado(client, django_user_model):
    u = django_user_model.objects.create_user('comp@t.local', password='x' * 12)
    client.force_login(u)
    return client


def test_url_resolve():
    assert reverse(URL) == '/dashboard/completude/'


@pytest.mark.django_db
def test_exige_login(client):
    assert client.get(reverse(URL)).status_code in (302, 403)


@pytest.mark.django_db
def test_miss_de_cache_nao_computa_nada(logado):
    """No miss a tela diz 'medindo' — jamais roda a medição no request."""
    with patch('dashboard.completude_views.cache.get', return_value=None), \
         patch('dashboard.completude_warm.warm_completude') as warm:
        r = logado.get(reverse(URL))
    assert r.status_code == 200
    assert r.context['pendente'] is True
    assert not warm.called, 'computou no caminho da requisição — foi assim que o site caiu'


@pytest.mark.django_db
def test_porta_sem_gabarito_nao_inventa_porcentagem(logado):
    """O DJEN não tem total declarado: a completude dele se mede dia a dia."""
    dados = {'portas': {'djen': {'temos': 1_386_220_582}}, 'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    djen = next(p for p in r.context['portas'] if p['slug'] == 'djen')
    assert djen['sem_gabarito'] is True
    assert djen['pct'] is None, 'inventou porcentagem sem denominador da fonte'
    assert 'sem total declarado' in r.content.decode()


@pytest.mark.django_db
def test_porta_com_gabarito_calcula_lacuna(logado):
    dados = {'portas': {'datajud': {'temos': 320_160_481}}, 'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    dj = next(p for p in r.context['portas'] if p['slug'] == 'datajud')
    assert dj['lacuna'] == 343_235_554 - 320_160_481
    assert 90 < dj['pct'] < 95


@pytest.mark.django_db
def test_mostra_as_duas_reguas_da_recuperacao(logado):
    """Uma régua só seria escolher a que soa melhor."""
    dados = {
        'portas': {},
        'recuperacao': [{'sigla': 'TJSP', 'alvo': 329, 'flat': 92, 'pos_corte': 76,
                         'falta': 237, 'recuperavel': 64_895_691,
                         'pct_flat': 28.0, 'pct_corte': 23.1}],
        'resumo_recup': {'alvo': 329, 'pct_flat': 28.0, 'pct_corte': 23.1},
        'medido_em': datetime.datetime.now(),
    }
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    assert '28' in h and '23' in h, 'não mostrou as duas leituras'
    assert 'falso positivo' in h, 'não avisou que a régua da razão tem falso positivo'


@pytest.mark.django_db
def test_medicao_velha_fica_visivel(logado):
    """Número declarado sem idade à vista envelhece e mente em silêncio."""
    with patch.object(M, 'PORTAS', [dict(M.PORTAS[1], medido_em=datetime.date(2020, 1, 1))]), \
         patch('dashboard.completude_views.cache.get', return_value={'portas': {}}):
        r = logado.get(reverse(URL))
    assert r.context['portas'][0]['idade_medicao'] > 2000
    assert 'd</span>' in r.content.decode() or 'idade' in r.content.decode().lower()


@pytest.mark.django_db
def test_sem_diarios_explica_em_vez_de_so_zerar(logado):
    """Tela zerada tem que dizer POR QUE está zerada — senão não se distingue
    'não coletamos' de 'o coletor quebrou'."""
    with patch('dashboard.completude_views.cache.get', return_value={'portas': {}}):
        h = logado.get(reverse(URL)).content.decode()
    assert 'Nenhuma edição catalogada' in h
    assert '2007' in h, 'não disse o que a porta fechada custa'


def test_fase2_bate_com_a_lista_de_recuperavel():
    """Regressão: tribunal na Fase 2 sem recuperável medido viraria linha com
    denominador vazio na tela."""
    faltando = [s for s in M.FASE_2 if s not in M.RECUPERAVEL_POR_TRIBUNAL]
    assert not faltando, f'sem medição de recuperável: {faltando}'
