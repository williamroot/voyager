"""Testes do serviço das telas de ENTIDADES (`search/agg_entidade.py`).

Não dependem de ES real: mockam o cliente (`search.agg_entidade.get_es`), no
mesmo padrão de `tests/test_agg_estado.py`. Cobrem o que o QA adversarial cobra:

  - `n_processos` AUSENTE vira `null` + "nao_contada" — NUNCA 0 (959 mil das
    1,14M entidades nunca foram contadas);
  - `nome_suspeito` e nome vazio ("(REQUERIDO(A))") fora da lista;
  - ausente ranqueia POR ÚLTIMO (`missing: _last`), não some e não vira topo;
  - a busca do ranking é `operator: and` — a rede de OR do autocomplete, com
    ordenação por contagem, devolvia o INSS pra "municipio de sao paulo";
  - `relevancia` pontua (texto em `must`); em `filter` o score sai 0 e a lista
    volta aleatória — bug medido no ES de prod;
  - cursor round-trip + cursor forjado/de outra ordenação → `CursorInvalido`;
  - a ficha usa o MESMO OR da contagem gravada (senão a lista mostra 4.402.239
    e a ficha mostra outro número);
  - `possiveis` = null quando o sinal nunca foi computado (≠ zero);
  - `cobertura_amostra` em TODO bloco parcial;
  - entidade inexistente → `EntidadeNaoEncontrada` (→ 404 na view).

Os números vêm de medição real no ES de prod (13/08/2026): INSS = 4.402.239
processos, 165.790 com sinal, R$ 6.092.037.156, FED 4.212.666 · MG 77.668 ·
SP 32.513 · MT 31.283 · MA 16.749.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from search import agg_entidade as ae


# --------------------------------------------------------------------------- #
# Helpers — envelopes ES
# --------------------------------------------------------------------------- #
def _qd(**kwargs):
    return SimpleNamespace(get=lambda k, default=None: kwargs.get(k, default))


#: o cadastro real do INSS no índice (recortado)
INSS = {
    'entidade_id': 'cnpj:29979036',
    'chave': 'cnpj',
    'raiz_cnpj': '29979036',
    'nome_canonico': 'INSTITUTO NACIONAL DO SEGURO SOCIAL',
    'nome_normalizado': 'INSTITUTO NACIONAL SEGURO SOCIAL',
    'variantes': ['INSTITUTO NACIONAL DO SEGURO SOCIAL',
                  'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
                  'Instituto Nacional do Seguro Social - INSS'],
    'variantes_n': [614, 17, 9],
    'variantes_busca': ['INSTITUTO NACIONAL DO SEGURO SOCIAL',
                        'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
                        'Instituto Nacional do Seguro Social - INSS'],
    'n_variantes': 104,
    'documentos': ['29.979.036/0001-40', '29.979.036/0002-21'],
    'n_documentos': 650,
    'documentos_secundarios': ['03.500.696/0001-03', '03.500.708/0001-08'],
    'n_documentos_secundarios': 4,
    'entidades_absorvidas': ['cnpj:03500696', 'cnpj:03500708'],
    'grupos_absorvidos': 5,
    'eh_ente_publico': True,
    'n_partes': 768,
    'n_processos': 4402239,
    'n_processos_em': '2026-08-12T23:26:57+00:00',
    'nome_suspeito': False,
    'tipo': 'pj',
}


def _hit(fonte, sort=None):
    h = {'_id': fonte.get('entidade_id'), '_source': fonte}
    if sort is not None:
        h['sort'] = sort
    return h


def _resp_ranking(fontes, total=None, contadas=None, indice=1141610, sorts=None):
    sorts = sorts or [[i, f.get('entidade_id')] for i, f in enumerate(fontes)]
    return {
        'hits': {'total': {'value': total if total is not None else len(fontes)},
                 'hits': [_hit(f, s) for f, s in zip(fontes, sorts, strict=False)]},
        'aggregations': {
            'contadas': {'doc_count': contadas if contadas is not None
                         else sum(1 for f in fontes
                                  if f.get('n_processos') is not None)},
            'indice': {'doc_count': indice},
        },
    }


def _resp_ficha(total=4402239, valor=6092037156.0, valor_conhecido=64527,
                possiveis=165790, sinal_conhecido=166963, confirmados=18765,
                todos=184037, ufs=(('FED', 4212666), ('MG', 77668),
                                   ('SP', 32513), ('MT', 31283), ('MA', 16749)),
                tribunais=(('TRF3', 1842369), ('TJMG', 77668)),
                anos=((2026, 815483), (2025, 1252342), (1900, 27)),
                classificacoes=(('NAO_LEAD', 3671850), ('PRECATORIO', 18765)),
                classif_preenchida=None, ano_preenchido=None):
    def _terms(pares):
        return {'buckets': [{'key': k, 'doc_count': v} for k, v in pares]}

    return {
        'hits': {'total': {'value': total}},
        'aggregations': {
            'valor': {'value': valor},
            'valor_conhecido': {'doc_count': valor_conhecido},
            'potencial': {'doc_count': possiveis},
            'sinal_conhecido': {'doc_count': sinal_conhecido},
            'confirmado': {'doc_count': confirmados},
            'todos': {'doc_count': todos},
            'por_uf': _terms(ufs),
            'uf_conhecida': {'doc_count': total},
            'por_tribunal': _terms(tribunais),
            'tribunal_conhecido': {'doc_count': total},
            'por_ano': _terms(anos),
            'ano_preenchido': {'doc_count': total if ano_preenchido is None
                               else ano_preenchido},
            'por_classificacao': _terms(classificacoes),
            'classificacao_preenchida': {
                'doc_count': total if classif_preenchida is None
                else classif_preenchida},
        },
    }


def _mock_es(search=None, msearch=None):
    es = MagicMock()
    if search is not None:
        if isinstance(search, list):
            es.search.side_effect = search
        else:
            es.search.return_value = search
    if msearch is not None:
        es.msearch.return_value = {'responses': msearch}
    return es


@pytest.fixture(autouse=True)
def _limpa_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


# --------------------------------------------------------------------------- #
# Reuso — nada aqui pode ser cópia do mapa
# --------------------------------------------------------------------------- #
def test_reexporta_parse_filtros_do_mapa():
    """A ficha usa os MESMOS filtros do mapa — reuso, não cópia."""
    from search import agg_comercial
    assert ae.parse_filtros is agg_comercial.parse_filtros


def test_reusa_o_dono_do_cadastro_para_montar_o_or():
    """O OR e a poda de over-match são do `search.entidades`, não daqui."""
    from search import entidades
    assert ae.ent is entidades


# --------------------------------------------------------------------------- #
# Saneamento de entrada
# --------------------------------------------------------------------------- #
def test_parse_ranking_defaults():
    o = ae.parse_ranking(_qd())
    assert o == {'q': '', 'ente': False, 'min_partes': 0,
                 'somente_contadas': False, 'ordenar': 'n_processos',
                 'tamanho': ae.RANKING_TAMANHO_DEFAULT, 'cursor': None,
                 'offset': 0}


@pytest.mark.parametrize('valor,esperado', [
    ('999', ae.RANKING_TAMANHO_MAX), ('0', 1), ('-3', 1), ('lixo', 20),
    ('30', 30),
])
def test_parse_ranking_clampa_tamanho(valor, esperado):
    assert ae.parse_ranking(_qd(n=valor))['tamanho'] == esperado


def test_parse_ranking_aceita_tamanho_como_apelido_de_n():
    """O front da lista manda `tamanho=30`; o autocomplete manda `n`."""
    assert ae.parse_ranking(_qd(tamanho='30'))['tamanho'] == 30
    # os dois juntos: `n` ganha (é o nome do contrato antigo, já em uso)
    assert ae.parse_ranking(_qd(n='7', tamanho='30'))['tamanho'] == 7


def test_parse_ranking_clampa_min_partes_e_offset():
    assert ae.parse_ranking(_qd(min_partes='-5'))['min_partes'] == 0
    assert ae.parse_ranking(_qd(min_partes='999999'))['min_partes'] == ae.MIN_PARTES_MAX
    assert ae.parse_ranking(_qd(offset='999999'))['offset'] == ae.OFFSET_MAX


def test_ordenacao_desconhecida_cai_no_default():
    assert ae.normalizar_ordenacao('lixo') == 'n_processos'
    assert ae.normalizar_ordenacao(None) == 'n_processos'
    assert ae.normalizar_ordenacao('N_PARTES') == 'n_partes'
    assert ae.normalizar_ordenacao('n_variantes') == 'n_variantes'


def test_relevancia_sem_termo_cai_no_default():
    """Ordenar 1,14M entidades por relevância de query vazia é ordenar por nada."""
    assert ae.normalizar_ordenacao('relevancia', tem_busca=False) == 'n_processos'
    assert ae.normalizar_ordenacao('relevancia', tem_busca=True) == 'relevancia'
    assert ae.parse_ranking(_qd(ordenar='relevancia', q='in'))['ordenar'] == 'n_processos'
    assert ae.parse_ranking(_qd(ordenar='relevancia', q='inss'))['ordenar'] == 'relevancia'


# --------------------------------------------------------------------------- #
# Cursor
# --------------------------------------------------------------------------- #
def test_cursor_round_trip():
    c = ae.codificar_cursor('n_processos', [910751, 'nome:abc'])
    assert ae.decodificar_cursor(c, 'n_processos') == [910751, 'nome:abc']


@pytest.mark.parametrize('cursor', ['lixo!!', '', 'YWJj', 'eyJvIjoieCJ9'])
def test_cursor_forjado_levanta(cursor):
    with pytest.raises(ae.CursorInvalido):
        ae.decodificar_cursor(cursor, 'n_processos')


def test_cursor_de_outra_ordenacao_levanta():
    """Cursor de outra ordem daria uma página silenciosamente errada."""
    c = ae.codificar_cursor('n_processos', [1, 'x'])
    with pytest.raises(ae.CursorInvalido):
        ae.decodificar_cursor(c, 'n_partes')


# --------------------------------------------------------------------------- #
# Query do ranking
# --------------------------------------------------------------------------- #
def _must_not(body):
    return body['query']['bool']['must_not']


def test_ranking_exclui_nome_suspeito_e_nome_vazio():
    """"JOSÉ" casava 1.796.174 processos; "(REQUERIDO(A))" não é entidade."""
    body = ae.build_body_ranking(ae.parse_ranking(_qd()))
    assert {'term': {'nome_suspeito': True}} in _must_not(body)
    assert {'term': {'nome_normalizado': ''}} in _must_not(body)


def test_exclusao_e_aditiva_doc_sem_o_campo_continua_na_lista():
    """`must_not term` (não `must_not exists`): índice antigo não some."""
    for clausula in _must_not(ae.build_body_ranking(ae.parse_ranking(_qd()))):
        assert 'exists' not in clausula


def test_ranking_ordena_com_ausente_por_ultimo_e_desempate_estavel():
    body = ae.build_body_ranking(ae.parse_ranking(_qd()))
    assert body['sort'][0] == {'n_processos': {'order': 'desc',
                                               'missing': '_last'}}
    # sem o desempate, `search_after` pula/repete entidades empatadas — e
    # empate em n_processos é comum entre facetas da mesma entidade
    assert body['sort'][1] == {'entidade_id': 'asc'}


def test_ordenar_por_nome_usa_o_normalizado_e_e_crescente():
    """`nome_canonico.raw` é case/acento-sensível: "Álvaro" cairia após "Zebra"."""
    body = ae.build_body_ranking(ae.parse_ranking(_qd(ordenar='nome')))
    assert body['sort'][0] == {'nome_normalizado': {'order': 'asc',
                                                    'missing': '_last'}}


def test_filtros_do_ranking_viram_clausulas():
    body = ae.build_body_ranking(ae.parse_ranking(
        _qd(ente='1', min_partes='10', contadas='1')))
    filtros = body['query']['bool']['filter']
    assert {'term': {'eh_ente_publico': True}} in filtros
    assert {'range': {'n_partes': {'gte': 10}}} in filtros
    assert {'exists': {'field': 'n_processos'}} in filtros


def test_busca_do_ranking_e_and_estrito():
    """Medido: com a rede de OR do autocomplete, ordenar por `n_processos`
    devolvia INSTITUTO NACIONAL DO SEGURO SOCIAL pra "municipio de sao paulo"
    (casava "de"/"sao" e ganhava no volume): 368.905 "resultados" contra 237."""
    q = ae.query_texto('municipio de sao paulo')
    for clausula in q['bool']['should']:
        assert clausula['multi_match']['operator'] == 'and'
    campos = [c['multi_match']['fields'][0] for c in q['bool']['should']]
    assert 'nome_canonico.autocomplete' in campos
    assert 'variantes_busca.autocomplete' in campos


def test_busca_curta_nao_filtra_e_a_lista_volta_global():
    body = ae.build_body_ranking(ae.parse_ranking(_qd(q='in')))
    assert 'filter' not in body['query']['bool']
    assert 'must' not in body['query']['bool']


def test_relevancia_pontua_o_texto_em_must_nao_em_filter():
    """Em `filter` o _score sai 0 e o function_score multiplica zero — medido
    no ES de prod: a lista voltava em ordem aleatória, com score 0.0."""
    body = ae.build_body_ranking(ae.parse_ranking(
        _qd(q='inss', ordenar='relevancia')))
    fs = body['query']['function_score']
    assert fs['query']['bool']['must']            # texto PONTUA
    assert 'filter' not in fs['query']['bool']
    assert body['sort'][0] == '_score'
    # ranking calibrado do autocomplete, não um inventado aqui
    from search import entidades
    assert fs['functions'] == entidades.funcoes_prevalencia()


def test_paginacao_cursor_e_offset():
    c = ae.codificar_cursor('n_processos', [123, 'cnpj:1'])
    body = ae.build_body_ranking({**ae.parse_ranking(_qd()), 'cursor': c})
    assert body['search_after'] == [123, 'cnpj:1']
    body2 = ae.build_body_ranking(ae.parse_ranking(_qd(offset='40')))
    assert body2['from'] == 40
    assert 'search_after' not in body2


def test_ranking_pede_total_exato():
    """Sem `track_total_hits` o ES 8 pararia em 10.000 e a tela diria "10.000"."""
    assert ae.build_body_ranking(ae.parse_ranking(_qd()))['track_total_hits'] is True


# --------------------------------------------------------------------------- #
# Ranking — payload
# --------------------------------------------------------------------------- #
NAO_CONTADA = {
    'entidade_id': 'nome:abc123', 'chave': 'nome',
    'nome_canonico': 'MUNICIPIO DE ITAPEVA', 'n_partes': 2, 'n_variantes': 2,
    'eh_ente_publico': True, 'variantes_busca': ['MUNICIPIO DE ITAPEVA'],
}


@patch('search.agg_entidade.get_es')
def test_ranking_contagem_ausente_e_null_nunca_zero(mock_es):
    """959 mil entidades nunca foram contadas. Zero seria uma AFIRMAÇÃO."""
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS, NAO_CONTADA]))
    p = ae.ranking_entidades(ae.parse_ranking(_qd()))

    inss, outra = p['itens']
    assert inss['n_processos'] == 4402239
    assert inss['contagem'] == 'medida'
    assert outra['n_processos'] is None
    assert outra['contagem'] == 'nao_contada'
    assert 'não contamos ainda' in p['nota'] or 'NÃO foram contadas' in p['nota']


@patch('search.agg_entidade.get_es')
def test_ranking_expoe_a_confianca_da_fusao(mock_es):
    """`chave=cnpj` é identidade PROVADA; `nome` é heurística de grafia."""
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS, NAO_CONTADA]))
    inss, outra = ae.ranking_entidades(ae.parse_ranking(_qd()))['itens']
    assert (inss['chave'], inss['confianca']) == ('cnpj', 'forte')
    assert (outra['chave'], outra['confianca']) == ('nome', 'fraca')
    # atestação (o que separa a autarquia da faceta de 1 linha) vai junto
    assert inss['n_partes'] == 768
    # e a evidência de erro de cadastro do tribunal também
    assert inss['n_documentos_secundarios'] == 4


@patch('search.agg_entidade.get_es')
def test_ranking_cobertura_da_contagem_e_honesta(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking(
        [INSS, NAO_CONTADA], total=1140972, contadas=182026, indice=1141610))
    p = ae.ranking_entidades(ae.parse_ranking(_qd()))
    cob = p['cobertura_contagem']
    assert cob['docs_com_o_campo'] == 182026
    assert cob['total_do_escopo'] == 1140972
    assert cob['total_do_indice'] == 1141610
    assert cob['pct'] == 16.0
    assert p['total'] == 1140972


@patch('search.agg_entidade.get_es')
def test_ranking_cursor_da_proxima_pagina_sai_do_ultimo_hit(mock_es):
    fontes = [INSS, NAO_CONTADA]
    mock_es.return_value = _mock_es(search=_resp_ranking(
        fontes, sorts=[[4402239, 'cnpj:29979036'], [910751, 'nome:abc123']]))
    p = ae.ranking_entidades(ae.parse_ranking(_qd(n='2')))
    assert p['tem_mais'] is True
    assert ae.decodificar_cursor(p['proximo_cursor'], 'n_processos') == \
        [910751, 'nome:abc123']
    # alias de compatibilidade com a 1ª versão do front
    assert p['cursor_proximo'] == p['proximo_cursor']


@patch('search.agg_entidade.get_es')
def test_ranking_ultima_pagina_nao_tem_cursor(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]))
    p = ae.ranking_entidades(ae.parse_ranking(_qd(n='20')))
    assert p['proximo_cursor'] is None and p['tem_mais'] is False


@patch('search.agg_entidade.get_es')
def test_ranking_diz_que_nao_filtra_por_uf(mock_es):
    """Não oferecer é decisão medida — e tem que estar ESCRITO no payload."""
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]))
    p = ae.ranking_entidades(ae.parse_ranking(_qd()))
    assert 'uf' in p['exclusoes']
    assert 'estado' in p['exclusoes']['uf']


@patch('search.agg_entidade.get_es')
def test_ranking_busca_curta_avisa_o_motivo(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]))
    p = ae.ranking_entidades(ae.parse_ranking(_qd(q='in')))
    assert p['busca'] == {'termo': 'in', 'aplicada': False,
                          'motivo': 'termo_curto',
                          'min_caracteres': ae.BUSCA_MIN_CARACTERES}


@patch('search.agg_entidade.get_es')
def test_ranking_cursor_invalido_levanta_antes_de_tocar_o_es(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]))
    with pytest.raises(ae.CursorInvalido):
        ae.ranking_entidades({**ae.parse_ranking(_qd()), 'cursor': 'lixo!!'})
    mock_es.return_value.search.assert_not_called()


@patch('search.agg_entidade.get_es')
def test_ranking_usa_cache(mock_es):
    es = _mock_es(search=_resp_ranking([INSS]))
    mock_es.return_value = es
    ae.ranking_entidades(ae.parse_ranking(_qd()))
    ae.ranking_entidades(ae.parse_ranking(_qd()))
    assert es.search.call_count == 1


# --------------------------------------------------------------------------- #
# Ficha — o OR
# --------------------------------------------------------------------------- #
def test_grafias_da_consulta_sao_as_da_contagem_gravada():
    """Se divergir, a lista mostra 4.402.239 e a ficha mostra outro número."""
    assert ae.grafias_da_consulta(INSS) == INSS['variantes_busca']


def test_grafias_da_consulta_poda_over_match():
    """Grafia curta que é SUB-FRASE de outra e não é mais frequente é ruído.

    Medido na 1ª contagem: "PROCURADORIA" (1 linha) dentro de "Procuradoria -
    Allianz" marcava 7.222.852 processos — as procuradorias do país inteiro.
    """
    fonte = {
        'variantes': ['PROCURADORIA', 'Procuradoria - Allianz'],
        'variantes_n': [1, 3],
        'variantes_busca': ['PROCURADORIA', 'Procuradoria - Allianz'],
    }
    assert ae.grafias_da_consulta(fonte) == ['Procuradoria - Allianz']


def test_grafias_da_consulta_cai_no_nome_canonico():
    assert ae.grafias_da_consulta(
        {'nome_canonico': 'MUNICIPIO DE ITAPEVA'}) == ['MUNICIPIO DE ITAPEVA']


def test_body_da_ficha_e_or_de_match_phrase_com_total_exato():
    body = ae.build_body_ficha(INSS, {})
    clausulas = body['query']['bool']['filter']
    ors = clausulas[0]['bool']['should']
    assert all('match_phrase' in c for c in ors)
    assert {'match_phrase': {'partes': INSS['variantes_busca'][0]}} in ors
    assert body['size'] == 0 and body['track_total_hits'] is True


def test_body_da_ficha_aplica_os_filtros_do_mapa():
    from search.agg_comercial import parse_filtros
    filtros = parse_filtros(_qd(uf='SP', ano_min='2024'))
    clausulas = ae.build_body_ficha(INSS, filtros)['query']['bool']['filter']
    assert {'term': {'uf': 'SP'}} in clausulas
    assert {'range': {'ano_cnj': {'gte': 2024}}} in clausulas


# --------------------------------------------------------------------------- #
# Ficha — payload
# --------------------------------------------------------------------------- #
@patch('search.agg_entidade.get_es')
def test_ficha_do_inss_bate_os_numeros_medidos(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    f = ae.ficha_entidade('cnpj:29979036')
    r = f['resumo']
    assert r['processos'] == 4402239
    assert r['processos_entidade'] == 4402239
    assert r['possiveis'] == 165790
    assert r['confirmados'] == 18765
    assert r['valor'] == 6092037156.0
    assert r['n_processos_indice'] == 4402239
    assert r['divergencia_contagem'] == 0
    assert [(i['uf'], i['volume']) for i in f['por_uf']['itens'][:3]] == \
        [('FED', 4212666), ('MG', 77668), ('SP', 32513)]
    assert f['entidade']['nome_canonico'] == 'INSTITUTO NACIONAL DO SEGURO SOCIAL'


@patch('search.agg_entidade.get_es')
def test_ficha_possiveis_null_quando_o_sinal_nunca_foi_computado(mock_es):
    """Desconhecido ≠ zero: o backfill do sinal cobre 3,4% da base."""
    mock_es.return_value = _mock_es(
        search=_resp_ranking([INSS]),
        msearch=[_resp_ficha(possiveis=0, sinal_conhecido=0)])
    r = ae.ficha_entidade('cnpj:29979036')['resumo']
    assert r['possiveis'] is None
    assert r['sinal_processado'] is False


@patch('search.agg_entidade.get_es')
def test_ficha_expoe_cobertura_do_valor_e_do_sinal(mock_es):
    """`valor_causa` em 1,5% e sinal em 3,8% dos processos do INSS."""
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    r = ae.ficha_entidade('cnpj:29979036')['resumo']
    assert r['cobertura_valor']['pct'] == 1.5
    assert r['cobertura_valor']['campo'] == 'valor_causa'
    assert r['cobertura_sinal']['pct'] == 3.8
    # o denominador tem nome de ENTIDADE, não de estado (o bloco é reusado)
    assert r['cobertura_valor']['total_do_universo'] == 4402239
    assert 'total_do_estado' not in r['cobertura_valor']


@patch('search.agg_entidade.get_es')
def test_ficha_tem_cobertura_amostra_em_todo_bloco(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    f = ae.ficha_entidade('cnpj:29979036')
    for bloco in ('por_uf', 'por_tribunal', 'por_ano', 'por_classificacao'):
        cob = f[bloco]['cobertura_amostra']
        assert cob['total_do_escopo'] == 4402239
        assert 'total_do_universo' in cob and 'total_do_estado' not in cob


@patch('search.agg_entidade.get_es')
def test_ficha_tira_ano_malformado_da_serie(mock_es):
    """CNJ malformado gera 1900/9900 — fora da série, mas contabilizado."""
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    f = ae.ficha_entidade('cnpj:29979036')
    anos = [i['ano'] for i in f['por_ano']['itens']]
    assert anos == sorted(anos)             # série temporal, não por contagem
    assert 1900 not in anos
    assert f['por_ano']['fora_da_faixa'] == 27


@patch('search.agg_entidade.get_es')
def test_ficha_declara_como_o_numero_foi_obtido(mock_es):
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    consulta = ae.ficha_entidade('cnpj:29979036')['consulta']
    assert consulta['campo'] == 'partes'
    assert consulta['metodo'] == 'match_phrase_or'
    assert consulta['grafias'] == INSS['variantes_busca']
    assert consulta['n_grafias'] == 3


@patch('search.agg_entidade.get_es')
def test_ficha_expoe_a_auditoria_da_fusao(mock_es):
    """CNPJ que o tribunal digitou errado é evidência — não se esconde."""
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    e = ae.ficha_entidade('cnpj:29979036')['entidade']
    assert e['documentos_secundarios'] == INSS['documentos_secundarios']
    assert e['entidades_absorvidas'] == INSS['entidades_absorvidas']
    assert e['n_documentos'] == 650
    assert e['confianca'] == 'forte'


@patch('search.agg_entidade.get_es')
def test_ficha_com_filtro_traz_o_denominador_da_entidade_inteira(mock_es):
    """"23.795 de 4.402.239" só existe com a 2ª sub-busca (msearch)."""
    from search.agg_comercial import parse_filtros
    es = _mock_es(search=_resp_ranking([INSS]),
                  msearch=[_resp_ficha(total=23795),
                           {'hits': {'total': {'value': 4402239}}}])
    mock_es.return_value = es
    f = ae.ficha_entidade('cnpj:29979036', parse_filtros(_qd(uf='SP')))
    assert f['resumo']['processos'] == 23795
    assert f['resumo']['processos_entidade'] == 4402239
    assert f['filtros'] == {'uf': 'SP'}
    # 1 round-trip HTTP, 2 sub-buscas
    assert es.msearch.call_count == 1


@patch('search.agg_entidade.get_es')
def test_ficha_ignora_parte_e_entidade_id_da_querystring(mock_es):
    """Quem manda é a rota — senão a ficha do INSS mostraria outra entidade."""
    from search.agg_comercial import parse_filtros
    mock_es.return_value = _mock_es(search=_resp_ranking([INSS]),
                                    msearch=[_resp_ficha()])
    filtros = parse_filtros(_qd(parte='caixa', entidade_id='cnpj:00360305'))
    f = ae.ficha_entidade('cnpj:29979036', filtros)
    assert f['filtros'] == {}
    assert f['entidade']['entidade_id'] == 'cnpj:29979036'


@patch('search.agg_entidade.get_es')
def test_ficha_de_entidade_inexistente_levanta(mock_es):
    mock_es.return_value = _mock_es(
        search={'hits': {'total': {'value': 0}, 'hits': []}})
    with pytest.raises(ae.EntidadeNaoEncontrada):
        ae.ficha_entidade('cnpj:99999999')


@patch('search.agg_entidade.get_es')
def test_ficha_avisa_quando_a_entidade_e_fraca(mock_es):
    fraca = {**INSS, 'chave': 'nome', 'raiz_cnpj': None, 'n_partes': 1,
             'entidade_id': 'nome:abc'}
    mock_es.return_value = _mock_es(search=_resp_ranking([fraca]),
                                    msearch=[_resp_ficha()])
    f = ae.ficha_entidade('nome:abc')
    assert 'NOME' in f['nota'] and 'Atestação baixa' in f['nota']


@patch('search.agg_entidade.get_es')
def test_ficha_usa_cache(mock_es):
    es = _mock_es(search=_resp_ranking([INSS]), msearch=[_resp_ficha()])
    mock_es.return_value = es
    ae.ficha_entidade('cnpj:29979036')
    ae.ficha_entidade('cnpj:29979036')
    assert es.msearch.call_count == 1


# --------------------------------------------------------------------------- #
# Falhas de ES viram exceção (a view traduz em 503)
# --------------------------------------------------------------------------- #
@patch('search.agg_entidade.get_es')
def test_es_fora_sobe_excecao_no_ranking(mock_es):
    es = MagicMock()
    es.search.side_effect = RuntimeError('connection refused')
    mock_es.return_value = es
    with pytest.raises(RuntimeError):
        ae.ranking_entidades(ae.parse_ranking(_qd()))


@patch('search.agg_entidade.get_es')
def test_msearch_com_erro_sobe_excecao(mock_es):
    mock_es.return_value = _mock_es(
        search=_resp_ranking([INSS]),
        msearch=[{'error': {'type': 'search_phase_execution_exception'}}])
    with pytest.raises(RuntimeError):
        ae.ficha_entidade('cnpj:29979036')
