"""Adiciona permission `can_view_motivo` em ProcessoValidacao.

Motivo: REGRAS_NEGOCIO_VALIDACAO §7 — separação read-motivo vs anotar.
- Validador comum: vê só o próprio `motivo`.
- Revisor sênior, auditor: pode ler `motivo` de outros (com `can_view_motivo`).
- model_admin: NÃO ganha (só agregado, conforme tabela).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0026_seed_classificador_versao_v6'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='processovalidacao',
            options={
                'verbose_name': 'validação de processo',
                'verbose_name_plural': 'validações de processo',
                'permissions': [
                    ('can_validate_lead',
                     'Pode anotar validações de leads'),
                    ('can_publish_model',
                     'Pode promover ClassificadorVersao e editar thresholds'),
                    ('can_view_validacao_dashboard',
                     'Pode ver dashboard de validação'),
                    ('can_resolve_disagreement',
                     'Pode resolver divergências preenchendo label_final'),
                    ('can_view_motivo',
                     'Pode ler o campo motivo de validações de outros usuários'),
                ],
            },
        ),
    ]
