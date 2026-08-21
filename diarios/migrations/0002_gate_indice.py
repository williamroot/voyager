"""Gate de índice no `EdicaoDiario`: "coletada" deixa de ser sinônimo de "buscável".

Quatro colunas nulas numa tabela de 8 linhas em produção (21/08/2026) —
`AddField` puro, sem default a preencher, sem reescrita de heap. Não precisa de
`CONCURRENTLY` nem de `atomic = False`: a tabela é o catálogo de edições de
diário, não `tribunals_movimentacao`.

Por que as colunas existem: os 8 cadernos do DJE/TJSP de 12/03/2025 fecharam
`status=ok` com 220.544 linhas gravadas enquanto 27.619 delas ainda estavam
fora do Elasticsearch. Nada no banco sabia disso. Ver `diarios/indice.py`.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('diarios', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='edicaodiario',
            name='indice_conferido_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='edicaodiario',
            name='indice_no_es_no_dia',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='edicaodiario',
            name='indice_faltando_no_dia',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='edicaodiario',
            name='indice_reenfileiradas',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
