"""Sync incremental PG→ES: watermarks, tetos, kill-switch e o sinal do processo novo.

Sem ES real e sem fila real — a indexação é mockada (mesmo padrão de
tests/test_es_schema_partes.py). O que importa aqui é a MECÂNICA: ancorar no
primeiro tick, avançar por keyset, respeitar teto, não perder escrita e computar
`tem_sinal_precatorio` ANTES de indexar.
"""
from unittest.mock import patch

import pytest
from django.core.cache import cache

from search import sync_incremental as sync


@pytest.fixture(autouse=True)
def _limpa_cache():
    cache.clear()
    yield
    cache.clear()


def test_kill_switch_desliga_o_tick():
    cache.set('sync_es:off', True)
    assert sync.tick_sync_es_incremental() == {'off': True}


@patch('search.sync_incremental.Process')
def test_primeiro_tick_ancora_sem_reprocessar_a_base(mock_proc):
    # 1º tick NÃO pode enfileirar a base inteira — só ancora o watermark no topo.
    (mock_proc.objects.order_by.return_value.values_list.return_value
     .first.return_value) = 12345
    out = sync.sync_processos_novos()
    assert out['ancorou'] is True
    assert out['novos'] == 0
    assert cache.get('sync_es:wm:proc_id') == 12345


@patch('search.sync_incremental._enfileirar_processos', return_value=3)
@patch('search.sync_incremental.computar_sinal', return_value=3)
@patch('search.sync_incremental.Process')
def test_processos_novos_computam_sinal_antes_de_indexar(mock_proc, mock_sinal, mock_enf):
    cache.set('sync_es:wm:proc_id', 100, None)
    qs = mock_proc.objects.filter.return_value.order_by.return_value.values_list.return_value
    qs.__getitem__.return_value = [101, 102, 103]

    out = sync.sync_processos_novos()

    # o sinal é computado pros MESMOS pks que vão ser indexados...
    mock_sinal.assert_called_once_with([101, 102, 103])
    mock_enf.assert_called_once_with([101, 102, 103])
    # ...e o watermark avança pro último id visto (keyset)
    assert cache.get('sync_es:wm:proc_id') == 103
    assert out['novos'] == 3 and out['sinal'] == 3


@patch('search.sync_incremental._enfileirar_processos', return_value=0)
@patch('search.sync_incremental.computar_sinal')
@patch('search.sync_incremental.Process')
def test_sem_processo_novo_nao_mexe_no_watermark(mock_proc, mock_sinal, mock_enf):
    cache.set('sync_es:wm:proc_id', 500, None)
    qs = mock_proc.objects.filter.return_value.order_by.return_value.values_list.return_value
    qs.__getitem__.return_value = []

    out = sync.sync_processos_novos()

    mock_sinal.assert_not_called()
    assert out['novos'] == 0
    assert cache.get('sync_es:wm:proc_id') == 500


@patch('search.sync_incremental._enfileirar_movs', return_value=2)
@patch('search.sync_incremental.Movimentacao')
def test_movs_novas_avancam_keyset(mock_mov, mock_enf):
    cache.set('sync_es:wm:mov_id', 9, None)
    qs = mock_mov.objects.filter.return_value.order_by.return_value.values_list.return_value
    qs.__getitem__.return_value = [10, 11]

    out = sync.sync_movimentacoes_novas()

    mock_enf.assert_called_once_with([10, 11])
    assert cache.get('sync_es:wm:mov_id') == 11
    assert out['movs'] == 2


@patch('search.sync_incremental._enfileirar_processos', return_value=sync.LIMITE_PROC_ATUALIZADOS)
@patch('search.sync_incremental.Process')
def test_atualizados_no_teto_nao_pulam_o_resto(mock_proc, mock_enf):
    """Bateu o teto ⇒ o watermark NÃO salta pra 'agora' (senão perde o resto)."""
    from django.utils import timezone
    t0 = timezone.now() - timezone.timedelta(minutes=30)
    cache.set('sync_es:wm:proc_ts', t0, None)
    pks = list(range(sync.LIMITE_PROC_ATUALIZADOS))
    (mock_proc.objects.filter.return_value.order_by.return_value
     .values_list.return_value.__getitem__.return_value) = pks
    meio = t0 + timezone.timedelta(minutes=5)
    (mock_proc.objects.filter.return_value.values_list.return_value
     .first.return_value) = meio

    sync.sync_processos_atualizados()

    # avançou só até o último registro lido — o próximo tick continua daí
    assert cache.get('sync_es:wm:proc_ts') == meio


@patch('search.sync_incremental.sync_movimentacoes_novas', return_value={'movs': 0})
@patch('search.sync_incremental.sync_processos_atualizados', side_effect=RuntimeError('boom'))
@patch('search.sync_incremental.sync_processos_novos', return_value={'novos': 1})
def test_bloco_que_falha_nao_derruba_o_tick(mock_novos, mock_atu, mock_movs):
    out = sync.tick_sync_es_incremental()
    assert out['proc_novos'] == {'novos': 1}
    assert out['proc_atualizados'] == {'erro': True}   # degradou, não propagou
    assert out['movs_novas'] == {'movs': 0}
