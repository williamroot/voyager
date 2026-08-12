"""Testes do serviço de agregação do Mapa Comercial (search/agg_comercial.py).

Não dependem de ES real: mockam o cliente (`search.agg_comercial.get_es`) e o
cache de cobertura. Cobrem: montagem do corpo ES (asserts no body), cálculo do
score de foco, parse/saneamento dos filtros e filtros combinados.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from search import agg_comercial as agg


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _qd(**kwargs):
    """Simula um request.GET (dict-like com .get)."""
    return SimpleNamespace(get=lambda k, default=None: kwargs.get(k, default))


def _es_resp_terms(campo, buckets):
    """Envelope ES de resposta terms-agg."""
    return {
        'aggregations': {
            'buckets': {
                'buckets': [
                    {
                        'key': b['key'],
                        'doc_count': b['volume'],
                        'valor': {'value': b.get('valor', 0.0)},
                        'potencial': {'doc_count': b.get('potencial', 0)},
                        # default: sinal processado no bucket inteiro (docs com o
                        # campo presente); use sinal_conhecido=0 pra simular UF
                        # não-backfillada (potencial → None)
                        'sinal_conhecido': {'doc_count': b.get('sinal_conhecido', b['volume'])},
                        'confirmado': {'doc_count': b.get('confirmado', 0)},
                        # união possível ∪ confirmado (default: o maior dos dois,
                        # que é o piso lógico da união)
                        'todos': {'doc_count': b.get('todos',
                                  max(b.get('potencial', 0), b.get('confirmado', 0)))},
                    }
                    for b in buckets
                ]
            }
        }
    }


def _es_resp_total(volume, valor, potencial, confirmado, todos=None):
    return {
        'hits': {'total': {'value': volume}},
        'aggregations': {
            'valor': {'value': valor},
            'potencial': {'doc_count': potencial},
            'confirmado': {'doc_count': confirmado},
            # união possível ∪ confirmado (default: piso lógico = o maior)
            'todos': {'doc_count': max(potencial, confirmado) if todos is None else todos},
        },
    }


# --------------------------------------------------------------------------- #
# parse_filtros
# --------------------------------------------------------------------------- #
def test_parse_filtros_vazio():
    assert agg.parse_filtros(_qd()) == {}


def test_parse_filtros_basico():
    f = agg.parse_filtros(_qd(uf='sp', tribunal='tjsp', classificacao='precatorio'))
    assert f['uf'] == 'SP'
    assert f['tribunal'] == 'TJSP'
    assert f['classificacao'] == 'PRECATORIO'


def test_parse_filtros_tipo_potencial():
    f = agg.parse_filtros(_qd(tipo='potencial'))
    assert f['tem_sinal'] is True


def test_parse_filtros_tipo_confirmado():
    f = agg.parse_filtros(_qd(tipo='confirmado'))
    assert f['classificacao'] == 'PRECATORIO'


def test_parse_filtros_tem_sinal_bool():
    assert agg.parse_filtros(_qd(tem_sinal='true'))['tem_sinal'] is True
    assert agg.parse_filtros(_qd(tem_sinal='0'))['tem_sinal'] is False


def test_parse_filtros_ano_futuro_clampado():
    ano_atual = datetime.date.today().year
    f = agg.parse_filtros(_qd(ano_min='2020', ano_max='3000'))
    assert f['ano_min'] == 2020
    assert f['ano_max'] == ano_atual   # 3000 clampado pro ano atual


def test_parse_filtros_ano_invertido_ordena():
    f = agg.parse_filtros(_qd(ano_min='2022', ano_max='2010'))
    assert f['ano_min'] == 2010
    assert f['ano_max'] == 2022


def test_parse_filtros_valor_negativo_ignorado():
    f = agg.parse_filtros(_qd(valor_min='-5', valor_max='1000'))
    assert 'valor_min' not in f
    assert f['valor_max'] == 1000.0


def test_parse_filtros_valor_invertido_ordena():
    f = agg.parse_filtros(_qd(valor_min='9000', valor_max='100'))
    assert f['valor_min'] == 100.0
    assert f['valor_max'] == 9000.0


def test_parse_filtros_lixo_nao_quebra():
    f = agg.parse_filtros(_qd(ano_min='abc', valor_max='xyz', n='???'))
    assert 'ano_min' not in f
    assert 'valor_max' not in f


def test_parse_filtros_natureza_vira_codigo_classe():
    f = agg.parse_filtros(_qd(natureza='1116'))
    assert f['codigo_classe'] == '1116'
    # codigo_classe explícito tem precedência
    f2 = agg.parse_filtros(_qd(natureza='1116', codigo_classe='9999'))
    assert f2['codigo_classe'] == '9999'


# --------------------------------------------------------------------------- #
# build_filter_clauses / build_body_*
# --------------------------------------------------------------------------- #
def test_build_filter_clauses_todos():
    filtros = {
        'uf': 'SP', 'tribunal': 'TJSP', 'classificacao': 'PRECATORIO',
        'codigo_classe': '1116', 'tem_sinal': True,
        'ano_min': 2020, 'ano_max': 2024, 'valor_min': 100.0, 'valor_max': 5000.0,
    }
    clauses = agg.build_filter_clauses(filtros)
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'term': {'tribunal': 'TJSP'}} in clauses
    assert {'term': {'classificacao': 'PRECATORIO'}} in clauses
    assert {'term': {'codigo_classe': '1116'}} in clauses
    assert {'term': {'tem_sinal_precatorio': True}} in clauses
    assert {'range': {'ano_cnj': {'gte': 2020, 'lte': 2024}}} in clauses
    assert {'range': {'valor_causa': {'gte': 100.0, 'lte': 5000.0}}} in clauses


def test_build_body_por_campo_estrutura():
    body = agg.build_body_por_campo('uf', {})
    assert body['size'] == 0
    terms = body['aggs']['buckets']['terms']
    assert terms['field'] == 'uf'
    assert terms['size'] == agg._TERMS_SIZE
    sub = body['aggs']['buckets']['aggs']
    assert sub['valor'] == {'sum': {'field': 'valor_causa'}}
    assert sub['potencial'] == {'filter': {'term': {'tem_sinal_precatorio': True}}}
    assert sub['confirmado'] == {'filter': {'term': {'classificacao': 'PRECATORIO'}}}
    # sem filtro → sem query
    assert 'query' not in body


def test_build_body_com_filtro_gera_query():
    body = agg.build_body_por_campo('tribunal', {'uf': 'SP', 'ano_min': 2020})
    assert body['aggs']['buckets']['terms']['field'] == 'tribunal'
    clauses = body['query']['bool']['filter']
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'range': {'ano_cnj': {'gte': 2020}}} in clauses


def test_build_body_total():
    body = agg.build_body_total({'uf': 'SP'})
    assert body['size'] == 0
    assert body['track_total_hits'] is True
    assert 'buckets' not in body['aggs']
    assert body['aggs']['valor'] == {'sum': {'field': 'valor_causa'}}
    assert body['query']['bool']['filter'] == [{'term': {'uf': 'SP'}}]


# --------------------------------------------------------------------------- #
# score de foco
# --------------------------------------------------------------------------- #
def test_score_foco_e_densidade_pura():
    """O ranking responde UMA pergunta: onde é denso em precatório.

    Até 12/08/2026 multiplicava por valor e por (1−cobertura), espremendo três
    perguntas num número só. Valor e cobertura seguem no bucket, como atributos
    exibidos — não como desconto silencioso na nota.
    """
    dens, score = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=0, valor_max=100.0)
    assert dens == 0.1
    assert score == dens


def test_cobertura_nao_mexe_no_score():
    """"Já olhamos" é BANDEIRA, não desconto.

    Medido em prod: o MT tem a 6ª maior densidade do país (5,59%) e aparecia em
    21º porque já tínhamos olhado 24,4% dele. Quem decide se vale reatacar um
    estado meio trabalhado é o comercial, lendo a bandeira — não a fórmula,
    escondendo o estado.
    """
    _, com_cobertura = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=50.0, valor_max=100.0)
    _, sem_cobertura = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=0.0, valor_max=100.0)
    assert com_cobertura == sem_cobertura == 0.1


def test_score_foco_cobertura_none_trata_como_zero():
    # sem cobertura no cache ⇒ (1-0)=1, score cheio (ataque primeiro)
    _, score = agg.calcular_score_foco(
        volume=1000, valor=100.0, potencial=100, cobertura_pct=None, valor_max=100.0)
    assert score == 0.1


def test_score_foco_valor_max_zero_fator_neutro():
    # conjunto sem dado de valor (ex.: federal, DJEN não traz valor_causa) →
    # fator valor NEUTRO: rankeia por densidade×(1−cobertura) em vez de zerar.
    dens, score = agg.calcular_score_foco(
        volume=1000, valor=0.0, potencial=100, cobertura_pct=0, valor_max=0.0)
    assert dens == 0.1
    assert score == 0.1


def test_parse_buckets_sinal_nao_processado_potencial_null():
    # UF onde o backfill Fase 0 nunca passou (nenhum doc tem o campo) →
    # potencial=None + sinal_processado=False (desconhecido ≠ zero; achado do QA:
    # SP parecia "sem precatório" no modo Potencial).
    resp = _es_resp_terms('uf', [
        {'key': 'SP', 'volume': 1000, 'potencial': 0, 'sinal_conhecido': 0},
        {'key': 'PR', 'volume': 500, 'potencial': 40, 'sinal_conhecido': 500},
    ])
    out = agg._parse_buckets(resp, 'uf')
    sp = next(b for b in out if b['uf'] == 'SP')
    pr = next(b for b in out if b['uf'] == 'PR')
    assert sp['potencial'] is None and sp['sinal_processado'] is False
    assert pr['potencial'] == 40 and pr['sinal_processado'] is True


def test_score_foco_valor_esparso_bucket_sem_valor_neutro():
    # valor_causa é esparso (só SP tem hoje): bucket SEM valor num conjunto onde
    # OUTRO bucket tem → fator neutro pro desconhecido (não multiplica por ~0).
    dens, score = agg.calcular_score_foco(
        volume=1000, valor=0.0, potencial=100, cobertura_pct=0, valor_max=1e12)
    assert dens == 0.1
    assert score == 0.1  # neutro, não esmagado pelo valor_max de SP


def test_score_foco_volume_zero_nao_div0():
    dens, score = agg.calcular_score_foco(
        volume=0, valor=100.0, potencial=0, cobertura_pct=0, valor_max=100.0)
    assert dens == 0.0
    assert score == 0.0


# --------------------------------------------------------------------------- #
# agg_por_uf (fim-a-fim com ES + cobertura mockados)
# --------------------------------------------------------------------------- #
@patch('search.agg_comercial._cobertura_por_uf')
@patch('search.agg_comercial.get_es')
def test_agg_por_uf_monta_query_e_calcula_score(mock_get_es, mock_cob):
    es = MagicMock()
    # 1ª chamada: terms; 2ª chamada: total
    es.search.side_effect = [
        _es_resp_terms('uf', [
            {'key': 'SP', 'volume': 1000, 'valor': 500.0, 'potencial': 200, 'confirmado': 50},
            {'key': 'RJ', 'volume': 500, 'valor': 250.0, 'potencial': 50, 'confirmado': 10},
        ]),
        _es_resp_total(1500, 750.0, 250, 60),
    ]
    mock_get_es.return_value = es
    mock_cob.return_value = {'SP': 20.0, 'RJ': 80.0}

    from django.core.cache import cache
    cache.clear()  # consultas filtradas agora também têm cache (por combinação)
    out = agg.agg_por_uf({'ano_min': 2020})

    # 1 request de terms + 1 de total (não N requests)
    assert es.search.call_count == 2
    body_terms = es.search.call_args_list[0].kwargs['body']
    assert body_terms['aggs']['buckets']['terms']['field'] == 'uf'
    assert {'range': {'ano_cnj': {'gte': 2020}}} in body_terms['query']['bool']['filter']

    ufs = {b['uf']: b for b in out['ufs']}
    # densidade CRUA (o que a tela mostra): SP 200/1000, RJ 50/500
    assert ufs['SP']['densidade'] == 0.2
    assert ufs['RJ']['densidade'] == 0.1
    assert ufs['SP']['cobertura_pct'] == 20.0     # segue no bucket, pra exibir
    # score = densidade AMORTECIDA pelo prior (média do conjunto = 250/1500):
    # com fixtures minúsculas o prior de 1.000 domina — em prod, com milhões de
    # processos por UF, o efeito some (ver test_amostra_minuscula...)
    media = 250 / 1500
    assert ufs['SP']['score_foco'] == pytest.approx((200 + 1000 * media) / 2000, abs=1e-4)
    assert ufs['RJ']['score_foco'] == pytest.approx((50 + 1000 * media) / 1500, abs=1e-4)
    assert ufs['SP']['score_foco'] > ufs['RJ']['score_foco']
    # ordenado por score desc → SP primeiro
    assert out['ufs'][0]['uf'] == 'SP'
    assert out['total']['volume'] == 1500
    assert out['total']['confirmado'] == 60


@patch('search.agg_comercial._cobertura_por_tribunal')
@patch('search.agg_comercial.get_es')
def test_agg_tribunais_aplica_uf(mock_get_es, mock_cob):
    es = MagicMock()
    es.search.side_effect = [
        _es_resp_terms('tribunal', [
            {'key': 'TJSP', 'volume': 900, 'valor': 400.0, 'potencial': 180, 'confirmado': 45},
        ]),
        _es_resp_total(900, 400.0, 180, 45),
    ]
    mock_get_es.return_value = es
    mock_cob.return_value = {'TJSP': 30.0}

    out = agg.agg_tribunais('sp', {'classificacao': 'PRECATORIO'})

    assert out['uf'] == 'SP'
    body = es.search.call_args_list[0].kwargs['body']
    clauses = body['query']['bool']['filter']
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'term': {'classificacao': 'PRECATORIO'}} in clauses
    assert body['aggs']['buckets']['terms']['field'] == 'tribunal'
    assert out['tribunais'][0]['tribunal'] == 'TJSP'
    assert out['tribunais'][0]['cobertura_pct'] == 30.0


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_ordena_por_metric(mock_agg):
    mock_agg.return_value = {
        'ufs': [
            {'uf': 'SP', 'volume': 1000, 'valor': 500.0, 'score_foco': 0.16, 'confirmado': 50},
            {'uf': 'RJ', 'volume': 2000, 'valor': 250.0, 'score_foco': 0.01, 'confirmado': 10},
        ],
        'gerado_em': '2026-08-11T00:00:00+00:00',
    }
    # por volume → RJ primeiro
    r = agg.ranking_top('volume', 1, {})
    assert r['metric'] == 'volume'
    assert r['n'] == 1
    assert len(r['ranking']) == 1
    assert r['ranking'][0]['uf'] == 'RJ'
    # por score → SP primeiro
    r2 = agg.ranking_top('score', 10, {})
    assert r2['ranking'][0]['uf'] == 'SP'
    # metric inválida cai em score
    r3 = agg.ranking_top('inexistente', 5, {})
    assert r3['metric'] == 'score'


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_clampa_n(mock_agg):
    mock_agg.return_value = {'ufs': [], 'gerado_em': 'x'}
    assert agg.ranking_top('score', 0, {})['n'] == 1
    assert agg.ranking_top('score', 999, {})['n'] == 100
    assert agg.ranking_top('score', 'abc', {})['n'] == 10


# --------------------------------------------------------------------------- #
# visão "todos" = UNIÃO possível ∪ confirmado
# --------------------------------------------------------------------------- #
def test_subagg_todos_e_uniao_nao_soma():
    """`todos` tem que ser bool/should (união), NUNCA soma dos dois contadores.

    Medido no ES em 12/08/2026: dos 47.720 confirmados só 6.421 também têm sinal
    de texto ⇒ somar contaria essa interseção em dobro. E 41.299 confirmados
    (87%) NÃO têm sinal, então ficavam invisíveis na visão "possíveis".
    """
    sub = agg._metric_subaggs()
    assert 'todos' in sub
    b = sub['todos']['filter']['bool']
    assert b['minimum_should_match'] == 1
    campos = {list(c['term'].keys())[0] for c in b['should']}
    assert campos == {'tem_sinal_precatorio', 'classificacao'}


def test_todos_no_bucket_e_no_total():
    resp = _es_resp_terms('uf', [
        # SP: sinal NÃO processado (potencial None) mas com confirmados reais —
        # é o caso que a visão "possíveis" esconde e a "todos" revela
        {'key': 'SP', 'volume': 1000, 'potencial': 0, 'sinal_conhecido': 0,
         'confirmado': 4218, 'todos': 4218},
        {'key': 'PR', 'volume': 500, 'potencial': 40, 'confirmado': 1, 'todos': 41},
    ])
    out = agg._parse_buckets(resp, 'uf')
    sp = next(b for b in out if b['uf'] == 'SP')
    pr = next(b for b in out if b['uf'] == 'PR')
    # 'todos' é SEMPRE numérico (mesmo sem sinal processado) — é o dado que existe
    assert sp['potencial'] is None and sp['todos'] == 4218
    assert pr['todos'] == 41
    # e nunca é a soma (que daria 41 == 40+1 por coincidência aqui, mas 4218 != 0+4218+…)
    assert sp['todos'] != (sp['potencial'] or 0) + sp['confirmado'] or sp['potencial'] is None


def test_total_expõe_todos():
    tot = agg._parse_total(_es_resp_total(1500, 750.0, 250, 60, todos=280))
    assert tot['todos'] == 280


# --------------------------------------------------------------------------- #
# score por LENTE — o "ataque primeiro" segue a lente escolhida no mapa
# --------------------------------------------------------------------------- #
def test_score_por_lente_muda_o_numerador():
    """Cada lente ranqueia pela SUA densidade.

    RO tem muito sinal de texto e pouco confirmado; MG é o inverso. Se o score
    ignorasse a lente, o mapa pintaria confirmados com MG na frente e o ranking
    continuaria mandando atacar RO — duas respostas na mesma tela.
    """
    buckets = [
        {'uf': 'RO', 'volume': 1_000_000, 'valor': 0.0,
         'potencial': 119_586, 'confirmado': 1_948, 'todos': 119_586},
        {'uf': 'MG', 'volume': 1_000_000, 'valor': 0.0,
         'potencial': 814, 'confirmado': 3_238, 'todos': 4_022},
    ]
    out = agg._enriquecer_buckets(buckets, 'uf', {})
    ro = next(b for b in out if b['uf'] == 'RO')
    mg = next(b for b in out if b['uf'] == 'MG')

    assert ro['score_por_lente']['potencial'] > mg['score_por_lente']['potencial']
    assert mg['score_por_lente']['confirmado'] > ro['score_por_lente']['confirmado']
    assert ro['score_por_lente']['todos'] > mg['score_por_lente']['todos']
    # densidade da lente 'todos' de MG usa 4.022 (união), não 4.052 (soma)
    assert mg['densidade_por_lente']['todos'] == round(4_022 / 1_000_000, 4)


def test_score_foco_sem_sufixo_continua_sendo_possiveis():
    """Compat: quem consome `score_foco`/`densidade` sem saber de lente não quebra."""
    out = agg._enriquecer_buckets(
        [{'uf': 'RO', 'volume': 1000, 'valor': 0.0,
          'potencial': 100, 'confirmado': 5, 'todos': 100}], 'uf', {})
    b = out[0]
    assert b['score_foco'] == b['score_por_lente']['potencial']
    assert b['densidade'] == b['densidade_por_lente']['potencial']


def test_score_lente_potencial_none_nao_quebra():
    """UF sem sinal processado (potencial None) ainda pontua nas outras lentes."""
    out = agg._enriquecer_buckets(
        [{'uf': 'SP', 'volume': 1000, 'valor': 0.0,
          'potencial': None, 'confirmado': 40, 'todos': 40}], 'uf', {})
    b = out[0]
    assert b['score_por_lente']['potencial'] == 0.0
    assert b['score_por_lente']['confirmado'] > 0


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_reordena_por_lente(mock_agg):
    mock_agg.return_value = {
        'ufs': [
            {'uf': 'RO', 'score_foco': 0.12, 'confirmado': 1_948,
             'score_por_lente': {'potencial': 0.12, 'confirmado': 0.001, 'todos': 0.12}},
            {'uf': 'MG', 'score_foco': 0.0008, 'confirmado': 3_238,
             'score_por_lente': {'potencial': 0.0008, 'confirmado': 0.003, 'todos': 0.004}},
        ],
        'gerado_em': 'x',
    }
    assert agg.ranking_top('score', 10, {}, lente='potencial')['ranking'][0]['uf'] == 'RO'
    assert agg.ranking_top('score', 10, {}, lente='confirmado')['ranking'][0]['uf'] == 'MG'
    assert agg.ranking_top('score', 10, {}, lente='todos')['ranking'][0]['uf'] == 'RO'
    # lente desconhecida cai no default histórico (possíveis), não estoura
    r = agg.ranking_top('score', 10, {}, lente='xpto')
    assert r['lente'] == 'potencial' and r['ranking'][0]['uf'] == 'RO'


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_payload_velho_sem_score_por_lente(mock_agg):
    """Cache de 2min pós-deploy pode devolver bucket sem `score_por_lente`."""
    mock_agg.return_value = {
        'ufs': [{'uf': 'RO', 'score_foco': 0.12}, {'uf': 'MG', 'score_foco': 0.01}],
        'gerado_em': 'x',
    }
    r = agg.ranking_top('score', 10, {}, lente='confirmado')
    assert r['ranking'][0]['uf'] == 'RO'      # cai pro score_foco, não quebra


@patch('search.agg_comercial.agg_por_uf')
def test_ranking_top_metric_potencial_none_nao_quebra(mock_agg):
    """Ordenar por `potencial` com UF não processada (None) não pode dar TypeError."""
    mock_agg.return_value = {
        'ufs': [{'uf': 'SP', 'potencial': None}, {'uf': 'PR', 'potencial': 42}],
        'gerado_em': 'x',
    }
    assert agg.ranking_top('potencial', 10, {})['ranking'][0]['uf'] == 'PR'


# --------------------------------------------------------------------------- #
# filtro por PARTE/ENTIDADE (ex.: ver só o INSS no mapa)
# --------------------------------------------------------------------------- #
def test_parse_filtros_parte():
    assert agg.parse_filtros(_qd(parte='  Instituto   Nacional do Seguro Social '))['parte'] \
        == 'Instituto Nacional do Seguro Social'
    assert 'parte' not in agg.parse_filtros(_qd(parte='   '))
    # nome absurdo não vira query gigante
    assert len(agg.parse_filtros(_qd(parte='x' * 500))['parte']) == 120


def test_filtro_parte_usa_campo_texto_com_AND():
    """`partes` (texto, 100% dos 71,18M docs), não o nested `participacoes`.

    Medido em 12/08/2026: o nested só existe em 1,9% da base — filtrar por ele
    daria 53.577 processos do INSS quando são 4.402.239 (82× menos).
    E o operador é AND: sem ele, "Fazenda São Paulo" traria todo processo que
    cite "estado", e com match_phrase perderia as grafias fora de ordem.
    """
    cl = agg.build_filter_clauses({'parte': 'Instituto Nacional do Seguro Social'})
    m = [c for c in cl if 'match' in c]
    assert len(m) == 1
    assert 'partes' in m[0]['match']
    assert m[0]['match']['partes']['operator'] == 'and'
    # nada de nested: quebraria a cobertura
    assert not any('nested' in c for c in cl)


def test_filtro_parte_combina_com_os_outros():
    cl = agg.build_filter_clauses({'parte': 'INSS', 'uf': 'MG', 'tem_sinal': True})
    tipos = [list(c)[0] for c in cl]
    assert tipos.count('match') == 1 and tipos.count('term') == 2


def test_valor_fora_da_formula_mas_no_bucket():
    """Valor saiu do score (era `valor/valor_max` e punia quem publicava: o MT,
    com 24% do valor de AL, caía 14 posições atrás de 23 UFs que não publicam
    nada). Continua no bucket porque a lista mostra R$ em cada linha.
    """
    base = dict(volume=1000, potencial=100, cobertura_pct=0, valor_max=1e12)
    _, sem_valor = agg.calcular_score_foco(valor=0.0, **base)
    _, valor_pequeno = agg.calcular_score_foco(valor=3.2e11, **base)
    _, valor_lider = agg.calcular_score_foco(valor=1e12, **base)
    assert sem_valor == valor_pequeno == valor_lider == 0.1


def test_amostra_minuscula_nao_lidera_o_ranking():
    """Com filtro por parte aparecem UFs com 1-2 processos.

    1 possível em 1 processo = 100% de densidade e o estado ficava em 1º com
    "prioridade 100" — em cima de uma amostra de UM. O prior puxa amostra
    pequena pra densidade típica do recorte; quem tem volume real não sente.
    """
    buckets = [
        # RO: amostra grande, densidade alta de verdade (11,15%)
        {'uf': 'RO', 'volume': 1_072_089, 'valor': 0.0,
         'potencial': 119_586, 'confirmado': 1_948, 'todos': 119_586},
        # MG: amostra grande, densidade baixa (0,02%) — puxa a média do recorte
        {'uf': 'MG', 'volume': 4_671_972, 'valor': 0.0,
         'potencial': 814, 'confirmado': 3_238, 'todos': 4_022},
        # AC: UM processo, que por acaso é possível
        {'uf': 'AC', 'volume': 1, 'valor': 0.0,
         'potencial': 1, 'confirmado': 0, 'todos': 1},
    ]
    out = agg._enriquecer_buckets(buckets, 'uf', {})
    ro = next(b for b in out if b['uf'] == 'RO')
    ac = next(b for b in out if b['uf'] == 'AC')

    # a taxa CRUA continua honesta — é o que a tela exibe
    assert ac['densidade'] == 1.0
    # mas o RANKING não deixa 1 processo liderar
    assert ro['score_foco'] > ac['score_foco'], '1 processo liderou o ranking'
    assert out[0]['uf'] == 'RO'
    # e quem tem volume real quase não sente o amortecimento
    assert ro['score_foco'] == pytest.approx(ro['densidade'], abs=0.001)
    # o estado de amostra 1 é tratado como MEDIANO (a densidade típica do
    # recorte), não como campeão nem como zero
    media = 120_401 / 5_744_062          # 2,1% neste conjunto
    assert ac['score_foco'] == pytest.approx(media, abs=0.005)
