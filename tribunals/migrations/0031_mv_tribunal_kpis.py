from django.db import migrations

CREATE = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_tribunal_kpis AS
WITH p AS (
  SELECT tribunal_id, COUNT(*)::bigint AS total_processos
  FROM tribunals_process GROUP BY 1
),
m AS (
  SELECT
    tribunal_id,
    COUNT(*)::bigint AS total_movs,
    (COUNT(*) FILTER (WHERE ativo = false))::bigint AS cancelados,
    (COUNT(DISTINCT nome_orgao) FILTER (WHERE nome_orgao <> ''))::bigint AS orgaos_unicos,
    (COUNT(DISTINCT nome_classe) FILTER (WHERE nome_classe <> ''))::bigint AS classes_unicas
  FROM tribunals_movimentacao GROUP BY 1
)
SELECT
  COALESCE(p.tribunal_id, m.tribunal_id) AS sigla,
  COALESCE(p.total_processos, 0)::bigint AS total_processos,
  COALESCE(m.total_movs, 0)::bigint        AS total_movs,
  COALESCE(m.cancelados, 0)::bigint        AS cancelados,
  COALESCE(m.orgaos_unicos, 0)::bigint     AS orgaos_unicos,
  COALESCE(m.classes_unicas, 0)::bigint    AS classes_unicas
FROM p FULL OUTER JOIN m ON p.tribunal_id = m.tribunal_id
WITH NO DATA;
CREATE UNIQUE INDEX IF NOT EXISTS mv_tribunal_kpis_sigla_uniq
  ON mv_tribunal_kpis (sigla);
"""
DROP = "DROP MATERIALIZED VIEW IF EXISTS mv_tribunal_kpis;"


class Migration(migrations.Migration):

    atomic = False

    dependencies = [('tribunals', '0030_recriar_indices_unicos_parte')]

    operations = [
        migrations.RunSQL(CREATE, DROP),
    ]
