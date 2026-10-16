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
    CHAVES_TJRS_CONHECIDAS,
    BundleTjrsMudouError,
    altcha_formulario,
    bundle_principal_tjrs,
    chaves_do_bundle_tjrs,
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
def _sem_cortex(settings):
    # Pool de credenciais e chaves do bundle vivem no cache: cada teste com o seu.
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                   'LOCATION': 'tjrs-testes'}}
    import django_rq
    from django.core.cache import cache
    cache.clear()
    conn = django_rq.get_connection('default')

    def limpar_pool():
        for k in conn.scan_iter('tjrs:cred:*'):
            conn.delete(k)
    limpar_pool()
    with patch('enrichers.tjrs.cortex_proxy_url', return_value=None), \
            patch('enrichers.tjrs.chaves_tjrs', return_value=CHAVES_TJRS_CONHECIDAS):
        yield
    cache.clear()
    limpar_pool()


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


def test_credencial_do_bundle_de_15_10_identica_a_da_spa():
    """Vetor do console da própria SPA (Chromium com o bundle `main.3ce0…`,
    constantes 60029/92120): o interceptor registrou a credencial que ELA
    calculou para este usuário."""
    assert credencial_tjrs('ChaAnon_89605450504550', (60029, 92120)) == (
        'Q2hhQW5vbl84OTYwNTQ1MDUwNDU1MDo2MmQ1MTM3MzQ2NmNlY2ZiODc5NzUzNDNjZTI1'
        'Y2I0NDA0YTgzMTllYzJmOGEwZjY3ZTNkNmQxYTUyZjNiYzRk')


def test_credencial_do_bundle_vivo_aceita_pela_api():
    """Vetor de 16/10/2026 com o bundle `main.667e…` (60029/92125): esta
    credencial levou **200** em `/consultaProcesso`; a mesma com 92120, 401."""
    usuario = 'ChaAnon_14336925147968'
    assert credencial_tjrs(usuario, (60029, 92125)) == (
        'Q2hhQW5vbl8xNDMzNjkyNTE0Nzk2ODo5MzAyMjgzOTlhMjM4YTJiYzczMDU2Y2QwZmQ2NGM0MDU2'
        'ODllNmRmNmQ2MTZlM2E2ODhjZWQxZjJjOGVkZWIw')
    assert credencial_tjrs(usuario, (60029, 92120)) != credencial_tjrs(usuario)
    assert CHAVES_TJRS_CONHECIDAS == (60029, 92125)


@pytest.mark.parametrize('arquivo,esperado', [
    ('spa_main_obfuscation_2026-10-15.js.txt', (60029, 92120)),
    ('spa_main_obfuscation_2026-10-16.js.txt', (60029, 92125)),
])
def test_chaves_lidas_do_trecho_real_do_bundle(arquivo, esperado):
    assert chaves_do_bundle_tjrs((FIX / arquivo).read_text()) == esperado


def test_bundle_com_formula_diferente_nao_e_lido_no_chute():
    trecho = (FIX / 'spa_main_obfuscation_2026-10-16.js.txt').read_text()
    with pytest.raises(BundleTjrsMudouError):
        chaves_do_bundle_tjrs(trecho.replace('"@"+x.toString()', '"#"+x.toString()'))
    with pytest.raises(BundleTjrsMudouError):
        chaves_do_bundle_tjrs(trecho + 'y=oe%BigInt(7)+BigInt(8)')
    with pytest.raises(BundleTjrsMudouError):
        chaves_do_bundle_tjrs('nada aqui')


def test_index_da_spa_aponta_o_bundle():
    html = (FIX / 'spa_index_scripts_2026-10-16.html.txt').read_text()
    assert bundle_principal_tjrs(html) == 'main.667e4d4a4b94df67.js'
    with pytest.raises(BundleTjrsMudouError):
        bundle_principal_tjrs('<html></html>')


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


def test_401_em_serie_arma_respiro_sem_evento_nem_tentativa():
    """16/10/2026 00:00 UTC: a API recusou toda credencial (até a da SPA).
    Não é o processo — nada de `erro`, nada de `nao_encontrado`: o tribunal
    respira e o processo continua `pendente`."""
    e = _enricher([_resp(b'', 401) for _ in range(5)])
    with patch('enrichers.tjrs.stream.publish') as pub, \
            patch('enrichers.jobs.armar_respiro') as respiro:
        r = e.enriquecer(_processo())
    assert r['status'] == 'adiado'
    pub.assert_not_called()
    respiro.assert_called_once()
    assert respiro.call_args[0][0] == 'TJRS'
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
        _resp(_json('eproc_busca_nomes_parte.json')),                 # partes, ativos
        _resp(_json('eproc_busca_nomes_parte.json')),                 # partes, baixados
        _resp(_json('eproc_processos_por_cod_parte.json')),           # situação A
        _resp(_json('eproc_processos_por_cod_parte.json')),           # situação B (repetidos)
    ])
    paginas = list(b.paginar(NOME, 'MARIA EXEMPLO DA SILVA'))
    cnjs = [i.numero_cnj for p in paginas for i in p.itens]
    assert '5232029-64.2026.8.21.0001' in cnjs
    assert len(cnjs) == len(set(cnjs))
    chamadas = [c for c in b.enricher.session.chamadas if '/v1/' in c[0]]
    assert {chamadas[0][1]['situacao'], chamadas[1][1]['situacao']} == {'A', 'B'}
    assert chamadas[2][1]['codParte1g'] == '10000000000000000000000000001'
    assert {chamadas[2][1]['situacao'], chamadas[3][1]['situacao']} == {'A', 'B'}
    assert paginas[-1].tem_proxima is False


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


# ─────────────────── revisão do PR #27: pool, bundle, busca ───────────────────

def test_pool_reaproveita_a_credencial_entre_instancias():
    """Duas instâncias (duas réplicas) dividem a mesma credencial: 7 consultas,
    1 submit — não 7 submits."""
    fichas = [_resp(_json('consulta_processo_cumprimento_rpv.json')) for _ in range(7)]
    a, b = _enricher(fichas[:4]), _enricher(fichas[4:])
    with patch('enrichers.tjrs.stream.publish'):
        for _ in range(4):
            assert a.enriquecer(_processo())['status'] == 'ok'
        for _ in range(3):
            assert b.enriquecer(_processo())['status'] == 'ok'
    assert a.session.submits + b.session.submits == 1


def _pool_redis():
    import django_rq

    from enrichers.tjrs_credencial import PoolCredenciais
    pool = PoolCredenciais(CHAVES_TJRS_CONHECIDAS, conexao=django_rq.get_connection('default'))
    pool.redis.delete(pool.chave, pool.trava)
    return pool


def test_pool_concorrente_usa_7_por_credencial_e_nunca_mais():
    """16 réplicas x 20 consultas no Redis real. Só uma gera por vez (SET NX);
    as outras releem. Resultado: cada credencial usada 7 vezes (a última,
    menos), nenhuma além disso — antes, sob contenção, cada réplica
    sobrescrevia a atual e jogava fora as consultas dela."""
    import threading
    from collections import Counter

    pool = _pool_redis()
    pool.PASSO_ESPERA_S = 0.005
    usos, geradas, trava = Counter(), [0], threading.Lock()

    def gerar():
        with trava:
            geradas[0] += 1
            n = geradas[0]
        __import__('time').sleep(0.01)          # o PoW/submit não é instantâneo
        return f'cred-{n}', 7

    def consultar():
        for _ in range(20):
            cred = pool.pegar_ou_gerar(gerar)
            with trava:
                usos[cred] += 1

    threads = [threading.Thread(target=consultar) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pool.redis.delete(pool.chave, pool.trava)
    assert sum(usos.values()) == 16 * 20
    assert max(usos.values()) == 7
    cheias = sum(1 for n in usos.values() if n == 7)
    assert cheias >= len(usos) - 1               # só a última pode sobrar
    assert geradas[0] == -(-320 // 7)            # 46: nenhuma credencial jogada fora


def test_pool_pegar_e_atomico_e_nao_recria_contador_sem_ttl():
    pool = _pool_redis()
    assert pool.pegar() is None
    assert not pool.redis.exists(pool.chave)     # o pegar no vazio não cria nada
    pool.publicar('c1', 2)
    assert 0 < pool.redis.ttl(pool.chave) <= 900
    assert pool.pegar() == 'c1'
    assert pool.pegar() is None                  # 2 consultas: 1 de quem gerou + 1
    pool.descartar('outra')                      # descartar outra não mexe na atual
    pool.publicar('c2', 7)
    pool.descartar('c2')
    assert pool.pegar() is None
    pool.redis.delete(pool.chave, pool.trava)


def test_401_em_serie_rele_o_bundle_uma_vez_e_segue_com_as_chaves_novas():
    """O caso de 16/10: a SPA trocou a constante. 3 x 401 com a antiga →
    relê o bundle → chaves novas → 200."""
    e = _enricher([_resp(b'', 401), _resp(b'', 401), _resp(b'', 401),
                   _resp(_json('consulta_processo_cumprimento_rpv.json'))],
                  usernames=('ChaAnon_1', 'ChaAnon_2', 'ChaAnon_3', 'ChaAnon_4'))
    leituras = []

    def chaves(sessao, forcar=False):
        leituras.append(forcar)
        return (60029, 92125) if forcar else (60029, 92120)

    with patch('enrichers.tjrs.chaves_tjrs', side_effect=chaves), \
            patch('enrichers.tjrs.stream.publish') as pub:
        r = e.enriquecer(_processo())
    assert r['status'] == 'ok'
    assert leituras == [False, True]
    assert pub.call_args[0][0]['status'] == 'ok'
    assert e.session.chamadas[-1][2]['Authorization'] == \
        'Basic ' + credencial_tjrs('ChaAnon_4', (60029, 92125))


def test_401_com_bundle_igual_arma_respiro_depois_de_reler_uma_vez():
    e = _enricher([_resp(b'', 401) for _ in range(5)])
    leituras = []
    with patch('enrichers.tjrs.chaves_tjrs',
               side_effect=lambda s, forcar=False: leituras.append(forcar) or (60029, 92125)), \
            patch('enrichers.tjrs.stream.publish') as pub, \
            patch('enrichers.jobs.armar_respiro') as respiro:
        r = e.enriquecer(_processo())
    assert r['status'] == 'adiado'
    assert leituras == [False, True]
    respiro.assert_called_once()
    pub.assert_not_called()


def test_chaves_tjrs_le_index_e_bundle_e_guarda_no_cache():
    from django.core.cache import cache

    from enrichers.tjrs_credencial import chaves_tjrs as chaves_reais
    html = (FIX / 'spa_index_scripts_2026-10-16.html.txt').read_text()
    js = (FIX / 'spa_main_obfuscation_2026-10-16.js.txt').read_text()
    sessao = MagicMock()
    sessao.get.side_effect = lambda url, timeout=None: MagicMock(
        text=js if url.endswith('.js') else html, raise_for_status=lambda: None)
    assert chaves_reais(sessao) == (60029, 92125)
    assert sessao.get.call_args_list[1].args[0].endswith('main.667e4d4a4b94df67.js')
    sessao.get.reset_mock()
    assert chaves_reais(sessao) == (60029, 92125)       # do cache, sem rede
    sessao.get.assert_not_called()
    assert cache.get('tjrs:spa:chaves:ultimo') == [60029, 92125]


def test_chaves_tjrs_bundle_ilegivel_usa_o_ultimo_par():
    from django.core.cache import cache

    from enrichers.tjrs_credencial import chaves_tjrs as chaves_reais
    cache.set('tjrs:spa:chaves:ultimo', [60029, 92125])
    sessao = MagicMock()
    sessao.get.return_value = MagicMock(text='<html>sem bundle</html>', raise_for_status=lambda: None)
    assert chaves_reais(sessao, forcar=True) == (60029, 92125)


def _cod_parte(n, inicio=0):
    return {'data': [{'numeroCNJ': f'5{i:06d}0020268210001', 'numeroCNJFormatado':
                      f'5{i:06d}-00.2026.8.21.0001', 'classeCNJ': 'X',
                      'orgaoJulgador': {'nome': 'Y'}, 'dataDistribuicao': '01/01/2026'}
                     for i in range(inicio, inicio + n)]}


def test_busca_nome_le_todas_as_paginas_de_partes_e_avisa_das_grafias_nao_seguidas():
    pag1 = {'data': [{'nome': 'MARIA EXEMPLO DA SILVA', 'cpfCnpj': '', 'codParte1g': '1'}]
            + [{'nome': f'MARIA EXEMPLO {i}', 'cpfCnpj': '', 'codParte1g': f'x{i}'}
               for i in range(499)]}
    pag2 = {'data': [{'nome': 'MARIA EXEMPLO DA SILVA', 'cpfCnpj': '', 'codParte1g': '2'}]}
    vazio = {'data': []}
    b = _busca([_resp(pag1), _resp(pag2), _resp(vazio),
                _resp(_cod_parte(3)), _resp(vazio), _resp(_cod_parte(2, 10)), _resp(vazio)])
    paginas = list(b.paginar(NOME, 'Maria Exemplo da Silva'))
    cnjs = [i.numero_cnj for p in paginas for i in p.itens]
    assert len(cnjs) == 5                       # a grafia da página 2 também foi seguida
    assert '499 grafia(s) parecida(s) não seguida(s)' in paginas[-1].aviso_fonte
    assert [p.tem_proxima for p in paginas] == [True, False]


def test_busca_pagina_so_de_repetidos_nao_encerra():
    """Página cheia de repetidos não é o fim — o fim é a fonte dizer."""
    cheia = _cod_parte(500)
    b = _busca([_resp({'data': [{'nome': 'A B', 'codParte1g': '1'}]}), _resp({'data': []}),
                _resp(cheia), _resp(cheia), _resp(_cod_parte(1, 900)), _resp({'data': []})])
    cnjs = [i.numero_cnj for p in b.paginar(NOME, 'A B') for i in p.itens]
    assert '5000900-00.2026.8.21.0001' in cnjs and len(cnjs) == 501


def test_busca_teto_de_paginas_sai_como_truncado():
    janelas = [_resp(_cod_parte(10, 100 * i)) for i in range(5)]
    b = _busca(janelas)
    paginas = list(b.paginar(OAB, 'RS1', teto_paginas=2))
    assert len(paginas) == 2
    assert paginas[-1].tem_proxima is True       # o job lê teto + tem_proxima = truncado


@pytest.mark.parametrize('valor,esperado', [
    ('RS083749', ('RS', '83749')), ('083749', ('RS', '83749')),
    ('RS12345A', ('RS', '12345A')), ('12345A/SC', ('SC', '12345A')),
    ('SC 012345', ('SC', '12345')),
])
def test_oab_preserva_letra_e_uf(valor, esperado):
    from enrichers.busca.rest import oab_tjrs
    assert oab_tjrs(valor) == esperado


@pytest.mark.parametrize('a,b,mesma', [
    ('MARIA EXEMPLO DA SILVA', 'MARIA EXEMPLO DA SILVA', True),
    ('MARIA E. DA SILVA', 'MARIA EXEMPLO DA SILVA', True),
    ('MARIA EXEMPLO DA SILVA', 'MARIA EXEMPLO DA SILVA SANTOS', True),
    ('Maria Exemplo da Silva (EXEQUENTE)', 'MARIA EXEMPLO DA SILVA', True),
    ('JOAO DA SILVA', 'MARIA DA SILVA', False),
    ('MARIA', 'MARIA EXEMPLO DA SILVA', False),
    ('MARIA SOUZA', 'MARIA EXEMPLO DA SILVA', False),
    # revisão: geração muda a pessoa; ente só casa igual
    ('JOSE EXEMPLO DA SILVA', 'JOSE EXEMPLO DA SILVA FILHO', False),
    ('JOSE EXEMPLO DA SILVA', 'JOSE EXEMPLO DA SILVA JUNIOR', False),
    ('JOSE EXEMPLO DA SILVA', 'JOSÉ EXEMPLO DA SILVA JÚNIOR', False),
    ('JOSE EXEMPLO', 'JOSE EXEMPLO NETO', False),
    ('JOSE EXEMPLO', 'JOSE EXEMPLO SOBRINHO', False),
    ('JOSE EXEMPLO', 'JOSE EXEMPLO SEGUNDO', False),
    ('JOSE EXEMPLO FILHO', 'JOSE EXEMPLO FILHO', True),
    ('MUNICIPIO DE SANTA MARIA', 'MUNICIPIO DE SANTA MARIA DO HERVAL', False),
    ('MUNICÍPIO DE SANTA MARIA', 'MUNICIPIO DE SANTA MARIA', True),
    ('ESTADO DO RIO GRANDE DO SUL', 'ESTADO DO RIO GRANDE DO SUL - SECRETARIA DA FAZENDA', False),
])
def test_mesma_pessoa(a, b, mesma):
    from enrichers.tjrs import mesma_pessoa
    assert mesma_pessoa(a, b) is mesma


@pytest.mark.django_db
def test_nome_abreviado_na_consulta_nao_duplica_a_parte_do_djen():
    from tribunals.models import Parte, Process as Proc, ProcessoParte, Tribunal
    t, _ = Tribunal.objects.get_or_create(sigla='TJRS', defaults={'nome': 'TJ RS',
                                                                   'sigla_djen': 'TJRS'})
    proc = Proc.objects.create(tribunal=t, numero_cnj='5232029-64.2026.8.21.0001')
    cheia = Parte.objects.create(nome='MARIA EXEMPLO DA SILVA SANTOS', tipo='desconhecido')
    ProcessoParte.objects.create(processo=proc, parte=cheia, polo='ativo', papel='', fonte='djen')
    e = TjrsEnricher(pool=MagicMock())
    partes = e.somar_partes_de_terceiros(proc.pk, {
        'ativo': [{'nome': 'MARIA EXEMPLO DA SILVA', 'papel': 'EXEQUENTE'}],
        'passivo': [], 'outros': []})
    assert [p['nome'] for p in partes['ativo']] == ['MARIA EXEMPLO DA SILVA']
