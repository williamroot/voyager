from datetime import date

from djen.ingestion import chunk_dates


def test_chunk_dates_30d_padrao():
    chunks = list(chunk_dates(date(2025, 1, 1), date(2025, 3, 15), days=30))
    assert chunks[0] == (date(2025, 1, 1), date(2025, 1, 30))
    assert chunks[1] == (date(2025, 1, 31), date(2025, 3, 1))
    assert chunks[2] == (date(2025, 3, 2), date(2025, 3, 15))


def test_chunk_dates_intervalo_menor_que_chunk():
    chunks = list(chunk_dates(date(2025, 1, 1), date(2025, 1, 10), days=30))
    assert chunks == [(date(2025, 1, 1), date(2025, 1, 10))]


def test_chunk_dates_dia_unico():
    chunks = list(chunk_dates(date(2025, 1, 1), date(2025, 1, 1), days=30))
    assert chunks == [(date(2025, 1, 1), date(2025, 1, 1))]
