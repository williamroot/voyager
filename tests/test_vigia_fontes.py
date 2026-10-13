"""O vigia das fontes: pausa quem está fora, e despausa quando volta.

Em 30/08/2026 o PJe de consulta pública do TJMG passou a servir uma página de
erro estática (912 bytes, `Last-Modified` de 2016) com **HTTP 200**. Como não
era 5xx, passava pelo `raise_for_status`, quebrava no parser e virava um `erro`
opaco: 1.184 erro/h por réplica, 0 ok, e ~7.100 req/h queimando o pool
COMPARTILHADO de IPs. Nada avisou.

O que este arquivo protege, em ordem de importância:

1. o vigia **NUNCA** despausa o que um humano pausou;
2. uma sonda que falha por REDE não vira "tribunal fora" (senão o vigia pausa
   tribunal saudável quando o proxy pisca);
3. a decisão é por MAIORIA de sondas independentes;
4. pausar e despausar saem em ERROR — nunca corte mudo (regra nº 2).
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from enrichers import jobs


PAGINA_ERRO = '<html><head><link href="/error/style.css" /></head>' \
              '<body><img src="/error/server_error.gif" /></body></html>'
PAGINA_BOA = '<html><body><form id="fPP">' + ('x' * 5000) + '</form></body></html>'


def _resposta(texto: str, status: int = 200):
    r = MagicMock()
    r.text = texto
    r.content = texto.encode()
    r.status_code = status
    r.ok = status < 400
    r.headers = {}
    return r


@pytest.fixture
def vigia(monkeypatch):
    """Isola o estado das pausas em dicionário de memória."""
    estado = {'pausados': set(), 'auto': set()}
    monkeypatch.setattr(jobs, 'enrich_pausados', lambda: set(estado['pausados']))
    monkeypatch.setattr(jobs, 'set_enrich_pausados',
                        lambda s: estado.__setitem__('pausados', set(s)))
    monkeypatch.setattr(jobs, '_auto_pausados', lambda: set(estado['auto']))
    monkeypatch.setattr(jobs, '_set_auto_pausados',
                        lambda s: estado.__setitem__('auto', set(s)))
    monkeypatch.setattr(jobs, '_ENRICHERS', {'TJMG': MagicMock(LIST_URL='https://x/y')})
    return estado


def test_pagina_de_erro_com_200_pausa_o_tribunal(vigia):
    with patch('requests.get', return_value=_resposta(PAGINA_ERRO)):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == {'TJMG'}
    assert vigia['auto'] == {'TJMG'}, 'tem que registrar que a pausa foi do vigia'
    assert 'pausado' in rel['TJMG']


def test_fonte_que_volta_e_despausada_sozinha(vigia):
    vigia['pausados'] = {'TJMG'}
    vigia['auto'] = {'TJMG'}
    with patch('requests.get', return_value=_resposta(PAGINA_BOA)):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == set(), 'pausa manual esquecida é o modo de falha'
    assert vigia['auto'] == set()
    assert 'despausado' in rel['TJMG']


def test_vigia_nunca_despausa_pausa_de_humano(vigia):
    """A pausa de gente tem motivo que o vigia não conhece."""
    vigia['pausados'] = {'TJMG'}
    vigia['auto'] = set()                 # ninguém registrou: foi humano
    with patch('requests.get', return_value=_resposta(PAGINA_BOA)) as g:
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == {'TJMG'}
    assert 'humano' in rel['TJMG']
    g.assert_not_called(), 'nem sonda: não é assunto do vigia'


def test_inconclusivo_NUNCA_despausa(vigia):
    """Abstenção não pode virar afirmação em direção nenhuma.

    Medido em produção (30/08/2026): `vigia DESPAUSOU TJPA — fonte voltou:
    inconclusivo (0 viva/1 morta/0 duvida/2 rede)`. Com um booleano, "não sei"
    caía no mesmo lado de "voltou", e o TJPA oscilou — pausado 11:38,
    despausado 11:58, e assim por diante.
    """
    vigia['pausados'] = {'TJMG'}
    vigia['auto'] = {'TJMG'}
    with patch('requests.get', side_effect=requests.exceptions.ProxyError('boom')):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == {'TJMG'}, 'sem prova, não se mexe em nada'
    assert 'inconclusivo' in rel['TJMG']


def test_desafio_do_waf_nao_e_fonte_fora(vigia):
    """O 202 do `awselb` vem com 0 bytes e caía em "2xx pequeno demais".

    O TJPE respondia 61% dos jobs e mesmo assim foi pausado pelo vigia. O
    anti-bot é tratado pelo enricher rotacionando proxy; pausar por causa dele
    joga fora coleta que estava funcionando.
    """
    r = _resposta('', status=202)
    r.headers = {'x-amzn-waf-action': 'challenge'}
    with patch('requests.get', return_value=r):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == set(), 'WAF não é fonte fora'
    assert 'WAF' in rel['TJMG']


def test_falha_de_rede_nao_e_tribunal_fora(vigia):
    """Uma sonda mede o PROXY tanto quanto a fonte. `ProxyError` isolado não
    pode pausar tribunal saudável — foi assim que dois timeouts viraram
    'o eproc bloqueia datacenter', e estava errado."""
    with patch('requests.get', side_effect=requests.exceptions.ProxyError("boom")):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == set()
    assert 'inconclusivo' in rel['TJMG'] or rel['TJMG'] == 'ok'


def test_uma_sonda_ruim_nao_derruba_a_maioria(vigia):
    with patch('requests.get', side_effect=[_resposta(PAGINA_ERRO),
                                            _resposta(PAGINA_BOA),
                                            _resposta(PAGINA_BOA)]):
        jobs.tick_vigia_fontes()
    assert vigia['pausados'] == set(), '1 de 3 não é maioria'


def test_duas_de_tres_pausam(vigia):
    with patch('requests.get', side_effect=[_resposta(PAGINA_ERRO),
                                            _resposta(PAGINA_BOA),
                                            _resposta(PAGINA_ERRO)]):
        jobs.tick_vigia_fontes()
    assert vigia['pausados'] == {'TJMG'}


def test_motivo_vem_de_quem_decidiu_nao_da_ultima_sonda(vigia):
    """2 sondas viram página de erro e 1 falhou por rede: o vigia dizia
    'fonte fora: ProxyError', culpando o proxy por uma decisão que foi da
    fonte. Motivo errado manda a investigação pro lado errado."""
    with patch('requests.get', side_effect=[_resposta(PAGINA_ERRO),
                                            _resposta(PAGINA_ERRO),
                                            requests.exceptions.ProxyError('boom')]):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == {'TJMG'}
    assert 'pagina de erro' in rel['TJMG'], rel['TJMG']
    assert 'ProxyError' not in rel['TJMG']


@pytest.mark.parametrize('status,bytes_,deve_pausar,porque', [
    (200,  800, True,  '2xx pequeno demais é casca de erro (TJPA: 848 bytes)'),
    (503, 2000, True,  '5xx é a fonte falando e falhando'),
    (404,  148, False, 'TJDFT devolve 404 na LIST_URL — pode ser sonda errada'),
    (401,    0, False, 'TJMT devolve 401 — pode exigir sessão antes'),
    (403, 5553, False, 'TJAP devolve 403 — WAF ou sonda errada, não dá pra separar'),
    (200, 9000, False, 'página de verdade'),
])
def test_4xx_em_get_seco_nao_prova_que_a_fonte_caiu(vigia, status, bytes_, deve_pausar, porque):
    """O vigia também tem que abster (regra nº 6).

    Pausar tribunal saudável porque NOSSA sonda pediu a URL errada seria o
    vigia produzindo exatamente a confiança falsa que ele existe para evitar.
    """
    resp = _resposta('x' * bytes_, status=status)
    with patch('requests.get', return_value=resp):
        jobs.tick_vigia_fontes()
    assert (vigia['pausados'] == {'TJMG'}) is deve_pausar, porque


# ── A porta mudou de lugar (TRF3, 14/09/2026) ───────────────────────────────
#
# O `listView.seam` do TRF3 passou a responder 302 para uma SPA Angular em
# `pje1g-consultapublica.trf3.jus.br`. O `requests` segue o redirect, a SPA
# vem com HTTP 200 e corpo grande, e o vigia dizia "viva" — enquanto o
# enricher gravava "javax.faces.ViewState não encontrado (HTTP 200)" em ~147
# mil processos em 5 dias e queimava ~170 mil requisições/dia do pool.
#
# A SPA abaixo é CONSTRUÍDA (o host não responde da máquina de dev). O que
# importa nela está medido: HTTP 200, corpo > 4.000 bytes, outro host, e
# nenhum `<input name="javax.faces.ViewState">`.
from enrichers.pje import BasePjeEnricher, porta_mudou, tem_viewstate  # noqa: E402
from enrichers.trf3 import Trf3Enricher  # noqa: E402

SPA_TRF3 = ('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            '<title>Consulta Processual</title><base href="/">'
            '<link rel="stylesheet" href="styles.css"></head><body>'
            '<app-root></app-root><script src="runtime.js" type="module"></script>'
            '<script src="main.js" type="module"></script>'
            + ('<!-- bundle -->' * 400) + '</body></html>')
FORM_JSF = ('<html><body><form id="fPP" name="fPP">'
            '<input type="hidden" name="javax.faces.ViewState" '
            'id="javax.faces.ViewState" value="j_id1" />'
            + ('x' * 5000) + '</form></body></html>')


def _redirecionada(texto: str, url_final: str, status: int = 200):
    r = _resposta(texto, status=status)
    r.history = [_resposta('', status=302)]
    r.url = url_final
    return r


@pytest.fixture
def vigia_trf3(vigia, monkeypatch):
    monkeypatch.setattr(jobs, '_ENRICHERS', {'TRF3': Trf3Enricher})
    return vigia


def test_redirect_para_outro_host_pausa_pje(vigia_trf3):
    spa = _redirecionada(
        SPA_TRF3, 'https://pje1g-consultapublica.trf3.jus.br/consulta-publica')
    assert len(spa.content) > jobs.VIGIA_BYTES_MINIMOS, 'controle: a SPA é "grande"'
    with patch('requests.get', return_value=spa):
        rel = jobs.tick_vigia_fontes()
    assert vigia_trf3['pausados'] == {'TRF3'}
    assert vigia_trf3['auto'] == {'TRF3'}, 'a pausa é do vigia: ele despausa se a porta voltar'
    assert 'pje1g-consultapublica.trf3.jus.br' in rel['TRF3'], rel['TRF3']


def test_pje_200_grande_sem_viewstate_e_fonte_morta(vigia_trf3):
    with patch('requests.get', return_value=_resposta(SPA_TRF3)):
        rel = jobs.tick_vigia_fontes()
    assert vigia_trf3['pausados'] == {'TRF3'}
    assert 'ViewState' in rel['TRF3']


def test_pje_com_formulario_jsf_continua_viva(vigia_trf3):
    """Controle positivo: sem ele, um vigia que pausa TUDO passaria nos dois
    testes acima."""
    vigia_trf3['pausados'] = {'TRF3'}
    vigia_trf3['auto'] = {'TRF3'}
    with patch('requests.get', return_value=_resposta(FORM_JSF)):
        rel = jobs.tick_vigia_fontes()
    assert vigia_trf3['pausados'] == set()
    assert 'despausado' in rel['TRF3']


def test_redirect_no_mesmo_host_nao_e_porta_mudada(vigia_trf3):
    mesmo = _redirecionada(
        FORM_JSF, 'https://pje1g.trf3.jus.br/pje/ConsultaPublica/listView.seam?x=1')
    with patch('requests.get', return_value=mesmo):
        jobs.tick_vigia_fontes()
    assert vigia_trf3['pausados'] == set()


def test_redirect_de_outro_sistema_so_abstem(vigia):
    """Fora do PJe/JSF não sabemos se redirect de host é normal (SSO, CDN):
    abster > chutar. Fica DITO no relatório, sem pausar."""
    outro = _redirecionada(PAGINA_BOA, 'https://outro.exemplo.jus.br/consulta')
    with patch('requests.get', return_value=outro):
        rel = jobs.tick_vigia_fontes()
    assert vigia['pausados'] == set()
    assert 'inconclusivo' in rel['TJMG'] and 'outro.exemplo.jus.br' in rel['TJMG']


def test_viewstate_e_o_elemento_nao_a_palavra():
    assert tem_viewstate(FORM_JSF)
    assert not tem_viewstate(SPA_TRF3)
    assert not tem_viewstate('<!-- javax.faces.ViewState some daqui -->' + 'x' * 5000)
    assert not tem_viewstate("<script>var k='javax.faces.ViewState';</script>")


def test_porta_mudou_nao_afirma_sem_prova():
    assert porta_mudou('https://a.jus.br/x', _resposta('ok')) is None, \
        'dublê sem history/url não pode virar "mudou de host"'
    assert porta_mudou('https://a.jus.br/x', _redirecionada('', 'https://a.jus.br/y')) is None
    assert porta_mudou('https://a.jus.br/x',
                       _redirecionada('', 'https://b.jus.br/y')) == 'b.jus.br'


def test_enricher_diz_que_a_porta_mudou_no_erro(monkeypatch):
    """O `enriquecimento_erro` tem de apontar a causa, não o sintoma."""
    from enrichers.pje import PjeEnricherError

    enr = Trf3Enricher.__new__(Trf3Enricher)
    enr.logger = MagicMock()
    spa = _redirecionada(
        SPA_TRF3, 'https://pje1g-consultapublica.trf3.jus.br/consulta-publica')
    monkeypatch.setattr(enr, '_get', lambda url: spa, raising=False)
    with pytest.raises(PjeEnricherError, match='mudou de host'):
        enr._buscar_em_grau('5000001-00.2024.4.03.6100', '1g')


def test_todo_enricher_pje_exige_viewstate_na_sonda():
    assert BasePjeEnricher.SONDA_EXIGE_VIEWSTATE is True
    assert Trf3Enricher.SONDA_EXIGE_VIEWSTATE is True
