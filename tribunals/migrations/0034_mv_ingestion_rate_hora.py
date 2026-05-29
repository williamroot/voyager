from django.db import migrations

# Codifica a MV mv_ingestion_rate_hora (até então criada à mão em prod, fora
# de migration) e encolhe a janela de 7d -> 4d. O gráfico "Velocidade de
# ingestão" (overview) usa no máximo 72h; 4d cobre com folga e torna o REFRESH
# ~2x mais barato, dando margem ao statement_timeout. Refresh dedicado e
# frequente vive em dashboard.tasks.refresh_ingestion_rate_hora (~30min).
#
# WITH NO DATA: a MV fica vazia após o migrate; o 1º REFRESH popula (modo
# não-concorrente, detectado por relispopulated no job). DROP+CREATE porque a
# versão antiga (7d) já existe em prod e CREATE ... IF NOT EXISTS não a troca.

CREATE = """
DROP MATERIALIZED VIEW IF EXISTS mv_ingestion_rate_hora;
CREATE MATERIALIZED VIEW mv_ingestion_rate_hora AS
  SELECT date_trunc('hour', inserido_em) AS hora,
         tribunal_id,
         count(*) AS total
  FROM tribunals_movimentacao
  WHERE inserido_em >= now() - '4 days'::interval
  GROUP BY date_trunc('hour', inserido_em), tribunal_id
WITH NO DATA;
CREATE UNIQUE INDEX mv_ingestion_rate_hora_uidx
  ON mv_ingestion_rate_hora (hora, tribunal_id);
"""

# Reverte pra definição antiga (janela de 7d).
REVERSE = """
DROP MATERIALIZED VIEW IF EXISTS mv_ingestion_rate_hora;
CREATE MATERIALIZED VIEW mv_ingestion_rate_hora AS
  SELECT date_trunc('hour', inserido_em) AS hora,
         tribunal_id,
         count(*) AS total
  FROM tribunals_movimentacao
  WHERE inserido_em >= now() - '7 days'::interval
  GROUP BY date_trunc('hour', inserido_em), tribunal_id
WITH NO DATA;
CREATE UNIQUE INDEX mv_ingestion_rate_hora_uidx
  ON mv_ingestion_rate_hora (hora, tribunal_id);
"""


class Migration(migrations.Migration):

    atomic = False

    dependencies = [('tribunals', '0033_seed_tjdft')]

    operations = [
        migrations.RunSQL(CREATE, REVERSE),
    ]
