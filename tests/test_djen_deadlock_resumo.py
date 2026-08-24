"""O resumo do processo trava na ordem do pk — e deadlock é retry CONTADO.

CONTEXTO (censo completo do `FailedJobRegistry` da `djen_backfill`, 24/08/2026).

Das 703 falhas acumuladas, depois de o OOM do TJDFT sair do caminho, a maior
fonte de dia queimado virou o **deadlock em `tribunals_process`**:

    342  (48,6%)  OOM — resolvido em 24/08 (orçamento de bytes em voo)
    203  (28,9%)  deadlock  → TRF2 55 · TRF3 50 · TRF6 37 · TRF4 30 · TJSP 17
    131  (18,6%)  id órfão (hash expirado, não é falha nova)
     19   (2,7%)  transporte/timeout/abandonado

A assinatura, lida no `exc_info` dos 150 jobs de deadlock ainda vivos no
registry (o resto já tinha expirado) — 100% deles no MESMO ponto:

    File ".../django/db/models/query.py", line 924, in bulk_update
        rows_updated += queryset.filter(pk__in=pks).update(**update_kwargs)
    django.db.utils.OperationalError: deadlock detected
    DETAIL:  Process 190600 waits for ShareLock on transaction 575242916;
             blocked by process 191118.
             Process 191118 waits for ShareLock on transaction 575243302;
             blocked by process 190600.
    CONTEXT:  while locking tuple (1126731,22) in relation "tribunals_process"

Ou seja: `_flush_resumo`, não `_process_page` (que já ordenava por CNJ desde
18/08). O `bulk_update` do Django envolve TODOS os batches num único
`transaction.atomic(savepoint=False)`; com a lista de objetos fora de ordem,
o batch 1 de um worker trava pks que o batch 2 de outro já segurava.

No banco, no dia da medição: **98 runs `failed` com deadlock em 24 h**
(18,2% das 538 falhas do período), concentrados em TJPR 64 · TRF6 13 · TJRJ 8.

O que estes testes protegem:
  1. a ordem de aquisição de lock é CRESCENTE por pk, provada no SQL emitido
     (`FOR NO KEY UPDATE ... ORDER BY id` antes do UPDATE) — se o `sort` ou o
     pré-lock sumirem num refactor, o teste cai;
  2. deadlock é retentado com teto, e o número REAL de tentativas fica no
     `IngestionRun.erros` (regra nº 2: teto é alerta, nunca corte mudo);
  3. quem NÃO ganhou movimentação nova não é reescrito — em 24 h de produção
     70,3% das publicações processadas eram duplicadas e 35,6% dos runs com
     dado fecharam com ZERO publicação nova. Reescrever a linha pra gravar o
     mesmo valor é a escrita que abria a janela de contenção;
  4. o reparo continua: processo que aparece no diário com
     `total_movimentacoes=0` é recalculado mesmo sem novidade — o trigger
     `mov_update_process_agg` da migration 0004 NÃO existe no banco de
     produção (conferido em `pg_trigger`, 24/08/2026), então este código é o
     único que mantém o resumo.
"""
import re
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext

from djen import ingestion as I  # noqa: N812 — mesmo apelido dos vizinhos de djen/


class CausaDeadlockError(Exception):
    """Dublê do erro do psycopg: o que identifica deadlock é o SQLSTATE."""
    sqlstate = '40P01'


def erro_de_deadlock() -> OperationalError:
    exc = OperationalError(
        'deadlock detected\nDETAIL:  Process 190600 waits for ShareLock...\n'
        'CONTEXT:  while locking tuple (1126731,22) in relation "tribunals_process"'
    )
    exc.__cause__ = CausaDeadlockError()
    return exc


@pytest.fixture
def tribunal(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF2', defaults={'nome': 'TRF 2ª Região', 'sigla_djen': 'TRF2'})
    return t


def cria_processos(tribunal, n=6, movs_por_processo=2, total_gravado=None):
    """Cria n processos COM movimentações. `total_gravado` força o valor
    (errado) de `total_movimentacoes` pra provar quem é recalculado.

    ⚠️ O `total_gravado` é aplicado DEPOIS de inserir as movimentações, por
    `UPDATE` direto: o banco de TESTE roda as migrations e portanto TEM o
    trigger `mov_update_process_agg` (migration 0004), que recalcula o resumo
    a cada INSERT. O banco de PRODUÇÃO não tem — conferido em `pg_trigger` em
    24/08/2026, só sobrou `process_set_ano_cnj`. É por isso que `_flush_resumo`
    é o único mantenedor do resumo em prod, e é por isso que ele não pode
    simplesmente parar de calcular.
    """
    from tribunals.models import Movimentacao, Process
    procs = []
    for i in range(n):
        cnj = f'{i:07d}-11.2024.4.02.0001'
        p = Process.objects.create(tribunal=tribunal, numero_cnj=cnj)
        for k in range(movs_por_processo):
            Movimentacao.objects.create(
                processo=p, tribunal=tribunal, external_id=f'{i}-{k}',
                data_disponibilizacao=datetime(2026, 8, 20, 12, 0,
                                               tzinfo=UTC),
            )
        if total_gravado is not None:
            Process.objects.filter(pk=p.pk).update(total_movimentacoes=total_gravado)
        p.refresh_from_db()
        procs.append(p)
    return procs


def ids_do_update(sql: str) -> list[int]:
    """Os pks que um `UPDATE ... WHERE id IN (...)` trava, na ordem em que
    aparecem no SQL."""
    trecho = sql.rsplit(' IN (', 1)[-1]
    return [int(x) for x in re.findall(r'\d+', trecho.split(')')[0])]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Ordem determinística de aquisição de lock
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.django_db(transaction=True)
def test_trava_em_ordem_crescente_de_pk(tribunal):
    """Sem ordem comum não existe ciclo de espera — e ordenar a LISTA não basta:
    dentro de um `UPDATE ... WHERE id IN (...)` quem manda na ordem é o plano.

    Medido com EXPLAIN em produção (`tribunals_process`, 86 M linhas):

        Update on tribunals_process
          ->  Index Scan using tribunals_process_pkey       ← sobe ordenado
        LockRows                                            ← o pré-lock
          ->  Index Scan using tribunals_process_pkey

    Depender do plano é depender de estatística. Por isso a transação começa
    travando explicitamente, `ORDER BY id FOR NO KEY UPDATE`.
    """
    procs = cria_processos(tribunal, n=6)
    # Ordem EMBARALHADA de propósito: é exatamente o que o `set` de CNJs
    # tocados entregava, e o que fechava o ciclo entre dois workers.
    cnjs = [p.numero_cnj for p in procs][::-1]
    novidade = {p.numero_cnj for p in procs}

    with CaptureQueriesContext(connection) as capturadas:
        I._flush_resumo(tribunal, cnjs, novidade, None)

    sqls = [q['sql'] for q in capturadas.captured_queries]
    locks = [s for s in sqls if 'FOR NO KEY UPDATE' in s]
    assert locks, 'sumiu o pré-lock ordenado — a ordem voltou a depender do plano'
    assert 'ORDER BY' in locks[0], 'pré-lock sem ORDER BY não ordena nada'
    travados = ids_do_update(locks[0])
    assert travados == sorted(travados), f'lock fora de ordem: {travados}'

    updates = [s for s in sqls if s.lstrip().upper().startswith('UPDATE "TRIBUNALS_PROCESS"')]
    assert updates, 'nada foi escrito'
    escritos = ids_do_update(updates[0])
    assert escritos == sorted(escritos), f'UPDATE fora de ordem de pk: {escritos}'


@pytest.mark.django_db(transaction=True)
def test_lotes_saem_em_faixas_crescentes_e_disjuntas(tribunal):
    """Cada lote é uma transação PRÓPRIA e cobre uma faixa de pk maior que a
    anterior. Era o contrário disso — um `atomic()` só, com batches em ordem
    arbitrária — que fechava o ciclo entre dois workers do mesmo tribunal."""
    procs = cria_processos(tribunal, n=7)
    cnjs = [p.numero_cnj for p in procs][::-1]
    novidade = {p.numero_cnj for p in procs}

    with patch.object(I, 'LOTE_UPDATE_PROCESS', 3), \
            CaptureQueriesContext(connection) as capturadas:
        I._flush_resumo(tribunal, cnjs, novidade, None)

    faixas = [ids_do_update(q['sql']) for q in capturadas.captured_queries
              if q['sql'].lstrip().upper().startswith('UPDATE "TRIBUNALS_PROCESS"')]
    assert len(faixas) == 3, f'esperava 3 lotes de até 3 linhas, veio {len(faixas)}'
    for faixa in faixas:
        assert faixa == sorted(faixa)
    assert max(faixas[0]) < min(faixas[1]) < max(faixas[1]) < min(faixas[2])


# ═════════════════════════════════════════════════════════════════════════════
# 2. Retry com teto, registrado no run
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.django_db(transaction=True)
def test_deadlock_e_retentado_e_contado_no_run(tribunal):
    """Deadlock é erro TRANSITÓRIO: quem perdeu não fez nada de errado, só
    chegou depois. Mas um retry que ninguém conta esconde a regressão — o
    número real de tentativas fica no `IngestionRun.erros`."""
    from tribunals.models import IngestionRun
    procs = cria_processos(tribunal, n=3)
    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=date(2026, 8, 20), janela_fim=date(2026, 8, 20))

    real = I.Process.objects.bulk_update
    chamadas = {'n': 0}

    def falha_uma_vez(*a, **kw):
        chamadas['n'] += 1
        if chamadas['n'] == 1:
            raise erro_de_deadlock()
        return real(*a, **kw)

    with patch.object(I.Process.objects, 'bulk_update', falha_uma_vez), \
            patch.object(I.time, 'sleep') as dorme:
        I._flush_resumo(tribunal, [p.numero_cnj for p in procs],
                        {p.numero_cnj for p in procs}, run)

    assert chamadas['n'] == 2, 'não retentou o lote que perdeu o deadlock'
    assert dorme.called, 'retentou sem backoff — dois perdedores voltam juntos'
    run.refresh_from_db()
    alerta = [e for e in run.erros if e['erro'] == 'deadlock_em_tribunals_process']
    assert alerta, 'deadlock retentado sem deixar rastro no run'
    assert alerta[0]['ocorrencias'] == 1
    assert alerta[0]['tentativas_max'] == 2
    assert alerta[0]['esgotou_tentativas'] is False


@pytest.mark.django_db(transaction=True)
def test_teto_de_tentativas_vira_erro_e_falha_o_run(tribunal):
    """Teto é alerta, nunca corte mudo: esgotar as tentativas propaga a exceção
    (o dia falha e volta pelo watchdog) COM o número no run."""
    from tribunals.models import IngestionRun
    procs = cria_processos(tribunal, n=3)
    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=date(2026, 8, 20), janela_fim=date(2026, 8, 20))

    def sempre_falha(*a, **kw):
        raise erro_de_deadlock()

    with patch.object(I.Process.objects, 'bulk_update', sempre_falha), \
            patch.object(I.time, 'sleep'), pytest.raises(OperationalError):
        I._flush_resumo(tribunal, [p.numero_cnj for p in procs],
                        {p.numero_cnj for p in procs}, run)

    run.refresh_from_db()
    alerta = next(e for e in run.erros if e['erro'] == 'deadlock_em_tribunals_process')
    assert alerta['esgotou_tentativas'] is True
    assert alerta['tentativas_max'] == I.DEADLOCK_TENTATIVAS
    assert alerta['teto_tentativas'] == I.DEADLOCK_TENTATIVAS


@pytest.mark.django_db(transaction=True)
def test_erro_que_nao_e_deadlock_nao_e_retentado(tribunal):
    """Retry é pra erro transitório. Erro de verdade tem que subir na hora —
    retentar 5 vezes um bug custa 5 vezes o tempo e esconde a causa."""
    procs = cria_processos(tribunal, n=2)
    chamadas = {'n': 0}

    def falha_de_verdade(*a, **kw):
        chamadas['n'] += 1
        raise OperationalError('could not serialize access')

    with patch.object(I.Process.objects, 'bulk_update', falha_de_verdade), \
            pytest.raises(OperationalError):
        I._flush_resumo(tribunal, [p.numero_cnj for p in procs],
                        {p.numero_cnj for p in procs}, None)
    assert chamadas['n'] == 1


def test_deadlock_reconhecido_por_sqlstate_e_por_texto():
    """O SQLSTATE 40P01 é o critério; o texto é fallback pra driver que não o
    exponha. Nenhum dos dois pode virar `except OperationalError: retry` cego."""
    assert I._e_deadlock(erro_de_deadlock())
    assert I._e_deadlock(OperationalError('deadlock detected'))
    assert not I._e_deadlock(OperationalError('server closed the connection'))


# ═════════════════════════════════════════════════════════════════════════════
# 3. Nível 2: não reescrever o que não mudou
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.django_db(transaction=True)
def test_processo_sem_movimentacao_nova_nao_e_reescrito(tribunal):
    """70,3% das publicações processadas em 24 h de produção eram duplicadas, e
    158 dos 444 runs com dado (35,6%) não trouxeram UMA publicação nova. O
    resumo desses processos é bit a bit o mesmo — reescrevê-lo é bloat, WAL e
    janela de deadlock de graça."""
    procs = cria_processos(tribunal, n=4)
    antes = list(I.Process.objects.filter(pk__in=[p.pk for p in procs])
                 .values_list('pk', 'data_enriquecimento_djen'))

    with CaptureQueriesContext(connection) as capturadas:
        I._flush_resumo(tribunal, [p.numero_cnj for p in procs], set(), None)

    escritas = [q for q in capturadas.captured_queries
                if q['sql'].lstrip().upper().startswith('UPDATE "TRIBUNALS_PROCESS"')]
    assert not escritas, 'reescreveu processo que não mudou'
    agregacoes = [q for q in capturadas.captured_queries
                  if 'MIN(' in q['sql'].upper() and 'TRIBUNALS_MOVIMENTACAO' in q['sql'].upper()]
    assert not agregacoes, 'recontou o histórico inteiro de quem não mudou'
    depois = list(I.Process.objects.filter(pk__in=[p.pk for p in procs])
                  .values_list('pk', 'data_enriquecimento_djen'))
    assert antes == depois


@pytest.mark.django_db(transaction=True)
def test_processo_com_movimentacao_nova_e_recalculado(tribunal):
    """E quem mudou continua sendo recalculado — exato, pelo COUNT/MIN/MAX."""
    procs = cria_processos(tribunal, n=3, movs_por_processo=2, total_gravado=99)
    alvo = procs[1]
    I._flush_resumo(tribunal, [p.numero_cnj for p in procs], {alvo.numero_cnj}, None)

    alvo.refresh_from_db()
    assert alvo.total_movimentacoes == 2
    assert alvo.data_enriquecimento_djen is not None
    assert alvo.ultima_movimentacao_em is not None
    intocado = I.Process.objects.get(pk=procs[0].pk)
    assert intocado.total_movimentacoes == 99, 'reescreveu quem não tinha novidade'


@pytest.mark.django_db(transaction=True)
def test_total_zerado_e_reparado_mesmo_sem_novidade(tribunal):
    """Processo que aparece no diário mas está com `total_movimentacoes=0` tem
    linha nunca somada — o trigger `mov_update_process_agg` (migration 0004)
    não existe no banco de produção, conferido em `pg_trigger` em 24/08/2026.
    Pular esse é deixar erro velho para sempre."""
    procs = cria_processos(tribunal, n=2, movs_por_processo=3, total_gravado=0)
    I._flush_resumo(tribunal, [p.numero_cnj for p in procs], set(), None)
    for p in procs:
        p.refresh_from_db()
        assert p.total_movimentacoes == 3, 'resumo zerado não foi reparado'


@pytest.mark.django_db(transaction=True)
def test_process_page_marca_so_o_que_chegou_novo(tribunal):
    """A novidade é por `external_id`: o mesmo processo pode vir com 3
    publicações das quais 2 já estavam no banco. Reprocessar a MESMA página
    não pode marcar novidade nenhuma — é o caso do dia recoletado."""
    itens = [{
        'id': 900 + i,
        'numeroprocessocommascara': f'{i:07d}-22.2024.4.02.0002',
        'numero_processo': f'{i:07d}2220244020002',
        'siglaTribunal': 'TRF2', 'nomeOrgao': 'Vara', 'idOrgao': 1,
        'tipoComunicacao': 'Intimação', 'tipoDocumento': 'Despacho',
        'data_disponibilizacao': '2026-08-20', 'datadisponibilizacao': '2026-08-20',
        'texto': 'texto', 'destinatarios': [], 'destinatarioadvogados': [],
        'nomeClasse': '', 'codigoClasse': '', 'link': '',
        'numeroComunicacao': '', 'hash': f'h{i}', 'meio': 'D',
        'meiocompleto': 'Diário', 'status': 'ok', 'ativo': True,
        'data_cancelamento': None, 'motivo_cancelamento': '',
    } for i in range(3)]

    tocados, novidade = set(), set()
    I._process_page(itens, tribunal, None, tocados, novidade)
    assert len(novidade) == 3 and novidade == tocados

    tocados2, novidade2 = set(), set()
    I._process_page(itens, tribunal, None, tocados2, novidade2)
    assert len(tocados2) == 3, 'perdeu o processo tocado na segunda passada'
    assert novidade2 == set(), 'página 100% duplicada marcou novidade'
