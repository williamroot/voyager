from django.db import models

from tribunals.models import Movimentacao


class PdfArquivo(models.Model):
    """PDF de uma Movimentacao baixado e armazenado no MinIO (cached_docurl)."""

    movimentacao = models.OneToOneField(
        Movimentacao, on_delete=models.CASCADE, related_name='pdf',
    )
    arquivo = models.FileField(storage='pdfs', upload_to='movimentacoes/%Y/%m/')
    tamanho_bytes = models.PositiveBigIntegerField(default=0)
    hash_sha256 = models.CharField(max_length=64, blank=True)
    baixado_em = models.DateTimeField(auto_now_add=True)

    STATUS_PENDENTE = 'pendente'
    STATUS_OK = 'ok'
    STATUS_ERRO = 'erro'
    STATUS_CHOICES = [
        (STATUS_PENDENTE, 'Pendente'),
        (STATUS_OK, 'OK'),
        (STATUS_ERRO, 'Erro'),
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDENTE)
    erro = models.TextField(blank=True)
    tentativas = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=['-baixado_em'])]
        ordering = ['-baixado_em']

    def __str__(self):
        return f'PDF {self.movimentacao_id} · {self.status}'