"""Reclassifica leads já rotulados pra aplicar a regra de sinal de DESFECHO TERMINAL
(F30_extinto_neg_ANTI): processos extintos sem mérito / improcedentes / prescritos que
foram classificados como lead ANTES do F30 (2026-07-08) continuam com rótulo velho e o
Juriscope pode estar consumindo. Este comando re-roda o classificador neles — os
extintos/pagos caem a NAO_LEAD via F30/F24.

Não faz o filtro por regex de texto (a EXISTS ~* sobre milhões de movs dá timeout sob
contenção). Em vez disso, ENFILEIRA `reclassificar_batch` na fila `classificacao` — os
8 workers reprocessam no ritmo deles (bounded por processo), sem brigar numa conexão só.
Idempotente: re-classificar um lead legítimo re-confirma o mesmo rótulo.

Uso (rodar OFF-PEAK, quando o backfill trabalhista assentar):
  python manage.py reclassificar_desfecho --dry-run
  python manage.py reclassificar_desfecho --apply
  python manage.py reclassificar_desfecho --apply --classes PRECATORIO,PRE_PRECATORIO
  python manage.py reclassificar_desfecho --apply --tribunal TJAL --batch-size 500
"""
from __future__ import annotations

import time

import django_rq
from django.core.management.base import BaseCommand

from tribunals.jobs import reclassificar_batch
from tribunals.models import Process

_CLASSES_LEAD = ['PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO']


class Command(BaseCommand):
    help = 'Re-classifica leads pra aplicar a regra de desfecho terminal (F30). Enfileira em batches.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Enfileira de fato (sem isso é dry-run).')
        parser.add_argument('--classes', default='PRECATORIO',
                            help='Classificações a reprocessar, separadas por vírgula. Default: PRECATORIO (N1).')
        parser.add_argument('--tribunal', default=None, help='Restringe a um tribunal (ex.: TJAL).')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--enqueue-sleep', type=float, default=0.05,
                            help='Pausa entre enfileiramentos (rate-limit; não sufoca a fila).')

    def handle(self, *args, **o):
        classes = [c.strip() for c in o['classes'].split(',') if c.strip() in _CLASSES_LEAD]
        if not classes:
            self.stderr.write('Nenhuma classe válida. Use: ' + ','.join(_CLASSES_LEAD))
            return
        qs = Process.objects.filter(classificacao__in=classes)
        if o['tribunal']:
            qs = qs.filter(tribunal_id=o['tribunal'].upper())
        pks = list(qs.values_list('pk', flat=True).iterator(chunk_size=5000))
        total = len(pks)
        bs = o['batch_size']
        n_batches = (total + bs - 1) // bs
        self.stdout.write(f'Leads a reprocessar: {total:,} ({classes}'
                          f"{' @' + o['tribunal'] if o['tribunal'] else ''}) → {n_batches} batches de {bs}")
        if not o['apply']:
            self.stdout.write('DRY-RUN — nada enfileirado. Use --apply pra rodar.')
            return
        q = django_rq.get_queue('classificacao')
        enq = 0
        for i in range(0, total, bs):
            q.enqueue(reclassificar_batch, pks[i:i + bs], job_timeout=1800)
            enq += 1
            if o['enqueue_sleep']:
                time.sleep(o['enqueue_sleep'])
            if enq % 20 == 0:
                self.stdout.write(f'  enfileirados {enq}/{n_batches} batches…')
        self.stdout.write(self.style.SUCCESS(
            f'OK — {enq} batches ({total:,} leads) na fila classificacao. Os workers reprocessam; '
            f'os extintos/pagos caem a NAO_LEAD via F30/F24.'))
