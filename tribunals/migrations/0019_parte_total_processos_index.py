"""Índice em `tribunals_parte (total_processos DESC, nome)`.

Cobre o sort default da listagem `/dashboard/partes/` (ordenação
`-total_processos, nome`). Sem ele, postgres faz parallel seq scan +
top-N heapsort em ~1M rows, custando ~2s só pra trazer LIMIT 50.

**Histórico**: tentativa anterior usava `SeparateDatabaseAndState` +
`RunSQL CREATE INDEX CONCURRENTLY` pra evitar bloquear escrita em prod.
Por motivo não totalmente diagnosticado, em prod a migration foi
marcada como aplicada **sem criar o índice** — tive que rodar o SQL
manualmente. Pra evitar repetir o silent-fail em dev/restores futuros,
voltamos pro `AddIndex` padrão do Django: bloqueia escrita brevemente,
mas em DBs novos a tabela está vazia (instantâneo) e o prod já tem o
índice + 0019 marcado como aplicado (no-op).

Se um dia reaplicar em outro DB populado, criar manualmente antes:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS parte_total_procs_nome_idx
    ON tribunals_parte (total_processos DESC, nome);
    INSERT INTO django_migrations (app, name, applied)
    VALUES ('tribunals', '0019_parte_total_processos_index', NOW());
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tribunals', '0018_data_enriquecimento_per_source'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='parte',
            index=models.Index(
                fields=['-total_processos', 'nome'],
                name='parte_total_procs_nome_idx',
            ),
        ),
    ]
