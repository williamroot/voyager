"""Fila oficial de precatórios do TJPA (Power BI): coletor, parser estrito,
descoberta, classificação, vínculo com a origem pelo DJEN e a trava do pull.

As respostas em `tests/fixtures/tjpa/` são REAIS, da API pública do Power BI,
coletadas em 24/10/2026 com o próprio coletor (4 requisições): o modelo
(aparado aos campos usados), o `conceptualschema`, a lista cronológica inteira
(3.771 linhas) e as superpreferências (3 colunas). Sem dado pessoal: a lista
só traz CNJ do precatório, ente, natureza, número, autuação, valor e exercício.
"""
from __future__ import annotations

import copy
import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from api import leads_cache
from filas_oficiais import execucao, vinculo
from filas_oficiais.base import ItemFila, ResultadoEntidade
from filas_oficiais.descoberta import Placar, gravar_entidade, tipo_do_numero
from filas_oficiais.fontes import tjpa
from tribunals import classificador
from tribunals.models import (
    ApiClient,
    ClassificacaoLog,
    IngestionRun,
    Movimentacao,
    PrecatorioFilaOficial,
    Process,
    Tribunal,
)

D = Decimal
FIX = Path(__file__).parent / 'fixtures' / 'tjpa'


def _json(nome):
    if nome.endswith('.gz'):
        with gzip.open(FIX / nome, 'rt', encoding='utf-8') as f:
            return json.load(f)
    return json.loads((FIX / nome).read_text(encoding='utf-8'))


class _Resp:
    def __init__(self, corpo, status=200):
        self.corpo, self.status_code = corpo, status

    def json(self):
        return self.corpo


class _Sessao:
    """Serve as fixtures pelo caminho; `mexer(nome, corpo)` altera uma resposta."""

    def __init__(self, mexer=None, status=None):
        self.headers = {}
        self.mexer = mexer or (lambda nome, corpo: corpo)
        self.status = status or {}
        self.pedidos = []

    def request(self, metodo, url, json=None, timeout=None, headers=None):
        self.pedidos.append((metodo, url))
        if 'modelsAndExploration' in url:
            nome = 'models_and_exploration.json'
        elif 'conceptualschema' in url:
            nome = 'conceptualschema.json'
        else:
            ent = json['queries'][0]['Query']['Commands'][0][
                'SemanticQueryDataShapeCommand']['Query']['From'][0]['Entity']
            nome = ('querydata_lista.json.gz' if ent == tjpa.ENTIDADE_LISTA
                    else 'querydata_super.json.gz')
        if nome in self.status:
            return _Resp({}, self.status[nome])
        return _Resp(self.mexer(nome, copy.deepcopy(_json(nome))))


def _coletor(sessao=None, pendentes=0):
    pausas = []
    c = tjpa.ColetorTjpa(session=sessao or _Sessao(), pausa=1.0, dormir=pausas.append,
                         pendentes_registrados=pendentes)
    return c, pausas


# ---------------------------------------------------------------------------
# Coletor + parser (sem banco)
# ---------------------------------------------------------------------------
def test_lista_real_inteira():
    c, pausas = _coletor()
    falhas = []
    res = {r.entidade: r for r in c.iterar(falhas=falhas)}
    assert not falhas and len(res) == 127 and all(r.completa for r in res.values())
    itens = [i for r in res.values() for i in r.itens]
    assert len(itens) == 3757 == len({i.chave for i in itens})   # um ente por CNJ
    assert c.modelo['linhas'] == 3771 and c.modelo['model_id'] == 9156608
    assert sum(i.valor_original for i in itens) == D('1127722124.44')
    assert all(i.situacao == 'pendente' and i.fonte == 'tjpa_powerbi' for i in itens)
    assert all(i.chave.endswith('.8.14.0000') for i in itens)
    assert len(res['GOVERNO DO ESTADO DO PARA'].itens) == 942
    assert len(res['BELEM'].itens) == 424
    prefs = {i.preferencia for i in itens}
    assert prefs == {'', 'Idade', 'Doença Grave', 'Deficiência'}
    assert sum(1 for i in itens if i.preferencia) == 655
    assert {i.natureza for i in itens} <= {'ALIMENTAR', 'COMUM', '', 'COMUM/PARC',
                                           'ALIMENTAR/PARC', 'ALIMENTAR/COMUM'}
    # 2 números de precatório no mesmo processo: somados.
    assert sum(1 for i in itens if i.n_itens > 1) == 8
    # Devedor solidário (Estado + autarquia): um registro, os dois devedores.
    solid = [i for i in itens if ' / ' in i.entidade_devedora]
    assert len(solid) == 6 and all(i.n_itens == 1 for i in solid)
    # 4 requisições, uma por vez, pausa >= 1 s.
    assert c.requisicoes == 4 and len(pausas) == 4 and min(pausas) >= 1.0


def test_pausa_minima_de_um_segundo():
    assert tjpa.ColetorTjpa(session=_Sessao(), pausa=0.1, pendentes_registrados=0).pausa == 1.0


def _lista(mexer):
    return _Sessao(mexer=lambda n, c: mexer(c) if n == 'querydata_lista.json.gz' else c)


def _ds(c):
    return c['results'][0]['result']['data']['dsr']['DS'][0]


def _sem_coluna(c):
    for e in c['schemas'][0]['schema']['Entities']:
        if e['Name'] == tjpa.ENTIDADE_LISTA:
            e['Properties'] = [p for p in e['Properties'] if p['Name'] != 'vl_causa']
    return c


def _trunca(c):
    _ds(c)['IC'] = False
    return c


def _sobra_valor(c):
    _ds(c)['PH'][0]['DM0'][5]['C'].append(1)
    return c


def _vazia(c):
    _ds(c)['PH'][0]['DM0'] = []
    return c


def _cnj_ruim(c):
    _ds(c)['ValueDicts']['D0'][0] = '0800305-14.2025.8.14.0000'   # DV errado
    return c


def _fora_do_2g(c):
    _ds(c)['ValueDicts']['D0'][0] = '0020647-97.2015.8.14.0301'
    return c


def _outra_coluna(c):
    c['results'][0]['result']['data']['descriptor']['Select'][6]['Name'] = 't.vl_saldo'
    return c


@pytest.mark.parametrize('sessao,trecho', [
    (_Sessao(mexer=lambda n, c: _sem_coluna(c) if n == 'conceptualschema.json' else c),
     'colunas ausentes'),
    (_Sessao(mexer=lambda n, c: {'models': c['models'] * 2}
             if n == 'models_and_exploration.json' else c), '2 modelos'),
    (_lista(_trunca), 'truncada'),
    (_lista(_sobra_valor), 'sobra valor'),
    (_lista(_vazia), 'vazia'),
    (_lista(_cnj_ruim), 'dígito verificador'),
    (_lista(_fora_do_2g), 'fora do PJe 2º grau'),
    (_lista(_outra_coluna), 'colunas'),
    (_Sessao(status={'models_and_exploration.json': 404}), 'HTTP 404'),
])
def test_parser_estrito_falha_alto(sessao, trecho):
    """Nunca devolve fila vazia como sucesso: nenhum resultado e a falha anotada."""
    c, _ = _coletor(sessao)
    falhas = []
    assert list(c.iterar(falhas=falhas)) == []
    assert falhas and trecho in falhas[0]


def test_lista_que_encolheu_demais_e_falha():
    c, _ = _coletor(pendentes=10000)
    falhas = []
    assert list(c.iterar(falhas=falhas)) == []
    assert 'encolheu' in falhas[0]


def test_dsr_forma_nomeada_e_nulo():
    resp = {'results': [{'result': {'data': {
        'descriptor': {'Select': [{'Name': 't.a'}, {'Name': 't.b'}]},
        'dsr': {'DS': [{'IC': True, 'ValueDicts': {'D0': ['x', 'y']}, 'PH': [{'DM0': [
            {'S': [{'N': 'G0', 'T': 1, 'DN': 'D0'}, {'N': 'G1', 'T': 4}], 'C': [1, 7]},
            {'C': [3], 'R': 1},
            {'C': [0], 'Ø': 2},
        ]}]}]}}}}]}
    assert tjpa.decodificar_dsr(resp, ('a', 'b')) == [
        {'a': 'y', 'b': 7}, {'a': 'y', 'b': 3}, {'a': 'x', 'b': None}]


def test_tipo_do_numero_tjpa():
    assert tipo_do_numero('0800305-13.2025.8.14.0000', False, 'TJPA') == 'tjpa_2g'
    assert tipo_do_numero('0020647-97.2015.8.14.0301', False, 'TJPA') == 'tjpa_outro_numero'


# ---------------------------------------------------------------------------
# Descoberta + classificação (banco)
# ---------------------------------------------------------------------------
@pytest.fixture
def tjpa_trib(db):
    return Tribunal.objects.get_or_create(sigla='TJPA', defaults={'nome': 'TJPA'})[0]


PREC = '0827902-54.2025.8.14.0000'      # Estado do Pará, na lista de 24/10/2026
PREC2 = '0800305-13.2025.8.14.0000'     # Anajás
ORIGEM = '0020647-97.2015.8.14.0301'    # cita PREC no DJEN (04/02/2026)


def _it(chave=PREC, entidade='GOVERNO DO ESTADO DO PARA', **kw):
    kw.setdefault('valor_original', D('100000.00'))
    return ItemFila(chave=chave, fonte='tjpa_powerbi', entidade=entidade,
                    situacao='pendente', situacao_raw='lista cronológica', **kw)


@pytest.mark.django_db
def test_pendente_nasce_precatorio_2g(tjpa_trib):
    placar = Placar()
    gravar_entidade(tjpa_trib, [_it(id_externo='00771/2025', ano_orcamento=2027)], placar)
    p = Process.objects.get(numero_cnj=PREC)
    assert (p.tribunal_id, p.classe_codigo, p.grau) == ('TJPA', '1265', 'G2')
    assert p.classificacao == Process.CLASSIF_PRECATORIO and p.classificacao_score == 1.0
    reg = PrecatorioFilaOficial.objects.get(chave=PREC)
    assert (reg.id_externo, reg.ano_orcamento, reg.processo_id) == ('00771/2025', 2027, p.pk)
    assert placar.criados == {'tjpa_2g': 1}


@pytest.mark.django_db
def test_numero_fora_do_2g_nao_cria(tjpa_trib):
    placar = Placar()
    gravar_entidade(tjpa_trib, [_it(ORIGEM)], placar)
    assert not Process.objects.exists() and placar.falhas == 1


class _ColetorFixo:
    cobertura_total = True

    def __init__(self, resultados):
        self.resultados, self.requisicoes = resultados, 4

    def iterar(self, entidades=None, pular=None, falhas=None):
        yield from self.resultados


def _res(itens, entidade='GOVERNO DO ESTADO DO PARA'):
    return ResultadoEntidade(entidade=entidade, itens=itens, completa=True,
                             info={'chave': f'powerbi:{entidade}'})


@pytest.fixture
def cache_limpo():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_saiu_da_lista_rebaixa_e_anota(tjpa_trib, cache_limpo):
    ontem = timezone.now() - timedelta(days=1)
    gravar_entidade(tjpa_trib, [_it(), _it(PREC2, entidade='ANAJAS')], Placar(), agora=ontem)
    resumo = execucao.executar('TJPA', escrever=True, coletor=_ColetorFixo(
        [_res([_it(PREC2, entidade='ANAJAS')], entidade='ANAJAS')]))
    assert IngestionRun.objects.get(pk=resumo['run_id']).status == 'success'
    reg = PrecatorioFilaOficial.objects.get(chave=PREC)
    assert reg.situacao == 'fora_da_fila' and 'sem pagamento' in reg.motivo_saida
    p = Process.objects.get(numero_cnj=PREC)
    assert p.classificacao == Process.CLASSIF_NAO_LEAD          # rebaixado, não removido
    log = ClassificacaoLog.objects.filter(processo=p).order_by('-pk').first()
    assert log.features_snapshot.get('FILA_OFICIAL_saiu_sem_pagamento_ANTI') == 1
    assert Process.objects.get(numero_cnj=PREC2).classificacao == Process.CLASSIF_PRECATORIO


@pytest.mark.django_db
def test_coleta_que_falhou_nao_tira_ninguem_da_fila(tjpa_trib, cache_limpo):
    gravar_entidade(tjpa_trib, [_it()], Placar(), agora=timezone.now() - timedelta(days=1))
    c, _ = _coletor(_lista(_trunca))
    resumo = execucao.executar('TJPA', escrever=True, coletor=c)
    assert IngestionRun.objects.get(pk=resumo['run_id']).status == 'failed'
    assert PrecatorioFilaOficial.objects.get(chave=PREC).situacao == 'pendente'


@pytest.mark.django_db
def test_dry_run_da_lista_real_nao_escreve(tjpa_trib, cache_limpo):
    c, _ = _coletor()
    resumo = execucao.executar('TJPA', escrever=False, coletor=c)
    assert resumo['itens'] == 3757 and resumo['criados'] == {'tjpa_2g': 3757}
    assert not Process.objects.exists() and not PrecatorioFilaOficial.objects.exists()
    assert not IngestionRun.objects.exists()


# ---------------------------------------------------------------------------
# Vínculo com a origem pelo DJEN + promoção da origem
# ---------------------------------------------------------------------------
CITA = ('2. Determino o cancelamento do Ofício Precatório nº 771/2025 (ID 162222855), '
        'devendo ser oficiado à CPREC junto ao processo 0827902-54.2025.8.14.0000 para '
        'ciência. 3. Levante-se a suspensão do processo principal.')
AGRAVO = ('Interposto agravo de instrumento nº 0812345-12.2025.8.14.0000, mantenho a '
          'decisão agravada por seus próprios fundamentos.')


def _origem(trib, cnj=ORIGEM, texto=CITA, classificacao=Process.CLASSIF_PRE_PRECATORIO):
    p = Process.objects.create(tribunal=trib, numero_cnj=cnj, classe_codigo='156',
                               classificacao=classificacao, classificacao_score=0.5,
                               ultima_movimentacao_em=timezone.now())
    Movimentacao.objects.create(processo=p, tribunal=trib, external_id=f'{cnj}-0',
                                texto=texto, data_disponibilizacao=timezone.now())
    return p


@pytest.mark.django_db
def test_vinculo_pelo_djen_e_promocao_da_origem(tjpa_trib):
    gravar_entidade(tjpa_trib, [_it()], Placar())
    o = _origem(tjpa_trib)
    _origem(tjpa_trib, cnj='0800069-46.2016.8.14.0301', texto=AGRAVO)
    medido = vinculo.vincular_origens('TJPA', escrever=False)
    assert medido == {'citacoes': 1, 'vinculados': 1}
    assert PrecatorioFilaOficial.objects.get(chave=PREC).processo_origem_id is None
    feito = vinculo.vincular_origens('TJPA', escrever=True)
    assert feito['vinculados'] == 1 and feito['classificado_PRECATORIO'] == 1
    reg = PrecatorioFilaOficial.objects.get(chave=PREC)
    assert (reg.processo_origem_id, reg.origem_cnj) == (o.pk, ORIGEM)
    o.refresh_from_db()
    assert o.classificacao == Process.CLASSIF_PRECATORIO
    # Idempotente.
    assert vinculo.vincular_origens('TJPA', escrever=True) == {'citacoes': 1, 'ja_vinculado': 1}


@pytest.mark.django_db
def test_vinculo_nao_inventa(tjpa_trib):
    """Citado fora da fila não cria; duas origens citando o mesmo não escolhe."""
    _origem(tjpa_trib)                                  # PREC não está na fila
    assert vinculo.vincular_origens('TJPA', escrever=True) == {
        'citacoes': 1, 'citado_fora_da_fila': 1}
    gravar_entidade(tjpa_trib, [_it()], Placar())
    _origem(tjpa_trib, cnj='0800069-46.2016.8.14.0301')
    placar = vinculo.vincular_origens('TJPA', escrever=True)
    assert placar['ambiguo'] == 1 and 'vinculados' not in placar
    assert PrecatorioFilaOficial.objects.get(chave=PREC).processo_origem_id is None


@pytest.mark.django_db
def test_origem_se_abstem_quando_o_precatorio_sai(tjpa_trib):
    gravar_entidade(tjpa_trib, [_it()], Placar())
    o = _origem(tjpa_trib)
    vinculo.vincular_origens('TJPA', escrever=True)
    PrecatorioFilaOficial.objects.filter(chave=PREC).update(situacao='fora_da_fila')
    _, _, feats = classificador.classificar(o)
    assert 'FILA_OFICIAL_saiu_sem_pagamento_ANTI' not in feats


# ---------------------------------------------------------------------------
# TRAVA do pull (FILA_OFICIAL_2G_PULL_LIBERADO)
# ---------------------------------------------------------------------------
@pytest.fixture
def redis_limpo():
    conn = leads_cache._conn()

    def _limpar():
        for k in list(conn.scan_iter('leads:*')):
            conn.delete(k)
    _limpar()
    yield
    _limpar()


@pytest.fixture
def cenario_pull(tjpa_trib, redis_limpo):
    ApiClient.objects.create(nome='juriscope-tjpa', api_key='k-tjpa', ativo=True)
    gravar_entidade(tjpa_trib, [_it(), _it(PREC2, entidade='ANAJAS')], Placar())
    o = _origem(tjpa_trib)
    Process.objects.filter(pk=o.pk).update(
        ultima_movimentacao_em=datetime(2026, 10, 22, tzinfo=UTC))
    vinculo.vincular_origens('TJPA', escrever=True)
    assert Process.objects.filter(classificacao=Process.CLASSIF_PRECATORIO).count() == 3


def _pull(tribunal='TJPA'):
    resp = Client().get(f'/api/v1/leads/?nivel=PRECATORIO&tribunal={tribunal}&limit=50'
                        '&min_score=0.7', HTTP_X_API_KEY='k-tjpa')
    assert resp.status_code == 200
    return {r['cnj'] for r in resp.json()['results']}


@pytest.mark.django_db
@override_settings(FILA_OFICIAL_2G_PULL_LIBERADO=[], TJMT_PRECATORIO_2G_NO_PULL=False)
def test_trava_padrao_nao_serve_o_2g_do_tjpa(cenario_pull):
    # A origem de 1º grau (onde está o ofício) sai; os de 2º grau não.
    assert _pull() == {ORIGEM}
    assert _pull('') == {ORIGEM}
    stats = Client().get('/api/v1/leads/stats/', HTTP_X_API_KEY='k-tjpa').json()
    assert stats['pending']['PRECATORIO'] == 3


@pytest.mark.django_db
@override_settings(FILA_OFICIAL_2G_PULL_LIBERADO=['TJPA'])
def test_trava_liberada_serve_o_2g_do_tjpa(cenario_pull):
    assert _pull() == {ORIGEM, PREC, PREC2}


def test_trava_por_tribunal():
    with override_settings(FILA_OFICIAL_2G_PULL_LIBERADO=[], TJMT_PRECATORIO_2G_NO_PULL=False):
        assert leads_cache.tribunais_2g_travados('') == ['TJMT', 'TJPA']
        assert leads_cache.tribunais_2g_travados('TJPA') == ['TJPA']
        assert leads_cache.tribunais_2g_travados('TJAL') == []
    with override_settings(FILA_OFICIAL_2G_PULL_LIBERADO=['tjpa'],
                           TJMT_PRECATORIO_2G_NO_PULL=False):
        assert leads_cache.tribunais_2g_travados('') == ['TJMT']
    with override_settings(FILA_OFICIAL_2G_PULL_LIBERADO=[], TJMT_PRECATORIO_2G_NO_PULL=True):
        assert leads_cache.tribunais_2g_travados('') == ['TJPA']
        assert not leads_cache.trava_tjmt_2g_ativa('TJMT')
