import pytest

from tribunals.models import Movimentacao, Process, Tribunal


@pytest.mark.django_db
def test_pdf_arquivo_criacao():
    from pdf_storage.models import PdfArquivo

    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    p = Process.objects.create(numero_cnj='0001234-56.2025.4.01.0000', tribunal=t)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T00:00:00Z',
        link='http://exemplo.com/doc.pdf',
    )
    pdf = PdfArquivo.objects.create(
        movimentacao=mov, status=PdfArquivo.STATUS_PENDENTE,
    )
    assert pdf.status == 'pendente'
    assert pdf.tentativas == 0
    assert str(pdf).startswith(f'PDF {mov.id}')


@pytest.mark.django_db
def test_cached_docurl_sem_pdf_retorna_none():
    from pdf_storage.cached_docurl import cached_docurl_for

    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    p = Process.objects.create(numero_cnj='0001234-56.2025.4.01.0000', tribunal=t)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T00:00:00Z',
    )
    assert cached_docurl_for(mov) is None