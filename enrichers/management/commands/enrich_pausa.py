"""Pausa/despausa o enrichment de tribunais (kill-switch).

Uso:
  python manage.py enrich_pausa TJRO TJAP        # pausa
  python manage.py enrich_pausa --off TJRO       # despausa
  python manage.py enrich_pausa --list           # mostra pausados

Pausar NÃO perde nada: os processos continuam `pendente` no banco; o refill
para de enfileirar, o auto-enqueue da ingestão ignora, e jobs residuais na fila
viram no-op (drenam sem queimar proxy). Ao despausar, o refill repõe sozinho.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from enrichers.jobs import _ENRICHERS, enrich_pausados, set_enrich_pausados


class Command(BaseCommand):
    help = 'Pausa/despausa o enrichment por tribunal (kill-switch em cache Redis).'

    def add_arguments(self, parser):
        parser.add_argument('siglas', nargs='*', help='Siglas (ex.: TJRO TJAP)')
        parser.add_argument('--off', action='store_true', help='Despausa as siglas dadas.')
        parser.add_argument('--list', action='store_true', dest='listar',
                            help='Lista os tribunais pausados.')

    def handle(self, *args, **o):
        atuais = enrich_pausados()
        if o['listar'] or not o['siglas']:
            self.stdout.write('Pausados: ' + (', '.join(sorted(atuais)) or '(nenhum)'))
            return
        siglas = {s.upper() for s in o['siglas']}
        invalidas = siglas - set(_ENRICHERS)
        if invalidas:
            raise CommandError(f'Sem enricher cadastrado: {", ".join(sorted(invalidas))}')
        novo = (atuais - siglas) if o['off'] else (atuais | siglas)
        set_enrich_pausados(novo)
        verbo = 'despausados' if o['off'] else 'pausados'
        self.stdout.write(self.style.SUCCESS(
            f'{", ".join(sorted(siglas))} {verbo}. Pausados agora: '
            + (', '.join(sorted(novo)) or '(nenhum)')))
