from django.core.management.base import BaseCommand

from elasticsearch import helpers

from search.client import get_es, index_name
from search.documents import processo_to_doc
from tribunals.models import Process


class Command(BaseCommand):
    help = 'Reindexa Process no Elasticsearch (bulk).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla do tribunal.')
        parser.add_argument('--limit', type=int, default=0, help='Máximo de docs (0=ilimitado).')
        parser.add_argument('--batch-size', type=int, default=1000, help='Tamanho do bulk.')

    def handle(self, *args, **options):
        qs = Process.objects.select_related('tribunal').all()
        if options['tribunal']:
            qs = qs.filter(tribunal_id=options['tribunal'])
        if options['limit']:
            qs = qs[:options['limit']]

        total = qs.count()
        self.stdout.write(f'Reindexando {total:,} processos...')

        es = get_es()
        idx = index_name('processos')
        actions = []
        for proc in qs.iterator(chunk_size=options['batch_size']):
            doc = processo_to_doc(proc)
            actions.append({
                '_index': idx,
                '_id': proc.id,
                '_source': doc,
            })
            if len(actions) >= options['batch_size']:
                helpers.bulk(es, actions)
                self.stdout.write(f'  {len(actions)} enviados...')
                actions = []
        if actions:
            helpers.bulk(es, actions)

        self.stdout.write(self.style.SUCCESS(f'Reindexação concluída: {total:,} docs.'))