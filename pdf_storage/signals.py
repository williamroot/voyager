"""Signals de download de PDF (write-through)."""
import django_rq
from django.db.models.signals import post_save
from django.dispatch import receiver

from tribunals.models import Movimentacao


@receiver(post_save, sender=Movimentacao)
def mov_post_save_check_pdf(sender, instance, created, update_fields, **kwargs):
    """Se a Movimentacao tem link e não tem PdfArquivo, agenda download."""
    if not instance.link:
        return
    if hasattr(instance, 'pdf') and instance.pdf.status in ('ok', 'pendente'):
        return
    if update_fields and 'link' not in update_fields:
        return
    q = django_rq.get_queue('pdf_download')
    q.enqueue('pdf_storage.jobs.baixar_pdf', instance.pk)