"""Índices que tiram ~1,08 PB de leitura de disco do banco.

MEDIDO em 18/08/2026 via `pg_stat_statements`, com o banco (1,7 TB) rodando com
~22 GB de cache — ou seja, working set 14× maior que a RAM, cache hit de 82% no
heap e 41 de 44 queries esperando I/O. Sem RAM nova pra comprar, o caminho é
encolher o que precisa caber nela.

Os três maiores consumidores de disco do banco eram:

    436.191 GB / 6.476 chamadas   fila do reclassificador  (67 GB por chamada!)
    393.605 GB / 33.420 chamadas  contador do dashboard
    247.732 GB / 15.735 chamadas  gráfico diário do dashboard

**Por que varriam tudo.** A do reclassificador compara DUAS colunas
(`classificacao_em < ultima_movimentacao_em`), e nenhum B-tree comum cobre isso
— então ela lia os 108 GB de `tribunals_process` e ordenava. As duas do
dashboard filtram por `classificacao` E `criada_em`, mas o log só tinha índice
de `criada_em` sozinho.

**Depois (EXPLAIN ANALYZE em produção):**

    fila do reclassificador ... 3,8 MB e 144 ms   (era 67 GB)
    contador do dashboard ..... 32 KB e 1,7 ms
    gráfico diário ............ 56 KB e 0,07 ms

E o índice parcial ocupa **56 MB** — ele guarda só as linhas pendentes, que são
uma fração minúscula da tabela.

⚠️ `CONCURRENTLY` + `atomic = False` são obrigatórios: `tribunals_process` e
`tribunals_movimentacao` são tabelas quentes, e a versão bloqueante já derrubou
o site por 50 minutos (ver .ia/OPS.md). Os índices já foram criados à mão em
produção com o reclassificador pausado; `IF NOT EXISTS` faz esta migration ser
no-op lá e valer para qualquer ambiente novo.
"""
from django.db import migrations

CRIAR = [
    # a condição de um índice PARCIAL aceita comparação entre colunas da mesma
    # linha — é isso que resolve o `coluna_A < coluna_B` que o B-tree não indexa
    """CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_reclassificar_idx
           ON tribunals_process (ultima_movimentacao_em DESC)
        WHERE ultima_movimentacao_em IS NOT NULL
          AND (classificacao_em IS NULL OR classificacao_em < ultima_movimentacao_em)""",
    # igualdade primeiro, faixa depois
    """CREATE INDEX CONCURRENTLY IF NOT EXISTS classiflog_classif_criada_idx
           ON tribunals_classificacaolog (classificacao, criada_em DESC)""",
]

# Índices com ZERO scans na vida inteira do banco, conferidos em
# `pg_stat_user_indexes` no momento do drop. Cada um era pago em TODO insert e
# lido por ninguém — 19 GB somados, em tabelas onde o gargalo é I/O.
DROPAR = [
    'mov_classe_id_idx',          # 12 GB
    'proc_numero_cnj_idx_ccnew',  # 5,4 GB — sobra de um REINDEX interrompido
    'proc_enriq_status_idx',      # 1,6 GB
    'mov_data_disp_brin',         # 101 MB
]


class Migration(migrations.Migration):
    atomic = False        # CONCURRENTLY não roda dentro de transação

    dependencies = [('tribunals', '0049_stj_horizonte_djen')]

    operations = (
        [migrations.RunSQL(sql, migrations.RunSQL.noop) for sql in CRIAR]
        + [migrations.RunSQL(f'DROP INDEX CONCURRENTLY IF EXISTS {n}', migrations.RunSQL.noop)
           for n in DROPAR]
    )
