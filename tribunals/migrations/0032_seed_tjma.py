"""Seed do Tribunal TJMA (PJe via pje.tjma.jus.br).

Idempotente: usa update_or_create. Sobe `ativo=False` — ativação manual
após descobrir floor (`djen_descobrir_inicio TJMA`) e validar enricher
em prod. Procedimento padrão em .ia/OPS.md "Subir backfill de tribunal
novo".
"""
from django.db import migrations


def seed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.update_or_create(
        sigla='TJMA',
        defaults={
            'sigla_djen': 'TJMA',
            'nome': 'Tribunal de Justiça do Estado do Maranhão',
            'ativo': False,
        },
    )


def unseed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.filter(sigla='TJMA').delete()


class Migration(migrations.Migration):
    dependencies = [('tribunals', '0031_mv_tribunal_kpis')]
    operations = [migrations.RunPython(seed, unseed)]
