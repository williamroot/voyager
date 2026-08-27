"""Índice em `Process.atualizado_em` — o que fazia o `sync_es` DIVERGIR.

O tique incremental Postgres→ES varre `atualizado_em > watermark` a cada 10 min.
Não havia índice: `EXPLAIN` em produção devolvia `Parallel Seq Scan` + `Sort`
sobre 102 M linhas, custo 3,7 M, com 14,3 M linhas casando o filtro. Com teto de
10.000 por tique, a watermark andava **45 s de relógio a cada 600 s** — perdia
13:1 e a idade só crescia (127,92 → 128,08 → 128,26 h em três tiques seguidos).
Toda escrita em lote posterior a 19/08 estava fora da busca.

`CONCURRENTLY` porque `tribunals_process` é tabela quente: o `ALTER` normal toma
`ACCESS EXCLUSIVE` e vira auto-jam (ver `.ia/OPS.md`). `atomic = False` porque
`CREATE INDEX CONCURRENTLY` não roda dentro de transação.

`IF NOT EXISTS`: o índice foi criado à mão em produção em 27/08/2026, antes desta
migration existir. Aqui ela é no-op — e é assim que tem que ser, para o ESTADO
do Django parar de divergir do banco (a armadilha do índice declarado-e-ausente).
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [('tribunals', '0052_campos_da_auditoria')]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name='process',
                    index=models.Index(fields=['atualizado_em'],
                                       name='proc_atualizado_em_idx'),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql='CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_atualizado_em_idx '
                        'ON tribunals_process (atualizado_em);',
                    reverse_sql='DROP INDEX CONCURRENTLY IF EXISTS proc_atualizado_em_idx;',
                ),
            ],
        ),
    ]
