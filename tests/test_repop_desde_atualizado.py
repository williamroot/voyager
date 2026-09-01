"""O modo `--desde-atualizado` do `repop_classe_assunto`.

Este arquivo protegia o tique horário `tick_repop_fk_recente`. **O tique não
existe mais** (01/09/2026): os dois escritores que reabriam o buraco do #104 —
`datajud/hidratacao.py` e `datajud/ingestion.py::sync_processo` — passaram a
fechar a FK na própria escrita, e conserto na origem torna vassoura periódica
código morto. Ver `tests/test_fk_catalogo_na_origem.py`.

O que sobra aqui é a opção `--desde-atualizado`, que continua no comando para a
passada pontual. Ela tem duas armadilhas medidas em produção, e são estas que
os testes prendem:

1. o filtro por `atualizado_em` de fato entra no SQL do bloco — sem ele a
   passada varre 104 M linhas;
2. o CURSOR também tem que ser guiado pelo índice: com o filtro certo e o
   cursor cego, a varredura levava 30 s para chegar em pk 32 M de 104 M;
3. e o salto do cursor para na PRIMEIRA linha (`ORDER BY id LIMIT 1`) — o
   `min(id)` agrega a janela inteira e estourou `statement_timeout` em prod.

Sem `--desde-atualizado`, a corrida completa segue existindo, intocada.

⚠️ A opção NÃO serve para tique recorrente enquanto houver backfill em massa
nesta tabela: os shards do `backfill_fase` carimbam `atualizado_em` em milhões
de linhas e a "janela recente" vira quase a tabela inteira. Ver `djen/scheduler.py`.
"""
from unittest.mock import patch

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
