"""Um (tribunal, dia) só pode estar sendo coletado por UM worker de cada vez.

CONTEXTO MEDIDO (27/08/2026, produção). O dia 2026-08-27 do TJSP fechou
íntegro — 262.341 publicações, batendo com a fonte — mas foi coletado por
**12 `IngestionRun` concorrentes**, somando **7.437 páginas** contra as **264**
que o dia exige. Vinte e oito vezes o trabalho, para trazer exatamente as
mesmas publicações. Na frota inteira:

    janela 2026-08-26 ... 10.402 páginas lidas · ~4.670 necessárias · 2,2x
    janela 2026-08-27 ... 14.760 páginas lidas · ~5.506 necessárias · 2,7x

Isso não perde acervo — gasta a banda contra a API do CNJ que o
circuit-breaker existe para proteger. E é a banda estourada que ABRE o
circuito e adia os dias dos outros tribunais (`réplicas × páginas ≤ 64`).

As duas causas, as duas nesta mudança:

1. `_dia_coberto` só enxerga dia que já FECHOU. Enquanto o dia está sendo
   coletado, nada impedia uma segunda coleta — e o fan-out do
   `run_daily_ingestion` usa job_id ALEATÓRIO de propósito, então o RQ também
   não deduplica. Agora há trava `SET NX EX` por (tribunal, dia).

2. `tick_backfill_retroativo` conferia só a LISTA da fila, e job entregue a um
   worker sai da lista. A cada 10 minutos ele re-enfileirava o dia que estava
   em voo. Agora soma o `StartedJobRegistry` — a mesma lição de
   "fila não é backlog" que já custou 77% de duplicação no enriquecimento.

3. E o teto de tempo: `job_timeout=3600` no tique, contra um dia de TJSP que
   leva 136 min. O work-horse era morto por `JobTimeoutException` no meio da
   coleta, TODA vez (5 runs assim em 27/08), e o dia voltava 10 min depois.
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

from djen import jobs as J


@pytest.fixture
def trib(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJ São Paulo', 'sigla_djen': 'TJSP'})
    return t


@pytest.fixture(autouse=True)
def _cache_limpo():
    from django.core.cache import cache
    cache.delete(J._chave_coleta('TJSP', '2026-08-27'))
    yield
    cache.delete(J._chave_coleta('TJSP', '2026-08-27'))


DIA = '2026-08-27'


def test_segunda_coleta_do_mesmo_dia_nao_abre(trib):
    """Com o dia em voo, a segunda entrada devolve `ja_em_coleta` e NÃO chama a API."""
    with patch.object(J, '_dia_coberto', return_value=False), \
         patch.object(J, 'ingest_window') as ing:
        ing.return_value = MagicMock(pk=1, movimentacoes_novas=10)

        # 1ª execução: pega a trava e coleta.
        r1 = J.backfill_dia('TJSP', DIA)
        assert r1['run_id'] == 1
        assert ing.call_count == 1

        # a trava é solta no fim; simulamos "em voo" tomando-a por fora.
        from django.core.cache import cache
        assert cache.add(J._chave_coleta('TJSP', DIA), 1, 60) is True

        r2 = J.backfill_dia('TJSP', DIA)

    assert r2 == {'skip': 'ja_em_coleta', 'dia': DIA, 'tribunal': 'TJSP'}
    assert ing.call_count == 1, 'a segunda coleta chamou a API do CNJ de novo'


def test_trava_e_solta_no_fim_para_o_dia_poder_ser_refeito(trib):
    """Trava presa = dia que nunca mais é coletado. Ela tem que sair no `finally`."""
    from django.core.cache import cache
    with patch.object(J, '_dia_coberto', return_value=False), \
         patch.object(J, 'ingest_window') as ing:
        ing.return_value = MagicMock(pk=1, movimentacoes_novas=10)
        J.backfill_dia('TJSP', DIA)
    assert cache.get(J._chave_coleta('TJSP', DIA)) is None


def test_trava_sai_tambem_quando_o_circuito_abre(trib):
    """`DjenBusyError` é ADIAR. Se a trava ficasse, o dia adiado não voltaria."""
    from django.core.cache import cache

    from djen.client import DjenBusyError
    with patch.object(J, '_dia_coberto', return_value=False), \
         patch.object(J, 'ingest_window', side_effect=DjenBusyError('ocupado')):
        r = J.backfill_dia('TJSP', DIA)
    assert r['skip'] == 'circuito_aberto'
    assert cache.get(J._chave_coleta('TJSP', DIA)) is None


def test_redis_fora_falha_aberto_e_coleta_mesmo_assim(trib):
    """Perder a trava custa duplicação; perder o dia custa acervo. Fail-open."""
    with patch.object(J, '_dia_coberto', return_value=False), \
         patch.object(J, 'ingest_window') as ing, \
         patch('djen.jobs.cache.add', side_effect=RuntimeError('redis fora')):
        ing.return_value = MagicMock(pk=7, movimentacoes_novas=3)
        r = J.backfill_dia('TJSP', DIA)
    assert r['run_id'] == 7
    assert ing.call_count == 1


def test_tique_nao_reenfileira_dia_que_esta_em_execucao(trib, monkeypatch):
    """Fila não é backlog: o job em voo saiu da lista, mas o dia NÃO está livre."""
    hoje = datetime.date.today()
    trib.data_inicio_disponivel = hoje - datetime.timedelta(days=2)
    trib.overlap_dias = 1
    trib.ativo = True
    trib.save()

    em_voo = f'{J.BACKFILL_JOB_PREFIX}:TJSP:{hoje}'
    fila = MagicMock()
    fila.get_job_ids.return_value = []                       # já foi entregue
    fila.started_job_registry.get_job_ids.return_value = [em_voo]

    monkeypatch.setattr('django_rq.get_queue', lambda *a, **k: fila)
    with patch.object(J, 'circuit_is_open', return_value=False, create=True), \
         patch('djen.client.circuit_is_open', return_value=False), \
         patch.object(J, '_dias_cobertos', return_value=set()):
        J.tick_backfill_retroativo('TJSP')

    enfileirados = [c.kwargs.get('job_id') for c in fila.enqueue.call_args_list]
    # o tique TEM que continuar trabalhando: o que não pode é repetir o dia em voo
    assert enfileirados, 'o tique parou de enfileirar — o filtro comeu tudo'
    assert em_voo not in enfileirados, 'o tique duplicou o dia que já estava em voo'


def test_tique_usa_o_teto_de_tempo_que_cabe_o_maior_dia(trib, monkeypatch):
    """3.600 s mata o TJSP no meio (136 min medidos). O tique tem que usar o mesmo
    teto do fan-out do daily."""
    hoje = datetime.date.today()
    trib.data_inicio_disponivel = hoje - datetime.timedelta(days=2)
    trib.overlap_dias = 1
    trib.ativo = True
    trib.save()

    fila = MagicMock()
    fila.get_job_ids.return_value = []
    fila.started_job_registry.get_job_ids.return_value = []
    monkeypatch.setattr('django_rq.get_queue', lambda *a, **k: fila)
    with patch('djen.client.circuit_is_open', return_value=False), \
         patch.object(J, '_dias_cobertos', return_value=set()):
        J.tick_backfill_retroativo('TJSP')

    assert fila.enqueue.call_args_list, 'o tique não enfileirou nada'
    for c in fila.enqueue.call_args_list:
        assert c.kwargs['job_timeout'] == J.BACKFILL_DIA_TIMEOUT_S
        assert c.kwargs['job_timeout'] >= 8 * 3600, (
            'teto menor que o maior dia medido (TJSP, 136 min) volta a matar '
            'o work-horse no meio da coleta')
