"""`repop_classe_assunto` — fechar a FK do catálogo sem inventar vínculo (#104).

Medido em produção em 31/08/2026, com a consulta registrada:

    SELECT count(*) FROM tribunals_process
     WHERE classe_codigo IS NOT NULL AND classe_codigo <> '' AND classe_id IS NULL
    -- 8.054.334

O que estes testes protegem:
  1. **varredura por faixa de pk**, nunca `ctid IN (SELECT … LIMIT n)` — foi o
     `LIMIT` sobre Seq Scan que fez a versão anterior nunca terminar em 104,1 M
     de linhas (a pendência não andou de 30/08 para 31/08);
  2. **um UPDATE fecha `classe` E `assunto`** — a linha é reescrita uma vez só;
     duas passadas custariam duas versões de tupla e 25 inserções de índice a
     mais por linha, sobre 8 M de linhas;
  3. **abstenção declarada** em código fora da TPU (regra nº 6). Não existe FK
     no banco (`pg_constraint` em `tribunals_process`: zero do tipo `f`), então
     o Postgres aceitaria o vínculo pendurado e quem quebraria seria o ORM;
  4. **`--criar-catalogo` não inventa nome**: cria a linha a partir do nome que
     já está gravado, e abstém quando não há nome;
  5. **não sobrescreve FK já fechada** (`COALESCE`) — entre o SELECT e o UPDATE
     a ingestão ao vivo escreve na mesma tabela;
  6. **não toca a campainha**, e o motivo é verificável: `classe_id`/`assunto_id`
     não aparecem no documento do ES. Se alguém os puser lá, o teste quebra;
  7. teto atingido é ERRO com o número REAL (regra nº 2);
  8. `--dry-run` não escreve — um dry-run que escrevia já custou 39.303 `Parte`
     órfãs em produção;
  9. `SET LOCAL` dentro de `transaction.atomic()` — solto no autocommit ele é
     descartado e o teto simplesmente não existe.
"""
import json as _json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test.utils import CaptureQueriesContext

from tribunals.management.commands.repop_classe_assunto import OFF, WM

FONTE = open('tribunals/management/commands/repop_classe_assunto.py').read()
#: só o CÓDIGO — a docstring do módulo cita `ctid` e `SET LOCAL` justamente para
#: explicar o defeito que estes testes proíbem. Conferir a prosa junto faria o
#: teste falhar pela documentação e passar pelo bug.
CODIGO = FONTE.split('"""', 2)[2]
WM_PROC = f'{WM}:process'


def _tribunal():
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJ de Minas Gerais'})
    return t


def _catalogo():
    from tribunals.models import Assunto, ClasseJudicial
    ClasseJudicial.objects.get_or_create(codigo='7',
                                         defaults={'nome': 'PROCEDIMENTO COMUM CÍVEL'})
    Assunto.objects.get_or_create(codigo='10375', defaults={'nome': 'Aposentadoria'})


def _proc(i=0, **kw):
    """Uma linha no estado do buraco: código gravado, FK NULL."""
    import datetime as dt

    from django.utils import timezone

    from tribunals.models import Process
    campos = {'classe_codigo': '7', 'classe_nome': 'PROCEDIMENTO COMUM CÍVEL',
              'assunto_codigo': '10375', 'assunto_nome': 'Aposentadoria'}
    campos.update(kw)
    p = Process.objects.create(
        tribunal=_tribunal(), numero_cnj=f'500{i:04d}-11.2025.8.13.0001', **campos)
    velha = timezone.now() - dt.timedelta(days=30)
    Process.objects.filter(pk=p.pk).update(atualizado_em=velha)
    return Process.objects.get(pk=p.pk)


@pytest.fixture(autouse=True)
def _sem_estado_vazado():
    from django.core.cache import cache
    for k in (WM, WM_PROC, f'{WM_PROC}:a', OFF):
        cache.delete(k)          # NUNCA `cache.clear()`
    yield
    for k in (WM, WM_PROC, f'{WM_PROC}:a', OFF):
        cache.delete(k)


# ------------------------------------------------------- a varredura --------

def test_varre_por_faixa_de_pk_e_nao_por_ctid():
    """O motivo de a pendência não ter andado em dois meses.

    `UPDATE … WHERE ctid IN (SELECT ctid … LIMIT n)` para cedo enquanto sobra
    linha quebrada na frente da tabela; quando a frente esvazia, cada iteração
    varre mais fundo até varrer os 28 GB inteiros. Custo quadrático — em
    104,1 M de linhas não termina.
    """
    assert 'ctid' not in CODIGO, 'a varredura por ctid voltou'
    assert 'WHERE id > %s AND id <= %s' in CODIGO


def test_set_local_sempre_dentro_de_transacao():
    """`SET LOCAL` em autocommit é DESCARTADO — o teto deixa de existir."""
    linhas = CODIGO.splitlines()
    executa_set_local = [i for i, ln in enumerate(linhas)
                         if 'SET LOCAL' in ln and 'execute(' in ln]
    assert executa_set_local, 'nenhum SET LOCAL — o teto sumiu'
    for i in executa_set_local:
        vizinhanca = '\n'.join(linhas[max(0, i - 6):i])
        assert 'transaction.atomic()' in vizinhanca, \
            f'`SET LOCAL` sem atomic() por perto na linha {i + 1}'


# ------------------------------------------------------- o reparo -----------

@pytest.mark.django_db(transaction=True)
def test_liga_classe_e_assunto_no_mesmo_update():
    """Duas FKs, uma reescrita de linha.

    A tabela tem 25 índices e o UPDATE não é HOT (a FK é indexada): reparar
    `classe` e `assunto` em passadas separadas dobraria versões de tupla e
    inserções de índice sobre 8 M de linhas, de graça.
    """
    from django.db import connection

    from tribunals.models import Process
    _catalogo()
    p = _proc()
    with CaptureQueriesContext(connection) as ctx:
        call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                     sem_checkpoint=True, sleep=0, stdout=StringIO())
    d = Process.objects.values('classe_id', 'assunto_id').get(pk=p.pk)
    assert d == {'classe_id': '7', 'assunto_id': '10375'}
    updates = [q['sql'] for q in ctx.captured_queries
               if q['sql'].lstrip().upper().startswith('UPDATE TRIBUNALS_PROCESS')]
    assert len(updates) == 1, f'{len(updates)} UPDATEs para a mesma linha'


@pytest.mark.django_db(transaction=True)
def test_e_idempotente():
    from tribunals.models import Process
    _catalogo()
    p = _proc()
    for _ in range(2):
        call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                     sem_checkpoint=True, sleep=0, stdout=StringIO())
    assert Process.objects.values('classe_id', 'assunto_id').get(pk=p.pk) == \
        {'classe_id': '7', 'assunto_id': '10375'}


@pytest.mark.django_db(transaction=True)
def test_dry_run_nao_escreve_nada():
    """Um `--dry-run` que escreve já custou 39.303 `Parte` órfãs em produção."""
    from tribunals.models import Assunto, Process
    _catalogo()
    p = _proc(assunto_codigo='99999', assunto_nome='Assunto Inexistente')
    antes = Process.objects.values('classe_id', 'assunto_id',
                                   'atualizado_em').get(pk=p.pk)
    out = StringIO()
    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk, dry_run=True,
                 criar_catalogo=True, sem_checkpoint=True, sleep=0, stdout=out)
    assert Process.objects.values('classe_id', 'assunto_id',
                                  'atualizado_em').get(pk=p.pk) == antes
    assert not Assunto.objects.filter(codigo='99999').exists(), \
        '--dry-run criou linha de catálogo'
    assert 'DRY-RUN' in out.getvalue()


# ------------------------------------------------------- abster > chutar ----

@pytest.mark.django_db(transaction=True)
def test_abstem_em_codigo_fora_da_tpu_e_conta_por_codigo_e_tribunal():
    """Não há FK no BANCO: ligar sem catálogo criaria vínculo pendurado.

    `pg_constraint` sobre `tribunals_process` (31/08/2026) devolve **zero**
    constraints do tipo `f` — o Postgres aceitaria `assunto_id='99999'` sem
    reclamar, e o estouro apareceria depois, no `proc.assunto` do ORM.
    """
    from tribunals.models import Process
    _catalogo()
    p = _proc(assunto_codigo='99999', assunto_nome='Assunto Inexistente')
    out, err = StringIO(), StringIO()
    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 sem_checkpoint=True, sleep=0, json=True, stdout=out, stderr=err)
    r = _json.loads(out.getvalue().splitlines()[-1])

    d = Process.objects.values('classe_id', 'assunto_id').get(pk=p.pk)
    assert d['classe_id'] == '7', 'a classe, que TEM catálogo, não foi ligada'
    assert d['assunto_id'] is None, 'ligou FK sem linha no catálogo'
    assert r['orfao_assunto_id'] == 1 and r['liga_assunto_id'] == 0
    assert r['orfaos_por_codigo']['assunto_id:99999'] == 1
    assert r['orfaos_por_tribunal']['assunto_id:TJMG'] == 1
    # regra nº 2: abstenção em massa é ERRO registrado, não silêncio
    assert 'ABSTEVE' in err.getvalue() and '99999' in err.getvalue()


@pytest.mark.django_db(transaction=True)
def test_criar_catalogo_usa_o_nome_da_linha():
    """604.954 linhas de assunto (1.255 códigos) esperam por isto.

    São a faixa 13xxx/14xxx da TPU trabalhista — `Verbas Rescisórias`,
    `Adicional de Insalubridade` —, que o catálogo nunca recebeu porque foi
    semeado antes de os TRTs entrarem no acervo. O nome está na própria linha.
    """
    from tribunals.models import Assunto, Process
    _catalogo()
    p = _proc(assunto_codigo='13970', assunto_nome='Verbas Rescisórias')
    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 criar_catalogo=True, sem_checkpoint=True, sleep=0,
                 stdout=StringIO())
    assert Assunto.objects.get(codigo='13970').nome == 'Verbas Rescisórias'
    assert Process.objects.values_list('assunto_id', flat=True).get(pk=p.pk) == '13970'


@pytest.mark.django_db(transaction=True)
def test_criar_catalogo_nao_inventa_nome():
    """Código sem nome NÃO vira catálogo com o código no lugar do nome.

    Medido: o mesmo código aparece com nome cheio em dezenas de milhares de
    linhas e vazio em algumas dezenas (13875 = 39.872 com nome, 59 sem). Criar
    pelo primeiro que aparecer deixaria o catálogo com `nome='13875'` para
    sempre — `codigo` é PK e o comando nunca reescreve linha existente.
    """
    from tribunals.models import Assunto, Process
    _catalogo()
    p = _proc(assunto_codigo='13875', assunto_nome='')
    out = StringIO()
    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 criar_catalogo=True, sem_checkpoint=True, sleep=0,
                 json=True, stdout=out)
    r = _json.loads(out.getvalue().splitlines()[-1])
    assert not Assunto.objects.filter(codigo='13875').exists()
    assert Process.objects.values_list('assunto_id', flat=True).get(pk=p.pk) is None
    assert r['orfao_sem_nome_assunto_id'] == 1
    assert r['catalogo_criado'] == 0


@pytest.mark.django_db(transaction=True)
def test_criar_catalogo_nao_aceita_codigo_fora_do_padrao_da_tpu():
    """`99999999` entrou no catálogo NACIONAL na primeira corrida real.

    A guarda não é hipótese: em 31/08/2026, dois minutos de corrida com
    `--criar-catalogo` criaram `tribunals.Assunto(codigo='99999999')` porque um
    tribunal publicou isso. A TPU é numérica e cabe em 5 dígitos (o maior
    código do nosso catálogo de assuntos é 57.501). Medido sobre os 604.954
    órfãos: 1.249 códigos (604.822 linhas) passam, 6 códigos (132 linhas, os
    `4010000x` e o `99999999`) não. O custo de abster é 0,02% das linhas; o de
    não abster é sujeira permanente numa tabela com FK `PROTECT`.
    """
    from tribunals.models import Assunto, Process
    _catalogo()
    p = _proc(assunto_codigo='99999999', assunto_nome='Alguma Coisa')
    out = StringIO()
    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 criar_catalogo=True, sem_checkpoint=True, sleep=0,
                 json=True, stdout=out, stderr=StringIO())
    r = _json.loads(out.getvalue().splitlines()[-1])
    assert not Assunto.objects.filter(codigo='99999999').exists(), \
        'criou código fora da TPU no catálogo nacional'
    assert Process.objects.values_list('assunto_id', flat=True).get(pk=p.pk) is None
    assert r['orfao_fora_do_padrao_assunto_id'] == 1
    assert r['catalogo_criado'] == 0
    # e o de 5 dígitos, na mesma corrida, TEM que passar — senão a guarda
    # estaria só recusando tudo e o teste passaria por engano
    q = _proc(1, assunto_codigo='13970', assunto_nome='Verbas Rescisórias')
    call_command('repop_classe_assunto', de=q.pk - 1, ate=q.pk,
                 criar_catalogo=True, sem_checkpoint=True, sleep=0,
                 stdout=StringIO())
    assert Process.objects.values_list('assunto_id', flat=True).get(pk=q.pk) == '13970'


@pytest.mark.django_db(transaction=True)
def test_nao_sobrescreve_fk_ja_fechada():
    """`COALESCE` repete a guarda do SELECT.

    Entre ler e escrever, a ingestão ao vivo pode ter fechado a FK — e ela
    sabe mais do que nós. Em 31/08 havia 21.853 linhas com
    `classe_id <> classe_codigo`: são outro fato, com outro dono, e este
    comando não os toca.
    """
    from tribunals.models import ClasseJudicial, Process
    _catalogo()
    ClasseJudicial.objects.get_or_create(codigo='198', defaults={'nome': 'OUTRA'})
    p = _proc()
    # divergente de propósito: código diz 7, FK diz 198
    Process.objects.filter(pk=p.pk).update(classe_id='198', assunto_id=None)

    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 sem_checkpoint=True, sleep=0, stdout=StringIO())
    d = Process.objects.values('classe_id', 'assunto_id').get(pk=p.pk)
    assert d['classe_id'] == '198', 'sobrescreveu FK que já estava fechada'
    assert d['assunto_id'] == '10375', 'não fechou a FK que estava aberta'


# ------------------------------------------------------- a campainha --------

@pytest.mark.django_db(transaction=True)
def test_nao_toca_a_campainha_porque_a_fk_nao_esta_no_doc_do_es():
    """8,05 M de reindexações que produziriam documentos idênticos.

    Os backfills irmãos tocam `atualizado_em = now()` porque mexem em campo
    que o índice serve. Aqui a afirmação "não muda o doc" fica presa: se
    `classe_id`/`assunto_id` entrarem no `processo_to_doc`, este teste
    quebra e a decisão volta à mesa.
    """
    from search.documents import processo_to_doc
    from tribunals.models import Process
    _catalogo()
    p = _proc()
    antes = Process.objects.values_list('atualizado_em', flat=True).get(pk=p.pk)
    doc_antes = processo_to_doc(Process.objects.get(pk=p.pk))

    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 sem_checkpoint=True, sleep=0, stdout=StringIO())

    depois = Process.objects.get(pk=p.pk)
    assert depois.atualizado_em == antes, 'tocou a campainha à toa'
    assert processo_to_doc(depois) == doc_antes, \
        'a FK mudou o documento do ES — a campainha passou a ser obrigatória'
    assert 'classe_id' not in doc_antes and 'assunto_id' not in doc_antes


# ------------------------------------------------------- tetos e freios -----

@pytest.mark.django_db(transaction=True)
def test_teto_de_linhas_para_com_erro_e_o_numero_real():
    """Regra nº 2: teto é ERRO com o número real, nunca `return` discreto."""
    _catalogo()
    ps = [_proc(i) for i in range(3)]
    err = StringIO()
    call_command('repop_classe_assunto', de=ps[0].pk - 1, ate=ps[-1].pk,
                 bloco=1, teto_linhas=1, sem_checkpoint=True, sleep=0,
                 stdout=StringIO(), stderr=err)
    msg = err.getvalue()
    assert 'TETO DE LINHAS' in msg
    assert 'FALTA rodar de' in msg
    assert str(ps[-1].pk) in msg, 'parou sem dizer até onde faltava'


@pytest.mark.django_db(transaction=True)
def test_kill_switch_para_a_corrida():
    from django.core.cache import cache

    from tribunals.models import Process
    _catalogo()
    p = _proc()
    cache.set(OFF, True, 60)
    out = StringIO()
    call_command('repop_classe_assunto', de=p.pk - 1, ate=p.pk,
                 sem_checkpoint=True, sleep=0, stdout=out)
    assert 'kill switch' in out.getvalue()
    assert Process.objects.values_list('classe_id', flat=True).get(pk=p.pk) is None, \
        'escreveu com o kill switch ligado'


@pytest.mark.django_db(transaction=True)
def test_shard_tem_checkpoint_proprio():
    """Faixas disjuntas em paralelo: um shard não pode apagar o marco do outro."""
    from django.core.cache import cache
    _catalogo()
    ps = [_proc(i) for i in range(2)]
    call_command('repop_classe_assunto', de=ps[0].pk - 1, ate=ps[-1].pk,
                 shard='a', sleep=0, stdout=StringIO())
    assert cache.get(f'{WM_PROC}:a') == ps[-1].pk
    assert cache.get(WM_PROC) is None, 'o shard escreveu no checkpoint global'
    call_command('repop_classe_assunto', shard='a', zerar_checkpoint=True,
                 stdout=StringIO())
    assert cache.get(f'{WM_PROC}:a') is None


def test_freio_mede_varredura_e_nao_densidade():
    """ms por 1.000 pks varridos — comparável entre faixas.

    Medido em produção em 31/08, blocos de 50.000 pks: 81-99 ms por 1.000 pks
    só lendo e 455-1.024 com escrita (3,94-4,64 ms por linha). A banda é larga
    porque a densidade de linhas quebradas varia 5x entre faixas VIZINHAS —
    2.559, 8.451 e 20.973 linhas em três blocos consecutivos. Por isso os tetos
    saem do máximo medido, e não da mediana.
    """
    assert 'freio_ms_kpk' in CODIGO and 'parar_ms_kpk' in CODIGO
    assert 'parar_ms_linha' not in CODIGO, 'a métrica de densidade voltou'
    i = CODIGO.find('sleep, custo_caro = self._freio(')
    assert i > 0
    assert 'if n' not in CODIGO[max(0, i - 200):i], \
        'freio atrás de `if escreveu` ignora exatamente a faixa cara e vazia'
