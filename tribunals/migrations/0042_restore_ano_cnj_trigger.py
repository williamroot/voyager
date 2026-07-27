"""Recria o trigger que mantém Process.ano_cnj derivado de numero_cnj.

O trigger `process_set_ano_cnj` foi criado na migration 0008 mas sumiu do
prod em algum momento (provavelmente num pg_restore --clean / rewrite de
tabela — triggers não são recriados por migrations Django subsequentes).
Consequência: todo Process inserido depois disso nasceu com ano_cnj NULL
(TJSP 100% NULL, ~65M linhas no total). Esta migration recria o trigger.

IMPORTANTE: NÃO faz o UPDATE de backfill em massa aqui (como a 0008 fazia) —
seriam 65M linhas sob carga pesada de prod, o que reescreveria a tabela e
travaria. O backfill histórico é feito à parte, em lotes gentis por ctid.
Esta migration só (re)estabelece a função + trigger — operações leves
(sem lock pesado, sem rewrite).
"""
from django.db import migrations


SQL_FORWARD = """
CREATE OR REPLACE FUNCTION set_process_ano_cnj() RETURNS trigger AS $$
BEGIN
    IF NEW.numero_cnj ~ '^\\d{7}-\\d{2}\\.\\d{4}\\.' THEN
        NEW.ano_cnj := substring(NEW.numero_cnj from 12 for 4)::smallint;
    ELSE
        NEW.ano_cnj := NULL;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS process_set_ano_cnj ON tribunals_process;
CREATE TRIGGER process_set_ano_cnj
BEFORE INSERT OR UPDATE OF numero_cnj ON tribunals_process
FOR EACH ROW EXECUTE FUNCTION set_process_ano_cnj();
"""

SQL_REVERSE = """
DROP TRIGGER IF EXISTS process_set_ano_cnj ON tribunals_process;
"""


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0041_seed_superiores')]

    operations = [
        migrations.RunSQL(sql=SQL_FORWARD, reverse_sql=SQL_REVERSE),
    ]
