"""Enfileira enriquecimento dos Processes ainda pendentes.

Filtros:
  --tribunal SIGLA      restringe ao tribunal (default: todos com enricher)
  --status STATUS       enriquecimento_status alvo (default: pendente)
  --ano-de N            ano CNJ mínimo (útil pra evitar pré-PJe)
  --limit N             limita quantos enfileirar (default 1000, 0 = todos)
  --dry-run             só conta, não enfileira
"""
from django.core.management.base import BaseCommand

from enrichers.jobs import _ENRICHERS, enqueue_enriquecimento
from tribunals.models import Process


class Command(BaseCommand):
    help = 'Enfileira jobs de enriquecimento pra processos pendentes.'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default=None)
        parser.add_argument('--status', default=Process.ENRIQ_PENDENTE)
        parser.add_argument('--ano-de', type=int, default=None, dest='ano_de')
        parser.add_argument('--limit', type=int, default=1000)
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')

    def handle(self, *args, tribunal, status, ano_de, limit, dry_run, **opts):
        siglas = [tribunal] if tribunal else list(_ENRICHERS.keys())
        qs = Process.objects.filter(
            tribunal_id__in=siglas,
            enriquecimento_status=status,
        )
        if ano_de is not None:
            qs = qs.filter(ano_cnj__gte=ano_de)

        # Prioriza processos com mais movs (mais "ativos") — maior valor por job
        qs = qs.order_by('-total_movimentacoes', '-ultima_movimentacao_em')
        if limit > 0:
            qs = qs[:limit]

        # Buscar pid + sigla juntos pra rotear cada job pra fila do tribunal.
        rows = list(qs.values_list('pk', 'tribunal_id'))
        total = len(rows)
        self.stdout.write(self.style.HTTP_INFO(
            f'{total} processos elegíveis (tribunais={siglas}, status={status}'
            + (f', ano>={ano_de}' if ano_de else '') + ')'
        ))
        if dry_run or total == 0:
            return

        enfileirados = 0
        por_fila = {}
        for pid, sigla in rows:
            try:
                enqueue_enriquecimento(pid, sigla)
                enfileirados += 1
                por_fila[sigla] = por_fila.get(sigla, 0) + 1
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  falha em {pid}: {exc}'))
        for sigla, n in sorted(por_fila.items()):
            self.stdout.write(self.style.SUCCESS(f'  enrich_{sigla.lower()}: {n:,} jobs'))
        self.stdout.write(self.style.SUCCESS(f'{enfileirados} jobs enfileirados'))
