"""Re-popula FKs `classe`/`assunto` em chunks com lock_timeout curto.

Substituto chunked da migration `0010_populate_classe_assunto`. A migration
faz UPDATE em transação única — segura ingestão concorrente por minutos.
Aqui cada batch tem seu próprio commit + lock_timeout=5s, então não trava
o resto do sistema.

Idempotente: só atualiza linhas com `<fk>_id IS NULL`. Pode rodar quantas
vezes quiser.
"""
from django.core.management.base import BaseCommand
from django.db import connection


CHUNK = 50_000


class Command(BaseCommand):
    help = 'Backfill chunked das FKs classe/assunto em Process e Movimentacao.'

    def add_arguments(self, parser):
        parser.add_argument('--chunk', type=int, default=CHUNK)
        parser.add_argument('--only', choices=['process', 'movimentacao', 'all'], default='all')

    def handle(self, *args, chunk, only, **opts):
        if only in ('process', 'all'):
            self._chunked_update(
                'tribunals_process', 'classe_id', 'classe_codigo', chunk,
                label='Process.classe',
            )
            self._chunked_update(
                'tribunals_process', 'assunto_id', 'assunto_codigo', chunk,
                label='Process.assunto',
            )
        if only in ('movimentacao', 'all'):
            self._chunked_update(
                'tribunals_movimentacao', 'classe_id', 'codigo_classe', chunk,
                label='Movimentacao.classe',
            )

    def _chunked_update(self, table: str, fk_col: str, src_col: str, chunk: int, *, label: str):
        self.stdout.write(self.style.HTTP_INFO(f'==> {label}: chunks de {chunk:,}'))
        total = 0
        while True:
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '5s';")
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET {fk_col} = {src_col}
                    WHERE ctid IN (
                        SELECT ctid FROM {table}
                        WHERE {src_col} <> '' AND {fk_col} IS NULL
                        LIMIT %s
                    )
                    """,
                    [chunk],
                )
                affected = cur.rowcount
            total += affected
            self.stdout.write(f'  {label}: {affected:>7,} (acum {total:,})')
            if affected == 0:
                break
        self.stdout.write(self.style.SUCCESS(f'<== {label}: {total:,} linhas'))
