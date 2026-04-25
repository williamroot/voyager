from datetime import datetime

from djen.parser import _hash_chaves, normalizar_cnj, parse_dt


def test_normalizar_cnj_formato_valido():
    s = '0001234-56.2025.4.01.0000'
    assert normalizar_cnj(s) == s


def test_normalizar_cnj_extrai_de_texto():
    texto = 'algum prefixo... Processo: 0001234-56.2025.4.01.0000 ... resto'
    assert normalizar_cnj(texto) == '0001234-56.2025.4.01.0000'


def test_normalizar_cnj_raw_20_digitos():
    raw = '00012345620254010000'
    assert normalizar_cnj(raw) == '0001234-56.2025.4.01.0000'


def test_normalizar_cnj_invalido_retorna_none():
    assert normalizar_cnj(None, '', 'sem cnj aqui') is None


def test_parse_dt_iso_8601():
    dt = parse_dt('2025-04-20T10:30:00')
    assert dt is not None
    assert dt.year == 2025 and dt.month == 4 and dt.day == 20


def test_parse_dt_invalido():
    assert parse_dt('') is None
    assert parse_dt('xpto') is None


def test_hash_chaves_determinismo():
    assert _hash_chaves(['b', 'a']) == _hash_chaves(['a', 'b'])
    assert _hash_chaves(['a']) != _hash_chaves(['b'])
