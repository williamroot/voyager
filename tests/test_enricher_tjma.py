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


def test_botao_enriquecer_e_registry_driven():
    """O botão de enrich é derivado de `_ENRICHERS` (via context
    `pode_enriquecer`), não de um or-chain hardcoded no template — então
    qualquer tribunal com enricher registrado ganha o botão. TJMA está no
    registry (test_registry_em_enrichers_jobs)."""
    from enrichers.jobs import _ENRICHERS
    assert 'TJMA' in _ENRICHERS
    tpl = (Path(__file__).resolve().parents[1]
           / 'dashboard' / 'templates' / 'dashboard' / 'processo_detail.html')
    body = tpl.read_text()
    assert '{% if pode_enriquecer %}' in body
    assert "tribunal_id == 'TJMA'" not in body


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
    `nao_encontrado` — sem fetch de detalhe.

    Como o TJMA tem 2º grau configurado, um miss no 1º grau dispara o
    fallback pro 2º grau (busca, não detalhe) antes de concluir — daí as
    4 respostas: 1g list/search vazio + 2g list/search vazio."""
    listview = (FIXTURES / 'listView.html').read_text()
    post_empty = (FIXTURES / 'post_search_empty.html').read_text()
    listview_2g = (FIXTURES / 'listView_2g.html').read_text()
    _, fake_rwr = queue_responses(listview, post_empty, listview_2g, post_empty)

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


# --------------------------- 5. 2º grau (pje2.tjma.jus.br/pje2g) ---------------------------

def test_config_2g_endpoints():
    """2º grau é uma instância PJe separada (host próprio). A subclasse só
    declara as URLs `*_2G`; a base roteia. Confere que apontam pro pje2g."""
    e = _make_enricher()
    assert e.BASE_URL_2G == 'https://pje2.tjma.jus.br'
    assert e.LIST_URL_2G == 'https://pje2.tjma.jus.br/pje2g/ConsultaPublica/listView.seam'
    assert e.DETALHE_PATH_2G == '/pje2g/ConsultaPublica/DetalheProcessoConsultaPublica'


@pytest.mark.parametrize('cnj, esperado', [
    ('0836521-81.2025.8.10.0000', '2g'),   # originária do tribunal (OOOO=0000)
    ('0843265-07.2016.8.10.0001', '1g'),   # apelação mantém comarca de origem
    ('0801341-50.2025.8.10.0114', '1g'),   # 1º grau comum
    ('', '1g'),                            # defensivo: vazio não quebra
])
def test_grau_heuristica_por_cnj(cnj, esperado):
    """`_grau` é só um PALPITE barato (OOOO=='0000') pra escolher a instância
    inicial. No TJMA a apelação termina no código da comarca (0001), não 0000 —
    por isso a busca tem fallback (test_grau_fallback_*)."""
    assert BasePjeEnricher._grau(cnj) == esperado


def test_urls_for_grau_roteia_por_instancia():
    e = _make_enricher()
    assert e._urls_for_grau('1g') == (e.BASE_URL, e.LIST_URL, e.DETALHE_PATH)
    assert e._urls_for_grau('2g') == (e.BASE_URL_2G, e.LIST_URL_2G, e.DETALHE_PATH_2G)


def test_urls_for_grau_cai_pro_1g_quando_sem_2g_configurado():
    """Tribunal só-1g (sem LIST_URL_2G) nunca sai das URLs de 1g, mesmo pra
    um CNJ originário — comportamento legado preservado pros TRFs/TJMG."""
    class So1g(BasePjeEnricher):
        BASE_URL = 'https://pje.x.jus.br'
        LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
        DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
        TRIBUNAL_SIGLA = 'TJMA'
    e = So1g(pool=MagicMock())
    assert e._urls_for_grau('2g') == (e.BASE_URL, e.LIST_URL, e.DETALHE_PATH)


@pytest.fixture
def processo_2g():
    # Agravo de Instrumento de competência originária — só existe no 2º grau.
    return SimpleNamespace(
        pk=99, tribunal_id='TJMA',
        numero_cnj='0836521-81.2025.8.10.0000',
    )


def test_enriquecer_2g_originaria_via_pje2g(processo_2g, queue_responses):
    """Fluxo completo de 2º grau com fixtures HTML reais capturadas contra
    pje2.tjma.jus.br/pje2g (Agravo de Instrumento, Primeira Câmara de Direito
    Público). Palpite=2g (OOOO=0000) → busca direta no pje2g, sem fallback."""
    listview = (FIXTURES / 'listView_2g.html').read_text()
    post_ok = (FIXTURES / 'post_search_ok_2g.html').read_text()
    detalhe = (FIXTURES / 'detalhe_ok_2g.html').read_text()
    _, fake_rwr = queue_responses(listview, post_ok, detalhe)

    captured, cm_http, cm_pub, cm_sleep = _patches(fake_rwr)
    with cm_http, cm_pub, cm_sleep:
        result = _make_enricher().enriquecer(processo_2g)

    assert result['status'] == 'ok'
    assert result['partes_total'] >= 2

    payload = captured[0]
    dados = payload['dados']
    assert 'AGRAVO DE INSTRUMENTO' in dados['classe'].upper()
    # Órgão julgador é de 2ª instância (câmara), não vara — prova que veio do 2g.
    assert 'Câmara' in dados.get('orgao_julgador', '')

    passivo = payload['partes']['passivo']
    assert any('ESTADO DO MARANHAO' in p['nome'] for p in passivo)
    estado = next(p for p in passivo if 'ESTADO DO MARANHAO' in p['nome'])
    assert estado['tipo'] == 'pj'

    # Advogada com OAB capturada (CPF/CNPJ não-mascarado, igual ao 1º grau).
    advs = [
        r
        for polo in payload['partes'].values()
        for p in polo
        for r in [p, *p.get('representantes', [])]
        if r['oab']
    ]
    assert any('SONIA MARIA LOPES COELHO' in a['nome'] for a in advs)


def test_grau_fallback_quando_1g_nao_acha_tenta_2g(queue_responses):
    """Se o palpite cai no 1º grau mas o processo não está lá, a base tenta o
    2º grau antes de desistir. Sequência: GET 1g list → POST 1g (vazio) →
    GET 2g list → POST 2g (ok) → GET detalhe 2g. CNJ não-originário (palpite
    1g) que só existe no 2g exercita exatamente esse caminho."""
    proc = SimpleNamespace(pk=77, tribunal_id='TJMA',
                           numero_cnj='0836521-81.2025.8.10.0114')
    listview_1g = (FIXTURES / 'listView.html').read_text()
    post_empty = (FIXTURES / 'post_search_empty.html').read_text()
    listview_2g = (FIXTURES / 'listView_2g.html').read_text()
    post_ok_2g = (FIXTURES / 'post_search_ok_2g.html').read_text()
    detalhe_2g = (FIXTURES / 'detalhe_ok_2g.html').read_text()
    responses, fake_rwr = queue_responses(
        listview_1g, post_empty, listview_2g, post_ok_2g, detalhe_2g,
    )

    captured, cm_http, cm_pub, cm_sleep = _patches(fake_rwr)
    with cm_http, cm_pub, cm_sleep:
        result = _make_enricher().enriquecer(proc)

    assert result['status'] == 'ok'
    assert not responses, 'esperado consumir as 5 respostas (1g miss + 2g hit)'
    assert 'AGRAVO DE INSTRUMENTO' in captured[0]['dados']['classe'].upper()
