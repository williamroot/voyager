"""Comando do container `scheduler`: registra crons (idempotente) e mantém o rq-scheduler rodando."""
from django.core.management.base import BaseCommand
from rq_scheduler.scheduler import Scheduler
from rq_scheduler.utils import setup_loghandlers

import django_rq

from djen.scheduler import register_all


class Command(BaseCommand):
    help = 'Registra todos os crons (cancela existentes primeiro) e roda o rq-scheduler em loop.'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=30)

    def handle(self, *args, interval, **opts):
        result = register_all()
        self.stdout.write(self.style.SUCCESS(
            f'schedules: cancelados={result["cancelados"]} novos={result["novos"]}'
        ))
        setup_loghandlers('INFO')
        connection = django_rq.get_connection('default')
        scheduler = Scheduler(connection=connection, interval=interval)
        scheduler.run()
