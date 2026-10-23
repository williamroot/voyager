"""Reclassifica processos do TJAL depois de mudança numa regra de sinal.
Enfileira reclassificar_batch na fila 'classificacao', do mais recente ao mais
antigo.

Alvos (`--alvo`):
  cumprimento  (padrão) os Cumprimentos de 1º grau — regra F14/F20.
  pa-comarca   o "Processo Administrativo" (1298) de comarca, numeração 050,
               fora do foro 9003 — regra do órgão de precatórios. Só os que
               ainda não são PRECATORIO.

Uso:
  python manage.py reclassificar_tjal_precatorio --dry-run
  python manage.py reclassificar_tjal_precatorio --apply
  python manage.py reclassificar_tjal_precatorio --apply --batch-size 500
  python manage.py reclassificar_tjal_precatorio --alvo pa-comarca --dry-run
"""
from __future__ import annotations

import django_rq
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from tribunals.classificador import (
    CLASSES_CUMPRIMENTO, CLASSES_PA_PRECATORIO_ORGAO, _RE_CNJ_PA_COMARCA_TJAL,
)
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
        parser.add_argument('--alvo', choices=('cumprimento', 'pa-comarca'),
                            default='cumprimento')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply.')
        batch_size = opts['batch_size']

        if opts['alvo'] == 'pa-comarca':
            classes = sorted(CLASSES_PA_PRECATORIO_ORGAO)
            qs = (Process.objects
                  .filter(tribunal_id=TRIBUNAL)
                  .filter(Q(classe_codigo__in=classes) | Q(fase_codigo__in=classes))
                  .filter(numero_cnj__regex=_RE_CNJ_PA_COMARCA_TJAL.pattern)
                  .exclude(classificacao=Process.CLASSIF_PRECATORIO))
        else:
            classes = CLASSES
            qs = Process.objects.filter(tribunal_id=TRIBUNAL, classe_codigo__in=classes)
        qs = qs.order_by('-ultima_movimentacao_em', '-id')
        total = qs.count()
        n_batches = (total + batch_size - 1) // batch_size
        self.stdout.write(f'tribunal={TRIBUNAL} alvo_regra={opts["alvo"]} classes={classes} | '
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
