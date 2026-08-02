import pytest

from tribunals.models import Movimentacao, Process, Tribunal


@pytest.mark.django_db
def test_create_monitored_term_valido():
    from monitoring.models import MonitoredTerm
    mt = MonitoredTerm.objects.create(term='precatório', source_ids=[1, 59])
    assert mt.term == 'precatório'
    assert mt.is_active is True
    assert mt.source_ids == [1, 59]


@pytest.mark.django_db
def test_create_monitored_term_curto_falha():
    from monitoring.models import MonitoredTerm
    from django.core.exceptions import ValidationError
    mt = MonitoredTerm(term='ab')
    with pytest.raises(ValidationError):
        mt.full_clean()


@pytest.mark.django_db
def test_create_monitored_term_com_virgula_falha():
    from monitoring.models import MonitoredTerm
    from django.core.exceptions import ValidationError
    mt = MonitoredTerm(term='termo,com,virgula')
    with pytest.raises(ValidationError):
        mt.full_clean()


@pytest.mark.django_db
def test_create_monitored_person():
    from monitoring.models import MonitoredPerson
    mp = MonitoredPerson.objects.create(
        nome='João da Silva', documento='123.456.789-00', is_advogado=False,
    )
    assert mp.nome == 'João da Silva'
    assert mp.is_active is True


@pytest.mark.django_db
def test_create_monitored_process():
    from monitoring.models import MonitoredProcess
    mp = MonitoredProcess.objects.create(cnj='0001234-56.2025.4.01.0000')
    assert mp.cnj == '0001234-56.2025.4.01.0000'
    assert mp.is_active is True


@pytest.mark.django_db
def test_detection_idempotente():
    from monitoring.models import Detection, MonitoredTerm

    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    p = Process.objects.create(numero_cnj='0001234-56.2025.4.01.0000', tribunal=t)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T00:00:00Z',
    )
    mt = MonitoredTerm.objects.create(term='precatório')

    det1, created1 = Detection.objects.get_or_create(
        target_type='term', target_id=mt.pk, movimentacao=mov,
        defaults={'snippet': 'texto'},
    )
    assert created1 is True

    det2, created2 = Detection.objects.get_or_create(
        target_type='term', target_id=mt.pk, movimentacao=mov,
        defaults={'snippet': 'texto'},
    )
    assert created2 is False
    assert det1.pk == det2.pk


@pytest.mark.django_db
def test_build_recorte_payload():
    from monitoring.payload import build_recorte_payload
    from tribunals.models import FonteDiario

    t = Tribunal.objects.create(sigla='TRF1', nome='TRF1', sigla_djen='TRF1')
    FonteDiario.objects.create(
        source_id=1, tribunal=t, diario_slug='dje-trf1',
        orgao_slug='trf1', caderno_slug='', nome='TRF - 1ª Reg.',
    )
    p = Process.objects.create(
        numero_cnj='0001234-56.2025.4.01.0000', tribunal=t,
        assunto_nome='Execução Fiscal',
    )
    mov = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='ext1',
        data_disponibilizacao='2025-01-01T10:00:00Z',
        texto='Texto do recorte', link='http://exemplo.com/doc.pdf',
        tipo_comunicacao='Intimação', nome_orgao='Vara Federal',
    )
    payload = build_recorte_payload(mov, 'term', 1)
    assert payload['proc'] == '0001234-56.2025.4.01.0000'
    assert payload['doc_id'] == mov.id
    assert payload['source_id'] == 1
    assert payload['periodico_diario_slug'] == 'dje-trf1'
    assert payload['assunto'] == 'Execução Fiscal'
    assert payload['snippet'] == 'Texto do recorte'
    assert payload['target_type'] == 'term'
    assert payload['target_id'] == 1