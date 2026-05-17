from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('tribunals', '0027_can_view_motivo'),
    ]

    operations = [
        migrations.AddField(
            model_name='leadconsumption',
            name='lote_id',
            field=models.UUIDField(
                null=True, blank=True,
                help_text='UUID do lote de reporte (idempotência). NULL = legado.'),
        ),
        AddIndexConcurrently(
            'leadconsumption',
            models.Index(fields=['lote_id'], name='trib_lc_lote_id_idx'),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "CREATE UNIQUE INDEX CONCURRENTLY uniq_consumo_cliente_proc_lote "
                    "ON tribunals_leadconsumption (cliente_id, processo_id, lote_id) "
                    "WHERE lote_id IS NOT NULL;",
                    "DROP INDEX IF EXISTS uniq_consumo_cliente_proc_lote;",
                ),
            ],
            state_operations=[
                migrations.AddConstraint(
                    model_name='leadconsumption',
                    constraint=models.UniqueConstraint(
                        fields=['cliente', 'processo', 'lote_id'],
                        name='uniq_consumo_cliente_proc_lote',
                        condition=models.Q(lote_id__isnull=False),
                    ),
                ),
            ],
        ),
    ]
