"""O que a migration 0059 DEIXOU NO BANCO — conferido por COLUNA, nunca por nome.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
`proc_tribunal_id_idx` é declarado no model como `(tribunal, -id)` e existe no
banco como `(tribunal_id)` — uma coluna só. Mesmo nome, colunas diferentes:
`\\di`, `pg_indexes.indexname` e `makemigrations` respondem "ok", e o custo real
foi um `LIMIT 1` de **1.318 s** que enfileirou 63 sessões atrás de um
`ALTER TABLE` (`.ia/DATA_MODEL.md`). A migration 0051 declarou três índices que
o banco nunca teve.

A régua da casa é: **confira por COLUNA** (`pg_index` / `pg_attribute` /
`pg_constraint`), nunca pelo nome. É o que este arquivo faz com a 0059.

A régua é o BANCO DE TESTE, que o pytest constrói rodando as migrations — o que
se mede aqui é o que as migrations DECLARAM. Divergência entre as migrations e
a `.101` é outro problema e tem outra ferramenta (`manage.py auditar_schema`).
"""
import pytest
from django.db import connection

pytestmark = pytest.mark.django_db

T_MAG = 'tribunals_magistrado'
T_ATU = 'tribunals_magistradoatuacao'

SQL_INDICES = """
SELECT c.relname, array_agg(a.attname ORDER BY k.ord)
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE t.relname = %s
GROUP BY c.relname
"""

SQL_FKS = """
SELECT c.conname,
       (SELECT array_agg(att.attname ORDER BY x.ord)
          FROM unnest(c.conkey) WITH ORDINALITY AS x(attnum, ord)
          JOIN pg_attribute att ON att.attrelid = c.conrelid AND att.attnum = x.attnum),
       ref.relname,
       (SELECT array_agg(att.attname ORDER BY y.ord)
          FROM unnest(c.confkey) WITH ORDINALITY AS y(attnum, ord)
          JOIN pg_attribute att ON att.attrelid = c.confrelid AND att.attnum = y.attnum)
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_class ref ON ref.oid = c.confrelid
WHERE t.relname = %s AND c.contype = 'f'
"""

SQL_SEM_DEFAULT = """
SELECT a.attname
FROM pg_class cl
JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef ad ON ad.adrelid = cl.oid AND ad.adnum = a.attnum
WHERE cl.relname = %s AND a.attnotnull AND ad.adbin IS NULL
  AND format_type(a.atttypid, a.atttypmod) LIKE 'character varying%%'
"""


def _indices(tabela):
    with connection.cursor() as cur:
        cur.execute(SQL_INDICES, [tabela])
        return {nome: list(cols) for nome, cols in cur.fetchall()}


def _fks(tabela):
    with connection.cursor() as cur:
        cur.execute(SQL_FKS, [tabela])
        return {nome: (list(cols), ref, list(refcols))
                for nome, cols, ref, refcols in cur.fetchall()}


def _varchar_sem_default(tabela):
    with connection.cursor() as cur:
        cur.execute(SQL_SEM_DEFAULT, [tabela])
        return sorted(linha[0] for linha in cur.fetchall())


# --------------------------------------------------------------------------- #
def test_controle_positivo_as_duas_tabelas_existem():
    """Sonda que não enxerga a tabela passa medindo o vazio — e aprova tudo."""
    assert _indices(T_MAG), f'{T_MAG} não existe: os testes abaixo seriam vácuo'
    assert _indices(T_ATU), f'{T_ATU} não existe'


def test_a_identidade_e_tribunal_orgao_nome_e_esta_no_BANCO():
    """A unique de três colunas é o que impede a ficha de fundir homônimos —
    56 de 195 publicações com um mesmo nome são de outros estados."""
    cols = _indices(T_MAG).get('uniq_magistrado_tribunal_orgao_nome')
    assert cols == ['tribunal_id', 'orgao_chave', 'nome_chave']


def test_o_indice_da_pessoa_atraves_dos_orgaos_tem_as_DUAS_colunas():
    assert _indices(T_MAG).get('mag_trib_nome_idx') == ['tribunal_id', 'nome_chave']


def test_o_indice_de_homonimo_nacional_existe():
    assert _indices(T_MAG).get('mag_nome_idx') == ['nome_chave']


def test_a_atuacao_e_idempotente_por_publicacao():
    """Uma linha por (magistrado, publicação). É a unique que torna o backfill
    re-executável sem contador para manter."""
    cols = _indices(T_ATU).get('uniq_atuacao_magistrado_mov')
    assert cols == ['magistrado_id', 'movimentacao_id']


def test_os_indices_de_consulta_da_atuacao():
    idx = _indices(T_ATU)
    assert idx.get('atu_mag_data_idx') == ['magistrado_id', 'publicado_em']
    assert idx.get('atu_processo_idx') == ['processo_id']


# --------------------------------------------------------------------------- #
def test_a_fk_do_tribunal_aponta_para_SIGLA_e_nao_para_id():
    """`tribunals_tribunal` não tem coluna `id` — a PK é `sigla`. Escrever
    `REFERENCES ... ("id")` por hábito falha, e falhava DENTRO do laço de lock,
    gastando 40 tentativas num erro que nenhuma espera conserta."""
    cols, ref, refcols = _fks(T_MAG)['tribunals_magistrado_tribunal_id_fk']
    assert (cols, ref, refcols) == (['tribunal_id'], 'tribunals_tribunal', ['sigla'])


def test_as_duas_fks_da_atuacao_existem_com_as_colunas_certas():
    fks = _fks(T_ATU)
    assert fks['tribunals_magistradoatuacao_magistrado_id_fk'] == \
        (['magistrado_id'], T_MAG, ['id'])
    assert fks['tribunals_magistradoatuacao_processo_id_fk'] == \
        (['processo_id'], 'tribunals_process', ['id'])


def test_NAO_existe_fk_para_movimentacao_e_isso_e_deliberado():
    """`tribunals_movimentacao` tem ~1,4 bilhão de linhas e recebe INSERT 24 h
    por dia. `ADD CONSTRAINT ... FOREIGN KEY` pede `SHARE ROW EXCLUSIVE` na
    tabela REFERENCIADA, e nesta casa todo lock enfileira (63 sessões em
    25/08/2026). A unique `(magistrado_id, movimentacao_id)` já dá a
    idempotência; a FK só compraria o auto-jam.

    Este teste existe para que a ausência seja uma DECISÃO registrada e não
    pareça esquecimento para quem vier depois."""
    alvos = {ref for _, ref, _ in _fks(T_ATU).values()}
    assert 'tribunals_movimentacao' not in alvos


# --------------------------------------------------------------------------- #
#: `tribunal_id` é varchar porque a PK de `tribunals_tribunal` é a `sigla`.
#: Ela fica FORA da regra de propósito, pelo mesmo critério de
#: `tests/test_default_no_banco.py`: é CHAVE, e um INSERT que a omita **tem**
#: que explodir. `DEFAULT ''` ali trocaria um erro alto por uma linha órfã
#: apontando para o tribunal de sigla vazia.
CHAVES_SEM_DEFAULT = {'tribunal_id'}


def test_toda_coluna_de_texto_NOT_NULL_tem_DEFAULT_no_banco():
    """A regra que a 0052 custou 10.410 `IngestionRun` `failed` para aprender e
    que a 0054 repetiu seis dias depois. Aqui não há escritor antigo — e é
    exatamente por isso que a hora de cumprir é agora."""
    assert set(_varchar_sem_default(T_MAG)) - CHAVES_SEM_DEFAULT == set()
    assert set(_varchar_sem_default(T_ATU)) - CHAVES_SEM_DEFAULT == set()


def test_controle_a_sonda_de_DEFAULT_realmente_enxerga_coluna_sem_default():
    """Catraca que nunca reprovou não se sabe se trava: a chave `tribunal_id`
    TEM que aparecer na sonda crua, senão ela está medindo o vazio."""
    assert 'tribunal_id' in _varchar_sem_default(T_MAG)


def test_n_publicacoes_e_NULAVEL_porque_ausente_nao_e_zero():
    """`NOT NULL DEFAULT 0` faria toda linha nunca medida afirmar 'este
    magistrado não tem publicação' — afirmação que ninguém fez."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT a.attnotnull FROM pg_class c
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relname = %s AND a.attname = 'n_publicacoes'
        """, [T_MAG])
        (notnull,) = cur.fetchone()
    assert notnull is False
