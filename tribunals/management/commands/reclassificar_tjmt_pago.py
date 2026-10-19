"""Reclassifica o TJMT pelos vetos de pagamento (F39 e F40).

Por que é preciso: o cron `reclassificar_por_prioridade` só revisita processo
com movimento depois do veredito, e o estoque do TJMT em PRECATORIO é quase
todo antigo (última publicação em 2023 a 2025). Sem este backfill os 5,2 mil
PRECATORIO continuariam lá — e o PRE que o pull do TJMT serve hoje seguiria
trazendo execução já extinta pela satisfação.

Alvo:
  * todo PRECATORIO dos tribunais do F39/F40;
  * com `--incluir-pre`, também PRE_PRECATORIO e DIREITO_CREDITORIO (só o F39
    vale nesses níveis). São ~165 mil no TJMT: rode fora do horário do pull.

O `--dry-run` calcula as datas com as MESMAS regex e as mesmas funções do
classificador e mostra para onde cada categoria iria. Não grava nada.

Uso:
  python manage.py reclassificar_tjmt_pago --dry-run
  python manage.py reclassificar_tjmt_pago --apply
  python manage.py reclassificar_tjmt_pago --apply --incluir-pre
"""
from __future__ import annotations

import collections

import django_rq
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from tribunals.classificador import (
    EXTINCAO_SATISFEITA_SQL,
    EXTINCAO_SATISFEITA_VETO_SQL,
    F20_EXPEDICAO_SQL,
    INSS_NOME_PARTE_RE,
    INSS_PASSIVO_SQL,
    RPV_FEDERAL_PARADA_TRIBUNAIS,
    SATISFACAO_SINAL_TRIBUNAIS,
    f39_extinto_satisfeito,
    f40_rpv_federal_parada,
)
from tribunals.jobs import reclassificar_batch

_ALVO_SQL = """
    SELECT id FROM tribunals_process
    WHERE tribunal_id = ANY(%s) AND classificacao = ANY(%s)
"""
# Mesmo recorte do `_MOVS_AGG_SQL`: o regex do F39 só nos tribunais dele.
_DATAS_SQL = """
    SELECT m.processo_id,
           MAX(m.data_disponibilizacao) FILTER (
               WHERE m.tribunal_id = ANY(%(tribs_f39)s)
                 AND m.texto ~* %(satisf)s AND m.texto !~* %(satisf_veto)s),
           MAX(m.data_disponibilizacao) FILTER (WHERE m.texto ~* %(f20)s),
           COALESCE(BOOL_OR(m.tribunal_id = ANY(%(tribs_f40)s)
                            AND m.texto ~* %(inss)s), FALSE),
           MAX(m.data_disponibilizacao)
    FROM tribunals_movimentacao m
    WHERE m.processo_id = ANY(%(pids)s)
    GROUP BY m.processo_id
"""
_PARTES_SQL = """
    SELECT pp.processo_id, pa.nome
    FROM tribunals_processoparte pp
    JOIN tribunals_parte pa ON pa.id = pp.parte_id
    WHERE pp.processo_id = ANY(%s) AND pp.polo = 'passivo'
"""
_CLASSIF_SQL = 'SELECT id, tribunal_id, classificacao FROM tribunals_process WHERE id = ANY(%s)'


class Command(BaseCommand):
    help = 'Reclassifica o TJMT pelos vetos de pagamento (F39/F40) em batches.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--incluir-pre', action='store_true')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--sql-timeout', type=int, default=1800,
                            help='statement_timeout das varreduras (s)')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        tribunais = sorted(SATISFACAO_SINAL_TRIBUNAIS | RPV_FEDERAL_PARADA_TRIBUNAIS)
        classifs = ['PRECATORIO'] + (
            ['PRE_PRECATORIO', 'DIREITO_CREDITORIO'] if opts['incluir_pre'] else [])

        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s', [f"{opts['sql_timeout']}s"])
            cur.execute(_ALVO_SQL, [tribunais, classifs])
            pids = sorted(r[0] for r in cur.fetchall())
        self.stdout.write(
            f'tribunais={",".join(tribunais)} | {"+".join(classifs)} | alvo={len(pids)}')

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
        datas, inss_partes, classif = {}, set(), {}
        params = {
            'tribs_f39': sorted(SATISFACAO_SINAL_TRIBUNAIS),
            'tribs_f40': sorted(RPV_FEDERAL_PARADA_TRIBUNAIS),
            'satisf': EXTINCAO_SATISFEITA_SQL,
            'satisf_veto': EXTINCAO_SATISFEITA_VETO_SQL,
            'f20': F20_EXPEDICAO_SQL,
            'inss': INSS_PASSIVO_SQL,
        }
        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s', [f'{sql_timeout}s'])
            for i in range(0, len(pids), 1000):
                lote = pids[i:i + 1000]
                cur.execute(_DATAS_SQL, {**params, 'pids': lote})
                for pid, satisf, exped, inss_txt, ult in cur.fetchall():
                    datas[pid] = (satisf, exped, inss_txt, ult)
                cur.execute(_PARTES_SQL, [lote])
                inss_partes.update(pid for pid, nome in cur.fetchall()
                                   if INSS_NOME_PARTE_RE.search(nome or ''))
                cur.execute(_CLASSIF_SQL, [lote])
                classif.update((pid, (trib, cat)) for pid, trib, cat in cur.fetchall())

        tab = collections.Counter()
        for pid in pids:
            trib, atual = classif.get(pid, ('', '-'))
            satisf, exped, inss_txt, ult = datas.get(pid, (None, None, False, None))
            f39 = trib in SATISFACAO_SINAL_TRIBUNAIS and f39_extinto_satisfeito(satisf, exped)
            f40 = (trib in RPV_FEDERAL_PARADA_TRIBUNAIS
                   and f40_rpv_federal_parada(inss_txt or pid in inss_partes, ult, agora))
            if f39:
                motivo, destino = 'F39', 'NAO_LEAD'
            elif f40 and atual == 'PRECATORIO':
                motivo, destino = 'F40', 'NAO_LEAD'
            else:
                motivo, destino = '-', f'{atual} (LR)'
            tab[(trib, atual, motivo, destino)] += 1
        for (trib, atual, motivo, destino), n in sorted(tab.items()):
            self.stdout.write(f'{trib:6s} {atual:20s} {motivo:4s} -> {destino:26s} {n}')
        self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado.'))
