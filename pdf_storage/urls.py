from django.http import HttpResponseRedirect, JsonResponse
from django.urls import path

from .models import PdfArquivo


def serve_pdf(request, mov_id: int):
    """Redireciona pro PDF no MinIO. 404 se não baixado ainda."""
    try:
        pdf = PdfArquivo.objects.get(movimentacao_id=mov_id, status='ok')
    except PdfArquivo.DoesNotExist:
        return JsonResponse({'available': False, 'error': 'pdf_nao_disponivel'}, status=404)
    url = pdf.arquivo.url
    return HttpResponseRedirect(url)


urlpatterns = [
    path('<int:mov_id>/', serve_pdf, name='serve-pdf'),
]