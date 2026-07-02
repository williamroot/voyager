"""Reclassifica os leads do TJMA (PRECATORIO/PRE_PRECATORIO) — para a regra
de sinal NEGATIVA rebaixar a NAO_LEAD os que têm pagamento publicado no DJEN
(alvará de levantamento/sequestro deferido/extinção) posterior ao último
sinal de expedição. Enfileira reclassificar_batch na fila 'classificacao',
do mais recente ao mais antigo.

Uso:
  python manage.py reclassificar_tjma_pagamento --dry-run
  python manage.py reclassificar_tjma_pagamento --apply
  python manage.py reclassificar_tjma_pagamento --apply --batch-size 500
"""
from __future__ import annotations

import django_rq
from django.core.management.base import BaseCommand, CommandError

from tribunals.jobs import reclassificar_batch
from tribunals.models import Process

TRIBUNAL = 'TJMA'
# Só quem está classificado como lead hoje pode ser rebaixado pela regra —
# reprocessar NAO_LEAD seria custo sem efeito.
CLASSIFICACOES = [Process.CLASSIF_PRECATORIO, Process.CLASSIF_PRE_PRECATORIO]


class Command(BaseCommand):
    help = 'Reclassifica leads TJMA (regra de sinal de pagamento) em batches.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=500)

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        batch_size = opts['batch_size']

        qs = (Process.objects
              .filter(tribunal_id=TRIBUNAL, classificacao__in=CLASSIFICACOES)
              .order_by('-ultima_movimentacao_em', '-id'))
        total = qs.count()
        n_batches = (total + batch_size - 1) // batch_size
        self.stdout.write(f'tribunal={TRIBUNAL} '
                          f'classificacoes={CLASSIFICACOES} | '
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
