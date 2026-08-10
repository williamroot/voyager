"""Fase 0 — marca Process.tem_sinal_precatorio lendo o texto DJEN das movimentações.

Sinal precatório computável ANTES de enriquecer (o refill datajud prioriza os True).
Set-based por JANELA DE PK: cada linha usa EXISTS com o índice (processo) sobre a
movimentação — NÃO varre a tabela de 1,33B (não há índice de texto). ~7ms/proc.

Resumível (só toca tem_sinal_precatorio IS NULL), throttlável (--sleep) e shardável
(--min-id/--max-id → rode N cópias em paralelo em faixas disjuntas).

    python manage.py backfill_sinal_precatorio --tribunais TJPR,TRF4,TRF6,TRF2
    python manage.py backfill_sinal_precatorio --tribunais TJPR --min-id 0 --max-id 5000000
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection

# Sinal de precatório no TEXTO das movimentações (mesmos termos do classificador,
# _MOVS_AGG_SQL: f11/f12/f13/f14). Precursor do crédito contra a Fazenda.
PADRAO = r'precat[óo]rio|of[íi]cio requisit[óo]rio|\mrpv\M|requisi[çc][ãa]o de pagamento'

DATAJUD_ALVO = ['TJPR', 'TRF4', 'TRF6', 'TRF2']


class Command(BaseCommand):
    help = 'Fase 0: marca tem_sinal_precatorio pelo texto DJEN (por janela de pk).'

    def add_arguments(self, p):
        p.add_argument('--tribunais', default=','.join(DATAJUD_ALVO),
                       help='CSV de siglas (default: alvo datajud).')
        p.add_argument('--batch', type=int, default=20000, help='tamanho da janela de pk')
        p.add_argument('--sleep', type=float, default=0.2, help='pausa entre janelas (s)')
        p.add_argument('--min-id', type=int, default=None)
        p.add_argument('--max-id', type=int, default=None)
        p.add_argument('--dry-run', action='store_true')

    def handle(self, *a, **o):
        tribs = [s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
        batch, sleep = o['batch'], o['sleep']
        # Range de pk via ORDER BY id LIMIT 1 POR tribunal (usa o índice
        # (tribunal,-id) → instantâneo). MIN/MAX com `tribunal_id = ANY(array)`
        # NÃO usa o índice → vira scan e trava (visto em prod).
        lo0 = hi0 = None
        with connection.cursor() as c:
            for t in tribs:
                c.execute("SELECT id FROM tribunals_process WHERE tribunal_id=%s "
                          "ORDER BY id ASC LIMIT 1", [t])
                r = c.fetchone()
                if r:
                    lo0 = r[0] if lo0 is None else min(lo0, r[0])
                c.execute("SELECT id FROM tribunals_process WHERE tribunal_id=%s "
                          "ORDER BY id DESC LIMIT 1", [t])
                r = c.fetchone()
                if r:
                    hi0 = r[0] if hi0 is None else max(hi0, r[0])
        lo0, hi0 = lo0 or 0, hi0 or 0
        lo = o['min_id'] if o['min_id'] is not None else lo0
        hi = o['max_id'] if o['max_id'] is not None else hi0
        self.stdout.write(f'alvo={tribs} pk[{lo:,}..{hi:,}] batch={batch:,} sleep={sleep}s'
                          + (' DRY-RUN' if o['dry_run'] else ''))
        if o['dry_run']:
            return

        sql = """
            UPDATE tribunals_process p
               SET tem_sinal_precatorio = EXISTS (
                     SELECT 1 FROM tribunals_movimentacao m
                      WHERE m.processo_id = p.id AND m.texto ~* %s)
             WHERE p.tribunal_id = ANY(%s)
               AND p.data_enriquecimento_datajud IS NULL
               AND p.tem_sinal_precatorio IS NULL
               AND p.id >= %s AND p.id < %s
        """
        t0 = time.monotonic()
        feitos = marcados = 0
        cur = lo
        while cur <= hi:
            nxt = cur + batch
            with connection.cursor() as c:
                c.execute(sql, [PADRAO, tribs, cur, nxt])
                n = c.rowcount
                c.execute(
                    "SELECT COUNT(*) FROM tribunals_process WHERE tribunal_id = ANY(%s) "
                    "AND tem_sinal_precatorio IS TRUE AND id >= %s AND id < %s",
                    [tribs, cur, nxt])
                m = c.fetchone()[0]
            feitos += n
            marcados += m
            if feitos and (cur // batch) % 20 == 0:
                dt = time.monotonic() - t0
                self.stdout.write(
                    f'  pk~{cur:,} · processados {feitos:,} · com_sinal {marcados:,} '
                    f'({100*marcados/feitos:.1f}%) · {feitos/dt:.0f}/s')
            cur = nxt
            if sleep:
                time.sleep(sleep)
        dt = time.monotonic() - t0
        self.stdout.write(self.style.SUCCESS(
            f'FIM: {feitos:,} processados, {marcados:,} com sinal '
            f'({100*marcados/feitos if feitos else 0:.1f}%) em {dt/60:.1f}min'))
