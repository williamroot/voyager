"""Trigger Postgres que mantém Process.total_movimentacoes / primeira_/ultima_movimentacao_em
em sincronia automaticamente após INSERT em Movimentacao.

Statement-level (1 UPDATE por bulk_create) usando REFERENCING NEW TABLE — Postgres 10+.
Reduz custo a 1 UPDATE batch por operação (vs 1 UPDATE por linha em row-level).
"""
from django.db import migrations


SQL_FORWARD = """
CREATE OR REPLACE FUNCTION update_process_aggregates_stmt()
RETURNS trigger AS $$
BEGIN
    UPDATE tribunals_process p SET
        total_movimentacoes = s.cnt,
        primeira_movimentacao_em = s.minimo,
        ultima_movimentacao_em = s.maximo
    FROM (
        SELECT processo_id,
               COUNT(*) AS cnt,
               MIN(data_disponibilizacao) AS minimo,
               MAX(data_disponibilizacao) AS maximo
        FROM tribunals_movimentacao
        WHERE processo_id IN (SELECT DISTINCT processo_id FROM new_table)
        GROUP BY processo_id
    ) s
    WHERE p.id = s.processo_id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mov_update_process_agg ON tribunals_movimentacao;

CREATE TRIGGER mov_update_process_agg
AFTER INSERT ON tribunals_movimentacao
REFERENCING NEW TABLE AS new_table
FOR EACH STATEMENT EXECUTE FUNCTION update_process_aggregates_stmt();
"""

SQL_REVERSE = """
DROP TRIGGER IF EXISTS mov_update_process_agg ON tribunals_movimentacao;
DROP FUNCTION IF EXISTS update_process_aggregates_stmt();
"""


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0003_movimentacao_campos_djen')]

    operations = [migrations.RunSQL(sql=SQL_FORWARD, reverse_sql=SQL_REVERSE)]
