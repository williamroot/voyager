"""Regra de sinal do TJRS: F31 (intimação da expedição do precatório).

Medido em 2026-10-14 contra 41 processos TJRS com os autos completos baixados
do eproc (ofício conferido na íntegra): F31 casa 30/32 com ofício e 0/9 sem.
O F20 no TJRS casava a ORDEM ("determinada expedição", 0/4 com ofício) e a RPV
SEFAZ — por isso o TJRS entra só com F31.

A regex roda no Postgres (`~*`), não no Python: os testes de texto vão ao banco
para medir a semântica que vale em produção. Os textos são trechos reais do
DJEN do TJRS — inclusive o HTML com acento em entidade, que é a forma em que
TODOS os acertos da amostra chegaram.
"""
from __future__ import annotations

import pytest
from django.db import connection

from tribunals.classificador import (
    PRECATORIO_SINAL_FEATURES, SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL, classificar,
)
from tribunals.models import Process


def _proc(tribunal_id='TJRS', classe_codigo='12078'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='5023148-96.2024.8.21.0019')


def _casa(texto: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL])
        return cur.fetchone()[0]


class TestRegraTJRS:
    def test_f31_promove(self):
        cat, score, _ = classificar(
            _proc(), features={'F1_cumprim': 1, 'F31_exp_precatorio_tjrs': 1})
        assert cat == Process.CLASSIF_PRECATORIO
        assert score == 1.0

    @pytest.mark.django_db
    def test_f20_sozinho_NAO_promove_no_tjrs(self):
        """'Determinada expedição' e RPV SEFAZ casam F20 — não são ofício."""
        _cat, score, _ = classificar(
            _proc(), features={'F1_cumprim': 1, 'F20_exp_juriscope': 1})
        assert score != 1.0

    @pytest.mark.django_db
    def test_sem_cumprimento_nao_promove(self):
        _cat, score, _ = classificar(
            _proc(), features={'F1_cumprim': 0, 'F31_exp_precatorio_tjrs': 1})
        assert score != 1.0

    @pytest.mark.django_db
    def test_f31_nao_vale_fora_do_tjrs(self):
        """O ato ordinatório é do eproc TJRS; outro tribunal não ganha a regra."""
        _cat, score, _ = classificar(
            _proc('TJMG'), features={'F1_cumprim': 1, 'F31_exp_precatorio_tjrs': 1})
        assert score != 1.0

    def test_tjrs_so_com_f31(self):
        assert PRECATORIO_SINAL_FEATURES['TJRS'] == ('F31_exp_precatorio_tjrs',)


@pytest.mark.django_db
class TestRegexNoPostgres:
    @pytest.mark.parametrize('texto', [
        # Forma em que chegam os acertos reais: HTML com entidade.
        '<p>Ficam as partes intimadas da expedi&ccedil;&atilde;o do(s) '
        'precat&oacute;rio(s) para, querendo, manifestarem-se no prazo de 5 dias.',
        # Mesma frase com acento literal (DJEN em texto puro).
        'Ficam as partes intimadas da expedição do(s) precatório(s) para, '
        'querendo, manifestarem-se no prazo de 5 dias. Inexistindo oposição, '
        'os autos serão distribuídos no sistema eproc 2g.',
        # Variante no singular, vista na amostra.
        'as partes foram intimadas da expedi&ccedil;&atilde;o do '
        'precat&oacute;rio no Evento 67, tendo o',
    ])
    def test_casa_expedicao(self, texto):
        assert _casa(texto)

    @pytest.mark.parametrize('texto', [
        # Ordem do juiz, não expedição (4/9 dos sem ofício).
        'Expeça-se, conforme o caso, precatório (na forma do art. 535, §3, I) '
        'ou RPV (nos termos do art. 535, §3º, II), em favor da parte credora.',
        'Determinada expedição de precatório.',
        # RPV não é precatório.
        'Ficam as partes intimadas da expedição da(s) RPV(s) para, querendo, '
        'manifestarem-se.',
        # Pedido de dados para expedir — ainda não expedido.
        'Para fins de expedição do precatório, intimo a exequente para '
        'minudenciar as informações requeridas no formulário.',
    ])
    def test_nao_casa(self, texto):
        assert not _casa(texto)
