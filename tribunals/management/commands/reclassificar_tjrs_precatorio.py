"""Reclassifica os processos do TJRS que têm a intimação da expedição do
precatório no DJEN — para a regra de sinal F31 promovê-los a PRECATORIO.

Por que é preciso um backfill: o cron `reclassificar_por_prioridade` só pega
processo com classificação vencida (`classificacao_em < ultima_movimentacao_em`)
ou nunca classificado. Medido em 2026-10-14: 18.480 processos TJRS têm o sinal,
15.529 deles classificados como NAO_LEAD — o cron não os revisita, e a regra
nova só valeria para movimento futuro.

O alvo é quem TEM o sinal (não todo Cumprimento do TJRS): a regra só promove
com F31 e F1, então reclassificar o resto não muda nada e gasta a fila. A
varredura usa a mesma regex do classificador (fonte única) e o índice por
tribunal da movimentação — ~5 min no TJRS inteiro.

15/10/2026: o mesmo alvo cobre o F32 (precatório convertido em RPV) — todo
processo com F32 tem F31, por definição — e o F31 ampliado (eventos "precatório
preparada para envio/enviada ao tribunal"). Rodar de novo depois do deploy
rebaixa os convertidos (~274) e promove os que só tinham o evento (~322).

16/10/2026: o F31 passou a ser só o evento de ENVIO, e a intimação da
expedição (a minuta) virou o F33, com teto PRE_PRECATORIO. O alvo agora é
F31 OU F33 — sem isso, os processos que só têm a minuta (e hoje estão em
PRECATORIO) nunca seriam revisitados. O `--dry-run` mostra quantos processos em
PRECATORIO só têm a minuta, isto é, quantos descem a PRE_PRECATORIO.

Uso:
  python manage.py reclassificar_tjrs_precatorio --dry-run
  python manage.py reclassificar_tjrs_precatorio --apply
  python manage.py reclassificar_tjrs_precatorio --apply --batch-size 500
"""
from __future__ import annotations

import django_rq
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from tribunals.classificador import (
    INTIMACAO_EXPEDICAO_PRECATORIO_TJRS_SQL,
    SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL,
)
from tribunals.jobs import reclassificar_batch

TRIBUNAL = 'TJRS'

# `strpos(..., 'precat')` corta antes da regex: as duas famílias citam
# "precat" e a regex com alternâncias de entidade é a parte cara da varredura.
_ALVO_SQL = """
    SELECT m.processo_id,
           bool_or(m.texto ~* %(envio)s) AS envio,
           bool_or(m.texto ~* %(minuta)s) AS minuta
    FROM tribunals_movimentacao m
    WHERE m.tribunal_id = %(tribunal)s
      AND strpos(lower(m.texto), 'precat') > 0
      AND (m.texto ~* %(envio)s OR m.texto ~* %(minuta)s)
    GROUP BY m.processo_id
    ORDER BY m.processo_id
"""

_CLASSIF_SQL = """
    SELECT id, classificacao FROM tribunals_process WHERE id = ANY(%s)
"""


class Command(BaseCommand):
    help = 'Reclassifica processos TJRS com sinal de expedição (F31) em batches.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--sql-timeout', type=int, default=900,
                            help='statement_timeout da varredura (s)')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        batch_size = opts['batch_size']

        with connection.cursor() as cur:
            cur.execute('SET statement_timeout = %s',
                        [f"{opts['sql_timeout']}s"])
            cur.execute(_ALVO_SQL, {
                'tribunal': TRIBUNAL,
                'envio': SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL,
                'minuta': INTIMACAO_EXPEDICAO_PRECATORIO_TJRS_SQL,
            })
            linhas = cur.fetchall()
            pids = [r[0] for r in linhas]
            if opts['dry_run']:
                cur.execute(_CLASSIF_SQL, [pids])
                classif = dict(cur.fetchall())

        total = len(pids)
        n_batches = (total + batch_size - 1) // batch_size
        self.stdout.write(f'tribunal={TRIBUNAL} | alvo={total} | '
                          f'batch={batch_size} | batches={n_batches}')

        if opts['dry_run']:
            com_envio = sum(1 for _pid, envio, _m in linhas if envio)
            so_minuta = [pid for pid, envio, minuta in linhas if minuta and not envio]
            descem = sum(1 for pid in so_minuta if classif.get(pid) == 'PRECATORIO')
            self.stdout.write(f'com envio (F31)={com_envio} | só minuta (F33)={len(so_minuta)} | '
                              f'só minuta hoje em PRECATORIO (descem a PRE_PRECATORIO)={descem}')
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado.'))
            return

        q = django_rq.get_queue('classificacao')
        for i in range(0, total, batch_size):
            q.enqueue(reclassificar_batch, pids[i:i + batch_size])
        self.stdout.write(self.style.SUCCESS(
            f'enfileirados {n_batches} batches ({total} processos).'))
