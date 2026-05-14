"""CLI wrapper do job `gerar_lotes_semanais_fn` (T21).

Uso:
    python manage.py gerar_lotes_semanais_fn \\
        [--tribunais TRF1,TRF3] [--tamanho 300] [--no-notificar] [--sync]

Por default enfileira na fila `default`. Use `--sync` para rodar inline
no processo atual (útil em dev / smoke test).
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from tribunals.jobs import gerar_lotes_semanais_fn


class Command(BaseCommand):
    help = 'Dispara pipeline semanal de mineração FN + criação de lotes de validação.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tribunais', default='',
            help='Lista de siglas separadas por vírgula. Default: todos ativos.',
        )
        parser.add_argument(
            '--tamanho', type=int, default=300,
            help='Tamanho alvo por tribunal (default: 300).',
        )
        parser.add_argument(
            '--no-notificar', action='store_true',
            help='Não envia notificações (Slack/email).',
        )
        parser.add_argument(
            '--sync', action='store_true',
            help='Roda inline (não enfileira). Útil em dev.',
        )

    def handle(self, *args, **opts):
        tribs_arg = (opts.get('tribunais') or '').strip()
        tribunais = None
        if tribs_arg:
            tribunais = [s.strip().upper() for s in tribs_arg.split(',') if s.strip()]
        tamanho = int(opts['tamanho'])
        notificar = not opts['no_notificar']
        sync = bool(opts['sync'])

        self.stdout.write(self.style.NOTICE(
            f'gerar_lotes_semanais_fn  tribunais={tribunais or "TODOS_ATIVOS"}  '
            f'tamanho={tamanho}  notificar={notificar}  sync={sync}'
        ))

        if sync:
            resultados = gerar_lotes_semanais_fn(
                tribunais=tribunais,
                tamanho_por_tribunal=tamanho,
                notificar=notificar,
            )
            self.stdout.write(self.style.SUCCESS('Resultados:'))
            self.stdout.write(json.dumps(resultados, indent=2, ensure_ascii=False, default=str))
        else:
            job = gerar_lotes_semanais_fn.delay(
                tribunais=tribunais,
                tamanho_por_tribunal=tamanho,
                notificar=notificar,
            )
            self.stdout.write(self.style.SUCCESS(
                f'job enfileirado · id={job.id} · fila=default'
            ))
