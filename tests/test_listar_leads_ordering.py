from datetime import datetime, timezone

import pytest
from django.test import Client

from tribunals.models import ApiClient, Process, Tribunal


@pytest.fixture
def cliente(db):
    return ApiClient.objects.create(nome='falcon-ord', api_key='k-ord', ativo=True)


@pytest.fixture
def tribunais(db):
    out = {}
    for sigla in ('TJMG', 'TRF1', 'TRF3'):
        t, _ = Tribunal.objects.get_or_create(
            sigla=sigla,
            defaults={'nome': sigla, 'sigla_djen': sigla},
        )
        out[sigla] = t
    return out


def _mk(t, cnj, ult, score):
    return Process.objects.create(
        tribunal=t,
        numero_cnj=cnj,
        classificacao=Process.CLASSIF_PRECATORIO,
        classificacao_score=score,
        ultima_movimentacao_em=ult,
    )


@pytest.mark.django_db
def test_listar_leads_ordena_por_ultima_mov_desc(cliente, tribunais):
    # 3 processos, datas embaralhadas em relação ao score → garante que a
    # ordenação NÃO está caindo no antigo (-score, -id).
    p_velho_alto = _mk(tribunais['TJMG'], '1-1.2023.8.13.0001',
                       datetime(2026, 3, 1, tzinfo=timezone.utc), score=0.99)
    p_meio_baixo = _mk(tribunais['TRF1'], '2-2.2024.4.01.3300',
                       datetime(2026, 5, 1, tzinfo=timezone.utc), score=0.40)
    p_novo_medio = _mk(tribunais['TRF3'], '3-3.2025.4.03.6100',
                       datetime(2026, 5, 15, tzinfo=timezone.utc), score=0.75)

    resp = Client().get('/api/v1/leads/?nivel=PRECATORIO&limit=10',
                        HTTP_X_API_KEY='k-ord')
    assert resp.status_code == 200
    cnjs = [r['cnj'] for r in resp.json()['results']]
    assert cnjs == [
        p_novo_medio.numero_cnj,   # 2026-05-15 (mais recente)
        p_meio_baixo.numero_cnj,   # 2026-05-01
        p_velho_alto.numero_cnj,   # 2026-03-01 (apesar de score 0.99)
    ]


@pytest.mark.django_db
def test_listar_leads_tiebreaker_score_no_empate_de_data(cliente, tribunais):
    mesma = datetime(2026, 5, 10, tzinfo=timezone.utc)
    p_baixo = _mk(tribunais['TJMG'], '10-10.2024.8.13.0001', mesma, score=0.55)
    p_alto = _mk(tribunais['TJMG'], '11-11.2024.8.13.0001', mesma, score=0.92)

    resp = Client().get('/api/v1/leads/?nivel=PRECATORIO&tribunal=TJMG&limit=10',
                        HTTP_X_API_KEY='k-ord')
    assert resp.status_code == 200
    cnjs = [r['cnj'] for r in resp.json()['results']]
    assert cnjs == [p_alto.numero_cnj, p_baixo.numero_cnj]


@pytest.mark.django_db
def test_listar_leads_vale_pra_qualquer_tribunal(cliente, tribunais):
    # 1 processo por tribunal, ordem de ultima_mov definida → mesmo critério.
    dt_old = datetime(2026, 4, 1, tzinfo=timezone.utc)
    dt_mid = datetime(2026, 4, 20, tzinfo=timezone.utc)
    dt_new = datetime(2026, 5, 10, tzinfo=timezone.utc)
    p1 = _mk(tribunais['TRF1'], '21-21.2024.4.01.3300', dt_old, score=0.80)
    p2 = _mk(tribunais['TRF3'], '22-22.2024.4.03.6100', dt_new, score=0.80)
    p3 = _mk(tribunais['TJMG'], '23-23.2024.8.13.0001', dt_mid, score=0.80)

    resp = Client().get('/api/v1/leads/?nivel=PRECATORIO&limit=10',
                        HTTP_X_API_KEY='k-ord')
    assert resp.status_code == 200
    cnjs = [r['cnj'] for r in resp.json()['results']]
    assert cnjs == [p2.numero_cnj, p3.numero_cnj, p1.numero_cnj]
