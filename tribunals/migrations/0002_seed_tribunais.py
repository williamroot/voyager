from django.db import migrations

TRIBUNAIS = [
    ('TRF1', 'TRF1', 'Tribunal Regional Federal da 1ª Região',  True),
    ('TRF3', 'TRF3', 'Tribunal Regional Federal da 3ª Região',  True),
    ('TRF2', 'TRF2', 'Tribunal Regional Federal da 2ª Região',  False),
    ('TRF4', 'TRF4', 'Tribunal Regional Federal da 4ª Região',  False),
    ('TRF5', 'TRF5', 'Tribunal Regional Federal da 5ª Região',  False),
    ('TRF6', 'TRF6', 'Tribunal Regional Federal da 6ª Região',  False),
    ('TJSP', 'TJSP', 'Tribunal de Justiça do Estado de São Paulo', False),
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
    dependencies = [('tribunals', '0001_initial')]
    operations = [migrations.RunPython(seed, unseed)]
