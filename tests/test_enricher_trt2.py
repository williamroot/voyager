"""Testes do enricher TRT2 (PJe SPA Angular + REST API + captcha de imagem).

Cobre:
  1. Config e invariantes (endpoints, sigla, header X-Grau-Instancia).
  2. Helpers de parsing (documento canônico, data ISO→BR, valor BR).
  3. Detecção de captcha (`_tem_captcha` — desafio, resposta errada, detalhe).
  4. Fluxo completo `enriquecer()` com HTTP mockado em cima das fixtures
     JSON reais (`tests/fixtures/trt2/`, capturadas da API do TRT2 em
     2026-09-17). Sem rede, sem DB, sem CapSolver — `stream.publish` é
     interceptado, `solve_image` é stubbado e o proxy é mockado.
  5. Roteamento de grau (1g/2g/fallback).
  6. Proxy / rotação (403, pool exausto).
  7. Contrato do payload (schema v1, status, chaves obrigatórias).

As fixtures foram capturadas ao vivo: `dadosbasicos` (sem captcha) e
`detalhe_ok` (captcha resolvido via CapSolver) do processo
`1000296-68.2024.5.02.0006` (ATOrd, 6ª Vara do Trabalho de São Paulo).
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from enrichers.trt2 import (
    API_BASE,
    Trt2Enricher,
    Trt2EnricherError,
    _formatar_documento,
    _iso_para_br,
    _so_digitos,
    _tem_captcha,
    _valor_para_br,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'trt2'
CNJ = '1000296-68.2024.5.02.0006'
PID = 5959240


def _resp(json_body: dict, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(json_body, ensure_ascii=False).encode('utf-8')
    r.encoding = 'utf-8'
    r.headers['Content-Type'] = 'application/json'
    return r


def _make_enricher() -> Trt2Enricher:
    return Trt2Enricher(pool=MagicMock(), prefer_cortex=True)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --------------------------- 1. Config ---------------------------

def test_config_endpoints_e_sigla():
    e = _make_enricher()
    assert e.TRIBUNAL_SIGLA == 'TRT2'
    assert e.BASE_URL == API_BASE
    assert API_BASE == 'https://pje.trt2.jus.br/pje-consulta-api/api'
    assert e.LOG_NAME == 'voyager.enrichers.trt2'


def test_construtor_assinatura_plugavel():
    e = Trt2Enricher(pool=MagicMock(), prefer_cortex=False)
    assert e.prefer_cortex is False


def test_grau_header_obrigatorio():
    assert Trt2Enricher._grau_header('1g') == '1'
    assert Trt2Enricher._grau_header('2g') == '2'


def test_grau_do_cnj_1g():
    # foro 0006 → 1º grau
    assert Trt2Enricher._grau('1000296-68.2024.5.02.0006') == '1g'


def test_grau_do_cnj_2g():
    # foro 0000 → 2º grau (originária/apelação)
    assert Trt2Enricher._grau('0001234-56.2023.5.02.0000') == '2g'


# --------------------------- 2. Helpers ---------------------------

def test_so_digitos():
    assert _so_digitos('1000296-68.2024.5.02.0006') == '10002966820245020006'


def test_formatar_documento_cpf():
    assert _formatar_documento('40506720578') == '405.067.205-78'


def test_formatar_documento_cnpj():
    assert _formatar_documento('45543915000181') == '45.543.915/0001-81'


def test_formatar_documento_tamanho_inesperado():
    assert _formatar_documento('123') == '123'


def test_formatar_documento_ja_formatado():
    # '325.188.498-07' → só dígitos → formatar de novo
    assert _formatar_documento('325.188.498-07') == '325.188.498-07'


def test_iso_para_br():
    assert _iso_para_br('2024-02-29T11:58:33.853') == '29/02/2024'
    assert _iso_para_br('') == ''
    assert _iso_para_br(None) == ''


def test_valor_para_br():
    assert _valor_para_br(1267071.0) == 'R$ 1.267.071,00'
    assert _valor_para_br(354.67) == 'R$ 354,67'
    assert _valor_para_br(None) == ''
    assert _valor_para_br('') == ''


# --------------------------- 3. Detecção de captcha ---------------------------

def test_tem_captcha_desafio_pendente():
    d = _load(f'detalhe_captcha_{PID}.json')
    assert _tem_captcha(d) is True


def test_tem_captcha_resposta_errada():
    d = _load(f'detalhe_resposta_errada_{PID}.json')
    assert _tem_captcha(d) is True


def test_tem_captcha_detalhe_completo_false():
    d = _load(f'detalhe_ok_{PID}.json')
    assert _tem_captcha(d) is False


def test_tem_captcha_sem_token_false():
    assert _tem_captcha({}) is False
    assert _tem_captcha({'id': 1, 'numero': 'x'}) is False


def test_tem_captcha_nao_dict_false():
    assert _tem_captcha(None) is False
    assert _tem_captcha([]) is False


# --------------------------- 4. Parsing ---------------------------

def test_extrair_dados_classe_orgao_segredo():
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    dados = e._extrair_dados(d)
    assert dados['classe'] == 'ATOrd'
    assert dados['orgao_julgador'] == '6ª Vara do Trabalho de São Paulo'
    assert 'segredo_justica' not in dados  # segredoJustica=False → não entra


def test_extrair_dados_data_autuacao_valor_causa():
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    dados = e._extrair_dados(d)
    assert dados['data_autuacao'] == '29/02/2024'
    assert dados['valor_causa'] == 'R$ 1.267.071,00'


def test_extrair_dados_assunto():
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    dados = e._extrair_dados(d)
    assert dados['assunto'] == 'Adicional de Insalubridade'


def test_extrair_partes_mapeia_polos_e_representantes():
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    partes = e._extrair_partes(d)

    # poloAtivo: 1 parte (RECLAMANTE) com 2 advogados
    ativo = partes['ativo']
    assert len(ativo) == 1
    reclamante = ativo[0]
    assert reclamante['papel'] == 'RECLAMANTE'
    assert reclamante['documento'] == '405.067.205-78'
    assert reclamante['tipo_documento'] == 'CPF'
    assert reclamante['tipo'] == 'pf'
    assert len(reclamante['representantes']) == 2
    rep0 = reclamante['representantes'][0]
    assert rep0['papel'] == 'ADVOGADO'
    assert rep0['tipo'] == 'advogado'
    assert rep0['documento'] == '325.188.498-07'
    # OAB não vem na API do TRT2
    assert rep0['oab'] == ''

    # poloPassivo: 1 parte (RECLAMADO) com 1 advogado
    passivo = partes['passivo']
    assert len(passivo) == 1
    reclamado = passivo[0]
    assert reclamado['papel'] == 'RECLAMADO'
    assert reclamado['documento'] == '45.543.915/0001-81'
    assert reclamado['tipo_documento'] == 'CNPJ'
    assert reclamado['tipo'] == 'pj'
    assert len(reclamado['representantes']) == 1

    # poloOutros: 1 parte (PERITO)
    outros = partes['outros']
    assert len(outros) == 1
    perito = outros[0]
    assert perito['papel'] == 'PERITO'
    assert perito['tipo'] == 'pf'


def test_extrair_partes_nome_inicial_sigla_nao_inclui_sigla():
    """Nomes como 'J. B. C. P.' são preservados intactos (iniciais)."""
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    partes = e._extrair_partes(d)
    assert partes['ativo'][0]['nome'] == 'J. B. C. P.'


def test_extrair_dados_segredo_justica_true():
    """segredoJustica=True → campo entra no dados."""
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    d['segredoJustica'] = True
    dados = e._extrair_dados(d)
    assert dados['segredo_justica'] is True


def test_extrair_dados_valor_causa_none_nao_entra():
    """valorDaCausa=None → campo não entra (abstenção, não zero)."""
    e = _make_enricher()
    d = _load(f'detalhe_ok_{PID}.json')
    d['valorDaCausa'] = None
    dados = e._extrair_dados(d)
    assert 'valor_causa' not in dados


# --------------------------- 5. Fluxo enriquecer() ---------------------------

@pytest.fixture
def processo():
    return SimpleNamespace(
        pk=42, tribunal_id='TRT2',
        numero_cnj=CNJ,
    )


def _run(enricher, processo, responses, solve_result='abc123'):
    """Roda enriquecer() com session.request mockado para devolver
    `responses` em sequência. Captura o payload publicado no stream.

    `responses` pode ser uma lista de dicts (JSON) ou Response objects.
    """
    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    if responses and not isinstance(responses[0], requests.Response):
        responses = [_resp(r) for r in responses]

    mock_request = MagicMock(side_effect=responses)
    enricher.session.request = mock_request
    with patch.object(Trt2Enricher, '_next_proxy', return_value='http://dummy'), \
            patch('enrichers.trt2.stream.publish', side_effect=fake_publish), \
            patch('enrichers.trt2.solve_image', return_value=solve_result):
        result = enricher.enriquecer(processo, direct_apply=False)
    return result, captured, mock_request


def test_enriquecer_ok_com_captcha_resolvido(processo):
    """Fluxo completo: dadosbasicos → captcha → solve → detalhe_ok."""
    db = _load(f'dadosbasicos_{CNJ}_1g.json')
    cap = _load(f'detalhe_captcha_{PID}.json')
    ok = _load(f'detalhe_ok_{PID}.json')

    result, captured, mock_req = _run(_make_enricher(), processo,
                                      [db, cap, ok], solve_result='fwh663')

    assert result['status'] == 'ok'
    assert result['partes_total'] == 3  # 1 ativo + 1 passivo + 1 outros
    assert len(captured) == 1
    payload = captured[0]
    assert payload['v'] == 1
    assert payload['status'] == 'ok'
    assert payload['process_id'] == 42
    assert payload['tribunal'] == 'TRT2'
    assert payload['numero_cnj'] == CNJ
    assert 'dados' in payload
    assert 'partes' in payload
    assert payload['dados']['classe'] == 'ATOrd'
    assert payload['dados']['valor_causa'] == 'R$ 1.267.071,00'

    # 3 chamadas: dadosbasicos, detalhe(captcha), detalhe(com resposta)
    assert mock_req.call_count == 3
    # A 3ª chamada tem params tokenDesafio + resposta
    last_call = mock_req.call_args_list[-1]
    assert last_call.kwargs['params']['resposta'] == 'fwh663'


def test_enriquecer_ok_sem_captcha(processo):
    """Se o detalhe não exigir captcha, devolve direto (2 chamadas só)."""
    db = _load(f'dadosbasicos_{CNJ}_1g.json')
    ok = _load(f'detalhe_ok_{PID}.json')

    result, captured, mock_req = _run(_make_enricher(), processo, [db, ok])

    assert result['status'] == 'ok'
    assert mock_req.call_count == 2  # dadosbasicos + detalhe direto


def test_enriquecer_nao_encontrado(processo):
    """dadosbasicos vazio em ambos os graus → nao_encontrado."""
    vazio = []
    # 1g vazio, 2g vazio
    result, captured, mock_req = _run(_make_enricher(), processo, [vazio, vazio])

    assert result['status'] == 'nao_encontrado'
    assert len(captured) == 1
    assert captured[0]['status'] == 'nao_encontrado'
    assert 'dados' not in captured[0]
    assert 'partes' not in captured[0]


def test_enriquecer_rejeita_tribunal_diferente():
    proc = SimpleNamespace(pk=1, tribunal_id='TRF1', numero_cnj='x')
    with pytest.raises(Trt2EnricherError):
        _make_enricher().enriquecer(proc)


def test_enriquecer_cnj_invalido(processo):
    """CNJ sem 20 dígitos → erro."""
    processo.numero_cnj = '123'
    result, captured, _ = _run(_make_enricher(), processo, [])
    assert result['status'] == 'erro'
    assert captured[0]['status'] == 'erro'


def test_enriquecer_captcha_nao_resolvido_vira_erro(processo):
    """solve_image falha 3x (MAX_TENTATIVAS_CAPTCHA) → erro."""
    db = _load(f'dadosbasicos_{CNJ}_1g.json')
    cap = _load(f'detalhe_captcha_{PID}.json')

    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    # Cada GET /processos/{id} devolve captcha (sempre pendente)
    cap_resps = [_resp(cap), _resp(cap), _resp(cap)]
    enricher = _make_enricher()
    enricher.session.request = MagicMock(side_effect=[_resp(db)] + cap_resps)
    with patch.object(Trt2Enricher, '_next_proxy', return_value='http://dummy'), \
            patch('enrichers.trt2.stream.publish', side_effect=fake_publish), \
            patch('enrichers.trt2.solve_image', side_effect=Exception('CapSolver timeout')):
        result = enricher.enriquecer(processo, direct_apply=False)

    assert result['status'] == 'erro'
    assert 'captcha' in result['erro'].lower() or 'cap' in result['erro'].lower()


def test_enriquecer_resposta_errada_rota_novo_desafio(processo):
    """1ª resposta errada → re-tenta com novo desafio → 2ª resolve → ok.

    Sequência: dadosbasicos → captcha1 → solve('errada') → resposta_errada
    (novo tokenDesafio) → captcha2 → solve('certa') → detalhe_ok.
    """
    db = _load(f'dadosbasicos_{CNJ}_1g.json')
    cap1 = _load(f'detalhe_captcha_{PID}.json')
    errada = _load(f'detalhe_resposta_errada_{PID}.json')
    ok = _load(f'detalhe_ok_{PID}.json')

    # A 1ª chamada de solve devolve 'errada', a 2ª 'certa'.
    # Mas o mock de solve_image é um side_effect fixo — precisamos variar.
    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    enricher = _make_enricher()
    # Sequência de responses: db, cap1, errada (resposta errada), cap1 (novo
    # desafio do errada... mas o errada JÁ tem tokenDesafio+imagem, então a
    # próxima chamada usa esses campos). Na prática: db → cap1 → solve →
    # errada → solve → ok.
    enricher.session.request = MagicMock(side_effect=[
        _resp(db),      # dadosbasicos
        _resp(cap1),    # detalhe → captcha pendente
        _resp(errada),  # detalhe com resposta errada → novo desafio
        _resp(ok),      # detalhe com resposta certa → ok
    ])
    with patch.object(Trt2Enricher, '_next_proxy', return_value='http://dummy'), \
            patch('enrichers.trt2.stream.publish', side_effect=fake_publish), \
            patch('enrichers.trt2.solve_image', side_effect=['errada', 'certa']):
        result = enricher.enriquecer(processo, direct_apply=False)

    assert result['status'] == 'ok'
    assert result['partes_total'] == 3


def test_enriquecer_dadosbasicos_500_vira_erro(processo):
    """HTTP 500 no dadosbasicos → erro (HTTPError da requests)."""
    r500 = _resp({'codigoErro': 'ARQ-012', 'mensagem': 'erro'}, status=500)
    enricher = _make_enricher()
    enricher.session.request = MagicMock(side_effect=[r500])
    with patch.object(Trt2Enricher, '_next_proxy', return_value='http://dummy'), \
            patch('enrichers.trt2.stream.publish', side_effect=lambda p, **kw: '0-0'):
        result = enricher.enriquecer(processo, direct_apply=False)
    assert result['status'] == 'erro'


# --------------------------- 6. Roteamento de grau ---------------------------

def test_fallback_de_grau(processo):
    """Grau 1 não acha (dadosbasicos vazio), tenta grau 2, acha."""
    db2 = _load(f'dadosbasicos_{CNJ}_2g.json')
    ok = _load(f'detalhe_ok_{PID}.json')

    # CNJ com foro 0006 → grau 1 primeiro. 1g vazio → fallback 2g.
    result, captured, mock_req = _run(
        _make_enricher(), processo,
        [[], db2, ok],  # 1g vazio, 2g acha, detalhe ok
    )
    assert result['status'] == 'ok'
    # 3 chamadas: 1g-dadosbasicos, 2g-dadosbasicos, detalhe
    assert mock_req.call_count == 3


# --------------------------- 7. Proxy / rotação ---------------------------

def test_request_com_rotacao_em_403():
    """1º proxy 403, 2º 200 → usa 2º, marca 1º como bad."""
    enricher = _make_enricher()
    r403 = _resp({}, status=403)
    r200 = _resp({'ok': True}, status=200)
    enricher.session.request = MagicMock(side_effect=[r403, r200])

    proxies_seq = ['http://p1', 'http://p2']
    enricher._next_proxy = MagicMock(side_effect=proxies_seq)
    enricher.pool = MagicMock()

    resp = enricher._request_with_rotation('GET', 'http://x')
    assert resp.status_code == 200
    # p1 foi marcado bad
    enricher.pool.mark_bad.assert_called_once_with('http://p1')


def test_pool_exausto_vira_erro():
    """Nenhum proxy disponível → HTTPError."""
    enricher = _make_enricher()
    enricher._next_proxy = MagicMock(return_value=None)
    with pytest.raises(requests.HTTPError):
        enricher._request_with_rotation('GET', 'http://x')


# --------------------------- 8. Contrato do payload ---------------------------

def test_payload_ok_tem_schema_v1(processo):
    db = _load(f'dadosbasicos_{CNJ}_1g.json')
    ok = _load(f'detalhe_ok_{PID}.json')
    result, captured, _ = _run(_make_enricher(), processo, [db, ok])
    p = captured[0]
    assert p['v'] == 1
    assert p['status'] == 'ok'
    for k in ('process_id', 'tribunal', 'numero_cnj', 'scraped_at', 'dados', 'partes'):
        assert k in p


def test_payload_nao_encontrado_sem_dados_nem_partes(processo):
    result, captured, _ = _run(_make_enricher(), processo, [[], []])
    p = captured[0]
    assert p['status'] == 'nao_encontrado'
    assert 'dados' not in p
    assert 'partes' not in p


def test_payload_erro_tem_erro_truncado(processo):
    processo.numero_cnj = '123'
    result, captured, _ = _run(_make_enricher(), processo, [])
    p = captured[0]
    assert p['status'] == 'erro'
    assert 'erro' in p
    assert len(p['erro']) <= 1000


def test_payload_scraped_at_em_utc_iso(processo):
    """scraped_at deve terminar com '+00:00' (UTC ISO 8601)."""
    db = _load(f'dadosbasicos_{CNJ}_1g.json')
    ok = _load(f'detalhe_ok_{PID}.json')
    result, captured, _ = _run(_make_enricher(), processo, [db, ok])
    sa = captured[0]['scraped_at']
    assert sa.endswith('+00:00') or sa.endswith('Z')