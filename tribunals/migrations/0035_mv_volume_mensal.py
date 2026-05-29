from django.db import migrations

# MV de volume mensal por tribunal, todo histórico desde 2020 (floor DJEN).
# Serve dois consumidores que faziam o MESMO TruncMonth ao vivo em ~614M rows
# (custo de plano ~48M, varreduras de ~400s+ a cada ciclo de warm):
#   - volume_temporal(None / >365d)  -> gráfico de volume da overview
#   - compute_tribunal_status (volume_por_trib) -> /dashboard/tribunais/status/
# `mes <= now()` (no SELECT) + floor 2020 descartam datas-lixo do Datajud
# (ano 2400, etc). Refresh diário via refresh_materialized_views.
#
# WITH NO DATA: vazia após migrate; 1º refresh é não-concorrente (CONCURRENTLY
# exige MV populada). Os readers caem pra agregação live enquanto não-populada.

CREATE = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_volume_mensal AS
  SELECT date_trunc('month', data_disponibilizacao)::date AS mes,
         tribunal_id,
         count(*) AS total
  FROM tribunals_movimentacao
  WHERE data_disponibilizacao >= '2020-01-01'::timestamptz
    AND data_disponibilizacao <= now()
  GROUP BY date_trunc('month', data_disponibilizacao)::date, tribunal_id
WITH NO DATA;
CREATE UNIQUE INDEX IF NOT EXISTS mv_volume_mensal_uidx
  ON mv_volume_mensal (mes, tribunal_id);
"""
DROP = "DROP MATERIALIZED VIEW IF EXISTS mv_volume_mensal;"


class Migration(migrations.Migration):

    atomic = False

    dependencies = [('tribunals', '0034_mv_ingestion_rate_hora')]

    operations = [
        migrations.RunSQL(CREATE, DROP),
    ]
