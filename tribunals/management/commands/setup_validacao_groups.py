"""Cria/atualiza os grupos Django de validação humana de leads.

Idempotente — `get_or_create` em grupos e `set/add` em permissions.

Grupos criados:
- `validadores_leads`: anotadores; podem anotar e ver dashboard de validação.
- `revisores_seniores`: subset sênior — adicional pra resolver divergências.
- `auditores_leads`: read-only do dashboard (não anotam).
- `model_admins`: pode promover ClassificadorVersao e editar thresholds.

Permissions provêm da Meta de ProcessoValidacao (migration 0024).
"""
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

PERMISSIONS_VALIDADORES = [
    'can_validate_lead',
    'can_view_validacao_dashboard',
]
PERMISSIONS_REVISORES_EXTRA = [
    'can_resolve_disagreement',
    'can_view_motivo',
]
PERMISSIONS_MODEL_ADMINS = [
    'can_publish_model',
    'can_view_validacao_dashboard',
]
PERMISSIONS_AUDITORES = [
    'can_view_validacao_dashboard',
    'can_view_motivo',
]


def _get_perms(codenames):
    perms = list(
        Permission.objects.filter(
            content_type__app_label='tribunals',
            codename__in=codenames,
        )
    )
    found = {p.codename for p in perms}
    missing = set(codenames) - found
    if missing:
        raise RuntimeError(
            f'Permissions não encontradas (rode migrate primeiro): {sorted(missing)}'
        )
    return perms


class Command(BaseCommand):
    help = 'Cria/atualiza grupos de validação humana (idempotente).'

    def handle(self, *args, **options):
        # validadores_leads
        validadores, created_v = Group.objects.get_or_create(name='validadores_leads')
        validadores.permissions.set(_get_perms(PERMISSIONS_VALIDADORES))
        self.stdout.write(
            self.style.SUCCESS(
                f'{"+" if created_v else "="} validadores_leads · {len(PERMISSIONS_VALIDADORES)} perms'
            )
        )

        # revisores_seniores (= validadores_leads + can_resolve_disagreement)
        revisores, created_r = Group.objects.get_or_create(name='revisores_seniores')
        revisores.permissions.set(
            _get_perms(PERMISSIONS_VALIDADORES + PERMISSIONS_REVISORES_EXTRA)
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'{"+" if created_r else "="} revisores_seniores · '
                f'{len(PERMISSIONS_VALIDADORES) + len(PERMISSIONS_REVISORES_EXTRA)} perms'
            )
        )

        # auditores_leads (read-only)
        auditores, created_a = Group.objects.get_or_create(name='auditores_leads')
        auditores.permissions.set(_get_perms(PERMISSIONS_AUDITORES))
        self.stdout.write(
            self.style.SUCCESS(
                f'{"+" if created_a else "="} auditores_leads · {len(PERMISSIONS_AUDITORES)} perms'
            )
        )

        # model_admins
        model_admins, created_m = Group.objects.get_or_create(name='model_admins')
        model_admins.permissions.set(_get_perms(PERMISSIONS_MODEL_ADMINS))
        self.stdout.write(
            self.style.SUCCESS(
                f'{"+" if created_m else "="} model_admins · {len(PERMISSIONS_MODEL_ADMINS)} perms'
            )
        )

        self.stdout.write(self.style.SUCCESS('Grupos de validação configurados.'))
