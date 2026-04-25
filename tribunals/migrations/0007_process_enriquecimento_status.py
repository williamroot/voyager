from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0006_processoparte_constraint_partial')]

    operations = [
        migrations.AddField(
            model_name='process',
            name='enriquecimento_status',
            field=models.CharField(
                choices=[
                    ('pendente', 'Pendente'),
                    ('ok', 'Enriquecido'),
                    ('nao_encontrado', 'Não encontrado'),
                    ('erro', 'Erro'),
                ],
                default='pendente',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='process',
            name='enriquecimento_erro',
            field=models.TextField(blank=True),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['enriquecimento_status'], name='proc_enriq_status_idx'),
        ),
        # Marca os já enriquecidos com sucesso
        migrations.RunSQL(
            sql="UPDATE tribunals_process SET enriquecimento_status='ok' WHERE enriquecido_em IS NOT NULL;",
            reverse_sql="",
        ),
    ]
