"""IngestionRun ganha `fonte` — qual PORTA trouxe as movimentações do run.

Até 08/2026 só existia o DJEN, então `default='djen'` deixa toda a história
correta sem backfill. A partir das fontes de diário próprio (DJE/TJSP, DEJT,
STF — ver `diarios/base.py`), o mesmo tribunal passa a ter runs de origens
diferentes; sem este campo, um run do diário próprio na mesma janela faria o
`_dia_coberto` do DJEN pular o dia como já coberto (perda silenciosa).

O índice é criado CONCURRENTLY (`atomic = False`) de propósito: a
`tribunals_ingestionrun` recebe escrita o tempo todo (cada janela de ingestão
faz vários UPDATE incrementais), e um CREATE INDEX comum a trava. É a mesma
lição do incidente de DROP INDEX não-concorrente em tabela quente.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('tribunals', '0047_tribunal_datajud_varredura_cursor_and_more'),
    ]

    operations = [
        # ADD COLUMN com default constante é metadata-only no PG 11+ (não
        # reescreve a tabela); o lock é breve, mas ainda é ACCESS EXCLUSIVE —
        # rodar fora do pico.
        migrations.AddField(
            model_name='ingestionrun',
            name='fonte',
            field=models.CharField(db_index=True, default='djen', max_length=16),
        ),
        AddIndexConcurrently(
            model_name='ingestionrun',
            index=models.Index(fields=['fonte', 'tribunal', 'janela_inicio'],
                               name='tribunals_i_fonte_2ac386_idx'),
        ),
    ]
