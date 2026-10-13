"""Reclassifica, de uma vez, processos cujo veredito é ANTERIOR a uma data.

Por que existe (medido em 13/10/2026): o F1 (Cumprimento) passou a ler a FASE
(`fase_codigo`) além do campo legado em 03–04/09/2026 (b1a5d3a/b1898a3). Quem
foi classificado antes disso e não ganhou movimento novo nunca foi revisitado:
o cron `reclassificar_por_prioridade` só olhava o grupo 2 (vereditos velhos)
quando o grupo 1 esvaziava, o que não acontece. No TRF3 são **16.966**
Cumprimentos-pela-fase com movimento no último ano ainda `NAO_LEAD` (38.576 sem
o recorte de movimento). O mesmo perfil classificado depois do fix sai 17% N2 e
14% DC.

Não reinventa nada: enfileira `tribunals.jobs.reclassificar_batch` na fila
`classificacao`, em lotes, com pausa entre enfileiramentos e freio pela
profundidade da fila — os workers reprocessam no ritmo deles, sem afogar o cron.

Recorte default (todos ajustáveis):
  tribunal TRF3 · classificacao NAO_LEAD · classificacao_em < 2026-09-04 ·
  Cumprimento pela FASE e não pelo campo legado · movimento nos últimos 365 dias

Uso:
  python manage.py reclassificar_classificados_antes --dry-run
  python manage.py reclassificar_classificados_antes --apply
  python manage.py reclassificar_classificados_antes --apply --mov-desde-dias 0
  python manage.py reclassificar_classificados_antes --dry-run --tribunal TJMA \\
      --antes 2026-09-04 --classificacao NAO_LEAD
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import django_rq
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from tribunals.classificador import CLASSES_CUMPRIMENTO
from tribunals.jobs import reclassificar_batch
from tribunals.models import Process

FILA = 'classificacao'


def montar_queryset(tribunal: str, antes, classificacao: str | None,
                    so_cumprimento_pela_fase: bool, mov_desde_dias: int):
    """O recorte. Separado do comando para o teste provar o MESMO filtro."""
    qs = Process.objects.filter(tribunal_id=tribunal, classificacao_em__lt=antes)
    if classificacao:
        qs = qs.filter(classificacao=classificacao)
    if so_cumprimento_pela_fase:
        # Cumprimento pela fase e NÃO pelo legado: é exatamente o que o F1 não
        # via antes do fix. Quem já era Cumprimento pelo legado foi avaliado
        # com F1=1 e não muda.
        classes = sorted(CLASSES_CUMPRIMENTO)
        # (`classe_codigo` é NOT NULL; "sem classe" é string vazia.)
        qs = qs.filter(fase_codigo__in=classes).exclude(classe_codigo__in=classes)
    if mov_desde_dias:
        qs = qs.filter(ultima_movimentacao_em__gte=timezone.now()
                       - timedelta(days=mov_desde_dias))
    return qs.order_by('-ultima_movimentacao_em', '-id')


def _ids_com_teto(qs, segundos: int) -> list[int]:
    """`SET LOCAL` na MESMA transação (pgbouncer em transaction-mode)."""
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [int(segundos * 1000)])
        return list(qs.values_list('id', flat=True))


class Command(BaseCommand):
    help = ('Reclassifica processos com veredito anterior a uma data (default: '
            'Cumprimentos-pela-fase do TRF3 em NAO_LEAD antes de 04/09/2026).')

    def add_arguments(self, parser):
        modo = parser.add_mutually_exclusive_group(required=True)
        modo.add_argument('--dry-run', action='store_true',
                          help='Só conta e mostra o plano. Não enfileira nada.')
        modo.add_argument('--apply', action='store_true', help='Enfileira de fato.')
        parser.add_argument('--tribunal', default='TRF3')
        parser.add_argument('--antes', default='2026-09-04',
                            help='Data (AAAA-MM-DD): classificacao_em < esta data.')
        parser.add_argument('--classificacao', default='NAO_LEAD',
                            help="Nível atual a reprocessar; 'todas' = qualquer.")
        parser.add_argument('--sem-filtro-fase', action='store_true',
                            help='Não exige Cumprimento-pela-fase.')
        parser.add_argument('--mov-desde-dias', type=int, default=365,
                            help='Só quem teve movimento nos últimos N dias (0 = sem recorte).')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--pausa', type=float, default=0.5,
                            help='Segundos entre enfileiramentos.')
        parser.add_argument('--fila-max', type=int, default=400,
                            help='Espera a fila classificacao baixar disto antes de enfileirar.')
        parser.add_argument('--espera-max', type=int, default=3600,
                            help='Desiste (erro) se a fila não baixar em N segundos.')
        parser.add_argument('--sql-timeout', type=int, default=600,
                            help='Teto da consulta de seleção, em segundos.')

    def handle(self, *args, **o):
        try:
            data = datetime.strptime(o['antes'], '%Y-%m-%d')
        except ValueError as exc:
            raise CommandError(f'--antes inválido: {o["antes"]}') from exc
        antes = timezone.make_aware(data)
        classificacao = None if o['classificacao'].lower() == 'todas' else o['classificacao'].upper()
        if classificacao and classificacao not in dict(Process.CLASSIF_CHOICES):
            raise CommandError(f'--classificacao inválida: {classificacao}')
        tribunal = o['tribunal'].upper()
        bs = max(1, o['batch_size'])

        qs = montar_queryset(tribunal, antes, classificacao,
                             not o['sem_filtro_fase'], o['mov_desde_dias'])
        t0 = time.monotonic()
        pids = _ids_com_teto(qs, o['sql_timeout'])
        n_batches = (len(pids) + bs - 1) // bs
        self.stdout.write(
            f'recorte: tribunal={tribunal} classificacao={classificacao or "todas"} '
            f'classificacao_em<{o["antes"]} '
            f'cumprimento_pela_fase={not o["sem_filtro_fase"]} '
            f'mov_desde_dias={o["mov_desde_dias"] or "sem recorte"}')
        self.stdout.write(f'alvo: {len(pids):,} processos → {n_batches} lotes de {bs} '
                          f'(seleção levou {time.monotonic() - t0:.1f}s)')

        if o['dry_run']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada enfileirado.'))
            return
        if not pids:
            self.stdout.write('nada a fazer.')
            return

        q = django_rq.get_queue(FILA)
        enq = 0
        for i in range(0, len(pids), bs):
            self._esperar_fila(q, o['fila_max'], o['espera_max'])
            q.enqueue(reclassificar_batch, pids[i:i + bs], job_timeout=1800)
            enq += 1
            if enq % 10 == 0 or enq == n_batches:
                self.stdout.write(f'  enfileirados {enq}/{n_batches} lotes…')
            if o['pausa']:
                time.sleep(o['pausa'])
        self.stdout.write(self.style.SUCCESS(
            f'OK — {enq} lotes ({len(pids):,} processos) na fila {FILA}.'))

    def _esperar_fila(self, q, fila_max: int, espera_max: int) -> None:
        """Freio: não empilha lote numa fila que já está cheia. Teto de espera é
        ERRO, nunca corte mudo — o operador vê onde parou."""
        if fila_max <= 0:
            return
        inicio = time.monotonic()
        while len(q) >= fila_max:
            if time.monotonic() - inicio > espera_max:
                raise CommandError(
                    f'fila {FILA} ficou ≥ {fila_max} por mais de {espera_max}s — '
                    'parei; rode de novo depois (o que já entrou não se perde, '
                    'reclassificar é idempotente).')
            time.sleep(10)
