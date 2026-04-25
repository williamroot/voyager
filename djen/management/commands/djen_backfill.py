from django.core.management.base import BaseCommand

from djen.jobs import run_backfill


class Command(BaseCommand):
    help = 'Enfileira backfill completo para um tribunal na fila djen_backfill.'

    def add_arguments(self, parser):
        parser.add_argument('sigla')
        parser.add_argument('--inicio', default=None, help='Sobrescreve data_inicio_disponivel (YYYY-MM-DD).')
        parser.add_argument('--sync', action='store_true', help='Roda inline em vez de enfileirar.')

    def handle(self, *args, sigla, inicio, sync, **opts):
        if sync:
            result = run_backfill(sigla, force_inicio=inicio)
            self.stdout.write(self.style.SUCCESS(f'backfill sync concluído: {result}'))
        else:
            j = run_backfill.delay(sigla, force_inicio=inicio)
            self.stdout.write(self.style.SUCCESS(f'enfileirado job {j.id} em djen_backfill'))
