"""TJMT: execução extinta pela satisfação (F39) e RPV federal parada (F40).

O PRECATORIO do TJMT era quase todo RPV já paga — a juntada do alvará não sai
no DJEN. Verdade usada (19/10/2026): amostra aleatória de 50 dos 841
PRECATORIO consumidos pela homologação, conferida na consulta pública do TJMT
(eventos do PJe e o PDF do alvará): 48 pagos, 1 só com alvará de honorários
(o resto do credor depositado), 1 não é lead, 0 lead vivo. Com os vetos, 46 dos
50 saem de PRECATORIO (36 pelo F39, 10 pelo F40), sem nenhum lead vivo.

A regex roda no Postgres (`~*`): os testes de texto vão ao banco. Os textos são
trechos reais do DJEN (TJMT, e TJRS/TJMG nos vetos), sem nomes.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from django.db import connection
from django.utils import timezone

from tribunals.classificador import (
    EXTINCAO_SATISFEITA_SQL,
    EXTINCAO_SATISFEITA_VETO_SQL,
    INSS_PASSIVO_SQL,
    RPV_FEDERAL_PARADA_DIAS,
    SATISFACAO_SINAL_TRIBUNAIS,
    SCORE_REBAIXAMENTO_SINAL,
    classificar,
    compute_features,
    f39_extinto_satisfeito,
    f40_rpv_federal_parada,
)
from tribunals.models import Process

# --- Extinção pela satisfação (casam o F39) ---------------------------------
# 1000418-65.2023.8.11.0087 (Guarantã do Norte), 14/11/2023.
EXTINGO_PELO_PAGAMENTO = (
    'SENTENÇA Processo: 1000418-65.2023.8.11.0087. EXEQUENTE: FULANA DE TAL '
    'EXECUTADO: INSTITUTO NACIONAL DO SEGURO SOCIAL Vistos etc. Diante da comprovação de '
    'pagamento da condenação, determino a expedição de alvará de levantamento dos créditos. '
    'Desta forma, haja vista o adimplemento da obrigação, extingo a execução pelo '
    'pagamento, na forma do art. 924, II, do CPC. Sem custas ou honorários advocatícios.'
)
# 1001709-40.2020.8.11.0044 (Paranatinga), 04/12/2023.
RPV_ADIMPLIDAS = (
    'Verifica-se que foram adimplidos os valores relativos às Requisições de Pequeno Valor '
    'anteriormente expedidas. Dessa forma, ante o integral cumprimento da obrigação pelo '
    'executado, julgo extinto o presente feito, consoante regra ínsita ao artigo 924, '
    'inciso II, do novo Código Processual Civil. Expeça-se alvará de levantamento em favor '
    'da parte exequente para o levantamento das importâncias depositadas.'
)
# 1005177-38.2022.8.11.0045 (Lucas do Rio Verde), 14/04/2025 — a causa vem ANTES.
CAUSA_ANTES = (
    'Realizado depósito dos valores com expedição de alvará de quitação (id 186222393). '
    'Vieram conclusos. Por todo exposto, satisfeita a obrigação, por força do art. 925 e '
    '924, II, ambos do Código de Processo Civil, e, sem mais atos processuais pendentes, '
    'EXTINGUE-SE o processo. Ausente interesse recursal, certifique-se o trânsito em '
    'julgado. Sem pendências, ARQUIVE-SE.'
)
# 1004580-68.2022.8.11.0013 (Juizado da Fazenda), 28/05/2024 — cita a lei e decide.
CITA_A_LEI_E_DECIDE = (
    '2. Fundamentação. Considerando o pagamento, com anuência tácita do credor, o processo '
    'de execução cumpriu o seu objetivo referente ao título judicial, ensejando a sua '
    'extinção, conforme art. 924 CPC. “Art. 924. Extingue-se a execução quando: I - a '
    'petição inicial for indeferida; II - a obrigação for satisfeita; (...)” Grifei. 3. '
    'Dispositivo. Ante ao exposto, JULGO EXTINTO o processo com resolução de mérito, com '
    'fulcro no art. 924, II, do Código de Processo Civil.'
)
# 1003357-62.2023.8.11.0040 (Sorriso), 16/08/2024.
DECLARO_SATISFEITA = (
    'Trata-se de cumprimento de sentença com o pagamento dos valores devidos. A parte '
    'exequente concordou com os depósitos efetuados. Alvarás expedidos. Vieram conclusos. '
    'É O RELATÓRIO. FUNDAMENTO E DECIDO. A exequente concordou com os valores depositados, '
    'razão pela qual, nos termos do artigo 924 II do CPC, DECLARO SATISFEITA A OBRIGAÇÃO, '
    'extinguindo o feito com resolução do mérito.'
)
# 1000984-54.2022.8.11.0085 (Terra Nova do Norte), 14/08/2024.
FASE_EXECUTIVA = (
    'Réu: 29.979.036.0001-40 - INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS e outros '
    'Considerando que a obrigação foi integralmente satisfeita pela parte devedora, '
    'DECLARO EXTINTA POR SENTENÇA A FASE EXECUTIVA, nos termos dos artigos 924, II, e 925, '
    'ambos do Código de Processo Civil.'
)

# --- Não é a quitação (não casam, ou casam o veto) ---------------------------
# 1043011-76.2023.8.11.0001, 11/03/2024 — aviso, sem dispositivo.
AVISO_SOB_PENA = (
    '2. Após, intime-se o exequente para, no prazo de 5 (cinco) dias, manifestar-se '
    'objetivamente se o depósito realizado satisfaz a obrigação do executado ou se ainda há '
    'interesse da exequente no prosseguimento da ação, sob pena de extinção processual em '
    'razão do cumprimento da obrigação (CPC, art. 924, inciso II).'
)
SO_A_LEI = (
    'É o que prevê o artigo 924, II do CPC: Art. 924. Extingue-se a execução quando: (...) '
    'II - a obrigação for satisfeita;'
)
# 1003968-51.2022.8.11.0007, 12/12/2023 — embargos, não extinção.
EMBARGOS_PROCEDENTES = (
    'Isto posto, conheço dos embargos interpostos pelo executado nestes autos e no mérito '
    'julgo-os procedentes, o que faço com fundamento no artigo 924, inciso II, do Código de '
    'Processo Civil. Por conseguinte, HOMOLOGO o cálculo apresentado pelo Executado.'
)
# 1005271-82.2022.8.11.0013, 31/05/2023 — acordo na fase de conhecimento; o
# precatório ainda vai ser expedido.
ACORDO_487 = (
    'HOMOLOGO POR SENTENÇA, para que possa produzir seus jurídicos e legais efeitos, a '
    'transação celebrada entre as partes e, via de consequência, julgo extinto o processo, '
    'com resolução de mérito, com fundamento no art. 487, III, b, do Código de Processo '
    'Civil. Expeça-se precatório, remetendo-se ao Eg. TJMT para pagamento.'
)
# 1000215-96.2022.8.11.0036, 17/05/2023 — sentença de mérito ("inadimplemento",
# "pagamento dos valores" da condenação).
MERITO_487_I = (
    'EXTINGUE-SE o processo com resolução do mérito, nos termos do art. 487, I e 490, do '
    'CPC. 2. CONDENO a autarquia requerida no pagamento dos valores das parcelas vencidas e '
    'não pagas administrativamente, com correção desde o inadimplemento de cada parcela.'
)
# 1000145-25.2019.8.11.0088, 27/05/2025 — extinção com o PRECATÓRIO por pagar.
PRECATORIO_PENDENTE = (
    'Considerando o pagamento das requisições e/ou precatórios, abrangendo as verbas '
    'requisitadas, DECRETO A EXTINÇÃO da fase de cumprimento de sentença, na forma do '
    'artigo 924, II, do CPC. Em relação ao precatório, certifique a Secretaria acerca da '
    'sua expedição e, tendo havido, aguarde-se o pagamento, mantendo os autos arquivados.'
)
# TJMG 5000006-65.2022.8.13.0016, 15/12/2025 — extinção na EXPEDIÇÃO do precatório.
TJMG_PRECATORIO_EXPEDIDO = (
    'Conforme ID 10586204637, a parte executada já se encontra devidamente intimada para '
    'efetivar o pagamento do ofício precatório expedido. Assim, considerando o documento '
    'de ID 10579337857 e a intimação para pagamento, JULGO EXTINTA A EXECUÇÃO, com '
    'fundamento no art. 924, inciso II, do Código de Processo Civil.'
)
# TJRS 5004680-91.2025.8.21.0070, 24/06/2025 — despacho inicial, extinção antecipada.
TJRS_DESDE_LOGO = (
    'III- Feita a requisição, aguarde-se em Cartório o adimplemento. IV- Comprovado o '
    'pagamento, expeça-se alvará automatizado em favor do(s) credor(es), acrescido dos '
    'rendimentos legais. V- Adimplido o débito, desde logo, declaro extinta a fase de '
    'cumprimento de sentença, com fundamento no artigo 924, inc. II, do CPC. Baixe-se.'
)
# TJRS 5001255-88.2025.8.21.0124, 13/06/2025.
TJRS_NADA_MAIS = (
    'Em caso de não apresentação de impugnação, expeça-se RPV. Expedido o requisitório e '
    'comprovado o pagamento, expeça-se o competente alvará para levantamento de valores, e '
    'na sequência, nada mais sendo requerido, julgo extinto o feito, nos termos do art. '
    '924, II, do CPC.'
)
# TJRS 5225108-26.2025.8.21.0001, 09/06/2026.
TJRS_NO_SILENCIO = (
    'INTIME-SE a parte exequente para manifestação em 5 dias sobre a satisfação do seu '
    'crédito, advertindo-a de que, no silêncio, o feito será julgado extinto pelo pagamento.'
)
# 1003298-62.2023.8.11.0044 (Paranatinga), 17/09/2025 — antecipada.
TJMT_INEXISTINDO_PENDENCIAS = (
    '3. Após a expedição, aguarde-se o requisitório em arquivo, até o seu efetivo '
    'pagamento; 4. Concretizado o levantamento dos valores pela parte exequente, desde já '
    'determino a expedição de alvará em favor da beneficiária; 5. Cumpridas as '
    'determinações e inexistindo outras pendências, JULGO EXTINTO O CUMPRIMENTO DE '
    'SENTENÇA, com resolução do mérito, nos termos do art. 924, II, do CPC.'
)
# TJRS 5074943-40.2020.8.21.0001, 05/06/2025 — extinção de UM litisconsorte.
TJRS_PARCIAL = (
    '1 . Julgo extinta a execução pelo pagamento, forte no art. 924, II, do CPC/2015 em '
    'relação à FULANA DE TAL . Preclusa esta decisão, ao Cartório para excluir FULANA DE TAL '
    'do polo ativo. 2. Diante da concordância da parte exequente, expeça-se precatório '
    'eletrônico.'
)
# Alvará só dos honorários (1006071-94.2023.8.11.0007): o F39 não olha alvará.
ALVARA_HONORARIOS = (
    'Nos termos da Legislação vigente e artigo 203 § 4º do CPC, impulsiono o presente '
    'feito com a finalidade de abrir vistas ao(a) Procurador(a) do(a) requerente para '
    'ciência acerca do(s) Alvará(s) Judicial(is) expedido(s) nos autos.'
)
EXPEDICAO_RPV = 'Determinada expedição de RPV. RPV expedida em favor da parte exequente.'

# --- INSS no polo passivo (F40) ----------------------------------------------
INSS_EXECUTADO = (
    'Processo: 1000418-65.2023.8.11.0087. EXEQUENTE: FULANA DE TAL EXECUTADO: INSTITUTO '
    'NACIONAL DO SEGURO SOCIAL Vistos etc.'
)
INSS_REQUERIDO_SIGLA = 'REQUERENTE: FULANA DE TAL REQUERIDO: INSS - INSTITUTO NACIONAL DO SEGURO'
# 1005271-82.2022.8.11.0013: precatório do ESTADO que cita o INSS no texto.
ESTADO_CITA_INSS = (
    'lotada na Escola Estadual em Pontes Lacerda-MT, com carga horária em regra de 30 '
    '(trinta) horas semanais, sob regime Geral de Previdência Social – INSS, sendo que no '  # noqa: RUF001 (texto real)
    'período teve renovado diversos contratos de trabalho.'
)


def _regex(texto: str, regex: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, regex])
        return cur.fetchone()[0]


def _f39(texto: str) -> bool:
    return (_regex(texto, EXTINCAO_SATISFEITA_SQL)
            and not _regex(texto, EXTINCAO_SATISFEITA_VETO_SQL))


def _proc(tribunal_id='TJMT', classe_codigo='12078'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='1000418-65.2023.8.11.0087')


@pytest.mark.django_db
class TestRegexF39:
    @pytest.mark.parametrize('texto', [
        EXTINGO_PELO_PAGAMENTO, RPV_ADIMPLIDAS, CAUSA_ANTES, CITA_A_LEI_E_DECIDE,
        DECLARO_SATISFEITA, FASE_EXECUTIVA,
    ])
    def test_extincao_pela_satisfacao_casa(self, texto):
        assert _f39(texto)

    @pytest.mark.parametrize('texto', [
        AVISO_SOB_PENA, SO_A_LEI, EMBARGOS_PROCEDENTES, ACORDO_487, MERITO_487_I,
        ALVARA_HONORARIOS, EXPEDICAO_RPV,
    ])
    def test_nao_e_extincao_pela_satisfacao(self, texto):
        assert not _regex(texto, EXTINCAO_SATISFEITA_SQL)

    @pytest.mark.parametrize('texto', [
        PRECATORIO_PENDENTE, TJMG_PRECATORIO_EXPEDIDO, TJRS_DESDE_LOGO, TJRS_NADA_MAIS,
        TJMT_INEXISTINDO_PENDENCIAS, TJRS_PARCIAL,
    ])
    def test_veto_no_mesmo_texto(self, texto):
        """Casa o dispositivo, mas o crédito não está quitado: o veto tira."""
        assert _regex(texto, EXTINCAO_SATISFEITA_SQL)
        assert _regex(texto, EXTINCAO_SATISFEITA_VETO_SQL)
        assert not _f39(texto)

    def test_aviso_no_silencio_nao_e_dispositivo(self):
        assert not _f39(TJRS_NO_SILENCIO)


@pytest.mark.django_db
class TestRegexInss:
    @pytest.mark.parametrize('texto', [INSS_EXECUTADO, INSS_REQUERIDO_SIGLA])
    def test_inss_no_polo_passivo(self, texto):
        assert _regex(texto, INSS_PASSIVO_SQL)

    def test_mencao_ao_inss_nao_basta(self):
        assert not _regex(ESTADO_CITA_INSS, INSS_PASSIVO_SQL)


class TestFuncoes:
    agora = timezone.now()

    def test_f39_depois_da_expedicao(self):
        d = self.agora
        assert f39_extinto_satisfeito(d, None) == 1
        assert f39_extinto_satisfeito(d, d - dt.timedelta(days=30)) == 1
        assert f39_extinto_satisfeito(d, d) == 1
        # Requisição nova depois da extinção: crédito novo.
        assert f39_extinto_satisfeito(d - dt.timedelta(days=30), d) == 0
        assert f39_extinto_satisfeito(None, d) == 0

    def test_f40_conta_o_silencio_do_djen(self):
        velho = self.agora - dt.timedelta(days=RPV_FEDERAL_PARADA_DIAS + 1)
        recente = self.agora - dt.timedelta(days=RPV_FEDERAL_PARADA_DIAS - 1)
        assert f40_rpv_federal_parada(True, velho, self.agora) == 1
        assert f40_rpv_federal_parada(True, recente, self.agora) == 0
        assert f40_rpv_federal_parada(False, velho, self.agora) == 0
        assert f40_rpv_federal_parada(True, None, self.agora) == 0


def _classificar_com_lr(proc, features, cat_lr):
    with mock.patch('tribunals.classificador.predict_score', return_value=0.95), \
         mock.patch('tribunals.classificador._current_weights', return_value={}), \
         mock.patch('tribunals.classificador._categorizar', return_value=cat_lr):
        return classificar(proc, features=features)


class TestRegra:
    @pytest.mark.parametrize('cat_lr', [
        Process.CLASSIF_PRECATORIO, Process.CLASSIF_PRE_PRECATORIO,
        Process.CLASSIF_DIREITO_CREDITORIO])
    def test_f39_rebaixa_qualquer_lead(self, cat_lr):
        cat, score, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1, 'F39_extinto_satisfeito_ANTI': 1}, cat_lr)
        assert cat == Process.CLASSIF_NAO_LEAD
        assert score <= SCORE_REBAIXAMENTO_SINAL

    def test_f40_rebaixa_precatorio(self):
        cat, score, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1, 'F40_rpv_federal_parada_ANTI': 1},
            Process.CLASSIF_PRECATORIO)
        assert cat == Process.CLASSIF_NAO_LEAD
        assert score <= SCORE_REBAIXAMENTO_SINAL

    def test_f40_nao_mexe_no_pre(self):
        """O PRE parado não foi conferido: o F40 só vale na RPV expedida."""
        cat, _, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1, 'F40_rpv_federal_parada_ANTI': 1},
            Process.CLASSIF_PRE_PRECATORIO)
        assert cat == Process.CLASSIF_PRE_PRECATORIO

    def test_sem_os_vetos_fica(self):
        cat, _, _ = _classificar_com_lr(
            _proc(), {'F1_cumprim': 1}, Process.CLASSIF_PRECATORIO)
        assert cat == Process.CLASSIF_PRECATORIO

    @pytest.mark.parametrize('tribunal', ['TJMG', 'TJAL', 'TRF1'])
    def test_fora_do_rollout_nao_mexe(self, tribunal):
        assert tribunal not in SATISFACAO_SINAL_TRIBUNAIS
        cat, _, _ = _classificar_com_lr(
            _proc(tribunal), {'F1_cumprim': 1, 'F39_extinto_satisfeito_ANTI': 1,
                              'F40_rpv_federal_parada_ANTI': 1},
            Process.CLASSIF_PRECATORIO)
        assert cat == Process.CLASSIF_PRECATORIO


@pytest.mark.django_db
class TestNoBanco:
    """Movimentações reais → features → categoria, de ponta a ponta no Postgres."""

    def _processo(self, cnj, textos, tribunal='TJMT', classe='12078', passivo=None):
        from tribunals.models import Movimentacao, Parte, ProcessoParte, Tribunal
        t, _ = Tribunal.objects.get_or_create(
            sigla=tribunal, defaults={'nome': tribunal, 'sigla_djen': tribunal})
        p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo=classe)
        agora = timezone.now()
        for i, (dias, texto) in enumerate(textos):
            Movimentacao.objects.create(
                processo=p, tribunal=t, external_id=f'{cnj}-{i}', texto=texto,
                data_disponibilizacao=agora - dt.timedelta(days=dias))
        if passivo:
            parte = Parte.objects.create(nome=passivo)
            ProcessoParte.objects.create(processo=p, parte=parte, polo='passivo')
        return p

    def test_extinto_pela_satisfacao_vira_nao_lead(self):
        p = self._processo('1000418-65.2023.8.11.0087', [
            (400, EXPEDICAO_RPV), (300, EXTINGO_PELO_PAGAMENTO)])
        feats = compute_features(p)
        assert feats['F39_extinto_satisfeito_ANTI'] == 1
        assert classificar(p, features=feats)[0] == Process.CLASSIF_NAO_LEAD

    def test_expedicao_depois_da_extincao_e_credito_novo(self):
        p = self._processo('1005419-07.2025.8.11.0040', [
            (200, EXTINGO_PELO_PAGAMENTO), (100, EXPEDICAO_RPV)])
        assert compute_features(p)['F39_extinto_satisfeito_ANTI'] == 0

    def test_precatorio_pendente_nao_veta(self):
        p = self._processo('1000145-25.2019.8.11.0088', [(500, PRECATORIO_PENDENTE)])
        assert compute_features(p)['F39_extinto_satisfeito_ANTI'] == 0

    def test_mesmo_texto_no_tjmg_nao_conta(self):
        p = self._processo('5011469-11.2023.8.13.0261', [(10, EXTINGO_PELO_PAGAMENTO)],
                           tribunal='TJMG')
        assert compute_features(p)['F39_extinto_satisfeito_ANTI'] == 0

    def test_rpv_do_inss_parada_pelo_cabecalho(self):
        p = self._processo('1001052-26.2022.8.11.0013', [
            (RPV_FEDERAL_PARADA_DIAS + 30, INSS_EXECUTADO)])
        assert compute_features(p)['F40_rpv_federal_parada_ANTI'] == 1

    def test_rpv_do_inss_parada_pelo_cadastro_de_partes(self):
        p = self._processo('1002294-24.2022.8.11.0044', [
            (RPV_FEDERAL_PARADA_DIAS + 30, EXPEDICAO_RPV)],
            passivo='INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS')
        assert compute_features(p)['F40_rpv_federal_parada_ANTI'] == 1

    def test_inss_recente_nao_veta(self):
        """Precatório federal: orçamento do ano seguinte. 1001868-86.2023.8.11.0008
        ficou 430 dias em silêncio sem o credor receber."""
        p = self._processo('1001868-86.2023.8.11.0008', [(430, INSS_EXECUTADO)])
        assert compute_features(p)['F40_rpv_federal_parada_ANTI'] == 0

    def test_precatorio_do_estado_parado_fica(self):
        """No MT o precatório do Estado e dos municípios espera ANOS no arquivo
        do 1º grau — é o lead vivo do TJMT."""
        p = self._processo('1005271-82.2022.8.11.0013', [(900, ESTADO_CITA_INSS)],
                           passivo='ESTADO DE MATO GROSSO')
        assert compute_features(p)['F40_rpv_federal_parada_ANTI'] == 0


@pytest.mark.django_db
def test_revisao_diaria_pega_o_precatorio_parado():
    from tribunals import jobs
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMT', defaults={'nome': 'TJMT', 'sigla_djen': 'TJMT'})
    velho = timezone.now() - dt.timedelta(days=RPV_FEDERAL_PARADA_DIAS + 1)
    parado = Process.objects.create(
        tribunal=t, numero_cnj='1001052-26.2022.8.11.0013', classificacao='PRECATORIO',
        ultima_movimentacao_em=velho)
    Process.objects.create(  # recente: o relógio ainda não venceu
        tribunal=t, numero_cnj='1001868-86.2023.8.11.0008', classificacao='PRECATORIO',
        ultima_movimentacao_em=timezone.now() - dt.timedelta(days=30))
    Process.objects.create(  # PRE: fora do F40
        tribunal=t, numero_cnj='1000006-17.2023.8.11.0029', classificacao='PRE_PRECATORIO',
        ultima_movimentacao_em=velho)
    with mock.patch.object(jobs.reclassificar_batch, 'delay') as delay:
        r = jobs.revisar_rpv_federal_parada()
    assert r == {'enfileirados': 1}
    assert delay.call_args.args[0] == [parado.pk]


@pytest.mark.django_db
def test_dry_run_do_backfill_mostra_o_destino():
    import io

    from django.core.management import call_command

    from tribunals.models import Movimentacao, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMT', defaults={'nome': 'TJMT', 'sigla_djen': 'TJMT'})
    agora = timezone.now()
    casos = [  # (cnj, classificação atual, [(dias, texto)])
        ('1000418-65.2023.8.11.0087', 'PRECATORIO', [(300, EXTINGO_PELO_PAGAMENTO)]),
        ('1001052-26.2022.8.11.0013', 'PRECATORIO', [(RPV_FEDERAL_PARADA_DIAS + 30,
                                                       INSS_EXECUTADO)]),
        ('1019362-11.2021.8.11.0015', 'PRECATORIO', [(400, ACORDO_487)]),
        ('1003611-35.2023.8.11.0040', 'PRE_PRECATORIO', [(10, CITA_A_LEI_E_DECIDE)]),
    ]
    for cnj, classif, textos in casos:
        p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo='12078',
                                   classificacao=classif)
        for i, (dias, texto) in enumerate(textos):
            Movimentacao.objects.create(
                processo=p, tribunal=t, external_id=f'{cnj}-{i}', texto=texto,
                data_disponibilizacao=agora - dt.timedelta(days=dias))
    out = io.StringIO()
    call_command('reclassificar_tjmt_pago', '--dry-run', '--incluir-pre', stdout=out)
    s = out.getvalue()
    assert 'alvo=4' in s
    assert 'TJMT   PRECATORIO           F39  -> NAO_LEAD                   1' in s
    assert 'TJMT   PRECATORIO           F40  -> NAO_LEAD                   1' in s
    assert 'TJMT   PRECATORIO           -    -> PRECATORIO (LR)            1' in s
    assert 'TJMT   PRE_PRECATORIO       F39  -> NAO_LEAD                   1' in s
