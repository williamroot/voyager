"""Marca tribunais como CORTEX-ONLY (só proxy residencial, sem datacenter).

Uso:
  python manage.py enrich_cortex TJRO TJAP     # datacenter bloqueado → só Cortex
  python manage.py enrich_cortex --off TJRO    # volta ao normal (datacenter-first)
  python manage.py enrich_cortex --list

Pra tribunais cujo WAF bloqueia 100% dos IPs datacenter (403 em todo o pool),
tentar datacenter é só desperdício de rotação. Marcados aqui, o enricher vai
DIRETO e SÓ pro Cortex (residencial, que fura o WAF). Granular por tribunal;
vive em cache Redis (sem TTL), lido no boot de cada enricher.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from enrichers.jobs import _ENRICHERS, enrich_cortex_only, set_cortex_only


class Command(BaseCommand):
    help = 'Marca tribunais como cortex-only (só proxy residencial).'

    def add_arguments(self, parser):
        parser.add_argument('siglas', nargs='*', help='Siglas (ex.: TJRO TJAP)')
        parser.add_argument('--off', action='store_true', help='Remove as siglas (volta a datacenter-first).')
        parser.add_argument('--list', action='store_true', dest='listar')

    def handle(self, *args, **o):
        atuais = enrich_cortex_only()
        if o['listar'] or not o['siglas']:
            self.stdout.write('Cortex-only: ' + (', '.join(sorted(atuais)) or '(nenhum)'))
            return
        siglas = {s.upper() for s in o['siglas']}
        invalidas = siglas - set(_ENRICHERS)
        if invalidas:
            raise CommandError(f'Sem enricher cadastrado: {", ".join(sorted(invalidas))}')
        novo = (atuais - siglas) if o['off'] else (atuais | siglas)
        set_cortex_only(novo)
        verbo = 'removidos de' if o['off'] else 'marcados'
        self.stdout.write(self.style.SUCCESS(
            f'{", ".join(sorted(siglas))} {verbo} cortex-only. Cortex-only agora: '
            + (', '.join(sorted(novo)) or '(nenhum)')))
