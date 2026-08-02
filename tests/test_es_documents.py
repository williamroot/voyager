import pytest

from tribunals.models import FonteDiario, Movimentacao, Process, Tribunal


@pytest.mark.django_db
def test_movimentacao_to_doc_campos_basicos():
    from search.documents import movimentacao_to_doc

    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    FonteDiario.objects.create(
        source_id=1, tribunal=t, diario_slug='dje-trf1',
        orgao_slug='trf1', caderno_slug='', nome='TRF - 1ª Reg.',
    )
    p = Process.objects.create(numero_cnj='0001234-56.2025.4.01.0000', tribunal=t)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T10:00:00Z',
        texto='Texto da publicação', link='http://exemplo.com/pdf.pdf',
        tipo_comunicacao='Intimação', nome_orgao='Vara Federal',
    )
    doc = movimentacao_to_doc(mov)
    assert doc['id'] == mov.id
    assert doc['tribunal'] == 'TRF1'
    assert doc['source'] == 1
    assert doc['body'] == 'Texto da publicação'
    assert doc['proc'] == '0001234-56.2025.4.01.0000'
    assert doc['docurl'] == 'http://exemplo.com/pdf.pdf'
    assert doc['processo_id'] == p.id
    assert doc['periodico_diario_slug'] == 'dje-trf1'
    assert doc['periodico_orgao_slug'] == 'trf1'
    assert doc['recorte_id'] == mov.id
    assert doc['ativo'] is True


@pytest.mark.django_db
def test_processo_to_doc_campos_basicos():
    from search.documents import processo_to_doc

    t = Tribunal.objects.create(sigla='TRF3', nome='TRF3', sigla_djen='TRF3')
    p = Process.objects.create(
        numero_cnj='0001234-56.2025.4.01.0000', tribunal=t,
        classe_nome='Execução Fiscal', valor_causa=10000.50,
        total_movimentacoes=5,
    )
    doc = processo_to_doc(p)
    assert doc['id'] == p.id
    assert doc['tribunal'] == 'TRF3'
    assert doc['proc'] == '0001234-56.2025.4.01.0000'
    assert doc['classe_nome'] == 'Execução Fiscal'
    assert doc['valor_causa'] == 10000.50
    assert doc['total_movimentacoes'] == 5


@pytest.mark.django_db
def test_document_sem_fonte_diario_source_none():
    from search.documents import movimentacao_to_doc

    t = Tribunal.objects.create(sigla='TJXX', nome='TJXX', sigla_djen='TJXX')
    p = Process.objects.create(numero_cnj='0001234-56.2025.4.01.0000', tribunal=t)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T10:00:00Z',
    )
    doc = movimentacao_to_doc(mov)
    assert doc['source'] is None
    assert doc['periodico_diario_slug'] == 'tjxx'