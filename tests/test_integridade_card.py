"""O card de Integridade: régua torta não publica número.

Em 30/08/2026 a primeira medição deste card deu `partes 18,4%` e
`classe_codigo 0,0%` — e ia virar manchete de perda de dados. O que estava
quebrado era a régua: o índice renomeia os campos (`numero_cnj` → `proc`,
`classe_codigo` → `codigo_classe`, invertido), então o nome adivinhado lia
`None` em todo documento.

Quem denunciou foi o **campo de controle**: `proc` deu 0,0%, e é impossível.

O que este arquivo protege, em ordem de importância:

1. controle abaixo de 100% **suprime o bloco**, com o motivo — nunca publica;
2. a divergência é contada nos DOIS sentidos (o índice pode perder E inventar);
3. `partes` compara "tem parte" contra "tem parte", não texto contra contagem;
4. bloco que falhou sai da lista dizendo qual foi.
"""
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from dashboard import integridade


def _hit(pid, **campos):
    base = {'id': pid, 'proc': f'{pid:07d}-11.2024.8.26.0100',
            'assunto': '', 'codigo_classe': '', 'grau': '', 'partes': ''}
    base.update(campos)
    return {'_source': base}


def _es_falso(hits):
    es = MagicMock()
    es.count.return_value = {'count': 103_707_711}
    es.search.return_value = {'hits': {'hits': hits}}
    return es


def _pg_falso(linhas):
    """`linhas` = {id: (assunto_nome, classe_codigo, grau, n_partes)}."""
    cur = MagicMock()
    cur.fetchall.return_value = [(pid,) + t for pid, t in linhas.items()]
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    return ctx


def _medir(hits, pg):
    with patch('search.client.get_es', return_value=_es_falso(hits)), \
         patch.object(integridade.transaction, 'atomic', nullcontext), \
         patch.object(integridade.connection, 'cursor', return_value=_pg_falso(pg)), \
         patch.object(integridade, 'PASSADAS', 1):
        return integridade._amostra_indice_vs_banco()


def test_controle_abaixo_de_100_suprime_o_bloco():
    """A régua que mediu errado não pode publicar nenhum número."""
    hits = [_hit(1), _hit(2)]
    hits[0]['_source']['proc'] = ''          # o CNJ some: impossível na vida real
    r = _medir(hits, {1: ('a', '123', '1', 1), 2: ('b', '456', '2', 1)})
    assert 'suprimido' in r
    assert 'controle' in r['suprimido']
    assert 'linhas' not in r, 'bloco suprimido não pode vazar número nenhum'


def test_sem_divergencia_publica_zero():
    hits = [_hit(1, assunto='Precatório', codigo_classe='1234', grau='1', partes='X'),
            _hit(2)]
    r = _medir(hits, {1: ('Precatório', '1234', '1', 3), 2: ('', '', '', 0)})
    assert r['divergencia'] == 0
    assert r['amostra'] == 2
    assert {l['rotulo'] for l in r['linhas']} == {'Assunto', 'Classe', 'Grau', 'Partes'}


def test_indice_que_PERDEU_campo_aparece_em_so_no_PG():
    hits = [_hit(1)]                                  # ES vazio
    r = _medir(hits, {1: ('Precatório', '1234', '2', 5)})   # PG cheio
    por = {l['rotulo']: l for l in r['linhas']}
    assert por['Assunto']['so_pg'] == 1
    assert por['Assunto']['so_es'] == 0
    assert r['divergencia'] == 4, 'os quatro campos divergem'


def test_indice_que_INVENTOU_campo_aparece_em_so_no_ES():
    """Divergência tem dois sentidos. Contar só um lado esconde metade."""
    hits = [_hit(1, assunto='Precatório', codigo_classe='1234', grau='2', partes='X')]
    r = _medir(hits, {1: ('', '', '', 0)})
    por = {l['rotulo']: l for l in r['linhas']}
    assert por['Assunto']['so_es'] == 1
    assert por['Assunto']['so_pg'] == 0


def test_partes_compara_TER_parte_nos_dois_lados():
    """No ES `partes` é string; no banco é contagem de ProcessoParte. Comparar
    texto contra número daria divergência inventada em todo processo."""
    r = _medir([_hit(1, partes='FULANO DE TAL')], {1: ('', '', '', 7)})
    por = {l['rotulo']: l for l in r['linhas']}
    assert por['Partes'] == {'rotulo': 'Partes', 'pg': 1, 'es': 1,
                             'so_pg': 0, 'so_es': 0}


def test_bloco_que_falhou_sai_da_lista_dizendo_qual():
    with patch.object(integridade, '_amostra_indice_vs_banco',
                      side_effect=RuntimeError('ES fora')), \
         patch.object(integridade, '_fontes', return_value={'total': 3, 'ok': ['TJSP'],
                                                            'pelo_vigia': [], 'por_humano': []}), \
         patch.object(integridade, '_perguntas_poupadas', return_value={'total': 9, 'tribunais': []}):
        p = integridade.calcular()
    assert p['indice'] is None
    assert p['nao_medidos'] == ['indice']
    assert p['fontes']['total'] == 3, 'um bloco quebrado não derruba os outros'


def test_fontes_separa_pausa_do_vigia_da_pausa_de_humano():
    """Quem pausou muda o que acontece depois: o vigia despausa o dele quando a
    fonte volta, e nunca toca no que um humano pausou."""
    with patch('enrichers.jobs._ENRICHERS', {'TJSP': 1, 'TJMG': 1, 'TJAP': 1}), \
         patch('enrichers.jobs.enrich_pausados', return_value={'TJMG', 'TJAP'}), \
         patch('enrichers.jobs._auto_pausados', return_value={'TJMG'}):
        f = integridade._fontes()
    assert f['ok'] == ['TJSP']
    assert f['pelo_vigia'] == ['TJMG']
    assert f['por_humano'] == ['TJAP']


def test_ler_nao_mede_nada():
    """A tela lê só cache: o sorteio no ES custa ~25 s, e medição de rodapé sem
    teto já derrubou o site (regra nº 7)."""
    with patch('search.client.get_es', side_effect=AssertionError('não pode medir')):
        assert integridade.ler() in (None, integridade.cache.get(integridade.CHAVE))


def test_zero_poupadas_nao_mostra_o_bloco():
    """`_mil(0)` devolve a string `'0'`, que é VERDADEIRA no `{% if %}` do
    Django. Guardar o bloco pelo campo já formatado mostraria um painel zerado
    anunciando economia que não houve."""
    with patch('enrichers.jobs.censo_fora_do_esaj', return_value={}):
        p = integridade._perguntas_poupadas()
    assert p['n'] == 0, 'o template guarda por `n`, que é número'
    assert p['total'] == '0'
    assert not p['n']


def test_milhar_em_pt_br():
    assert integridade._mil(103_707_711) == '103.707.711'
    assert integridade._mil(None) == 'None'
