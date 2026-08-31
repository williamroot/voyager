"""O gate que não depende de watermark nem de fila.

CONTEXTO (31/08/2026). O Postgres do TJSP fechou com 0 processos sem
`tem_sinal_precatorio` e o índice ainda tinha **524.945** docs com o campo em
`null`. Os três instrumentos de saúde do sync diziam "tudo certo":

    watermark `sync_es:wm:proc_ts` .... 1 min 23 s de atraso  (em dia)
    fila `es_index` .................. 0                     (drenada)
    FailedJobRegistry(es_index) ...... 0                     (nada falhou)

Os documentos ESTAVAM no índice — só que velhos. Contagem de documento sozinha
não teria acusado nada; presença sozinha, muito menos.

O que estes testes protegem:
  1. contagem dos DOIS lados por faixa acusa o que falta, e reenfileira;
  2. o CONTROLE (`proc` em 100%) manda a medição inteira fora quando falha —
     régua torta não se publica;
  3. a amostra de campo pega doc PRESENTE e DESATUALIZADO, usando
     `.get(...) is not None` e nunca `campo in _source` (que MENTE: a chave
     existe com valor `null`; a mesma amostra deu 60/60 com `in` e 38/60 com
     `.get(...) is not None`);
  4. faixa que não fecha NÃO avança o cursor;
  5. o cursor é DURÁVEL — um gate que morre no restart do Redis é o mesmo
     silêncio verde que ele veio matar.
"""
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache

from search import gate_completude as gc
from search import watermarks as wm_store


@pytest.fixture(autouse=True)
def _limpa_cache():
    cache.clear()
    yield
    cache.clear()


def _es_falso(total_es, controle=None, docs=None):
    """`count` devolve `total_es` (e `controle` na pergunta com `exists`)."""
    es = MagicMock()

    def count(index=None, query=None, **kw):
        tem_exists = 'bool' in query
        return {'count': (controle if tem_exists and controle is not None
                          else total_es)}

    es.count.side_effect = count
    es.mget.return_value = {'docs': docs or []}
    return es


def _doc(pid, proc='0001', **campos):
    fonte = {'proc': proc}
    fonte.update(campos)
    return {'_id': str(pid), 'found': True, '_source': fonte}


# --------------------------------------------------------------------------

def test_delta_entre_os_dois_lados_vira_erro_e_reenfileira():
    """Mutação: faça `conferir_faixa` devolver cedo quando `delta == 0` e o
    teste continua verde; faça-a IGNORAR o delta e ele quebra aqui."""
    es = _es_falso(total_es=900, docs=[_doc(i, tem_sinal_precatorio=True)
                                       for i in range(1, 4)])
    with patch.object(gc, '_pg') as pg, \
         patch('search.gate._es', return_value=es), \
         patch('search.gate.ausentes_no_bloco', return_value=[7, 8, 9]), \
         patch('search.gate.enfileirar_processos', return_value=3) as enf, \
         patch.object(gc.logger, 'error') as erro:
        pg.side_effect = [
            [(1000,)],                                   # count(*) no PG
            [(1, True), (2, True), (3, True)],           # amostra de campo
            [(i,) for i in range(1, 11)],                # ids da faixa
        ]
        saida = gc.conferir_faixa(0, 1_000_000)

    assert saida['pg'] == 1000 and saida['es'] == 900
    assert saida['delta'] == 100
    assert saida['reenfileirados'] == 3
    enf.assert_called_once_with([7, 8, 9])
    assert erro.called


def test_controle_abaixo_de_100_manda_a_medicao_inteira_fora():
    """Régua torta não se publica — foi um controle em 0,0% que pegou uma em 30/08."""
    es = _es_falso(total_es=1000, controle=10)
    with patch.object(gc, '_pg', return_value=[(1000,)]), \
         patch('search.gate._es', return_value=es), \
         patch('search.gate.enfileirar_processos') as enf, \
         patch.object(gc.logger, 'error') as erro:
        saida = gc.conferir_faixa(0, 1_000_000)

    assert saida['abstido'] == 'controle abaixo de 100%'
    assert 'delta' not in saida, 'publicou número medido com régua torta'
    enf.assert_not_called()
    assert erro.called


def test_amostra_pega_doc_presente_e_desatualizado():
    """O caso dos 524.945: doc no índice, campo em `null`, PG com valor.

    Mutação: troque `src.get(campo)` por `campo in src` e `divergentes` zera —
    a chave ESTÁ no `_source`, com valor `null`.
    """
    docs = [_doc(1, tem_sinal_precatorio=None),      # ← chave presente, valor null
            _doc(2, tem_sinal_precatorio=True),
            {'_id': '3', 'found': False}]
    es = _es_falso(total_es=3, docs=docs)
    with patch.object(gc, '_pg', return_value=[(1, True), (2, True), (3, True)]), \
         patch('search.gate._es', return_value=es):
        r = gc.amostrar_campos(0, 10)

    assert r['divergentes'] == [1]
    assert r['ausentes'] == [3]
    assert r['controle'] == 100.0


def test_amostra_nao_chama_de_divergente_a_abstencao_do_postgres():
    """`NULL` no PG é abstenção (regra nº 6), não atraso de entrega."""
    es = _es_falso(total_es=1, docs=[_doc(1, tem_sinal_precatorio=None)])
    with patch.object(gc, '_pg', return_value=[(1, None)]), \
         patch('search.gate._es', return_value=es):
        assert gc.amostrar_campos(0, 10)['divergentes'] == []


def test_contagem_abstem_quando_um_dos_lados_nao_responde():
    es = _es_falso(total_es=0)
    es.count.side_effect = RuntimeError('ES fora')
    with patch.object(gc, '_pg', return_value=[(1000,)]), \
         patch('search.gate._es', return_value=es):
        saida = gc.conferir_faixa(0, 1_000_000)

    assert saida['abstido'] == 'contagem não fechou dos dois lados'
    assert 'delta' not in saida


# --------------------------------------------------------------------------
# O cursor
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_faixa_que_nao_fecha_nao_avanca_o_cursor():
    with patch.object(gc, '_topo', return_value=10_000_000), \
         patch.object(gc, 'conferir_faixa',
                      return_value={'abstido': 'contagem não fechou dos dois lados'}):
        saida = gc.tick_gate_completude()

    assert saida['cursor'] == 0
    valor, estado = wm_store.obter(gc.WM_FAIXA)
    assert estado == 'primeiro', 'avançou o cursor por cima de faixa não conferida'


def _faixa_ok(ini, fim, reparar=True):
    """Faixa que fecha. Dicionário NOVO a cada chamada — o tick escreve nele."""
    return {'faixa': [ini, fim], 'pg': 1, 'es': 1, 'delta': 0}


@pytest.mark.django_db
def test_cursor_avanca_e_da_a_volta_no_acervo():
    with patch.object(gc, '_topo', return_value=int(1.5 * gc.FAIXA)), \
         patch.object(gc, 'conferir_faixa', side_effect=_faixa_ok):
        primeira = gc.tick_gate_completude()
        segunda = gc.tick_gate_completude()
        terceira = gc.tick_gate_completude()

    assert primeira['cursor'] == gc.FAIXA
    assert segunda['cursor'] == 0, 'não deu a volta ao chegar no topo'
    assert terceira['cursor'] == gc.FAIXA


@pytest.mark.django_db
def test_cursor_do_gate_sobrevive_ao_restart_do_redis():
    """Um gate com cursor volátil recomeça do zero e some com o alerta junto.

    Mutação: troque `wm_store` por `cache` em `tick_gate_completude` e a
    terceira passada volta para a faixa [0, 1 M) — o gate reconferindo o começo
    para sempre e nunca chegando na ponta nova, que é onde mora o buraco.
    """
    with patch.object(gc, '_topo', return_value=10 * gc.FAIXA), \
         patch.object(gc, 'conferir_faixa', side_effect=_faixa_ok):
        gc.tick_gate_completude()
        gc.tick_gate_completude()

        cache.clear()                       # ← o restart do Redis

        saida = gc.tick_gate_completude()

    assert saida['faixa'] == [2 * gc.FAIXA, 3 * gc.FAIXA], (
        'o gate perdeu a posição no restart e recomeçou a varredura')
