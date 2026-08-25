"""e-SAJ: cinco páginas com HTTP 200, e o código tratava três como a mesma.

Incidente (auditoria 24-25/08/2026 + sonda ao vivo de 25/08/2026):

  · `segredo_justica = true` em **0 de 91.638.494** documentos do índice
    (`_count`, não `exists`) e 0 em 120.000 processos amostrados no banco.
    Para 102 M de processos afirmávamos "não corre em segredo" sem nunca ter
    perguntado — o `default=False` do BooleanField virou uma AFIRMAÇÃO.
  · **17,5% dos `ok` do TJSP não tinham NENHUMA parte** (356 de 2.034 `ok` na
    amostra A) ≈ **302 k processos** rotulados "enriquecido" sobre um cadastro
    vazio. `run` verde sobre resposta vazia é a assinatura de erro que esta
    casa mais paga.

A sonda ao vivo (62 requisições a `esaj.tjsp.jus.br`, 3 s entre elas, amostra
de semente 20260825) achou as DUAS causas — e as duas são o mesmo defeito,
teste de SUBSTRING numa página inteira:

  1. `'classeProcesso' in resp.text` (o teste de "achou") casa a **classe CSS**
     `<div class="classeProcesso">` da página de LISTA. A lista virava
     "detalhe", `select_one('#classeProcesso')` não achava nada e o processo
     era gravado `ok` com o cadastro inteiro vazio. 6 das 62 respostas eram
     lista — 4 das 8 do estrato de 2º grau.
  2. A frase "É necessário informar uma senha para acessar processo em segredo
     de justiça" está dentro de `<form id="popupSenha" style="display: none;">`
     em **TODA** página de detalhe do e-SAJ: 36 das 62 páginas baixadas a
     contêm e **33 dessas 36 são detalhes normais COM partes**. Um detector por
     frase marcaria segredo em processo bom e o apagaria do funil.

Todas as fixtures aqui são **HTML REAL** capturado na sonda (`tests/fixtures/
tjsp/`), nunca sintetizado — a fixture do TJAL que "provava" a OAB é sintética
e foi por isso que o Achado 6 passou 3 meses despercebido.

Controle positivo E negativo em todo teste: verificação com input vazio
reporta SUCESSO.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from enrichers.esaj import (
    DESFECHO_AMBIGUO,
    DESFECHO_DETALHE,
    DESFECHO_LISTA,
    DESFECHO_NAO_EXISTE,
    DESFECHO_SEGREDO,
    TjspEnricher,
    classificar_resposta,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjsp'

# (arquivo, desfecho esperado, o que é na vida real)
PAGINAS_REAIS = [
    ('esaj_cpopg_segredo.html', DESFECHO_SEGREDO,
     'página de senha do 1º grau, 32.963 bytes na captura'),
    ('esaj_cpopg_nao_existe.html', DESFECHO_NAO_EXISTE,
     '"Não existem informações disponíveis" — CNJ do eproc, fora do e-SAJ'),
    ('esaj_cpopg_detalhe.html', DESFECHO_DETALHE,
     'cadastro completo (Reintegração / Manutenção de Posse) com partes'),
    ('esaj_cpopg_detalhe_sem_classe.html', DESFECHO_DETALHE,
     'cadastro REAL sem #classeProcesso (Carta Precatória) — tem partes'),
    ('esaj_cpopg_lista.html', DESFECHO_LISTA,
     'vários resultados no 1º grau (#listagemDeProcessos)'),
    ('esaj_cposg_lista.html', DESFECHO_LISTA,
     '"Selecione o processo" do 2º grau (#modalIncidentes)'),
]


def _ler(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding='utf-8', errors='replace')


# --------------------- 1. Classificador, página a página ---------------------

@pytest.mark.parametrize('nome,esperado,descricao', PAGINAS_REAIS)
def test_classifica_cada_pagina_real(nome, esperado, descricao):
    assert classificar_resposta(_ler(nome)) == esperado, descricao


def test_frase_da_senha_esta_em_pagina_de_detalhe_normal():
    """CONTROLE NEGATIVO do detector de segredo — a razão de ele ser
    estrutural. Se algum dia alguém trocar a regra por um `in` da frase, este
    teste falha e mostra por quê: a frase está no popup escondido de toda
    página de detalhe."""
    frase = 'informar uma senha para acessar processo em segredo de justi'
    detalhe = _ler('esaj_cpopg_detalhe.html')
    assert frase in detalhe, 'a fixture real perdeu o popup — reveja o marcador'
    assert 'id="popupSenha"' in detalhe
    assert classificar_resposta(detalhe) == DESFECHO_DETALHE


def test_classe_css_classeprocesso_esta_na_pagina_de_lista():
    """CONTROLE NEGATIVO do teste de "achou". `'classeProcesso' in html` é
    verdadeiro na LISTA (classe CSS), e era assim que a lista virava um `ok`
    de cadastro vazio."""
    lista = _ler('esaj_cpopg_lista.html')
    assert 'classeProcesso' in lista          # o substring ingênuo casa...
    assert 'id="classeProcesso"' not in lista  # ...mas o id não existe
    assert classificar_resposta(lista) == DESFECHO_LISTA


def test_segredo_exige_ausencia_de_todo_campo_do_cadastro():
    """O detector é conservador: falso positivo apaga processo bom do funil."""
    segredo = _ler('esaj_cpopg_segredo.html')
    for marca in ('id="classeProcesso"', 'id="assuntoProcesso"',
                  'id="foroProcesso"', 'id="varaProcesso"',
                  'id="numeroProcesso"', 'id="tablePartesPrincipais"'):
        assert marca not in segredo, f'{marca} na página de senha — regra quebrada'
    assert 'id="containerDadosPrincipaisProcesso"' in segredo
    assert 'id="popupSenha"' in segredo


def test_pagina_vazia_nao_e_segredo_nem_detalhe():
    """Verificação com input vazio reporta SUCESSO — aqui não."""
    for vazio in ('', '   ', '<html></html>'):
        assert classificar_resposta(vazio) == DESFECHO_AMBIGUO


def test_ambiguo_continua_ambiguo():
    """Soft-error/throttle do e-SAJ com 200 segue TRANSITÓRIO (re-tentável),
    nunca `nao_encontrado` terminal — foi o bug de 3,25 M presos (2026-07-06)."""
    assert classificar_resposta('<html><body>Servico indisponivel</body></html>') \
        == DESFECHO_AMBIGUO


# --------------------- 2. Fluxo do enricher ---------------------

def _resp(text: str, status: int = 200, history=None, url: str = '') -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode('utf-8', 'replace')
    r.encoding = 'utf-8'
    r.url = url
    r.history = history or []
    return r


def _mock_pool() -> MagicMock:
    pool = MagicMock()
    n = {'i': 0}

    def _get():
        n['i'] += 1
        return f'http://10.0.0.{n["i"]}:8080'

    pool.get.side_effect = _get
    return pool


def _enricher() -> TjspEnricher:
    return TjspEnricher(pool=_mock_pool())


def _patch_publish():
    capturado: list[dict] = []

    def fake(payload, redis_client=None):
        capturado.append(payload)
        return '0-0'

    return capturado, patch('enrichers.esaj.stream.publish', side_effect=fake)


@pytest.fixture
def processo():
    return SimpleNamespace(pk=4242, tribunal_id='TJSP',
                           numero_cnj='10002231820268260456')


def _rodar(enricher, processo, *respostas):
    fila = list(respostas)

    def fake_get(url, **kw):
        assert fila, 'enricher fez mais GETs do que o esperado'
        return fila.pop(0)

    capturado, cm = _patch_publish()
    with patch.object(enricher.session, 'get', side_effect=fake_get), cm:
        return enricher.enriquecer(processo), capturado


def test_segredo_grava_segredo_justica_e_nao_mente_ok(processo):
    """A REGRESSÃO PRINCIPAL: página de senha virava `ok` mudo com cadastro
    vazio (≈ 302 k processos só no TJSP). Agora sai `segredo_justica=True` e
    NENHUM campo inventado."""
    resultado, payloads = _rodar(
        _enricher(), processo,
        _resp('<html>open</html>'),
        _resp(_ler('esaj_cpopg_segredo.html'),
              history=[_resp('', status=302)],
              url='https://esaj.tjsp.jus.br/cpopg/show.do?processo.codigo=X'),
    )
    assert resultado['status'] == 'segredo'
    assert resultado['segredo_justica'] is True
    assert resultado['partes_total'] == 0

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload['status'] == 'ok'          # o drainer só aplica dados no `ok`
    assert payload['dados'] == {'segredo_justica': True}
    assert payload['dados'].get('classe') is None
    assert all(not lista for lista in payload['partes'].values())


def test_detalhe_normal_grava_segredo_false_e_nao_vira_segredo(processo):
    """CONTROLE POSITIVO do caminho bom: página com cadastro segue `ok`, com
    partes, e agora AFIRMA `segredo_justica=False` — porque perguntamos."""
    resultado, payloads = _rodar(
        _enricher(), processo,
        _resp('<html>open</html>'),
        _resp(_ler('esaj_cpopg_detalhe.html'), history=[_resp('', status=302)]),
    )
    assert resultado['status'] == 'ok'
    assert resultado['partes_total'] >= 2
    dados = payloads[0]['dados']
    assert dados['segredo_justica'] is False
    assert dados['classe'] == 'Reintegração / Manutenção de Posse'


def test_detalhe_sem_classe_ainda_e_detalhe(processo):
    """Existe cadastro REAL sem `#classeProcesso` (Carta Precatória Cível).
    Se o "achou" dependesse só da classe, ele viraria segredo — e uma parte
    real sumiria do acervo."""
    resultado, payloads = _rodar(
        _enricher(), processo,
        _resp('<html>open</html>'),
        _resp(_ler('esaj_cpopg_detalhe_sem_classe.html'),
              history=[_resp('', status=302)]),
    )
    assert resultado['status'] == 'ok'
    assert resultado['partes_total'] >= 2
    assert payloads[0]['dados']['segredo_justica'] is False


def test_nao_existe_continua_nao_encontrado(processo):
    """"Não existem informações" é o ÚNICO desfecho terminal do e-SAJ."""
    resultado, payloads = _rodar(
        _enricher(), processo,
        _resp('<html>open</html>'),
        _resp(_ler('esaj_cpopg_nao_existe.html')),
    )
    assert resultado['status'] == 'nao_encontrado'
    assert payloads[0]['status'] == 'nao_encontrado'


def test_lista_1g_segue_o_link_do_nosso_cnj(processo):
    """A lista trazia o dado na tela e nós devolvíamos `erro` depois de queimar
    8 IPs do pool COMPARTILHADO. Agora segue o link do NOSSO número."""
    proc = SimpleNamespace(pk=7, tribunal_id='TJSP',
                           numero_cnj='15009368420268260536')  # o CNJ da fixture
    urls: list[str] = []
    fila = [
        _resp('<html>open</html>'),
        _resp(_ler('esaj_cpopg_lista.html')),
        _resp(_ler('esaj_cpopg_detalhe.html')),
    ]

    def fake_get(url, **kw):
        urls.append(url)
        return fila.pop(0)

    e = _enricher()
    capturado, cm = _patch_publish()
    with patch.object(e.session, 'get', side_effect=fake_get), cm:
        resultado = e.enriquecer(proc)

    assert resultado['status'] == 'ok'
    assert any('show.do' in u and 'processo.codigo=EW000YSQ10000' in u
               for u in urls), urls
    assert capturado[0]['status'] == 'ok'


def test_lista_2g_monta_a_url_pelo_radio_selecionado():
    """2º grau usa `#modalIncidentes` com rádio `processoSelecionado`; a URL
    se monta com `processo.codigo` + `processo.numero` (conferido ao vivo:
    200 com 120.429 bytes, com classe e partes)."""
    e = _enricher()
    url = e._extrair_link_lista(_ler('esaj_cposg_lista.html'),
                                '2327387-09.2025.8.26.0000', 'cposg')
    assert url is not None
    assert '/cposg/show.do' in url
    assert 'processo.codigo=RI0093NLD0000' in url
    assert 'processo.numero=2327387-09.2025.8.26.0000' in url


def test_lista_nao_pega_o_primeiro_da_lista_a_esmo():
    """A lista mistura o processo com seus incidentes/recursos. Casar por
    posição traria o cadastro de OUTRO processo — inventar dado é pior que não
    ter (regra nº 6)."""
    e = _enricher()
    assert e._extrair_link_lista(_ler('esaj_cposg_lista.html'),
                                 '9999999-99.2099.8.26.0000', 'cposg') is None
    assert e._extrair_link_lista(_ler('esaj_cpopg_lista.html'),
                                 '9999999-99.2099.8.26.0536', 'cpopg') is None


def test_lista_sem_link_para_o_cnj_vira_erro_e_nao_nao_encontrado(processo):
    """Sem link para o nosso número, o desfecho é TRANSITÓRIO — jamais um
    `nao_encontrado` terminal em cima de uma página que nem era nossa."""
    def fake_get(url, **kw):
        if url.endswith('open.do'):
            return _resp('<html>open</html>')
        return _resp(_ler('esaj_cpopg_lista.html'))  # lista de OUTRO CNJ

    e = _enricher()
    capturado, cm = _patch_publish()
    with patch.object(e.session, 'get', side_effect=fake_get), cm:
        resultado = e.enriquecer(processo)

    assert resultado['status'] == 'erro'
    assert capturado[0]['status'] == 'erro'
