"""Deduplica tribunals_parte. Causado por índices únicos parciais que
ficaram INVÁLIDOS (CREATE UNIQUE INDEX CONCURRENTLY que falhou — ver
migration 0017).

Set-based em SQL: Python loop em ~80M linhas é inviável. Resumível por
grupo: cada grupo recalcula o mapa de duplicatas a partir do estado atual
da tabela, então re-rodar após interrupção refaz grupos concluídos como
no-op e continua de onde parou.

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
        """Consome a TEMP TABLE _dedup_map(loser_id, survivor_id) já criada e
        indexada. Por lote (faixa de loser_id): remove ProcessoParte que ficaria
        redundante pós-repoint (mantém o de menor id por slot), nulla
        representa_id que apontaria pra PP deletada, repointa o restante e
        deleta as Partes-loser.
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
                    # PP-loser deste lote + a parte que terão pós-repoint.
                    c2.execute("""
                        CREATE TEMP TABLE _pp_lote ON COMMIT DROP AS
                        SELECT ppl.id AS pp_id, ppl.processo_id, ppl.polo,
                               ppl.papel, ppl.representa_id,
                               m.survivor_id AS post_parte
                        FROM tribunals_processoparte ppl
                        JOIN _dedup_map m ON m.loser_id = ppl.parte_id
                        WHERE m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    # Redundante: já há outra PP no mesmo slot cuja parte
                    # pós-repoint é igual, com id menor (survivor PP existente
                    # OU outra PP-loser de id menor). Mantém só o menor id.
                    c2.execute("""
                        CREATE TEMP TABLE _pp_del ON COMMIT DROP AS
                        SELECT l.pp_id FROM _pp_lote l
                        WHERE EXISTS (
                            SELECT 1 FROM tribunals_processoparte o
                            LEFT JOIN _dedup_map mo ON mo.loser_id = o.parte_id
                            WHERE o.processo_id = l.processo_id
                              AND o.polo = l.polo AND o.papel = l.papel
                              AND o.representa_id IS NOT DISTINCT FROM l.representa_id
                              AND COALESCE(mo.survivor_id, o.parte_id) = l.post_parte
                              AND o.id < l.pp_id
                        )
                    """)
                    # representa_id é FK self; nulla quem aponta pras PP que
                    # serão deletadas (raw DELETE não dispara on_delete=SET_NULL).
                    c2.execute("""
                        UPDATE tribunals_processoparte
                        SET representa_id = NULL
                        WHERE representa_id IN (SELECT pp_id FROM _pp_del)
                    """)
                    # Deleta as PP redundantes.
                    c2.execute("""
                        DELETE FROM tribunals_processoparte
                        WHERE id IN (SELECT pp_id FROM _pp_del)
                    """)
                    # Repointa as PP-loser restantes pro survivor.
                    c2.execute("""
                        UPDATE tribunals_processoparte ppl
                        SET parte_id = m.survivor_id
                        FROM _dedup_map m
                        WHERE ppl.parte_id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    # Deleta as Partes-loser do lote.
                    c2.execute("""
                        DELETE FROM tribunals_parte p
                        USING _dedup_map m
                        WHERE p.id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
            self.stdout.write(f'[{label}] lote {cursor_id:,}–{fim:,} ok '
                              f'({time.time() - t0:.0f}s acum.)')
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
        """Absorve Parte de doc mascarado na Parte de doc real correspondente.
        Só funde com nome byte-idêntico + máscara casando + EXATAMENTE 1
        candidato real. `translate(doc,'Xx*','___')` vira o pattern LIKE.
        Roda depois de doc_real/doc_masc (compara contra dados já colapsados).
        """
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute("""
                CREATE TEMP TABLE _dedup_map AS
                SELECT masc_id AS loser_id, real_id AS survivor_id FROM (
                    SELECT m.id AS masc_id, min(r.id) AS real_id, count(*) AS n
                    FROM tribunals_parte m
                    JOIN tribunals_parte r
                      ON r.nome = m.nome
                     AND r.id <> m.id
                     AND r.documento <> ''
                     AND r.documento NOT LIKE '%X%' AND r.documento NOT LIKE '%x%'
                     AND r.documento NOT LIKE '%*%'
                     AND r.documento LIKE translate(m.documento, 'Xx*', '___')
                    WHERE m.documento LIKE '%X%' OR m.documento LIKE '%x%'
                       OR m.documento LIKE '%*%'
                    GROUP BY m.id
                ) cand
                WHERE n = 1
            """)
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
        self._apply_dedup_map(label='masc_to_real', dry_run=dry_run, batch=batch)
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
