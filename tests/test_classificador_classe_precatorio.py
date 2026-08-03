"""Regra de sinal por CLASSE: a 1265 'Precatório' é o precatório já autuado.

Ponto cego medido em 2026-08-03: 8.598 processos TJAL de classe 1265 (o
precatório de 2º grau, com credor/CPF/valor na consulta pública) estavam TODOS
como NAO_LEAD, score médio 0,055 — o LR foi treinado no precursor
('Cumprimento contra a Fazenda', F1) e a 1265 tem F1=0.
"""
from __future__ import annotations

import pytest

from tribunals.classificador import classificar
from tribunals.models import Process

LIMPO = {'F24_pago_pos_exped_ANTI': 0, 'F19_cancelado_ANTI': 0,
         'F30_extinto_neg_ANTI': 0}


def _proc(tribunal_id='TJAL', classe_codigo='1265', classe_nome='PRECATÓRIO',
          cnj='0500199-22.2022.8.02.9003'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   classe_nome=classe_nome, numero_cnj=cnj)


def test_classe_1265_tjal_vira_precatorio_n1():
    cat, score, _ = classificar(_proc(), features=dict(LIMPO))
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


def test_promove_mesmo_fora_do_foro_9003():
    """A classe é o sinal; o foro 9003 é artefato local do TJAL.

    1.113 dos 8.598 estão em outro foro e são precatório do mesmo jeito."""
    cat, score, _ = classificar(
        _proc(cnj='0500043-29.2025.8.02.0001'), features=dict(LIMPO))
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


# --- vetos: o crédito não existe mais ---

@pytest.mark.django_db
def test_precatorio_pago_nao_vira_lead():
    cat, score, _ = classificar(
        _proc(), features={**LIMPO, 'F24_pago_pos_exped_ANTI': 1})
    assert cat != Process.CLASSIF_PRECATORIO
    assert score != 1.0


@pytest.mark.django_db
def test_precatorio_cancelado_nao_vira_lead():
    cat, score, _ = classificar(
        _proc(), features={**LIMPO, 'F19_cancelado_ANTI': 1})
    assert cat != Process.CLASSIF_PRECATORIO
    assert score != 1.0


@pytest.mark.django_db
def test_precatorio_com_desfecho_negativo_nao_vira_lead():
    cat, score, _ = classificar(
        _proc(), features={**LIMPO, 'F30_extinto_neg_ANTI': 1})
    assert cat != Process.CLASSIF_PRECATORIO
    assert score != 1.0


# --- precisão: o que NÃO pode ser promovido ---

@pytest.mark.django_db
def test_codigo_1265_com_nome_processo_administrativo_nao_promove():
    """65 processos TJAL chegam do DJEN com código 1265 e este nome."""
    cat, score, _ = classificar(
        _proc(classe_nome='Processo Administrativo'), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_carta_precatoria_nao_promove():
    cat, score, _ = classificar(
        _proc(classe_nome='Carta Precatória Cível'), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_classe_diferente_nao_promove():
    cat, score, _ = classificar(
        _proc(classe_codigo='156', classe_nome='CUMPRIMENTO DE SENTENÇA'),
        features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_outro_tribunal_fora_do_rollout_nao_promove():
    """A 1265 existe em todo o fleet (~140k), mas só entra onde o JURISCOPE
    sabe ler o processo. Ampliar exige o leitor correspondente."""
    cat, score, _ = classificar(_proc(tribunal_id='TJSP'), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_nome_vazio_nao_promove():
    cat, score, _ = classificar(_proc(classe_nome=''), features=dict(LIMPO))
    assert score != 1.0


def test_regra_de_cumprimento_existente_continua_valendo():
    """Não pode ter regredido a promoção por F14/F20 (PR#5)."""
    cat, score, _ = classificar(
        _proc(classe_codigo='156', classe_nome='CUMPRIMENTO DE SENTENÇA'),
        features={'F1_cumprim': 1, 'F14_oficio_text': 1})
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0
