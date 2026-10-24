"""Reclassifica o TJPA pela regra da Coordenadoria de Precatórios (F41).

Por que é preciso: o cron `reclassificar_por_prioridade` só revisita processo
com movimento depois do veredito. O PRECATORIO do TJPA (quase todo RPV) e o
precatório de verdade parado em PRE_PRECATORIO continuariam onde estão até
sair publicação nova.

O que muda (ver `SINAL_PRECATORIO_TJPA_SQL` e `TETO_PRE_SEM_SINAL_TRIBUNAIS`):
  * Cumprimento (F1) com o F41 → PRECATORIO, em qualquer nível;
  * PRECATORIO sem o F41 → PRE_PRECATORIO (teto: o LR não promove sozinho).

Alvo: Cumprimentos TJPA em PRECATORIO, PRE_PRECATORIO e DIREITO_CREDITORIO;
com `--incluir-nao-lead`, também os NAO_LEAD (~124 mil em 23/10/2026; rode
fora do horário do pull).

O `--dry-run` calcula o F41 com as MESMAS regex e a mesma função do
classificador e mostra para onde cada categoria iria. Não grava nada. O
`--apply` enfileira em `classificacao` só os processos cujo destino muda — e
quem decide de fato é o classificador (`reclassificar_batch`).

Uso:
  python manage.py reclassificar_tjpa_precatorio --dry-run
  python manage.py reclassificar_tjpa_precatorio --dry-run --incluir-nao-lead
  python manage.py reclassificar_tjpa_precatorio --apply [--incluir-nao-lead]
"""
from __future__ import annotations

import collections

import django_rq
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from tribunals.classificador import (
    CLASSES_CUMPRIMENTO,
    REMESSA_OUTROS_MOTIVOS_SQL,
    SINAL_COORDENADORIA_TRIBUNAIS,
    SINAL_PRECATORIO_TJPA_SQL,
    TETO_PRE_SEM_SINAL_TRIBUNAIS,
    TIPO_REMESSA,
    TIPOS_F2_EXPEDICAO,
    f41_precatorio_coord,
)
from tribunals.jobs import reclassificar_batch

TRIBUNAL = 'TJPA'

# F1 = classe efetiva de Cumprimento, pela fase OU pelo legado (a união do
# `_classe_efetiva`).
_ALVO_SQL = """
    SELECT id, classificacao FROM tribunals_process
    WHERE tribunal_id = %(trib)s AND classificacao = ANY(%(classifs)s)
      AND (COALESCE(fase_codigo, '') = ANY(%(cumprim)s)
           OR COALESCE(classe_codigo, '') = ANY(%(cumprim)s))
"""
# Mesmo recorte do `_MOVS_AGG_SQL` (`f41_n`, `remessa_f41_dt`, `exped_tc_min_dt`).
_F41_SQL = """
    SELECT processo_id,
           COUNT(*) FILTER (WHERE texto ~* %(sinal)s),
           MAX(data_disponibilizacao) FILTER (
               WHERE tipo_comunicacao = %(tipo_remessa)s AND texto ~* %(remessa)s),
           MIN(data_disponibilizacao) FILTER (WHERE tipo_comunicacao = ANY(%(tipos_f2)s))
    FROM tribunals_movimentacao
    WHERE processo_id = ANY(%(pids)s)
    GROUP BY processo_id
"""

CLASSIFS_PADRAO = ['PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO']


def destino(atual: str, f41: int) -> tuple[str, str]:
    """(motivo, destino) — espelha a regra de sinal e o teto do classificador."""
    if f41:
        return 'F41', 'PRECATORIO'
    if atual == 'PRECATORIO' and TRIBUNAL in TETO_PRE_SEM_SINAL_TRIBUNAIS:
        return 'teto', 'PRE_PRECATORIO'
    return '-', atual


class Command(BaseCommand):
    help = 'Reclassifica o TJPA pela regra da Coordenadoria de Precatórios (F41).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--incluir-nao-lead', action='store_true')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--lote-sql', type=int, default=1000,
                            help='processos por consulta de movimentações')
        parser.add_argument('--sql-timeout', type=int, default=600,
                            help='statement_timeout de cada consulta (s)')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        if TRIBUNAL not in SINAL_COORDENADORIA_TRIBUNAIS:
            raise CommandError(f'{TRIBUNAL} fora de SINAL_COORDENADORIA_TRIBUNAIS.')
        classifs = CLASSIFS_PADRAO + (['NAO_LEAD'] if opts['incluir_nao_lead'] else [])

        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s', [f"{opts['sql_timeout']}s"])
            cur.execute(_ALVO_SQL, {'trib': TRIBUNAL, 'classifs': classifs,
                                    'cumprim': sorted(CLASSES_CUMPRIMENTO)})
            atual = dict(cur.fetchall())
        pids = sorted(atual)
        self.stdout.write(f'tribunal={TRIBUNAL} | {"+".join(classifs)} | alvo={len(pids)}')

        f41 = self._f41(pids, opts['lote_sql'], opts['sql_timeout'])
        tab = collections.Counter()
        mudam = []
        for pid in pids:
            motivo, dest = destino(atual[pid], f41.get(pid, 0))
            tab[(atual[pid], motivo, dest)] += 1
            if dest != atual[pid]:
                mudam.append(pid)
        for (cat, motivo, dest), n in sorted(tab.items()):
            self.stdout.write(f'{TRIBUNAL} {cat:20s} {motivo:4s} -> {dest:20s} {n}')
        antes = collections.Counter(atual.values())
        depois = collections.Counter()
        for (_cat, _motivo, dest), n in tab.items():
            depois[dest] += n
        for cat in classifs:
            self.stdout.write(f'  {cat:20s} antes={antes[cat]:7d} depois={depois[cat]:7d}')
        self.stdout.write(f'mudam={len(mudam)}')

        if opts['dry_run']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado.'))
            return

        q = django_rq.get_queue('classificacao')
        bs = opts['batch_size']
        for i in range(0, len(mudam), bs):
            q.enqueue(reclassificar_batch, mudam[i:i + bs])
        self.stdout.write(self.style.SUCCESS(
            f'enfileirados {(len(mudam) + bs - 1) // bs} batches ({len(mudam)} processos).'))

    def _f41(self, pids, lote, sql_timeout) -> dict[int, int]:
        params = {
            'sinal': SINAL_PRECATORIO_TJPA_SQL,
            'tipo_remessa': TIPO_REMESSA,
            'remessa': REMESSA_OUTROS_MOTIVOS_SQL,
            'tipos_f2': list(TIPOS_F2_EXPEDICAO),
        }
        out = {}
        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s', [f'{sql_timeout}s'])
            for i in range(0, len(pids), lote):
                cur.execute(_F41_SQL, {**params, 'pids': pids[i:i + lote]})
                for pid, sinal_n, remessa_dt, exped_min in cur.fetchall():
                    out[pid] = f41_precatorio_coord(sinal_n, remessa_dt, exped_min)
        return out
