from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0042_restore_ano_cnj_trigger'),
    ]

    operations = [
        migrations.CreateModel(
            name='FonteDiario',
            fields=[
                ('source_id', models.IntegerField(primary_key=True, serialize=False)),
                ('diario_slug', models.CharField(max_length=64)),
                ('orgao_slug', models.CharField(max_length=64)),
                ('caderno_slug', models.CharField(blank=True, max_length=64)),
                ('nome', models.CharField(max_length=200)),
                ('tribunal', models.OneToOneField(
                    on_delete=models.deletion.CASCADE,
                    related_name='fonte_diario',
                    to='tribunals.tribunal',
                )),
            ],
            options={'ordering': ['source_id']},
        ),
        migrations.AddField(
            model_name='movimentacao',
            name='assunto_norm',
            field=models.JSONField(blank=True, default=list),
        ),
    ]