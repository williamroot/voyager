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
