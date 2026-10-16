"""Ordem do enriquecimento no TJRS: PRECATORIO → PRE_PRECATORIO → resto, e o
respiro quando a fonte recusa toda credencial (16/10/2026)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from enrichers import jobs


def _fila():
    q = MagicMock()
    q.connection.zcard.return_value = 0
    q.connection.zrange.return_value = []
    q.__len__.return_value = 0
    q.name = 'enrich_tjrs'
    return q


def _pendentes(por_nivel):
    return lambda sigla, nivel, limite: [(pk, f'5{pk:06d}-00.2026.8.21.0001')
                                         for pk in por_nivel.get(nivel, [])][:limite]


def test_precatorio_sai_primeiro_e_pre_depois():
    q = _fila()
    with patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
            patch('enrichers.jobs.pendentes_por_nivel',
                  _pendentes({'PRECATORIO': [1, 2], 'PRE_PRECATORIO': [3]})), \
            patch('enrichers.jobs._filtrar_em_voo', lambda c, s, ids, n: ids[:n]):
        contagem = jobs.enfileirar_na_frente('TJRS')
    assert contagem == {'PRECATORIO': 2, 'PRE_PRECATORIO': 1}
    chamadas = q.enqueue.call_args_list
    assert all(c.kwargs['at_front'] for c in chamadas)
    assert all(c.kwargs['kwargs'] == {'so_pendente': True} for c in chamadas)
    # at_front empilha na cabeça: o último a entrar é o primeiro a sair.
    ordem_de_saida = [c.kwargs['args'][0] for c in reversed(chamadas)]
    assert ordem_de_saida == [1, 2, 3]


def test_lote_tem_teto_e_precatorio_consome_primeiro():
    q = _fila()
    with patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
            patch('enrichers.jobs.pendentes_por_nivel',
                  _pendentes({'PRECATORIO': list(range(1, 11)), 'PRE_PRECATORIO': [99]})), \
            patch('enrichers.jobs._filtrar_em_voo', lambda c, s, ids, n: ids[:n]):
        contagem = jobs.enfileirar_na_frente('TJRS', lote=4)
    assert contagem == {'PRECATORIO': 4}
    assert q.enqueue.call_count == 4


def test_prefixo_8_nao_entra_na_frente():
    q = _fila()
    linhas = {'PRECATORIO': [1]}
    with patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
            patch('enrichers.jobs.pendentes_por_nivel',
                  lambda s, n, lim: [(7, '8000060-71.2026.8.21.0030'), *_pendentes(linhas)(s, n, lim)]), \
            patch('enrichers.jobs._filtrar_em_voo', lambda c, s, ids, n: ids[:n]), \
            patch('enrichers.jobs.Process.objects') as objs, \
            patch('enrichers.jobs.registrar_fora_do_esaj'):
        objs.filter.return_value.update.return_value = 1
        jobs.enfileirar_na_frente('TJRS', niveis=('PRECATORIO',))
    assert [c.kwargs['args'][0] for c in q.enqueue.call_args_list] == [1]


def test_refill_fura_a_fila_mesmo_com_fila_cheia():
    """O PRECATORIO novo não espera o backlog: a frente vem ANTES do teste de
    profundidade."""
    q = _fila()
    q.__len__.return_value = 10**6
    with patch('enrichers.jobs._ENRICHERS', {'TJRS': object}), \
            patch('enrichers.jobs.enrich_pausados', return_value=set()), \
            patch('enrichers.jobs.em_respiro', return_value=False), \
            patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
            patch('enrichers.jobs._profundidade_alvo', return_value=2000), \
            patch('enrichers.jobs._alertar_fora_da_fonte'), \
            patch('enrichers.jobs.enfileirar_na_frente',
                  return_value={'PRECATORIO': 3}) as frente:
        rel = jobs._reabastecer_impl()
    frente.assert_called_once_with('TJRS')
    assert rel['TJRS'].startswith('frente PRECATORIO +3; skip')


def test_refill_nao_mexe_em_tribunal_sem_prioridade():
    q = _fila()
    q.__len__.return_value = 10**6
    with patch('enrichers.jobs._ENRICHERS', {'TJMT': object}), \
            patch('enrichers.jobs.enrich_pausados', return_value=set()), \
            patch('enrichers.jobs.em_respiro', return_value=False), \
            patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
            patch('enrichers.jobs._profundidade_alvo', return_value=2000), \
            patch('enrichers.jobs._alertar_fora_da_fonte'), \
            patch('enrichers.jobs.enfileirar_na_frente') as frente:
        jobs._reabastecer_impl()
    frente.assert_not_called()


def test_refill_pula_tribunal_em_respiro():
    with patch('enrichers.jobs._ENRICHERS', {'TJRS': object}), \
            patch('enrichers.jobs.enrich_pausados', return_value=set()), \
            patch('enrichers.jobs.em_respiro', return_value=True), \
            patch('enrichers.jobs._alertar_fora_da_fonte'), \
            patch('enrichers.jobs.enfileirar_na_frente') as frente:
        rel = jobs._reabastecer_impl()
    assert rel['TJRS'] == 'respiro'
    frente.assert_not_called()


@pytest.fixture
def cache_limpo(settings):
    # Cache próprio do teste: outro teste da suíte troca o backend por um que
    # não guarda nada, e o respiro é estado em cache.
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                   'LOCATION': 'respiro-tjrs'}}
    from django.core.cache import cache
    for k in ('enrich:respiro:TJRS', 'enrich:respiro_n:TJRS'):
        cache.delete(k)
    yield cache
    for k in ('enrich:respiro:TJRS', 'enrich:respiro_n:TJRS'):
        cache.delete(k)


def test_respiro_dobra_e_tem_teto(cache_limpo):
    prazos = []
    with patch('enrichers.alertas.alertar'):
        for _ in range(5):
            prazos.append(jobs.armar_respiro('TJRS', '401 em série'))
            assert jobs.armar_respiro('TJRS', 'outra réplica') == 0   # já armado
            cache_limpo.delete('enrich:respiro:TJRS')                  # o prazo venceu
        jobs.armar_respiro('TJRS', '401 em série')
    assert prazos == [600, 1200, 2400, 3600, 3600]
    assert jobs.em_respiro('TJRS') and jobs.enrich_pausado('TJRS')
    jobs.desarmar_respiro('TJRS')
    assert cache_limpo.get('enrich:respiro_n:TJRS') is None


def test_job_em_respiro_nao_toca_no_processo(cache_limpo):
    jobs.armar_respiro('TJRS', 'teste')
    proc = MagicMock(tribunal_id='TJRS')
    with patch('enrichers.jobs.Process.objects') as objs:
        objs.select_related.return_value.get.return_value = proc
        r = jobs.enriquecer_processo(1)
    assert r == {'skip': 'pausado', 'tribunal': 'TJRS'}


# ───────────── revisão do PR #27: frente sem multiplicar, respiro sem corrida ─────────────

@pytest.fixture
def redis_real():
    """A fila e as travas no Redis de teste de verdade — sem mockar
    `_filtrar_em_voo`, que é justamente o que precisa ser provado."""
    import django_rq
    q = django_rq.get_queue('enrich_tjrs')
    conn = q.connection
    for k in ('enr:frente:tjrs', 'enr:inflight:tjrs'):
        conn.delete(k)
    q.empty()
    yield q
    for k in ('enr:frente:tjrs', 'enr:inflight:tjrs'):
        conn.delete(k)
    q.empty()


def test_worker_parado_n_passadas_nao_multiplicam_jobs(redis_real):
    """37 mil PRECATORIO+PRE pendentes, worker parado, 30 passadas do refill e
    a trava de voo expirando no meio: a fila da frente fica no teto, e nenhum
    pid entra duas vezes."""
    q = redis_real
    pendentes = {'PRECATORIO': list(range(1, 30_001)), 'PRE_PRECATORIO': list(range(30_001, 37_643))}
    with patch('enrichers.jobs.pendentes_por_nivel', _pendentes(pendentes)):
        for passada in range(30):
            jobs.enfileirar_na_frente('TJRS')
            if passada == 15:
                q.connection.delete('enr:inflight:tjrs')     # 1 h depois: a trava de voo expirou
    pids = [j.args[0] for j in q.get_jobs()]
    assert all(j.kwargs == {'so_pendente': True} for j in q.get_jobs())
    assert len(pids) == jobs.ENRICH_FRENTE_MAX
    assert len(set(pids)) == len(pids)


def test_job_terminado_libera_vaga_na_frente(redis_real):
    q = redis_real
    with patch('enrichers.jobs.pendentes_por_nivel', _pendentes({'PRECATORIO': [1, 2, 3]})):
        jobs.enfileirar_na_frente('TJRS', lote=3)
        assert q.connection.zcard('enr:frente:tjrs') == 3
        jobs.sair_da_frente('TJRS', 2)
        assert q.connection.zcard('enr:frente:tjrs') == 2


def test_job_pula_processo_que_ja_nao_esta_pendente():
    proc = MagicMock(tribunal_id='TJRS', enriquecimento_status='ok')
    with patch('enrichers.jobs.Process.objects') as objs, \
            patch('enrichers.jobs.enrich_pausado', return_value=False), \
            patch('enrichers.jobs.sair_da_frente') as saiu, \
            patch('enrichers.jobs._ENRICHERS', {'TJRS': MagicMock()}) as reg:
        objs.select_related.return_value.get.return_value = proc
        r = jobs.enriquecer_processo(7, so_pendente=True)
    assert r['skip'] == 'nao_pendente'
    reg['TJRS'].assert_not_called()
    saiu.assert_called_once_with('TJRS', 7)


def test_clique_manual_reenriquece_mesmo_sem_pendente():
    proc = MagicMock(tribunal_id='TJRS', enriquecimento_status='ok', numero_cnj='5')
    class Cls:
        def __init__(self, prefer_cortex=None):
            pass

        def enriquecer(self, p, direct_apply=False):
            return {'status': 'ok'}
    cls = Cls
    with patch('enrichers.jobs.Process.objects') as objs, \
            patch('enrichers.jobs.enrich_pausado', return_value=False), \
            patch('enrichers.jobs._hook_fora_da_fonte', return_value=None), \
            patch('enrichers.jobs._ENRICHERS', {'TJRS': cls}):
        objs.select_related.return_value.get.return_value = proc
        assert jobs.enriquecer_processo(7)['status'] == 'ok'


def test_respiro_com_varias_replicas_ao_mesmo_tempo_arma_uma_vez(cache_limpo):
    import threading
    prazos = []
    with patch('enrichers.alertas.alertar') as alerta:
        ts = [threading.Thread(target=lambda: prazos.append(jobs.armar_respiro('TJRS', '401')))
              for _ in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    assert sorted(prazos) == [0] * 11 + [600]
    assert cache_limpo.get('enrich:respiro_n:TJRS') == 1
    alerta.assert_called_once()


def test_respiro_alerta_com_o_motivo(cache_limpo):
    with patch('enrichers.alertas.alertar') as alerta:
        jobs.armar_respiro('TJRS', 'API recusou 3 credenciais')
    titulo, detalhe = alerta.call_args[0]
    assert 'TJRS' in titulo and '10 min' in titulo
    assert detalhe == 'API recusou 3 credenciais'


def test_refill_comum_enfileira_de_verdade_no_rq():
    """Regressão: `enqueue(f, pid, kwargs=…)` é recusado pelo RQ ("Extra
    positional arguments cannot be used when using explicit args and kwargs")
    e o `except` do refill só logava WARNING — a fila não recebia nada. Aqui
    com o RQ real, sem mock da fila."""
    import django_rq
    q = django_rq.get_queue('enrich_tjmt')
    q.empty()
    q.connection.delete('enr:inflight:tjmt')
    linhas = [(i, f'1{i:06d}-00.2026.8.11.0001') for i in range(1, 6)]
    with patch('enrichers.jobs._ENRICHERS', {'TJMT': type('E', (), {})}), \
            patch('enrichers.jobs.enrich_pausados', return_value=set()), \
            patch('enrichers.jobs.em_respiro', return_value=False), \
            patch('enrichers.jobs._tribunais_com_prioridade', return_value=set()), \
            patch('enrichers.jobs._alertar_fora_da_fonte'), \
            patch('enrichers.jobs.Process.objects') as objs:
        objs.filter.return_value.values_list.return_value.__getitem__.return_value = linhas
        jobs._reabastecer_impl()
    try:
        assert sorted(j.args[0] for j in q.get_jobs()) == [1, 2, 3, 4, 5]
        assert all(j.kwargs == {'so_pendente': True} for j in q.get_jobs())
    finally:
        q.empty()
        q.connection.delete('enr:inflight:tjmt')
