"""ano_cnj derivado de numero_cnj — formato CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO.
Trigger SQL mantém sincronizado em INSERT/UPDATE; backfill em massa via UPDATE.
"""
from django.db import migrations, models


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

UPDATE tribunals_process
SET ano_cnj = substring(numero_cnj from 12 for 4)::smallint
WHERE numero_cnj ~ '^\\d{7}-\\d{2}\\.\\d{4}\\.';
"""

SQL_REVERSE = """
DROP TRIGGER IF EXISTS process_set_ano_cnj ON tribunals_process;
DROP FUNCTION IF EXISTS set_process_ano_cnj();
"""


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0007_process_enriquecimento_status')]

    operations = [
        migrations.AddField(
            model_name='process',
            name='ano_cnj',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['ano_cnj'], name='proc_ano_cnj_idx'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['tribunal', 'ano_cnj'], name='proc_trib_ano_idx'),
        ),
        migrations.RunSQL(sql=SQL_FORWARD, reverse_sql=SQL_REVERSE),
    ]
