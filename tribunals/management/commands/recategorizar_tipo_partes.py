"""Recategoriza `Parte.tipo` poluído (papel processual cru) de volta pra
canônico pf/pj/advogado/desconhecido — fazendo o MERGE das Partes que o bug
fragmentou.

Bug histórico (`enrichers/esaj.py`): TJSP/TJAL (e-SAJ) gravavam o papel
processual cru (Reqte/Exectdo/Agravante/...) em `Parte.tipo` em vez de
pf/pj/advogado/desconhecido. Como quase toda parte e-SAJ não tem doc nem OAB
(e-SAJ mascara CPF/CNPJ), o lookup de dedupe `(nome, '', '', tipo)` usava o
papel na chave → a MESMA entidade (ex: "Fazenda Pública do Estado de SP", INSS)
virou N Partes, uma por papel.

Por isso não dá pra só fazer `UPDATE tipo='desconhecido'`: várias linhas do
mesmo nome colidiriam na constraint `uniq_parte_sem_doc_nem_oab (nome, tipo)
WHERE documento='' AND oab=''`. A limpeza é um dedup-merge:

  FASE 1  Funde por nome as Partes sem-doc-sem-oab de nomes que têm ≥1
          fragmento não-canônico (survivor = MIN(id), inclui um eventual
          'desconhecido' já existente). Repoint de ProcessoParte + delete dos
          losers, reusando o padrão batched de `dedup_partes._apply_dedup_map`
          (UNLOGGED table real — sobrevive ao pgbouncer transaction-mode; trata
          colisão da unique de ProcessoParte por slot).
  FASE 2  Agora cada nome sem-doc tem 1 linha: normaliza `tipo` (CASE espelha
          `enrichers.parsers.classificar_tipo_parte`). Pega também as poucas
          linhas com doc/oab (constraints delas não incluem `tipo` → sem
          colisão).
  FASE 3  Recalcula `Parte.total_processos` dos survivors que absorveram PP
          (UPDATE de parte_id não dispara os triggers pp_total_*).

⚠️ JANELA DE MANUTENÇÃO: rode com backup (`pg_dump`) e os drainers PARADOS
(senão eventos in-flight re-inserem Partes durante o merge). Ver .ia/OPS.md.
Resumível: re-rodar reconstrói o mapa do estado atual e refaz concluído como
no-op.

    python manage.py recategorizar_tipo_partes --dry-run
    python manage.py recategorizar_tipo_partes
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

# Lista (não tupla): psycopg3 não aceita `IN %s` com tupla — usa-se
# `!= ALL(%s)` com uma lista (mesmo idioma de check_parte_indexes.py).
CANONICOS = ['pf', 'pj', 'advogado', 'desconhecido']

# Re-derivação canônica em SQL — espelha classificar_tipo_parte. Aplicada só
# depois do merge (FASE 2), quando cada nome sem-doc já tem 1 linha.
_RETIPO_SQL = """
UPDATE tribunals_parte
SET tipo = CASE
    WHEN oab <> '' THEN 'advogado'
    WHEN tipo_documento = 'CNPJ' THEN 'pj'
    WHEN tipo_documento = 'CPF' THEN 'pf'
    ELSE 'desconhecido'
END
WHERE tipo != ALL(%s)
  AND id >= %s AND id < %s
"""


class Command(BaseCommand):
    help = 'Merge + recategoriza Parte.tipo poluído com papel cru → canônico.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument('--batch-size', type=int, default=200_000,
                            help='Tamanho da faixa de id por lote (default 200k).')

    def handle(self, *args, dry_run, batch_size, **opts):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM tribunals_parte WHERE tipo != ALL(%s)", [CANONICOS])
            total_nc = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM tribunals_parte "
                "WHERE tipo != ALL(%s) AND documento = '' AND oab = ''", [CANONICOS]
            )
            nc_semdoc = cur.fetchone()[0]
            cur.execute(
                "SELECT tipo, count(*) FROM tribunals_parte WHERE tipo != ALL(%s) "
                "GROUP BY tipo ORDER BY count(*) DESC LIMIT 15", [CANONICOS]
            )
            amostra = cur.fetchall()

        self.stdout.write(f'tipo não-canônico: {total_nc:,} '
                          f'(sem-doc-sem-oab: {nc_semdoc:,} | com doc/oab: {total_nc - nc_semdoc:,})')
        for tipo, n in amostra:
            self.stdout.write(f'  {tipo!r}: {n:,}')
        if total_nc == 0:
            self.stdout.write(self.style.SUCCESS('Nada a fazer — todos os tipos já canônicos.'))
            return

        # ---- FASE 1: merge dos sem-doc-sem-oab por nome ----
        self._build_map_sem_doc()
        self._apply_merge(dry_run=dry_run, batch=batch_size)

        if dry_run:
            self._drop_map()
            self.stdout.write(self.style.WARNING(
                'dry-run: nenhuma alteração aplicada (merge nem recategorização).'))
            return

        # ---- FASE 3 (prep): coleta survivors antes de dropar o mapa ----
        with connection.cursor() as cur:
            cur.execute('SELECT DISTINCT survivor_id FROM _retipo_map')
            survivors = [r[0] for r in cur.fetchall()]
        self._drop_map()

        # ---- FASE 2: normaliza tipo (cada nome sem-doc já tem 1 linha) ----
        self.stdout.write('FASE 2: normalizando tipo …')
        with connection.cursor() as cur:
            cur.execute('SELECT coalesce(min(id), 0), coalesce(max(id), 0) FROM tribunals_parte')
            lo, hi = cur.fetchone()
        total = 0
        start = lo
        while start <= hi:
            end = start + batch_size
            with connection.cursor() as cur:
                cur.execute(_RETIPO_SQL, [CANONICOS, start, end])
                n = cur.rowcount
            total += n
            if n:
                self.stdout.write(f'  retipo ids [{start},{end}): {n} (acum {total:,})')
            start = end
        self.stdout.write(self.style.SUCCESS(f'FASE 2: {total:,} linhas recategorizadas.'))

        # ---- FASE 3: recalcula total_processos dos survivors ----
        self._recalc_total_processos(survivors, batch=batch_size)

        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM tribunals_parte WHERE tipo != ALL(%s)", [CANONICOS])
            restante = cur.fetchone()[0]
        msg = f'Concluído. tipo não-canônico restante: {restante:,}'
        self.stdout.write((self.style.SUCCESS if restante == 0 else self.style.WARNING)(msg))

    # ------------------------------------------------------------------ #
    def _build_map_sem_doc(self):
        """Mapa loser_id→survivor_id: Partes sem-doc-sem-oab agrupadas por nome,
        só pra nomes com ≥1 fragmento não-canônico. Survivor = MIN(id) (engloba
        um eventual 'desconhecido' já existente do mesmo nome)."""
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _retipo_map')
            cur.execute("""
                CREATE UNLOGGED TABLE _retipo_map AS
                SELECT loser_id, survivor_id FROM (
                    SELECT id AS loser_id,
                           min(id) OVER (PARTITION BY nome) AS survivor_id,
                           count(*) FILTER (WHERE tipo NOT IN ('pf','pj','advogado','desconhecido'))
                               OVER (PARTITION BY nome) AS n_frag
                    FROM tribunals_parte
                    WHERE documento = '' AND oab = ''
                ) s
                WHERE n_frag > 0
            """)
            cur.execute('DELETE FROM _retipo_map WHERE loser_id = survivor_id')
            cur.execute('CREATE INDEX ON _retipo_map (loser_id)')
            # ANALYZE é essencial: sem estatísticas na UNLOGGED, o planner
            # seq-scaneia os ~19GB de tribunals_processoparte a CADA batch
            # (horas). Com stats, escolhe nested-loop usando o índice
            # (parte_id,polo) → lookups pontuais (segundos no total).
            cur.execute('ANALYZE _retipo_map')

    def _drop_map(self):
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _retipo_map')

    def _apply_merge(self, *, dry_run, batch):
        """Repoint batched de ProcessoParte + delete dos losers. Espelha
        `dedup_partes._apply_dedup_map` (mesma maquinaria revisada): por lote,
        remove a PP que ficaria redundante pós-repoint (mantém menor id por
        slot processo/polo/papel/representa), nulla representa_id órfã, repointa
        o resto e deleta as Partes-loser."""
        with connection.cursor() as cur:
            cur.execute('SELECT count(*), coalesce(min(loser_id), 0), coalesce(max(loser_id), 0) '
                        'FROM _retipo_map')
            total, lo, hi = cur.fetchone()
        self.stdout.write(f'FASE 1: losers a fundir: {total or 0:,}'
                          + ('  (DRY-RUN)' if dry_run else ''))
        if dry_run or not total:
            return
        t0 = time.time()
        cursor_id = lo
        while cursor_id <= hi:
            fim = cursor_id + batch
            with transaction.atomic():
                with connection.cursor() as c2:
                    c2.execute("""
                        CREATE TEMP TABLE _pp_lote ON COMMIT DROP AS
                        SELECT ppl.id AS pp_id, ppl.processo_id, ppl.polo,
                               ppl.papel, ppl.representa_id,
                               m.survivor_id AS post_parte
                        FROM tribunals_processoparte ppl
                        JOIN _retipo_map m ON m.loser_id = ppl.parte_id
                        WHERE m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    # Mantém exatamente 1 PP por slot (processo,polo,papel,
                    # representa) que pós-repoint aponta pro survivor; deleta os
                    # losers redundantes. Diferença vs dedup_partes (que tem este
                    # bug latente): o PP do PRÓPRIO survivor nunca está em
                    # _pp_lote (survivor não é loser), então se o PP do survivor
                    # tem id MAIOR que um PP loser no mesmo slot, `o.id<l.pp_id`
                    # não dispararia e o loser seria repointado → colisão na
                    # unique uniq_processo_parte_polo_papel_principal. O 2º ramo
                    # (`mo.survivor_id IS NULL` ⇒ o é PP de não-loser = do próprio
                    # survivor) deleta o loser independente do id.
                    c2.execute("""
                        CREATE TEMP TABLE _pp_del ON COMMIT DROP AS
                        SELECT l.pp_id FROM _pp_lote l
                        WHERE EXISTS (
                            SELECT 1 FROM tribunals_processoparte o
                            LEFT JOIN _retipo_map mo ON mo.loser_id = o.parte_id
                            WHERE o.processo_id = l.processo_id
                              AND o.polo = l.polo AND o.papel = l.papel
                              AND o.representa_id IS NOT DISTINCT FROM l.representa_id
                              AND COALESCE(mo.survivor_id, o.parte_id) = l.post_parte
                              AND o.id <> l.pp_id
                              AND (o.id < l.pp_id OR mo.survivor_id IS NULL)
                        )
                    """)
                    c2.execute("""
                        UPDATE tribunals_processoparte
                        SET representa_id = NULL
                        WHERE representa_id IN (SELECT pp_id FROM _pp_del)
                    """)
                    c2.execute("DELETE FROM tribunals_processoparte WHERE id IN (SELECT pp_id FROM _pp_del)")
                    c2.execute("""
                        UPDATE tribunals_processoparte ppl
                        SET parte_id = m.survivor_id
                        FROM _retipo_map m
                        WHERE ppl.parte_id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    c2.execute("""
                        DELETE FROM tribunals_parte p
                        USING _retipo_map m
                        WHERE p.id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
            self.stdout.write(f'FASE 1: lote {cursor_id:,}–{fim:,} ok ({time.time() - t0:.0f}s acum.)')
            cursor_id = fim
        self.stdout.write(self.style.SUCCESS('FASE 1: merge concluído.'))

    def _recalc_total_processos(self, survivors, *, batch):
        if not survivors:
            return
        self.stdout.write(f'FASE 3: recalculando total_processos de {len(survivors):,} survivors …')
        for i in range(0, len(survivors), batch):
            chunk = survivors[i:i + batch]
            with connection.cursor() as cur:
                cur.execute("""
                    UPDATE tribunals_parte p
                    SET total_processos = COALESCE(sub.n, 0)
                    FROM (
                        SELECT parte_id, count(*) AS n
                        FROM tribunals_processoparte
                        WHERE parte_id = ANY(%s)
                        GROUP BY parte_id
                    ) sub
                    WHERE p.id = sub.parte_id
                """, [chunk])
                # survivors que ficaram com 0 PP (nenhum sobreviveu ao dedup)
                cur.execute(
                    "UPDATE tribunals_parte SET total_processos = 0 "
                    "WHERE id = ANY(%s) AND id NOT IN "
                    "(SELECT DISTINCT parte_id FROM tribunals_processoparte WHERE parte_id = ANY(%s))",
                    [chunk, chunk]
                )
        self.stdout.write(self.style.SUCCESS('FASE 3: total_processos recalculado.'))
