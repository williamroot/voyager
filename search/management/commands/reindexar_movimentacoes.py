from django.core.management.base import BaseCommand
from django.db import connection

from elasticsearch import helpers

from search.client import get_es, index_name
from search.documents import movimentacao_to_doc, movimentacao_to_doc_sem_partes
from tribunals.models import Movimentacao


class Command(BaseCommand):
    help = 'Reindexa Movimentacoes no Elasticsearch (bulk).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla do tribunal.')
        parser.add_argument('--desde', type=str, help='A partir de (YYYY-MM-DD).')
        parser.add_argument('--limit', type=int, default=0, help='Máximo de docs (0=ilimitado).')
        parser.add_argument('--batch-size', type=int, default=1000, help='Tamanho do bulk.')
        parser.add_argument('--sem-partes', action='store_true',
                            help='Pula serialização de partes (mais rápido pra backfill inicial).')

    def handle(self, *args, **options):
        # Paginação por PK — não depende de server-side cursors (pgbouncer incompatível).
        base_qs = Movimentacao.objects.all()
        if options['tribunal']:
            base_qs = base_qs.filter(tribunal_id=options['tribunal'])
        if options['desde']:
            base_qs = base_qs.filter(data_disponibilizacao__gte=options['desde'])

        total_estimado = _estimativa_count(base_qs)
        self.stdout.write(f'Reindexando ~{total_estimado:,} movimentações...')

        es = get_es()
        idx = index_name('movimentacoes')
        sem_partes = options.get('sem_partes', False)
        batch_size = options['batch_size']
        limit = options['limit']
        enviados = 0
        last_pk = 0

        while True:
            qs = base_qs.filter(id__gt=last_pk).order_by('id')[:batch_size]
            batch = list(qs)
            if not batch:
                break
            actions = []
            for mov in batch:
                if sem_partes:
                    doc = movimentacao_to_doc_sem_partes(mov)
                else:
                    doc = movimentacao_to_doc(mov)
                actions.append({'_index': idx, '_id': mov.id, '_source': doc})
            if actions:
                helpers.bulk(es, actions)
                enviados += len(actions)
                self.stdout.write(f'  {enviados:,} enviados...')
            last_pk = batch[-1].id
            if limit and enviados >= limit:
                break

        self.stdout.write(self.style.SUCCESS(f'Reindexação concluída: {enviados:,} docs.'))


def _estimativa_count(qs) -> int:
    """Estima count via pg_class.reltuples (instantâneo) em vez de COUNT(*)."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'tribunals_movimentacao'")
            row = c.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0