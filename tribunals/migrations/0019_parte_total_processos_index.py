"""Índice em `tribunals_parte (total_processos DESC, nome)`.

Cobre o sort default da listagem `/dashboard/partes/` (ordenação
`-total_processos, nome`). Sem ele, postgres faz parallel seq scan +
top-N heapsort em ~1M rows, custando ~2s só pra trazer LIMIT 50.

Usa `SeparateDatabaseAndState` pra rodar `CREATE INDEX CONCURRENTLY`
no DB (não bloqueia escrita) enquanto registra `AddIndex` só no
state do Django. `IF NOT EXISTS` mantém idempotência em DBs já
corrigidos por hotfix manual.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('tribunals', '0018_data_enriquecimento_per_source'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS parte_total_procs_nome_idx "
                        "ON tribunals_parte (total_processos DESC, nome);"
                    ),
                    reverse_sql=(
                        "DROP INDEX CONCURRENTLY IF EXISTS parte_total_procs_nome_idx;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name='parte',
                    index=models.Index(
                        fields=['-total_processos', 'nome'],
                        name='parte_total_procs_nome_idx',
                    ),
                ),
            ],
        ),
    ]
