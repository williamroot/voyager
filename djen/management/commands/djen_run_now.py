from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from djen.client import DJENClient
from djen.ingestion import ingest_window
from tribunals.models import Tribunal


class Command(BaseCommand):
    help = 'Roda 1 ingestão sob demanda para um tribunal (sem fila, sem checagem de backfill).'

    def add_arguments(self, parser):
        parser.add_argument('sigla')
        parser.add_argument('--dias', type=int, default=1, help='Quantos dias retroativos (default 1).')
        parser.add_argument('--inicio', default=None)
        parser.add_argument('--fim', default=None)

    def handle(self, *args, sigla, dias, inicio, fim, **opts):
        try:
            t = Tribunal.objects.get(sigla=sigla)
        except Tribunal.DoesNotExist:
            raise CommandError(f'Tribunal {sigla} não cadastrado')
        fim_dt = date.fromisoformat(fim) if fim else date.today()
        inicio_dt = date.fromisoformat(inicio) if inicio else fim_dt - timedelta(days=dias)
        run = ingest_window(t, inicio_dt, fim_dt, client=DJENClient())
        self.stdout.write(self.style.SUCCESS(
            f'run #{run.pk} {run.status}: novas={run.movimentacoes_novas} '
            f'duplicadas={run.movimentacoes_duplicadas} paginas={run.paginas_lidas}'
        ))
