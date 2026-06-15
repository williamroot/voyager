from __future__ import annotations

import pytest

from tribunals.classificador import classificar
from tribunals.models import Process


def _proc(tribunal_id, classe_codigo='156'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='0000001-00.2026.8.02.0001')


def test_cumprimento_tjal_com_oficio_f14_vira_precatorio():
    cat, score, _ = classificar(
        _proc('TJAL'), features={'F1_cumprim': 1, 'F14_oficio_text': 1})
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


def test_cumprimento_tjal_com_expedicao_f20_vira_precatorio():
    cat, score, _ = classificar(
        _proc('TJAL'), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1})
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


@pytest.mark.django_db
def test_cumprimento_tjal_sem_sinal_nao_promove():
    cat, score, _ = classificar(
        _proc('TJAL'), features={'F1_cumprim': 1, 'F11_precat_text': 1})
    assert score != 1.0


@pytest.mark.django_db
def test_tjsp_fora_do_escopo_nao_promove():
    cat, score, _ = classificar(
        _proc('TJSP'), features={'F1_cumprim': 1, 'F14_oficio_text': 1})
    assert score != 1.0
