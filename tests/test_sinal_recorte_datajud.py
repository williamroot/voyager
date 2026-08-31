"""O recorte do `backfill_sinal_precatorio` media o próprio buraco.

MEDIDO em produção, 31/08/2026, TJSP:

    tem_datajud | tem_sinal | processos
    ------------+-----------+-----------
    f           | f         | 14.504.845
    f           | t         |    763.477
    t           | (NULL)    |  1.513.486   ← 100% dos NULL restantes

Todo processo do TJSP que ainda não tinha sinal já havia passado pelo Datajud —
e o pick do comando trazia `AND data_enriquecimento_datajud IS NULL`. Ou seja:
acrescentar `'TJSP'` em `DATAJUD_ALVO` (o conserto óbvio, o que a pendência #97
pedia) selecionaria **zero** linhas e terminaria `SUCCESS: 0 processados`. Run
verde, log limpo, número redondo — as três assinaturas do CLAUDE.md.

Aqui a mecânica é exercitada de verdade (cursor falso que grava o SQL e os
parâmetros), não conferida por `grep` no fonte: os testes olham a consulta que o
comando MANDA, o que ele faz quando um lote estoura o teto, e o que ele declara
no fim sobre quem ficou de fora.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command

MOD = 'tribunals.management.commands.backfill_sinal_precatorio'


class FakeCursor:
    """Cursor que grava (sql, params) e devolve o que o teste programar.

    `picks` é a fila de lotes que o pick vai devolver, na ordem. `erro_em` é o
    número da chamada de UPDATE que levanta (simula o statement_timeout).
    """

    def __init__(self, picks, erro_em=(), censo=None):
        self.picks = list(picks)
        self.erro_em = set(erro_em)
        self.censo = censo if censo is not None else []
        self.executados = []
        self._ultimo = None
        self._n_update = 0
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executados.append((sql, params))
        if sql.startswith('SET LOCAL'):
            self._ultimo = 'set'
        elif sql.startswith('SELECT id FROM tribunals_process'):
            self._ultimo = 'pick'
        elif sql.startswith('UPDATE tribunals_process'):
            self._n_update += 1
            if self._n_update in self.erro_em:
                raise RuntimeError('canceling statement due to statement timeout')
            # por padrão escreve tudo que veio no lote
            self.rowcount = len(params[1])
            self._ultimo = 'update'
        elif 'GROUP BY 1' in sql or 'DISTINCT tribunal_id' in sql:
            self._ultimo = 'censo'
        else:
            self._ultimo = 'conta'

    def fetchall(self):
        if self._ultimo == 'pick':
            return [(i,) for i in (self.picks.pop(0) if self.picks else [])]
        return list(self.censo)

    def fetchone(self):
        return (0,)

    # --- helpers de leitura -------------------------------------------------
    def picks_sql(self):
        return [(s, p) for s, p in self.executados
                if s.startswith('SELECT id FROM tribunals_process')]

    def updates_sql(self):
        return [(s, p) for s, p in self.executados
                if s.startswith('UPDATE tribunals_process')]


class FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def roda(cursor, **kw):
    with patch(f'{MOD}.connection') as conn, patch(f'{MOD}.transaction') as tx, \
            patch(f'{MOD}.time.sleep'):
        conn.cursor.return_value = cursor
        tx.atomic.return_value = FakeCtx()
        opts = dict(tribunais='TJSP', batch=2, sleep=0, sem_censo=True)
        opts.update(kw)
        call_command('backfill_sinal_precatorio', **opts)


# --------------------------------------------------------------------- corte

def test_o_pick_nao_exclui_quem_ja_foi_ao_datajud():
    """MUTAÇÃO: devolva `AND data_enriquecimento_datajud IS NULL` ao pick e este
    teste quebra — que é exatamente o estado em que 1.513.486 processos do TJSP
    eram invisíveis para um run que terminava verde."""
    cur = FakeCursor(picks=[[1, 2], []])
    roda(cur)
    sql, _ = cur.picks_sql()[0]
    assert 'data_enriquecimento_datajud' not in sql, (
        'o pick voltou a excluir quem já passou pelo Datajud — no TJSP isso é '
        '100% dos NULL, e o comando termina SUCCESS sem tocar em nada')
    assert 'tem_sinal_precatorio IS NULL' in sql


def test_flag_so_sem_datajud_restaura_o_corte_da_fase_0():
    """O corte não some: vira flag explícita, para priorizar a fila do refill."""
    cur = FakeCursor(picks=[[1, 2], []])
    roda(cur, so_sem_datajud=True)
    sql, _ = cur.picks_sql()[0]
    assert 'data_enriquecimento_datajud IS NULL' in sql


# ---------------------------------------------------------------- abstenção

def test_processo_sem_movimentacao_nao_vira_false():
    """`EXISTS(texto ~* padrão)` dá `false` para quem não tem texto NENHUM.

    `false` na tela quer dizer "medimos e não tem" — confiança falsa. Regra 8:
    abster > chutar. MUTAÇÃO: tire o segundo EXISTS do UPDATE e o teste quebra.
    """
    cur = FakeCursor(picks=[[1, 2], []])
    roda(cur)
    sql, _ = cur.updates_sql()[0]
    assert 'FROM tribunals_movimentacao m2' in sql and 'AND EXISTS' in sql, (
        'o UPDATE escreve FALSE em processo sem movimentação nenhuma')


def test_abstidos_sao_contados_e_ditos(capsys):
    class _Cur(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith('UPDATE tribunals_process'):
                self.rowcount = 1        # 3 pegos, 1 escrito ⇒ 2 abstidos

    cur = _Cur(picks=[[1, 2, 3], []])
    roda(cur, batch=3)
    saida = capsys.readouterr().out
    assert 'abstidos 2' in saida, saida


# ------------------------------------------------------------ teto = ERRO

def test_lote_queimado_pelo_teto_e_erro_e_nao_volta():
    """Teto atingido é ERRO com o número real — e o lote NÃO é re-selecionado.

    Engolir o timeout sem excluir os ids seria pior que estourar: o pick não tem
    ORDER BY, então o MESMO lote voltaria para sempre. MUTAÇÃO: tire o
    `AND NOT (id = ANY(...))` do pick e o segundo pick não carregará os ids
    queimados — o comando entra em loop.
    """
    cur = FakeCursor(picks=[[10, 11], [12, 13], []], erro_em={1})
    roda(cur, batch=2)
    picks = cur.picks_sql()
    assert len(picks) >= 2, 'o comando parou no primeiro lote queimado'
    # o 2º pick tem que carregar os ids queimados na lista de exclusão
    queimados_no_2o = picks[1][1][-2]
    assert queimados_no_2o == [10, 11], (
        f'o pick não excluiu o lote queimado ({queimados_no_2o}) — loop infinito')


def test_lote_queimado_faz_o_fim_sair_como_erro(capsys):
    cur = FakeCursor(picks=[[10, 11], []], erro_em={1})
    roda(cur, batch=2)
    err = capsys.readouterr().err
    assert 'LOTE QUEIMADO' in err and 'QUEIMADOS pelo teto' in err, err
    assert '2' in err, 'o erro tem que trazer o NÚMERO real de processos'


# ------------------------------------------------------- alerta do recorte

def test_o_fim_declara_quem_ficou_NULL_fora_do_recorte(capsys):
    """Sem isto, "terminou" continua querendo dizer "acabou" — e não quer.

    MUTAÇÃO: troque o `stderr.write(ERROR)` por um `stdout.write` discreto e o
    teste quebra; era assim que 21,7 M de NULL nacionais cabiam num run verde.
    """
    cur = FakeCursor(picks=[[1, 2], []],
                     censo=[('TJMG', 4431133), ('TJSP', 1513486), ('TRF3', 4018460)])
    roda(cur, sem_censo=False)
    cap = capsys.readouterr()
    assert 'FORA DO RECORTE' in cap.err, cap.err
    assert '8,449,593' in cap.err or '8.449.593' in cap.err, (
        f'o alerta tem que trazer o total real fora do recorte: {cap.err}')
    assert 'TJMG 4,431,133' in cap.err


# ------------------------------------------------------- kill switch

def _com_switch(ligado):
    """Patch do cache do módulo: `get(OFF)` devolve `ligado`."""
    return patch(f'{MOD}.cache.get', side_effect=lambda k, *a: ligado)


def test_kill_switch_para_o_loop_sem_deploy():
    """31/08/2026: 8 cópias deste comando, lote de ~35 s cada, não deixaram o
    R105 pegar UMA janela de 3 s para aplicar a migration 0054 — 40 tentativas.
    A única forma de parar era `docker stop` + `pg_cancel_backend` à mão.

    MUTAÇÃO: tire o `if cache.get(OFF)` do topo do laço e o comando processa o
    primeiro lote mesmo com o switch ligado.
    """
    cur = FakeCursor(picks=[[1, 2], [3, 4], []])
    with _com_switch(True):
        roda(cur, batch=2)
    assert cur.picks_sql() == [], 'o comando pegou lote com o kill switch ligado'
    assert cur.updates_sql() == []


def test_sem_o_switch_o_comando_roda_normalmente():
    """Controle da mutação acima: com o switch DESLIGADO ele tem que trabalhar —
    senão o teste passaria por um motivo errado (o comando nunca rodar)."""
    cur = FakeCursor(picks=[[1, 2], []])
    with _com_switch(False):
        roda(cur, batch=2)
    assert len(cur.picks_sql()) >= 1 and len(cur.updates_sql()) == 1


def test_parada_pelo_switch_NAO_sai_como_sucesso(capsys):
    """`SUCCESS` num run interrompido vira "o TJSP está pronto" na cabeça de
    quem lê o log. Parada pedida também se declara, com o número."""
    cur = FakeCursor(picks=[[1, 2], []])
    with _com_switch(True):
        roda(cur, batch=2)
    cap = capsys.readouterr()
    assert 'KILL SWITCH' in cap.err, cap.err
    assert 'NÃO terminou' in cap.err
    assert 'PARADO PELO KILL SWITCH' in cap.err
    assert 'FIM' not in cap.out, f'saiu FIM em stdout (verde): {cap.out}'


def test_a_chave_do_switch_e_a_documentada():
    """Se a chave mudar, o runbook do `.ia/OPS.md` para de funcionar em silêncio."""
    from tribunals.management.commands import backfill_sinal_precatorio as cmd
    assert cmd.OFF == 'backfill_sinal:off'
    assert cache is not None
