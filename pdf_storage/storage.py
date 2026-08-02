"""Helpers de storage MinIO pra PDFs."""
import logging

from django.conf import settings

logger = logging.getLogger('voyager.pdf_storage')


def get_pdf_storage():
    """Retorna o storage de PDFs (S3/MinIO)."""
    from django.core.files.storage import storages
    return storages['pdfs']


def ensure_bucket():
    """Cria o bucket no MinIO se não existir. Idempotente."""
    try:
        import boto3
        scheme = 'https' if settings.MINIO_USE_SSL else 'http'
        client = boto3.client(
            's3',
            endpoint_url=f'{scheme}://{settings.MINIO_ENDPOINT}',
            aws_access_key_id=settings.MINIO_ACCESS_KEY,
            aws_secret_access_key=settings.MINIO_SECRET_KEY,
        )
        client.head_bucket(Bucket=settings.MINIO_BUCKET_PDFS)
    except Exception as e:
        if '404' in str(e) or 'NoSuchBucket' in str(e):
            try:
                client.create_bucket(Bucket=settings.MINIO_BUCKET_PDFS)
                logger.info('Bucket MinIO criado: %s', settings.MINIO_BUCKET_PDFS)
            except Exception as e2:
                logger.error('Erro criando bucket %s: %s', settings.MINIO_BUCKET_PDFS, e2)
        else:
            logger.debug('Bucket MinIO já existe ou erro benigno: %s', e)