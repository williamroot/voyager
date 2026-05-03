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
            warm_charts,
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

        for warm_job in (warm_kpis, warm_charts, warm_ingestao_por_hora,
                         warm_partes, warm_estatisticas_tribunal,
                         warm_filtros_movimentacoes):
            warm_job.delay()
        logger.info('aquecimento inicial do dashboard enfileirado (6 jobs)')

        scheduler = create_scheduler()
        self.stdout.write(self.style.SUCCESS(
            f'APScheduler iniciado · {len(ativos)} tribunais ativos'
        ))

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            self.stdout.write('Scheduler encerrado.')
