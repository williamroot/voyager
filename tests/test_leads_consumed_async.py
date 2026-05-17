import uuid
import pytest
from django.db import IntegrityError, transaction
from tribunals.models import ApiClient, LeadConsumption, Process, Tribunal


@pytest.fixture
def cliente(db):
    return ApiClient.objects.create(nome='falcon', api_key='k-test', ativo=True)


@pytest.fixture
def proc(db):
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1',
        defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1'},
    )
    return Process.objects.create(tribunal=t, numero_cnj='1000000-00.2025.4.01.3300')


@pytest.mark.django_db
def test_lote_id_unique_por_cliente_processo(cliente, proc):
    lote = uuid.uuid4()
    LeadConsumption.objects.create(processo=proc, cliente=cliente,
                                   resultado='pendente', lote_id=lote)
    with pytest.raises(IntegrityError), transaction.atomic():
        LeadConsumption.objects.create(processo=proc, cliente=cliente,
                                       resultado='pendente', lote_id=lote)


@pytest.mark.django_db
def test_lote_id_nulo_permite_duplicata(cliente, proc):
    LeadConsumption.objects.create(processo=proc, cliente=cliente, resultado='validado')
    LeadConsumption.objects.create(processo=proc, cliente=cliente, resultado='validado')
    assert LeadConsumption.objects.count() == 2


@pytest.mark.django_db
def test_registrar_consumo_idempotente(cliente, proc):
    from tribunals.jobs import registrar_consumo_leads
    lote = str(uuid.uuid4())
    consumos = [{'cnj': proc.numero_cnj, 'resultado': 'validado'}]
    r1 = registrar_consumo_leads(cliente.id, consumos, lote)
    r2 = registrar_consumo_leads(cliente.id, consumos, lote)  # replay
    assert r1['criados'] == 1
    assert r2['criados'] == 0
    assert LeadConsumption.objects.filter(cliente=cliente, processo=proc).count() == 1


@pytest.mark.django_db
def test_registrar_consumo_cnj_inexistente(cliente):
    from tribunals.jobs import registrar_consumo_leads
    r = registrar_consumo_leads(cliente.id, [{'cnj': '9-9.9.9.9', 'resultado': 'erro'}],
                                str(uuid.uuid4()))
    assert r['criados'] == 0
    assert '9-9.9.9.9' in r['nao_encontrados']


@pytest.mark.django_db
def test_consumed_endpoint_enfileira_202(cliente, proc):
    from django.test import Client
    import django_rq
    django_rq.get_queue('leads_consumo').empty()
    body = {'lote_id': str(uuid.uuid4()),
            'consumos': [{'cnj': proc.numero_cnj, 'resultado': 'validado'}]}
    resp = Client().post('/api/v1/leads/consumed/', data=body,
                          content_type='application/json',
                          HTTP_X_API_KEY='k-test')
    assert resp.status_code == 202
    assert resp.json()['enfileirado'] is True
    assert django_rq.get_queue('leads_consumo').count == 1


@pytest.mark.django_db
def test_consumed_sem_lote_id_400(cliente, proc):
    from django.test import Client
    resp = Client().post('/api/v1/leads/consumed/',
                          data={'consumos': [{'cnj': proc.numero_cnj, 'resultado': 'validado'}]},
                          content_type='application/json', HTTP_X_API_KEY='k-test')
    assert resp.status_code == 400
