"""Registra o STM — o 61º tribunal, que faltava na tabela desde sempre.

Por que isto é um bug de COMPLETUDE e não um cadastro esquecido: todo gate,
todo alarme e toda varredura desta casa percorrem `Tribunal.objects`. O que não
está na tabela não aparece como buraco — aparece como NADA, que é o pior jeito
de faltar. Medido em 31/08/2026:

    api_publica_stm declara ...... 27.055 documentos
    voyager-acervo tem ...........      0
    datajud_conferir_acervo ...... não listava o STM (nem como 0%)

`ativo=False`, igual ao STF e ao STJ quando entraram (migration 0041): `ativo`
governa a ingestão DJEN (scheduler diário + tick de backfill), que é decisão de
volume e não tem nada a ver com a varredura do Datajud. A varredura alcança o
STM por sigla explícita (`datajud_varredura --tribunais STM`) e o incremental
passa a alcançá-lo porque ele deixou de exigir `ativo=True` — ver
`datajud.jobs.tick_varredura_incremental`.

O `sigla_djen` fica preenchido porque a coluna é NOT NULL e porque a sigla no
DJEN é a mesma; ligar a ingestão DJEN do STM continua sendo decisão de quem
responde pelo volume, exatamente como no STJ.
"""
from django.db import migrations

TRIBUNAIS = [
    ('STM', 'STM', 'Superior Tribunal Militar', False),
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
    # `on_delete=PROTECT` em Process: se algum processo já foi criado sob o STM,
    # a remoção falha — e falhar alto é o comportamento certo.
    Tribunal.objects.filter(sigla__in=[t[0] for t in TRIBUNAIS]).delete()


class Migration(migrations.Migration):
    dependencies = [('tribunals', '0054_classe_cnj_e_fase')]
    operations = [migrations.RunPython(seed, unseed)]
