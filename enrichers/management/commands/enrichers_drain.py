"""Drainer service: consome o stream de resultados e aplica em bulk.

Roda como serviço de longa duração (1 réplica). Workers só publicam — só
o drainer escreve no Postgres. Elimina contenção de LWLock que tinhamos
com ~500 enrichers concorrentes.
"""
from django.core.management.base import BaseCommand

from enrichers import drainer


class Command(BaseCommand):
    help = 'Drena o stream de resultados de enrichment e aplica em batch.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=200,
                            help='Tamanho máximo do batch por iteração.')
        parser.add_argument('--block-ms', type=int, default=2000,
                            help='Timeout de XREADGROUP em ms (poll interval).')
        parser.add_argument('--idle-ms', type=int, default=60_000,
                            help='XAUTOCLAIM: pega entries idle há X ms (consumer travou).')
        parser.add_argument('--no-trim', action='store_true', dest='no_trim',
                            help='Não chama XDEL após ack — mantém histórico no stream.')

    def handle(self, *args, **opts):
        drainer.run(
            batch_size=opts['batch_size'],
            block_ms=opts['block_ms'],
            idle_ms=opts['idle_ms'],
            trim_after_ack=not opts['no_trim'],
        )
