"""Índice composto (tribunal, enriquecimento_status) pro pending-scan do reabastecer.

CONCURRENTLY (atomic=False) — a tabela é grande (~600M); criar sem lock. Só leitura
(otimização), não muda colunas → workers não precisam rebuild.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('tribunals', '0037_parte_bridges'),
    ]

    operations = [
        AddIndexConcurrently(
            'process',
            models.Index(fields=['tribunal', 'enriquecimento_status'],
                         name='proc_trib_enriq_idx'),
        ),
    ]
