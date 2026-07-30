"""Testes do motor de regras do Estágio do Crédito (núcleo puro, sem DB).

Cobre a hierarquia DC → PRE → EMITIDO → MORTO, os subtipos de extinção
(satisfeito / improcedente / sem mérito / incidente / ambígua) e as
utilidades de CNJ (normalização + inferência de tribunal).
"""
from datetime import datetime, timezone

from dashboard.services import estagio_rules as er


def _mov(i, dia, texto, tipo=''):
    return {'id': i, 'data': datetime(2024, 1, dia, tzinfo=timezone.utc),
            'tipo': tipo, 'texto': texto, 'meio': 'D'}


def test_emitido_por_expedicao_explicita():
    r = er.analisar_movs([
        _mov(1, 1, 'Iniciado o cumprimento de sentença contra a Fazenda Pública.'),
        _mov(2, 10, 'Certifico que foi expedido o ofício requisitório. Precatório expedido nesta data.'),
    ])
    assert r['classe'] == er.ESTAGIO_EMITIDO
    assert any(a['sinal'] == 'exped_confirmada' for a in r['ancoras'])
    assert r['confianca'] >= 0.85


def test_emitido_por_tipo_comunicacao():
    r = er.analisar_movs([
        _mov(1, 5, 'Intimação da parte.', tipo='Expedição de precatório/rpv'),
    ])
    assert r['classe'] == er.ESTAGIO_EMITIDO


def test_pre_cumprimento_sem_oficio():
    r = er.analisar_movs([
        _mov(1, 1, 'Autuado o cumprimento de sentença. Trânsito em julgado certificado.'),
        _mov(2, 8, 'Homologo os cálculos apresentados pela contadoria no valor de R$ 152.340,10.'),
    ])
    assert r['classe'] == er.ESTAGIO_PRE
    assert r['valor_homologado'] is not None
    assert abs(r['valor_homologado']['valor'] - 152340.10) < 0.01


def test_dc_sentenca_transito_sem_cumprimento():
    r = er.analisar_movs([
        _mov(1, 2, 'Julgo procedente o pedido e condeno o INSS ao pagamento das parcelas.'),
        _mov(2, 20, 'Certidão: trânsito em julgado em 15/01/2024.'),
    ])
    assert r['classe'] == er.ESTAGIO_DC


def test_morto_improcedente():
    r = er.analisar_movs([
        _mov(1, 2, 'Sentença: julgo improcedente o pedido inicial.'),
    ])
    assert r['classe'] == er.ESTAGIO_MORTO
    assert r['selo'] == er.SELO_IMPROCEDENTE


def test_morto_satisfeito_extincao_pelo_pagamento():
    r = er.analisar_movs([
        _mov(1, 1, 'Cumprimento de sentença. Expedição de precatório determinada.'),
        _mov(2, 25, 'Ante o pagamento integral do débito, julgo extinta a execução '
                    'nos termos do art. 924, II, do CPC. Expeça-se alvará de levantamento.'),
    ])
    assert r['classe'] == er.ESTAGIO_MORTO
    assert r['selo'] == er.SELO_SATISFEITO
    assert any(f['code'] == 'pagamento' for f in r['flags'])


def test_extincao_sem_merito_nao_rebaixa():
    r = er.analisar_movs([
        _mov(1, 1, 'Cumprimento de sentença iniciado contra a Fazenda.'),
        _mov(2, 5, 'Julgo extinto o processo sem resolução do mérito (art. 485, VI).'),
    ])
    assert r['classe'] == er.ESTAGIO_PRE  # mantém o estágio
    assert any(b['code'] == 'ext_sem_merito' for b in r['badges'])


def test_extincao_de_embargos_ignorada():
    r = er.analisar_movs([
        _mov(1, 1, 'Cumprimento de sentença em curso.'),
        _mov(2, 9, 'Extintos os embargos à execução opostos pela executada.'),
    ])
    assert r['classe'] == er.ESTAGIO_PRE
    assert any(b['code'] == 'ext_incidente' for b in r['badges'])


def test_extincao_ambigua_vira_badge():
    r = er.analisar_movs([
        _mov(1, 1, 'Cumprimento de sentença em curso.'),
        _mov(2, 9, 'Julgo extinto o processo.'),
    ])
    assert r['classe'] == er.ESTAGIO_PRE
    assert any(b['code'] == 'ext_ambigua' for b in r['badges'])


def test_improcedencia_superada_por_expedicao_posterior():
    r = er.analisar_movs([
        _mov(1, 1, 'Julgo improcedente o pedido.'),
        _mov(2, 20, 'Reformada a sentença. Precatório expedido nesta data.'),
    ])
    assert r['classe'] == er.ESTAGIO_EMITIDO
    assert any(b['code'] == 'improc_superada' for b in r['badges'])


def test_flag_rpv():
    r = er.analisar_movs([
        _mov(1, 1, 'Expedição de RPV — requisição de pequeno valor enviada ao TRF.'),
    ])
    assert r['classe'] == er.ESTAGIO_EMITIDO
    assert any(f['code'] == 'rpv' for f in r['flags'])


def test_indefinido_sem_sinais():
    r = er.analisar_movs([
        _mov(1, 1, 'Juntada de petição.'),
    ])
    assert r['classe'] == er.ESTAGIO_INDEFINIDO
    assert r['confianca'] <= 0.4


def test_classe_cadastro_cumprimento_vira_ancora():
    r = er.analisar_movs(
        [_mov(1, 1, 'Intimação da parte autora.')],
        classe_codigo='12078',
        classe_nome='Cumprimento de Sentença contra a Fazenda Pública',
    )
    assert r['classe'] == er.ESTAGIO_PRE
    assert any(a['sinal'] == 'cumprimento_classe' and a['mov_id'] is None
               for a in r['ancoras'])


def test_normalizar_cnj():
    assert er.normalizar_cnj('00081123420144013400') == '0008112-34.2014.4.01.3400'
    assert er.normalizar_cnj('0008112-34.2014.4.01.3400') == '0008112-34.2014.4.01.3400'
    assert er.normalizar_cnj('123') is None
    assert er.normalizar_cnj('') is None


def test_tribunal_do_cnj():
    assert er.tribunal_sigla_do_cnj('0008112-34.2014.4.01.3400') == 'TRF1'
    assert er.tribunal_sigla_do_cnj('0001234-56.2023.8.26.0000') == 'TJSP'
    assert er.tribunal_sigla_do_cnj('0001234-56.2023.8.07.0000') == 'TJDFT'
    assert er.tribunal_sigla_do_cnj('0001234-56.2023.8.10.0000') == 'TJMA'
    assert er.tribunal_sigla_do_cnj('0001234-56.2023.5.02.0000') == 'TRT2'
