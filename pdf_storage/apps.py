from django.apps import AppConfig


class PdfStorageConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pdf_storage'

    def ready(self):
        from . import signals  # noqa: F401