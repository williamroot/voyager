import pytest

from tribunals.models import FonteDiario, Tribunal


@pytest.mark.django_db
def test_fonte_diario_create():
    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    fd = FonteDiario.objects.create(
        source_id=1, tribunal=t, diario_slug='dje-trf1',
        orgao_slug='trf1', caderno_slug='', nome='TRF - 1ª Reg.',
    )
    assert fd.source_id == 1
    assert fd.tribunal_id == 'TRF1'
    assert str(fd) == '1 · TRF - 1ª Reg.'


@pytest.mark.django_db
def test_fonte_diario_one_to_one():
    t = Tribunal.objects.create(sigla='TRF3', nome='TRF3', sigla_djen='TRF3')
    FonteDiario.objects.create(
        source_id=59, tribunal=t, diario_slug='dje-trf3',
        orgao_slug='trf3', caderno_slug='', nome='TRF - 3ª Reg.',
    )
    with pytest.raises(Exception):
        FonteDiario.objects.create(
            source_id=60, tribunal=t, diario_slug='dje-trf3',
            orgao_slug='trf3', caderno_slug='', nome='TRF - 3ª Reg. outro',
        )


@pytest.mark.django_db
def test_assunto_norm_default_lista_vazia():
    from tribunals.models import Movimentacao, Process
    t = Tribunal.objects.create(sigla='TJSP', nome='TJSP', sigla_djen='TJSP')
    p = Process.objects.create(numero_cnj='0001234-56.2025.4.01.0000', tribunal=t)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T00:00:00Z',
    )
    assert mov.assunto_norm == []