"""Funde Partes sem doc nem OAB em Partes com doc REAL de mesmo nome,
quando há **exatamente 1** match — evita associar homônimos.

Pula tipo='pf' (pessoas físicas têm muitos homônimos verdadeiros).
Move ProcessoParte refs e deleta as órfãs.

Idempotente: pode rodar quantas vezes quiser.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Funde Partes sem doc em Partes com doc real (mesmo nome, único match, não pf)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--include-pf', action='store_true', dest='include_pf',
                            help='Inclui tipo=pf (CUIDADO: associa homônimos).')

    def handle(self, *args, dry_run, limit, include_pf, **opts):
        # Apenas CNPJ real — pattern XX.XXX.XXX/XXXX-XX. PFs (CPF) nunca são
        # candidatas mesmo com 1 match, pois o `tipo` pode estar errado em
        # rows antigas e o risco de associar 'João da Silva' homônimo é real.
        # CNPJ identifica entidade jurídica unicamente.
        cnpj_filter = (
            "documento ~ '^[0-9]{2}\\.[0-9]{3}\\.[0-9]{3}/[0-9]{4}-[0-9]{2}$'"
            if not include_pf else
            "documento <> '' AND documento NOT LIKE '%%X%%' AND documento NOT LIKE '%%x%%' AND documento NOT LIKE '%%*%%'"
        )
        with connection.cursor() as cur:
            cur.execute(f"""
                CREATE TEMP TABLE _merge_map AS
                WITH reais AS (
                    SELECT id AS real_id, nome
                    FROM tribunals_parte
                    WHERE {cnpj_filter}
                ),
                contagens AS (
                    SELECT nome, COUNT(*) AS n_reais, MIN(real_id) AS unico_real
                    FROM reais GROUP BY nome
                )
                SELECT p_sem.id AS sem_id, c.unico_real AS real_id
                FROM tribunals_parte p_sem
                JOIN contagens c ON c.nome = p_sem.nome AND c.n_reais = 1
                WHERE p_sem.documento = '' AND p_sem.oab = '';
            """)
            cur.execute("CREATE INDEX ON _merge_map(sem_id);")
            cur.execute("CREATE INDEX ON _merge_map(real_id);")
            cur.execute("SELECT COUNT(*) FROM _merge_map;")
            n = cur.fetchone()[0]
            self.stdout.write(self.style.HTTP_INFO(
                f'{n:,} Partes sem doc serão fundidas em Partes com doc real (1:1)'
            ))
            if limit:
                cur.execute(f"DELETE FROM _merge_map WHERE ctid NOT IN (SELECT ctid FROM _merge_map LIMIT {limit});")

            if dry_run:
                cur.execute("SELECT sem_id, real_id FROM _merge_map LIMIT 10;")
                for sem_id, real_id in cur.fetchall():
                    cur.execute(
                        "SELECT nome FROM tribunals_parte WHERE id IN (%s, %s);",
                        [sem_id, real_id],
                    )
                    nomes = [r[0] for r in cur.fetchall()]
                    self.stdout.write(f'  [dry] sem_id={sem_id} → real_id={real_id} ({nomes[0] if nomes else "?"})')
                return

            with transaction.atomic():
                # Re-aponta representa_id de filhos antes do DELETE de pp
                # conflitantes (mesma estratégia da migration 0012).
                cur.execute("""
                    CREATE TEMP TABLE _pp_envolvidos AS
                    SELECT pp.id AS pp_id,
                           pp.processo_id,
                           COALESCE(m.real_id, pp.parte_id) AS target_parte_id,
                           pp.polo, pp.papel, pp.representa_id
                    FROM tribunals_processoparte pp
                    LEFT JOIN _merge_map m ON pp.parte_id = m.sem_id
                    WHERE m.sem_id IS NOT NULL
                       OR EXISTS (SELECT 1 FROM _merge_map mk WHERE mk.real_id = pp.parte_id);
                """)
                cur.execute("CREATE INDEX ON _pp_envolvidos(pp_id);")

                cur.execute("""
                    CREATE TEMP TABLE _pp_keepers AS
                    SELECT DISTINCT ON (processo_id, target_parte_id, polo, papel,
                                        COALESCE(representa_id, -1))
                           pp_id, processo_id, target_parte_id, polo, papel, representa_id
                    FROM _pp_envolvidos
                    ORDER BY processo_id, target_parte_id, polo, papel,
                             COALESCE(representa_id, -1), pp_id;
                """)
                cur.execute("CREATE INDEX ON _pp_keepers(pp_id);")
                cur.execute("CREATE INDEX ON _pp_keepers(processo_id, target_parte_id, polo, papel);")

                cur.execute("""
                    CREATE TEMP TABLE _pp_redirect AS
                    SELECT e.pp_id AS dup_pp_id, k.pp_id AS keep_pp_id
                    FROM _pp_envolvidos e
                    JOIN _pp_keepers k
                      ON k.processo_id = e.processo_id
                     AND k.target_parte_id = e.target_parte_id
                     AND k.polo = e.polo
                     AND k.papel = e.papel
                     AND COALESCE(k.representa_id, -1) = COALESCE(e.representa_id, -1)
                    WHERE e.pp_id <> k.pp_id;
                """)
                cur.execute("CREATE INDEX ON _pp_redirect(dup_pp_id);")

                cur.execute("""
                    UPDATE tribunals_processoparte pp
                    SET representa_id = r.keep_pp_id
                    FROM _pp_redirect r
                    WHERE pp.representa_id = r.dup_pp_id;
                """)
                self.stdout.write(f'  representa_id re-apontados: {cur.rowcount:,}')

                cur.execute("""
                    DELETE FROM tribunals_processoparte
                    WHERE id IN (SELECT dup_pp_id FROM _pp_redirect);
                """)
                self.stdout.write(f'  ProcessoParte removidos: {cur.rowcount:,}')

                cur.execute("""
                    UPDATE tribunals_processoparte pp
                    SET parte_id = m.real_id
                    FROM _merge_map m
                    WHERE pp.parte_id = m.sem_id;
                """)
                self.stdout.write(f'  ProcessoParte re-apontados: {cur.rowcount:,}')

                cur.execute("""
                    DELETE FROM tribunals_parte
                    WHERE id IN (SELECT sem_id FROM _merge_map);
                """)
                self.stdout.write(self.style.SUCCESS(f'  Partes sem doc removidas: {cur.rowcount:,}'))
