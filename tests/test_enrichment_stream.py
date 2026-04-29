"""Testes do contrato de stream de enrichment (sem DB)."""
import json
from unittest.mock import MagicMock

import pytest

from enrichers import stream


def test_build_ok_payload_inclui_v_e_status():
    p = stream.build_ok_payload(
        process_id=42, tribunal='TRF1', numero_cnj='0001-23',
        scraped_at='2026-04-29T00:00:00', dados={'classe': 'X'}, partes={'ativo': []},
    )
    assert p['v'] == stream.SCHEMA_VERSION
    assert p['status'] == stream.STATUS_OK
    assert p['process_id'] == 42
    assert p['dados'] == {'classe': 'X'}
    assert p['partes'] == {'ativo': []}


def test_build_erro_payload_trunca_msg_grande():
    msg = 'X' * 5000
    p = stream.build_erro_payload(
        process_id=1, tribunal='TRF1', numero_cnj='c', scraped_at='t', erro=msg,
    )
    assert p['status'] == stream.STATUS_ERRO
    assert len(p['erro']) == 1000


def test_parse_entry_round_trip():
    payload = stream.build_ok_payload(
        process_id=10, tribunal='TRF3', numero_cnj='0001',
        scraped_at='2026-04-29T01:00:00', dados={'classe': 'C', 'valor_causa': 'R$ 10'},
        partes={'ativo': [{'nome': 'X', 'documento': '', 'oab': 'SP1', 'tipo': 'advogado'}]},
    )
    fields = stream.to_redis_fields(payload)
    assert set(fields.keys()) == {'data'}
    parsed = stream.parse_entry(fields)
    assert parsed == payload


def test_parse_entry_aceita_bytes():
    payload = stream.build_nao_encontrado_payload(
        process_id=1, tribunal='TRF1', numero_cnj='c', scraped_at='t',
    )
    raw = stream.to_redis_fields(payload)
    fields_bytes = {b'data': raw['data'].encode()}
    parsed = stream.parse_entry(fields_bytes)
    assert parsed == payload


def test_parse_entry_versao_desconhecida_retorna_none():
    bad = json.dumps({'v': 999, 'status': 'ok'})
    assert stream.parse_entry({'data': bad}) is None


def test_parse_entry_status_invalido_retorna_none():
    bad = json.dumps({'v': stream.SCHEMA_VERSION, 'status': 'maluco'})
    assert stream.parse_entry({'data': bad}) is None


def test_parse_entry_json_invalido_retorna_none():
    assert stream.parse_entry({'data': '{not json'}) is None


def test_parse_entry_sem_data_retorna_none():
    assert stream.parse_entry({}) is None


def test_parse_entry_acima_do_cap_retorna_none():
    big = '{"v": 1, "status": "ok", "fluff": "' + ('x' * (stream.MAX_PAYLOAD_BYTES + 100)) + '"}'
    assert stream.parse_entry({'data': big}) is None


def test_publish_aplica_maxlen():
    """publish() chama xadd com maxlen ~ STREAM_MAXLEN — defense-in-depth."""
    fake_redis = MagicMock()
    fake_redis.xadd.return_value = b'1-0'
    payload = stream.build_nao_encontrado_payload(
        process_id=1, tribunal='TRF1', numero_cnj='c', scraped_at='t',
    )
    stream.publish(payload, redis_client=fake_redis)
    _args, kwargs = fake_redis.xadd.call_args
    assert kwargs.get('maxlen') == stream.STREAM_MAXLEN
    assert kwargs.get('approximate') is True


def test_publish_chama_xadd_com_fields_corretos():
    fake_redis = MagicMock()
    fake_redis.xadd.return_value = b'1714350000000-0'
    payload = stream.build_ok_payload(
        process_id=1, tribunal='TRF1', numero_cnj='c', scraped_at='t',
        dados={}, partes={},
    )
    msg_id = stream.publish(payload, redis_client=fake_redis)
    assert msg_id == '1714350000000-0'
    fake_redis.xadd.assert_called_once()
    args, _ = fake_redis.xadd.call_args
    assert args[0] == stream.STREAM_KEY
    body = json.loads(args[1]['data'])
    assert body == payload


def test_ensure_consumer_group_idempotente_em_busygroup():
    fake_redis = MagicMock()
    fake_redis.xgroup_create.side_effect = Exception('BUSYGROUP Consumer Group name already exists')
    # Não deve levantar.
    stream.ensure_consumer_group(fake_redis)


def test_ensure_consumer_group_propaga_erros_outros():
    fake_redis = MagicMock()
    fake_redis.xgroup_create.side_effect = RuntimeError('redis down')
    with pytest.raises(RuntimeError):
        stream.ensure_consumer_group(fake_redis)
