"""TRF3: janela da minuta, envio recente e veto de pagamento (F35-F38).

O TRF3 virava PRECATORIO pelo LR no dia da intimação da minuta ("ficam as
partes intimadas do teor do(s) ofício(s) requisitório(s) expedido(s)", Res. CJF
983/2026) e o JURISCOPE consultava no mesmo dia: 11% dos consultados tinham
requisição ATIVA no TRF. Medição (60 dias de pull, 31.496 processos consultados,
estado da requisição na consulta pública do TRF3 no momento da consulta):

    estado no dia da entrega        n      requisição ativa
    janela (minuta 7-30 d)        1.027    75%
    janela (envio até 15 d)         613    92%
    espera (minuta < 7 d)         5.194    29%
    vencido (minuta > 30 / envio)  5.076     8%
    pago depois da expedição     13.573     0,2%
    sem sinal                     6.013     1%

A regex roda no Postgres (`~*`): os testes de texto vão ao banco. Os textos são
trechos reais do DJEN do TRF3 (outubro/2026), sem nomes.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import connection
from django.utils import timezone

from tribunals.classificador import (
    CANCELAMENTO_TRF3_SQL,
    ENVIO_TRF3_SQL,
    MINUTA_TRF3_SQL,
    PAGO_TRF3_SQL,
    PAGO_VETO_TRF3_SQL,
    SCORE_ESPERA_MINUTA,
    classificar,
    compute_features,
    estado_trf3,
)
from tribunals.models import Process

# 5003422-58.2024.4.03.6329 (JEF Bragança Paulista), 16/10/2026.
MINUTA = (
    'CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA (12078) EXECUTADO: INSTITUTO '
    'NACIONAL DO SEGURO SOCIAL - INSS A T O O R D I N A T Ó R I O Nos termos do artigo 13 '
    'da Resolução Conselho da Justiça Federal (CJF) n. 983/2026, ficam as partes intimadas '
    'do teor do(s) ofício(s) requisitório(s) expedido(s). Normativos observados: 1. '
    'Consoante disposto no artigo 17 da Resolução CJF n. 983/2026, o destacamento de '
    'honorários somente é possível antes da elaboração da requisição de pagamento. Tendo em '
    'vista que o requisitório já foi expedido, não é mais cabível a juntada de contrato de '
    'honorários para destaque nesta fase processual. 2. Em conformidade com o artigo 7º da '
    'Resolução CJF n. 983/2026, a atualização de valores de precatórios e RPV é realizada '
    'automaticamente até o efetivo pagamento. 3. Ressalvada a hipótese do artigo 494, '
    'inciso I, do Código de Processo Civil, a intimação das partes somente para possibilitar '
    'a conferência do preenchimento do(s) ofício(s) requisitório(s) pelos respectivos '
    'interessados. Se nada for requerido no prazo de 5 (cinco) dias, o(s) requisitório(s) '
    'será(ão) transmitido(s) ao tribunal. SÃO PAULO/SP, 15 de outubro de 2026.'
)
# 5004143-97.2024.4.03.6106 (2ª Vara Federal de São José do Rio Preto), 16/10/2026.
ENVIO_TRANSMISSAO = (
    'A T O O R D I N A T Ó R I O Nos termos do artigo 203, § 4.º, do Código de Processo '
    'Civil, encaminho este expediente para ciência às partes da transmissão ao Tribunal '
    'da(s) requisição(ões) de pagamento expedida(s) nos presentes autos. Caso o '
    'demonstrativo de pagamento não esteja anexado aos autos, o(s) beneficiário(s) do '
    'crédito poderá(ão) acessar o link https://web.trf3.jus.br/consultas/Internet/'
    'ConsultaReqPag (consulta pelo número do processo), para obter maiores informações sobre '
    'a(s) requisição(ões) expedida(s). O advogado, querendo, poderá levantar os valores de '
    'acordo com as normas da instituição bancária depositária, mediante a apresentação de '
    'certidão específica (de advogado constituído e procuração autenticada), a ser expedida '
    'pela Secretaria da Vara.'
)
# 5000503-21.2026.4.03.6202 (JEF Ponta Porã), 05/10/2026.
ENVIO_AO_TRIBUNAL = (
    'ATO ORDINATÓRIO Nos termos do artigo 203, § 4.º, do Código de Processo Civil, encaminho '
    'este expediente para ciência às partes da transmissão da(s) requisição(ões) de '
    'pagamento expedida(s) nos presentes autos ao Tribunal. O(s) beneficiário(s) do crédito '
    'poderá(ão) acessar o link https://web.trf3.jus.br/consultas/Internet/ConsultaReqPag '
    '(consulta pelo número do processo), para obter maiores informações sobre inscrição em '
    'proposta da(s) requisição(ões) expedida(s). Em se tratando de requisição de pequeno '
    'valor, o crédito poderá ocorrer em até 60 dias, e caberá à parte beneficiária verificar '
    'a ocorrência do pagamento/liberação do crédito e em qual agência realizado (Banco do '
    'Brasil ou Caixa Econômica Federal). Decorrido o prazo e nada mais sendo requerido, será '
    'proferida sentença de extinção da execução.'
)
# 5002528-59.2022.4.03.6327 (JEF São José dos Campos), 14/10/2026. Registro do
# precatório no TRF: 15 de 16 com requisição ativa quando recente.
ENVIO_REGISTRO = (
    'DESPACHO Nos termos do artigo 203, § 4.º, do Código de Processo Civil, encaminho este '
    'expediente para ciência às partes do registro da requisição de pagamento expedida nos '
    'presentes autos. O beneficiário do crédito poderá acessar o link '
    'https://web.trf3.jus.br/consultas/Internet/ConsultaReqPag para obter maiores '
    'informações sobre a requisição expedida. Após a liberação do pagamento do requisitório '
    '(sucumbencial) e considerando que o precatório expedido será liberado na proposta '
    'orçamentária de 2028, aguarde-se em arquivo provisório (sobrestado) a comunicação do '
    'Egrégio Tribunal Regional Federal da 3ª Região acerca da disponibilização dos valores. '
    'Int. SÃO JOSÉ DOS CAMPOS, na data da assinatura eletrônica.'
)
# 5013538-85.2025.4.03.6201 (JEF Campo Grande), sentença de 16/10/2026.
PAGO_SENTENCA = (
    'SENTENÇA I. Trata-se de cumprimento de sentença em face da Fazenda Pública. Decido. II. '
    'FUNDAMENTO Os valores a título de requisitório de pagamento, proposta 7/2026, foram '
    'pagos pelo Tribunal Regional Federal da 3ª Região e já podem ser sacados pelos '
    'beneficiários em qualquer agência do país, mediante apresentação de documento pessoal '
    'com foto e o extrato de pagamento. III. DISPOSITIVO Considero o cumprimento da obrigação '
    'de fazer e do pagamento pela parte ré, com o depósito judicial dos valores devidos a '
    'título de requisitório de pagamento, DECLARO EXTINTA A EXECUÇÃO, nos termos do art. '
    '924, II, do CPC.'
)
# 5002190-16.2025.4.03.6316 (JEF Andradina), 12/10/2026.
PAGO_CIENTIFICADA = (
    'ATO ORDINATÓRIO Em cumprimento à decisão anterior, expeço o seguinte ato ordinatório: '
    'Fica a parte autora cientificada acerca do pagamento do ofício requisitório expedido '
    'nos autos e para eventuais requerimentos. Salienta-se que, em regra, os saques '
    'correspondentes a precatórios e a RPVs serão feitos independentemente de alvará (art. '
    '49, §1º, da Resolução n. 983/2026-CJF).'
)
# 5005816-08.2023.4.03.6318 (JEF Franca), 08/10/2026.
PAGO_DISPONIBILIZACAO = (
    'ATO ORDINATÓRIO Nos termos da Portaria FRAN-JEF-SEJF nº 159, de 24 de junho de 2026, '
    'expeço o presente ATO ORDINATÓRIO para INTIMAÇÃO das partes sobre a disponibilização da '
    'REQUISIÇÃO DE PAGAMENTO (RPV ou PRC) expedida nos autos, já liberado para pagamento na '
    'instituição bancária oficial (CEF ou BB), sem restrições (extrato de pagamento lançado '
    'no processo).'
)
# Amostra de 16/10/2026 (JEF Araçatuba).
PAGO_DEPOSITADOS = (
    'fica a parte autora intimada de que os valores requisitados em seu favor foram '
    'depositados junto à rede bancária. Os saques correspondentes a precatórios e a RPVs '
    'serão feitos independentemente de alvará.'
)
# Ordem/futuro: NÃO é pagamento nem envio.
# 5003951-62.2023.4.03.6119 (JEF Guarulhos), 16/10/2026.
FUTURO_PAGAMENTO = (
    '8. Com a notícia do pagamento do ofício requisitório, tornem conclusos para extinção '
    'da execução. Guarulhos, data da assinatura eletrônica.'
)
# 5005070-43.2022.4.03.6103 (3ª Vara Federal de São José dos Campos), 09/10/2026.
ORDEM_DE_EXPEDIR = (
    'Expeça(m)-se ofício(s) requisitório(s) com observância aos termos da Resolução CJF '
    '983/2026. Após a confecção da(s) minuta(s) do(s) ofício(s), intimem-se as partes para '
    'manifestação, no prazo de 5 dias. Na ausência de questionamentos, proceda-se à '
    'transmissão do(s) referido(s) ofício(s) ao TRF-3. A disponibilização dos valores '
    'requisitados pode ser acompanhada no sítio eletrônico www.trf3.jus.br, na aba '
    'Requisições de Pagamento. Com o depósito, cientifique-se a parte exequente de que os '
    'saques correspondentes a precatórios e a RPVs serão feitos independentemente de alvará '
    'e reger-se-ão pelas normas aplicáveis aos depósitos bancários. Sem manifestação, '
    'decorridos 15 dias da intimação da disponibilização dos valores, arquive-se o feito.'
)
# Medido e descartado como minuta: 0 de 74 com requisição até 30 dias.
INTEIRO_TEOR = (
    'Intimação das partes para manifestação acerca do inteiro teor do ofício requisitório '
    '(Resolução CJF n. 983/2026). Inocorrentes impugnações, os ofícios expedidos serão '
    'enviados ao E. Tribunal Regional da 3a Região, aguardando-se o pagamento.'
)


# Falsos positivos achados na revisão do PR (textos reais do DJEN do TRF3).
# Sentença da fase de conhecimento: o "valores pagos" é benefício inacumulável.
FP_BENEFICIO = (
    'indicando separadamente os montantes devidos a título de Principal, atualização '
    'monetária, juros de mora, e Selic a partir de 09/12/2021, relativos à obrigação de '
    'pagar, apontando os índices usados para correção monetária, juros e RMI adotada, '
    'compensando-se os valores pagos a título de benefício inacumulável e/ou no curso da '
    'presente ação e observando-se o que segue: a) Correção monetária: Conforme julgado.'
)
# Ato sobre cálculos, antes da requisição: a NEGAÇÃO da intimação da liberação.
FP_NAO_SERA_INTIMADA = (
    'ATO ORDINATÓRIO Intimem-se as partes para, em 10 (dez) dias, manifestarem-se acerca dos '
    'cálculos ou informações prestadas pela Seção de Cálculos Judiciais, dando ciência à parte '
    'autora de que não será intimada da liberação do pagamento, tampouco para dizer se a '
    'sentença foi cumprida, uma vez que pode acompanhar a tramitação do requisitório '
    'diretamente no sítio eletrônico do Tribunal Regional Federal da 3ª Região.'
)
# 5002198-49.2022.4.03.6202, 02/10/2026: paga a RPV dos honorários; o precatório
# do principal segue ativo no TRF.
PAGO_SO_HONORARIOS = (
    'DESPACHO Ciência ao advogado da parte autora da disponibilização da(s) requisição(ões) '
    'referente(s) aos honorários sucumbenciais, cujos dados de pagamento poderão ser '
    'consultados através do seguinte endereço eletrônico: web.trf3.jus.br/consultas/'
    'Internet/ConsultaReqPag Os saques correspondentes ao precatórios/RPVs serão efetuados '
    'em qualquer agência do banco depositário.'
)
# 5000150-30.2021.4.03.6113, 30/09/2025: a RPV do advogado; o PRC segue ativo.
PAGO_SO_PATRONO = (
    'DESPACHO 1. Ciência ao patrono do(a) exequente acerca da disponibilização da quantia '
    'requisitada em seu nome, conforme extrato de pagamento retro, devendo o(s) saque(s) '
    'correspondente(s) ser(em) feito(s) independentemente de alvará.'
)
CANCELA = (
    'considerando a possibilidade de recurso desta decisão, determino à secretaria que '
    'proceda ao cancelamento da minuta de precatório anexada ao id 123, e confeccione novo '
    'ofício precatório relativo ao crédito principal.'
)
AVISO_CANCELAMENTO = (
    'o CPF da parte autora deve estar regular, sob pena de cancelamento do ofício '
    'requisitório. Fica a secretaria autorizada, apontado erro material, a cancelar o ofício '
    'correspondente. Ratifico o requisitório expedido.'
)


def _pago(texto: str) -> bool:
    return _casa(texto, PAGO_TRF3_SQL) and not _casa(texto, PAGO_VETO_TRF3_SQL)


def _casa(texto: str, regex: str) -> bool:
    with connection.cursor() as cur:
        cur.execute('SELECT %s ~* %s', [texto, regex])
        return cur.fetchone()[0]


@pytest.mark.django_db
class TestRegexNoPostgres:
    def test_minuta(self):
        assert _casa(MINUTA, MINUTA_TRF3_SQL)
        # A minuta anuncia a transmissão no futuro ("será(ão) transmitido(s)") e
        # cita o "efetivo pagamento": não é envio nem pagamento.
        assert not _casa(MINUTA, ENVIO_TRF3_SQL)
        assert not _casa(MINUTA, PAGO_TRF3_SQL)

    @pytest.mark.parametrize('texto', [ENVIO_TRANSMISSAO, ENVIO_AO_TRIBUNAL, ENVIO_REGISTRO])
    def test_envio(self, texto):
        assert _casa(texto, ENVIO_TRF3_SQL)
        assert not _casa(texto, MINUTA_TRF3_SQL)
        assert not _casa(texto, PAGO_TRF3_SQL)

    @pytest.mark.parametrize('texto', [
        PAGO_SENTENCA, PAGO_CIENTIFICADA, PAGO_DISPONIBILIZACAO, PAGO_DEPOSITADOS])
    def test_pagamento(self, texto):
        assert _casa(texto, PAGO_TRF3_SQL)
        assert not _casa(texto, MINUTA_TRF3_SQL)
        assert not _casa(texto, ENVIO_TRF3_SQL)

    @pytest.mark.parametrize('texto', [
        PAGO_SENTENCA, PAGO_CIENTIFICADA, PAGO_DISPONIBILIZACAO, PAGO_DEPOSITADOS])
    def test_pagamento_do_autor_nao_e_vetado(self, texto):
        assert _pago(texto)

    @pytest.mark.parametrize('texto', [
        FP_BENEFICIO, FP_NAO_SERA_INTIMADA, PAGO_SO_HONORARIOS, PAGO_SO_PATRONO])
    def test_nao_e_pagamento_do_credito(self, texto):
        assert not _pago(texto)

    def test_cancelamento_so_a_ordem(self):
        assert _casa(CANCELA, CANCELAMENTO_TRF3_SQL)
        assert not _casa(AVISO_CANCELAMENTO, CANCELAMENTO_TRF3_SQL)
        assert not _casa(MINUTA, CANCELAMENTO_TRF3_SQL)

    @pytest.mark.parametrize('texto', [FUTURO_PAGAMENTO, ORDEM_DE_EXPEDIR, INTEIRO_TEOR])
    def test_ordem_e_futuro_nao_casam(self, texto):
        assert not _casa(texto, MINUTA_TRF3_SQL)
        assert not _casa(texto, ENVIO_TRF3_SQL)
        assert not _casa(texto, PAGO_TRF3_SQL)


AGORA = dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)


def _dias(n):
    return AGORA - dt.timedelta(days=n)


class TestEstado:
    @pytest.mark.parametrize('minuta, envio, pago, esperado', [
        (_dias(0), None, None, 'espera'),
        (_dias(6), None, None, 'espera'),
        (_dias(7), None, None, 'janela'),
        (_dias(30), None, None, 'janela'),
        (_dias(31), None, None, 'vencido'),
        (None, _dias(15), None, 'janela'),
        (None, _dias(16), None, 'vencido'),
        # Envio recente vale mesmo com a minuta ainda na espera.
        (_dias(3), _dias(1), None, 'janela'),
        # Minuta nova depois de um envio antigo (requisição complementar): espera.
        (_dias(2), _dias(200), None, 'espera'),
        # Pagamento depois (ou no dia) da última expedição: pago.
        (_dias(40), _dias(30), _dias(5), 'pago'),
        (_dias(10), None, _dias(10), 'pago'),
        # Pagamento ANTERIOR à minuta nova: a requisição nova é crédito novo.
        (_dias(10), None, _dias(90), 'janela'),
        # Pagamento sem expedição no DJEN vale sozinho.
        (None, None, _dias(300), 'pago'),
        (None, None, None, ''),
    ])
    def test_estado(self, minuta, envio, pago, esperado):
        assert estado_trf3(minuta, envio, pago, AGORA) == esperado

    def test_cancelamento_depois_da_expedicao_tira_da_janela(self):
        assert estado_trf3(_dias(10), None, None, AGORA, cancel_dt=_dias(5)) == 'vencido'
        # Cancelamento antigo, minuta nova: a requisição nova vale.
        assert estado_trf3(_dias(10), None, None, AGORA, cancel_dt=_dias(60)) == 'janela'


def _proc(tribunal_id='TRF3', classe_codigo='12078'):
    return Process(tribunal_id=tribunal_id, classe_codigo=classe_codigo,
                   numero_cnj='5003422-58.2024.4.03.6329')


class TestRegra:
    def test_janela_promove_sem_exigir_cumprimento(self):
        """JEF (classe 436) tem a mesma minuta: 70% ativa na janela (n=255)."""
        for classe, f1 in (('12078', 1), ('436', 0)):
            cat, score, _ = classificar(_proc(classe_codigo=classe), features={
                'F1_cumprim': f1, 'F35_minuta_janela_trf3': 1})
            assert (cat, score) == (Process.CLASSIF_PRECATORIO, 1.0)

    def test_espera_fica_em_pre_fora_do_pull(self):
        cat, score, _ = classificar(_proc(), features={
            'F1_cumprim': 1, 'F36_minuta_espera_trf3': 1})
        assert cat == Process.CLASSIF_PRE_PRECATORIO
        assert score == SCORE_ESPERA_MINUTA < 0.20  # VOYAGER_MIN_SCORE_N2 do JURISCOPE

    def test_pago_vira_nao_lead(self):
        from unittest import mock
        with mock.patch('tribunals.classificador.predict_score', return_value=0.95), \
             mock.patch('tribunals.classificador._current_weights', return_value={}), \
             mock.patch('tribunals.classificador._categorizar',
                        return_value=Process.CLASSIF_PRECATORIO):
            cat, score, _ = classificar(_proc(), features={
                'F1_cumprim': 1, 'F11_precat_text': 1, 'F38_pago_trf3': 1})
        assert cat == Process.CLASSIF_NAO_LEAD
        assert score <= 0.10

    @pytest.mark.parametrize('flag', ['F37_exped_vencida_trf3', None])
    def test_lr_sozinho_nao_leva_a_precatorio(self, flag):
        """Minuta vencida ou nenhum sinal: o PRECATORIO do LR desce a PRE."""
        from unittest import mock
        feats = {'F1_cumprim': 1, 'F11_precat_text': 1}
        if flag:
            feats[flag] = 1
        with mock.patch('tribunals.classificador.predict_score', return_value=0.95), \
             mock.patch('tribunals.classificador._current_weights', return_value={}), \
             mock.patch('tribunals.classificador._categorizar',
                        return_value=Process.CLASSIF_PRECATORIO):
            cat, score, _ = classificar(_proc(), features=feats)
        assert cat == Process.CLASSIF_PRE_PRECATORIO
        assert 0.20 <= score < 0.70

    @pytest.mark.django_db
    @pytest.mark.parametrize('tribunal', ['TRF1', 'TJMG', 'TJRS', 'TJSP'])
    def test_flags_do_trf3_nao_valem_fora_dele(self, tribunal):
        from unittest import mock
        with mock.patch('tribunals.classificador.predict_score', return_value=0.95), \
             mock.patch('tribunals.classificador._current_weights', return_value={}), \
             mock.patch('tribunals.classificador._categorizar',
                        return_value=Process.CLASSIF_DIREITO_CREDITORIO):
            cat, _score, _ = classificar(_proc(tribunal), features={
                'F1_cumprim': 0, 'F35_minuta_janela_trf3': 1, 'F38_pago_trf3': 1})
        assert cat == Process.CLASSIF_DIREITO_CREDITORIO


@pytest.mark.django_db
class TestNoBanco:
    """Movimentações reais → features → categoria, de ponta a ponta no Postgres."""

    def _processo(self, cnj, textos, tribunal='TRF3', classe='12078'):
        from tribunals.models import Movimentacao, Tribunal
        t, _ = Tribunal.objects.get_or_create(
            sigla=tribunal, defaults={'nome': tribunal, 'sigla_djen': tribunal})
        p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo=classe)
        agora = timezone.now()
        for i, (dias, texto) in enumerate(textos):
            Movimentacao.objects.create(
                processo=p, tribunal=t, external_id=f'{cnj}-{i}', texto=texto,
                data_disponibilizacao=agora - dt.timedelta(days=dias))
        return p

    def test_minuta_de_ontem_espera(self):
        p = self._processo('5003422-58.2024.4.03.6329', [(1, MINUTA)])
        feats = compute_features(p)
        assert feats['F36_minuta_espera_trf3'] == 1
        assert classificar(p, features=feats)[:2] == (
            Process.CLASSIF_PRE_PRECATORIO, SCORE_ESPERA_MINUTA)

    def test_minuta_de_8_dias_promove(self):
        p = self._processo('5003422-58.2024.4.03.6329', [(8, MINUTA)])
        feats = compute_features(p)
        assert feats['F35_minuta_janela_trf3'] == 1
        assert classificar(p, features=feats)[:2] == (Process.CLASSIF_PRECATORIO, 1.0)

    def test_envio_de_ontem_promove_com_a_minuta_na_espera(self):
        p = self._processo('5000503-21.2026.4.03.6202', [(4, MINUTA), (1, ENVIO_AO_TRIBUNAL)])
        assert classificar(p)[0] == Process.CLASSIF_PRECATORIO

    def test_pago_depois_da_minuta_vira_nao_lead(self):
        p = self._processo('5013538-85.2025.4.03.6201', [
            (70, MINUTA), (60, ENVIO_TRANSMISSAO), (1, PAGO_SENTENCA)])
        feats = compute_features(p)
        assert feats['F38_pago_trf3'] == 1
        assert classificar(p, features=feats)[0] == Process.CLASSIF_NAO_LEAD

    def test_minuta_vencida_nao_e_precatorio(self):
        p = self._processo('5002332-20.2025.4.03.6316', [(45, MINUTA)])
        feats = compute_features(p)
        assert feats['F37_exped_vencida_trf3'] == 1
        assert classificar(p, features=feats)[0] != Process.CLASSIF_PRECATORIO

    def test_pago_so_honorarios_nao_veta_o_principal(self):
        p = self._processo('5002198-49.2022.4.03.6202', [
            (40, MINUTA), (12, ENVIO_REGISTRO), (5, PAGO_SO_HONORARIOS)])
        feats = compute_features(p)
        assert feats['F38_pago_trf3'] == 0
        assert feats['F35_minuta_janela_trf3'] == 1
        assert classificar(p, features=feats)[0] == Process.CLASSIF_PRECATORIO

    def test_mesmos_textos_no_trf1_nao_mexem(self):
        p = self._processo('1003422-58.2024.4.01.3400', [(8, MINUTA)], tribunal='TRF1')
        feats = compute_features(p)
        assert feats['F35_minuta_janela_trf3'] == 0
        assert feats['F36_minuta_espera_trf3'] == 0


@pytest.mark.django_db
def test_revisao_diaria_pega_espera_e_janela():
    from unittest import mock

    from tribunals import jobs
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    velho = timezone.now() - dt.timedelta(days=1)
    espera = Process.objects.create(
        tribunal=t, numero_cnj='5003422-58.2024.4.03.6329', classificacao='PRE_PRECATORIO',
        classificacao_score=SCORE_ESPERA_MINUTA, classificacao_em=velho)
    janela = Process.objects.create(
        tribunal=t, numero_cnj='5000503-21.2026.4.03.6202', classificacao='PRECATORIO',
        classificacao_score=1.0, classificacao_em=velho)
    Process.objects.create(  # PRE do LR: não vence pelo relógio
        tribunal=t, numero_cnj='5013538-85.2025.4.03.6201', classificacao='PRE_PRECATORIO',
        classificacao_score=0.55, classificacao_em=velho)
    recente = Process.objects.create(  # reclassificado de madrugada: entra também
        tribunal=t, numero_cnj='5002190-16.2025.4.03.6316', classificacao='PRECATORIO',
        classificacao_score=1.0, classificacao_em=timezone.now())
    with mock.patch.object(jobs.reclassificar_batch, 'delay') as delay:
        r = jobs.revisar_janela_trf3()
    assert r == {'enfileirados': 3}
    assert sorted(delay.call_args.args[0]) == sorted([espera.pk, janela.pk, recente.pk])


@pytest.mark.django_db
def test_dry_run_do_backfill_mostra_o_destino():
    import io

    from django.core.management import call_command

    from tribunals.models import Movimentacao, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    agora = timezone.now()
    casos = [  # (cnj, classificação atual, [(dias, texto)])
        ('5003422-58.2024.4.03.6329', 'PRECATORIO', [(1, MINUTA)]),
        ('5000503-21.2026.4.03.6202', 'PRE_PRECATORIO', [(10, MINUTA)]),
        ('5013538-85.2025.4.03.6201', 'PRECATORIO', [(70, MINUTA), (2, PAGO_SENTENCA)]),
        ('5002190-16.2025.4.03.6316', 'PRECATORIO', [(400, ORDEM_DE_EXPEDIR)]),
    ]
    for cnj, classif, textos in casos:
        p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo='12078',
                                   classificacao=classif)
        for i, (dias, texto) in enumerate(textos):
            Movimentacao.objects.create(
                processo=p, tribunal=t, external_id=f'{cnj}-{i}', texto=texto,
                data_disponibilizacao=agora - dt.timedelta(days=dias))
    out = io.StringIO()
    call_command('reclassificar_trf3_janela', '--dry-run', stdout=out)
    s = out.getvalue()
    assert 'PRECATORIO=3 | minuta/envio em 45 dias=2 | alvo=4' in s
    assert 'PRECATORIO           espera     -> PRE (espera)' in s
    assert 'PRE_PRECATORIO       janela     -> PRECATORIO' in s
    assert 'PRECATORIO           pago       -> NAO_LEAD' in s
    assert 'PRECATORIO           sem_sinal  -> PRE (teto)' in s
