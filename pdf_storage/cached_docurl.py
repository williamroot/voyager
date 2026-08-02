"""Helper pra obter a cached_docurl de uma Movimentacao."""
from .models import PdfArquivo


def cached_docurl_for(mov) -> str | None:
    """Retorna URL Voyager pro PDF se PdfArquivo existe, senão None."""
    try:
        pdf = PdfArquivo.objects.get(movimentacao_id=mov.id, status='ok')
        return pdf.arquivo.url
    except PdfArquivo.DoesNotExist:
        return None