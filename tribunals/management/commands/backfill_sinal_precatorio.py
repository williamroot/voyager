"""Fase 0 — marca Process.tem_sinal_precatorio lendo o texto DJEN das movimentações.

Sinal precatório computável ANTES de enriquecer (o refill datajud prioriza os True).
Set-based por JANELA DE PK: cada linha usa EXISTS com o índice (processo) sobre a
movimentação — NÃO varre a tabela de 1,33B (não há índice de texto). ~7ms/proc.

Resumível (só toca tem_sinal_precatorio IS NULL), throttlável (--sleep) e paralelizável:
o pick usa `FOR UPDATE SKIP LOCKED` dentro de uma transação, então dá pra rodar N cópias
NO MESMO tribunal que cada uma pega um lote disjunto (sem contention de row-lock). Também
shardável por --min-id/--max-id.

    python manage.py backfill_sinal_precatorio --tribunais TJPR,TRF4,TRF6,TRF2
    python manage.py backfill_sinal_precatorio --tribunais TJPR --min-id 0 --max-id 5000000
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

# Sinal de precatório no TEXTO das movimentações (mesmos termos do classificador,
# _MOVS_AGG_SQL: f11/f12/f13/f14). Precursor do crédito contra a Fazenda.
PADRAO = r'precat[óo]rio|of[íi]cio requisit[óo]rio|\mrpv\M|requisi[çc][ãa]o de pagamento'

DATAJUD_ALVO = ['TJPR', 'TRF4', 'TRF6', 'TRF2']


class Command(BaseCommand):
    help = 'Fase 0: marca tem_sinal_precatorio pelo texto DJEN (por janela de pk).'

    def add_arguments(self, p):
        p.add_argument('--tribunais', default=','.join(DATAJUD_ALVO),
                       help='CSV de siglas (default: alvo datajud).')
        p.add_argument('--batch', type=int, default=2000,
                       help=('linhas por lote. Era 20.000 e virou 12,7 h numa '
                             'transação só no TJSP — o custo é por MOVIMENTAÇÃO, '
                             'não por processo, e varia 100x entre tribunais.'))
        p.add_argument('--sleep', type=float, default=0.2, help='pausa entre janelas (s)')
        p.add_argument('--min-id', type=int, default=None)
        p.add_argument('--max-id', type=int, default=None)
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--statement-timeout', default='120s',
                       help=('teto por LOTE. O EXISTS com regex sobre o texto das '
                             'movimentações custa proporcional ao tamanho do '
                             'processo; sem teto um lote de TJSP rodou 12,7 h e '
                             'travou outros três jobs (27/08/2026).'))
        p.add_argument('--lock-timeout', default='5s')

    def handle(self, *a, **o):
        tribs = [s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
        batch, sleep = o['batch'], o['sleep']
        self.stdout.write(f'alvo={tribs} batch={batch} sleep={sleep}s'
                          + (' DRY-RUN' if o['dry_run'] else ''), ending='\n')
        self.stdout.flush()
        if o['dry_run']:
            return

        # Pega EXATAMENTE `batch` linhas ainda-NULL do alvo (usa o índice composto
        # proc_trib_sinalprec_idx) e marca só essas por pk. Batch pequeno = transação
        # curta + progresso visível + custo previsível (~batch × 7ms). Resumível.
        janela = [o['min_id'], o['max_id']]
        sql_pick = ("SELECT id FROM tribunals_process "
                    "WHERE tribunal_id = ANY(%s) AND tem_sinal_precatorio IS NULL "
                    "AND data_enriquecimento_datajud IS NULL "
                    + ("AND id >= %s " if janela[0] is not None else "")
                    + ("AND id < %s " if janela[1] is not None else "")
                    + "LIMIT %s FOR UPDATE SKIP LOCKED")
        sql_upd = ("UPDATE tribunals_process p SET tem_sinal_precatorio = EXISTS ("
                   "SELECT 1 FROM tribunals_movimentacao m "
                   "WHERE m.processo_id = p.id AND m.texto ~* %s) WHERE p.id = ANY(%s)")
        t0 = time.monotonic()
        feitos = 0
        while True:
            params = [tribs] + [x for x in janela if x is not None] + [batch]
            # pick+update na MESMA transação: o FOR UPDATE SKIP LOCKED segura o lote
            # até o UPDATE commitar, então cópias paralelas pegam lotes disjuntos.
            with transaction.atomic():
                with connection.cursor() as c:
                    # TETO DE ESPERA no lote. Sem isto, um único UPDATE ficou
                    # **12,7 HORAS** rodando em produção (27/08/2026, TJSP): o
                    # `EXISTS` com regex sobre `m.texto` custa proporcional ao
                    # número de movimentações do processo, e processo de TJSP tem
                    # muitas. Enquanto durou, ele segurou row-lock em 20.000
                    # linhas de `tribunals_process` e travou TRÊS outros jobs —
                    # o `DROP INDEX CONCURRENTLY` (12,6 h esperando), o backfill
                    # de `assunto_id` (2,6 h) e dois de `classificacao` (1,5 h
                    # cada), todos em `wait_event_type=Lock`.
                    #
                    # Com teto, o lote morre sozinho e a transação solta os
                    # locks: o comando registra o erro e segue, em vez de virar
                    # um bloqueio de meio dia que ninguém vê. Regra nº 7 —
                    # nada sem teto de espera.
                    c.execute('SET LOCAL lock_timeout = %s', [o['lock_timeout']])
                    c.execute('SET LOCAL statement_timeout = %s',
                              [o['statement_timeout']])
                    c.execute(sql_pick, params)
                    ids = [r[0] for r in c.fetchall()]
                    if ids:
                        c.execute(sql_upd, [PADRAO, ids])
            if not ids:
                break
            feitos += len(ids)
            dt = time.monotonic() - t0
            self.stdout.write(f'  processados {feitos:,} · {feitos/dt:.0f}/s '
                              f'· {dt/60:.1f}min', ending='\n')
            self.stdout.flush()
            if len(ids) < batch:
                break
            if sleep:
                time.sleep(sleep)
        with connection.cursor() as c:
            c.execute("SELECT count(*) FROM tribunals_process WHERE tribunal_id = ANY(%s) "
                      "AND tem_sinal_precatorio IS TRUE", [tribs])
            com = c.fetchone()[0]
        self.stdout.write(self.style.SUCCESS(
            f'FIM: {feitos:,} processados · {com:,} com sinal · {(time.monotonic()-t0)/60:.1f}min'))
