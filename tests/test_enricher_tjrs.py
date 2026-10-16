"""Enricher e busca por parte do TJRS (consulta processual pública, REST + ALTCHA).

As fixtures em `tests/fixtures/tjrs/` são respostas REAIS da API
`consulta-processual-service.tjrs.jus.br` capturadas em 15/10/2026 dos hosts do
Voyager, com nomes de pessoas trocados (MARIA EXEMPLO DA SILVA etc.) e as
chaves dos links de documento zeradas. Entes públicos ficam com o nome real.

Sem rede e sem banco: a sessão HTTP é um dublê que responde pela URL, e o
`stream.publish` é interceptado.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from enrichers.busca.base import ADVOGADO, DOCUMENTO, NOME, OAB, CriterioIndisponivel
from enrichers.busca.registry import CATALOGO, buscador
from enrichers.busca.rest import BuscaTjrs
from enrichers.busca.rest_parser import parse_tjrs_nomes, parse_tjrs_processos
from enrichers.fingerprints import (
    altcha_formulario,
    comarca_tjrs,
    credencial_tjrs,
    resolver_altcha,
)
from enrichers.tjrs import TjrsEnricher, TjrsEnricherError, assunto_folha

FIX = Path(__file__).parent / 'fixtures' / 'tjrs'


def _json(nome: str) -> dict:
    return json.loads((FIX / nome).read_text(encoding='utf-8'))


def _resp(corpo, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = (json.dumps(corpo) if not isinstance(corpo, bytes | str) else corpo)
    if isinstance(r._content, str):
        r._content = r._content.encode()
    r.encoding = 'utf-8'
    return r


def _desafio(numero: int = 1234, salt: str = 'abc123?expires=1792107308') -> dict:
    return {'algorithm': 'SHA-256', 'maxnumber': 5000, 'salt': salt,
            'challenge': hashlib.sha256(f'{salt}{numero}'.encode()).hexdigest(),
            'signature': 'assinatura-do-servidor'}


class SessaoFalsa:
    """Responde por caminho. `api` é uma lista de respostas consumidas em ordem
    pelas chamadas à API autenticada (não às de credencial)."""

    def __init__(self, api: list, usernames=('ChaAnon_64515858602004',)):
        self.api = list(api)
        self.usernames = list(usernames)
        self.headers = {}
        self.chamadas: list[tuple[str, dict, dict]] = []
        self.submits = 0

    def get(self, url, params=None, headers=None, proxies=None, timeout=None):
        self.chamadas.append((url, params or {}, headers or {}))
        if url.endswith('/public/auth/token'):
            return _resp(_desafio())
        return self.api.pop(0)

    def post(self, url, files=None, proxies=None, timeout=None):
        assert url.endswith('/public/auth/submit')
        enviado = json.loads(base64.b64decode(files['altcha'][1]))
        assert enviado['number'] == 1234
        u = self.usernames[min(self.submits, len(self.usernames) - 1)]
        self.submits += 1
        return _resp({'success': True, 'username': u, 'queries': 7})


def _enricher(api: list, **kw) -> TjrsEnricher:
    e = TjrsEnricher(pool=MagicMock(), prefer_cortex=False)
    e.session = SessaoFalsa(api, **kw)
    e._next_proxy = lambda exclude: 'http://proxy:1'
    return e


@pytest.fixture(autouse=True)
def _sem_cortex():
    with patch('enrichers.tjrs.cortex_proxy_url', return_value=None):
        yield


# ─────────────────────────── credencial ALTCHA ───────────────────────────

def test_resolver_altcha_acha_o_numero():
    sol = resolver_altcha(_desafio(4321))
    assert sol['number'] == 4321
    assert set(sol) == {'algorithm', 'challenge', 'number', 'salt', 'signature'}


def test_resolver_altcha_sem_solucao_levanta():
    d = _desafio(1)
    d['challenge'] = '0' * 64
    with pytest.raises(ValueError):
        resolver_altcha(d)


def test_altcha_formulario_e_btoa_do_json_compacto():
    sol = {'algorithm': 'SHA-256', 'challenge': 'c', 'number': 1, 'salt': 's', 'signature': 'x'}
    assert json.loads(base64.b64decode(altcha_formulario(sol))) == sol
    assert b' ' not in base64.b64decode(altcha_formulario(sol))


def test_credencial_identica_a_da_spa():
    """Vetor tirado do console da própria SPA (Chromium, 16/10/2026): o
    interceptor registrou a credencial que ELA calculou para este usuário."""
    assert credencial_tjrs('ChaAnon_89605450504550') == (
        'Q2hhQW5vbl84OTYwNTQ1MDUwNDU1MDo2MmQ1MTM3MzQ2NmNlY2ZiODc5NzUzNDNjZTI1'
        'Y2I0NDA0YTgzMTllYzJmOGEwZjY3ZTNkNmQxYTUyZjNiYzRk')


@pytest.mark.parametrize('cnj,esperado', [
    ('5232029-64.2026.8.21.0001', '1'),
    ('5001041-96.2026.8.21.7000', '700'),     # 2º grau
    ('5000001-30.2025.8.21.9000', '710'),     # Turmas Recursais
    ('5001665-17.2023.8.21.0125', '125'),
    ('lixo', ''),
])
def test_comarca_tjrs(cnj, esperado):
    assert comarca_tjrs(cnj) == esperado


def test_assunto_folha_pega_o_primeiro_da_hierarquia():
    assert assunto_folha('Licença-Prêmio, Licenças / Afastamentos, Servidor Público '
                         'Civil, DIREITO ADMINISTRATIVO') == 'Licença-Prêmio'
    assert assunto_folha('Precatório') == 'Precatório'
    assert assunto_folha('') == ''


# ─────────────────────────── parsing da ficha ───────────────────────────

def test_ficha_cumprimento_rpv():
    e = TjrsEnricher(pool=MagicMock())
    item = _json('consulta_processo_cumprimento_rpv.json')['data'][0]
    dados = e.extrair_dados(item)
    assert dados == {
        'classe': 'CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA',
        'assunto': 'Licença-Prêmio',
        'orgao_julgador': '2º Juízo do 2º Juizado Especial da Fazenda Pública do '
                          'Foro Central da Comarca de Porto Alegre',
        'data_autuacao': '12/08/2026',
        'segredo_justica': False,
    }
    assert 'valor_causa' not in dados          # a API pública não publica
    partes = e.extrair_partes(item)
    assert [(p['nome'], p['papel']) for p in partes['ativo']] == [
        ('MARIA EXEMPLO DA SILVA', 'EXEQUENTE')]
    assert [(p['nome'], p['papel']) for p in partes['passivo']] == [
        ('ESTADO DO RIO GRANDE DO SUL', 'EXECUTADO')]
    # eproc: advogados vêm {nome: null, oab: null} → nenhum representante
    assert partes['ativo'][0]['representantes'] == []
    assert partes['ativo'][0]['documento'] == ''


def test_ficha_precatorio_2g_traz_relator_como_juizo():
    e = TjrsEnricher(pool=MagicMock())
    item = _json('consulta_processo_precatorio_2g.json')['data'][0]
    dados = e.extrair_dados(item)
    assert dados['classe'] == 'Precatório'
    assert dados['juizo'] == 'DESEMBARGADOR EXEMPLO'
    assert dados['assunto'] == 'Pagamento Atrasado / Correção Monetária'
    partes = e.extrair_partes(item)
    assert partes['ativo'][0]['papel'] == 'REQUERENTE'
    assert partes['passivo'][0]['nome'] == 'MUNICÍPIO DE CRUZ ALTA / RS'


def test_polo_de_papel_desconhecido_vai_para_outros():
    e = TjrsEnricher(pool=MagicMock())
    item = {'partes': {'parte': [
        {'nome': 'MINISTÉRIO PÚBLICO DO ESTADO DO RIO GRANDE DO SUL',
         'descricaoTipo': 'MINISTÉRIO PÚBLICO', 'advogados': {'advogado': []}},
        {'nome': 'FULANO', 'descricaoTipo': 'RECORRENTE',
         'advogados': {'advogado': [{'nome': 'ADVOGADA EXEMPLO', 'oab': 'RS083749'}]}},
    ]}}
    partes = e.extrair_partes(item)
    assert partes['outros'][0]['papel'] == 'MINISTÉRIO PÚBLICO'
    rep = partes['ativo'][0]['representantes'][0]
    assert rep['oab'] == 'RS83749' and rep['papel'] == 'ADVOGADO'


# ─────────────────────────── fluxo HTTP ───────────────────────────

def _processo(cnj='5232029-64.2026.8.21.0001'):
    p = MagicMock()
    p.pk, p.tribunal_id, p.numero_cnj = 130243074, 'TJRS', cnj
    return p


def test_enriquecer_ok_publica_ficha_com_credencial_basic():
    e = _enricher([_resp(_json('consulta_processo_cumprimento_rpv.json'))])
    with patch('enrichers.tjrs.stream.publish') as pub:
        r = e.enriquecer(_processo())
    assert r['status'] == 'ok'
    evento = pub.call_args[0][0]
    assert evento['status'] == 'ok'
    assert evento['dados']['classe'].startswith('CUMPRIMENTO DE SENTENÇA')
    url, params, headers = e.session.chamadas[-1]
    assert url.endswith('/api/consulta-service/v1/consultaProcesso')
    assert params == {'numeroProcesso': '52320296420268210001', 'codComarca': '1'}
    assert headers['Authorization'] == 'Basic ' + credencial_tjrs('ChaAnon_64515858602004')


def test_404_e_nao_encontrado():
    e = _enricher([_resp(_json('nao_encontrado_404.json'), 404)])
    with patch('enrichers.tjrs.stream.publish') as pub:
        r = e.enriquecer(_processo())
    assert r['status'] == 'nao_encontrado'
    assert pub.call_args[0][0]['status'] == 'nao_encontrado'


def test_429_renova_credencial_e_segue():
    corpo_429 = {'data': None, 'exceptionKey': 429,
                 'messages': ['Limite de chamadas atingido, por favor, aguarde']}
    e = _enricher([_resp(corpo_429, 429), _resp(_json('consulta_processo_cumprimento_rpv.json'))],
                  usernames=('ChaAnon_1', 'ChaAnon_2'))
    with patch('enrichers.tjrs.stream.publish'):
        r = e.enriquecer(_processo())
    assert r['status'] == 'ok'
    assert e.session.submits == 2
    assert e.session.chamadas[-1][2]['Authorization'] == 'Basic ' + credencial_tjrs('ChaAnon_2')


def test_credencial_renovada_antes_de_esgotar_as_7():
    fichas = [_resp(_json('consulta_processo_cumprimento_rpv.json')) for _ in range(8)]
    e = _enricher(fichas)
    with patch('enrichers.tjrs.stream.publish'):
        for _ in range(8):
            assert e.enriquecer(_processo())['status'] == 'ok'
    assert e.session.submits == 2       # 7 consultas na 1ª, a 8ª já pede outra


def test_401_seguido_vira_erro_retentavel_nunca_nao_encontrado():
    """16/10/2026 00:00 UTC: a API recusou toda credencial (até a da SPA)."""
    e = _enricher([_resp(b'', 401) for _ in range(5)])
    with patch('enrichers.tjrs.stream.publish') as pub:
        r = e.enriquecer(_processo())
    assert r['status'] == 'erro'
    assert pub.call_args[0][0]['status'] == 'erro'
    assert e.session.submits == TjrsEnricher.MAX_CREDENCIAIS_RECUSADAS


def test_200_sem_o_processo_pedido_e_erro():
    e = _enricher([_resp(_json('consulta_processo_precatorio_2g.json'))])
    with patch('enrichers.tjrs.stream.publish'):
        r = e.enriquecer(_processo('5232029-64.2026.8.21.0001'))
    assert r['status'] == 'erro'


def test_prefixo_8_e_fora_da_fonte_sem_requisicao():
    e = _enricher([])
    with patch('enrichers.tjrs.stream.publish') as pub, \
            patch('enrichers.jobs.registrar_fora_do_esaj') as conta:
        r = e.enriquecer(_processo('8000060-71.2026.8.21.0030'))
    assert r['status'] == 'nao_encontrado' and r['requisicoes'] == 0
    conta.assert_called_once_with('TJRS', 'tjrs_prefixo_8_fora_da_consulta')
    assert pub.call_args[0][0]['status'] == 'nao_encontrado'
    assert e.session.chamadas == []
    assert TjrsEnricher.fora_da_fonte('5232029-64.2026.8.21.0001') is None


def test_registrado_no_refill():
    from enrichers.jobs import _ENRICHERS
    assert _ENRICHERS['TJRS'] is TjrsEnricher


# ─────────────────────────── busca por parte ───────────────────────────

def test_catalogo_so_declara_o_medido():
    fonte = CATALOGO['TJRS']
    assert fonte.criterios == frozenset({NOME, OAB})
    assert fonte.criterios_medidos == frozenset({NOME, OAB})
    assert fonte.verificado_em == '2026-10-15'


def test_parse_processos_deduplica_e_monta_link():
    itens = parse_tjrs_processos(_json('eproc_processos_por_oab_recorte.json'))
    numeros = [i.numero_cnj for i in itens]
    assert len(numeros) == len(set(numeros))
    assert itens[0].numero_cnj == '5041656-18.2022.8.21.0001'
    assert itens[0].tribunal == 'TJRS'
    assert itens[0].distribuicao == '07/03/2003'
    assert 'numeroProcesso=50416561820228210001&codComarca=1' in itens[0].url_fonte


def test_parse_nomes_so_grafia_igual():
    corpo = _json('eproc_busca_nomes_ente.json')
    partes = parse_tjrs_nomes(corpo, 'municipio de cruz alta')
    assert [p['nome'] for p in partes] == ['MUNICÍPIO DE CRUZ ALTA']
    partes = parse_tjrs_nomes(_json('eproc_busca_nomes_parte.json'), 'Maria Exemplo da Silva')
    assert partes[0]['documento_mascarado'] == '123.4**.***-**'


def _busca(api):
    b = BuscaTjrs.__new__(BuscaTjrs)
    b.enricher = _enricher(api)
    b.TRIBUNAL = 'TJRS'
    b.PAUSA_ENTRE_PAGINAS_S = 0
    return b


def test_busca_nome_dois_passos():
    b = _busca([
        _resp(_json('eproc_busca_nomes_parte.json')),
        _resp(_json('eproc_processos_por_cod_parte.json')),           # situação A
        _resp(_json('eproc_processos_por_cod_parte.json')),           # situação B (repetidos)
    ])
    paginas = list(b.paginar(NOME, 'MARIA EXEMPLO DA SILVA'))
    cnjs = [i.numero_cnj for p in paginas for i in p.itens]
    assert '5232029-64.2026.8.21.0001' in cnjs
    assert len(cnjs) == len(set(cnjs))
    chamadas = [c for c in b.enricher.session.chamadas if '/v1/' in c[0]]
    assert chamadas[1][1]['codParte1g'] == '10000000000000000000000000001'
    assert {chamadas[1][1]['situacao'], chamadas[2][1]['situacao']} == {'A', 'B'}


def test_busca_oab_janelas_ate_404():
    p1 = _json('eproc_processos_por_oab_recorte.json')
    fim = _json('nao_encontrado_404.json')
    b = _busca([_resp(p1), _resp(fim, 404), _resp(fim, 404)])
    paginas = list(b.paginar(OAB, 'RS083749'))
    assert len(paginas) == 1
    params = [c[1] for c in b.enricher.session.chamadas if '/v1/' in c[0]]
    assert params[0]['numeroOAB'] == '83749' and params[0]['ufOAB'] == 'RS'
    assert (params[0]['registroInicial'], params[0]['registroFinal']) == ('1', '2000')
    assert params[1]['registroInicial'] == '2001'
    assert params[2]['situacao'] == 'B'


@pytest.mark.parametrize('criterio', [DOCUMENTO, ADVOGADO])
def test_busca_recusa_criterio_que_a_fonte_nao_tem(criterio):
    b = _busca([])
    with pytest.raises(CriterioIndisponivel):
        list(b.paginar(criterio, 'x'))


def test_buscador_fabrica_tjrs():
    with patch('enrichers.tjrs.ProxyScrapePool.singleton', return_value=MagicMock()):
        assert isinstance(buscador('TJRS'), BuscaTjrs)


def test_erro_da_fonte_vira_fonte_indisponivel():
    from enrichers.busca.base import FonteIndisponivel
    b = _busca([_resp(b'', 401) for _ in range(5)])
    with pytest.raises(FonteIndisponivel):
        list(b.paginar(OAB, 'RS1'))
    assert isinstance(TjrsEnricherError('x'), Exception)


@pytest.mark.django_db
def test_advogados_do_djen_sobrevivem_ao_enriquecimento():
    """O drainer apaga e regrava as partes; os advogados que vieram do DJEN
    (a consulta pública não tem nenhum) precisam voltar no evento."""
    import datetime as dt

    from enrichers import stream as _stream
    from enrichers.drainer import apply_event
    from tribunals.models import Parte, Process as Proc, ProcessoParte, Tribunal

    t, _ = Tribunal.objects.get_or_create(sigla='TJRS', defaults={'nome': 'TJ RS',
                                                                   'sigla_djen': 'TJRS'})
    proc = Proc.objects.create(tribunal=t, numero_cnj='5232029-64.2026.8.21.0001')
    adv = Parte.objects.create(nome='ADVOGADA EXEMPLO', oab='RS83749', tipo='advogado')
    autora = Parte.objects.create(nome='MARIA EXEMPLO DA SILVA', tipo='desconhecido')
    ProcessoParte.objects.create(processo=proc, parte=adv, polo='outros', papel='', fonte='djen')
    ProcessoParte.objects.create(processo=proc, parte=autora, polo='ativo', papel='', fonte='djen')

    e = TjrsEnricher(pool=MagicMock())
    item = _json('consulta_processo_cumprimento_rpv.json')['data'][0]
    partes = e.somar_partes_de_terceiros(proc.pk, e.extrair_partes(item))
    apply_event(_stream.build_ok_payload(
        process_id=proc.pk, tribunal='TJRS', numero_cnj=proc.numero_cnj,
        scraped_at=dt.datetime.now(dt.UTC).isoformat(),
        dados=e.extrair_dados(item), partes=partes))

    linhas = {(pp.parte.nome, pp.polo, pp.papel, pp.fonte)
              for pp in ProcessoParte.objects.filter(processo=proc).select_related('parte')}
    assert ('ADVOGADA EXEMPLO', 'outros', '', 'djen') in linhas
    # A autora agora vem da consulta (com papel), sem duplicar a linha do DJEN.
    assert ('MARIA EXEMPLO DA SILVA', 'ativo', 'EXEQUENTE', None) in linhas
    assert not any(n == 'MARIA EXEMPLO DA SILVA' and f == 'djen' for n, _, _, f in linhas)
    assert ('ESTADO DO RIO GRANDE DO SUL', 'passivo', 'EXECUTADO', None) in linhas
