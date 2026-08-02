from django.core.management.base import BaseCommand

from pdf_storage.storage import ensure_bucket


class Command(BaseCommand):
    help = 'Cria o bucket MinIO se não existir.'

    def handle(self, *args, **options):
        self.stdout.write('Garantindo bucket MinIO...')
        ensure_bucket()
        self.stdout.write(self.style.SUCCESS('Bucket MinIO OK.'))