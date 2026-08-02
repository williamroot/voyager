"""Jobs RQ de download de PDFs (fila pdf_download)."""
import hashlib
import logging

import requests
from django.core.files.base import ContentFile

from tribunals.models import Movimentacao

from .models import PdfArquivo

logger = logging.getLogger('voyager.pdf_storage.jobs')

MAX_TENTATIVAS = 3
TIMEOUT = 30


def baixar_pdf(mov_pk: int):
    """Baixa o PDF de Movimentacao.link e salva no MinIO. Idempotente."""
    try:
        mov = Movimentacao.objects.get(pk=mov_pk)
    except Movimentacao.DoesNotExist:
        logger.warning('Movimentacao %s não encontrada pra baixar PDF', mov_pk)
        return

    if not mov.link:
        logger.debug('Movimentacao %s sem link — skip', mov_pk)
        return

    if hasattr(mov, 'pdf') and mov.pdf.status == PdfArquivo.STATUS_OK:
        logger.debug('PDF %s já baixado — skip', mov_pk)
        return

    tentativa = 1
    if hasattr(mov, 'pdf'):
        tentativa = mov.pdf.tentativas + 1
        mov.pdf.tentativas = tentativa
        mov.pdf.status = PdfArquivo.STATUS_PENDENTE
        mov.pdf.save()

    try:
        resp = requests.get(mov.link, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        content = resp.content
        sha256 = hashlib.sha256(content).hexdigest()
        filename = f'{mov.id}.pdf'

        if hasattr(mov, 'pdf'):
            pdf = mov.pdf
        else:
            pdf = PdfArquivo(movimentacao=mov, status=PdfArquivo.STATUS_PENDENTE)

        pdf.arquivo.save(filename, ContentFile(content), save=False)
        pdf.tamanho_bytes = len(content)
        pdf.hash_sha256 = sha256
        pdf.status = PdfArquivo.STATUS_OK
        pdf.erro = ''
        pdf.tentativas = tentativa
        pdf.save()
        logger.info('PDF %s baixado (%s bytes)', mov_pk, len(content))
    except Exception as e:
        logger.error('Erro baixando PDF %s: %s', mov_pk, e)
        if hasattr(mov, 'pdf'):
            mov.pdf.status = PdfArquivo.STATUS_ERRO
            mov.pdf.erro = str(e)[:1000]
            mov.pdf.save()
        else:
            PdfArquivo.objects.create(
                movimentacao=mov,
                status=PdfArquivo.STATUS_ERRO,
                erro=str(e)[:1000],
                tentativas=tentativa,
            )