"""Regra de sinal NEGATIVA do TJMA: pagamento publicado no DJEN posterior à
expedição (F24) rebaixa PRECATORIO/PRE_PRECATORIO → NAO_LEAD.

Contexto (2026-07-01): auditoria dos autos reais do 1º lote TJMA mostrou N1
0.70-0.77 dominado por RPV municipal já em pagamento (BacenJud/alvará de
levantamento) — crédito em levantamento não é lead comprável.
"""
from __future__ import annotations

import pytest

from tribunals.classificador import (
    SCORE_REBAIXAMENTO_SINAL,
    classificar,
)
from tribunals.models import Process


def _proc(tribunal_id, classe_codigo='156'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='0000001-00.2026.8.10.0001')


# Features que, sem o sinal-anti, categorizam como PRECATORIO (score alto
# via F2/F11 + os thresholds da hierarquia).
_FEATURES_N1 = {'F1_cumprim': 1, 'F2_precat_tc': 1, 'F11_precat_text': 1,
                'F15_logMovs': 1.0, 'F17_logN1count': 1.0, 'F20_exp_juriscope': 1}


@pytest.mark.django_db
def test_tjma_precatorio_com_pagamento_rebaixa_nao_lead():
    cat, score, _ = classificar(
        _proc('TJMA'),
        features={**_FEATURES_N1, 'F24_pago_pos_exped_ANTI': 1})
    assert cat == Process.CLASSIF_NAO_LEAD
    assert score <= SCORE_REBAIXAMENTO_SINAL


@pytest.mark.django_db
def test_tjma_sem_pagamento_nao_rebaixa():
    cat, _, _ = classificar(
        _proc('TJMA'),
        features={**_FEATURES_N1, 'F24_pago_pos_exped_ANTI': 0})
    assert cat != Process.CLASSIF_NAO_LEAD


@pytest.mark.django_db
def test_tjma_nao_lead_nao_e_afetado():
    # quem já não é lead não muda (regra só rebaixa N1/N2)
    cat, score, _ = classificar(
        _proc('TJMA'),
        features={'F1_cumprim': 0, 'F24_pago_pos_exped_ANTI': 1})
    assert cat == Process.CLASSIF_NAO_LEAD


@pytest.mark.django_db
def test_tjmg_fora_do_escopo_nao_rebaixa():
    cat, _, _ = classificar(
        _proc('TJMG'),
        features={**_FEATURES_N1, 'F24_pago_pos_exped_ANTI': 1})
    assert cat != Process.CLASSIF_NAO_LEAD
