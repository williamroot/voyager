import uuid

from django.core.management.base import BaseCommand, CommandError

from tribunals.models import ApiClient


class Command(BaseCommand):
    help = 'Gera um MCP token para um ApiClient existente.'

    def add_arguments(self, parser):
        parser.add_argument('nome', type=str, help='Nome do ApiClient.')

    def handle(self, *args, **options):
        nome = options['nome']
        try:
            cliente = ApiClient.objects.get(nome=nome)
        except ApiClient.DoesNotExist:
            raise CommandError(f'ApiClient "{nome}" não encontrado.')

        token = uuid.uuid4()
        cliente.mcp_token = token
        cliente.save(update_fields=['mcp_token'])
        self.stdout.write(self.style.SUCCESS(f'MCP token gerado para "{nome}":'))
        self.stdout.write(str(token))