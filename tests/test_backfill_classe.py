"""`backfill_classe` — o reparo dos ~1,84 M processos que o `\\d{2,5}` perdeu.

`PROCEDIMENTO COMUM CÍVEL (7)`. Código de UM dígito, parêntese fechado, texto
íntegro — e o regex do drainer exigia dois. Corrigido em `08d306e`, mas o que já
estava gravado seguia quebrado, e o drainer só passou a rodar o código novo em
29/08/2026 (os 5 processos tinham subido 25 min ANTES do commit; o bind mount
entrega o arquivo, o Python não recarrega).

Medido em 29/08/2026, amostra uniforme por pk (200.000 pks, semente 20260829,
195.741 existentes):

    com `classe_nome` .............................. 67.569
    … e `classe_codigo` vazio (o BURACO) ............ 4.440
    … recuperável pelo regex novo ................... 3.477   (78,3%)
    … que o regex ANTIGO também pegaria ................. 0   (0%)

O que estes testes protegem:
  1. o regex casa a classe mais comum do país, e o antigo não casava;
  2. abstenção onde não dá pra provar (regra nº 6) — inclusive no conflito;
  3. **a campainha**: o UPDATE carrega `atualizado_em = now()`, senão o código
     fica certo no banco e velho na busca (`tests/test_campainha_sync.py`);
  4. `--dry-run` **não escreve** — um dry-run que escrevia já custou 39.303
     `Parte` órfãs em produção;
  5. teto atingido é ERRO com o número REAL, nunca `return` discreto (regra nº 2);
  6. `SET LOCAL` dentro de `transaction.atomic()` — solto no autocommit ele é
     descartado e o teto simplesmente não existe.
"""
import re
from io import StringIO

import pytest
from django.core.management import call_command

from enrichers.management.commands.backfill_classe import WM, planejar

FONTE = open('enrichers/management/commands/backfill_classe.py').read()


# ---------------------------------------------------------------- o parser --

@pytest.mark.parametrize('texto', [
    'PROCEDIMENTO COMUM CÍVEL (7)',            # 2.604 na amostra
    '[CÍVEL] PROCEDIMENTO COMUM CÍVEL (7)',    #   753 (TJMG)
    'Procedimento Comum Cível (7)',            #   120
])
def test_recupera_a_classe_mais_comum_do_pais(texto):
    """Os 3 textos reais que respondem por 3.477 de 3.477 recuperáveis."""
    veredito, campos = planejar(texto, '', None)
    assert veredito == 'recupera'
    assert campos['classe_codigo'] == '7'
    assert campos['classe_nome'] == texto[:-4].strip(), 'o `(7)` sai do nome'


def test_o_regex_antigo_nao_casava_nenhum_deles():
    """O controle negativo: `{2,5}` exigia dois dígitos e ali só tem um."""
    antigo = re.compile(r'^(.+?)\s*\((\d{2,5})\)?\s*$')
    assert antigo.match('PROCEDIMENTO COMUM CÍVEL (7)') is None


def test_recupera_codigo_de_varios_digitos():
    veredito, campos = planejar('Cumprimento de Sentença (12078)', '', None)
    assert veredito == 'recupera'
    assert campos == {'classe_codigo': '12078',
                      'classe_nome': 'Cumprimento de Sentença'}


@pytest.mark.parametrize('texto', [
    'PROCEDIMENTO COMUM CÍVEL',        # 962 de 963 abstenções da amostra
    'ALGUMA CLASSE CORTADA (1',        # sem fecho ⇒ `1` seria chute
    'Tributário 12345 algo',           # dígito do MEIO não é código
])
def test_abstem_quando_o_texto_nao_prova_o_codigo(texto):
    assert planejar(texto, '', None) == ('abstem', {})


def test_abstem_no_conflito():
    """Dois escritores discordam sobre a classe — escolher no chute é pior."""
    assert planejar('Procedimento Comum (7)', '12078', '12078') == ('conflito', {})


def test_fk_orfa_e_contada_mas_nao_consertada():
    """15.499 de 63.136 (24,5%) têm código e `classe_id` NULL — ≈ 8,2 M linhas.

    É 4,5× este trabalho e dobraria a campainha; território de
    `repop_classe_assunto`. Contar sem consertar é a decisão, e ela é testada.
    """
    assert planejar('Procedimento Comum (7)', '7', None) == ('fk_orfa', {})
    assert planejar('Procedimento Comum (7)', '7', '7') == ('nada', {})


# ------------------------------------------------------- fonte: as regras --

def test_o_update_carrega_a_campainha():
    """Sem `atualizado_em = now()` o dado fica certo no banco e velho na busca."""
    i = FONTE.find('UPDATE tribunals_process p ')
    assert i > 0, 'o UPDATE sumiu — teste desatualizado'
    trecho = FONTE[i:i + 400]
    assert 'atualizado_em = now()' in trecho, (
        '`auto_now` não roda em SQL cru e `sync_processos_atualizados` é keyset '
        'por `atualizado_em`: sem a campainha, 1,84 M de classes invisíveis')


def test_set_local_sempre_dentro_de_transacao():
    """`SET LOCAL` em autocommit é INERTE — o teto de espera não existiria."""
    linhas = FONTE.split('\n')
    achou = 0
    for n, ln in enumerate(linhas):
        if 'SET LOCAL' not in ln or ln.lstrip().startswith('#'):
            continue          # comentário citando a regra não é código
        achou += 1
        # sobe até o `with` que abriu o cursor desta linha
        for m in range(n - 1, -1, -1):
            if linhas[m].lstrip().startswith('with '):
                assert 'transaction.atomic()' in linhas[m], (
                    f'SET LOCAL fora de `transaction.atomic()` na linha {n + 1} '
                    f'({ln.strip()}) — é descartado com WARNING e o teto não existe')
                break
        else:
            raise AssertionError(f'SET LOCAL sem `with` na linha {n + 1}')
    assert achou >= 3, 'sumiram SET LOCALs — teste desatualizado'
    assert 'SET LOCAL statement_timeout' in FONTE
    assert 'SET LOCAL lock_timeout' in FONTE


def test_freia_pela_fila_e_pelo_lock():
    """Empurrar pra fila cheia não aumenta vazão; e há OUTRO escritor na tabela."""
    assert "wait_event_type = 'Lock'" in FONTE, 'sem medir o impacto no banco'
    assert re.search(r"if fila > o\['fila_alta'\]", FONTE), 'sem freio de fila'


# ------------------------------------------------------- o comando, no DB --

def _tribunal():
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJMG', 'sigla_djen': 'TJMG'})
    return t


def _quebrado(n=1):
    """Cria processos no estado que o `{2,5}` deixou, com `atualizado_em` velho."""
    import datetime as dt

    from django.utils import timezone

    from tribunals.models import Process
    t = _tribunal()
    velha = timezone.now() - dt.timedelta(days=30)
    ps = [Process.objects.create(
        tribunal=t, numero_cnj=f'500{i:04d}-11.2025.8.13.0001',
        classe_nome='PROCEDIMENTO COMUM CÍVEL (7)', classe_codigo='')
        for i in range(n)]
    Process.objects.filter(pk__in=[p.pk for p in ps]).update(atualizado_em=velha)
    return [Process.objects.get(pk=p.pk) for p in ps]


@pytest.fixture(autouse=True)
def _sem_checkpoint_vazado():
    from django.core.cache import cache
    cache.delete(WM)          # NUNCA `cache.clear()`
    yield
    cache.delete(WM)


@pytest.mark.django_db(transaction=True)
def test_dry_run_nao_escreve_nada():
    """Um `--dry-run` que escreve já custou 39.303 `Parte` órfãs em produção."""
    from tribunals.models import ClasseJudicial, Process
    p, = _quebrado()
    antes = Process.objects.values('classe_codigo', 'classe_nome', 'classe_id',
                                   'atualizado_em').get(pk=p.pk)
    ClasseJudicial.objects.filter(codigo='7').delete()

    out = StringIO()
    call_command('backfill_classe', de=p.pk - 1, ate=p.pk, dry_run=True,
                 sem_checkpoint=True, sleep=0, stdout=out)

    depois = Process.objects.values('classe_codigo', 'classe_nome', 'classe_id',
                                    'atualizado_em').get(pk=p.pk)
    assert depois == antes, '--dry-run escreveu'
    assert not ClasseJudicial.objects.filter(codigo='7').exists(), \
        '--dry-run criou linha de catálogo'
    assert 'DRY-RUN' in out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_a_corrida_recupera_fecha_a_fk_e_toca_a_campainha():
    from tribunals.models import ClasseJudicial, Process
    p, = _quebrado()
    antes = Process.objects.values_list('atualizado_em', flat=True).get(pk=p.pk)

    call_command('backfill_classe', de=p.pk - 1, ate=p.pk,
                 sem_checkpoint=True, sleep=0, stdout=StringIO())

    d = Process.objects.values('classe_codigo', 'classe_nome', 'classe_id',
                               'atualizado_em').get(pk=p.pk)
    assert d['classe_codigo'] == '7'
    assert d['classe_nome'] == 'PROCEDIMENTO COMUM CÍVEL'
    assert d['classe_id'] == '7', 'FK do catálogo não foi fechada'
    assert d['atualizado_em'] > antes, 'campainha não tocou — busca fica velha'
    assert ClasseJudicial.objects.filter(codigo='7').exists()


@pytest.mark.django_db(transaction=True)
def test_e_idempotente():
    from tribunals.models import Process
    p, = _quebrado()
    for _ in range(2):
        call_command('backfill_classe', de=p.pk - 1, ate=p.pk,
                     sem_checkpoint=True, sleep=0, stdout=StringIO())
    d = Process.objects.values('classe_codigo', 'classe_nome').get(pk=p.pk)
    assert d == {'classe_codigo': '7', 'classe_nome': 'PROCEDIMENTO COMUM CÍVEL'}


@pytest.mark.django_db(transaction=True)
def test_teto_de_linhas_para_com_erro_e_o_numero_real():
    """Regra nº 2: teto é ERRO com o número real, nunca `return` discreto."""
    ps = _quebrado(3)
    err = StringIO()
    call_command('backfill_classe', de=ps[0].pk - 1, ate=ps[-1].pk, bloco=1,
                 teto_linhas=1, sem_checkpoint=True, sleep=0,
                 stdout=StringIO(), stderr=err)
    msg = err.getvalue()
    assert 'TETO DE LINHAS' in msg
    assert 'FALTA rodar de' in msg
    assert str(ps[-1].pk) in msg, 'parou sem dizer até onde faltava'


@pytest.mark.django_db(transaction=True)
def test_teto_de_tempo_tambem_grita():
    ps = _quebrado(3)
    err = StringIO()
    call_command('backfill_classe', de=ps[0].pk - 1, ate=ps[-1].pk, bloco=1,
                 max_segundos=1, sem_checkpoint=True, sleep=1.2,
                 stdout=StringIO(), stderr=err)
    assert 'TETO DE TEMPO' in err.getvalue()
    assert 'FALTA rodar de' in err.getvalue()


@pytest.mark.django_db(transaction=True)
def test_kill_switch_para_a_corrida():
    from django.core.cache import cache

    from enrichers.management.commands.backfill_classe import OFF
    from tribunals.models import Process
    p, = _quebrado()
    cache.set(OFF, True, 60)
    try:
        out = StringIO()
        call_command('backfill_classe', de=p.pk - 1, ate=p.pk,
                     sem_checkpoint=True, sleep=0, stdout=out)
        assert 'kill switch' in out.getvalue()
        assert Process.objects.values_list('classe_codigo', flat=True).get(
            pk=p.pk) == '', 'escreveu com o kill switch ligado'
    finally:
        cache.delete(OFF)


@pytest.mark.django_db(transaction=True)
def test_leitura_estreita_e_larga_recuperam_o_mesmo():
    """O default lê só o buraco (20-30x mais barato) — e não perde reparo.

    `nada`/`conflito`/`fk_orfa` exigem `classe_codigo <> ''` por construção,
    então a leitura estreita não pode deixar de reparar nada: ela só deixa de
    CONTAR denominador. Medido em prod (bloco de 20.000 pks): 18-34 ms contra
    342-1.117 ms.
    """
    from tribunals.models import Process
    ps = _quebrado(2)
    out = StringIO()
    call_command('backfill_classe', de=ps[0].pk - 1, ate=ps[-1].pk,
                 sem_checkpoint=True, sleep=0, json=True, stdout=out)
    import json as _json
    r = _json.loads(out.getvalue().splitlines()[-1])
    assert r['recupera'] == 2 and r['escritos'] == 2
    assert r['com_denominador'] is False
    assert r['nada'] == r['conflito'] == r['fk_orfa'] == 0

    # com denominador: mesma faixa, já reparada ⇒ tudo `nada`, zero escrita
    out = StringIO()
    call_command('backfill_classe', de=ps[0].pk - 1, ate=ps[-1].pk,
                 com_denominador=True, sem_checkpoint=True, sleep=0,
                 json=True, stdout=out)
    r = _json.loads(out.getvalue().splitlines()[-1])
    assert r['nada'] == 2 and r['escritos'] == 0
    assert Process.objects.filter(pk__in=[p.pk for p in ps],
                                  classe_codigo='7').count() == 2


@pytest.mark.django_db(transaction=True)
def test_shard_tem_checkpoint_proprio():
    """Faixas disjuntas em paralelo: um shard não pode apagar o marco do outro.

    Sem isto, dois processos gravando a MESMA chave fariam o checkpoint pular
    para trás e para frente — e a retomada perderia faixa inteira em silêncio,
    que é o corte mudo com outro nome.
    """
    from django.core.cache import cache
    ps = _quebrado(2)
    try:
        call_command('backfill_classe', de=ps[0].pk - 1, ate=ps[-1].pk,
                     shard='a', sleep=0, stdout=StringIO())
        assert cache.get(f'{WM}:a') == ps[-1].pk
        assert cache.get(WM) is None, 'o shard escreveu no checkpoint global'
        # e o --zerar-checkpoint tem que mirar o checkpoint DO SHARD
        call_command('backfill_classe', shard='a', zerar_checkpoint=True,
                     stdout=StringIO())
        assert cache.get(f'{WM}:a') is None
    finally:
        cache.delete(f'{WM}:a')


def test_freio_mede_varredura_e_nao_densidade():
    """ms por 1.000 pks, não ms por linha escrita.

    Medido em 59 blocos reais de produção (29/08): ms/1.000 pks varia 3,5x
    (p50 315 · p90 567 · max 1.093) e ms/linha escrita varia **25x**
    (p50 8,4 · p90 16,7 · max 95,9), porque o custo é a LEITURA e a densidade
    de linhas quebradas muda por faixa. Com a métrica errada o shard `d` parou
    sozinho a 37,56 ms/linha sem que nada estivesse caro — o mesmo erro que a
    1ª versão do backfill de assunto cometeu ao medir duração de bloco.
    """
    assert 'parar_ms_kpk' in FONTE and 'freio_ms_kpk' in FONTE
    assert 'parar_ms_linha' not in FONTE, 'a métrica de densidade voltou'
    # o freio TEM que rodar em bloco sem escrita nenhuma: faixa sem reparo
    # continua custando I/O e era ela que escapava do `if n_escritos:`
    i = FONTE.find('sleep, custo_caro = self._freio(')
    assert i > 0
    assert 'if n_escritos' not in FONTE[max(0, i - 200):i], (
        'freio atrás de `if n_escritos` ignora exatamente a faixa cara e vazia')


@pytest.mark.django_db(transaction=True)
def test_freio_para_com_erro_quando_a_varredura_fica_cara():
    """Teto de custo é ERRO com o número real, e diz onde parou."""
    ps = _quebrado(6)
    err, out = StringIO(), StringIO()
    call_command('backfill_classe', de=ps[0].pk - 1, ate=ps[-1].pk, bloco=1,
                 parar_ms_kpk=0.0, freio_ms_kpk=0.0, sem_checkpoint=True,
                 sleep=0, stdout=out, stderr=err)
    msg = err.getvalue()
    assert 'CUSTO DE VARREDURA' in msg and 'ms por 1.000' in msg
    assert 'FALTA rodar de' in msg
