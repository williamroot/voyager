from datetime import datetime, timezone

import pytest
from django.test import Client

from tribunals.models import ApiClient, Process, Tribunal


@pytest.fixture
def cliente(db):
    return ApiClient.objects.create(nome='falcon-dp', api_key='k-dp', ativo=True)


@pytest.fixture
def tjsp(db):
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    return t


def _dc(t, cnj, classe_cod='', classe_nome='', score=0.5):
    return Process.objects.create(
        tribunal=t, numero_cnj=cnj,
        classificacao=Process.CLASSIF_DIREITO_CREDITORIO,
        classificacao_score=score,
        classe_codigo=classe_cod, classe_nome=classe_nome,
        ultima_movimentacao_em=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


@pytest.mark.django_db
def test_devedor_publico_filtra_so_fazenda(cliente, tjsp):
    # Público por CÓDIGO de classe (Fazenda) e público por NOME (código vazio).
    pub_cod = _dc(tjsp, '1-1.2024.8.26.0001', classe_cod='12078',
                  classe_nome='Cumprimento de Sentença contra a Fazenda Pública')
    pub_nome = _dc(tjsp, '2-2.2024.8.26.0001', classe_cod='',
                   classe_nome='Cumprimento de Sentença contra a Fazenda Pública')
    # Privado (classe 156) — não deve vir com o filtro.
    priv = _dc(tjsp, '3-3.2024.8.26.0001', classe_cod='156',
               classe_nome='Cumprimento de Sentença')

    resp = Client().get(
        '/api/v1/leads/?nivel=DIREITO_CREDITORIO&tribunal=TJSP'
        '&devedor_publico=true&limit=10', HTTP_X_API_KEY='k-dp')
    assert resp.status_code == 200
    cnjs = {r['cnj'] for r in resp.json()['results']}
    assert cnjs == {pub_cod.numero_cnj, pub_nome.numero_cnj}
    assert priv.numero_cnj not in cnjs


@pytest.mark.django_db
def test_sem_filtro_retorna_publico_e_privado(cliente, tjsp):
    pub = _dc(tjsp, '1-1.2024.8.26.0001', classe_cod='12078',
              classe_nome='x contra a Fazenda Pública')
    priv = _dc(tjsp, '3-3.2024.8.26.0001', classe_cod='156',
               classe_nome='Cumprimento de Sentença')

    resp = Client().get(
        '/api/v1/leads/?nivel=DIREITO_CREDITORIO&tribunal=TJSP&limit=10',
        HTTP_X_API_KEY='k-dp')
    assert resp.status_code == 200
    cnjs = {r['cnj'] for r in resp.json()['results']}
    assert cnjs == {pub.numero_cnj, priv.numero_cnj}
