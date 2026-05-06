"""Comando do container `scheduler`: registra crons via APScheduler e roda em loop.

Ao iniciar, dispara imediatamente o primeiro tick de backfill retroativo para
TRF1 e TRF3 — depois o cron de 10 em 10 min assume.
"""
import logging

from django.core.management.base import BaseCommand

from djen.scheduler import create_scheduler
from tribunals.models import Tribunal

logger = logging.getLogger('voyager.djen.scheduler')


class Command(BaseCommand):
    help = 'Registra todos os crons (APScheduler) e roda o scheduler em loop.'

    def handle(self, *args, **opts):
        from dashboard.tasks import (
            warm_charts_leves,
            warm_charts_pesados,
            warm_estatisticas_tribunal,
            warm_filtros_movimentacoes,
            warm_ingestao_por_hora,
            warm_kpis,
            warm_partes,
        )
        from djen.jobs import tick_backfill_retroativo

        # Kick inicial: enfileira o primeiro tick pra cada tribunal ativo.
        # Sem isso teríamos que esperar até 10min pelo primeiro disparo do cron.
        ativos = list(Tribunal.objects.filter(ativo=True).order_by('sigla'))
        for t in ativos:
            tick_backfill_retroativo.delay(t.sigla)
            logger.info('tick inicial enfileirado para %s', t.sigla)

        from djen.scheduler import _enqueue_singleton
        for warm_job, job_id in (
            (warm_kpis,                  'warm_kpis'),
            (warm_charts_leves,          'warm_charts_leves'),
            (warm_charts_pesados,        'warm_charts_pesados'),
            (warm_ingestao_por_hora,     'warm_ingestao_por_hora'),
            (warm_partes,                'warm_partes'),
            (warm_estatisticas_tribunal, 'warm_estatisticas_tribunal'),
            (warm_filtros_movimentacoes, 'warm_filtros_movimentacoes'),
        ):
            _enqueue_singleton(warm_job, 'warm', job_id)
        logger.info('aquecimento inicial do dashboard enfileirado (via singleton)')

        scheduler = create_scheduler()
        self.stdout.write(self.style.SUCCESS(
            f'APScheduler iniciado · {len(ativos)} tribunais ativos'
        ))

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            self.stdout.write('Scheduler encerrado.')
