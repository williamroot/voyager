"""Seed do Tribunal TJDFT (PJe via SPA Angular + REST API).

Endpoint da API: https://pje-consultapublica-api.tjdft.jus.br/v1
SPA pública: https://pje-consultapublica.tjdft.jus.br/

Idempotente (update_or_create). Sobe `ativo=False` — ativação manual
após validar enricher em prod e descobrir floor com `djen_descobrir_inicio
TJDFT`. Procedimento padrão em .ia/OPS.md "Subir backfill de tribunal novo".
"""
from django.db import migrations


def seed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.update_or_create(
        sigla='TJDFT',
        defaults={
            'sigla_djen': 'TJDFT',
            'nome': 'Tribunal de Justiça do Distrito Federal e dos Territórios',
            'ativo': False,
        },
    )


def unseed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.filter(sigla='TJDFT').delete()


class Migration(migrations.Migration):
    dependencies = [('tribunals', '0032_seed_tjma')]
    operations = [migrations.RunPython(seed, unseed)]
