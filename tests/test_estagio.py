"""Testes da lib de estágio do crédito (tribunals/estagio.py).

Cobrem: montagem do vetor (ordem do bundle, categóricas unseen → -1, None →
NaN), fail-closed sem artefato, e o caminho completo de predição com um
booster mínimo treinado em memória (skip se lightgbm ausente no ambiente).
"""
from __future__ import annotations

import math

import pytest

from tribunals import estagio


def _bundle_minimo():
    """Treina um booster 4-classes de brinquedo com o MESMO schema do v1."""
    lgb = pytest.importorskip('lightgbm')
    np = pytest.importorskip('numpy')
    features = ['total_movs', 'tem_exped', 'tribunal']
    rng = np.random.default_rng(42)
    n = 400
    x = np.column_stack([
        rng.integers(0, 200, n),          # total_movs
        rng.integers(0, 2, n),            # tem_exped
        rng.integers(0, 3, n),            # tribunal (código categ.)
    ]).astype(float)
    # rótulo correlacionado com tem_exped pra ter sinal
    y = np.where(x[:, 1] == 1, 2, rng.integers(0, 2, n))
    ds = lgb.Dataset(x, label=y, categorical_feature=[2])
    booster = lgb.train({'objective': 'multiclass', 'num_class': 4,
                         'verbose': -1, 'seed': 1}, ds, num_boost_round=10)
    return {
        'versao': 'estagio_test',
        'booster_str': booster.model_to_string(),
        'features': features,
        'features_cat': ['tribunal'],
        'cat_maps': {'tribunal': ['TRF1', 'TJSP', 'TJMG']},
        'classes': list(estagio.CLASSES),
    }


def test_vetor_ordem_e_categoricas():
    bundle = _bundle_minimo()
    feats = {'total_movs': 42, 'tem_exped': 1, 'tribunal': 'TJSP'}
    v = estagio._vetor(feats, bundle)
    assert v == [42.0, 1.0, 1.0]  # TJSP = índice 1 no cat_map


def test_vetor_categoria_desconhecida_vira_menos_um():
    bundle = _bundle_minimo()
    v = estagio._vetor({'total_movs': 1, 'tem_exped': 0, 'tribunal': 'TJXX'}, bundle)
    assert v[2] == -1.0


def test_vetor_none_vira_nan():
    bundle = _bundle_minimo()
    v = estagio._vetor({'total_movs': None, 'tem_exped': 0, 'tribunal': 'TRF1'}, bundle)
    assert math.isnan(v[0])


def test_fail_closed_sem_artefato(settings, tmp_path):
    settings.ESTAGIO_MODEL_PATH = str(tmp_path / 'inexistente.joblib')
    estagio.reset_bundle_cache()
    with pytest.raises(estagio.EstagioIndisponivel):
        estagio._load_bundle()
    estagio.reset_bundle_cache()


def test_load_bundle_e_predicao_com_artefato(settings, tmp_path):
    joblib = pytest.importorskip('joblib')
    np = pytest.importorskip('numpy')
    path = tmp_path / 'estagio_test.joblib'
    joblib.dump(_bundle_minimo(), path)
    settings.ESTAGIO_MODEL_PATH = str(path)
    estagio.reset_bundle_cache()
    bundle, booster = estagio._load_bundle()
    x = np.asarray([estagio._vetor(
        {'total_movs': 10, 'tem_exped': 1, 'tribunal': 'TRF1'}, bundle)])
    proba = booster.predict(x)[0]
    assert len(proba) == 4
    assert abs(float(proba.sum()) - 1.0) < 1e-6
    estagio.reset_bundle_cache()


@pytest.mark.django_db
def test_computar_features_processo_sem_movs():
    from tribunals.models import Process, Tribunal  # noqa: PLC0415
    trib, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1'})
    p = Process.objects.create(
        numero_cnj='0000001-11.2020.4.01.3800', tribunal=trib,
        ano_cnj=2020, classe_codigo='12078', classe_nome='Cumprimento de Sentença')
    feats, extras = estagio.computar_features_publicas(p)
    assert feats['is_cumprimento'] == 1
    assert feats['is_fazenda'] == 1
    assert feats['total_movs'] == 0
    assert feats['tribunal'] == 'TRF1'
    assert feats['dias_ult_mov'] is None
    assert extras['valor_homologado'] is None
    # todo campo de feature precisa existir (schema completo)
    esperado = {
        'ano_cnj', 'is_cumprimento', 'is_fazenda', 'is_juizado_anti',
        'dias_autuacao', 'total_movs', 'distinct_tipos', 'exped_tc_n',
        'precat_text_n', 'rpv_text_n', 'reqpag_text_n', 'oficio_text_n',
        'exped_forte_n', 'cancel_n', 'transito_n', 'homolog_n', 'cumpr_text_n',
        'pago_n', 'ext_satisf_n', 'ext_semmerito_n', 'improc_n', 'dias_ult_mov',
        'duracao_dias', 'movs_por_ano', 'tem_exped', 'dias_desde_exped',
        'dias_transito_a_exped', 'tem_transito', 'dias_desde_transito',
        'tem_homolog', 'dias_desde_homolog', 'tem_pago', 'dias_desde_pago',
        'pago_pos_exped', 'extneg_pos_exped', 'n_partes',
        'tem_ente_publico_passivo', 'log_valor_homologado',
        'tribunal', 'classe_codigo', 'assunto_codigo',
    }
    assert esperado.issubset(feats.keys())


@pytest.mark.django_db
def test_predict_estagio_cnj_inexistente():
    from tribunals.models import Process  # noqa: PLC0415
    with pytest.raises(Process.DoesNotExist):
        estagio.predict_estagio('9999999-99.2099.4.01.9999')
