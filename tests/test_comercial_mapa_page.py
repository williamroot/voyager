"""Testes da PÁGINA do Mapa Comercial (dashboard/comercial_mapa.html).

A view (`comercial_mapa_page`) só renderiza o shell HTML — todos os dados vêm
dos 3 endpoints JSON via fetch no browser, então este teste NÃO precisa de ES.
Cobre: login-gate (302 pra anônimo), render 200 pra logado, o GeoJSON dos
estados vindo de {% static %} (não CDN), o registerMap('brasil') presente e a
ausência de qualquer referência externa (CSP: zero CDN) introduzida por esta
página.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()

URL_NAME = 'dashboard:comercial-mapa-page'


@pytest.fixture
def user(db):
    return User.objects.create_user(username='mapa_gate', password='x')


def test_url_resolve():
    assert reverse(URL_NAME) == '/dashboard/comercial/mapa/'


def test_anon_redireciona_login(client):
    resp = client.get(reverse(URL_NAME))
    assert resp.status_code == 302
    assert '/login' in resp['Location']


def test_logado_renderiza_200(user):
    c = Client()
    c.force_login(user)
    resp = c.get(reverse(URL_NAME))
    assert resp.status_code == 200
    html = resp.content.decode()
    # título/branding da página
    assert 'Mapa Comercial' in html
    # honestidade: potencial e confirmado nomeados e distintos
    assert 'Potencial' in html and 'Confirmado' in html


def test_geojson_local_e_registermap(user):
    c = Client()
    c.force_login(user)
    html = c.get(reverse(URL_NAME)).content.decode()
    # GeoJSON servido via {% static %} (não CDN)
    assert '/static/' in html and 'brasil-uf.geojson' in html
    # mapa registrado no ECharts com a chave 'brasil'
    assert "registerMap('brasil'" in html


def test_potencial_null_tratado_como_desconhecido(user):
    """Fix do QA adversarial: potencial null / sinal_processado=false NUNCA vira "0".

    O shell precisa carregar a lógica honesta: helper potDesconhecido (null/
    sinal_processado false ⇒ fora da escala de cor + rótulo distinto), o texto
    do tooltip "sinal ainda não processado" e o caveat de ranking parcial no
    Ataque primeiro ("processado em N de 27 estados").
    """
    c = Client()
    c.force_login(user)
    html = c.get(reverse(URL_NAME)).content.decode()
    # helper de desconhecido considera os DOIS sinais do contrato novo
    assert 'potDesconhecido' in html
    assert 'sinal_processado' in html
    # tooltip do choropleth: desconhecido tem rótulo próprio, não "Potencial: 0"
    assert 'sinal ainda não processado' in html
    # caveat honesto do ranking (derivado dos buckets)
    assert 'ranking parcial' in html
    assert 'de 27 estados' in html
    # painéis usam potLabel (— pra não-processado) em vez de fmt cru
    assert 'potLabel' in html


def test_glossario_tooltips_presentes(user):
    """Tooltips explicativos: glossário canônico definido 1x e aplicado via title.

    O exec precisa entender cada número sem perguntar — o shell carrega o
    GLOSSARIO (fonte única) e os pontos numéricos usam glos('<métrica>').
    """
    c = Client()
    c.force_login(user)
    html = c.get(reverse(URL_NAME)).content.decode()
    # objeto canônico definido uma vez
    assert 'window.GLOSSARIO' in html
    # trechos-chave de cada texto do glossário (1 por métrica)
    assert 'não é o acervo oficial do tribunal' in html          # volume
    assert 'O valor exato do precatório virá do Falcon' in html  # valor
    assert 'carta precatória' in html                            # potencial
    assert 'limitado pela cobertura de enriquecimento' in html   # confirmado
    assert 'já passaram por enriquecimento' in html              # cobertura
    assert 'densidade de precatório × valor relativo' in html    # score
    # aplicação via helper glos() em bindings :title (reuso, não copy-paste)
    assert html.count("glos('potencial')") >= 3   # barra + KPI + Top-N + drill + FED
    assert html.count("glos('score')") >= 3       # intro + Top-N + drill
    assert html.count("glos('cobertura')") >= 2   # Top-N + drill
    # linha de contexto da métrica ativa no tooltip do choropleth
    assert 'glosMetricaAtiva' in html


def test_bloco_didatico_visivel_na_tela(user):
    """Textos didáticos VISÍVEIS (não só no hover) — o comercial é leigo.

    Cobre: o bloco "Como ler estes números" do drill-down (colapsável, aberto
    por default + persistência), os 6 textos leigos, a frase de leitura
    automática (insightUf) e a legenda leiga de Potencial vs Confirmado no topo.
    """
    c = Client()
    c.force_login(user)
    html = c.get(reverse(URL_NAME)).content.decode()

    # cabeçalho do bloco + mecânica de colapso persistida
    assert 'Como ler estes números' in html
    assert 'ajudaAberta' in html
    assert 'toggleAjuda' in html
    assert 'voy-mapa:ajuda-drill' in html   # chave do localStorage

    # os 6 textos didáticos, em linguagem leiga, no corpo da página
    assert 'já trouxemos para a nossa base' in html                    # Volume
    assert 'ofício requisitório ou RPV' in html                        # Potencial
    assert 'É um forte indício, não uma certeza' in html               # Potencial
    assert 'nossa inteligência artificial já classificou' in html      # Confirmado
    assert 'não que o processo não tenha dinheiro' in html             # R$
    assert 'território inexplorado' in html                            # Validado
    assert 'onde atacar primeiro' in html                              # Score

    # frase de leitura automática (montada dos números do estado aberto)
    assert 'insightUf' in html
    assert 'têm indício de precatório' in html
    # caso honesto: sinal não processado tem frase própria
    assert 'Ainda não processamos o indício de precatório neste estado' in html

    # legenda leiga no topo (sem jargão "sinal DJEN" / "classificação ML")
    assert 'nossa IA já leu e confirmou' in html
    assert 'sinal DJEN —' not in html
    assert 'classificação ML —' not in html


def test_sem_cdn_externo_na_pagina(user):
    """A página não pode introduzir nenhuma URL http(s) externa (CSP).

    As únicas linhas offsite permitidas vêm do base.html (Google Fonts /
    tailwind local) — fora do escopo desta página. Aqui checamos que o BLOCO
    de conteúdo do mapa não trouxe CDN algum.
    """
    c = Client()
    c.force_login(user)
    html = c.get(reverse(URL_NAME)).content.decode()
    # recorta só o miolo da página (após o page_header do mapa)
    marca = 'Mapa Comercial de Precatórios'
    corpo = html[html.find(marca):] if marca in html else html
    proibidos = ('cdn.jsdelivr', 'unpkg.com', 'cdnjs', 'code.jquery',
                 'fonts.googleapis', 'fonts.gstatic')
    for termo in proibidos:
        assert termo not in corpo, f'referência externa proibida no corpo: {termo}'
