"""`tribunals_buscatribunalrun` — o estado de uma busca por parte ao vivo.

Tabela NOVA, e a única FK sai para `tribunals_apiclient`, que tem unidades de
linhas. É por isso que aqui um `CreateModel` seco é seguro, ao contrário da
0058 e da 0059: lá a FK apontava para `tribunals_process` (~104 milhões de
linhas, sob escrita contínua), e `ADD CONSTRAINT ... FOREIGN KEY` pede
`SHARE ROW EXCLUSIVE` na tabela REFERENCIADA — nesta casa, um lock desses
enfileira produção inteira.

Sem `Process` no meio: o resultado da busca vive em JSON dentro do run, e o
que vira acervo vira `Process` pelo caminho normal da hidratação.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0059_magistrado'),
    ]

    operations = [
        migrations.CreateModel(
            name='BuscaTribunalRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('criterio', models.CharField(max_length=16)),
                ('valor', models.CharField(max_length=255)),
                ('valor_normalizado', models.CharField(db_index=True, max_length=255)),
                ('tribunais', models.JSONField(default=list)),
                ('status', models.CharField(
                    choices=[('running', 'Em execução'), ('concluido', 'Concluído'),
                             ('erro', 'Erro')],
                    db_index=True, default='running', max_length=16)),
                ('por_tribunal', models.JSONField(default=dict)),
                ('resultados', models.JSONField(default=list)),
                ('encontrados', models.PositiveIntegerField(default=0)),
                ('novos_no_acervo', models.PositiveIntegerField(default=0)),
                ('erros', models.JSONField(default=list)),
                ('criado_em', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('finalizado_em', models.DateTimeField(blank=True, null=True)),
                ('api_client', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='buscas_tribunal', to='tribunals.apiclient')),
            ],
            options={
                'ordering': ['-criado_em'],
                'indexes': [
                    models.Index(fields=['criterio', 'valor_normalizado', '-criado_em'],
                                 name='busca_run_cache_idx'),
                ],
            },
        ),
    ]
