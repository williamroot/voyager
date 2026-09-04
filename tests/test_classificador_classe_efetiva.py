"""A classe que o classificador enxerga é a FASE, não o campo legado.

Medido em 03/09/2026, investigando "poucos precatórios no TJAL". A migration
0054 (#105) separou cadastro (`classe_cnj_*`) de publicação (`fase_*`) e
`classe_codigo` virou legado. O backfill de fase já correu: `fase_codigo` cobre
100% dos processos, contra 61% (TJMA) / 72% (TJMT) / 95% (TJAL) do campo antigo.

Consequência medida, com o classificador lendo o campo antigo:

    Cumprimento pela FASE mas com F1_cumprim = 0
      TJMA  169.321  (168.524 em NAO_LEAD)
      TJMT  116.295  (116.124 em NAO_LEAD)
      TJAL   11.121  (todos em NAO_LEAD)

    fase_nome = 'PRECATÓRIO' (classe 1265) em NAO_LEAD
      TJMT  23.923  |  TJMA  32.987  |  TJAL  1.264

F1 pesa +1,92, a interação F1×F15 +1,61, e a regra de sinal F14/F20 exige
`F1 == 1` antes de promover — sem F1 o processo não sobe nem pelo LR nem pela
regra.
"""
from __future__ import annotations

import pytest

from tribunals.classificador import (
    _classe_efetiva, _is_classe_precatorio_autuado, classificar, compute_features,
)
from tribunals.models import Process

LIMPO = {'F24_pago_pos_exped_ANTI': 0, 'F19_cancelado_ANTI': 0,
         'F30_extinto_neg_ANTI': 0}


# ---------------------------------------------------------------- _classe_efetiva

def test_fase_ganha_do_campo_legado():
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                classe_codigo='1298', classe_nome='Processo Administrativo',
                fase_codigo='1265', fase_nome='PRECATÓRIO')
    assert _classe_efetiva(p) == ('1265', 'PRECATÓRIO')


def test_cai_para_o_legado_quando_a_fase_nao_foi_provada():
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                classe_codigo='12078', classe_nome='Cumprimento contra a Fazenda',
                fase_codigo='', fase_nome='')
    assert _classe_efetiva(p) == ('12078', 'Cumprimento contra a Fazenda')


def test_par_vem_junto_da_mesma_fonte():
    """Nunca misturar `fase_codigo` com `classe_nome`: o par precisa existir.

    `_is_classe_precatorio_autuado` decide pela COERÊNCIA entre código e nome;
    um par costurado de duas fontes é um fato que não aconteceu.
    """
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                classe_codigo='156', classe_nome='CARTA PRECATÓRIA',
                fase_codigo='1265', fase_nome='PRECATÓRIO')
    assert _classe_efetiva(p) == ('1265', 'PRECATÓRIO')


def test_ambos_vazios_nao_explode():
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003')
    assert _classe_efetiva(p) == ('', '')


# ------------------------------------------------------------------ regra da 1265

def test_1265_so_na_fase_e_reconhecido():
    """Os 26.060 do TJMA e 11.662 do TJMT com `classe_codigo` VAZIO."""
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                classe_codigo='', classe_nome='',
                fase_codigo='1265', fase_nome='PRECATÓRIO')
    assert _is_classe_precatorio_autuado(p) is True


def test_1265_na_fase_promove_a_n1():
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                classe_codigo='1298', classe_nome='Processo Administrativo',
                fase_codigo='1265', fase_nome='PRECATÓRIO')
    cat, score, _ = classificar(p, features=dict(LIMPO))
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


def test_carta_precatoria_na_fase_continua_vetada():
    """O veto da carta precatória tem que sobreviver à troca de fonte."""
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                classe_codigo='1265', classe_nome='PRECATÓRIO',
                fase_codigo='1265', fase_nome='CARTA PRECATÓRIA')
    assert _is_classe_precatorio_autuado(p) is False


def test_1265_com_nome_incoerente_na_fase_nao_promove():
    """Os 320 do TJAL que chegam com código 1265 e nome 'Processo
    Administrativo' — exigir o nome descarta esse ruído."""
    p = Process(tribunal_id='TJAL', numero_cnj='0500199-22.2022.8.02.9003',
                fase_codigo='1265', fase_nome='Processo Administrativo')
    assert _is_classe_precatorio_autuado(p) is False


# ------------------------------------------------------------------------ F1 / F10

@pytest.mark.django_db
def test_f1_liga_pela_fase_quando_o_legado_esta_vazio():
    from tribunals.models import Tribunal
    t = Tribunal.objects.create(sigla='TJXX', nome='X', sigla_djen='TJXX')
    p = Process.objects.create(
        tribunal=t, numero_cnj='0500199-22.2022.8.02.9003',
        classe_codigo='', classe_nome='',
        fase_codigo='12078', fase_nome='CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA')
    f = compute_features(p)
    assert f['F1_cumprim'] == 1


@pytest.mark.django_db
def test_f10_e_f1_leem_a_mesma_fonte():
    """Fontes diferentes fariam o processo ser Cumprimento por um campo e
    Juizado pelo outro — F1 (+1,92) e F10 (−1,13) brigando sobre o mesmo fato."""
    from tribunals.models import Tribunal
    t = Tribunal.objects.create(sigla='TJYY', nome='Y', sigla_djen='TJYY')
    p = Process.objects.create(
        tribunal=t, numero_cnj='0500199-22.2022.8.02.9003',
        classe_codigo='436', classe_nome='Procedimento do Juizado Especial Cível',
        fase_codigo='12078', fase_nome='CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA')
    f = compute_features(p)
    assert f['F1_cumprim'] == 1
    assert f['F10_juizado_ANTI'] == 0


# ---------------------------------------------------------------------------
# A regressão de 03/09/2026: "fase substitui legado" custou 835 rebaixamentos
# ---------------------------------------------------------------------------
# `_classe_efetiva` preferia a fase incondicionalmente. Mas a fase é a classe do
# ÚLTIMO ATO PUBLICADO, não o estágio: um Cumprimento cujo último ato saiu num
# recurso tem fase apontando para o recurso, e o crédito continua lá. Medido nos
# 859 rebaixados de PRECATORIO: 835 tinham `F1_cumprim` indo de 1 para 0.

def _p(classe_codigo='', classe_nome='', fase_codigo='', fase_nome=''):
    return Process(tribunal_id='TJMA', numero_cnj='0800001-11.2024.8.10.0001',
                   classe_codigo=classe_codigo, classe_nome=classe_nome,
                   fase_codigo=fase_codigo, fase_nome=fase_nome)


@pytest.mark.parametrize('classe,fase,esperado,quantos', [
    ('156',   '198',   '156',   377),   # último ato numa Apelação Cível
    ('12078', '1114',  '12078', 221),
    ('12078', '198',   '12078',  62),
    ('12078', '7',     '12078',  50),   # último ato num Procedimento Comum
    ('156',   '436',   '156',    16),   # último ato num Juizado
    ('12078', '460',   '12078',  12),
])
def test_fase_de_incidente_nao_apaga_o_cumprimento(classe, fase, esperado, quantos):
    """Cada par aqui é um caso REAL contado nos 835 rebaixamentos."""
    cod, _ = _classe_efetiva(_p(classe_codigo=classe, fase_codigo=fase))
    assert cod == esperado, f'{quantos} processos dependiam disso'


def test_ganho_original_preservado_fase_supre_legado_vazio():
    """O motivo de a mudança existir: 297k processos com o legado VAZIO."""
    cod, _ = _classe_efetiva(_p(classe_codigo='', fase_codigo='12078'))
    assert cod == '12078'


def test_1265_ganha_de_cumprimento_venha_de_onde_vier():
    """O sinal mais forte primeiro: senão a regra da 1265 nunca dispara."""
    assert _classe_efetiva(_p(classe_codigo='1265', classe_nome='PRECATÓRIO',
                              fase_codigo='156', fase_nome='Cumprimento'))[0] == '1265'
    assert _classe_efetiva(_p(classe_codigo='156', classe_nome='Cumprimento',
                              fase_codigo='1265', fase_nome='PRECATÓRIO'))[0] == '1265'


def test_1265_sobrevive_a_ultimo_ato_em_recurso():
    p = _p(classe_codigo='1265', classe_nome='PRECATÓRIO',
           fase_codigo='198', fase_nome='Apelação Cível')
    assert _is_classe_precatorio_autuado(p) is True


def test_nenhum_dos_dois_e_nicho_a_fase_ganha_por_cobertura():
    cod, nome = _classe_efetiva(_p(classe_codigo='7', classe_nome='Procedimento Comum',
                                   fase_codigo='198', fase_nome='Apelação Cível'))
    assert (cod, nome) == ('198', 'Apelação Cível')
