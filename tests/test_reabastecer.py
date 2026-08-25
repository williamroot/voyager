"""Regressão do reabastecer_filas_enriquecimento.

Dois incidentes moram aqui:

1. **2026-07-01** — rodava concorrente (o scan lento sobrepunha runs do
   scheduler) e cada run passava o teste `len(queue) < high_water` →
   `enrich_tjmt` chegou a 387k jobs pra teto de 100k, saturando o DB.
   Fix: lock Redis (um run por vez) + teto de profundidade.

2. **2026-08-25** — o teto de 100k, com uma frota de 146 workers, deixava a
   fila com 20 a 74 HORAS de espera. Como o `Process` só sai de `pendente`
   quando o drainer aplica, o refill re-selecionava a MESMA cabeça do índice a
   cada 2 min: censo completo das 16 filas mediu 1.400.007 jobs para 321.130
   processos distintos nas 14 filas administradas pelo refill — **77,1% de
   cópia** (`enrich_tjmt` com 70,4 cópias por processo, uma delas 1.926×).
   Fix: profundidade-alvo medida pela vazão real + ZSET de processos em voo.
"""
from unittest.mock import MagicMock, patch

from enrichers import jobs


def _queue(n, nome='enrich_tjx'):
    q = MagicMock()
    q.__len__ = lambda self: n
    q.name = nome
    q.connection = MagicMock()
    q.connection.zcard.return_value = 0
    return q


def _process_objects(pendentes):
    qs = MagicMock()
    (qs.filter.return_value.values_list.return_value
       .__getitem__.side_effect) = lambda s: pendentes
    return qs


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


def test_respeita_profundidade_alvo():
    """Fila já no alvo medido → skip, sem enfileirar."""
    full = _queue(5_000)
    with patch.object(jobs, '_ENRICHERS', {'TJX': object}), \
         patch('enrichers.jobs.django_rq.get_queue', return_value=full), \
         patch('enrichers.jobs.queue_for', return_value='enrich_tjx'), \
         patch('enrichers.jobs._profundidade_alvo', return_value=5_000):
        rel = jobs._reabastecer_impl()
    full.enqueue.assert_not_called()
    assert 'skip' in rel['TJX']


def test_enfileira_ate_capacidade():
    """Fila abaixo do alvo → enfileira os pendentes retornados pelo query."""
    q = _queue(0)
    pend = [1, 2, 3, 4, 5]
    with patch.object(jobs, '_ENRICHERS', {'TJX': object}), \
         patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
         patch('enrichers.jobs.queue_for', return_value='enrich_tjx'), \
         patch('enrichers.jobs._profundidade_alvo', return_value=2_000), \
         patch('enrichers.jobs._filtrar_em_voo', side_effect=lambda c, s, ids, n: ids[:n]), \
         patch('tribunals.models.Process.objects', _process_objects(pend)):
        rel = jobs._reabastecer_impl()
    assert [c.args[1] for c in q.enqueue.call_args_list] == pend
    assert '+5' in rel['TJX']


def test_nao_reenfileira_processo_em_voo():
    """O coração do fix de 25/08: pid já enfileirado NÃO volta pra fila.

    Sem esta trava, `enrich_tjmt` tinha 99.818 jobs para 1.418 processos —
    o backlog inteiro re-enfileirado a cada 2 minutos, para sempre.
    """
    q = _queue(0)
    pend = [10, 11, 12]
    # zscore devolve valor pra 10 e 12 (já em voo) e None pra 11.
    q.connection.zmscore.return_value = [1.0, None, 1.0]
    with patch.object(jobs, '_ENRICHERS', {'TJX': object}), \
         patch('enrichers.jobs.django_rq.get_queue', return_value=q), \
         patch('enrichers.jobs.queue_for', return_value='enrich_tjx'), \
         patch('enrichers.jobs._profundidade_alvo', return_value=2_000), \
         patch('tribunals.models.Process.objects', _process_objects(pend)):
        jobs._reabastecer_impl()
    assert [c.args[1] for c in q.enqueue.call_args_list] == [11]
    # e só o 11 foi marcado em voo — marcar os outros seria mentira
    (chave, mapa), _ = q.connection.zadd.call_args
    assert chave == 'enr:inflight:tjx'
    assert list(mapa) == ['11']


def test_em_voo_e_podado_por_idade():
    """A chave de voo não cresce sem fim: poda por score antes de consultar."""
    conn = MagicMock()
    conn.zmscore.return_value = [None]
    jobs._filtrar_em_voo(conn, 'TJX', [7], 10)
    chave, minimo, maximo = conn.zremrangebyscore.call_args.args
    assert chave == 'enr:inflight:tjx'
    assert minimo == '-inf'
    # o corte é ENRICH_INFLIGHT_TTL_S atrás — não "agora", que apagaria tudo
    assert maximo < jobs.__dict__['ENRICH_INFLIGHT_TTL_S'] * 0 + 1e12


def test_profundidade_alvo_vem_da_vazao_medida():
    """Alvo = ENRICH_QUEUE_ALVO_S segundos de trabalho, pela vazão real do RQ.

    O `FinishedJobRegistry` guarda `result_ttl` (500 s em prod) de jobs
    terminados: len/500 é jobs/s sem instrumentação nova.
    """
    conn = MagicMock()
    with patch('rq.registry.FinishedJobRegistry') as reg:
        reg.return_value.__len__ = lambda self: 5_000      # 10 jobs/s
        assert jobs._profundidade_alvo(conn, 'enrich_tjx') == 10 * jobs.ENRICH_QUEUE_ALVO_S
        reg.return_value.__len__ = lambda self: 0          # fila parada
        assert jobs._profundidade_alvo(conn, 'enrich_tjx') == jobs.ENRICH_QUEUE_MIN
        reg.return_value.__len__ = lambda self: 10_000_000  # nunca acima do teto duro
        assert jobs._profundidade_alvo(conn, 'enrich_tjx') == jobs.QUEUE_HIGH_WATER
