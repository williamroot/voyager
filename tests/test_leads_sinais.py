"""`POST /api/v1/leads/sinais/` — sinais de expedição em lote para o vigia do JURISCOPE.

O contrato que importa: a data devolvida é a da última movimentação que casa
o MESMO sinal que o classificador usa naquele tribunal, e os vetos são as
mesmas features. Por isso o teste central (`TestParidadeComClassificador`)
roda `compute_features` sobre o mesmo processo e compara.

Textos: movimentações REAIS de produção (16/10/2026), nomes fora. Os de F2/F7
são movimentos do Datajud em que o texto é o próprio nome do movimento.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client

from tribunals import sinais_expedicao as sinais
from tribunals.classificador import compute_features
from tribunals.models import ApiClient, ClassificacaoLog, Movimentacao, Process, Tribunal

URL = '/api/v1/leads/sinais/'

# TJAL 0000015-53.2011.8.02.0033, mov 699587741 (Datajud).
TXT_F2 = 'Expedição de precatório/rpv'
# TJPA 0000352-51.2012.8.14.0040, mov 927447221 (Datajud).
TXT_F7 = 'Preparada para Envio'
# TJSP 0000023-70.2021.8.26.0337, mov 2344055704.
TXT_F20 = (
    'Cumprimento de sentença - Aposentadoria por Invalidez - Tratando-se o '
    'beneficio em questão de natureza previdenciária, a expedição do ofício '
    'requisitório se dá diretamente através do sistema Precweb. Providencie-se a '
    'consulta de pagamento do ofício requisitório expedido a fls. 115/116 (ofício '
    'requisitório nº 2022025033), já que a pesquisa realizada a fls.'
)
# TJRS: evento de ENVIO republicado no DJEN (o F31 desde 16/10/2026).
TXT_F31 = (
    '<p>ATO ORDINAT&Oacute;RIO</p><p>O ato refere-se ao seguinte evento: Evento 65 - '
    'Requisi&ccedil;&atilde;o de pagamento de precat&oacute;rio enviada ao tribunal '
    '- Precatorio 272595</p>'
)
# TJRS 5001727-66.2025.8.21.0067 (o caso do plano), mov 2380768703 — HTML do DJEN.
# É a MINUTA (F33): não é sinal de expedição, mas conta como "última expedição"
# para o veto F32.
TXT_MINUTA = (
    '<p>ATO ORDINAT&Oacute;RIO</p><p>Ficam as partes intimadas da expedi&ccedil;'
    '&atilde;o do(s) precat&oacute;rio(s) para, querendo, manifestarem-se no prazo '
    'de 5 dias. Inexistindo oposi&ccedil;&atilde;o, os autos ser&atilde;o '
    'distribu&iacute;dos no sistema eproc 2g.</p>'
)
# TJRS 5232029-64.2026.8.21.0001 — o caso do QA (ver test_classificador_sinal_tjrs).
TXT_F32 = (
    '2&nbsp; Porque direito dispon&iacute;vel,&nbsp; homologo&nbsp;a&nbsp;'
    'ren&uacute;ncia&nbsp;ao cr&eacute;dito no que exceda ao&nbsp;limite de dez '
    'sal&aacute;rios m&iacute;nimos nacionais 3 Cancele-se&nbsp;o precat&oacute;rio '
    'expedido &nbsp;-&nbsp; evento 18, PRECAT&Oacute;RIO1'
)
# TJMT 1002513-09.2022.8.11.0021, mov 1089773436.
TXT_F19 = (
    'expeça-se alvará de levantamento em favor da parte exequente, conforme dados '
    'bancários indicados, comunicando-se o Tribunal para eventual cancelamento de '
    'RPV ou Precatório expedido. Após, nada mais havendo, conclusos para extinção.'
)
# TJAL 0000015-53.2011.8.02.0033, mov 698643676.
TXT_PAGO = (
    'Ante o exposto, declaro por sentença satisfeito o débito descrito na inicial, '
    'nos termos do artigo 925 do Código de Processo Civil, e JULGO EXTINTO O '
    'PROCESSO DE EXECUÇÃO, com fundamento do artigo 924, inciso II'
)
# TRF5 0000201-16.2026.4.05.8403, mov 429306142.
TXT_EXTNEG = (
    'Adverte-se ainda a parte autora que o seu não comparecimento injustificado '
    'ensejará a extinção do processo sem resolução do mérito.'
)
# Ordem, não expedição: casa F14 ('ofício requisitório'), que o vigia NÃO usa.
TXT_F14_ORDEM = 'Expeça-se ofício requisitório em favor da parte autora.'
# 1000418-65.2023.8.11.0087 (TJMT, Guarantã do Norte), 14/11/2023 — F39.
TXT_EXTINTO_SATISFEITO = (
    'Diante da comprovação de pagamento da condenação, determino a expedição de alvará de '
    'levantamento dos créditos. Desta forma, haja vista o adimplemento da obrigação, '
    'extingo a execução pelo pagamento, na forma do art. 924, II, do CPC.'
)
TXT_NEUTRO = 'Intimem-se as partes para manifestação no prazo de 15 dias.'


def _trib(sigla):
    t, _ = Tribunal.objects.get_or_create(sigla=sigla, defaults={'nome': sigla, 'sigla_djen': sigla})
    return t


def _proc(sigla, cnj, movs, classificacao=None, classe='12078'):
    t = _trib(sigla)
    p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo=classe, classificacao=classificacao)
    for i, (dia, texto, *tipo) in enumerate(movs):
        Movimentacao.objects.create(
            processo=p,
            tribunal=t,
            external_id=f'{cnj}-{i}',
            texto=texto,
            tipo_comunicacao=tipo[0] if tipo else '',
            data_disponibilizacao=dt.datetime(*dia, 3, tzinfo=dt.UTC),
        )
    return p


def _um(cnj, **kw):
    r = sinais.calcular_sinais([cnj], **kw)
    assert len(r['results']) == 1, r
    return r['results'][0]


@pytest.mark.django_db
class TestSinalPorTipo:
    def test_f2_tipo_de_comunicacao(self):
        _proc(
            'TJAL',
            '0000015-53.2011.8.02.0033',
            [((2021, 10, 13), TXT_F2, 'Expedição de precatório/rpv'), ((2021, 11, 1), TXT_NEUTRO, 'Intimação')],
        )
        r = _um('0000015-53.2011.8.02.0033')
        assert r['sinal_tipo'] == 'F2_precat_tc'
        assert r['sinal_expedicao_em'] == '2021-10-13'
        assert r['sinal_trecho'] == 'Expedição de precatório/rpv'
        assert r['movimentacao_mais_recente_em'] == '2021-11-01'

    def test_f7_preparada_para_envio(self):
        _proc('TJPA', '0000352-51.2012.8.14.0040', [((2026, 5, 24), TXT_F7, 'Preparada para Envio')])
        r = _um('0000352-51.2012.8.14.0040')
        assert (r['sinal_tipo'], r['sinal_expedicao_em']) == ('F7_envTrib_tc', '2026-05-24')

    def test_f20_participio_e_trecho_centrado(self):
        _proc('TJSP', '0000023-70.2021.8.26.0337', [((2023, 3, 15), TXT_F20)])
        r = _um('0000023-70.2021.8.26.0337')
        assert r['sinal_tipo'] == 'F20_exp_juriscope'
        assert r['sinal_expedicao_em'] == '2023-03-15'
        assert 'ofício requisitório expedido' in r['sinal_trecho']
        assert len(r['sinal_trecho']) <= sinais.TRECHO_MAX
        assert r['sinal_trecho'].startswith('…')

    def test_f31_tjrs_com_entidade_html(self):
        _proc('TJRS', '5001727-66.2025.8.21.0067', [((2026, 9, 30), TXT_F31)])
        r = _um('5001727-66.2025.8.21.0067')
        assert (r['sinal_tipo'], r['sinal_expedicao_em']) == ('F31_exp_precatorio_tjrs', '2026-09-30')
        assert 'precatório enviada ao tribunal' in r['sinal_trecho']
        assert '&' not in r['sinal_trecho'] and '<' not in r['sinal_trecho']
        assert r['sinais_considerados'] == ['F31_exp_precatorio_tjrs']

    def test_f20_nao_vale_no_tjrs(self):
        """Como na regra de promoção: no TJRS o F20 casa ordem e RPV SEFAZ."""
        _proc('TJRS', '5023148-96.2024.8.21.0019', [((2026, 9, 1), TXT_F20)])
        r = _um('5023148-96.2024.8.21.0019')
        assert r['sinal_expedicao_em'] is None
        assert r['sinais_por_feature'] == {'F31_exp_precatorio_tjrs': None}

    def test_f31_nao_vale_fora_do_tjrs(self):
        _proc('TJMG', '5002816-68.2023.8.13.0051', [((2026, 9, 1), TXT_F31)])
        assert _um('5002816-68.2023.8.13.0051')['sinal_expedicao_em'] is None

    def test_ordem_f14_nao_e_sinal(self):
        _proc('TJMG', '5009780-22.2025.8.13.0079', [((2026, 9, 1), TXT_F14_ORDEM)])
        assert _um('5009780-22.2025.8.13.0079')['sinal_tipo'] is None

    def test_vale_a_ultima_movimentacao_que_casa(self):
        _proc(
            'TRF1',
            '1047957-32.2025.4.01.3300',
            [((2026, 1, 10), TXT_F20), ((2026, 3, 5), TXT_F7, 'Enviada ao Tribunal'), ((2026, 4, 1), TXT_NEUTRO)],
        )
        r = _um('1047957-32.2025.4.01.3300')
        assert (r['sinal_tipo'], r['sinal_expedicao_em']) == ('F7_envTrib_tc', '2026-03-05')
        assert r['sinais_por_feature'] == {
            'F2_precat_tc': None,
            'F7_envTrib_tc': '2026-03-05',
            'F20_exp_juriscope': '2026-01-10',
        }
        assert r['movimentacao_mais_recente_em'] == '2026-04-01'


@pytest.mark.django_db
class TestVetos:
    def test_f19_cancelamento(self):
        _proc('TJMT', '1002513-09.2022.8.11.0021', [((2024, 8, 6), TXT_F19)])
        v = _um('1002513-09.2022.8.11.0021')['vetos']['F19_cancelado_ANTI']
        assert v == {'ultima_em': '2024-08-06', 'ativo': True}

    def test_f24_pago_depois_da_expedicao(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033', [((2021, 10, 13), TXT_F20), ((2025, 9, 24), TXT_PAGO)])
        v = _um('0000015-53.2011.8.02.0033')['vetos']['F24_pago_pos_exped_ANTI']
        assert v == {'ultima_em': '2025-09-24', 'ativo': True}

    def test_f24_inativo_com_expedicao_depois_do_pagamento(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033', [((2021, 10, 13), TXT_PAGO), ((2025, 9, 24), TXT_F20)])
        v = _um('0000015-53.2011.8.02.0033')['vetos']['F24_pago_pos_exped_ANTI']
        assert v == {'ultima_em': '2021-10-13', 'ativo': False}

    def test_f30_extincao_negativa(self):
        _proc('TRF5', '0000201-16.2026.4.05.8403', [((2026, 3, 16), TXT_EXTNEG)])
        v = _um('0000201-16.2026.4.05.8403')['vetos']['F30_extinto_neg_ANTI']
        assert v == {'ultima_em': '2026-03-16', 'ativo': True}

    def test_f32_conversao_em_rpv_tjrs(self):
        _proc('TJRS', '5232029-64.2026.8.21.0001', [((2026, 10, 2), TXT_MINUTA), ((2026, 10, 9), TXT_F32)])
        r = _um('5232029-64.2026.8.21.0001')
        assert r['vetos']['F32_convertido_rpv_tjrs'] == {'ultima_em': '2026-10-09', 'ativo': True}
        # Minuta não é sinal de expedição (só o envio é); o veto F32 a usa como
        # referência, igual ao classificador.
        assert r['sinal_expedicao_em'] is None

    def test_minuta_sozinha_nao_e_sinal(self):
        _proc('TJRS', '5231192-09.2026.8.21.0001', [((2026, 10, 16), TXT_MINUTA)])
        r = _um('5231192-09.2026.8.21.0001')
        assert r['sinal_expedicao_em'] is None
        assert r['sinais_por_feature'] == {'F31_exp_precatorio_tjrs': None}


@pytest.mark.django_db
class TestParidadeComClassificador:
    """Mesmos processos, duas leituras: `compute_features` (o classificador) e
    o endpoint. Qualquer divergência de regex ou de regra quebra aqui."""

    CASOS = [
        (
            'TJAL',
            '0000015-53.2011.8.02.0033',
            [((2021, 10, 13), TXT_F2, 'Expedição de precatório/rpv'), ((2025, 9, 24), TXT_PAGO)],
        ),
        ('TJPA', '0000352-51.2012.8.14.0040', [((2026, 5, 24), TXT_F7, 'Preparada para Envio')]),
        ('TJSP', '0000023-70.2021.8.26.0337', [((2023, 3, 15), TXT_F20), ((2023, 1, 1), TXT_EXTNEG)]),
        ('TJRS', '5232029-64.2026.8.21.0001', [((2026, 10, 2), TXT_MINUTA), ((2026, 10, 9), TXT_F32)]),
        ('TJRS', '5023148-96.2024.8.21.0019', [((2026, 9, 1), TXT_F20)]),
        ('TJMT', '1002513-09.2022.8.11.0021', [((2024, 8, 6), TXT_F19)]),
        ('TRF5', '0000201-16.2026.4.05.8403', [((2026, 3, 16), TXT_EXTNEG), ((2026, 4, 1), TXT_F20)]),
        ('TJMG', '5009780-22.2025.8.13.0079', [((2026, 9, 1), TXT_F14_ORDEM)]),
        ('TJMT', '1000418-65.2023.8.11.0087', [((2023, 11, 14), TXT_EXTINTO_SATISFEITO)]),
        # Expedição depois da extinção: crédito novo, o F39 não vale.
        ('TJMT', '1005419-07.2025.8.11.0040',
         [((2026, 4, 24), TXT_EXTINTO_SATISFEITO), ((2026, 7, 25), TXT_F20)]),
        # Fora do rollout do F39: o mesmo texto não conta.
        ('TJMG', '5011469-11.2023.8.13.0261', [((2025, 10, 16), TXT_EXTINTO_SATISFEITO)]),
    ]

    @pytest.mark.parametrize('sigla,cnj,movs', CASOS, ids=[c[1] for c in CASOS])
    def test_mesmas_features(self, sigla, cnj, movs):
        p = _proc(sigla, cnj, movs)
        f = compute_features(p)
        r = _um(cnj)
        for feat in r['sinais_considerados']:
            assert bool(r['sinais_por_feature'][feat]) == bool(f[feat]), feat
        assert bool(r['sinal_expedicao_em']) == any(f[x] for x in r['sinais_considerados'])
        for veto in (
            'F19_cancelado_ANTI',
            'F24_pago_pos_exped_ANTI',
            'F30_extinto_neg_ANTI',
            'F32_convertido_rpv_tjrs',
            'F39_extinto_satisfeito_ANTI',
        ):
            assert r['vetos'][veto]['ativo'] == bool(f[veto]), veto


@pytest.mark.django_db
class TestLoteETransicao:
    def test_nao_encontrado_invalido_e_sem_mascara(self):
        _proc('TJRS', '5001727-66.2025.8.21.0067', [((2026, 9, 30), TXT_F31)])
        r = sinais.calcular_sinais(['50017276620258210067', '1234567-00.2025.8.26.0000', 'xx'])
        assert [x['cnj'] for x in r['results']] == ['5001727-66.2025.8.21.0067']
        assert r['nao_encontrados'] == ['1234567-00.2025.8.26.0000']
        assert r['invalidos'] == ['xx']
        assert r['completo'] is True and r['nao_processados'] == []

    def test_processo_sem_movimentacao(self):
        _proc('TRF3', '5108904-48.2021.4.03.9999', [])
        r = _um('5108904-48.2021.4.03.9999')
        assert r['sinal_expedicao_em'] is None and r['movimentacao_mais_recente_em'] is None
        assert all(not v['ativo'] for v in r['vetos'].values())

    def test_mesmo_cnj_em_dois_tribunais(self):
        """TRF6 herdou processos com o número antigo do TRF1."""
        _proc('TRF1', '1000041-41.2017.4.01.3507', [((2026, 1, 2), TXT_F20)])
        _proc('TRF6', '1000041-41.2017.4.01.3507', [])
        r = sinais.calcular_sinais(['1000041-41.2017.4.01.3507'])
        assert [(x['tribunal'], x['sinal_expedicao_em']) for x in r['results']] == [
            ('TRF1', '2026-01-02'),
            ('TRF6', None),
        ]

    def test_ultima_transicao_e_desde(self):
        p = _proc(
            'TJRS', '5001727-66.2025.8.21.0067', [((2026, 9, 30), TXT_F31)], classificacao=Process.CLASSIF_PRECATORIO
        )
        for nivel, quando in (
            ('PRE_PRECATORIO', dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)),
            ('PRECATORIO', dt.datetime(2026, 10, 15, 2, 41, tzinfo=dt.UTC)),
        ):
            log = ClassificacaoLog.objects.create(processo=p, classificacao=nivel, score=1.0, versao='v6')
            ClassificacaoLog.objects.filter(pk=log.pk).update(criada_em=quando)
        r = _um('5001727-66.2025.8.21.0067', desde=dt.date(2026, 10, 1))
        assert r['nivel'] == 'PRECATORIO'
        assert r['ultima_transicao']['nivel'] == 'PRECATORIO'
        assert r['ultima_transicao']['nivel_anterior'] == 'PRE_PRECATORIO'
        # 02:41 UTC de 15/10 é 23:41 de 14/10 em São Paulo — ainda depois de 01/10.
        assert r['ultima_transicao']['depois_de_desde'] is True
        assert r['sinal_depois_de_desde'] is False
        r2 = _um('5001727-66.2025.8.21.0067', desde=dt.date(2026, 9, 30))
        assert r2['sinal_depois_de_desde'] is True

    def test_orcamento_estourado_devolve_nao_processados(self, monkeypatch):
        monkeypatch.setattr(sinais, 'LOTE_SQL', 1)
        for i, cnj in enumerate(
            ['0000015-53.2011.8.02.0033', '0000352-51.2012.8.14.0040', '0000023-70.2021.8.26.0337']
        ):
            _proc('TJSP', cnj, [((2026, 1, 1 + i), TXT_F20)])
        r = sinais.calcular_sinais(
            ['0000015-53.2011.8.02.0033', '0000352-51.2012.8.14.0040', '0000023-70.2021.8.26.0337'], orcamento_s=0
        )
        # A primeira fatia roda sempre; o resto volta para o cliente reenviar.
        assert len(r['results']) == 1
        assert len(r['nao_processados']) == 2 and r['completo'] is False
        assert {x['cnj'] for x in r['results']}.isdisjoint(r['nao_processados'])


@pytest.mark.django_db(transaction=True)
def test_paralelo_da_o_mesmo_resultado(monkeypatch):
    """Threads com conexão própria (exige dado commitado: `transaction=True`)."""
    monkeypatch.setattr(sinais, 'LOTE_SQL', 1)
    cnjs = [
        '0000015-53.2011.8.02.0033',
        '0000352-51.2012.8.14.0040',
        '0000023-70.2021.8.26.0337',
        '5001727-66.2025.8.21.0067',
    ]
    _proc('TJAL', cnjs[0], [((2021, 10, 13), TXT_F2, 'Expedição de precatório/rpv')])
    _proc('TJPA', cnjs[1], [((2026, 5, 24), TXT_F7, 'Preparada para Envio')])
    _proc('TJSP', cnjs[2], [((2023, 3, 15), TXT_F20)])
    _proc('TJRS', cnjs[3], [((2026, 9, 30), TXT_F31)])
    seq = sinais.calcular_sinais(cnjs)
    par = sinais.calcular_sinais(cnjs, paralelismo=3, teto_s=20)
    assert par == seq
    # `results` sai em ordem de CNJ.
    assert [x['sinal_tipo'] for x in par['results']] == [
        'F2_precat_tc',
        'F20_exp_juriscope',
        'F7_envTrib_tc',
        'F31_exp_precatorio_tjrs',
    ]


@pytest.mark.django_db
class TestEndpoint:
    @pytest.fixture
    def cliente(self, settings):
        # Threads abrem conexão própria e não enxergam o dado não commitado do
        # teste; o caminho paralelo tem teste próprio (`transaction=True`).
        settings.SINAIS_PARALELISMO = 1
        return ApiClient.objects.create(nome='falcon-sinais', api_key='k-sinais', ativo=True)

    def _post(self, corpo, chave='k-sinais'):
        return Client().post(URL, corpo, content_type='application/json', HTTP_X_API_KEY=chave)

    def test_sem_chave_403(self, cliente):
        assert self._post({'cnjs': ['x']}, chave='errada').status_code == 403
        assert Client().post(URL, {'cnjs': ['x']}, content_type='application/json').status_code == 403

    def test_cliente_inativo_403(self, cliente):
        ApiClient.objects.filter(pk=cliente.pk).update(ativo=False)
        assert self._post({'cnjs': ['x']}).status_code == 403

    def test_limite_5000(self, cliente):
        r = self._post({'cnjs': [f'{i:07d}-00.2024.8.26.0001' for i in range(5001)]})
        assert r.status_code == 400
        assert '5000' in r.json()['erro']

    def test_corpo_invalido(self, cliente):
        assert self._post({'cnjs': []}).status_code == 400
        assert self._post({'cnjs': 'x'}).status_code == 400
        assert self._post({'cnjs': ['x'], 'desde': '16/10/2026'}).status_code == 400

    def test_resposta(self, cliente):
        _proc('TJRS', '5001727-66.2025.8.21.0067', [((2026, 9, 30), TXT_F31)])
        r = self._post({'cnjs': ['5001727-66.2025.8.21.0067', '1234567-00.2025.8.26.0000'], 'desde': '2026-09-01'})
        assert r.status_code == 200
        corpo = r.json()
        assert corpo['count'] == 1 and corpo['desde'] == '2026-09-01'
        assert corpo['completo'] is True and corpo['nao_processados'] == []
        assert corpo['nao_encontrados'] == ['1234567-00.2025.8.26.0000']
        item = corpo['results'][0]
        assert item['sinal_expedicao_em'] == '2026-09-30'
        assert item['sinal_depois_de_desde'] is True
        assert isinstance(corpo['tempo_ms'], int)

    def test_lote_de_5000_aceito(self, cliente):
        """O limite é inclusivo; CNJs inexistentes custam só a busca no índice."""
        r = self._post({'cnjs': [f'{i:07d}-00.2024.8.26.0001' for i in range(5000)]})
        assert r.status_code == 200
        assert len(r.json()['nao_encontrados']) == 5000


@pytest.mark.django_db
def test_veto_f39_tjmt_traz_a_data_da_extincao():
    _proc('TJMT', '1000418-65.2023.8.11.0087', [((2023, 11, 14), TXT_EXTINTO_SATISFEITO)])
    v = _um('1000418-65.2023.8.11.0087')['vetos']['F39_extinto_satisfeito_ANTI']
    assert v == {'ultima_em': '2023-11-14', 'ativo': True}
