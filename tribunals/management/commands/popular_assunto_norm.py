from django.core.management.base import BaseCommand

from tribunals.models import Movimentacao
from tribunals.tipos_norm import classificar_tipo_norm


class Command(BaseCommand):
    help = 'Popula Movimentacao.assunto_norm via classificador heurístico (backfill).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla.')
        parser.add_argument('--limit', type=int, default=1000, help='Máximo.')
        parser.add_argument('--batch-size', type=int, default=500, help='Batch update.')

    def handle(self, *args, **options):
        qs = Movimentacao.objects.filter(assunto_norm=[])
        if options['tribunal']:
            qs = qs.filter(tribunal_id=options['tribunal'])
        qs = qs.order_by('-id')[:options['limit']]

        total = 0
        batch = []
        for mov in qs.iterator(chunk_size=options['batch_size']):
            norm = classificar_tipo_norm(mov)
            if norm:
                mov.assunto_norm = [[t, s] for t, s in norm]
                batch.append(mov)
                total += 1
            if len(batch) >= options['batch_size']:
                Movimentacao.objects.bulk_update(batch, ['assunto_norm'])
                self.stdout.write(f'  {total} atualizados...')
                batch = []
        if batch:
            Movimentacao.objects.bulk_update(batch, ['assunto_norm'])

        self.stdout.write(self.style.SUCCESS(f'{total} movimentações classificadas.'))