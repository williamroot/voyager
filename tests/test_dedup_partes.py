import pytest
from django.core.management import call_command
from tribunals.models import Tribunal, Process, Parte, ProcessoParte

pytestmark = pytest.mark.django_db


def _proc(n='1'):
    t, _ = Tribunal.objects.get_or_create(sigla='TRF1', defaults={'sigla_djen': 'TRF1', 'nome': 'TRF1'})
    return Process.objects.create(tribunal=t, numero_cnj=f'{n:0>7}-00.2024.4.01.0000')


def test_dedup_oab_colapsa_para_min_id():
    p1 = Parte.objects.create(nome='ADV UM', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='ADV UM VARIANTE', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='ADV UM', oab='SP111', tipo='advogado')
    call_command('dedup_partes', '--group', 'oab')
    restantes = list(Parte.objects.filter(oab='SP111'))
    assert len(restantes) == 1 and restantes[0].id == p1.id


def test_dedup_nao_funde_oabs_diferentes():
    Parte.objects.create(nome='JOSE DA SILVA', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='JOSE DA SILVA', oab='SP222', tipo='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert Parte.objects.filter(nome='JOSE DA SILVA').count() == 2
