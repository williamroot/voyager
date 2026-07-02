"""Regra de sinal POSITIVA do TJMA: Cumprimento com ofício requisitório (F14)
ou expedição (F20) nos movimentos promove a PRECATORIO/1.0 — MAS o guard F24
(pagamento posterior à expedição) veta a promoção: expedido e já pago não é
lead comprável. Composição das duas regras de sinal do TJMA.
"""
from __future__ import annotations

import pytest

import tribunals.classificador as clf
from tribunals.classificador import HARDCODED_WEIGHTS, classificar
from tribunals.models import ClassificadorVersao, Process


def _proc(tribunal_id, classe_codigo='12078'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='0000001-00.2026.8.10.0001')


def _reset_cache():
    with clf._WEIGHTS_LOCK:
        clf._WEIGHTS_CACHE.update(
            versao=None, pesos=None, thresholds=None, normas=None,
            loaded_at=0.0,
        )


@pytest.fixture
def _versao_score_alto(settings):
    """Score ~0.99 p/ qualquer processo: isola o efeito das regras de sinal.

    _validate_pesos exige TODAS as features do extrator (rejeita subset):
    zera os pesos e sobe só o intercept.
    """
    settings.SHADOW_SAMPLE_RATE = 0.0
    _reset_cache()
    ClassificadorVersao.objects.all().delete()
    pesos = {k: 0.0 for k in HARDCODED_WEIGHTS}
    pesos['_intercept_'] = 5.0
    ClassificadorVersao.objects.create(versao='vtest', pesos=pesos, ativa=True)
    yield
    _reset_cache()
    ClassificadorVersao.objects.all().delete()


def test_cumprimento_tjma_com_oficio_f14_vira_precatorio():
    cat, score, _ = classificar(
        _proc('TJMA'), features={'F1_cumprim': 1, 'F14_oficio_text': 1})
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


def test_cumprimento_tjma_com_expedicao_f20_vira_precatorio():
    cat, score, _ = classificar(
        _proc('TJMA'), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1})
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


@pytest.mark.django_db
def test_expedido_mas_pago_nao_promove(_versao_score_alto):
    # F24 (pagamento posterior à expedição) VETA a promoção e a regra
    # negativa rebaixa: as duas regras de sinal do TJMA compõem — sobe o
    # vivo, nunca o morto. Sem o guard, este processo sairia N1/1.0.
    cat, score, _ = classificar(
        _proc('TJMA'), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1,
                                 'F24_pago_pos_exped_ANTI': 1})
    assert cat == Process.CLASSIF_NAO_LEAD
    assert score <= clf.SCORE_REBAIXAMENTO_SINAL


@pytest.mark.django_db
def test_cumprimento_tjma_sem_sinal_nao_promove():
    cat, score, _ = classificar(
        _proc('TJMA'), features={'F1_cumprim': 1, 'F11_precat_text': 1})
    assert score != 1.0


def test_tjal_continua_promovendo_sem_guard():
    # TJAL não está em PAGAMENTO_SINAL_TRIBUNAIS: F24=1 não muda o rollout
    # existente (comportamento do TJAL intacto).
    cat, score, _ = classificar(
        _proc('TJAL', classe_codigo='156'),
        features={'F1_cumprim': 1, 'F14_oficio_text': 1,
                  'F24_pago_pos_exped_ANTI': 1})
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


@pytest.mark.django_db
def test_tjsp_fora_do_escopo_nao_promove():
    cat, score, _ = classificar(
        _proc('TJSP'), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1})
    assert score != 1.0
