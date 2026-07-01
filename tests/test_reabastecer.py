"""Regressão do reabastecer_filas_enriquecimento (fix 2026-07-01).

Bug do incidente: rodava concorrente (scan lento sobrepunha runs do scheduler) e
cada run passava o teste `len(queue) < high_water` → enrich_tjmt 387k jobs p/ teto
100k, saturando o DB. Fix: lock Redis (um run por vez) + teto QUEUE_HIGH_WATER.
"""
from unittest.mock import MagicMock, patch

from enrichers import jobs


def _queue(n):
    q = MagicMock()
    q.__len__ = lambda self: n
    return q


def test_lock_impede_concorrencia():
    """Com o lock tomado, o run é pulado (não roda a impl)."""
    with patch('django.core.cache.cache') as cache, \
         patch('enrichers.jobs._reabastecer_impl') as impl:
        cache.add.return_value = False  # lock já tomado
        assert jobs.reabastecer_filas_enriquecimento() == {'skip': 'lock held'}
        impl.assert_not_called()


def test_lock_liberado_no_fim():
    """Sem lock tomado, roda e libera o lock (delete) no finally."""
    with patch('django.core.cache.cache') as cache, \
         patch('enrichers.jobs._reabastecer_impl', return_value={'ok': 1}) as impl:
        cache.add.return_value = True
        assert jobs.reabastecer_filas_enriquecimento() == {'ok': 1}
        impl.assert_called_once()
        cache.delete.assert_called_once_with('lock:reabastecer_enriquecimento')


def test_respeita_high_water():
    """Fila já no teto → skip, sem enfileirar."""
    full = _queue(jobs.QUEUE_HIGH_WATER)
    with patch.object(jobs, '_ENRICHERS', {'TJX': object}), \
         patch('enrichers.jobs.django_rq.get_queue', return_value=full), \
         patch('enrichers.jobs.queue_for', return_value='enrich_tjx'):
        rel = jobs._reabastecer_impl()
    full.enqueue.assert_not_called()
    assert 'skip' in rel['TJX']


def test_enfileira_ate_capacidade():
    """Fila abaixo do teto → enfileira os pendentes retornados pelo query."""
    q = _queue(0)
    pend = [1, 2, 3, 4, 5]
    qs = MagicMock()
    qs.filter.return_value.values_list.return_value.__getitem__.side_effect = lambda s: pend
    with patch.object(jobs, '_ENRICHERS', {'TJX': object}), \
         patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
         patch('enrichers.jobs.queue_for', return_value='enrich_tjx'), \
         patch('tribunals.models.Process.objects', qs):
        rel = jobs._reabastecer_impl()
    assert [c.args[1] for c in q.enqueue.call_args_list] == pend
    assert '+5' in rel['TJX']
