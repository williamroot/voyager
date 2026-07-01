"""Regressão do reabastecer_filas_enriquecimento (fix 2026-07-01).

Bug: re-enfileirava os mesmos PENDENTE a cada ciclo (status só vira OK no drainer
async) + rodava concorrente → enrich_tjmt chegou a 387k jobs p/ high-water 100k,
saturando o DB. Fix: lock + cursor por pk. Estes testes travam o comportamento.
"""
from unittest.mock import MagicMock, patch

from enrichers import jobs


class _FakeConn:
    """Redis fake: só get/set de cursor (bytes)."""
    def __init__(self):
        self.kv = {}
    def get(self, k):
        return self.kv.get(k)
    def set(self, k, v):
        self.kv[k] = str(v).encode()


def _fake_pending(pks):
    """Simula Process.objects.filter(...).order_by('pk').values_list('pk')[:n]
    respeitando pk__gt=cursor."""
    qs = MagicMock()
    def _filter(**kw):
        cursor = kw.get('pk__gt', -1)
        restantes = [p for p in pks if p > cursor]
        inner = MagicMock()
        sliced = MagicMock()
        sliced.__iter__ = lambda self: iter(restantes)  # sem limite real (teste pequeno)
        ob = MagicMock()
        ob.values_list.return_value.__getitem__ = lambda self, s: restantes[s]
        inner.order_by.return_value = ob
        return inner
    qs.filter.side_effect = _filter
    return qs


def test_nao_reenfileira_in_flight():
    """2 passadas sobre o mesmo backlog não duplicam: cada pk é enfileirado 1x."""
    conn = _FakeConn()
    q = MagicMock(); q.__len__ = lambda self: 0
    with patch.object(jobs, '_ENRICHERS', {'TJX': object}), \
         patch('enrichers.jobs.django_rq.get_connection', return_value=conn), \
         patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
         patch('enrichers.jobs.queue_for', return_value='enrich_tjx'), \
         patch('enrichers.jobs.Process', create=True):
        from tribunals.models import Process
        with patch.object(Process, 'objects', _fake_pending([10, 20, 30])):
            jobs._reabastecer_impl()   # passada 1: enfileira 10,20,30 (cursor->30)
            jobs._reabastecer_impl()   # passada 2: nada > 30 -> wrap (cursor->0)
    enfileirados = [c.args[1] for c in q.enqueue.call_args_list]
    assert enfileirados == [10, 20, 30], enfileirados  # sem repetição


def test_lock_impede_concorrencia():
    """Com o lock tomado, o run é pulado (não roda a impl)."""
    with patch('django.core.cache.cache') as cache, \
         patch('enrichers.jobs._reabastecer_impl') as impl:
        cache.add.return_value = False  # lock já tomado
        out = jobs.reabastecer_filas_enriquecimento()
        assert out == {'skip': 'lock held'}
        impl.assert_not_called()
