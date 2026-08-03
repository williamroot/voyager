import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0002_chatfile'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ShowcaseAnalise',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('arquivo', models.CharField(max_length=255)),
                ('content_type', models.CharField(blank=True, default='', max_length=120)),
                ('tamanho_bytes', models.BigIntegerField(default=0)),
                ('sha256', models.CharField(blank=True, default='', max_length=64)),
                ('versao', models.CharField(blank=True, default='', max_length=20)),
                ('modelo_label', models.CharField(blank=True, default='', max_length=120)),
                ('elapsed_ms', models.IntegerField(default=0)),
                ('tempos', models.JSONField(blank=True, default=dict)),
                ('n_partes', models.IntegerField(default=0)),
                ('n_docs', models.IntegerField(default=0)),
                ('paginas', models.IntegerField(default=0)),
                ('resultado', models.JSONField(default=dict)),
                ('upload_id', models.CharField(blank=True, default='', max_length=64)),
                ('usuario', models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='showcase_analises', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-criado_em']},
        ),
        migrations.AddIndex(
            model_name='showcaseanalise',
            index=models.Index(fields=['-criado_em'], name='showanalise_criado_idx'),
        ),
        migrations.AddIndex(
            model_name='showcaseanalise',
            index=models.Index(fields=['usuario', '-criado_em'], name='showanalise_user_criado_idx'),
        ),
    ]
