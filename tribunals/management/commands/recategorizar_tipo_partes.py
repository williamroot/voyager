"""Recategoriza `Parte.tipo` poluído (papel processual cru) de volta pra
canônico pf/pj/advogado/desconhecido.

Bug histórico (`enrichers/esaj.py`): TJSP/TJAL (e-SAJ) gravavam o papel
processual cru (Reqte/Exectdo/Agravante/...) em `Parte.tipo` em vez de
classificar pf/pj/advogado/desconhecido. Resultado: o donut "Distribuição por
tipo" da página `/dashboard/partes/` mostrava centenas de rótulos sem sentido.
O código já foi corrigido; este command limpa os dados históricos.

Re-deriva `tipo` das colunas já no banco, espelhando exatamente
`enrichers.parsers.classificar_tipo_parte`:

    oab <> ''            -> advogado
    tipo_documento=CNPJ  -> pj
    tipo_documento=CPF   -> pf
    senão                -> desconhecido

Só toca linhas com `tipo` fora do conjunto canônico (idempotente — re-rodar
não acha nada). UPDATE em lotes por faixa de id pra não travar a tabela de
~12M linhas.

    python manage.py recategorizar_tipo_partes --dry-run
    python manage.py recategorizar_tipo_partes
    python manage.py recategorizar_tipo_partes --batch-size 100000
"""
from django.core.management.base import BaseCommand
from django.db import connection

# Lista (não tupla): psycopg3 não aceita `IN %s` com tupla — usa-se
# `!= ALL(%s)` com uma lista (mesmo idioma de check_parte_indexes.py).
CANONICOS = ['pf', 'pj', 'advogado', 'desconhecido']

# Espelha enrichers.parsers.classificar_tipo_parte. Mantém em SQL pra rodar
# o UPDATE no banco sem trazer 12M linhas pro Python.
_UPDATE_SQL = """
UPDATE tribunals_parte
SET tipo = CASE
    WHEN oab <> '' THEN 'advogado'
    WHEN tipo_documento = 'CNPJ' THEN 'pj'
    WHEN tipo_documento = 'CPF' THEN 'pf'
    ELSE 'desconhecido'
END
WHERE tipo NOT IN ('pf', 'pj', 'advogado', 'desconhecido')
  AND id >= %s AND id < %s
"""


class Command(BaseCommand):
    help = 'Recategoriza Parte.tipo poluído com papel cru → pf/pj/advogado/desconhecido.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument('--batch-size', type=int, default=50_000,
                            help='Tamanho da faixa de id por UPDATE (default 50k).')

    def handle(self, *args, dry_run, batch_size, **opts):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM tribunals_parte WHERE tipo != ALL(%s)", [CANONICOS]
            )
            afetadas = cur.fetchone()[0]
            cur.execute(
                "SELECT tipo, count(*) FROM tribunals_parte WHERE tipo != ALL(%s) "
                "GROUP BY tipo ORDER BY count(*) DESC LIMIT 20", [CANONICOS]
            )
            amostra = cur.fetchall()
            cur.execute("SELECT coalesce(min(id), 0), coalesce(max(id), 0) FROM tribunals_parte")
            lo, hi = cur.fetchone()

        self.stdout.write(f'Partes com tipo não-canônico: {afetadas:,} (ids {lo}..{hi})')
        for tipo, n in amostra:
            self.stdout.write(f'  {tipo!r}: {n:,}')

        if afetadas == 0:
            self.stdout.write(self.style.SUCCESS('Nada a fazer — todos os tipos já canônicos.'))
            return
        if dry_run:
            self.stdout.write(self.style.WARNING('dry-run: nenhuma alteração aplicada.'))
            return

        total = 0
        start = lo
        while start <= hi:
            end = start + batch_size
            with connection.cursor() as cur:
                cur.execute(_UPDATE_SQL, [start, end])
                n = cur.rowcount
            total += n
            if n:
                self.stdout.write(f'  ids [{start},{end}): recategorizadas={n} acum={total:,}')
            start = end

        self.stdout.write(self.style.SUCCESS(f'Concluído. Linhas recategorizadas: {total:,}'))
