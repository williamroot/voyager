"""Registra a Justiça do Trabalho: TST + 24 TRTs.

Todos entram `ativo=False` — a ativação (que dispara ingestão DJEN + backfill)
é feita em ondas via admin/shell, vigiando capacidade. sigla_djen = sigla
(confirmado no recon 2026-07-04: DJEN responde a siglaTribunal='TRTn'/'TST').
"""
from django.db import migrations

TRIBUNAIS = [
    ('TST',   'TST',   'Tribunal Superior do Trabalho', False),
    ('TRT1',  'TRT1',  'Tribunal Regional do Trabalho da 1ª Região (RJ)', False),
    ('TRT2',  'TRT2',  'Tribunal Regional do Trabalho da 2ª Região (SP)', False),
    ('TRT3',  'TRT3',  'Tribunal Regional do Trabalho da 3ª Região (MG)', False),
    ('TRT4',  'TRT4',  'Tribunal Regional do Trabalho da 4ª Região (RS)', False),
    ('TRT5',  'TRT5',  'Tribunal Regional do Trabalho da 5ª Região (BA)', False),
    ('TRT6',  'TRT6',  'Tribunal Regional do Trabalho da 6ª Região (PE)', False),
    ('TRT7',  'TRT7',  'Tribunal Regional do Trabalho da 7ª Região (CE)', False),
    ('TRT8',  'TRT8',  'Tribunal Regional do Trabalho da 8ª Região (PA/AP)', False),
    ('TRT9',  'TRT9',  'Tribunal Regional do Trabalho da 9ª Região (PR)', False),
    ('TRT10', 'TRT10', 'Tribunal Regional do Trabalho da 10ª Região (DF/TO)', False),
    ('TRT11', 'TRT11', 'Tribunal Regional do Trabalho da 11ª Região (AM/RR)', False),
    ('TRT12', 'TRT12', 'Tribunal Regional do Trabalho da 12ª Região (SC)', False),
    ('TRT13', 'TRT13', 'Tribunal Regional do Trabalho da 13ª Região (PB)', False),
    ('TRT14', 'TRT14', 'Tribunal Regional do Trabalho da 14ª Região (RO/AC)', False),
    ('TRT15', 'TRT15', 'Tribunal Regional do Trabalho da 15ª Região (Campinas/SP)', False),
    ('TRT16', 'TRT16', 'Tribunal Regional do Trabalho da 16ª Região (MA)', False),
    ('TRT17', 'TRT17', 'Tribunal Regional do Trabalho da 17ª Região (ES)', False),
    ('TRT18', 'TRT18', 'Tribunal Regional do Trabalho da 18ª Região (GO)', False),
    ('TRT19', 'TRT19', 'Tribunal Regional do Trabalho da 19ª Região (AL)', False),
    ('TRT20', 'TRT20', 'Tribunal Regional do Trabalho da 20ª Região (SE)', False),
    ('TRT21', 'TRT21', 'Tribunal Regional do Trabalho da 21ª Região (RN)', False),
    ('TRT22', 'TRT22', 'Tribunal Regional do Trabalho da 22ª Região (PI)', False),
    ('TRT23', 'TRT23', 'Tribunal Regional do Trabalho da 23ª Região (MT)', False),
    ('TRT24', 'TRT24', 'Tribunal Regional do Trabalho da 24ª Região (MS)', False),
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
    dependencies = [('tribunals', '0039_proc_numero_cnj_idx')]
    operations = [migrations.RunPython(seed, unseed)]
