from __future__ import annotations

import pytest

from tribunals.classificador import PRECATORIO_SINAL_FEATURES, classificar
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
def test_tribunal_fora_do_escopo_nao_promove():
    """A regra de sinal é escopada por tribunal — quem está fora não sobe.

    O exemplo sai de `PRECATORIO_SINAL_FEATURES`, não de uma sigla escrita à
    mão. Este teste usava 'TJSP' e passou a falhar em 06/07/2026, quando
    `d556ca6` ligou a regra no TJSP de propósito (com comando dedicado) e
    ninguém veio atualizar os dois testes que usavam o TJSP como exemplo de
    "fora". Ficou vermelho por dois meses afirmando um fato que tinha deixado
    de ser verdade. Derivando do código, isso não se repete.
    """
    fora = next(t for t in ('TRF1', 'TRF3', 'TJMS', 'TJPR')
                if t not in PRECATORIO_SINAL_FEATURES)
    cat, score, _ = classificar(
        _proc(fora), features={'F1_cumprim': 1, 'F14_oficio_text': 1})
    assert score != 1.0
