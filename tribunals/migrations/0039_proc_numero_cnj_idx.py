"""Índice standalone em numero_cnj (lookup por CNJ sem tribunal).

A busca semântica do acervo (Zordon) resolve CNJ→Process.pk filtrando só por
numero_cnj; o índice único (tribunal, numero_cnj) tem líder tribunal, então o
filtro varria o índice inteiro (cost ~893k) e travava a request. CONCURRENTLY.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('tribunals', '0038_proc_trib_enriq_idx'),
    ]

    operations = [
        AddIndexConcurrently(
            'process',
            models.Index(fields=['numero_cnj'], name='proc_numero_cnj_idx'),
        ),
    ]
