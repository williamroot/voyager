"""Regra de sinal do TJRS: F31 (evento de ENVIO da requisição do precatório).

16/10/2026: a intimação da expedição saiu do F31 e virou o F33 (minuta) — teto
PRE_PRECATORIO. Ver `TestMinutaNaoEhEnvio` no fim do arquivo.

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
    PRECATORIO_SINAL_FEATURES,
    SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL,
    classificar,
)
from tribunals.models import Process


def _proc(tribunal_id='TJRS', classe_codigo='12078'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='5023148-96.2024.8.21.0019')


def _casa(texto: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL])
        return cur.fetchone()[0]


def _minuta(texto: str) -> bool:
    from tribunals.classificador import INTIMACAO_EXPEDICAO_PRECATORIO_TJRS_SQL
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, INTIMACAO_EXPEDICAO_PRECATORIO_TJRS_SQL])
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
    def test_intimacao_e_minuta_nao_envio(self, texto):
        """A intimação da expedição é a fase de MINUTA: casa o F33, não o F31."""
        assert _minuta(texto)
        assert not _casa(texto)

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
        assert not _minuta(texto)


# ─────────────────────────────────────────────────────────────────────────────
# F31 ampliado (eventos do eproc republicados no DJEN) e F32 (conversão em RPV)
# ─────────────────────────────────────────────────────────────────────────────
# Textos: trechos REAIS do DJEN do TJRS (15/10/2026), nomes fora. Verdade de
# cada caso conferida nos eventos públicos do eproc e no estado do precatório
# no 2º grau (consulta processual pública do TJRS).

import datetime as _dt  # noqa: E402

from tribunals.classificador import (  # noqa: E402
    CONVERSAO_RPV_TJRS_SQL,
    VETO_CONVERSAO_RPV_TJRS_SQL,
    compute_features,
    f32_convertido,
)


def _regex(texto: str, regex: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, regex])
        return cur.fetchone()[0]


def _converte(texto: str) -> bool:
    return _regex(texto, CONVERSAO_RPV_TJRS_SQL) and not _regex(texto, VETO_CONVERSAO_RPV_TJRS_SQL)


@pytest.mark.django_db
class TestF31Eventos:
    @pytest.mark.parametrize('texto', [
        'ATO ORDINATÓRIO Intimação realizada no sistema eproc. O ato refere-se ao '
        'seguinte evento: Evento 41 - 08/01/2026 - Requisição de pagamento de '
        'precatório preparada para envio',
        'O ato refere-se ao seguinte evento: Evento 42 - Requisi&ccedil;&atilde;o '
        'de pagamento de precat&oacute;rio enviada ao tribunal - Precatorio 280031',
    ])
    def test_eventos_de_envio_contam_como_expedicao(self, texto):
        assert _casa(texto)

    @pytest.mark.parametrize('texto', [
        # RPV não é precatório.
        'Evento 42 - 14/10/2026 - Requisição de pagamento de pequeno valor expedida',
        'Requisição de pagamento de pequeno valor enviada - RPV NR 260068055 '
        'transmitida para SEFAZ/RS',
    ])
    def test_eventos_de_rpv_nao_contam(self, texto):
        assert not _casa(texto)


@pytest.mark.django_db
class TestF32Regex:
    @pytest.mark.parametrize('texto', [
        # 5232029-64.2026.8.21.0001 — o caso do QA (HTML com entidade).
        '2&nbsp; Porque direito dispon&iacute;vel,&nbsp; homologo&nbsp;a&nbsp;'
        'ren&uacute;ncia&nbsp;ao cr&eacute;dito no que exceda ao&nbsp;limite de dez '
        'sal&aacute;rios m&iacute;nimos nacionais &nbsp;-&nbsp; evento 26, TERMREN2 '
        '3 Cancele-se&nbsp;o precat&oacute;rio expedido &nbsp;-&nbsp; evento 18',
        'DESPACHO/DECIS&Atilde;O Cancelado o Precat&oacute;rio, expe&ccedil;a-se a&nbsp;RPV.',
        'HOMOLOGO a renúncia ao crédito excedente a 10 salários mínimos ( evento 73, '
        'TERMREN2 ). Cancele-se o Precatório n. 52133682620258217000.',
        'Em consequência, homologo a renúncia do exequente ao valor do crédito que '
        'excede o teto para pagamento por Requisição de Pequeno Valor',
        'HOMOLOGO a renúncia da parte autora ao valor excedente a 30 salários-mínimos',
        '1. Considerando o requerido pela parte autora, cancele-se o precatório expedido.',
    ])
    def test_conversao_casa(self, texto):
        assert _converte(texto)

    @pytest.mark.parametrize('texto', [
        # Renúncia dos HONORÁRIOS: o precatório do principal continua (2º grau vivo).
        'homologo a renúncia ao crédito dos honorários advocatícios no que exceda ao '
        'limite de dez salários mínimos nacionais - evento 121, PET1 . 3 Cancele-se o '
        'precatório expedido - evento 115, PRECATÓRIO1 .',
        'homologo a renúncia ao crédito dos valores dos honorários advocatícios no que '
        'exceda ao limite de dez salários mínimos nacionais',
        # Renúncia de UM litisconsorte; o precatório do outro segue.
        'homologo a renúncia ao crédito da exequente FULANA no que exceda ao limite. '
        '3 Cancele-se o precatório - evento 46. 4 Prossiga-se com a expedição do '
        'precatório do exequente BELTRANO.',
        # Parte do crédito; o precatório do valor principal segue.
        'homologo a renúncia ao crédito no que exceda ao limite de dez salários '
        'mínimos. 3 Cancele-se o precatório expedido. ANDAMENTO PROCESSUAL: - '
        'prossiga-se com a distribuição do precatório do valor principal;',
        # Ordem de expedição, RPV e alvará: nada disso é conversão.
        'prossiga-se com a expedição do precatório e intimem-se as partes',
        'Evento 42 - Requisição de pagamento de pequeno valor expedida',
        'Expedição de alvará eletrônico automatizado - NR 1234',
        # Renúncia ao PRAZO não é renúncia ao crédito.
        'PETIÇÃO PROTOCOLADA JUNTADA - CIÊNCIA, COM RENÚNCIA AO PRAZO',
    ])
    def test_nao_e_conversao(self, texto):
        assert not _converte(texto)


class TestF32Regra:
    def test_conversao_depois_da_expedicao(self):
        d = _dt.datetime
        assert f32_convertido(d(2026, 10, 2), d(2026, 10, 9)) == 1
        assert f32_convertido(d(2026, 10, 2), d(2026, 10, 2)) == 1
        # Precatório expedido DE NOVO depois da renúncia: vale a expedição.
        assert f32_convertido(d(2026, 10, 9), d(2026, 10, 2)) == 0
        assert f32_convertido(None, d(2026, 10, 2)) == 0
        assert f32_convertido(d(2026, 10, 2), None) == 0

    def test_f32_impede_a_promocao_do_f31(self):
        cat, score, _ = classificar(_proc(), features={
            'F1_cumprim': 1, 'F31_exp_precatorio_tjrs': 1, 'F32_convertido_rpv_tjrs': 1})
        assert cat == Process.CLASSIF_NAO_LEAD
        assert score <= 0.10

    def test_sem_f32_continua_promovendo(self):
        cat, score, _ = classificar(_proc(), features={
            'F1_cumprim': 1, 'F31_exp_precatorio_tjrs': 1, 'F32_convertido_rpv_tjrs': 0})
        assert (cat, score) == (Process.CLASSIF_PRECATORIO, 1.0)

    @pytest.mark.django_db
    def test_f32_nao_vale_fora_do_tjrs(self):
        """TJMG com F32=1 (impossível na prática: a SQL só olha TJRS) não muda."""
        _cat, score, _ = classificar(_proc('TJMG'), features={
            'F1_cumprim': 1, 'F20_exp_juriscope': 1, 'F32_convertido_rpv_tjrs': 1})
        assert score == 1.0


@pytest.mark.django_db
class TestF32NoBanco:
    """O caso do QA reproduzido de ponta a ponta no Postgres: DJEN real do
    5232029-64.2026.8.21.0001 (sem nomes) → features → NAO_LEAD."""

    def _processo(self, textos):
        from tribunals.models import Movimentacao, Tribunal
        t, _ = Tribunal.objects.get_or_create(
            sigla='TJRS', defaults={'nome': 'TJ RS', 'sigla_djen': 'TJRS'})
        p = Process.objects.create(tribunal=t, numero_cnj='5232029-64.2026.8.21.0001',
                                   classe_codigo='12078')
        for i, (dia, texto) in enumerate(textos):
            Movimentacao.objects.create(
                processo=p, tribunal=t, external_id=f'qa-{i}', texto=texto,
                data_disponibilizacao=_dt.datetime(2026, *dia, 3, tzinfo=_dt.UTC))
        return p

    F31 = ('ATO ORDINAT&Oacute;RIO Ficam as partes intimadas da expedi&ccedil;&atilde;o '
           'do(s) precat&oacute;rio(s) para, querendo, manifestarem-se no prazo de 5 dias.')
    CONV = ('2&nbsp; Porque direito dispon&iacute;vel,&nbsp; homologo&nbsp;a&nbsp;'
            'ren&uacute;ncia&nbsp;ao cr&eacute;dito no que exceda ao&nbsp;limite de dez '
            'sal&aacute;rios m&iacute;nimos nacionais 3 Cancele-se&nbsp;o precat&oacute;rio '
            'expedido &nbsp;-&nbsp; evento 18, PRECAT&Oacute;RIO1')
    ENVIO = ('ATO ORDINAT&Oacute;RIO Intima&ccedil;&atilde;o realizada no sistema eproc. O ato '
             'refere-se ao seguinte evento: Evento 65 - Requisi&ccedil;&atilde;o de pagamento de '
             'precat&oacute;rio enviada ao tribunal - Precatorio 272595')
    RPV = ('ATO ORDINATÓRIO Intimação realizada no sistema eproc. O ato refere-se ao '
           'seguinte evento: Evento 42 - 14/10/2026 - Requisição de pagamento de '
           'pequeno valor expedida')

    def test_caso_do_qa_vira_nao_lead(self):
        """A renúncia veio no prazo da minuta, antes do envio: a minuta conta
        como "última expedição" para o F32."""
        p = self._processo([((10, 2), self.F31), ((10, 9), self.CONV), ((10, 15), self.RPV)])
        feats = compute_features(p)
        assert feats['F31_exp_precatorio_tjrs'] == 0
        assert feats['F33_minuta_precatorio_tjrs'] == 1
        assert feats['F32_convertido_rpv_tjrs'] == 1
        cat, _score, _ = classificar(p, features=feats)
        assert cat == Process.CLASSIF_NAO_LEAD

    def test_so_rpv_depois_do_f31_continua_precatorio(self):
        """RPV de honorários depois do precatório é o caso COMUM (77/85)."""
        p = self._processo([((10, 2), self.F31), ((10, 8), self.ENVIO), ((10, 15), self.RPV)])
        feats = compute_features(p)
        assert feats['F32_convertido_rpv_tjrs'] == 0
        assert classificar(p, features=feats)[0] == Process.CLASSIF_PRECATORIO


# ─────────────────────────────────────────────────────────────────────────────
# Minuta (F33) versus envio (F31) — 16/10/2026
# ─────────────────────────────────────────────────────────────────────────────
# O pull de 16/10 baixou 7 processos TJRS; 6 eram minuta (rascunho no
# JURISCOPE, sem ofício): "Juntada de peças digitalizadas" + ato ordinatório +
# intimação de 5 dias em 15/10, DJEN em 16/10. O único lead (5000214-04.2012.
# 8.21.0040) tinha "preparada para envio" e "enviada ao tribunal". Textos
# abaixo: DJEN real do TJRS, nomes fora.

MINUTA_HTML = (
    '<p>ATO ORDINAT&Oacute;RIO</p><p>Ficam as partes intimadas da expedi&ccedil;'
    '&atilde;o do(s) precat&oacute;rio(s) para, querendo, manifestarem-se no prazo '
    'de 5 dias. Inexistindo oposi&ccedil;&atilde;o, os autos ser&atilde;o '
    'distribu&iacute;dos no sistema eproc 2g.</p>'
)
MINUTA_TEXTO = (
    'ATO ORDINATÓRIO Ficam as partes intimadas da expedição do(s) precatório(s) '
    'para, querendo, manifestarem-se no prazo de 5 dias. Inexistindo oposição, os '
    'autos serão distribuídos no sistema eproc 2g.'
)
PREPARADA_TEXTO = (
    'ATO ORDINATÓRIO Intimação realizada no sistema eproc. O ato refere-se ao '
    'seguinte evento: Evento 64 - 21/07/2025 - Requisição de pagamento de '
    'precatório preparada para envio'
)
ENVIADA_HTML = (
    'O ato refere-se ao seguinte evento: Evento&nbsp;65 - Requisi&ccedil;&atilde;o '
    'de&nbsp;pagamento de precat&oacute;rio enviada&nbsp;ao tribunal - Precatorio 272595'
)


@pytest.mark.django_db
class TestMinutaNaoEhEnvio:
    @pytest.mark.parametrize('texto', [MINUTA_HTML, MINUTA_TEXTO])
    def test_minuta_casa_f33_e_nao_f31(self, texto):
        assert _minuta(texto)
        assert not _casa(texto)

    @pytest.mark.parametrize('texto', [PREPARADA_TEXTO, ENVIADA_HTML])
    def test_envio_casa_f31_e_nao_f33(self, texto):
        assert _casa(texto)
        assert not _minuta(texto)

    def _processo(self, cnj, textos, classe='12078'):
        from tribunals.models import Movimentacao, Tribunal
        t, _ = Tribunal.objects.get_or_create(
            sigla='TJRS', defaults={'nome': 'TJ RS', 'sigla_djen': 'TJRS'})
        p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo=classe)
        for i, (dia, texto) in enumerate(textos):
            Movimentacao.objects.create(
                processo=p, tribunal=t, external_id=f'{cnj}-{i}', texto=texto,
                data_disponibilizacao=_dt.datetime(*dia, 3, tzinfo=_dt.UTC))
        return p

    def test_minuta_do_pull_de_16_10_vira_pre_precatorio(self):
        """5231192-09.2026.8.21.0001: intimação em 15/10, DJEN em 16/10, sem envio."""
        p = self._processo('5231192-09.2026.8.21.0001', [((2026, 10, 16), MINUTA_HTML)])
        feats = compute_features(p)
        assert feats['F31_exp_precatorio_tjrs'] == 0
        assert feats['F33_minuta_precatorio_tjrs'] == 1
        cat, score, _ = classificar(p, features=feats)
        assert cat == Process.CLASSIF_PRE_PRECATORIO
        assert 0.40 <= score < 0.70

    def test_minuta_seguida_de_envio_vira_precatorio(self):
        """5000214-04.2012.8.21.0040: minuta → preparada → enviada (o lead do lote)."""
        p = self._processo('5000214-04.2012.8.21.0040', [
            ((2025, 6, 26), MINUTA_HTML), ((2025, 7, 21), PREPARADA_TEXTO),
            ((2025, 7, 23), ENVIADA_HTML)])
        feats = compute_features(p)
        assert feats['F31_exp_precatorio_tjrs'] == 1
        assert classificar(p, features=feats)[:2] == (Process.CLASSIF_PRECATORIO, 1.0)

    def test_so_preparada_para_envio_ja_promove(self):
        p = self._processo('5003906-96.2026.8.21.0047', [((2026, 10, 1), PREPARADA_TEXTO)])
        assert classificar(p)[0] == Process.CLASSIF_PRECATORIO


class TestTetoSemEnvio:
    def test_lr_alto_sem_envio_desce_a_pre(self):
        """O LR sozinho também não leva o TJRS a PRECATORIO."""
        from unittest import mock
        feats = {'F1_cumprim': 1, 'F11_precat_text': 1, 'F33_minuta_precatorio_tjrs': 0}
        with mock.patch('tribunals.classificador.predict_score', return_value=0.95), \
             mock.patch('tribunals.classificador._current_weights', return_value={}), \
             mock.patch('tribunals.classificador._categorizar',
                        return_value=Process.CLASSIF_PRECATORIO):
            cat, score, _ = classificar(_proc(), features=feats)
        assert cat == Process.CLASSIF_PRE_PRECATORIO
        assert score < 0.70

    def test_minuta_com_conversao_continua_nao_lead(self):
        cat, score, _ = classificar(_proc(), features={
            'F1_cumprim': 1, 'F33_minuta_precatorio_tjrs': 1, 'F32_convertido_rpv_tjrs': 1})
        assert cat == Process.CLASSIF_NAO_LEAD
        assert score <= 0.10

    @pytest.mark.django_db
    def test_teto_nao_vale_fora_do_tjrs(self):
        from tribunals.classificador import TETO_PRE_SEM_ENVIO_TRIBUNAIS
        assert TETO_PRE_SEM_ENVIO_TRIBUNAIS == {'TJRS'}
        cat, _score, _ = classificar(_proc('TJMG'), features={
            'F1_cumprim': 1, 'F20_exp_juriscope': 1})
        assert cat == Process.CLASSIF_PRECATORIO
