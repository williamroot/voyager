"""`SET LOCAL` fora de transação é silenciosamente inócuo — e nós tínhamos isso.

Incidente de 25/08/2026. Um `ALTER TABLE` protegido por um `SET lock_timeout`
solto ficou preso atrás de uma transação exploratória de 88 minutos e, como
ACCESS EXCLUSIVE **enfileira**, produção foi a **63 sessões esperando Lock** e
79 backends ativos — todo mundo que chegou depois do ALTER ficou atrás dele,
inclusive quem só queria um SELECT. `pg_cancel_backend` no ALTER devolveu o
número a 0 e os backends a 12.

Na auditoria que se seguiu, `mcp_server/delegates.py` tinha as quatro
variações do mesmo defeito no MESMO arquivo:

    linha 319  SET statement_timeout = 10000        <- solto
    linha 421  SET statement_timeout = 5000         <- solto
    linha 446  SET statement_timeout = 20000        <- solto
    linha 520  SET LOCAL statement_timeout = 10000  <- LOCAL, mas em autocommit

A linha 520 é a pior das quatro, e é o motivo deste arquivo existir: ela
**parece certa** na revisão de código e não faz absolutamente nada, porque
`SET LOCAL` só tem efeito dentro de uma transação. É a mesma família de
"verificação com input vazio reporta SUCESSO" que esta casa vem pagando a
semana inteira: o mecanismo de proteção existe, passa na leitura, e protege
zero.

Vítima medida do lado do `SET` solto: `IngestionRun 223570` (TJDFT,
2025-05-27) fechou `failed` por um timeout que não estava no código daquele
run — era o teto vazado de outra sessão que reusou a mesma conexão do pool.

O primeiro teste PROVA o comportamento no banco (controle positivo E
negativo, senão ele "passaria" sem medir nada). O segundo varre o código e
falha se o defeito voltar a ser escrito.
"""
import pathlib
import re

import pytest
from django.db import connection, transaction

RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: `SET` de teto SEM `LOCAL` — vaza para quem reusar a conexão do pool.
SET_SOLTO_RE = re.compile(r"""SET\s+(?!LOCAL\b)(statement_timeout|lock_timeout)""", re.I)
#: `SET LOCAL` de teto — só vale dentro de transação.
SET_LOCAL_RE = re.compile(r"""SET\s+LOCAL\s+(statement_timeout|lock_timeout)""", re.I)

#: Só linhas que EXECUTAM: docstring e comentário citando `SET LOCAL` são
#: documentação, não caminho de código — contá-los era falso positivo.
#: `tests/` e `scripts/` são ferramenta de bancada, não caminho de produção.
IGNORAR = ('/tests/', '/scripts/', '/migrations/', '/.git/', '/showcase_src/')


def _fontes_de_producao():
    for caminho in RAIZ.rglob('*.py'):
        texto = str(caminho)
        if any(parte in texto for parte in IGNORAR):
            continue
        yield caminho


@pytest.mark.django_db(transaction=True)
def test_set_local_so_tem_efeito_dentro_de_transacao():
    """Controle positivo E negativo. Sem os dois, este teste não mede nada."""
    with connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '7s'")
        c.execute('SHOW statement_timeout')
        em_autocommit = c.fetchone()[0]

    with transaction.atomic(), connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '7s'")
        c.execute('SHOW statement_timeout')
        dentro_da_transacao = c.fetchone()[0]

    with connection.cursor() as c:
        c.execute('SHOW statement_timeout')
        depois_do_commit = c.fetchone()[0]

    # controle POSITIVO: dentro da transação o teto vale de fato
    assert dentro_da_transacao == '7s', (
        'SET LOCAL dentro de transaction.atomic() não pegou — se isto falhar, '
        'a proteção que escrevemos em todo lugar não existe'
    )
    # controle NEGATIVO: em autocommit ele é inócuo — este é o defeito
    assert em_autocommit != '7s', (
        'SET LOCAL fora de transação pegou. Se o banco passou a se comportar '
        'assim, reescreva este teste antes de confiar nele'
    )
    # e não vaza para a próxima transação
    assert depois_do_commit != '7s'


def _funcao_que_contem(linhas, i):
    """Devolve (inicio, fim) do `def` que contém a linha i."""
    recuo = len(linhas[i]) - len(linhas[i].lstrip())
    inicio = 0
    for j in range(i, -1, -1):
        atual = linhas[j]
        if not atual.strip():
            continue
        r = len(atual) - len(atual.lstrip())
        if r < recuo and atual.lstrip().startswith(('def ', 'async def ')):
            inicio, recuo_def = j, r
            break
    else:
        return 0, len(linhas)
    for j in range(inicio + 1, len(linhas)):
        atual = linhas[j]
        if not atual.strip():
            continue
        r = len(atual) - len(atual.lstrip())
        if r <= recuo_def and not atual.lstrip().startswith(')'):
            return inicio, j
    return inicio, len(linhas)


def test_set_de_teto_solto_ou_reseta_ou_usa_conexao_dedicada():
    """`SET` de sessão VAZA para quem reusar a conexão do pool.

    Vítima medida: `IngestionRun 223570` (TJDFT, 2025-05-27) fechou `failed`
    por um timeout que **não estava no código daquele run** — era o teto
    vazado de outra sessão que reusou a mesma conexão.

    Nem todo `SET` solto é defeito, e a regra reflete isso em vez de proibir
    por proibir. Ele é legítimo em exatamente dois casos:

      · **quando a query NÃO PODE estar em transação.** `REFRESH MATERIALIZED
        VIEW CONCURRENTLY` é o caso real aqui (3 sítios em `dashboard/tasks.py`):
        `SET LOCAL` ali seria inócuo e `atomic()` quebraria o comando. A
        obrigação, então, é **devolver a conexão limpa** — `RESET` depois.
      · **quando a conexão é dedicada** (`psycopg.connect()` próprio, fechado
        no `with`), porque ela não volta para pool nenhum.

    Fora desses dois, é vazamento.
    """
    ofensores = []
    for caminho in _fontes_de_producao():
        linhas = caminho.read_text(encoding='utf-8').splitlines()
        for i, linha in enumerate(linhas):
            if '.execute(' not in linha:
                continue
            m = SET_SOLTO_RE.search(linha)
            if not m:
                continue
            guc = m.group(1).lower()
            ini, fim = _funcao_que_contem(linhas, i)
            corpo = '\n'.join(linhas[ini:fim])
            reseta = re.search(rf'RESET\s+{guc}|SET\s+{guc}\s*=?\s*DEFAULT',
                               corpo, re.I)
            dedicada = 'psycopg.connect(' in corpo or '_psycopg.connect(' in corpo
            if not reseta and not dedicada:
                ofensores.append(
                    f'{caminho.relative_to(RAIZ)}:{i+1}: {linha.strip()}')
    assert not ofensores, (
        'SET de teto de sessão sem RESET e sem conexão dedicada — vaza para '
        'quem reusar a conexão do pool:\n  ' + '\n  '.join(ofensores)
    )


def test_todo_set_local_de_teto_esta_dentro_de_uma_transacao():
    """`SET LOCAL` em autocommit **parece certo e não faz nada**.

    Heurística deliberadamente simples: exige `transaction.atomic()` na mesma
    linha do `with` que abre o cursor, ou numa linha anterior com indentação
    MENOR (o bloco que envolve). Prefere falso positivo a falso negativo —
    um teto que não protege é o defeito que este arquivo existe para impedir.
    """
    ofensores = []
    for caminho in _fontes_de_producao():
        linhas = caminho.read_text(encoding='utf-8').splitlines()
        for i, linha in enumerate(linhas):
            if '.execute(' not in linha or not SET_LOCAL_RE.search(linha):
                continue
            recuo = len(linha) - len(linha.lstrip())
            protegido = False
            for anterior in reversed(linhas[max(0, i - 30):i]):
                if not anterior.strip():
                    continue
                r_ant = len(anterior) - len(anterior.lstrip())
                if r_ant < recuo and 'atomic(' in anterior:
                    protegido = True
                    break
                if r_ant < recuo and anterior.lstrip().startswith(('def ', 'class ')):
                    # Chegamos ao `def` que contém a linha sem achar `atomic()`.
                    # Uma saída, e SÓ uma: helper que PROVA a transação em
                    # tempo de execução (`in_atomic_block`). Aconteceu em
                    # 02/09/2026 — este scanner é léxico e extrair a chamada
                    # para um helper o cega; a resposta certa não foi ignorar o
                    # helper, foi o helper passar a garantir o que o scanner só
                    # supunha. Aceitar aqui sem exigir a guarda seria trocar
                    # uma régua por um comentário.
                    corpo = '\n'.join(linhas[max(0, i - 30):i + 5])
                    if 'in_atomic_block' in corpo and 'raise' in corpo:
                        protegido = True
                    break
            if not protegido:
                ofensores.append(
                    f'{caminho.relative_to(RAIZ)}:{i+1}: {linha.strip()}')
    assert not ofensores, (
        'SET LOCAL de teto fora de transaction.atomic() — silenciosamente '
        'inócuo, a pior das quatro variações:\n  ' + '\n  '.join(ofensores)
    )


def test_o_helper_de_teto_RECUSA_fora_de_transacao():
    """A guarda que o scanner passou a aceitar tem que existir de verdade.

    Sem este teste, a saída que ensinei ao scanner em 02/09/2026 vira um
    buraco: bastaria alguém escrever `in_atomic_block` e `raise` num comentário
    para o `SET LOCAL` passar. Aqui a guarda é EXERCITADA.

    Cursor de mentira de propósito: a guarda dispara ANTES de tocar no cursor,
    então o teste não precisa de banco — e um teste que precisa de banco para
    provar uma invariante de código é um teste que ninguém roda.
    """
    import pytest

    from tribunals.services import partes_djen as P

    class _CursorQueNaoDeveSerUsado:
        def execute(self, *a, **k):  # pragma: no cover
            raise AssertionError('a guarda deixou passar: o SET LOCAL rodou '
                                 'fora de transação')

    with pytest.raises(RuntimeError) as exc:
        P._cursor_com_teto(_CursorQueNaoDeveSerUsado(), 60)
    msg = str(exc.value)
    assert 'atomic' in msg, 'a mensagem tem que dizer o que fazer'
    assert 'INÓCUO' in msg.upper(), 'e por que isso importa'
