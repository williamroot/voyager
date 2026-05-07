"""Esvazia a fila 'classificacao' e reenfileira todos os processos de um
tribunal do mais recente para o mais antigo, em batches de 500.

Uso:
  python manage.py reclassificar_trf1_bulk --dry-run
  python manage.py reclassificar_trf1_bulk --apply
  python manage.py reclassificar_trf1_bulk --apply --tribunal TRF3
  python manage.py reclassificar_trf1_bulk --apply --batch-size 1000
"""
from __future__ import annotations

import django_rq
from django.core.management.base import BaseCommand, CommandError

from tribunals.jobs import reclassificar_batch
from tribunals.models import Process


class Command(BaseCommand):
    help = 'Limpa fila classificacao e reenfileira tribunal inteiro do mais recente ao mais antigo.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--tribunal', default='TRF1')
        parser.add_argument('--batch-size', type=int, default=500)

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        dry = opts['dry_run']
        tribunal = opts['tribunal'].upper()
        batch_size = opts['batch_size']

        q = django_rq.get_queue('classificacao')
        pendentes_antes = q.count

        total = Process.objects.filter(tribunal_id=tribunal).count()
        n_batches = (total + batch_size - 1) // batch_size

        self.stdout.write(
            f'tribunal={tribunal} | processos={total:,} | '
            f'batch={batch_size} | batches a enfileirar={n_batches:,}\n'
            f'fila atual (antes): {pendentes_antes:,} jobs'
        )

        if dry:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nenhuma alteração feita.'))
            return

        # 1) Esvazia a fila
        q.empty()
        self.stdout.write(f'fila esvaziada ({pendentes_antes:,} jobs removidos)')

        # 2) Reenfileira do mais recente ao mais antigo
        pids = list(
            Process.objects
            .filter(tribunal_id=tribunal)
            .order_by('-ultima_movimentacao_em', '-id')
            .values_list('id', flat=True)
        )
        self.stdout.write(f'enfileirando {len(pids):,} processos em batches de {batch_size}...')

        enfileirados = 0
        for i in range(0, len(pids), batch_size):
            batch = pids[i:i + batch_size]
            reclassificar_batch.delay(batch)
            enfileirados += 1
            if enfileirados % 500 == 0:
                self.stdout.write(f'  {enfileirados:,}/{n_batches:,} batches enfileirados')

        self.stdout.write(self.style.SUCCESS(
            f'\n=== pronto ===\n'
            f'  {enfileirados:,} batches enfileirados na fila "classificacao"\n'
            f'  ordem: ultima_movimentacao_em DESC (mais recente primeiro)\n'
            f'  workers ativos: python manage.py rqworker classificacao'
        ))
