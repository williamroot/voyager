from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0002_seed_tribunais')]

    operations = [
        migrations.AddField('movimentacao', 'id_orgao', models.IntegerField(blank=True, null=True)),
        migrations.AddField('movimentacao', 'destinatario_advogados', models.JSONField(default=list)),
        migrations.AddField('movimentacao', 'numero_comunicacao', models.CharField(blank=True, max_length=120)),
        migrations.AddField('movimentacao', 'hash', models.CharField(blank=True, max_length=128)),
        migrations.AddField('movimentacao', 'meio', models.CharField(blank=True, max_length=20)),
        migrations.AddField('movimentacao', 'meio_completo', models.CharField(blank=True, max_length=120)),
        migrations.AddField('movimentacao', 'status', models.CharField(blank=True, max_length=40)),
        migrations.AddField('movimentacao', 'ativo', models.BooleanField(default=True)),
        migrations.AddField('movimentacao', 'data_cancelamento', models.DateTimeField(blank=True, null=True)),
        migrations.AddField('movimentacao', 'motivo_cancelamento', models.TextField(blank=True)),
        migrations.AddIndex(
            model_name='movimentacao',
            index=models.Index(fields=['tribunal', 'ativo'], name='mov_trib_ativo_idx'),
        ),
        migrations.AddIndex(
            model_name='movimentacao',
            index=models.Index(fields=['hash'], name='mov_hash_idx'),
        ),
    ]
