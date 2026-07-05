"""Registra os Tribunais Superiores: STJ + STF.

`ativo=False` — ativação (ingestão DJEN) em ondas, igual aos TRTs. sigla_djen=sigla.
Valor pra leads é menor que os graus de execução (é cassação), mas cobre
rastreamento de teses/andamentos de precatório em recurso.
"""
from django.db import migrations

TRIBUNAIS = [
    ('STJ', 'STJ', 'Superior Tribunal de Justiça', False),
    ('STF', 'STF', 'Supremo Tribunal Federal', False),
]


def seed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    for sigla, sigla_djen, nome, ativo in TRIBUNAIS:
        Tribunal.objects.update_or_create(
            sigla=sigla,
            defaults={'sigla_djen': sigla_djen, 'nome': nome, 'ativo': ativo},
        )


def unseed(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.filter(sigla__in=[t[0] for t in TRIBUNAIS]).delete()


class Migration(migrations.Migration):
    dependencies = [('tribunals', '0040_seed_trts')]
    operations = [migrations.RunPython(seed, unseed)]
