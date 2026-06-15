"""Reclassifica os Cumprimentos do TJAL (1º grau) — para a regra de sinal
promover a PRECATORIO os que têm ofício requisitório/expedição. Enfileira
reclassificar_batch na fila 'classificacao', do mais recente ao mais antigo.

Uso:
  python manage.py reclassificar_tjal_precatorio --dry-run
  python manage.py reclassificar_tjal_precatorio --apply
  python manage.py reclassificar_tjal_precatorio --apply --batch-size 500
"""
from __future__ import annotations

import django_rq
from django.core.management.base import BaseCommand, CommandError

from tribunals.classificador import CLASSES_CUMPRIMENTO
from tribunals.jobs import reclassificar_batch
from tribunals.models import Process

TRIBUNAL = 'TJAL'
# Fonte única: as classes de Cumprimento do classificador (evita divergência).
CLASSES = sorted(CLASSES_CUMPRIMENTO)


class Command(BaseCommand):
    help = 'Reclassifica Cumprimentos TJAL (regra de sinal) em batches.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=500)

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        batch_size = opts['batch_size']

        qs = (Process.objects
              .filter(tribunal_id=TRIBUNAL, classe_codigo__in=CLASSES)
              .order_by('-ultima_movimentacao_em', '-id'))
        total = qs.count()
        n_batches = (total + batch_size - 1) // batch_size
        self.stdout.write(f'tribunal={TRIBUNAL} classes={CLASSES} | '
                          f'alvo={total} | batch={batch_size} | '
                          f'batches={n_batches}')

        if opts['dry_run']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado.'))
            return

        pids = list(qs.values_list('id', flat=True))
        q = django_rq.get_queue('classificacao')
        for i in range(0, len(pids), batch_size):
            q.enqueue(reclassificar_batch, pids[i:i + batch_size])
        self.stdout.write(self.style.SUCCESS(
            f'enfileirados {n_batches} batches ({total} processos).'))
