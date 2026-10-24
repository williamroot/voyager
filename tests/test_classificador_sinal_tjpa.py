"""TJPA: o ofício foi à Coordenadoria de Precatórios (F41) e o teto do LR.

Verdade usada (23/10/2026): 325 processos TJPA com ofício lido pelo JURISCOPE
(148 com precatório, 177 só RPV). Antes, o PRECATORIO do TJPA era 11 com
precatório e 28 só RPV; com o F41, 85 com precatório e 3 só RPV. Ver
`SINAL_PRECATORIO_TJPA_SQL`.

A regex roda no Postgres (`~*`): os testes de texto vão ao banco. Os textos são
trechos reais do DJEN do TJPA, sem nomes.
"""
from __future__ import annotations

import datetime as dt
import io
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from tribunals.classificador import (
    PRECATORIO_SINAL_FEATURES,
    REMESSA_OUTROS_MOTIVOS_SQL,
    SCORE_PROMOCAO_SINAL,
    SCORE_TETO_PRE_SEM_ENVIO,
    SINAL_PRECATORIO_TJPA_SQL,
    TETO_PRE_SEM_SINAL_TRIBUNAIS,
    classificar,
    compute_features,
    f41_precatorio_coord,
)
from tribunals.models import Process

# --- Casam o F41 ----------------------------------------------------------
# 0832423-51.2021.8.14.0301 (UPJ das Varas da Fazenda da Capital), 02/10/2026.
ENVIO_BELEM = (
    'Nos termos do art. 7º, §5º, da Resolução nº 303/2019 do Conselho Nacional de '
    'Justiça, ficam as partes intimadas para, querendo, apresentarem, no prazo de cinco '
    'dias (art. 218, §3º, do CPC/2015), manifestações sobre o ofício requisitório do ID '
    '179948366, a ser enviado à Coordenadoria de Precatórios. Belém, 1 de outubro de 2026.'
)
# 0013274-54.2011.8.14.0301, 17/08/2026 — plural, com "(s)".
ENVIO_BELEM_PLURAL = (
    'ficam as partes intimadas para, querendo, apresentarem, no prazo de cinco dias (art. '
    '218, §3º, do CPC/2015), manifestações sobre o(s) ofício(s) requisitório(s) a ser(em) '
    'enviado(s) à Coordenadoria de Precatórios.. Belém - PA, 14 de agosto de 2026'
)
# 0803908-47.2021.8.14.0061 (Tucuruí), 16/01/2026.
ENVIO_TUCURUI = (
    'Art. 7º, § 5º da Resolução nº 303/2019 do CNJ, ficam intimadas as partes, na pessoa '
    'de seu advogado/procurador, da expedição do Ofício Precatório, a ser enviado à '
    'Coordenadoria de Precatórios do TJPA, para, querendo, apresentarem manifestação no '
    'prazo de 5 (cinco) dias'
)
# 0803507-72.2022.8.14.0074 (Tailândia), 22/10/2026 — a ordem de envio.
ENCAMINHEM_SE = (
    '3. Antes do encaminhamento, intimem-se as partes sobre o inteiro teor das '
    'requisições, no prazo de 5 (cinco) dias. Não havendo impugnação, encaminhem-se os '
    'ofícios requisitórios à coordenadoria de precatórios. Intimem-se. Cumpra-se.'
)
# 0001464-51.2019.8.14.0059 (Soure), 05/10/2026.
DEVOLVIDO = (
    'DECISÃO Vistos, etc. O ofício precatório foi devolvido pela Coordenadoria de '
    'Precatórios porque a planilha juntada não contempla a quantia requisitada.'
)
# 0824465-09.2024.8.14.0301, 30/09/2026 — o formulário publicado.
FORMULARIO = (
    'PODER JUDICIÁRIO TRIBUNAL DE JUSTIÇA DO ESTADO DO PARÁ EXCELENTÍSSIMO SENHOR '
    'PRESIDENTE DO TRIBUNAL DE JUSTIÇA DO ESTADO DO PARÁ OFÍCIO PRECATÓRIO JUÍZO '
    'REQUISITANTE FULANA DE TAL JUÍZO DA FASE DE CONHECIMENTO'
)
# 0001872-34.2013.8.14.0065 (Xinguara), 04/08/2026.
INTEIRO_TEOR = (
    'Nos termos do art. 7º, § 6º, da Resolução nº 303/2019, ficam as partes intimadas do '
    'inteiro teor do Ofício Precatório, documento nº 185145887. Xinguara-PA'
)
# 0020647-97.2015.8.14.0301, 04/02/2026 — o número do precatório no 2º grau.
NUMERO_2G = (
    'devendo ser oficiado à CPREC junto ao processo 0827902-54.2025.8.14.0000 para ciência.'
)

# --- Não casam --------------------------------------------------------------
# 0005468-93.2016.8.14.0138 (Anapu), 20/01/2026 — RPV do INSS.
ANAPU_RPV_INSS = (
    'REQUERIDO: INSS - INSTITUTO NACIONAL DO SEGURO SOCIAL ATO ORDINATÓRIO Comunica-se às '
    'partes interessadas sobre a expedição de Ofício(s) Precatório(s) em favor do(s) '
    'exequente(s) nesta ação judicial, ficando intimadas para manifestação no prazo de '
    '05(cinco) dias, sob pena de preclusão.'
)
# Despacho-padrão das Varas da Fazenda de Belém (sai também na RPV).
DILIGENCIAS_COORDENADORIA = (
    'APÓS A EXPEDIÇÃO DA ORDEM DE PAGAMENTO, SOBREVINDO DILIGÊNCIAS DA COORDENADORIA DE '
    'PRECATÓRIO, DEVERÁ A UPJ PROVIDENCIAR A REGULARIZAÇÃO POR ATO ORDINATÓRIO, '
    'INDEPENDENTE DE CONCLUSÃO. Expedidas as requisições de pagamento, CERTIFIQUE-SE'
)
# 0000055-57.2002.8.14.0052 (São Domingos do Capim), 10/04/2024.
RPV_OU_PRECATORIO = (
    'Expeça-se RPV/precatório com relação aos outros exequentes, como já determinado nos '
    'autos. 4. Expeça-se RPV em relação aos honorários sucumbenciais em nome do patrono.'
)
DETERMINO_EXPEDICAO = (
    'Considerando o valor da condenação e a natureza da parte executada, DETERMINO a '
    'expedição de PRECATÓRIO em favor dos beneficiários, nos termos do art. 535, § 3º.'
)
# 0006134-47.2003.8.14.0301 (só RPV), 18/04/2023.
RETORNE = 'Retorne à Coordenadoria de Precatórios. Após, arquive-se. Cumpra-se.'

REMESSA_OUTROS = 'Remessa · motivo da remessa: outros motivos'
REMESSA_RECURSO = 'Remessa · motivo da remessa: em grau de recurso'


def _regex(texto: str, regex: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, regex])
        return cur.fetchone()[0]


@pytest.mark.django_db
class TestRegexF41:
    @pytest.mark.parametrize('texto', [
        ENVIO_BELEM, ENVIO_BELEM_PLURAL, ENVIO_TUCURUI, ENCAMINHEM_SE, DEVOLVIDO,
        FORMULARIO, INTEIRO_TEOR, NUMERO_2G,
    ])
    def test_oficio_na_coordenadoria_casa(self, texto):
        assert _regex(texto, SINAL_PRECATORIO_TJPA_SQL)

    @pytest.mark.parametrize('texto', [
        ANAPU_RPV_INSS, DILIGENCIAS_COORDENADORIA, RPV_OU_PRECATORIO, DETERMINO_EXPEDICAO,
        RETORNE, REMESSA_OUTROS,
    ])
    def test_nao_casa(self, texto):
        assert not _regex(texto, SINAL_PRECATORIO_TJPA_SQL)

    def test_remessa_outros_motivos(self):
        assert _regex(REMESSA_OUTROS, REMESSA_OUTROS_MOTIVOS_SQL)
        assert not _regex(REMESSA_RECURSO, REMESSA_OUTROS_MOTIVOS_SQL)


class TestFuncao:
    agora = timezone.now()

    def test_texto_basta(self):
        assert f41_precatorio_coord(1, None, None) == 1

    def test_remessa_so_depois_da_expedicao(self):
        d = self.agora
        assert f41_precatorio_coord(0, d, d - dt.timedelta(days=10)) == 1
        assert f41_precatorio_coord(0, d, d) == 1
        assert f41_precatorio_coord(0, d - dt.timedelta(days=10), d) == 0
        assert f41_precatorio_coord(0, d, None) == 0
        assert f41_precatorio_coord(0, None, d) == 0


def _proc(tribunal_id='TJPA', classe_codigo='156'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='0832423-51.2021.8.14.0301')


def _classificar_com_lr(proc, features, cat_lr, score=0.95):
    with mock.patch('tribunals.classificador.predict_score', return_value=score), \
         mock.patch('tribunals.classificador._current_weights', return_value={}), \
         mock.patch('tribunals.classificador._categorizar', return_value=cat_lr):
        return classificar(proc, features=features)


class TestRegra:
    def test_tjpa_promove_so_pelo_f41(self):
        assert PRECATORIO_SINAL_FEATURES['TJPA'] == ('F41_precatorio_coord_tjpa',)
        assert 'TJPA' in TETO_PRE_SEM_SINAL_TRIBUNAIS

    @pytest.mark.parametrize('cat_lr', [
        Process.CLASSIF_PRE_PRECATORIO, Process.CLASSIF_DIREITO_CREDITORIO,
        Process.CLASSIF_NAO_LEAD])
    def test_f41_promove_a_precatorio(self, cat_lr):
        cat, score, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1, 'F41_precatorio_coord_tjpa': 1}, cat_lr)
        assert cat == Process.CLASSIF_PRECATORIO
        assert score == SCORE_PROMOCAO_SINAL

    def test_sem_cumprimento_nao_promove(self):
        cat, _, _ = _classificar_com_lr(
            _proc(classe_codigo='7'), {'F1_cumprim': 0, 'F41_precatorio_coord_tjpa': 1},
            Process.CLASSIF_NAO_LEAD)
        assert cat == Process.CLASSIF_NAO_LEAD

    def test_lr_sozinho_para_em_pre(self):
        """Era o caminho da RPV: 28 dos 39 PRECATORIO com ofício eram só RPV."""
        cat, score, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1, 'F11_precat_text': 1, 'F41_precatorio_coord_tjpa': 0},
            Process.CLASSIF_PRECATORIO)
        assert cat == Process.CLASSIF_PRE_PRECATORIO
        assert score <= SCORE_TETO_PRE_SEM_ENVIO

    def test_teto_nao_sobe_quem_estava_abaixo(self):
        cat, score, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1}, Process.CLASSIF_DIREITO_CREDITORIO, score=0.3)
        assert (cat, score) == (Process.CLASSIF_DIREITO_CREDITORIO, 0.3)

    def test_extincao_negativa_ainda_rebaixa_o_teto(self):
        cat, _, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1, 'F30_extinto_neg_ANTI': 1}, Process.CLASSIF_PRECATORIO)
        assert cat == Process.CLASSIF_NAO_LEAD

    @pytest.mark.parametrize('tribunal', ['TJMG', 'TRF1', 'TJMA'])
    def test_fora_do_tjpa_nao_mexe(self, tribunal):
        cat, _, _ = _classificar_com_lr(
            _proc(tribunal), {'F1_cumprim': 1, 'F11_precat_text': 1,
                              'F41_precatorio_coord_tjpa': 0},
            Process.CLASSIF_PRECATORIO)
        assert cat == Process.CLASSIF_PRECATORIO


def _criar(cnj, movs, tribunal='TJPA', classe='156', classificacao=None):
    """movs: [(dias atrás, texto, tipo_comunicacao)]."""
    from tribunals.models import Movimentacao, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla=tribunal, defaults={'nome': tribunal, 'sigla_djen': tribunal})
    kw = {'classificacao': classificacao} if classificacao else {}
    p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo=classe, **kw)
    agora = timezone.now()
    for i, (dias, texto, tipo) in enumerate(movs):
        Movimentacao.objects.create(
            processo=p, tribunal=t, external_id=f'{cnj}-{i}', texto=texto,
            tipo_comunicacao=tipo, data_disponibilizacao=agora - dt.timedelta(days=dias))
    return p


@pytest.mark.django_db
class TestNoBanco:
    """Movimentações reais → features → categoria, de ponta a ponta no Postgres."""

    def test_envio_a_coordenadoria_vira_precatorio(self):
        p = _criar('0832423-51.2021.8.14.0301', [
            (40, RPV_OU_PRECATORIO, 'Intimação'), (20, ENVIO_BELEM, 'Intimação')])
        feats = compute_features(p)
        assert feats['F41_precatorio_coord_tjpa'] == 1
        assert classificar(p, features=feats)[0] == Process.CLASSIF_PRECATORIO

    def test_remessa_depois_da_expedicao(self):
        p = _criar('0892898-02.2023.8.14.0301', [
            (60, '', 'Expedição de precatório/rpv'), (30, REMESSA_OUTROS, 'Remessa')])
        assert compute_features(p)['F41_precatorio_coord_tjpa'] == 1

    def test_remessa_sem_expedicao_nao_conta(self):
        p = _criar('0809590-34.2024.8.14.0301', [(30, REMESSA_OUTROS, 'Remessa')])
        assert compute_features(p)['F41_precatorio_coord_tjpa'] == 0

    def test_rpv_do_inss_nao_conta(self):
        p = _criar('0005468-93.2016.8.14.0138', [(10, ANAPU_RPV_INSS, 'Intimação')])
        assert compute_features(p)['F41_precatorio_coord_tjpa'] == 0

    def test_mesmo_texto_fora_do_tjpa_nao_conta(self):
        p = _criar('5011469-11.2023.8.13.0261', [(10, ENVIO_BELEM, 'Intimação')],
                   tribunal='TJMG')
        assert compute_features(p)['F41_precatorio_coord_tjpa'] == 0


@pytest.mark.django_db
def test_dry_run_mostra_o_destino():
    _criar('0832423-51.2021.8.14.0301', [(20, ENVIO_BELEM, 'Intimação')],
           classificacao='PRE_PRECATORIO')
    _criar('0833989-35.2021.8.14.0301', [(20, RPV_OU_PRECATORIO, 'Intimação')],
           classificacao='PRECATORIO')
    _criar('0000055-57.2002.8.14.0052', [(20, RPV_OU_PRECATORIO, 'Intimação')],
           classificacao='PRE_PRECATORIO')
    _criar('0001872-34.2013.8.14.0065', [(20, INTEIRO_TEOR, 'Intimação')],
           classificacao='NAO_LEAD')
    _criar('0800000-00.2026.8.14.0301', [(20, ENVIO_BELEM, 'Intimação')],
           classe='7', classificacao='NAO_LEAD')  # sem Cumprimento: fora do alvo

    out = io.StringIO()
    call_command('reclassificar_tjpa_precatorio', '--dry-run', stdout=out)
    s = out.getvalue()
    assert 'alvo=3' in s
    assert 'TJPA PRE_PRECATORIO       F41  -> PRECATORIO           1' in s
    assert 'TJPA PRECATORIO           teto -> PRE_PRECATORIO       1' in s
    assert 'TJPA PRE_PRECATORIO       -    -> PRE_PRECATORIO       1' in s
    assert 'mudam=2' in s

    out = io.StringIO()
    call_command('reclassificar_tjpa_precatorio', '--dry-run', '--incluir-nao-lead', stdout=out)
    s = out.getvalue()
    assert 'alvo=4' in s
    assert 'TJPA NAO_LEAD             F41  -> PRECATORIO           1' in s


@pytest.mark.django_db
def test_apply_enfileira_so_quem_muda():
    from tribunals.management.commands import reclassificar_tjpa_precatorio as cmd
    muda = _criar('0832423-51.2021.8.14.0301', [(20, ENVIO_BELEM, 'Intimação')],
                  classificacao='PRE_PRECATORIO')
    _criar('0000055-57.2002.8.14.0052', [(20, RPV_OU_PRECATORIO, 'Intimação')],
           classificacao='PRE_PRECATORIO')
    fila = mock.Mock()
    with mock.patch.object(cmd.django_rq, 'get_queue', return_value=fila):
        call_command('reclassificar_tjpa_precatorio', '--apply', stdout=io.StringIO())
    assert fila.enqueue.call_count == 1
    assert fila.enqueue.call_args.args[1] == [muda.pk]
