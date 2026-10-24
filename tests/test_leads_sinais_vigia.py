"""`POST /api/v1/leads/sinais/` com `itens` — o contrato do vigia de expedição (F1).

O que importa: só contam as publicações POSTERIORES ao `desde` de cada CNJ
(o download do JURISCOPE), os sinais são os do classificador por tribunal e o
`pago` é o F24 recortado nessa janela. Textos reais reaproveitados de
`test_leads_sinais.py`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client

from tribunals import sinais_expedicao as sinais
from tribunals.models import ApiClient, Movimentacao, Process, Tribunal

from .test_leads_sinais import TXT_F2, TXT_F7, TXT_F14_ORDEM, TXT_F20, TXT_F31, TXT_NEUTRO, TXT_PAGO

URL = '/api/v1/leads/sinais/'

# TJPA 0800xxx (formulário do ofício publicado) — casa o F41.
TXT_F41 = 'Ficam as partes intimadas do inteiro teor do Ofício Precatório juntado aos autos.'
TXT_RPV_EXPEDIDA = 'Certifico que a RPV expedida foi encaminhada ao ente devedor.'
TXT_PRECAT_EXPEDIDO = 'Certifico que o precatório expedido foi encaminhado ao Tribunal.'

DESDE = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)


def _trib(sigla):
    t, _ = Tribunal.objects.get_or_create(sigla=sigla, defaults={'nome': sigla, 'sigla_djen': sigla})
    return t


def _proc(sigla, cnj, movs, classificacao=None):
    t = _trib(sigla)
    p = Process.objects.create(tribunal=t, numero_cnj=cnj, classe_codigo='12078', classificacao=classificacao)
    for i, (dia, texto, *tipo) in enumerate(movs):
        Movimentacao.objects.create(
            processo=p, tribunal=t, external_id=f'{cnj}-{sigla}-{i}', texto=texto,
            tipo_comunicacao=tipo[0] if tipo else '',
            data_disponibilizacao=dt.datetime(*dia, 3, tzinfo=dt.UTC),
        )
    return p


def _um(cnj, desde=DESDE):
    r = sinais.calcular_sinais_vigia([(cnj, desde)])
    assert len(r['sinais']) == 1, r
    return r['sinais'][0]


@pytest.mark.django_db
class TestJanela:
    def test_sinal_antes_do_desde_nao_conta(self):
        _proc('TJSP', '0000023-70.2021.8.26.0337',
              [((2026, 7, 10), TXT_F20), ((2026, 8, 5), TXT_NEUTRO), ((2026, 8, 6), TXT_NEUTRO)])
        s = _um('0000023-70.2021.8.26.0337')
        assert s['conhecido'] is True
        assert s['publicacoes_desde'] == 2
        assert s['sinal_expedicao'] is False
        assert s['tipo'] is None and s['ultimo_sinal_em'] is None

    def test_sinal_depois_do_desde_conta(self):
        _proc('TJSP', '0000023-70.2021.8.26.0337',
              [((2026, 7, 10), TXT_NEUTRO), ((2026, 9, 2), TXT_F20)], classificacao='PRE_PRECATORIO')
        s = _um('0000023-70.2021.8.26.0337')
        assert s['publicacoes_desde'] == 1
        assert s['sinal_expedicao'] is True
        assert s['tipo'] == 'oficio'  # "ofício requisitório expedido"
        assert s['ultimo_sinal_em'].startswith('2026-09-02')
        assert s['classificacao'] == 'PRE_PRECATORIO'
        assert s['pago'] is False

    def test_desde_por_item(self):
        _proc('TJSP', '0000023-70.2021.8.26.0337', [((2026, 9, 2), TXT_F20)])
        _proc('TJSP', '0000024-55.2021.8.26.0337', [((2026, 9, 2), TXT_F20)])
        r = sinais.calcular_sinais_vigia([
            ('0000023-70.2021.8.26.0337', DESDE),
            ('0000024-55.2021.8.26.0337', dt.datetime(2026, 10, 1, tzinfo=dt.UTC)),
        ])
        por_cnj = {s['cnj']: s for s in r['sinais']}
        assert por_cnj['0000023-70.2021.8.26.0337']['sinal_expedicao'] is True
        assert por_cnj['0000024-55.2021.8.26.0337']['sinal_expedicao'] is False
        assert por_cnj['0000024-55.2021.8.26.0337']['publicacoes_desde'] == 0

    def test_desconhecido_e_invalido(self):
        r = sinais.calcular_sinais_vigia([('1234567-00.2025.8.26.0000', DESDE), ('lixo', DESDE)])
        assert r['invalidos'] == ['lixo']
        assert r['sinais'] == [sinais._item_vazio('1234567-00.2025.8.26.0000')]

    def test_cnj_sem_mascara_e_repetido_usa_o_desde_mais_antigo(self):
        _proc('TJSP', '0000023-70.2021.8.26.0337', [((2026, 9, 2), TXT_F20)])
        r = sinais.calcular_sinais_vigia([
            ('00000237020218260337', dt.datetime(2026, 10, 1, tzinfo=dt.UTC)),
            ('0000023-70.2021.8.26.0337', DESDE),
        ])
        assert len(r['sinais']) == 1
        assert r['sinais'][0]['sinal_expedicao'] is True


@pytest.mark.django_db
class TestSinaisETipo:
    def test_f14_ordem_nao_e_sinal(self):
        _proc('TJSP', '0000023-70.2021.8.26.0337', [((2026, 9, 2), TXT_F14_ORDEM)])
        assert _um('0000023-70.2021.8.26.0337')['sinal_expedicao'] is False

    def test_f2_tipo_de_comunicacao(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033',
              [((2026, 9, 2), TXT_F2, 'Expedição de precatório/rpv')])
        s = _um('0000015-53.2011.8.02.0033')
        assert s['sinal_expedicao'] is True
        assert s['tipo'] == 'oficio'  # o movimento TPU é um só para os dois

    def test_f7_envio(self):
        _proc('TJPA', '0000352-51.2012.8.14.0040', [((2026, 9, 2), TXT_F7, 'Preparada para Envio')])
        s = _um('0000352-51.2012.8.14.0040')
        assert s['sinal_expedicao'] is True and s['tipo'] == 'oficio'

    def test_f20_rpv_e_precatorio(self):
        _proc('TJMG', '5000001-11.2024.8.13.0024', [((2026, 9, 2), TXT_RPV_EXPEDIDA)])
        _proc('TJMG', '5000002-11.2024.8.13.0024', [((2026, 9, 2), TXT_PRECAT_EXPEDIDO)])
        assert _um('5000001-11.2024.8.13.0024')['tipo'] == 'rpv'
        assert _um('5000002-11.2024.8.13.0024')['tipo'] == 'precatorio'

    def test_tjrs_so_f31(self):
        _proc('TJRS', '5001727-66.2025.8.21.0067', [((2026, 9, 2), TXT_PRECAT_EXPEDIDO)])
        assert _um('5001727-66.2025.8.21.0067')['sinal_expedicao'] is False
        _proc('TJRS', '5001728-66.2025.8.21.0067', [((2026, 9, 2), TXT_F31)])
        s = _um('5001728-66.2025.8.21.0067')
        assert s['sinal_expedicao'] is True and s['tipo'] == 'precatorio'

    def test_tjpa_f41(self):
        _proc('TJPA', '0800001-11.2024.8.14.0301', [((2026, 9, 2), TXT_F41)])
        s = _um('0800001-11.2024.8.14.0301')
        assert s['sinal_expedicao'] is True and s['tipo'] == 'precatorio'

    def test_f41_nao_vale_fora_do_tjpa(self):
        _proc('TJMA', '0800001-11.2024.8.10.0001', [((2026, 9, 2), TXT_F41)])
        assert _um('0800001-11.2024.8.10.0001')['sinal_expedicao'] is False

    def test_dois_tribunais_duas_linhas(self):
        _proc('TRF1', '1000001-11.2020.4.01.3800', [((2026, 9, 2), TXT_F20)])
        _proc('TRF6', '1000001-11.2020.4.01.3800', [((2026, 9, 2), TXT_NEUTRO)])
        r = sinais.calcular_sinais_vigia([('1000001-11.2020.4.01.3800', DESDE)])
        assert [(s['tribunal'], s['sinal_expedicao']) for s in r['sinais']] == [('TRF1', True), ('TRF6', False)]


@pytest.mark.django_db
class TestPago:
    def test_pago_depois_do_sinal(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033',
              [((2026, 9, 2), TXT_F20), ((2026, 10, 2), TXT_PAGO)])
        s = _um('0000015-53.2011.8.02.0033')
        assert s['sinal_expedicao'] is True
        assert s['pago'] is True
        assert s['tipo'] == 'alvara_pago'
        assert s['ultimo_sinal_em'].startswith('2026-10-02')

    def test_sinal_novo_depois_do_pago_nao_e_pago(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033',
              [((2026, 9, 2), TXT_PAGO), ((2026, 10, 2), TXT_F20)])
        s = _um('0000015-53.2011.8.02.0033')
        assert s['pago'] is False and s['tipo'] == 'oficio'

    def test_pago_sem_sinal_na_janela(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033',
              [((2026, 7, 2), TXT_F20), ((2026, 9, 2), TXT_PAGO)])
        s = _um('0000015-53.2011.8.02.0033')
        assert s['sinal_expedicao'] is False
        assert s['pago'] is True and s['tipo'] == 'alvara_pago'

    def test_pago_antes_do_desde_nao_conta(self):
        _proc('TJAL', '0000015-53.2011.8.02.0033', [((2026, 7, 2), TXT_PAGO)])
        assert _um('0000015-53.2011.8.02.0033')['pago'] is False


@pytest.mark.django_db
class TestEndpoint:
    @pytest.fixture
    def cliente(self):
        return ApiClient.objects.create(nome='falcon-vigia', api_key='k-vigia', ativo=True)

    def _post(self, corpo, chave='k-vigia'):
        return Client().post(URL, corpo, content_type='application/json', HTTP_X_API_KEY=chave)

    def test_sem_chave_403(self, cliente):
        assert self._post({'itens': [{'cnj': 'x', 'desde': '2026-08-01'}]}, chave='errada').status_code == 403

    def test_contrato(self, cliente):
        _proc('TJSP', '0000023-70.2021.8.26.0337', [((2026, 9, 2), TXT_F20)], classificacao='PRE_PRECATORIO')
        r = self._post({'itens': [
            {'cnj': '0000023-70.2021.8.26.0337', 'desde': '2026-08-01T00:00:00Z'},
            {'cnj': '1234567-00.2025.8.26.0000', 'desde': '2026-08-01'},
            {'cnj': 'lixo', 'desde': '2026-08-01'},
        ]})
        assert r.status_code == 200, r.content
        corpo = r.json()
        assert corpo['invalidos'] == ['lixo']
        assert corpo['count'] == 2
        primeiro, segundo = corpo['sinais']
        assert set(primeiro) == {'cnj', 'tribunal', 'conhecido', 'publicacoes_desde', 'sinal_expedicao',
                                 'ultimo_sinal_em', 'tipo', 'classificacao', 'pago'}
        assert primeiro['sinal_expedicao'] is True and primeiro['classificacao'] == 'PRE_PRECATORIO'
        assert segundo['conhecido'] is False

    def test_desde_obrigatorio(self, cliente):
        r = self._post({'itens': [{'cnj': '0000023-70.2021.8.26.0337'}]})
        assert r.status_code == 400 and 'desde' in r.json()['erro']
        r = self._post({'itens': [{'cnj': '0000023-70.2021.8.26.0337', 'desde': 'ontem'}]})
        assert r.status_code == 400

    def test_itens_vazio_e_limite(self, cliente):
        assert self._post({'itens': []}).status_code == 400
        muitos = [{'cnj': f'{i:07d}-00.2025.8.26.0000', 'desde': '2026-08-01'} for i in range(501)]
        assert self._post({'itens': muitos}).status_code == 400

    def test_data_sem_hora_e_meia_noite_de_sao_paulo(self, cliente):
        # Publicação disponibilizada em 01/08 00:00 -03 (03:00 UTC) não é "> desde".
        _proc('TJSP', '0000023-70.2021.8.26.0337', [])
        p = Process.objects.get(numero_cnj='0000023-70.2021.8.26.0337')
        Movimentacao.objects.create(
            processo=p, tribunal=p.tribunal, external_id='x', texto=TXT_F20,
            data_disponibilizacao=dt.datetime(2026, 8, 1, 3, tzinfo=dt.UTC))
        r = self._post({'itens': [{'cnj': '0000023-70.2021.8.26.0337', 'desde': '2026-08-01'}]})
        assert r.json()['sinais'][0]['sinal_expedicao'] is False

    def test_contrato_antigo_continua(self, cliente):
        _proc('TJSP', '0000023-70.2021.8.26.0337', [((2026, 9, 2), TXT_F20)])
        r = self._post({'cnjs': ['0000023-70.2021.8.26.0337']})
        assert r.status_code == 200 and 'results' in r.json()


@pytest.mark.django_db(transaction=True)
def test_paralelo_da_o_mesmo_resultado():
    """Threads com conexão própria (exige dado commitado: `transaction=True`)."""
    _proc('TJAL', '0000015-53.2011.8.02.0033', [((2026, 9, 2), TXT_F2, 'Expedição de precatório/rpv')])
    _proc('TJPA', '0000352-51.2012.8.14.0040', [((2026, 9, 2), TXT_F7, 'Preparada para Envio')])
    _proc('TJSP', '0000023-70.2021.8.26.0337', [((2026, 9, 2), TXT_F20), ((2026, 10, 2), TXT_PAGO)])
    _proc('TJRS', '5001727-66.2025.8.21.0067', [((2026, 9, 30), TXT_F31)])
    itens = [(c, DESDE) for c in ('0000015-53.2011.8.02.0033', '0000352-51.2012.8.14.0040',
                                  '0000023-70.2021.8.26.0337', '5001727-66.2025.8.21.0067',
                                  '1234567-00.2025.8.26.0000')]
    seq = sinais.calcular_sinais_vigia(itens)
    par = sinais.calcular_sinais_vigia(itens, teto_s=20, paralelismo=3)
    assert par == seq
    assert [s['tipo'] for s in par['sinais']] == ['oficio', 'oficio', 'alvara_pago', 'precatorio', None]
