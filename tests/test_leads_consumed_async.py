import uuid
import pytest
from django.db import IntegrityError
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
    with pytest.raises(IntegrityError):
        LeadConsumption.objects.create(processo=proc, cliente=cliente,
                                       resultado='pendente', lote_id=lote)


@pytest.mark.django_db
def test_lote_id_nulo_permite_duplicata(cliente, proc):
    LeadConsumption.objects.create(processo=proc, cliente=cliente, resultado='validado')
    LeadConsumption.objects.create(processo=proc, cliente=cliente, resultado='validado')
    assert LeadConsumption.objects.count() == 2
