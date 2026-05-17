from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

CREATE = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_pipeline_diario AS
  SELECT tribunal_id, data_enriquecimento_datajud::date AS dia,
         'datajud'::text AS fonte, COUNT(*)::int AS processos
    FROM tribunals_process
   WHERE data_enriquecimento_datajud IS NOT NULL GROUP BY 1,2
  UNION ALL
  SELECT tribunal_id, enriquecido_em::date, 'pje', COUNT(*)::int
    FROM tribunals_process WHERE enriquecido_em IS NOT NULL GROUP BY 1,2
  UNION ALL
  SELECT tribunal_id, classificacao_em::date, 'classif', COUNT(*)::int
    FROM tribunals_process WHERE classificacao_em IS NOT NULL GROUP BY 1,2;
CREATE UNIQUE INDEX IF NOT EXISTS mv_pipeline_diario_uniq
  ON mv_pipeline_diario (tribunal_id, dia, fonte);
"""
DROP = "DROP MATERIALIZED VIEW IF EXISTS mv_pipeline_diario;"


class Migration(migrations.Migration):

    atomic = False

    dependencies = [('tribunals', '0028_leadconsumption_lote_id')]

    operations = [
        migrations.RunSQL(CREATE, DROP),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                AddIndexConcurrently(
                    'process',
                    models.Index(
                        fields=['data_enriquecimento_datajud'],
                        name='proc_datajud_em_idx',
                    ),
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name='process',
                    index=models.Index(
                        fields=['data_enriquecimento_datajud'],
                        name='proc_datajud_em_idx',
                    ),
                ),
            ],
        ),
    ]
