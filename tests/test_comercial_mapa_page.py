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
