"""Testes end-to-end do enricher TJMA.

Cobre:
  1. Configuração da subclasse (URLs, sigla, log name)
  2. Wiring (registry `_ENRICHERS`, queue RQ, auto-enqueue na ingestão)
  3. Reconhecimento do script de pesquisa do PJe-TJMA — TJMA usa
     `executarPesquisa` (sem `ReCaptcha`); o fallback `A4J.AJAX.Submit`
     da base precisa capturar `fPP:j_id252` corretamente.
  4. Fluxo completo `enriquecer()` com HTTP mockado em cima de fixtures
     HTML reais coletadas contra `pje.tjma.jus.br` (data: 2026-05-26).
     Sem DB e sem Redis — `stream.publish` é interceptado.

Fixtures vivem em `tests/fixtures/tjma/` e foram capturadas com UA
Firefox vanilla; o enricher em prod usa UA `voyager-ops/0.1` mas o
parsing/comportamento da página é o mesmo (verificado em probe live).
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup

from enrichers.pje import BasePjeEnricher, PjeEnricherError
from enrichers.tjma import TjmaEnricher

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjma'


def _resp(text: str, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode('utf-8', 'replace')
    r.encoding = 'utf-8'
    return r


def _make_enricher() -> TjmaEnricher:
    # pool mockado evita Redis; _request_with_rotation é patchado nos
    # testes que exercitam HTTP, então o pool nunca é consultado.
    return TjmaEnricher(pool=MagicMock())


# --------------------------- 1. Config ---------------------------

def test_config_endpoints_e_sigla():
    e = _make_enricher()
    assert e.TRIBUNAL_SIGLA == 'TJMA'
    assert e.BASE_URL == 'https://pje.tjma.jus.br'
    assert e.LIST_URL == 'https://pje.tjma.jus.br/pje/ConsultaPublica/listView.seam'
    assert e.DETALHE_PATH == '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    assert e.LOG_NAME == 'voyager.enrichers.tjma'


def test_e_subclasse_de_base_pje():
    """Sem código próprio — toda a lógica vem da base. Reforçar com isinstance."""
    assert issubclass(TjmaEnricher, BasePjeEnricher)


def test_construtor_rejeita_se_falta_config():
    """Sanidade da própria base: instanciar sem BASE_URL deve quebrar."""
    class Incompleto(BasePjeEnricher):
        TRIBUNAL_SIGLA = 'TJMA'
    with pytest.raises(NotImplementedError):
        Incompleto(pool=MagicMock())


# --------------------------- 2. Wiring ---------------------------

def test_registry_em_enrichers_jobs():
    from enrichers.jobs import _ENRICHERS, queue_for
    assert _ENRICHERS['TJMA'] is TjmaEnricher
    assert queue_for('TJMA') == 'enrich_tjma'


def test_fila_rq_configurada():
    from django.conf import settings
    assert 'enrich_tjma' in settings.RQ_QUEUES
    assert settings.RQ_QUEUES['enrich_tjma']['DEFAULT_TIMEOUT'] == 600


def test_auto_enqueue_na_ingestao_djen():
    from djen.ingestion import TRIBUNAIS_COM_ENRICHER
    assert 'TJMA' in TRIBUNAIS_COM_ENRICHER


def test_botao_enriquecer_disponivel_no_dashboard():
    """A condição do template é manualmente sincronizada — proteger contra
    esquecer de adicionar TJMA quando alguém refatorar."""
    tpl = (Path(__file__).resolve().parents[1]
           / 'dashboard' / 'templates' / 'dashboard' / 'processo_detail.html')
    assert "tribunal_id == 'TJMA'" in tpl.read_text()


# --------------------------- 3. Parsing do listView ---------------------------

def test_find_search_script_id_pega_fpp_j_id252():
    """TJMA não tem `executarPesquisaReCaptcha` — só `executarPesquisa`.
    A base precisa achar via fallback `A4J.AJAX.Submit`."""
    e = _make_enricher()
    html = (FIXTURES / 'listView.html').read_text()
    soup = BeautifulSoup(html, 'html.parser')
    sid = e._find_search_script_id(soup)
    assert sid == 'fPP:j_id252', (
        'Fallback A4J.AJAX.Submit deveria pegar fPP:j_id252; quebrou e '
        'o POST de pesquisa não vai disparar a action JSF.'
    )


def test_extract_form_fields_traz_view_state_e_inputs():
    e = _make_enricher()
    html = (FIXTURES / 'listView.html').read_text()
    soup = BeautifulSoup(html, 'html.parser')
    vs = soup.find('input', {'name': 'javax.faces.ViewState'})
    assert vs and vs.get('value'), 'listView precisa expor ViewState'
    fields = e._extract_form_fields(soup)
    # form fPP tem dezenas de inputs do JSF — sanidade mínima.
    assert len(fields) > 10
    assert any(k.startswith('fPP:') for k in fields)


# --------------------------- 4. Fluxo enriquecer() ---------------------------

@pytest.fixture
def processo():
    return SimpleNamespace(
        pk=42, tribunal_id='TJMA',
        numero_cnj='0801341-50.2025.8.10.0114',
    )


@pytest.fixture
def queue_responses():
    """Construtor de uma fila de respostas HTTP em ordem, devolvendo um
    patcher de `_request_with_rotation`. Cada chamada consome uma."""
    def make(*texts):
        responses = [_resp(t) for t in texts]

        def fake_rwr(self, method, url, **kw):  # noqa: ARG001
            assert responses, 'enricher fez mais requests do que o esperado'
            return responses.pop(0)

        return responses, fake_rwr
    return make


def _patches(fake_rwr):
    """Patchset comum: HTTP mockado + stream.publish capturado, time.sleep
    no-op pra não esperar 400ms entre POST e GET de detalhe."""
    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    cm_http = patch.object(BasePjeEnricher, '_request_with_rotation', fake_rwr)
    cm_pub = patch('enrichers.pje.stream.publish', side_effect=fake_publish)
    cm_sleep = patch('enrichers.pje.time.sleep')
    return captured, cm_http, cm_pub, cm_sleep


def test_enriquecer_ok_extrai_dados_e_partes(processo, queue_responses):
    listview = (FIXTURES / 'listView.html').read_text()
    post_ok = (FIXTURES / 'post_search_ok.html').read_text()
    detalhe = (FIXTURES / 'detalhe_ok.html').read_text()
    _, fake_rwr = queue_responses(listview, post_ok, detalhe)

    captured, cm_http, cm_pub, cm_sleep = _patches(fake_rwr)
    with cm_http, cm_pub, cm_sleep:
        result = _make_enricher().enriquecer(processo)

    # Retorno síncrono pro caller
    assert result['cnj'] == processo.numero_cnj
    assert result['status'] == 'ok'
    assert result['partes_total'] >= 2

    # Stream recebeu 1 payload OK
    assert len(captured) == 1
    payload = captured[0]
    assert payload['v'] == 1
    assert payload['status'] == 'ok'
    assert payload['process_id'] == 42
    assert payload['tribunal'] == 'TJMA'
    assert payload['numero_cnj'] == processo.numero_cnj

    # `dados` extraídos do propertyView
    dados = payload['dados']
    assert 'PROCEDIMENTO COMUM' in dados['classe'].upper()
    assert 'DIREITO DO CONSUMIDOR' in dados['assunto']
    assert dados['data_autuacao'] == '17/07/2025'
    # Órgão julgador pode vir do <b> tag — TJMA tem "Vara Única de Riachão"
    assert 'Riachão' in dados.get('orgao_julgador', '')

    # Polos
    ativo = payload['partes']['ativo']
    passivo = payload['partes']['passivo']
    assert any('NEEMIAS CARDOSO' in p['nome'] for p in ativo)
    assert any('BANCO DO BRASIL' in p['nome'] for p in passivo)

    # CPF/CNPJ não-mascarado preservado (TJMA igual TRF1/TJMG)
    autor = next(p for p in ativo if 'NEEMIAS CARDOSO' in p['nome'])
    assert autor['documento'].replace('.', '').replace('-', '') == '99829827372'
    assert autor['tipo'] == 'pf'
    banco = next(p for p in passivo if 'BANCO DO BRASIL' in p['nome'])
    assert banco['documento'].startswith('00.000.000') or banco['documento'] == '00000000000191'
    assert banco['tipo'] == 'pj'

    # Advogados com OAB capturados (em representantes ou principal)
    todos = [
        r
        for polo in payload['partes'].values()
        for p in polo
        for r in [p, *p.get('representantes', [])]
    ]
    advs = [p for p in todos if p['oab']]
    assert any('JOSENIEL' in a['nome'] for a in advs), \
        'advogado autor JOSENIEL BEZERRA DE ASSIS deveria ter OAB MA16087-A'
    assert any('WILSON SALES' in a['nome'] for a in advs), \
        'advogado réu WILSON SALES BELCHIOR deveria ter OAB MA11099-A'


def test_enriquecer_segredo_de_justica_emite_nao_encontrado(processo, queue_responses):
    """Quando o PJe-TJMA não acha resultado (CNJ em segredo de justiça
    ou inexistente), retorna tabela vazia. O enricher deve emitir
    `nao_encontrado` — sem 2º fetch de detalhe."""
    listview = (FIXTURES / 'listView.html').read_text()
    post_empty = (FIXTURES / 'post_search_empty.html').read_text()
    _, fake_rwr = queue_responses(listview, post_empty)

    captured, cm_http, cm_pub, cm_sleep = _patches(fake_rwr)
    with cm_http, cm_pub, cm_sleep:
        result = _make_enricher().enriquecer(processo)

    assert result['status'] == 'nao_encontrado'
    assert len(captured) == 1
    payload = captured[0]
    assert payload['status'] == 'nao_encontrado'
    assert payload['tribunal'] == 'TJMA'
    assert 'dados' not in payload
    assert 'partes' not in payload


def test_enriquecer_rejeita_tribunal_diferente():
    proc = SimpleNamespace(pk=1, tribunal_id='TRF1', numero_cnj='x')
    with pytest.raises(PjeEnricherError):
        _make_enricher().enriquecer(proc)
