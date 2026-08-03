"""Reindexa Process no Elasticsearch — bulk via HTTP, throttled, resumível.

Usa o endpoint ``_bulk`` do ES via ``requests`` (NDJSON) em vez da lib
``elasticsearch`` — assim roda no container ``web`` (que tem o código atualizado
por bind-mount + ``requests``), sem depender da lib que só está nos workers.
"""
import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from search.documents import processo_to_doc
from tribunals.models import Process, ProcessoParte


class Command(BaseCommand):
    help = 'Reindexa Process no Elasticsearch (bulk HTTP, throttled, resumível por tribunal).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla do tribunal.')
        parser.add_argument('--limit', type=int, default=0, help='Máximo de docs (0=ilimitado).')
        parser.add_argument('--batch-size', type=int, default=2000, help='Tamanho do bulk.')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Pausa (s) entre bulks — throttle do DB (I/O sensível).')

    def handle(self, *args, **options):
        es = getattr(settings, 'ELASTICSEARCH_URL', 'http://elasticsearch:9200').rstrip('/')
        prefix = getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')
        idx = f'{prefix}-processos'

        # prefetch das participações (+parte) → _serialize_partes NÃO faz query por
        # processo. Sem isso, 71M processos = 71M+ queries e o DB (I/O sensível) cai.
        qs = (Process.objects
              .select_related('tribunal')
              .prefetch_related(Prefetch('participacoes',
                                         queryset=ProcessoParte.objects.select_related('parte')))
              .order_by('id'))
        if options['tribunal']:
            qs = qs.filter(tribunal_id=options['tribunal'])
        if options['limit']:
            qs = qs[:options['limit']]

        total = qs.count()
        bs = options['batch_size']
        slp = options['sleep']
        self.stdout.write(f'Reindexando {total:,} processos '
                          f'(tribunal={options["tribunal"] or "TODOS"}, batch={bs}, sleep={slp}s) → {idx}')
        self.stdout.flush()

        sess = requests.Session()

        def flush(actions):
            if not actions:
                return
            lines = []
            for pid, doc in actions:
                lines.append(json.dumps({'index': {'_index': idx, '_id': pid}}))
                lines.append(json.dumps(doc, default=str, ensure_ascii=False))
            body = ('\n'.join(lines) + '\n').encode('utf-8')
            try:
                r = sess.post(f'{es}/_bulk', data=body,
                              headers={'Content-Type': 'application/x-ndjson'}, timeout=180)
                if r.status_code >= 300:
                    self.stderr.write(f'  bulk HTTP {r.status_code}: {r.text[:200]}')
            except Exception as e:  # noqa: BLE001 — ES hipou; loga e segue (idempotente)
                self.stderr.write(f'  bulk ERRO: {str(e)[:200]}')

        actions = []
        enviados = 0
        t0 = time.monotonic()
        for proc in qs.iterator(chunk_size=bs):
            actions.append((proc.id, processo_to_doc(proc)))
            if len(actions) >= bs:
                flush(actions)
                enviados += len(actions)
                actions = []
                el = time.monotonic() - t0
                rate = enviados / el if el else 0
                eta = (total - enviados) / rate / 60 if rate else 0
                self.stdout.write(f'  {enviados:,}/{total:,} '
                                  f'({100 * enviados / max(total, 1):.1f}%) · {rate:.0f}/s · ETA {eta:.0f}min')
                self.stdout.flush()
                if slp:
                    time.sleep(slp)
        flush(actions)
        enviados += len(actions)
        self.stdout.write(self.style.SUCCESS(f'Concluído: {enviados:,} docs indexados em {idx}.'))
