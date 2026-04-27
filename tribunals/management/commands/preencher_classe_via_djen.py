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

    def handle(self, *args, dry_run, **opts):
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
