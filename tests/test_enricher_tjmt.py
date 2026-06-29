"""Testes do enricher TJMT (PJe SPA Angular + REST API).

Cobre:
  1. Geração do header X-Fingerprint (HMAC-SHA256 + formato JSON) — o
     algoritmo extraído do bundle `main-*.js` (função `Bd()`).
  2. Helpers de formatação (documento canônico, data ISO→BR, valor BR).
  3. Fluxo completo `enriquecer()` com HTTP mockado em cima das fixtures
     JSON reais (`tests/fixtures/tjmt/search_v2_*.json`, capturadas via
     Cortex em 2026-06-29). Sem rede e sem DB — `stream.publish` é
     interceptado e o proxy é stubbado.

Verifica que o parsing mapeia partes (polo/papel/doc) e advogados como
`representantes` (papel ADVOGADO, OAB) no formato que o drainer consome.
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from enrichers.tjmt import (
    FINGERPRINT_KEY,
    TjmtEnricher,
    TjmtEnricherError,
    _formatar_documento,
    _iso_para_br,
    _valor_para_br,
    gerar_fingerprint,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjmt'


def _resp(text: str, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode('utf-8', 'replace')
    r.encoding = 'utf-8'
    return r


def _make_enricher() -> TjmtEnricher:
    # pool mockado evita Redis; _next_proxy é stubbado nos testes de HTTP.
    return TjmtEnricher(pool=MagicMock(), prefer_cortex=True)


# --------------------------- 1. Config ---------------------------

def test_config_endpoints_e_sigla():
    e = _make_enricher()
    assert e.TRIBUNAL_SIGLA == 'TJMT'
    assert e.BASE_URL == 'https://hellsgate.tjmt.jus.br'
    assert e.SEARCH_PATH == '/consultaprocessual/ProcessosJudiciais/v2'
    assert e.LOG_NAME == 'voyager.enrichers.tjmt'
    assert e.OAB_UF == 'MT'


def test_construtor_assinatura_plugavel_no_registry():
    """Assinatura idêntica aos outros enrichers (pool, prefer_cortex)."""
    e = TjmtEnricher(pool=MagicMock(), prefer_cortex=False)
    assert e.prefer_cortex is False


# --------------------------- 2. X-Fingerprint ---------------------------

def test_gerar_fingerprint_estrutura_e_assinatura():
    """X-Fingerprint reproduz a função Bd() do bundle Angular:
    msg = '{userAgent}-{screenResolution}-{language}-{timestamp}',
    signature = base64(HMAC_SHA256(msg, FINGERPRINT_KEY))."""
    ts = 1_700_000_000_000
    fp = gerar_fingerprint(ts_ms=ts)
    obj = json.loads(fp)

    assert set(obj) == {'signature', 'timestamp', 'userAgent',
                        'screenResolution', 'language'}
    assert obj['timestamp'] == ts
    assert obj['screenResolution'] == '1920x1080'
    assert obj['language'] == 'pt-BR'

    msg = f"{obj['userAgent']}-{obj['screenResolution']}-{obj['language']}-{ts}"
    esperado = base64.b64encode(
        hmac.new(FINGERPRINT_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    assert obj['signature'] == esperado


def test_gerar_fingerprint_chave_correta():
    """A chave hardcoded do bundle é a do TJMT (regressão se mudar)."""
    assert FINGERPRINT_KEY == 'A_mesma_mao_que_aplaude_e_a_que_vaia!'


def test_gerar_fingerprint_fresco_por_chamada():
    """Sem ts_ms usa o relógio — dois fingerprints diferem (timestamp novo)."""
    import time
    a = json.loads(gerar_fingerprint())
    time.sleep(0.002)
    b = json.loads(gerar_fingerprint())
    assert b['timestamp'] >= a['timestamp']
    # Assinatura válida pra o timestamp gerado.
    msg = f"{b['userAgent']}-{b['screenResolution']}-{b['language']}-{b['timestamp']}"
    esperado = base64.b64encode(
        hmac.new(FINGERPRINT_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    assert b['signature'] == esperado


# --------------------------- 3. Helpers ---------------------------

def test_formatar_documento():
    assert _formatar_documento('07015990000180', 'CNPJ') == '07.015.990/0001-80'
    assert _formatar_documento('03234678146', 'CPF') == '032.346.781-46'
    # Tamanho inesperado → dígitos crus (defensivo).
    assert _formatar_documento('123', 'CPF') == '123'


def test_iso_para_br():
    assert _iso_para_br('2026-04-14T10:18:02.841') == '14/04/2026'
    assert _iso_para_br('') == ''


def test_valor_para_br():
    assert _valor_para_br(354.67) == 'R$ 354,67'
    assert _valor_para_br(177781.42) == 'R$ 177.781,42'
    assert _valor_para_br(None) == ''


# --------------------------- 4. Fluxo enriquecer() ---------------------------

@pytest.fixture
def processo():
    return SimpleNamespace(
        pk=42, tribunal_id='TJMT',
        numero_cnj='1005334-93.2026.8.11.0037',
    )


def _run(enricher, processo, fixture_text):
    """Roda enriquecer() com session.get e _next_proxy stubbados; captura
    o payload publicado no stream."""
    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    enricher.session.get = MagicMock(return_value=_resp(fixture_text))
    with patch.object(TjmtEnricher, '_next_proxy', return_value='http://dummy'), \
            patch('enrichers.tjmt.stream.publish', side_effect=fake_publish):
        result = enricher.enriquecer(processo, direct_apply=False)
    return result, captured, enricher.session.get


def test_enriquecer_ok_extrai_dados_e_partes(processo):
    fixture = (FIXTURES / 'search_v2_1005334-93.2026.8.11.0037.json').read_text()
    result, captured, mock_get = _run(_make_enricher(), processo, fixture)

    assert result['status'] == 'ok'
    assert result['partes_total'] == 2  # 1 ativo (EXEQUENTE) + 1 passivo (EXECUTADO)
    assert len(captured) == 1
    payload = captured[0]
    assert payload['v'] == 1
    assert payload['status'] == 'ok'
    assert payload['process_id'] == 42
    assert payload['tribunal'] == 'TJMT'

    # X-Fingerprint foi enviado e é JSON válido com assinatura.
    _, kwargs = mock_get.call_args
    fp = json.loads(kwargs['headers']['X-Fingerprint'])
    assert fp['signature'] and fp['timestamp']
    # numeroUnico sem máscara nos params.
    assert kwargs['params']['numeroUnico'] == '10053349320268110037'

    # dados
    dados = payload['dados']
    assert dados['classe'] == 'EXECUÇÃO DE TÍTULO EXTRAJUDICIAL (12154)'
    assert dados['orgao_julgador'] == 'JUIZADOS ESPECIAIS DE PRIMAVERA DO LESTE'
    assert dados['data_autuacao'] == '14/04/2026'
    assert dados['valor_causa'] == 'R$ 354,67'

    # polos
    ativo = payload['partes']['ativo']
    passivo = payload['partes']['passivo']
    assert len(ativo) == 1 and len(passivo) == 1

    # principal ativo: PJ (CNPJ), papel EXEQUENTE
    exeq = ativo[0]
    assert exeq['nome'] == 'ADIR ALFREDO WACHHOLZ - ME'
    assert exeq['papel'] == 'EXEQUENTE'
    assert exeq['documento'] == '07.015.990/0001-80'
    assert exeq['tipo'] == 'pj'

    # advogados entram como representantes (papel ADVOGADO + OAB MT)
    reps = exeq['representantes']
    assert len(reps) == 2
    rafael = next(r for r in reps if 'RAFAEL' in r['nome'])
    assert rafael['papel'] == 'ADVOGADO'
    assert rafael['tipo'] == 'advogado'
    assert rafael['oab'] == 'MT20688'
    assert rafael['documento'] == '022.515.681-40'
    # Sufixo alfanumérico preservado ('30885/O' → 'MT30885O').
    alvaro = next(r for r in reps if 'ALVARO' in r['nome'])
    assert alvaro['oab'] == 'MT30885O'

    # passivo: PF (CPF), sem advogado
    exec_ = passivo[0]
    assert exec_['nome'] == 'VALERIA CRISTINA DE SOUZA'
    assert exec_['papel'] == 'EXECUTADO'
    assert exec_['tipo'] == 'pf'
    assert exec_['representantes'] == []


def test_enriquecer_segunda_fixture_polos(processo):
    """A 2ª fixture tem ordem passivo→ativo→passivo; confere agrupamento."""
    processo.numero_cnj = '1001498-88.2026.8.11.0045'
    fixture = (FIXTURES / 'search_v2_1001498-88.2026.8.11.0045.json').read_text()
    result, captured, _ = _run(_make_enricher(), processo, fixture)

    assert result['status'] == 'ok'
    payload = captured[0]
    ativo = payload['partes']['ativo']
    passivo = payload['partes']['passivo']
    assert [p['nome'] for p in ativo] == ['BANCO DO BRASIL S.A.']
    assert ativo[0]['documento'] == '00.000.000/0001-91'
    assert ativo[0]['representantes'][0]['oab'] == 'MT17210'
    assert {p['nome'] for p in passivo} == {
        'RD EMPREENDIMENTOS IMOBILIARIOS LTDA', 'ROBERTO JOAO VALENTIM MILKE',
    }
    assert payload['dados']['valor_causa'] == 'R$ 177.781,42'


def test_enriquecer_nao_encontrado(processo):
    """itens vazio (ou sem match do numeroUnico) → nao_encontrado, sem dados."""
    vazio = json.dumps({'pagina': 0, 'totalRegistros': 0, 'itens': []})
    result, captured, _ = _run(_make_enricher(), processo, vazio)

    assert result['status'] == 'nao_encontrado'
    assert len(captured) == 1
    assert captured[0]['status'] == 'nao_encontrado'
    assert 'dados' not in captured[0]
    assert 'partes' not in captured[0]


def test_enriquecer_rejeita_tribunal_diferente():
    proc = SimpleNamespace(pk=1, tribunal_id='TRF1', numero_cnj='x')
    with pytest.raises(TjmtEnricherError):
        _make_enricher().enriquecer(proc)


def test_buscar_cnj_invalido_emite_erro(processo):
    """CNJ sem 20 dígitos → status erro (não estoura sem capturar)."""
    processo.numero_cnj = '123'
    result, captured, _ = _run(_make_enricher(), processo, '{}')
    assert result['status'] == 'erro'
    assert captured[0]['status'] == 'erro'
