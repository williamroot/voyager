from django.core.management.base import BaseCommand

from elasticsearch import helpers

from search.client import get_es, index_name
from search.documents import movimentacao_to_doc
from tribunals.models import Movimentacao


class Command(BaseCommand):
    help = 'Reindexa Movimentacoes no Elasticsearch (bulk).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla do tribunal.')
        parser.add_argument('--desde', type=str, help='A partir de (YYYY-MM-DD).')
        parser.add_argument('--limit', type=int, default=0, help='Máximo de docs (0=ilimitado).')
        parser.add_argument('--batch-size', type=int, default=1000, help='Tamanho do bulk.')

    def handle(self, *args, **options):
        qs = Movimentacao.objects.select_related('processo', 'tribunal').all()
        if options['tribunal']:
            qs = qs.filter(tribunal_id=options['tribunal'])
        if options['desde']:
            qs = qs.filter(data_disponibilizacao__gte=options['desde'])
        if options['limit']:
            qs = qs[:options['limit']]

        total = qs.count()
        self.stdout.write(f'Reindexando {total:,} movimentações...')

        es = get_es()
        idx = index_name('movimentacoes')
        actions = []
        for mov in qs.iterator(chunk_size=options['batch_size']):
            doc = movimentacao_to_doc(mov)
            actions.append({
                '_index': idx,
                '_id': mov.id,
                '_source': doc,
            })
            if len(actions) >= options['batch_size']:
                helpers.bulk(es, actions)
                self.stdout.write(f'  {len(actions)} enviados...')
                actions = []
        if actions:
            helpers.bulk(es, actions)

        self.stdout.write(self.style.SUCCESS(f'Reindexação concluída: {total:,} docs.'))