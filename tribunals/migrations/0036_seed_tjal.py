"""Seed do Tribunal TJAL (e-SAJ consulta pública — mesmo software do TJSP).

e-SAJ público: https://www2.tjal.jus.br/cpopg/
Enricher: enrichers.esaj.TjalEnricher (subclasse de BaseEsajEnricher).

Idempotente (update_or_create). Sobe `ativo=False` — ativação manual após
validar enricher em prod e descobrir floor com `djen_descobrir_inicio TJAL`.
Procedimento padrão em .ia/OPS.md "Subir backfill de tribunal novo".
"""
from django.db import migrations


def seed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.update_or_create(
        sigla='TJAL',
        defaults={
            'sigla_djen': 'TJAL',
            'nome': 'Tribunal de Justiça do Estado de Alagoas',
            'ativo': False,
        },
    )


def unseed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.filter(sigla='TJAL').delete()


class Migration(migrations.Migration):
    dependencies = [('tribunals', '0035_mv_volume_mensal')]
    operations = [migrations.RunPython(seed, unseed)]
