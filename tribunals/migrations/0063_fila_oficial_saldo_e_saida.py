"""Fila oficial: saldo (valor pago), preferência e a anotação de saída.

`tribunals_precatoriofilaoficial` é tabela nova e pequena (dezenas de milhares
de linhas), sem FK apontando para ela: o `ADD COLUMN` com `DEFAULT` constante é
só catálogo no Postgres 11+ e o lock é da própria tabela, por instantes.
`IF NOT EXISTS` para reaplicar sem erro; `DEFAULT` em toda coluna de texto,
regra da casa (ver 0057/0058/0062).
"""
from django.db import migrations, models

TABELA = 'tribunals_precatoriofilaoficial'

SQL = f"""
ALTER TABLE "{TABELA}"
    ADD COLUMN IF NOT EXISTS "valor_pago"   numeric(18, 2) NULL,
    ADD COLUMN IF NOT EXISTS "preferencia"  varchar(32)  NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "saiu_em"      timestamp with time zone NULL,
    ADD COLUMN IF NOT EXISTS "motivo_saida" varchar(500) NOT NULL DEFAULT '';
"""
REVERSO = f"""
ALTER TABLE "{TABELA}"
    DROP COLUMN IF EXISTS "valor_pago",
    DROP COLUMN IF EXISTS "preferencia",
    DROP COLUMN IF EXISTS "saiu_em",
    DROP COLUMN IF EXISTS "motivo_saida";
"""


class Migration(migrations.Migration):
    dependencies = [
        ('tribunals', '0062_fila_oficial_precatorios'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='precatoriofilaoficial', name='valor_pago',
                    field=models.DecimalField(blank=True, decimal_places=2, max_digits=18,
                                              null=True)),
                migrations.AddField(
                    model_name='precatoriofilaoficial', name='preferencia',
                    field=models.CharField(blank=True, max_length=32)),
                migrations.AddField(
                    model_name='precatoriofilaoficial', name='saiu_em',
                    field=models.DateTimeField(blank=True, null=True)),
                migrations.AddField(
                    model_name='precatoriofilaoficial', name='motivo_saida',
                    field=models.CharField(blank=True, max_length=500)),
            ],
            database_operations=[migrations.RunSQL(SQL, REVERSO)],
        ),
    ]
