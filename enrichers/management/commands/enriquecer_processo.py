from django.core.management.base import BaseCommand, CommandError

from enrichers.jobs import enqueue_enriquecimento, enriquecer_processo
from tribunals.models import Process


class Command(BaseCommand):
    help = 'Enriquece um Process com dados da consulta pública do tribunal (TRF1, etc.).'

    def add_arguments(self, parser):
        parser.add_argument('cnj_or_id', help='Número CNJ ou ID do Process.')
        parser.add_argument('--async', action='store_true', dest='async_',
                            help='Enfileira na queue default em vez de rodar inline.')

    def handle(self, *args, cnj_or_id, async_, **opts):
        try:
            p = Process.objects.get(pk=int(cnj_or_id))
        except (ValueError, Process.DoesNotExist):
            try:
                p = Process.objects.get(numero_cnj=cnj_or_id)
            except Process.DoesNotExist:
                raise CommandError(f'Process não encontrado: {cnj_or_id}')

        if async_:
            j = enqueue_enriquecimento(p.pk, p.tribunal_id)
            self.stdout.write(self.style.SUCCESS(
                f'Enfileirado job {j.id} em enrich_{p.tribunal_id.lower()} '
                f'para Process #{p.pk} ({p.numero_cnj})'
            ))
            return

        result = enriquecer_processo(p.pk)
        self.stdout.write(self.style.SUCCESS(f'OK: {result}'))
