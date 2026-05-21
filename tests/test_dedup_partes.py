import pytest
from django.core.management import call_command
from django.db import connection
from tribunals.models import Tribunal, Process, Parte, ProcessoParte

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _drop_partial_unique_indexes(django_db_setup, django_db_blocker):
    """Em prod os 3 índices únicos parciais de tribunals_parte estão
    INVÁLIDOS (cascas mortas) — é exatamente por isso que o dedup_partes
    existe. Num banco de teste recém-criado eles nascem VÁLIDOS e
    impediriam o seed de Partes duplicadas que os testes precisam.
    Dropamos pra reproduzir o estado de prod que o command conserta.
    """
    with django_db_blocker.unblock():
        with connection.cursor() as cur:
            for nome in (
                'uniq_parte_oab',
                'uniq_parte_documento_real',
                'uniq_parte_documento_mascarado',
            ):
                cur.execute(f'DROP INDEX IF EXISTS {nome}')


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


def test_dedup_repoint_processoparte():
    proc = _proc(n='100')
    p1 = Parte.objects.create(nome='ADV', oab='RS9', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV', oab='RS9', tipo='advogado')
    pp = ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    pp.refresh_from_db()
    assert pp.parte_id == p1.id
    assert not Parte.objects.filter(id=p2.id).exists()


def test_dedup_collisao_processoparte_nao_duplica():
    proc = _proc(n='200')
    p1 = Parte.objects.create(nome='ADV', oab='RS8', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV', oab='RS8', tipo='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p1, polo='ativo', papel='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert ProcessoParte.objects.filter(processo=proc).count() == 1
    assert ProcessoParte.objects.get(processo=proc).parte_id == p1.id


def test_doc_masc_colapsa_nome_e_doc_identicos():
    p1 = Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    qs = Parte.objects.filter(nome='MARIA SOUZA', documento='639.XXX.XXX-XX')
    assert qs.count() == 1 and qs.first().id == p1.id


def test_doc_masc_nao_funde_nomes_diferentes_mesma_mascara():
    Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    Parte.objects.create(nome='MARIA SANTOS', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    assert Parte.objects.filter(documento='639.XXX.XXX-XX').count() == 2


def test_dedup_dois_losers_sem_survivor_pp_nao_duplica():
    """C1: 2 loser Partes no mesmo slot, survivor SEM PP pré-existente →
    repoint deve resultar em UMA ProcessoParte, não duas."""
    proc = _proc(n='300')
    p1 = Parte.objects.create(nome='ADV', oab='RS7', tipo='advogado')  # survivor (min id)
    p2 = Parte.objects.create(nome='ADV', oab='RS7', tipo='advogado')  # loser
    p3 = Parte.objects.create(nome='ADV', oab='RS7', tipo='advogado')  # loser
    ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p3, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert ProcessoParte.objects.filter(processo=proc).count() == 1
    assert ProcessoParte.objects.get(processo=proc).parte_id == p1.id


def test_dedup_representa_nao_viola_fk():
    """I1: quando uma PP que é alvo de representa_id é deletada na
    consolidação, o repoint não pode quebrar a FK self de representa."""
    proc = _proc(n='400')
    adv1 = Parte.objects.create(nome='ADVOGADO', oab='SP500', tipo='advogado')  # survivor
    adv2 = Parte.objects.create(nome='ADVOGADO', oab='SP500', tipo='advogado')  # loser
    autor = Parte.objects.create(nome='AUTOR', tipo='pf')
    pp_autor = ProcessoParte.objects.create(processo=proc, parte=autor, polo='ativo', papel='autor')
    # 2 PP de advogado no mesmo slot (uma por Parte duplicada), ambas
    # representam o autor — uma será deletada na consolidação.
    ProcessoParte.objects.create(processo=proc, parte=adv1, polo='ativo',
                                 papel='advogado', representa=pp_autor)
    ProcessoParte.objects.create(processo=proc, parte=adv2, polo='ativo',
                                 papel='advogado', representa=pp_autor)
    call_command('dedup_partes', '--group', 'oab')  # não deve levantar FK error
    # advogado consolidado: sobra 1 PP de advogado apontando pro survivor
    advs = ProcessoParte.objects.filter(processo=proc, papel='advogado')
    assert advs.count() == 1 and advs.first().parte_id == adv1.id
