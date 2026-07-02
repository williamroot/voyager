"""Testes do rate-limiter global do Datajud (token-bucket Redis)."""
import time

import pytest
from django.test import override_settings

from datajud import ratelimit


@pytest.fixture(autouse=True)
def _limpa_bucket():
    import django_rq
    django_rq.get_connection('datajud').delete(ratelimit._BUCKET_KEY)
    yield


@override_settings(DATAJUD_RATE_LIMIT_RPM=0)
def test_rpm_zero_desliga():
    # 0 = sem limite → sempre True, instantâneo
    assert ratelimit.acquire(max_wait=0) is True


@override_settings(DATAJUD_RATE_LIMIT_RPM=6000)
def test_rpm_alto_libera():
    # 6000/min = 100/s → primeiro token sai na hora
    assert ratelimit.acquire(max_wait=0) is True


@override_settings(DATAJUD_RATE_LIMIT_RPM=60)
def test_estoura_quando_sem_token():
    # cap=60; drena os 60 tokens do burst, o próximo (com max_wait=0) falha
    for _ in range(60):
        assert ratelimit.acquire(max_wait=0) is True
    assert ratelimit.acquire(max_wait=0) is False


@override_settings(DATAJUD_RATE_LIMIT_RPM=60)
def test_refill_ao_longo_do_tempo():
    for _ in range(60):
        ratelimit.acquire(max_wait=0)
    assert ratelimit.acquire(max_wait=0) is False
    time.sleep(1.1)  # 60/min = 1/s → ~1 token repõe
    assert ratelimit.acquire(max_wait=0) is True
