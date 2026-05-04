"""Índices compostos em Process para cobrir ORDER BY id DESC LIMIT 50 com filtro.

Cobrem as queries da listagem /dashboard/processos/:
  - tribunal_id filtro:        (tribunal_id, id DESC)
  - enriquecimento_status:     (enriquecimento_status, id DESC)

Sem esses índices, o planner faz seq scan + sort (bitmap heap) em 500k+ rows
quando o resultado esperado é 50 rows e o filtro não é muito seletivo.

**Em produção com tabela populada** — criar manualmente antes de aplicar a migration:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_tribunal_id_idx
        ON tribunals_process (tribunal_id, id DESC);

    CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_enriq_id_idx
        ON tribunals_process (enriquecimento_status, id DESC);

    INSERT INTO django_migrations (app, name, applied)
    VALUES ('tribunals', '0021_process_list_indexes', NOW());
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tribunals', '0020_classificacao_leads_api'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['tribunal', '-id'], name='proc_tribunal_id_idx'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['enriquecimento_status', '-id'], name='proc_enriq_id_idx'),
        ),
    ]
