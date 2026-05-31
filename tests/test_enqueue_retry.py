"""Auto-retry de falhas transientes (Redis/PG drop) nas filas enrich/datajud.

Sem isso, cada blip de conexão virava failed job permanente (RQ não re-tenta
sozinho) — causa do acúmulo de 244k failed na datajud, 34k na tjmg, etc.
"""
from unittest.mock import MagicMock, patch

from rq import Retry


def test_enrich_e_datajud_retry_sao_max_3():
    from datajud.jobs import DATAJUD_RETRY
    from enrichers.jobs import ENRICH_RETRY
    assert isinstance(ENRICH_RETRY, Retry) and ENRICH_RETRY.max == 3
    assert isinstance(DATAJUD_RETRY, Retry) and DATAJUD_RETRY.max == 3


def test_enqueue_enriquecimento_passa_retry():
    from enrichers.jobs import ENRICH_RETRY, enqueue_enriquecimento
    q = MagicMock()
    with patch('enrichers.jobs.django_rq.get_queue', return_value=q):
        enqueue_enriquecimento(123, 'TJSP')
    assert q.enqueue.call_args.kwargs.get('retry') is ENRICH_RETRY


def test_enqueue_manual_passa_retry():
    from enrichers.jobs import ENRICH_RETRY, enqueue_enriquecimento_manual
    q = MagicMock()
    with patch('enrichers.jobs.django_rq.get_queue', return_value=q):
        enqueue_enriquecimento_manual(123)
    assert q.enqueue.call_args.kwargs.get('retry') is ENRICH_RETRY
