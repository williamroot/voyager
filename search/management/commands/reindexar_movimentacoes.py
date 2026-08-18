"""Reindexa Movimentacoes no Elasticsearch — bulk HTTP, keyset, throttled, resumível.

Backfill de VOLUME (~1,2 bi de linhas). Decisões:
- ``_bulk`` via ``requests`` (roda no container web, que NÃO tem a lib elasticsearch).
- **keyset por PK** (``id__gt``) — sem cursor server-side (pgbouncer-safe) e
  naturalmente resumível; o último id vai pra um CHECKPOINT em disco.
- ``select_related('processo')`` — mata o N+1 (``mov.processo`` por linha, que
  existe até no ``_sem_partes``).
- ``--sem-partes`` é o DEFAULT do backfill: pula ``_serialize_partes``.
- ``--sleep`` entre lotes — throttle do DB (I/O-sensível, compartilha com prod).
"""
import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from search.documents import movimentacao_to_doc, movimentacao_to_doc_sem_partes
from tribunals.models import Movimentacao


class Command(BaseCommand):
    help = 'Reindexa Movimentacoes no ES (bulk HTTP, keyset, throttled, resumível).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla do tribunal.')
        parser.add_argument('--desde', type=str, help='data_disponibilizacao >= YYYY-MM-DD.')
        parser.add_argument('--limit', type=int, default=0, help='Máx. docs nesta execução (0=ilimitado).')
        parser.add_argument('--batch-size', type=int, default=2000, help='Tamanho do bulk.')
        parser.add_argument('--sleep', type=float, default=0.0, help='Pausa (s) entre lotes — throttle.')
        parser.add_argument('--com-partes', action='store_true',
                            help='Serializa partes (mais lento; default do backfill é SEM partes).')
        parser.add_argument('--checkpoint', type=str, default='/app/media/reindex_mov_checkpoint.txt',
                            help='Arquivo que persiste o último id (resume).')
        # Faixa de id: é o que permite SHARDAR. Um processo só fez 438 docs/s
        # medidos — 179 milhões nesse ritmo são 4,7 dias. Cada shard leva a sua
        # faixa e o seu checkpoint, sem coordenação e sem escrever no mesmo doc.
        parser.add_argument('--desde-id', type=int, default=0,
                            help='Só ids > este (o checkpoint vence se for maior).')
        parser.add_argument('--ate-id', type=int, default=0,
                            help='Para neste id (0 = vai até o fim da tabela).')

    def _load_ckpt(self, path):
        try:
            with open(path) as f:
                return int((f.read() or '0').strip() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _save_ckpt(self, path, pk):
        try:
            with open(path, 'w') as f:
                f.write(str(pk))
        except Exception:  # noqa: BLE001
            pass

    def handle(self, *args, **options):
        es = getattr(settings, 'ELASTICSEARCH_URL', 'http://elasticsearch:9200').rstrip('/')
        idx = f"{getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')}-movimentacoes"
        com_partes = options.get('com_partes', False)
        bs = options['batch_size']
        slp = options['sleep']
        ckpt = options['checkpoint']

        base = Movimentacao.objects.select_related('processo').all()
        if options['tribunal']:
            base = base.filter(tribunal_id=options['tribunal'])
        if options['desde']:
            base = base.filter(data_disponibilizacao__gte=options['desde'])

        ate = options.get('ate_id') or 0
        if ate:
            base = base.filter(id__lte=ate)

        # o checkpoint vence o --desde-id: relançar um shard tem que CONTINUAR,
        # não voltar pro começo da faixa (foi o defeito que fez a extração de
        # entidades nunca convergir — ver es_movs_v2)
        last_pk = max(self._load_ckpt(ckpt), options.get('desde_id') or 0)
        total = _janela_count(last_pk, ate) if (ate or last_pk) else _estimativa_count()
        self.stdout.write(f'Reindexando ~{total:,} movimentações (batch={bs}, sleep={slp}s, '
                          f'com_partes={com_partes}) → {idx} | de id>{last_pk}'
                          + (f' até {ate:,}' if ate else ''))
        self.stdout.flush()

        sess = requests.Session()
        enviados = 0
        t0 = time.monotonic()
        while True:
            batch = list(base.filter(id__gt=last_pk).order_by('id')[:bs])
            if not batch:
                break
            lines = []
            for mov in batch:
                doc = movimentacao_to_doc(mov) if com_partes else movimentacao_to_doc_sem_partes(mov)
                lines.append(json.dumps({'index': {'_index': idx, '_id': mov.id}}))
                lines.append(json.dumps(doc, default=str, ensure_ascii=False))
            body = ('\n'.join(lines) + '\n').encode('utf-8')
            try:
                r = sess.post(f'{es}/_bulk', data=body,
                              headers={'Content-Type': 'application/x-ndjson'}, timeout=180)
                if r.status_code >= 300:
                    self.stderr.write(f'  bulk HTTP {r.status_code}: {r.text[:200]}')
            except Exception as e:  # noqa: BLE001 — ES hipou; loga e segue (idempotente por _id)
                self.stderr.write(f'  bulk ERRO: {str(e)[:200]}')

            enviados += len(batch)
            last_pk = batch[-1].id
            self._save_ckpt(ckpt, last_pk)
            el = time.monotonic() - t0
            rate = enviados / el if el else 0
            eta_h = (total - enviados) / rate / 3600 if rate else 0
            self.stdout.write(f'  {enviados:,} (~{100 * enviados / max(total, 1):.2f}%) · id={last_pk} · '
                              f'{rate:.0f}/s · ETA {eta_h:.1f}h')
            self.stdout.flush()
            if options['limit'] and enviados >= options['limit']:
                break
            if slp:
                time.sleep(slp)

        self.stdout.write(self.style.SUCCESS(f'Fim desta execução: {enviados:,} docs (último id={last_pk}).'))


def _janela_count(desde: int, ate: int) -> int:
    """COUNT exato da faixa de id. É a PK, então é varredura de índice e é barato
    — e um ETA calculado sobre a tabela inteira mentiria por ordens de grandeza
    quando o shard cuida de 3% dela."""
    try:
        with connection.cursor() as c:
            if ate:
                c.execute('SELECT count(*) FROM tribunals_movimentacao WHERE id > %s AND id <= %s',
                          [desde, ate])
            else:
                c.execute('SELECT count(*) FROM tribunals_movimentacao WHERE id > %s', [desde])
            return int(c.fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def _estimativa_count() -> int:
    """Estima count via pg_class.reltuples (instantâneo) em vez de COUNT(*)."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass('tribunals_movimentacao')")
            row = c.fetchone()
            return int(row[0]) if row and row[0] and row[0] > 0 else 0
    except Exception:  # noqa: BLE001
        return 0
