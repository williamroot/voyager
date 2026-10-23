"""Regra do "Processo Administrativo" (1298) de comarca do TJAL publicado pelo
órgão de precatórios — o mesmo tratamento da classe 1265.

Medido em 23/10/2026: 169 processos TJAL 1298 NAO_LEAD com publicação da
Diretoria de Precatórios. Os 63 de foro de comarca são requisição individual
(credor e devedor no cabeçalho); os 106 do foro 9003 são o acompanhamento de
repasses do ente devedor, sem credor — ficam fora.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from tribunals import classificador
from tribunals.classificador import classificar
from tribunals.models import Movimentacao, Process, Tribunal

LIMPO = {'F24_pago_pos_exped_ANTI': 0, 'F19_cancelado_ANTI': 0,
         'F30_extinto_neg_ANTI': 0}
COMARCA = '0500205-98.2024.8.02.0001'
ACOMPANHAMENTO_9003 = '0500029-89.2018.8.02.9003'


def _proc(cnj=COMARCA, classe='1298', classe_nome='Processo Administrativo',
          fase='1298', fase_nome='PROCESSO ADMINISTRATIVO', tribunal='TJAL'):
    return Process(tribunal_id=tribunal, numero_cnj=cnj, classe_codigo=classe,
                   classe_nome=classe_nome, fase_codigo=fase, fase_nome=fase_nome)


@pytest.fixture
def orgao_publicou(monkeypatch):
    monkeypatch.setattr(classificador, '_publicado_por_orgao_de_precatorio',
                        lambda p: True)


@pytest.fixture
def orgao_nao_publicou(monkeypatch):
    monkeypatch.setattr(classificador, '_publicado_por_orgao_de_precatorio',
                        lambda p: False)


def test_pa_comarca_com_publicacao_do_orgao_vira_precatorio(orgao_publicou):
    cat, score, _ = classificar(_proc(), features=dict(LIMPO))
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


def test_codigo_1265_com_nome_de_pa_e_fase_1298_tambem_entra(orgao_publicou):
    """5 dos 169: classe_codigo 1265 com nome 'Processo Administrativo' (que a
    regra da 1265 recusa pela incoerência) e fase 1298."""
    cat, score, _ = classificar(_proc(classe='1265'), features=dict(LIMPO))
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0


def test_so_a_fase_1298_basta(orgao_publicou):
    cat, _, _ = classificar(_proc(classe='', classe_nome=''), features=dict(LIMPO))
    assert cat == Process.CLASSIF_PRECATORIO


@pytest.mark.django_db
@pytest.mark.parametrize('veto', sorted(LIMPO))
def test_vetos_da_1265_valem_para_o_pa(orgao_publicou, veto):
    cat, score, _ = classificar(_proc(), features={**LIMPO, veto: 1})
    assert cat != Process.CLASSIF_PRECATORIO
    assert score != 1.0


@pytest.mark.django_db
def test_sem_publicacao_do_orgao_nao_promove(orgao_nao_publicou):
    """PA de comarca pode ser Júri, suscitação de dúvida, prestação pecuniária."""
    _, score, _ = classificar(_proc(), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_foro_9003_nao_promove(orgao_publicou):
    """Acompanhamento de repasses do município: sem credor, não é lead."""
    _, score, _ = classificar(_proc(cnj=ACOMPANHAMENTO_9003), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
@pytest.mark.parametrize('cnj', ['0700103-71.2017.8.02.0055',   # não começa com 050
                                 '0500205-98.2024.8.26.0001'])  # outro tribunal no CNJ
def test_numeracao_fora_do_padrao_nao_promove(orgao_publicou, cnj):
    _, score, _ = classificar(_proc(cnj=cnj), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_outro_tribunal_nao_promove(orgao_publicou):
    _, score, _ = classificar(_proc(tribunal='TJSP'), features=dict(LIMPO))
    assert score != 1.0


@pytest.mark.django_db
def test_outra_classe_nao_promove(orgao_publicou):
    _, score, _ = classificar(
        _proc(classe='7', classe_nome='Procedimento Comum Cível', fase='7',
              fase_nome='PROCEDIMENTO COMUM CÍVEL'), features=dict(LIMPO))
    assert score != 1.0


# --- a consulta do órgão, no banco ---

def _salvo(cnj=COMARCA):
    t, _ = Tribunal.objects.get_or_create(sigla='TJAL', defaults={'nome': 'TJAL'})
    return Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo='1298',
                                  classe_nome='Processo Administrativo', fase_codigo='1298')


def _mov(p, orgao, ext):
    return Movimentacao.objects.create(
        processo=p, tribunal=p.tribunal, external_id=ext, nome_orgao=orgao,
        data_disponibilizacao='2026-03-09T00:00:00Z')


@pytest.mark.django_db
@pytest.mark.parametrize('orgao', ['Diretoria de Precatório e RPV - Presidência',
                                   'Precatório/Presidência'])
def test_orgaos_medidos_contam(orgao):
    p = _salvo()
    _mov(p, '16ª Vara Cível da Capital / Fazenda Estadual', 'a')
    _mov(p, orgao, 'b')
    assert classificador._is_pa_precatorio_comarca(p) is True


@pytest.mark.django_db
def test_so_publicacao_da_vara_nao_conta():
    p = _salvo()
    _mov(p, '16ª Vara Cível da Capital / Fazenda Estadual', 'a')
    _mov(p, 'Foro de Maceió - 16ª Vara Cível da Capital / Fazenda Estadual', 'b')
    assert classificador._is_pa_precatorio_comarca(p) is False


@pytest.mark.django_db
def test_9003_com_orgao_continua_fora():
    p = _salvo(ACOMPANHAMENTO_9003)
    _mov(p, 'Diretoria de Precatório e RPV - Presidência', 'a')
    assert classificador._is_pa_precatorio_comarca(p) is False


# --- o backfill ---

@pytest.mark.django_db
def test_reclassificar_alvo_pa_comarca_conta_so_o_recorte():
    t, _ = Tribunal.objects.get_or_create(sigla='TJAL', defaults={'nome': 'TJAL'})
    Process.objects.create(tribunal=t, numero_cnj=COMARCA, classe_codigo='1298',
                           classificacao=Process.CLASSIF_NAO_LEAD)
    Process.objects.create(tribunal=t, numero_cnj='0500003-71.2024.8.02.0050',
                           classe_codigo='', fase_codigo='1298')
    # fora: 9003, já PRECATORIO, outra classe, sem prefixo 050
    Process.objects.create(tribunal=t, numero_cnj=ACOMPANHAMENTO_9003,
                           classe_codigo='1298')
    Process.objects.create(tribunal=t, numero_cnj='0500006-26.2024.8.02.0050',
                           classe_codigo='1298', classificacao=Process.CLASSIF_PRECATORIO)
    Process.objects.create(tribunal=t, numero_cnj='0500007-26.2024.8.02.0050',
                           classe_codigo='156')
    Process.objects.create(tribunal=t, numero_cnj='0700103-71.2017.8.02.0055',
                           classe_codigo='1298')
    out = StringIO()
    call_command('reclassificar_tjal_precatorio', '--alvo', 'pa-comarca',
                 '--dry-run', stdout=out)
    assert 'alvo=2 ' in out.getvalue()
