from django.core.management.base import BaseCommand, CommandError

from djen.jobs import sincronizar_movimentacoes
from tribunals.models import Process


class Command(BaseCommand):
    help = 'Sincroniza movimentações de UM processo via DJEN (?numeroProcesso=...).'

    def add_arguments(self, parser):
        parser.add_argument('cnj_or_id')
        parser.add_argument('--async', action='store_true', dest='async_')

    def handle(self, *args, cnj_or_id, async_, **opts):
        try:
            p = Process.objects.get(pk=int(cnj_or_id))
        except (ValueError, Process.DoesNotExist):
            try:
                p = Process.objects.get(numero_cnj=cnj_or_id)
            except Process.DoesNotExist:
                raise CommandError(f'Process não encontrado: {cnj_or_id}')

        if async_:
            j = sincronizar_movimentacoes.delay(p.pk)
            self.stdout.write(self.style.SUCCESS(
                f'Enfileirado job {j.id} para Process #{p.pk} ({p.numero_cnj})'
            ))
            return

        result = sincronizar_movimentacoes(p.pk)
        self.stdout.write(self.style.SUCCESS(f'OK: {result}'))
