"""Preenche Process.classe (codigo + nome + FK) usando a classe da
movimentação DJEN mais recente, pra processos que estão sem classe.

DJEN sempre traz codigoClasse/nomeClasse em cada item. Quando o PJe não
retorna detalhe (status nao_encontrado/erro), o fallback DJEN vale a
pena — classe é o único campo estruturado garantido.

SQL puro pra performance: UPDATE com sub-select. Idempotente — só toca
rows com classe_codigo=''.
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Preenche Process.classe a partir da movimentação DJEN mais recente."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument('--all-at-once', action='store_true',
                            help='UPDATE único global (PERIGOSO em 1B+ movs). Default: batelado por tribunal.')

    def handle(self, *args, dry_run, all_at_once=False, **opts):
        if not all_at_once:
            return self._por_tribunal(dry_run)
        return self._global(dry_run)

    # Batelado por tribunal: cada UPDATE limita o DISTINCT ON aos movs do
    # tribunal (sort menor, usa índice processo_id) e a transação aos processos
    # dele — 60 transações pequenas em vez de 1 monstro sobre 1,1B linhas.
    def _por_tribunal(self, dry_run):
        import time
        from tribunals.models import Tribunal
        siglas = list(Tribunal.objects.filter(ativo=True).order_by('sigla')
                      .values_list('sigla', flat=True))
        total = 0
        for sig in siglas:
            with connection.cursor() as cur:
                if dry_run:
                    cur.execute("""
                        SELECT COUNT(*) FROM tribunals_process
                        WHERE classe_codigo='' AND tribunal_id=%s
                          AND EXISTS (SELECT 1 FROM tribunals_movimentacao m
                                      WHERE m.processo_id=tribunals_process.id AND m.codigo_classe<>'')
                    """, [sig])
                    n = cur.fetchone()[0]
                    self.stdout.write(f'{sig}: {n:,} a preencher (dry-run)')
                    total += n
                    continue
                t0 = time.time()
                cur.execute("""
                    UPDATE tribunals_process p
                    SET classe_codigo=m.codigo_classe, classe_nome=m.nome_classe, classe_id=m.classe_id
                    FROM (
                        SELECT DISTINCT ON (processo_id) processo_id, codigo_classe, nome_classe, classe_id
                        FROM tribunals_movimentacao
                        WHERE codigo_classe<>'' AND tribunal_id=%s
                        ORDER BY processo_id, data_disponibilizacao DESC
                    ) m
                    WHERE p.id=m.processo_id AND p.classe_codigo='' AND p.tribunal_id=%s
                """, [sig, sig])
                total += cur.rowcount
                self.stdout.write(f'{sig}: {cur.rowcount:,} atualizados ({time.time()-t0:.0f}s)', ending='\n')
                self.stdout.flush()
        self.stdout.write(self.style.SUCCESS(f'TOTAL: {total:,} Process com classe preenchida'))

    def _global(self, dry_run):
        with connection.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM tribunals_process
                WHERE classe_codigo = ''
                  AND EXISTS (SELECT 1 FROM tribunals_movimentacao m
                              WHERE m.processo_id = tribunals_process.id
                                AND m.codigo_classe <> '');
            """)
            n = cur.fetchone()[0]
            self.stdout.write(self.style.HTTP_INFO(
                f'{n:,} Process sem classe que têm Movimentacao com classe DJEN'
            ))
            if dry_run or n == 0:
                return

            cur.execute("""
                UPDATE tribunals_process p
                SET classe_codigo = m.codigo_classe,
                    classe_nome = m.nome_classe,
                    classe_id = m.classe_id
                FROM (
                    SELECT DISTINCT ON (processo_id)
                           processo_id, codigo_classe, nome_classe, classe_id
                    FROM tribunals_movimentacao
                    WHERE codigo_classe <> ''
                    ORDER BY processo_id, data_disponibilizacao DESC
                ) m
                WHERE p.id = m.processo_id
                  AND p.classe_codigo = '';
            """)
            self.stdout.write(self.style.SUCCESS(
                f'  {cur.rowcount:,} Process atualizados'
            ))
