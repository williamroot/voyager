"""O watchdog não pode morrer calado — nem rodar código de sete dias atrás.

CONTEXTO (21/08/2026). Fui chamado pra consertar "o watchdog que morre por
timeout toda vez". Medi antes de mexer, e o que estava lá era outra coisa —
pior:

  * o FailedJobRegistry da fila `default` tinha **4.259** falhas. Censo
    COMPLETO (não amostra): **4.247 (99,7%)** eram jobs do datajud morrendo em
    ~30 ms com `ValueError: Invalid attribute name`, e só **11** eram
    `JobTimeoutException` do djen — todas as 11 num intervalo de 15 minutos do
    dia 17/08, em linhas de código que já nem existem mais;
  * a causa das duas coisas era a MESMA: os dois `worker_default` subiram em
    14/08 17:40 e nunca reiniciaram. O bind-mount `.:/app` entrega o `git pull`
    dentro do container, mas o processo Python guarda os módulos em memória.
    Sonda enfileirada no próprio worker (`builtins.eval`) devolveu:

        hasattr(djen.jobs, 'ressuscitar_dias_de_recuperacao') -> False
        hasattr(djen.proxies, 'sessao_rotativa')              -> False

    Ou seja: o reforço do watchdog deployado em 19/08 NUNCA executou, e
    **3.007 dias-tribunal** ficaram `failed` sem ninguém devolver;
  * os 11 timeouts de 17/08 não eram custo de algoritmo. As mesmas consultas,
    medidas em 21/08 com o banco sob carga de batch, somam **1,6 s** (pior
    tribunal em `_dias_cobertos`: 0,14 s). Era contenção de disco — e como são
    59 ticks numa fila com 2 workers, cada um segurando 120 s, eles bloquearam
    a FIFO e levaram o watchdog junto.

O que estes testes protegem:
  1. deploy que não recarregou vira ERRO com nome de módulo e idade — um
     `run` verde num worker de código velho é indistinguível de sucesso;
  2. orçamento estourado termina FALANDO (etapas puladas), nunca por SIGKILL
     do RQ, que apaga o relatório inteiro;
  3. cemitério de falhas cheio vira ERRO — em 4 dias ninguém abriu o registry;
  4. fila sem vaga não é corte mudo: sai o número real de órfãos esperando;
  5. dia que já tem `bfd:` na fila CONTINUA voltando pela recuperação —
     `backfill_dia` faz skip em dia coberto, e 97,4% dos órfãos estão
     "cobertos" justamente porque fecharam `success` batendo o cap de 10k.
"""
import datetime
import time
from unittest.mock import MagicMock, patch

import pytest

from djen import jobs as J


@pytest.fixture
def trib(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJ São Paulo', 'sigla_djen': 'TJSP'})
    return t


def _run(trib, dia, status):
    from tribunals.models import IngestionRun
    return IngestionRun.objects.create(
        tribunal=trib, fonte='djen', status=status,
        janela_inicio=dia, janela_fim=dia)


@pytest.fixture
def fila():
    """Fila RQ falsa com `len()` controlável (vaga = teto - len)."""
    f = MagicMock()
    f.job_ids = []
    f.name = 'djen_backfill'
    f.__len__.return_value = 0
    with patch('django_rq.get_queue', return_value=f), \
         patch('rq.registry.ScheduledJobRegistry') as sched, \
         patch('rq.registry.StartedJobRegistry') as start:
        sched.return_value.get_job_ids.return_value = []
        start.return_value.get_job_ids.return_value = []
        yield f


# ---------------------------------------------------------------- código velho

def test_codigo_velho_vira_erro_com_nome_e_idade():
    """Worker rodando o `djen/` de 14/08 em 21/08 tem que gritar.

    Sem isto o deploy é indistinguível do não-deploy: foi assim que 3.007 dias
    de recuperação e 4.247 jobs do datajud morreram em silêncio.
    """
    # o processo "subiu" 1h antes do arquivo mais novo do projeto → tudo velho
    with patch.object(J, '_inicio_do_processo', return_value=time.time() - 3600), \
         patch.object(J.logger, 'error') as erro:
        r = J._alerta_codigo_velho()

    assert r['modulos_velhos'] > 0, 'não viu que o código do disco é mais novo'
    assert r['atraso_horas'] >= 0
    assert erro.called, 'código velho passou sem ERRO'
    assert 'CÓDIGO VELHO' in erro.call_args.args[0]
    assert any('djen' in nome for nome in r['exemplos']), \
        'não disse QUAIS módulos — "algo está velho" não é acionável'


def test_codigo_em_dia_nao_alarma():
    """Falso positivo aqui gastaria o alarme — o watchdog roda a cada 5min."""
    with patch.object(J, '_inicio_do_processo', return_value=time.time() + 86400), \
         patch.object(J.logger, 'error') as erro:
        r = J._alerta_codigo_velho()

    assert r == {'checado': True, 'modulos_velhos': 0}
    assert not erro.called


def test_sem_proc_o_detector_se_cala_em_vez_de_derrubar():
    """Diagnóstico nunca pode ser o motivo de o watchdog cair."""
    with patch.object(J, '_inicio_do_processo', return_value=None):
        assert J._alerta_codigo_velho() == {'checado': False}


# ------------------------------------------------------------------- orçamento

@pytest.mark.django_db
def test_orcamento_estourado_termina_falando(trib, fila):
    """Morrer por JobTimeoutException apaga o relatório inteiro.

    Em 17/08 o watchdog levou SIGKILL aos 120 s: nada do que ele fez apareceu,
    o teto não virou ERRO e a única marca foi 1 linha no registry.
    """
    with patch.object(J, 'WATCHDOG_ORCAMENTO_S', -1), \
         patch.object(J, '_alerta_codigo_velho', return_value={'checado': False}), \
         patch.object(J, '_alerta_registry_de_falhas', return_value=0), \
         patch.object(J.logger, 'error') as erro:
        r = J.watchdog_ingestao()

    assert r['etapas_puladas'] == ['zumbis', 'reenfileiramento', 'recuperacao']
    assert erro.called, 'estourou o orçamento em silêncio'
    assert r['dias_recuperacao_reenfileirados'] == 0
    assert not fila.enqueue.called


@pytest.mark.django_db
def test_orcamento_folgado_faz_o_ciclo_inteiro(trib, fila):
    dia = datetime.date(2025, 8, 7)
    _run(trib, dia, 'failed')
    with patch.object(J, '_alerta_codigo_velho', return_value={'checado': False}), \
         patch.object(J, '_alerta_registry_de_falhas', return_value=0):
        r = J.watchdog_ingestao()

    assert r['etapas_puladas'] == []
    assert r['dias_recuperacao_reenfileirados'] == 1
    assert r['recuperacao']['orfaos'] == 1


# ------------------------------------------------------- cemitério de falhas

def test_registry_cheio_vira_erro():
    """4.259 falhas em 4 dias e ninguém abriu o registry. O ZCARD é grátis."""
    fila = MagicMock()
    fila.name = 'default'
    registry = MagicMock()
    registry.__len__.return_value = J.WATCHDOG_FALHAS_ALERTA + 1
    with patch('rq.registry.FailedJobRegistry', return_value=registry), \
         patch.object(J.logger, 'error') as erro:
        n = J._alerta_registry_de_falhas(fila)

    assert n == J.WATCHDOG_FALHAS_ALERTA + 1
    assert erro.called
    assert J.WATCHDOG_FALHAS_ALERTA + 1 in erro.call_args.args, \
        'gritou sem dizer quantas falhas havia'


def test_registry_dentro_do_normal_nao_alarma():
    fila = MagicMock()
    fila.name = 'default'
    registry = MagicMock()
    registry.__len__.return_value = 3
    with patch('rq.registry.FailedJobRegistry', return_value=registry), \
         patch.object(J.logger, 'error') as erro:
        assert J._alerta_registry_de_falhas(fila) == 3
    assert not erro.called


# ----------------------------------------------------------- teto e não-mudez

@pytest.mark.django_db
def test_fila_sem_vaga_nao_e_corte_mudo(trib, fila):
    """Teto é alerta, nunca `return` discreto (regra nº 2 do CLAUDE.md)."""
    for i in range(5):
        _run(trib, datetime.date(2025, 1, 1) + datetime.timedelta(days=i), 'failed')
    fila.__len__.return_value = J.BACKFILL_WATERMARK      # fila no teto global

    resumo = {}
    with patch.object(J.logger, 'error') as erro:
        n = J.ressuscitar_dias_de_recuperacao(resumo=resumo)

    assert n == 0
    assert not fila.enqueue.called
    assert resumo == {'orfaos': 5, 'devolvidos': 0, 'sobraram': 5, 'vagas_na_fila': 0}
    assert erro.called, 'fila cheia engoliu 5 dias sem dizer nada'
    assert 5 in erro.call_args.args, 'não disse quantos ficaram esperando'


@pytest.mark.django_db
def test_resumo_conta_os_dois_lados(trib, fila):
    """Contagem própria não prova nada: quero devolvidos E sobraram."""
    for i in range(J.RECUP_POR_TIQUE + 7):
        _run(trib, datetime.date(2025, 1, 1) + datetime.timedelta(days=i), 'failed')

    resumo = {}
    n = J.ressuscitar_dias_de_recuperacao(resumo=resumo)

    assert n == J.RECUP_POR_TIQUE
    assert resumo['orfaos'] == J.RECUP_POR_TIQUE + 7
    assert resumo['devolvidos'] == J.RECUP_POR_TIQUE
    assert resumo['sobraram'] == 7


@pytest.mark.django_db
def test_dia_com_bfd_na_fila_continua_voltando(trib, fila):
    """`bfd:` NÃO ocupa o dia — e essa distinção vale 97,4% da recuperação.

    Medido em 21/08/2026: 2.929 dos 3.007 órfãos já têm um `success` cobrindo
    o dia, porque a recuperação nacional existe pra refazer exatamente o dia
    que fechou `success` batendo o cap de 10k. `backfill_dia` faz skip em dia
    coberto — se o `bfd:` bloqueasse o `f2:`, o dia capado nunca seria refeito.
    """
    dia = datetime.date(2025, 8, 7)
    _run(trib, dia, 'failed')
    fila.job_ids = ['bfd:TJSP:2025-08-07']

    assert J.ressuscitar_dias_de_recuperacao() == 1
    assert fila.enqueue.call_args.kwargs['job_id'] == 'f2:TJSP:2025-08-07'


# ------------------------------------------------------------- teto de SQL

@pytest.mark.django_db
def test_leitura_com_teto_aplica_statement_timeout():
    """pgbouncer em transaction-mode descarta `SET` solto — tem que ser LOCAL."""
    from django.db import connection
    executadas = []
    real = connection.cursor

    class Espiao:
        def __init__(self, cur): self._cur = cur
        def __enter__(self): return self
        def __exit__(self, *a): return self._cur.__exit__(*a)
        def execute(self, sql, params=None):
            executadas.append((sql, params))
            return self._cur.__enter__().execute(sql, params)

    with patch.object(connection, 'cursor', lambda: Espiao(real())):
        J._leitura_com_teto(lambda: 42, segundos=7)

    tetos = [(sql, p) for sql, p in executadas if 'statement_timeout' in sql]
    assert tetos, f'não aplicou timeout nenhum: {executadas}'
    sql, params = tetos[0]
    assert 'SET LOCAL' in sql, 'SET solto morre no pgbouncer transaction-mode'
    assert params == [7000]


@pytest.mark.django_db
def test_tick_com_banco_travado_devolve_o_worker(trib):
    """120 s de worker preso vezes 59 tribunais foi o que derrubou o watchdog."""
    from django.db.utils import OperationalError

    from tribunals.models import Tribunal
    Tribunal.objects.filter(pk=trib.pk).update(
        ativo=True, data_inicio_disponivel=datetime.date(2025, 1, 1))

    def explode(*a, **k):
        raise OperationalError('canceling statement due to statement timeout')

    with patch.object(J, '_leitura_com_teto', side_effect=explode), \
         patch.object(J.logger, 'error') as erro, \
         patch('djen.client.circuit_is_open', return_value=False):
        r = J.tick_backfill_retroativo('TJSP')

    assert r['skip'] == 'banco em contenção'
    assert erro.called, 'banco travado passou sem ERRO'
