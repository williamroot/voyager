"""Deduplica tribunals_parte. Causado por índices únicos parciais que
ficaram INVÁLIDOS (CREATE UNIQUE INDEX CONCURRENTLY que falhou — ver
migration 0017).

Set-based em SQL: Python loop em ~80M linhas é inviável. Idempotente e
resumível — re-rodar após interrupção recalcula e continua.

Anti-homônimo: colapso só por chave EXATA; absorção masc_to_real só com 1
candidato. Survivor = MIN(id) / sempre a Parte de doc real.
"""
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

logger = logging.getLogger('voyager.dedup_partes')

# Grupos de colapso por chave byte-idêntica: nome -> (predicado WHERE, PARTITION BY)
GRUPOS = {
    'oab': ("oab <> ''", 'oab'),
    'doc_real': (
        "documento <> '' AND documento NOT LIKE '%X%' "
        "AND documento NOT LIKE '%x%' AND documento NOT LIKE '%*%'",
        'documento',
    ),
    'doc_masc': (
        "(documento LIKE '%X%' OR documento LIKE '%x%' OR documento LIKE '%*%')",
        'nome, documento',
    ),
}
ORDEM_ALL = ['oab', 'doc_real', 'doc_masc', 'masc_to_real']


class Command(BaseCommand):
    help = 'Deduplica tribunals_parte (anti-homônimo). Ver plano dedup-partes.'

    def add_arguments(self, parser):
        parser.add_argument('--group', choices=ORDEM_ALL + ['all'], default='all')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200_000)

    def handle(self, *args, **opts):
        grupos = ORDEM_ALL if opts['group'] == 'all' else [opts['group']]
        for g in grupos:
            if g == 'masc_to_real':
                self._merge_masc_to_real(dry_run=opts['dry_run'], batch=opts['batch_size'])
            else:
                self._dedup_grupo(g, dry_run=opts['dry_run'], batch=opts['batch_size'])

    def _apply_dedup_map(self, *, label, dry_run, batch):
        """Consome a TEMP TABLE _dedup_map(loser_id, survivor_id) já criada
        e indexada por loser_id. Repointa ProcessoParte (à prova de colisão
        com uniq_processo_parte_polo_papel_principal) e deleta as Partes-loser.
        """
        with connection.cursor() as cur:
            cur.execute('SELECT count(*), min(loser_id), max(loser_id) FROM _dedup_map')
            total, lo, hi = cur.fetchone()
        self.stdout.write(f'[{label}] losers a colapsar: {total or 0:,}'
                          + ('  (DRY-RUN)' if dry_run else ''))
        if dry_run or not total:
            return
        t0 = time.time()
        cursor_id = lo
        while cursor_id <= hi:
            fim = cursor_id + batch
            with transaction.atomic():
                with connection.cursor() as c2:
                    # 1) Apaga ProcessoParte-loser que colidiria com uma
                    #    ProcessoParte-survivor já existente no mesmo processo.
                    c2.execute("""
                        DELETE FROM tribunals_processoparte ppl
                        USING _dedup_map m, tribunals_processoparte pps
                        WHERE ppl.parte_id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                          AND pps.processo_id = ppl.processo_id
                          AND pps.parte_id = m.survivor_id
                          AND pps.polo = ppl.polo AND pps.papel = ppl.papel
                          AND pps.representa_id IS NOT DISTINCT FROM ppl.representa_id
                    """, [cursor_id, fim])
                    # 2) Repointa o restante.
                    c2.execute("""
                        UPDATE tribunals_processoparte ppl
                        SET parte_id = m.survivor_id
                        FROM _dedup_map m
                        WHERE ppl.parte_id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    # 3) Deleta as Partes-loser do lote.
                    c2.execute("""
                        DELETE FROM tribunals_parte p
                        USING _dedup_map m
                        WHERE p.id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
            self.stdout.write(f'[{label}] lote {cursor_id:,}–{fim:,} ok '
                              f'({time.time() - t0:.0f}s)')
            cursor_id = fim
        self.stdout.write(self.style.SUCCESS(f'[{label}] concluído'))

    def _dedup_grupo(self, grupo, *, dry_run, batch):
        where, partition = GRUPOS[grupo]
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute(f"""
                CREATE TEMP TABLE _dedup_map AS
                SELECT id AS loser_id,
                       min(id) OVER (PARTITION BY {partition}) AS survivor_id
                FROM tribunals_parte WHERE {where}
            """)
            cur.execute('DELETE FROM _dedup_map WHERE loser_id = survivor_id')
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
        self._apply_dedup_map(label=grupo, dry_run=dry_run, batch=batch)
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')

    def _merge_masc_to_real(self, *, dry_run, batch):
        raise CommandError('[masc_to_real] não implementado — ver Task 4')
