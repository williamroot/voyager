"""Reconstrói as pontes denormalizadas `ParteTribunal` e `PartePapel` a partir
de `tribunals_processoparte` (+ join em `tribunals_process` pro tribunal).

Por quê: filtrar a lista de Partes por tribunal/papel via EXISTS sobre
`tribunals_processoparte` (bilhões de linhas) custa ~43s (medido 2026-06-27).
As pontes (índice por tribunal/papel + `total_processos` desnormalizado) tornam
o filtro instantâneo. Ver dashboard `partes`.

Estratégia: batched por faixa de `parte_id` (não por id da PP — assim cada
INSERT ... SELECT DISTINCT é local a um conjunto de partes e o ON CONFLICT
torna o re-run idempotente/resumível). `--truncate` zera antes (rebuild full);
sem ele, faz upsert incremental (ON CONFLICT DO NOTHING) — bom pra cron.

⚠️ Varre a `tribunals_processoparte` inteira — rode em janela mais calma
(ex.: fora de backfill pesado) pra não disputar IO. Resumível: re-rodar
continua de onde o ON CONFLICT já cobriu.

    python manage.py rebuild_parte_bridges --truncate          # rebuild full
    python manage.py rebuild_parte_bridges --batch 50000       # incremental
"""
import time
from contextlib import contextmanager

from django.core.management.base import BaseCommand
from django.db import connection, transaction

STMT_TIMEOUT_MS = 1_200_000  # 20min/statement — trava anti-runaway (cada lote leva segundos)


@contextmanager
def _big_cursor():
    """Cursor com statement_timeout alto (SET LOCAL só cola sob transaction.atomic
    no pgbouncer transaction-pooling — o default de 20s cancelaria os lotes)."""
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [STMT_TIMEOUT_MS])
            yield cur


_INSERT_TRIBUNAL = """
INSERT INTO tribunals_partetribunal (parte_id, tribunal_id, total_processos)
SELECT DISTINCT pp.parte_id, p.tribunal_id, pt.total_processos
FROM tribunals_processoparte pp
JOIN tribunals_process p ON p.id = pp.processo_id
JOIN tribunals_parte pt ON pt.id = pp.parte_id
WHERE pp.parte_id >= %s AND pp.parte_id < %s
ON CONFLICT (parte_id, tribunal_id) DO UPDATE SET total_processos = EXCLUDED.total_processos
"""

_INSERT_PAPEL = """
INSERT INTO tribunals_partepapel (parte_id, papel, total_processos)
SELECT DISTINCT pp.parte_id, pp.papel, pt.total_processos
FROM tribunals_processoparte pp
JOIN tribunals_parte pt ON pt.id = pp.parte_id
WHERE pp.parte_id >= %s AND pp.parte_id < %s AND pp.papel <> ''
ON CONFLICT (parte_id, papel) DO UPDATE SET total_processos = EXCLUDED.total_processos
"""


class Command(BaseCommand):
    help = 'Reconstrói as pontes ParteTribunal/PartePapel pra filtro rápido da lista de Partes.'

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=50_000, help='Tamanho do range de parte_id por lote.')
        parser.add_argument('--truncate', action='store_true', help='Zera as pontes antes (rebuild full).')
        parser.add_argument('--start', type=int, default=0, help='parte_id inicial (pra retomar).')

    def handle(self, *args, batch, truncate, start, **opts):
        with connection.cursor() as cur:
            cur.execute('SELECT COALESCE(MAX(id), 0) FROM tribunals_parte')
            max_parte_id = cur.fetchone()[0]
        self.stdout.write(f'max parte_id = {max_parte_id:,} · batch = {batch:,} · start = {start:,}')

        if truncate:
            with _big_cursor() as cur:
                cur.execute('TRUNCATE tribunals_partetribunal, tribunals_partepapel')
            self.stdout.write(self.style.WARNING('pontes truncadas (rebuild full).'))

        t0 = time.time()
        lo = start
        while lo <= max_parte_id:
            hi = lo + batch
            with _big_cursor() as cur:
                cur.execute(_INSERT_TRIBUNAL, [lo, hi])
                n_trib = cur.rowcount
                cur.execute(_INSERT_PAPEL, [lo, hi])
                n_pap = cur.rowcount
            self.stdout.write(
                f'  parte_id {lo:,}–{hi:,}: +{n_trib} trib, +{n_pap} papel ({int(time.time() - t0)}s acum.)',
                ending='\r',
            )
            lo = hi
        self.stdout.write('')

        with connection.cursor() as cur:
            cur.execute('SELECT count(*) FROM tribunals_partetribunal')
            tot_t = cur.fetchone()[0]
            cur.execute('SELECT count(*) FROM tribunals_partepapel')
            tot_p = cur.fetchone()[0]
            cur.execute('SELECT count(DISTINCT papel) FROM tribunals_partepapel')
            n_papeis = cur.fetchone()[0]
        self.stdout.write(self.style.SUCCESS(
            f'Concluído em {int(time.time() - t0)}s · ParteTribunal={tot_t:,} · PartePapel={tot_p:,} · papéis distintos={n_papeis}'
        ))
