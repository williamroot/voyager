"""O zero do #104 é FOTO, não estado — e o tique existe por causa disso.

Medido em 31/08/2026: a corrida completa levou `classe_id IS NULL` de 8.054.334
a **21** e `assunto_id` a **222**. Trinta minutos depois, sem corrida nova:
**25** e **227**. Quem volta são linhas ANTIGAS reescritas ao vivo (datajud,
hidratação, enricher) por um caminho que grava `classe_codigo` e não resolve a
FK.

O que este arquivo protege:

1. o modo incremental filtra por `atualizado_em` — sem isso, o tique varreria
   104 M linhas de hora em hora atrás de duas dezenas;
2. sem `--desde-atualizado`, a corrida completa segue existindo (não quebrei o
   modo antigo);
3. a janela é MAIOR que o intervalo entre execuções — sobreposição é barata,
   buraco não é.
"""
from unittest.mock import patch

import tribunals.jobs as J
from tribunals.management.commands import repop_classe_assunto as R


def _sql_do_bloco(opts):
    """O SQL que `_um_bloco` monta, sem tocar no banco."""
    capturado = {}

    class CursorFalso:
        def execute(self, sql, params=None):
            if 'SELECT' in sql:
                capturado['sql'] = sql
                capturado['params'] = params
        def fetchall(self):
            return []
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    cmd = R.Command()
    with patch.object(R.connection, 'cursor', return_value=CursorFalso()), \
         patch.object(R.transaction, 'atomic'):
        tabela, pares = R.ALVOS['process']
        cmd._um_bloco(tabela, pares, 0, 1000, {'lidos': 0}, {}, {}, opts)
    return capturado


def _opts(**extra):
    base = {'statement_timeout': '120s', 'desde_atualizado': 0.0}
    base.update(extra)
    return base


def test_modo_incremental_filtra_por_atualizado_em():
    """Sem o filtro, o tique de hora em hora faria Seq Scan de 104 M linhas."""
    cap = _sql_do_bloco(_opts(desde_atualizado=3.0))
    assert 'atualizado_em >' in cap['sql'], cap['sql']
    assert '3.0 hours' in cap['params'], cap['params']


def test_corrida_completa_continua_sem_filtro():
    """O modo antigo não pode ter sido quebrado pelo novo."""
    cap = _sql_do_bloco(_opts())
    assert 'atualizado_em' not in cap['sql']
    assert cap['params'] == [0, 1000]


def test_janela_cobre_o_intervalo_entre_execucoes():
    """3 h de janela para 1 h de intervalo. Se alguém encurtar a janela abaixo
    do intervalo, abre buraco: linha reescrita logo após um tique não seria
    vista pelo seguinte."""
    assert J.JANELA_REPOP_H > 1.0, 'janela tem que ser maior que o intervalo'


def test_tique_chama_o_comando_com_teto_e_janela():
    """Nada no caminho de um tique periódico sem teto de espera (regra nº 7)."""
    with patch('django.core.management.call_command') as cc:
        r = J.tick_repop_fk_recente()
    _, kwargs = cc.call_args
    assert kwargs['desde_atualizado'] == J.JANELA_REPOP_H
    assert kwargs['max_segundos'] == 900, 'sem teto, o tique atropela o próximo'
    assert r['janela_h'] == J.JANELA_REPOP_H


def test_cursor_salta_para_o_proximo_pk_da_janela():
    """O filtro dentro do bloco não basta: o LAÇO também precisa ser guiado.

    Medido em 31/08/2026, antes deste salto: o tique percorria os blocos de pk
    um a um mesmo com a janela vazia — 30 s para chegar em pk 32 M de 104 M, ou
    seja, nunca fecharia dentro do teto de 900 s. Filtro certo, cursor cego.
    """
    cmd = R.Command()
    with patch.object(cmd, '_proximo_pk_recente') as prox:
        prox.return_value = None
        assert cmd._proximo_pk_recente('t', (), 0, 100, {}) is None
    # e o SQL do salto tem que parar na PRIMEIRA linha (nunca agregar a janela)
    capturado = {}

    class CursorFalso:
        def execute(self, sql, params=None):
            if 'ORDER BY id LIMIT 1' in sql:
                capturado['sql'] = sql
                capturado['params'] = params
        def fetchone(self):
            return (777,)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    tabela, pares = R.ALVOS['process']
    with patch.object(R.connection, 'cursor', return_value=CursorFalso()), \
         patch.object(R.transaction, 'atomic'):
        n = R.Command()._proximo_pk_recente(
            tabela, pares, 10, 999, _opts(desde_atualizado=3.0))
    assert n == 777
    assert 'atualizado_em >' in capturado['sql']
    assert '3.0 hours' in capturado['params']
    # `min(id)` agrega a janela inteira e estourou o statement_timeout em prod
    assert 'min(' not in capturado['sql'], capturado['sql']
    assert 'LIMIT 1' in capturado['sql']
