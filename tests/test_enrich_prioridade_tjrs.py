"""Ordem do enriquecimento no TJRS: PRECATORIO → PRE_PRECATORIO → resto, e o
respiro quando a fonte recusa toda credencial (16/10/2026)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from enrichers import jobs


def _fila():
    q = MagicMock()
    q.connection.zcard.return_value = 0
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
    # at_front empilha na cabeça: o último a entrar é o primeiro a sair.
    ordem_de_saida = [c.args[1] for c in reversed(chamadas)]
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
    assert [c.args[1] for c in q.enqueue.call_args_list] == [1]


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
    prazos = [jobs.armar_respiro('TJRS', '401 em série') for _ in range(5)]
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
