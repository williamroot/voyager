"""Adiciona estrutura de classificação de leads + integração API.

Não-bloqueante:
- Process.classificacao* são nullable (sem backfill); workers populam após enriquecimento.
- Index CREATE INDEX CONCURRENTLY (atomic=False).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('tribunals', '0019_parte_total_processos_index'),
    ]

    operations = [
        # Process: 4 campos nullable (não trava table com 2.4M rows)
        migrations.AddField(
            model_name='process',
            name='classificacao',
            field=models.CharField(
                choices=[
                    ('PRECATORIO', 'Precatório'),
                    ('PRE_PRECATORIO', 'Pré-precatório'),
                    ('DIREITO_CREDITORIO', 'Direito creditório'),
                    ('NAO_LEAD', 'Não-lead'),
                ],
                max_length=20, null=True, blank=True, db_index=True,
            ),
        ),
        migrations.AddField(
            model_name='process',
            name='classificacao_score',
            field=models.FloatField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='process',
            name='classificacao_versao',
            field=models.CharField(max_length=10, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='process',
            name='classificacao_em',
            field=models.DateTimeField(null=True, blank=True, db_index=True),
        ),
        # Tabelas novas
        migrations.CreateModel(
            name='ClassificadorVersao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('versao', models.CharField(max_length=10, unique=True)),
                ('pesos', models.JSONField()),
                ('metricas', models.JSONField(default=dict)),
                ('ativa', models.BooleanField(default=False, db_index=True)),
                ('criada_em', models.DateTimeField(auto_now_add=True)),
                ('notas', models.TextField(blank=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name='classificadorversao',
            constraint=models.UniqueConstraint(
                fields=['ativa'],
                condition=models.Q(ativa=True),
                name='uniq_classificador_versao_ativa',
            ),
        ),
        migrations.CreateModel(
            name='ClassificacaoLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('classificacao', models.CharField(max_length=20)),
                ('score', models.FloatField()),
                ('versao', models.CharField(max_length=10)),
                ('features_snapshot', models.JSONField(default=dict)),
                ('criada_em', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('processo', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='classif_logs', to='tribunals.process',
                )),
            ],
        ),
        migrations.AddIndex(
            model_name='classificacaolog',
            index=models.Index(fields=['processo', '-criada_em'], name='classif_log_proc_dt_idx'),
        ),
        migrations.CreateModel(
            name='ApiClient',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('nome', models.CharField(max_length=64, unique=True)),
                ('api_key', models.CharField(max_length=64, unique=True, db_index=True)),
                ('ativo', models.BooleanField(default=True)),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('notas', models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name='LeadConsumption',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('consumido_em', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('resultado', models.CharField(
                    max_length=20, default='pendente', db_index=True,
                    choices=[
                        ('validado', 'Validado'),
                        ('sem_expedicao', 'Sem expedição'),
                        ('erro', 'Erro'),
                        ('pendente', 'Pendente'),
                        ('pago', 'Pago'),
                        ('arquivado', 'Arquivado'),
                        ('cedido', 'Cedido'),
                    ],
                )),
                ('cliente', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='consumos', to='tribunals.apiclient',
                )),
                ('processo', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='consumos', to='tribunals.process',
                )),
            ],
        ),
        migrations.AddIndex(
            model_name='leadconsumption',
            index=models.Index(fields=['cliente', '-consumido_em'], name='leadcons_cli_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='leadconsumption',
            index=models.Index(fields=['cliente', 'processo'], name='leadcons_cli_proc_idx'),
        ),
    ]
