from django.core.management.base import BaseCommand

from search.index import ensure_indexes


class Command(BaseCommand):
    help = 'Cria indexes Elasticsearch se não existirem.'

    def handle(self, *args, **options):
        self.stdout.write('Garantindo indexes ES...')
        ensure_indexes()
        self.stdout.write(self.style.SUCCESS('Indexes ES OK.'))