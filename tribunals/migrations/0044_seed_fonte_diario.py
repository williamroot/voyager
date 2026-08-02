from django.db import migrations

# Mapeamento source_id (Jusbrasil/Digesto) → tribunal Voyager.
# Granularidade: 1 por tribunal (sem comarca). TJMG usa source_id 154 representativo.
# Tribunais não ativos no Jusbrasil ficam sem FonteDiario (podem ser criados depois via admin).
FONTES = [
    # source_id, sigla_tribunal, diario_slug, orgao_slug, caderno_slug, nome
    (1,   'TRF1', 'dje-trf1',    'trf1',  '',          'TRF - 1ª Reg.'),
    (59,  'TRF3', 'dje-trf3',    'trf3',  '',          'TRF - 3ª Reg.'),
    (60,  'TRF4', 'dje-trf4',    'trf4',  '',          'TRF - 4ª Reg.'),
    (61,  'TRF5', 'dje-trf5',    'trf5',  '',          'TRF - 5ª Reg. (Jud)'),
    (74,  'TRF2', 'dje-trf2',    'trf2',  '',          'TRF - 2ª Reg. Judicial'),
    (18,  'TJSP', 'dje-tjsp',    'tjsp',  'cad.2-2-inst', 'SP - TJ - cad.2 2ª Inst'),
    (154, 'TJMG', 'dje-tjmg',    'tjmg',  'editais',      'MG - TJ - Editais'),
    (84,  'TRT3', 'dje-trabalhista', 'trt-3a-regiao', '', 'TRT da 3ª Reg. (MG)'),
    (85,  'TRT4', 'dje-trabalhista', 'trt-4a-regiao', '', 'TRT da 4ª Reg. (RS)'),
    (86,  'TRT5', 'dje-trabalhista', 'trt-5a-regiao', '', 'TRT da 5ª Reg. (BA)'),
    (87,  'TRT6', 'dje-trabalhista', 'trt-6a-regiao', '', 'TRT da 6ª Reg. (PE)'),
    (25,  'STF',  'dje-stf',     'stf',   '',          'Nacional - STF'),
    (81,  'STJ',  'dje-stj',     'stj',   '',          'Nacional - STJ'),
    (26,  'TST',  'comunica',    'tst',   '',          'Nacional - TST - DJEN'),
]


def seed_fontes(apps, schema_editor):
    FonteDiario = apps.get_model('tribunals', 'FonteDiario')
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    for source_id, sigla, diario_slug, orgao_slug, caderno, nome in FONTES:
        tribunal = Tribunal.objects.filter(sigla=sigla).first()
        if tribunal is None:
            continue  # tribunal não existe no Voyager ainda
        FonteDiario.objects.update_or_create(
            source_id=source_id,
            defaults={
                'tribunal': tribunal,
                'diario_slug': diario_slug,
                'orgao_slug': orgao_slug,
                'caderno_slug': caderno,
                'nome': nome,
            },
        )


def unseed_fontes(apps, schema_editor):
    FonteDiario = apps.get_model('tribunals', 'FonteDiario')
    FonteDiario.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0043_fonte_diario_e_assunto_norm'),
    ]

    operations = [
        migrations.RunPython(seed_fontes, unseed_fontes),
    ]