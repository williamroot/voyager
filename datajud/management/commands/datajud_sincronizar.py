"""Sincroniza um processo via Datajud (CLI helper)."""
from django.core.management.base import BaseCommand, CommandError

from datajud.client import DatajudClient
from datajud.ingestion import sync_processo
from tribunals.models import Process


class Command(BaseCommand):
    help = 'Sincroniza Movimentacao de um processo via Datajud (CNJ).'

    def add_arguments(self, parser):
        parser.add_argument('cnj_or_pk', help='Numero CNJ formatado ou pk numerica')
        parser.add_argument('--no-cortex', action='store_true',
                            help='Não usa Cortex (proxy residencial) primeiro.')

    def handle(self, *args, **opts):
        ident = opts['cnj_or_pk']
        if ident.isdigit() and len(ident) <= 10:
            proc = Process.objects.filter(pk=int(ident)).first()
        else:
            proc = Process.objects.filter(numero_cnj=ident).first()
        if not proc:
            raise CommandError(f'Process não encontrado: {ident}')

        client = DatajudClient(prefer_cortex=not opts['no_cortex'])
        result = sync_processo(proc, client=client)
        self.stdout.write(self.style.SUCCESS(
            f"{result['cnj']} fonte={result['fonte']} encontrado={result['encontrado']} "
            f"novos={result['novos']} duplicados={result['duplicados']}"
        ))
