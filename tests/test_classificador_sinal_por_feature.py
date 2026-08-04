"""Regra de sinal escopada por tribunal E por feature.

O TJMG forçou essa separação. Medido em 2026-08-04, em 60 movimentações de
CumSenFaz do TJMG que casam cada feature:

  F14 → 17% é expedição de fato (33% é "Expeça-se ofício requisitório",
        50% ambíguo). Ligar F14 no TJMG = 83% de lead falso, que é a
        reversão de maio/2026 ("despacho não é ofício").
  F20 → 95% é expedição de fato (exige o particípio "expedido", não o
        imperativo "expeça-se").

Daí TJMG entrar só com F20.
"""
from __future__ import annotations

import pytest

from tribunals.classificador import (
    PRECATORIO_SINAL_FEATURES, PRECATORIO_SINAL_TRIBUNAIS, classificar,
)
from tribunals.models import Process


def _proc(tribunal_id, classe_codigo='156'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='0000001-00.2026.8.13.0001')


class TestTJMG:
    """TJMG: promove por F20, NÃO por F14."""

    def test_f20_promove(self):
        cat, score, _ = classificar(
            _proc('TJMG'), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1})
        assert cat == Process.CLASSIF_PRECATORIO
        assert score == 1.0

    @pytest.mark.django_db
    def test_f14_sozinho_NAO_promove(self):
        """O ponto central: 'Expeça-se ofício requisitório' é despacho."""
        _cat, score, _ = classificar(
            _proc('TJMG'), features={'F1_cumprim': 1, 'F14_oficio_text': 1})
        assert score != 1.0

    def test_f14_e_f20_juntos_promovem_pelo_f20(self):
        cat, score, _ = classificar(
            _proc('TJMG'), features={'F1_cumprim': 1, 'F14_oficio_text': 1,
                                     'F20_exp_juriscope': 1})
        assert cat == Process.CLASSIF_PRECATORIO
        assert score == 1.0

    @pytest.mark.django_db
    def test_sem_cumprimento_nao_promove(self):
        _cat, score, _ = classificar(
            _proc('TJMG'), features={'F1_cumprim': 0, 'F20_exp_juriscope': 1})
        assert score != 1.0


class TestNaoRegrediuOsAntigos:
    """TJAL/TJMA/TJSP continuam promovendo por F14 OU F20."""

    @pytest.mark.parametrize('trib', ['TJAL', 'TJMA', 'TJSP'])
    def test_f14_ainda_promove(self, trib):
        cat, score, _ = classificar(
            _proc(trib), features={'F1_cumprim': 1, 'F14_oficio_text': 1})
        assert cat == Process.CLASSIF_PRECATORIO
        assert score == 1.0

    @pytest.mark.parametrize('trib', ['TJAL', 'TJSP'])
    def test_f20_ainda_promove(self, trib):
        cat, score, _ = classificar(
            _proc(trib), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1})
        assert cat == Process.CLASSIF_PRECATORIO
        assert score == 1.0


class TestEscopo:
    @pytest.mark.django_db
    def test_tribunal_fora_do_mapa_nao_promove(self):
        _cat, score, _ = classificar(
            _proc('TJBA'), features={'F1_cumprim': 1, 'F14_oficio_text': 1,
                                     'F20_exp_juriscope': 1})
        assert score != 1.0

    def test_o_set_derivado_bate_com_o_mapa(self):
        assert PRECATORIO_SINAL_TRIBUNAIS == frozenset(PRECATORIO_SINAL_FEATURES)

    def test_tjmg_declarado_apenas_com_f20(self):
        assert PRECATORIO_SINAL_FEATURES['TJMG'] == ('F20_exp_juriscope',)
        assert 'F14_oficio_text' not in PRECATORIO_SINAL_FEATURES['TJMG']
