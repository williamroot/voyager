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


def test_masc_to_real_funde_quando_um_candidato():
    real = Parte.objects.create(nome='ANA LIMA', documento='639.979.036-40', tipo='pf')
    masc = Parte.objects.create(nome='ANA LIMA', documento='639.XXX.XXX-XX', tipo='pf')
    proc = _proc(n='500')
    pp = ProcessoParte.objects.create(processo=proc, parte=masc, polo='ativo', papel='autor')
    call_command('dedup_partes', '--group', 'masc_to_real')
    pp.refresh_from_db()
    assert pp.parte_id == real.id
    assert not Parte.objects.filter(id=masc.id).exists()


def test_masc_to_real_nao_funde_com_dois_candidatos():
    Parte.objects.create(nome='JOSE SILVA', documento='639.111.222-33', tipo='pf')
    Parte.objects.create(nome='JOSE SILVA', documento='639.444.555-66', tipo='pf')
    masc = Parte.objects.create(nome='JOSE SILVA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'masc_to_real')
    assert Parte.objects.filter(id=masc.id).exists()           # mascarada intacta
    assert Parte.objects.filter(nome='JOSE SILVA').count() == 3


def test_masc_to_real_nao_funde_digitos_divergentes():
    Parte.objects.create(nome='PEDRO ROCHA', documento='100.111.222-33', tipo='pf')
    masc = Parte.objects.create(nome='PEDRO ROCHA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'masc_to_real')
    assert Parte.objects.filter(id=masc.id).exists()


# ==========================================================================
# oab_zero — zero à esquerda (pendência #96)
#
# Teto remedido em produção em 31/08/2026 sobre 943.510 `Parte` com OAB:
# 19.494 linhas em colisão (19.482 grupos), e 0 grupos SEM nenhuma forma
# zero-padded — o zero responde por 100% da colisão. A regra funde 18.501
# e ABSTÉM em 993 (992 nome divergente + 1 CPF divergente).
# ==========================================================================


def _oab_zero(**kw):
    call_command('dedup_partes', '--group', 'oab_zero', **kw)


def test_oab_zero_funde_e_survivor_e_a_forma_canonica():
    """`PE00475` some, `PE475` fica — o survivor tem que ser a forma que a
    porta de escrita canônica vai produzir, senão a duplicata volta no
    primeiro enriquecimento."""
    padded = Parte.objects.create(nome='TANEY QUEIROZ', oab='PE00475', tipo='advogado')
    canon = Parte.objects.create(nome='TANEY QUEIROZ', oab='PE475', tipo='advogado')
    assert padded.id < canon.id                      # o zero-padded é o MENOR id
    _oab_zero()
    restantes = list(Parte.objects.filter(nome='TANEY QUEIROZ'))
    assert len(restantes) == 1
    assert restantes[0].id == canon.id and restantes[0].oab == 'PE475'


def test_oab_zero_repoint_processoparte():
    proc = _proc(n='600')
    padded = Parte.objects.create(nome='ADV ZERO', oab='RS039599', tipo='advogado')
    canon = Parte.objects.create(nome='ADV ZERO', oab='RS39599', tipo='advogado')
    pp = ProcessoParte.objects.create(processo=proc, parte=padded, polo='ativo', papel='advogado')
    _oab_zero()
    pp.refresh_from_db()
    assert pp.parte_id == canon.id
    assert not Parte.objects.filter(id=padded.id).exists()


# --------------------------------------------------------------------------
# CONTROLE NEGATIVO — o que NÃO pode ser fundido
# --------------------------------------------------------------------------
def test_controle_negativo_mesma_oab_numerica_uf_diferente_nao_funde():
    """Dois advogados DIFERENTES: `475` em PE e `475` em SP. O número bate
    depois de tirar o zero; a UF não. Fundir aqui seria inventar identidade —
    e o nome idêntico não salva, porque homônimo existe."""
    Parte.objects.create(nome='JOAO DA SILVA', oab='PE00475', tipo='advogado')
    Parte.objects.create(nome='JOAO DA SILVA', oab='SP475', tipo='advogado')
    _oab_zero()
    assert Parte.objects.filter(nome='JOAO DA SILVA').count() == 2
    assert set(Parte.objects.filter(nome='JOAO DA SILVA').values_list('oab', flat=True)) == {
        'PE475', 'SP475',          # o zero-padded solitário é só reescrito
    }


def test_controle_negativo_quebra_quando_a_guarda_de_uf_e_desligada(monkeypatch):
    """MUTAÇÃO: tira a UF da chave canônica e o controle negativo acima
    passa a FUNDIR. É a prova de que o teste anterior mede a guarda, e não
    o acaso de as duas linhas nunca se encontrarem."""
    from tribunals.management.commands import dedup_partes as mod

    monkeypatch.setattr(
        mod.Command, '_SQL_CANON',
        mod.Command._SQL_CANON.replace(
            "substring(oab from '^([A-Z]{2})')\n                 || COALESCE",
            "'' \n                 || COALESCE"),
    )
    Parte.objects.create(nome='JOAO DA SILVA', oab='PE00475', tipo='advogado')
    Parte.objects.create(nome='JOAO DA SILVA', oab='SP475', tipo='advogado')
    _oab_zero()
    assert Parte.objects.filter(nome='JOAO DA SILVA').count() == 1   # fundiu — errado


def test_oab_zero_abstem_com_nome_divergente():
    """O par REAL que motivou a guarda: `PE00475` = TANEY QUEIROZ E FARIAS,
    `PE475` = LUZIA HELENA DE VALOIS CORREIA. Mesma UF, mesmo número,
    pessoas diferentes."""
    Parte.objects.create(nome='TANEY QUEIROZ E FARIAS', oab='PE00475', tipo='advogado')
    Parte.objects.create(nome='LUZIA HELENA DE VALOIS CORREIA', oab='PE475', tipo='advogado')
    _oab_zero()
    assert Parte.objects.filter(oab__in=['PE00475', 'PE475']).count() == 2


def test_oab_zero_abstem_com_cpf_real_divergente():
    Parte.objects.create(nome='ANA LIMA', oab='BA05249', documento='111.222.333-44', tipo='advogado')
    Parte.objects.create(nome='ANA LIMA', oab='BA5249', documento='555.666.777-88', tipo='advogado')
    _oab_zero()
    assert Parte.objects.filter(oab__in=['BA05249', 'BA5249']).count() == 2


def test_oab_zero_nao_funde_sufixo_de_letra_diferente():
    """`AL10715A` e `AL10715` são categorias de inscrição diferentes."""
    Parte.objects.create(nome='ANTONIO DOURADO', oab='AL010715A', tipo='advogado')
    Parte.objects.create(nome='ANTONIO DOURADO', oab='AL10715', tipo='advogado')
    _oab_zero()
    assert set(Parte.objects.filter(nome='ANTONIO DOURADO').values_list('oab', flat=True)) == {
        'AL10715A', 'AL10715',
    }


def test_oab_zero_ignora_forma_que_nao_reconhece():
    """`MT10079GO` (1.424 linhas em prod) tem o TRIBUNAL no prefixo e a UF no
    sufixo. A régua não sabe ler isso — então não escreve nada nele."""
    Parte.objects.create(nome='ADV MT', oab='MT10079GO', tipo='advogado')
    Parte.objects.create(nome='ADV MT', oab='MT010079GO', tipo='advogado')
    _oab_zero()
    assert Parte.objects.filter(nome='ADV MT').count() == 2


def test_oab_zero_normaliza_solitaria_sem_fundir():
    """Zero-padded sem gêmea: a linha é a MESMA entidade, só muda a grafia.
    Sem isso a porta de escrita canônica cria uma duplicata nova nela."""
    p = Parte.objects.create(nome='SOLITARIA', oab='GO026464', tipo='advogado')
    _oab_zero()
    p.refresh_from_db()
    assert p.oab == 'GO26464'
    assert Parte.objects.filter(nome='SOLITARIA').count() == 1


def test_oab_zero_sem_normalizar_solitarias_nao_toca_na_grafia():
    p = Parte.objects.create(nome='SOLITARIA 2', oab='GO026465', tipo='advogado')
    _oab_zero(sem_normalizar_solitarias=True)
    p.refresh_from_db()
    assert p.oab == 'GO026465'


def test_oab_zero_dry_run_nao_escreve():
    a = Parte.objects.create(nome='DRY RUN', oab='SC050129', tipo='advogado')
    b = Parte.objects.create(nome='DRY RUN', oab='SC50129', tipo='advogado')
    _oab_zero(dry_run=True)
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.oab == 'SC050129' and b.oab == 'SC50129'
    assert Parte.objects.filter(nome='DRY RUN').count() == 2
