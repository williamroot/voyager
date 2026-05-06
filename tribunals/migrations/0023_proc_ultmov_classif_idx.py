from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False  # CREATE INDEX CONCURRENTLY — não bloqueia leituras/escritas

    dependencies = [
        ('tribunals', '0022_index_renames_autofield_noop'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='process',
            index=models.Index(
                fields=['ultima_movimentacao_em', 'classificacao_em'],
                name='proc_ultmov_classif_idx',
            ),
        ),
    ]
