"""Testes do serviço de agregação do Mapa Comercial (search/agg_comercial.py).

Não dependem de ES real: mockam o cliente (`search.agg_comercial.get_es`) e o
cache de cobertura. Cobrem: montagem do corpo ES (asserts no body), cálculo do
score de foco, parse/saneamento dos filtros e filtros combinados.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from search import agg_comercial as agg


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _qd(**kwargs):
    """Simula um request.GET (dict-like com .get)."""
    return SimpleNamespace(get=lambda k, default=None: kwargs.get(k, default))


def _es_resp_terms(campo, buckets):
    """Envelope ES de resposta terms-agg."""
    return {
        'aggregations': {
            'buckets': {
                'buckets': [
                    {
                        'key': b['key'],
                        'doc_count': b['volume'],
                        'valor': {'value': b.get('valor', 0.0)},
                        'potencial': {'doc_count': b.get('potencial', 0)},
                        'confirmado': {'doc_count': b.get('confirmado', 0)},
                    }
                    for b in buckets
                ]
            }
        }
    }


def _es_resp_total(volume, valor, potencial, confirmado):
    return {
        'hits': {'total': {'value': volume}},
        'aggregations': {
            'valor': {'value': valor},
            'potencial': {'doc_count': potencial},
            'confirmado': {'doc_count': confirmado},
        },
    }


# --------------------------------------------------------------------------- #
# parse_filtros
# --------------------------------------------------------------------------- #
def test_parse_filtros_vazio():
    assert agg.parse_filtros(_qd()) == {}


def test_parse_filtros_basico():
    f = agg.parse_filtros(_qd(uf='sp', tribunal='tjsp', classificacao='precatorio'))
    assert f['uf'] == 'SP'
    assert f['tribunal'] == 'TJSP'
    assert f['classificacao'] == 'PRECATORIO'


def test_parse_filtros_tipo_potencial():
    f = agg.parse_filtros(_qd(tipo='potencial'))
    assert f['tem_sinal'] is True


def test_parse_filtros_tipo_confirmado():
    f = agg.parse_filtros(_qd(tipo='confirmado'))
    assert f['classificacao'] == 'PRECATORIO'


def test_parse_filtros_tem_sinal_bool():
    assert agg.parse_filtros(_qd(tem_sinal='true'))['tem_sinal'] is True
    assert agg.parse_filtros(_qd(tem_sinal='0'))['tem_sinal'] is False


def test_parse_filtros_ano_futuro_clampado():
    ano_atual = datetime.date.today().year
    f = agg.parse_filtros(_qd(ano_min='2020', ano_max='3000'))
    assert f['ano_min'] == 2020
    assert f['ano_max'] == ano_atual   # 3000 clampado pro ano atual


def test_parse_filtros_ano_invertido_ordena():
    f = agg.parse_filtros(_qd(ano_min='2022', ano_max='2010'))
    assert f['ano_min'] == 2010
    assert f['ano_max'] == 2022


def test_parse_filtros_valor_negativo_ignorado():
    f = agg.parse_filtros(_qd(valor_min='-5', valor_max='1000'))
    assert 'valor_min' not in f
    assert f['valor_max'] == 1000.0


def test_parse_filtros_valor_invertido_ordena():
    f = agg.parse_filtros(_qd(valor_min='9000', valor_max='100'))
    assert f['valor_min'] == 100.0
    assert f['valor_max'] == 9000.0


def test_parse_filtros_lixo_nao_quebra():
    f = agg.parse_filtros(_qd(ano_min='abc', valor_max='xyz', n='???'))
    assert 'ano_min' not in f
    assert 'valor_max' not in f


def test_parse_filtros_natureza_vira_codigo_classe():
    f = agg.parse_filtros(_qd(natureza='1116'))
    assert f['codigo_classe'] == '1116'
    # codigo_classe explícito tem precedência
    f2 = agg.parse_filtros(_qd(natureza='1116', codigo_classe='9999'))
    assert f2['codigo_classe'] == '9999'


# --------------------------------------------------------------------------- #
# build_filter_clauses / build_body_*
# --------------------------------------------------------------------------- #
def test_build_filter_clauses_todos():
    filtros = {
        'uf': 'SP', 'tribunal': 'TJSP', 'classificacao': 'PRECATORIO',
        'codigo_classe': '1116', 'tem_sinal': True,
        'ano_min': 2020, 'ano_max': 2024, 'valor_min': 100.0, 'valor_max': 5000.0,
    }
    clauses = agg.build_filter_clauses(filtros)
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'term': {'tribunal': 'TJSP'}} in clauses
    assert {'term': {'classificacao': 'PRECATORIO'}} in clauses
    assert {'term': {'codigo_classe': '1116'}} in clauses
    assert {'term': {'tem_sinal_precatorio': True}} in clauses
    assert {'range': {'ano_cnj': {'gte': 2020, 'lte': 2024}}} in clauses
    assert {'range': {'valor_causa': {'gte': 100.0, 'lte': 5000.0}}} in clauses


def test_build_body_por_campo_estrutura():
    body = agg.build_body_por_campo('uf', {})
    assert body['size'] == 0
    terms = body['aggs']['buckets']['terms']
    assert terms['field'] == 'uf'
    assert terms['size'] == agg._TERMS_SIZE
    sub = body['aggs']['buckets']['aggs']
    assert sub['valor'] == {'sum': {'field': 'valor_causa'}}
    assert sub['potencial'] == {'filter': {'term': {'tem_sinal_precatorio': True}}}
    assert sub['confirmado'] == {'filter': {'term': {'classificacao': 'PRECATORIO'}}}
    # sem filtro → sem query
    assert 'query' not in body


def test_build_body_com_filtro_gera_query():
    body = agg.build_body_por_campo('tribunal', {'uf': 'SP', 'ano_min': 2020})
    assert body['aggs']['buckets']['terms']['field'] == 'tribunal'
    clauses = body['query']['bool']['filter']
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'range': {'ano_cnj': {'gte': 2020}}} in clauses


def test_build_body_total():
    body = agg.build_body_total({'uf': 'SP'})
    assert body['size'] == 0
    assert body['track_total_hits'] is True
    assert 'buckets' not in body['aggs']
    assert body['aggs']['valor'] == {'sum': {'field': 'valor_causa'}}
    assert body['query']['bool']['filter'] == [{'term': {'uf': 'SP'}}]


# --------------------------------------------------------------------------- #
# score de foco
# --------------------------------------------------------------------------- #
def test_score_foco_basico():
    # densidade=0.1, valor_relativo=1.0, cobertura=0 → score=0.1
    dens, score = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=0, valor_max=100.0)
    assert dens == 0.1
    assert score == 0.1


def test_score_foco_penaliza_cobertura():
    # cobertura 50% derruba o score pela metade
    _, score = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=50.0, valor_max=100.0)
    assert score == pytest.approx(0.05)


def test_score_foco_cobertura_none_trata_como_zero():
    # sem cobertura no cache ⇒ (1-0)=1, score cheio (ataque primeiro)
    _, score = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=None, valor_max=100.0)
    assert score == 0.1


def test_score_foco_valor_max_zero_fator_neutro():
    # conjunto sem dado de valor (ex.: federal, DJEN não traz valor_causa) →
    # fator valor NEUTRO: rankeia por densidade×(1−cobertura) em vez de zerar.
    dens, score = agg.calcular_score_foco(
        volume=1000, valor=0.0, potencial=100, cobertura_pct=0, valor_max=0.0)
    assert dens == 0.1
    assert score == 0.1


def test_score_foco_volume_zero_nao_div0():
    dens, score = agg.calcular_score_foco(
        volume=0, valor=100.0, potencial=0, cobertura_pct=0, valor_max=100.0)
    assert dens == 0.0
    assert score == 0.0


# --------------------------------------------------------------------------- #
# agg_por_uf (fim-a-fim com ES + cobertura mockados)
# --------------------------------------------------------------------------- #
@patch('search.agg_comercial._cobertura_por_uf')
@patch('search.agg_comercial.get_es')
def test_agg_por_uf_monta_query_e_calcula_score(mock_get_es, mock_cob):
    es = MagicMock()
    # 1ª chamada: terms; 2ª chamada: total
    es.search.side_effect = [
        _es_resp_terms('uf', [
            {'key': 'SP', 'volume': 1000, 'valor': 500.0, 'potencial': 200, 'confirmado': 50},
            {'key': 'RJ', 'volume': 500, 'valor': 250.0, 'potencial': 50, 'confirmado': 10},
        ]),
        _es_resp_total(1500, 750.0, 250, 60),
    ]
    mock_get_es.return_value = es
    mock_cob.return_value = {'SP': 20.0, 'RJ': 80.0}

    out = agg.agg_por_uf({'ano_min': 2020})

    # 1 request de terms + 1 de total (não N requests)
    assert es.search.call_count == 2
    body_terms = es.search.call_args_list[0].kwargs['body']
    assert body_terms['aggs']['buckets']['terms']['field'] == 'uf'
    assert {'range': {'ano_cnj': {'gte': 2020}}} in body_terms['query']['bool']['filter']

    ufs = {b['uf']: b for b in out['ufs']}
    # SP: dens=200/1000=0.2, valor_rel=500/500=1, cob=0.2 → 0.2*1*0.8=0.16
    assert ufs['SP']['densidade'] == 0.2
    assert ufs['SP']['score_foco'] == pytest.approx(0.16)
    assert ufs['SP']['cobertura_pct'] == 20.0
    # RJ: dens=50/500=0.1, valor_rel=250/500=0.5, cob=0.8 → 0.1*0.5*0.2=0.01
    assert ufs['RJ']['score_foco'] == pytest.approx(0.01)
    # ordenado por score desc → SP primeiro
    assert out['ufs'][0]['uf'] == 'SP'
    assert out['total']['volume'] == 1500
    assert out['total']['confirmado'] == 60


@patch('search.agg_comercial._cobertura_por_tribunal')
@patch('search.agg_comercial.get_es')
def test_agg_tribunais_aplica_uf(mock_get_es, mock_cob):
    es = MagicMock()
    es.search.side_effect = [
        _es_resp_terms('tribunal', [
            {'key': 'TJSP', 'volume': 900, 'valor': 400.0, 'potencial': 180, 'confirmado': 45},
        ]),
        _es_resp_total(900, 400.0, 180, 45),
    ]
    mock_get_es.return_value = es
    mock_cob.return_value = {'TJSP': 30.0}

    out = agg.agg_tribunais('sp', {'classificacao': 'PRECATORIO'})

    assert out['uf'] == 'SP'
    body = es.search.call_args_list[0].kwargs['body']
    clauses = body['query']['bool']['filter']
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'term': {'classificacao': 'PRECATORIO'}} in clauses
    assert body['aggs']['buckets']['terms']['field'] == 'tribunal'
    assert out['tribunais'][0]['tribunal'] == 'TJSP'
    assert out['tribunais'][0]['cobertura_pct'] == 30.0


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_ordena_por_metric(mock_agg):
    mock_agg.return_value = {
        'ufs': [
            {'uf': 'SP', 'volume': 1000, 'valor': 500.0, 'score_foco': 0.16, 'confirmado': 50},
            {'uf': 'RJ', 'volume': 2000, 'valor': 250.0, 'score_foco': 0.01, 'confirmado': 10},
        ],
        'gerado_em': '2026-08-11T00:00:00+00:00',
    }
    # por volume → RJ primeiro
    r = agg.ranking_top('volume', 1, {})
    assert r['metric'] == 'volume'
    assert r['n'] == 1
    assert len(r['ranking']) == 1
    assert r['ranking'][0]['uf'] == 'RJ'
    # por score → SP primeiro
    r2 = agg.ranking_top('score', 10, {})
    assert r2['ranking'][0]['uf'] == 'SP'
    # metric inválida cai em score
    r3 = agg.ranking_top('inexistente', 5, {})
    assert r3['metric'] == 'score'


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_clampa_n(mock_agg):
    mock_agg.return_value = {'ufs': [], 'gerado_em': 'x'}
    assert agg.ranking_top('score', 0, {})['n'] == 1
    assert agg.ranking_top('score', 999, {})['n'] == 100
    assert agg.ranking_top('score', 'abc', {})['n'] == 10
