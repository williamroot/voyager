"""Reclassifica o TRF3 pela janela da minuta (F35-F38, `estado_trf3`).

Por que é preciso: o cron `reclassificar_por_prioridade` só revisita processo
com movimento depois do veredito. Sem este backfill, os ~14,7 mil TRF3 em
PRECATORIO (entregues pelo LR no dia da minuta, ou pela revisão do grupo 2)
continuariam lá, e as minutas recentes classificadas antes do deploy não
entrariam na espera.

Alvo:
  * todo TRF3 em PRECATORIO;
  * todo TRF3 com minuta ou envio publicado nos últimos `--dias` (padrão 45):
    é quem pode estar na espera ou na janela hoje;
  * com `--incluir-pre`, também todo TRF3 em PRE_PRECATORIO — para o veto de
    pagamento (F38) tirar do pull de PRE as RPV já pagas (0,2% ativa na
    medição). São ~263 mil processos: rode fora do horário do pull.

O `--dry-run` calcula o estado de cada alvo com as mesmas regex e a mesma
função do classificador e mostra para onde cada categoria iria.

Uso:
  python manage.py reclassificar_trf3_janela --dry-run
  python manage.py reclassificar_trf3_janela --apply
  python manage.py reclassificar_trf3_janela --apply --incluir-pre
"""
from __future__ import annotations

import collections

import django_rq
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from tribunals.classificador import (
    CANCELAMENTO_TRF3_SQL,
    ENVIO_TRF3_SQL,
    ESTADO_TRF3_ESPERA,
    ESTADO_TRF3_JANELA,
    ESTADO_TRF3_PAGO,
    MINUTA_TRF3_SQL,
    PAGO_TRF3_SQL,
    PAGO_VETO_TRF3_SQL,
    estado_trf3,
)
from tribunals.jobs import reclassificar_batch

TRIBUNAL = 'TRF3'

_RECENTES_SQL = """
    SELECT DISTINCT processo_id FROM tribunals_movimentacao
    WHERE tribunal_id = %(tribunal)s
      AND data_disponibilizacao >= now() - make_interval(days => %(dias)s)
      AND (texto ~* %(minuta)s OR texto ~* %(envio)s)
"""
_POR_CLASSIF_SQL = """
    SELECT id FROM tribunals_process WHERE tribunal_id = %s AND classificacao = ANY(%s)
"""
_DATAS_SQL = """
    SELECT m.processo_id,
           MAX(m.data_disponibilizacao) FILTER (WHERE m.texto ~* %(minuta)s),
           MAX(m.data_disponibilizacao) FILTER (WHERE m.texto ~* %(envio)s),
           MAX(m.data_disponibilizacao) FILTER (WHERE m.texto ~* %(pago)s
                                                  AND m.texto !~* %(veto)s),
           MAX(m.data_disponibilizacao) FILTER (WHERE m.texto ~* %(cancel)s)
    FROM tribunals_movimentacao m
    WHERE m.processo_id = ANY(%(pids)s)
    GROUP BY m.processo_id
"""
_CLASSIF_SQL = 'SELECT id, classificacao FROM tribunals_process WHERE id = ANY(%s)'

_DESTINO = {
    ESTADO_TRF3_PAGO: 'NAO_LEAD',
    ESTADO_TRF3_JANELA: 'PRECATORIO',
    ESTADO_TRF3_ESPERA: 'PRE (espera)',
}


class Command(BaseCommand):
    help = 'Reclassifica o TRF3 pela janela da minuta (F35-F38) em batches.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--incluir-pre', action='store_true')
        parser.add_argument('--dias', type=int, default=45)
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--sql-timeout', type=int, default=1800,
                            help='statement_timeout das varreduras (s)')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        classifs = ['PRECATORIO'] + (['PRE_PRECATORIO'] if opts['incluir_pre'] else [])

        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s', [f"{opts['sql_timeout']}s"])
            cur.execute(_POR_CLASSIF_SQL, [TRIBUNAL, classifs])
            alvo = {r[0] for r in cur.fetchall()}
            n_classif = len(alvo)
            cur.execute(_RECENTES_SQL, {'tribunal': TRIBUNAL, 'dias': opts['dias'],
                                        'minuta': MINUTA_TRF3_SQL, 'envio': ENVIO_TRF3_SQL})
            recentes = {r[0] for r in cur.fetchall()}
        alvo |= recentes
        pids = sorted(alvo)
        self.stdout.write(
            f'tribunal={TRIBUNAL} | {"+".join(classifs)}={n_classif} | '
            f'minuta/envio em {opts["dias"]} dias={len(recentes)} | alvo={len(pids)}')

        if opts['dry_run']:
            self._dry_run(pids, opts['sql_timeout'])
            return

        q = django_rq.get_queue('classificacao')
        bs = opts['batch_size']
        for i in range(0, len(pids), bs):
            q.enqueue(reclassificar_batch, pids[i:i + bs])
        self.stdout.write(self.style.SUCCESS(
            f'enfileirados {(len(pids) + bs - 1) // bs} batches ({len(pids)} processos).'))

    def _dry_run(self, pids, sql_timeout):
        agora = timezone.now()
        estados = {}
        classif = {}
        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s', [f'{sql_timeout}s'])
            for i in range(0, len(pids), 1000):
                lote = pids[i:i + 1000]
                cur.execute(_DATAS_SQL, {'minuta': MINUTA_TRF3_SQL, 'envio': ENVIO_TRF3_SQL,
                                         'pago': PAGO_TRF3_SQL, 'veto': PAGO_VETO_TRF3_SQL,
                                         'cancel': CANCELAMENTO_TRF3_SQL, 'pids': lote})
                for pid, minuta, envio, pago, cancel in cur.fetchall():
                    estados[pid] = estado_trf3(minuta, envio, pago, agora, cancel_dt=cancel)
                cur.execute(_CLASSIF_SQL, [lote])
                classif.update(cur.fetchall())
        tab = collections.Counter()
        for pid in pids:
            est = estados.get(pid, '')
            atual = classif.get(pid) or '-'
            if est in _DESTINO:
                destino = _DESTINO[est]
            elif atual == 'PRECATORIO':
                destino = 'PRE (teto)'
            else:
                destino = f'{atual} (LR)'
            tab[(atual, est or 'sem_sinal', destino)] += 1
        for (atual, est, destino), n in sorted(tab.items()):
            self.stdout.write(f'{atual:20s} {est:10s} -> {destino:22s} {n}')
        self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado.'))
