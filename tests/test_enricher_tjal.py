"""Testes end-to-end do enricher TJAL (e-SAJ).

TJAL roda o mesmo software e-SAJ do TJSP (`www2.tjal.jus.br/cpopg/`). O enricher
reusa toda a lógica de `BaseEsajEnricher`; a subclasse só troca `BASE_URL`,
`TRIBUNAL_SIGLA` e `LOG_NAME`.

Cobre:
  1. Config da subclasse (URLs/sigla/log) + herança da base.
  2. Wiring (registry `_ENRICHERS`, fila RQ, auto-enqueue na ingestão, botão
     do dashboard).
  3. Generalização do split de CNJ — o código antigo cravava `.8.26` (TJSP);
     o novo deriva `numeroDigitoAnoUnificado`/`foroNumeroUnificado` por
     segmento, então funciona pra `.8.02` (TJAL) E continua certo pra `.8.26`
     (regressão TJSP).
  4. Fluxo completo `enriquecer()` com HTTP mockado sobre fixtures e-SAJ
     sintetizadas (estrutura fiel ao real). Sem DB e sem Redis —
     `stream.publish` é interceptado.

Fixtures em `tests/fixtures/tjal/` são sintetizadas (não há CNJ real / rede no
CI) mas espelham os seletores reais do e-SAJ já validados no TjspEnricher.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from enrichers.esaj import (
    BaseEsajEnricher,
    EsajEnricherError,
    TjalEnricher,
    TjspEnricher,
    _format_cnj,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjal'


def _resp(text: str, status: int = 200, history=None, url: str = '') -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode('utf-8', 'replace')
    r.encoding = 'utf-8'
    r.url = url
    r.history = history or []
    return r


def _make_enricher() -> TjalEnricher:
    return TjalEnricher()


# --------------------------- 1. Config ---------------------------

def test_config_endpoints_e_sigla():
    e = _make_enricher()
    assert e.TRIBUNAL_SIGLA == 'TJAL'
    assert e.BASE_URL == 'https://www2.tjal.jus.br'
    assert e.OPEN_URL == 'https://www2.tjal.jus.br/cpopg/open.do'
    assert e.SEARCH_URL == 'https://www2.tjal.jus.br/cpopg/search.do'
    assert e.LOG_NAME == 'voyager.enrichers.tjal'


def test_e_subclasse_de_base_esaj():
    """Toda a lógica vem da base — a subclasse é só config."""
    assert issubclass(TjalEnricher, BaseEsajEnricher)
    assert issubclass(TjspEnricher, BaseEsajEnricher)


def test_construtor_rejeita_se_falta_config():
    """Sanidade da base: instanciar sem BASE_URL/SIGLA deve quebrar."""
    class Incompleto(BaseEsajEnricher):
        pass
    with pytest.raises(NotImplementedError):
        Incompleto()


# --------------------------- 2. Wiring ---------------------------

def test_registry_em_enrichers_jobs():
    from enrichers.jobs import _ENRICHERS, queue_for
    assert _ENRICHERS['TJAL'] is TjalEnricher
    assert queue_for('TJAL') == 'enrich_tjal'


def test_fila_rq_configurada():
    from django.conf import settings
    assert 'enrich_tjal' in settings.RQ_QUEUES
    assert settings.RQ_QUEUES['enrich_tjal']['DEFAULT_TIMEOUT'] == 600


def test_auto_enqueue_na_ingestao_djen():
    from djen.ingestion import TRIBUNAIS_COM_ENRICHER
    assert 'TJAL' in TRIBUNAIS_COM_ENRICHER


def test_botao_enriquecer_e_registry_driven():
    """O botão "Atualizar dados públicos" é derivado de `_ENRICHERS` (via
    context `pode_enriquecer` na view), não de um or-chain hardcoded. Logo
    qualquer tribunal no registry ganha o botão automaticamente — TJAL está
    no registry (test_registry_em_enrichers_jobs)."""
    from enrichers.jobs import _ENRICHERS
    assert 'TJAL' in _ENRICHERS
    tpl = (Path(__file__).resolve().parents[1]
           / 'dashboard' / 'templates' / 'dashboard' / 'processo_detail.html')
    body = tpl.read_text()
    assert '{% if pode_enriquecer %}' in body
    assert "tribunal_id == 'TJAL'" not in body  # não hardcodar sigla no template


# ----------------- 3. Generalização do split de CNJ -----------------

def test_format_cnj():
    assert _format_cnj('07001234520248020001') == '0700123-45.2024.8.02.0001'
    with pytest.raises(EsajEnricherError):
        _format_cnj('123')


def test_search_params_tjal_deriva_foro_e_ndo():
    """TJAL: .8.02. `foro` = OOOO, `ndo` = NNNNNNN-DD.AAAA."""
    e = _make_enricher()
    cnj = _format_cnj('07001234520248020001')  # 0700123-45.2024.8.02.0001
    params = e._build_search_params(cnj)
    assert params['numeroDigitoAnoUnificado'] == '0700123-45.2024'
    assert params['foroNumeroUnificado'] == '0001'
    assert params['dadosConsulta.valorConsultaNuUnificado'] == cnj
    assert params['cbPesquisa'] == 'NUMPROC'


def test_search_params_regressao_tjsp_8_26():
    """Regressão: o split generalizado tem que dar EXATAMENTE o que o antigo
    `.split('.8.26')` dava pro TJSP."""
    e = TjspEnricher()
    cnj = _format_cnj('10000005020238260100')  # 1000000-50.2023.8.26.0100
    params = e._build_search_params(cnj)
    assert params['numeroDigitoAnoUnificado'] == '1000000-50.2023'
    assert params['foroNumeroUnificado'] == '0100'


# --------------------------- 4. Fluxo enriquecer() ---------------------------

@pytest.fixture
def processo():
    return SimpleNamespace(
        pk=77, tribunal_id='TJAL',
        numero_cnj='07001234520248020001',
    )


def _patch_session(enricher, *responses):
    """Substitui `enricher.session.get` por uma fila de respostas em ordem.
    1ª chamada = open.do (sessão); 2ª = search.do."""
    queue = list(responses)

    def fake_get(url, **kw):  # noqa: ARG001
        assert queue, 'enricher fez mais GETs do que o esperado'
        return queue.pop(0)

    return patch.object(enricher.session, 'get', side_effect=fake_get)


def _patch_publish():
    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    return captured, patch('enrichers.esaj.stream.publish', side_effect=fake_publish)


def test_enriquecer_ok_extrai_dados_e_partes(processo):
    open_pg = _resp('<html>ok</html>')
    show = _resp((FIXTURES / 'show.html').read_text(),
                 history=[_resp('', status=302)],
                 url='https://www2.tjal.jus.br/cpopg/show.do?processo.codigo=ABC')

    e = _make_enricher()
    captured, cm_pub = _patch_publish()
    with _patch_session(e, open_pg, show), cm_pub:
        result = e.enriquecer(processo)

    # Retorno síncrono
    assert result['cnj'] == processo.numero_cnj
    assert result['status'] == 'ok'
    assert result['partes_total'] >= 2

    # 1 payload OK no stream
    assert len(captured) == 1
    payload = captured[0]
    assert payload['status'] == 'ok'
    assert payload['process_id'] == 77
    assert payload['tribunal'] == 'TJAL'
    assert payload['numero_cnj'] == processo.numero_cnj

    # dados
    dados = payload['dados']
    assert dados['classe'] == 'Procedimento Comum Cível'
    assert dados['assunto'] == 'Indenização por Dano Moral'
    assert 'Maceió' in dados['orgao_julgador']
    assert '3ª Vara Cível' in dados['orgao_julgador']
    assert dados['juizo'] == '3ª Vara Cível da Capital'
    assert dados['data_autuacao'].startswith('12/03/2024')
    assert 'R$ 30.000,00' in dados['valor_causa']

    # polos: Reqte→ativo, Reqdo→passivo
    ativo = payload['partes']['ativo']
    passivo = payload['partes']['passivo']
    assert any('MARIA DA SILVA SANTOS' in p['nome'] for p in ativo)
    assert any('BANCO XPTO' in p['nome'] for p in passivo)

    # e-SAJ público mascara doc → documento vazio (limitação aceita, igual TJSP)
    maria = next(p for p in ativo if 'MARIA' in p['nome'])
    assert maria['documento'] == ''

    # advogados com OAB capturados
    todos = [p for polo in payload['partes'].values() for p in polo]
    advs = [p for p in todos if p['oab']]
    assert any(a['oab'] == 'AL12345' and 'João Carlos Pereira' in a['nome'] for a in advs)
    assert any(a['oab'] == 'AL67890' and 'Ana Beatriz Lima' in a['nome'] for a in advs)
    assert all(a['tipo'] == 'advogado' for a in advs)


def test_enriquecer_nao_encontrado_emite_payload(processo):
    open_pg = _resp('<html>ok</html>')
    form = _resp((FIXTURES / 'search_form.html').read_text())  # sem history, tem formConsulta

    e = _make_enricher()
    captured, cm_pub = _patch_publish()
    with _patch_session(e, open_pg, form), cm_pub:
        result = e.enriquecer(processo)

    assert result['status'] == 'nao_encontrado'
    assert len(captured) == 1
    payload = captured[0]
    assert payload['status'] == 'nao_encontrado'
    assert payload['tribunal'] == 'TJAL'
    assert 'dados' not in payload
    assert 'partes' not in payload


def test_enriquecer_rejeita_tribunal_diferente():
    proc = SimpleNamespace(pk=1, tribunal_id='TRF1', numero_cnj='x')
    with pytest.raises(EsajEnricherError):
        _make_enricher().enriquecer(proc)
