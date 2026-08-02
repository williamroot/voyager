from django.core.management.base import BaseCommand

import django_rq

from tribunals.models import Movimentacao


class Command(BaseCommand):
    help = 'Enfileira download de PDFs pendentes (backfill).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla.')
        parser.add_argument('--desde', type=str, help='A partir de (YYYY-MM-DD).')
        parser.add_argument('--limit', type=int, default=1000, help='Máximo.')

    def handle(self, *args, **options):
        qs = Movimentacao.objects.exclude(link='').all()
        if options['tribunal']:
            qs = qs.filter(tribunal_id=options['tribunal'])
        if options['desde']:
            qs = qs.filter(data_disponibilizacao__gte=options['desde'])
        qs = qs.filter(pdf__isnull=True).order_by('-id')
        qs = qs[: options['limit']]

        q = django_rq.get_queue('pdf_download')
        total = 0
        for mov in qs:
            q.enqueue('pdf_storage.jobs.baixar_pdf', mov.pk)
            total += 1

        self.stdout.write(self.style.SUCCESS(f'{total} PDFs enfileirados.'))