"""Deduplica tribunals_parte. Causado por índices únicos parciais que
ficaram INVÁLIDOS (CREATE UNIQUE INDEX CONCURRENTLY que falhou — ver
migration 0017).

Set-based em SQL: Python loop em ~80M linhas é inviável. Idempotente e
resumível — re-rodar após interrupção recalcula e continua.

Anti-homônimo: colapso só por chave EXATA; absorção masc_to_real só com 1
candidato. Survivor = MIN(id) / sempre a Parte de doc real.
"""
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

logger = logging.getLogger('voyager.dedup_partes')

# Grupos de colapso por chave byte-idêntica: nome -> (predicado WHERE, PARTITION BY)
GRUPOS = {
    'oab': ("oab <> ''", 'oab'),
    'doc_real': (
        "documento <> '' AND documento NOT LIKE '%X%' "
        "AND documento NOT LIKE '%x%' AND documento NOT LIKE '%*%'",
        'documento',
    ),
    'doc_masc': (
        "(documento LIKE '%X%' OR documento LIKE '%x%' OR documento LIKE '%*%')",
        'nome, documento',
    ),
}
ORDEM_ALL = ['oab', 'doc_real', 'doc_masc', 'masc_to_real']


class Command(BaseCommand):
    help = 'Deduplica tribunals_parte (anti-homônimo). Ver plano dedup-partes.'

    def add_arguments(self, parser):
        parser.add_argument('--group', choices=ORDEM_ALL + ['all'], default='all')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200_000)

    def handle(self, *args, **opts):
        grupos = ORDEM_ALL if opts['group'] == 'all' else [opts['group']]
        for g in grupos:
            if g == 'masc_to_real':
                self._merge_masc_to_real(dry_run=opts['dry_run'], batch=opts['batch_size'])
            else:
                self._dedup_grupo(g, dry_run=opts['dry_run'], batch=opts['batch_size'])

    def _dedup_grupo(self, grupo, *, dry_run, batch):
        raise CommandError(f'[{grupo}] colapso não implementado — ver Task 3')

    def _merge_masc_to_real(self, *, dry_run, batch):
        raise CommandError('[masc_to_real] não implementado — ver Task 4')
