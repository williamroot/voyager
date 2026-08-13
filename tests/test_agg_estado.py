"""Testes do serviço da página de ESTADO do Mapa Comercial (search/agg_estado.py).

Não dependem de ES real: mockam o cliente (`search.agg_estado.get_es`) e o cache
de cobertura. Cobrem o que o QA adversarial cobra:
  - fusão de grafias da MESMA entidade (o INSS vira 3 buckets no `.raw`);
  - descarte de não-ente público no polo passivo (pessoa física do processo);
  - `todos` = UNIÃO (bool.should), nunca soma;
  - UF inválida → 400 / ES fora → 503 (nunca 500 cru);
  - `cobertura_amostra` presente em TODO bloco de campo parcial.

Os números das grafias vêm de medição real no ES de prod (12/08/2026).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from search import agg_estado as ae


# --------------------------------------------------------------------------- #
# Helpers — envelopes ES
# --------------------------------------------------------------------------- #
def _qd(**kwargs):
    return SimpleNamespace(get=lambda k, default=None: kwargs.get(k, default))


def _bucket_metricas(volume, valor=0.0, possiveis=0, confirmados=0,
                     sinal_conhecido=None, todos=None):
    return {
        'doc_count': volume,
        'valor': {'value': valor},
        'potencial': {'doc_count': possiveis},
        'sinal_conhecido': {'doc_count': volume if sinal_conhecido is None
                            else sinal_conhecido},
        'confirmado': {'doc_count': confirmados},
        'todos': {'doc_count': max(possiveis, confirmados) if todos is None else todos},
    }


def _resp_estado(volume=1000, valor=500.0, valor_conhecido=100, possiveis=200,
                 confirmados=50, todos=None, sinal_conhecido=None,
                 tribunais=('TJSP',)):
    aggs = _bucket_metricas(volume, valor, possiveis, confirmados,
                            sinal_conhecido, todos)
    aggs.pop('doc_count')
    aggs['valor_conhecido'] = {'doc_count': valor_conhecido}
    aggs['tribunais'] = {'buckets': [{'key': t, 'doc_count': 1} for t in tribunais]}
    return {'hits': {'total': {'value': volume}}, 'aggregations': aggs}


def _resp_escopo(volume=250, classes=(), classe_preenchida=None, anos=(),
                 ano_preenchido=None, tribunais=(), classificacoes=(),
                 classif_preenchida=None, entidades=(), participacoes_docs=None,
                 ente_flag=None, ente_docs=None, valor=0.0, valor_conhecido=0,
                 advogados=0):
    def _terms(pares):
        return {'buckets': [{'key': k, 'doc_count': v} for k, v in pares]}

    nomes = [{'key': nome, 'doc_count': part,
              'processos': {'doc_count': proc if proc is not None else part}}
             for nome, part, proc in entidades]
    return {
        'hits': {'total': {'value': volume}},
        'aggregations': {
            'valor': {'value': valor},
            'valor_conhecido': {'doc_count': valor_conhecido},
            'por_classe': _terms(classes),
            'classe_preenchida': {'doc_count': volume if classe_preenchida is None
                                  else classe_preenchida},
            'por_ano': _terms(anos),
            'ano_preenchido': {'doc_count': volume if ano_preenchido is None
                               else ano_preenchido},
            'por_tribunal': {'buckets': [
                dict(key=t, **_bucket_metricas(v, possiveis=p, confirmados=c))
                for t, v, p, c in tribunais]},
            'por_classificacao': _terms(classificacoes),
            'classificacao_preenchida': {
                'doc_count': volume if classif_preenchida is None
                else classif_preenchida},
            'participacoes_presentes': {
                'doc_count': volume if participacoes_docs is None
                else participacoes_docs},
            'ente_flag_conhecida': {'doc_count': volume if ente_flag is None
                                    else ente_flag},
            'entidades': {
                'doc_count': volume if ente_docs is None else ente_docs,
                'nested': {'passivo': {'nomes': {'buckets': nomes}},
                           'advogados': {'doc_count': advogados}},
            },
        },
    }


def _mock_es(resp_estado, resp_escopo):
    es = MagicMock()
    es.msearch.return_value = {'responses': [resp_estado, resp_escopo]}
    return es


#: TOP real medido no ES de prod (12/08/2026), polo passivo, nested `.raw`.
#: 3 grafias do INSS + 1 pessoa física (não é ente público).
ENTIDADES_REAIS = (
    ('Instituto Nacional do Seguro Social - INSS', 23171, 23171),
    ('Fazenda Pública do Estado de São Paulo', 19099, 19099),
    ('INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS', 17575, 17575),
    ('Procuradoria Federal nos Estados e no Distrito Federal', 10509, 10509),
    ('PREFEITURA MUNICIPAL DE SÃO PAULO', 7380, 7380),
    ('INSTITUTO NACIONAL DO SEGURO SOCIAL', 3617, 3617),
    ('UNIÃO FEDERAL', 3007, 3007),
    ('Daniel Gerber', 2636, 2636),
)


@pytest.fixture(autouse=True)
def _limpa_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


# --------------------------------------------------------------------------- #
# Saneamento de entrada
# --------------------------------------------------------------------------- #
def test_normalizar_uf_aceita_minuscula_e_fed():
    assert ae.normalizar_uf('sp') == 'SP'
    assert ae.normalizar_uf(' ro ') == 'RO'
    assert ae.normalizar_uf('fed') == 'FED'


@pytest.mark.parametrize('uf', ['XX', '', None, 'BRASIL', 'S'])
def test_normalizar_uf_invalida_levanta(uf):
    with pytest.raises(ae.UfInvalida):
        ae.normalizar_uf(uf)


def test_normalizar_metrica_default_e_saneamento():
    assert ae.normalizar_metrica(None) == 'todos'
    assert ae.normalizar_metrica('lixo') == 'todos'
    assert ae.normalizar_metrica('POSSIVEIS') == 'possiveis'
    assert ae.normalizar_metrica('confirmados') == 'confirmados'


def test_reexporta_parse_filtros_do_mapa():
    """Os filtros são os MESMOS do mapa — reuso, não cópia."""
    from search import agg_overview
    assert ae.parse_filtros is agg_overview.parse_filtros


# --------------------------------------------------------------------------- #
# Normalização / fusão de grafias
# --------------------------------------------------------------------------- #
def test_normalizar_entidade_funde_grafias_do_inss():
    chaves = {ae.normalizar_entidade(n) for n in (
        'Instituto Nacional do Seguro Social - INSS',
        'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
        'INSTITUTO NACIONAL DO SEGURO SOCIAL',
        'INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))',
        'Instituto  Nacional  de Seguro Social/INSS',
    )}
    assert len(chaves) == 1


def test_normalizar_entidade_nao_funde_entidades_distintas():
    a = ae.normalizar_entidade('Município de Ribeirão Preto')
    b = ae.normalizar_entidade('MUNICÍPIO DE RIBEIRAO PIRES')
    c = ae.normalizar_entidade('Fazenda Pública do Estado de São Paulo')
    assert len({a, b, c}) == 3
    # acento/caixa NÃO distinguem
    assert a == ae.normalizar_entidade('MUNICIPIO DE RIBEIRAO PRETO')


def test_normalizar_entidade_lixo_nao_quebra():
    assert ae.normalizar_entidade('') == ''
    assert ae.normalizar_entidade(None) == ''
    assert ae.normalizar_entidade('---') == ''
    # só sigla: não some (fallback mantém os segmentos)
    assert ae.normalizar_entidade('INSS - SP') == 'INSS SP'


def test_fundir_entidades_inss_vira_um_bucket_de_44363():
    """3 grafias do INSS (23.171 + 17.575 + 3.617) = 44.363 num bucket só."""
    buckets = [{'key': n, 'doc_count': p, 'processos': {'doc_count': pr}}
               for n, p, pr in ENTIDADES_REAIS]
    out = ae.fundir_entidades(buckets)
    por_nome = {i['entidade']: i for i in out['itens']}

    inss = [i for i in out['itens'] if 'Seguro Social' in i['entidade']]
    assert len(inss) == 1, 'INSS tem que virar UM bucket'
    assert inss[0]['volume'] == 44363
    assert inss[0]['variantes'] == 3
    # rótulo canônico = grafia mais frequente (legível, não CAIXA ALTA)
    assert inss[0]['entidade'] == 'Instituto Nacional do Seguro Social - INSS'
    # e o INSS passa a liderar o ranking (antes fatiado em 3)
    assert out['itens'][0]['entidade'] == 'Instituto Nacional do Seguro Social - INSS'
    assert por_nome['Fazenda Pública do Estado de São Paulo']['volume'] == 19099
    # 8 buckets crus → 6 grupos (2 absorvidos pela fusão)
    assert out['buckets_brutos'] == 8
    assert out['buckets_fundidos'] == 2


def test_fundir_entidades_descarta_pessoa_fisica():
    """Filtro por doc (tem_ente_publico_passivo) traz PF do mesmo processo."""
    buckets = [{'key': n, 'doc_count': p, 'processos': {'doc_count': pr}}
               for n, p, pr in ENTIDADES_REAIS]
    out = ae.fundir_entidades(buckets)
    nomes = [i['entidade'] for i in out['itens']]
    assert 'Daniel Gerber' not in nomes
    assert out['descartados_nao_ente'] == 1
    assert out['descartados_nao_ente_volume'] == 2636
    # os entes de verdade continuam
    assert 'UNIÃO FEDERAL' in nomes
    assert 'Procuradoria Federal nos Estados e no Distrito Federal' in nomes


def test_fundir_entidades_usa_regex_canonica_do_estagio():
    """Gate primário do "é ente público?" é o RE_ENTE_PUBLICO do Estágio."""
    from tribunals.estagio import RE_ENTE_PUBLICO
    import search.agg_estado as mod
    assert mod.RE_ENTE_PUBLICO is RE_ENTE_PUBLICO
    assert ae.eh_ente_publico('Fazenda Pública do Estado de São Paulo') == (True, False)
    assert ae.eh_ente_publico('Daniel Gerber') == (False, False)


def test_complemento_pega_falso_negativo_institucional_da_regex_canonica():
    """A regex do Estágio derruba ente óbvio; o complemento recupera e MARCA.

    Medido 12/08: em FED ela descartava 153 dos 200 buckets (FUNASA 314
    processos, IBAMA, UnB, FNDE, SUFRAMA, DNIT, UFG, CEF, FUNAI); em MG,
    "ESTADO DE MINAS GERAIS" (o alternante `estado d` exige fronteira depois
    do "d"). Não dá pra corrigir em `tribunals/estagio.py`: é feature de modelo.
    """
    from tribunals.estagio import RE_ENTE_PUBLICO
    for nome in ('ESTADO DE MINAS GERAIS', 'FUNDACAO NACIONAL DE SAUDE',
                 'UNIVERSIDADE FEDERAL DE GOIAS', 'CAIXA ECONOMICA FEDERAL - CEF',
                 'DNIT-DEPARTAMENTO NACIONAL DE INFRAEST DE TRANSPORTES'):
        assert not RE_ENTE_PUBLICO.search(nome), nome   # a canônica erra
        assert ae.eh_ente_publico(nome) == (True, True), nome

    # e o complemento NÃO abre a porta pra empresa privada
    for nome in ('BANCO GM S.A', 'EPAVE SERVICOS E REPRESENTACOES LTDA - ME',
                 'Edelton Carbinatto', 'IPE COUNTRY CLUBE'):
        assert ae.eh_ente_publico(nome) == (False, False), nome


def test_fundir_entidades_marca_o_que_veio_do_complemento():
    buckets = [
        {'key': 'ESTADO DE MINAS GERAIS', 'doc_count': 21,
         'processos': {'doc_count': 21}},
        {'key': 'PREFEITURA MUNICIPAL DE SÃO PAULO', 'doc_count': 7380,
         'processos': {'doc_count': 7380}},
        {'key': 'BANCO GM S.A', 'doc_count': 5, 'processos': {'doc_count': 5}},
    ]
    out = ae.fundir_entidades(buckets)
    por_nome = {i['entidade']: i for i in out['itens']}
    assert por_nome['ESTADO DE MINAS GERAIS']['por_complemento'] is True
    assert por_nome['PREFEITURA MUNICIPAL DE SÃO PAULO']['por_complemento'] is False
    assert out['aceitos_por_complemento'] == 1
    assert out['descartados_nao_ente'] == 1     # BANCO GM fora


def test_rotulo_limpa_marcador_de_papel_do_pje():
    """'... (REQUERIDO(A))' é papel colado no nome pelo PJe — some do rótulo."""
    buckets = [
        {'key': 'INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))',
         'doc_count': 761, 'processos': {'doc_count': 760}},
        {'key': 'INSTITUTO NACIONAL DO SEGURO SOCIAL', 'doc_count': 8,
         'processos': {'doc_count': 8}},
    ]
    out = ae.fundir_entidades(buckets)
    assert len(out['itens']) == 1
    assert out['itens'][0]['entidade'] == 'INSTITUTO NACIONAL DO SEGURO SOCIAL'
    assert out['itens'][0]['volume'] == 768
    # a grafia crua continua disponível como evidência da fusão
    assert out['itens'][0]['grafias'][0]['nome'].endswith('(REQUERIDO(A))')


def test_fundir_entidades_volume_e_de_processos_nao_de_participacoes():
    buckets = [{'key': 'MUNICIPIO DE MANGA', 'doc_count': 90,
                'processos': {'doc_count': 71}}]
    out = ae.fundir_entidades(buckets)
    assert out['itens'][0]['volume'] == 71
    assert out['itens'][0]['participacoes'] == 90


def test_fundir_entidades_respeita_top_n():
    buckets = [{'key': f'MUNICIPIO DE CIDADE {i}', 'doc_count': 100 - i,
                'processos': {'doc_count': 100 - i}} for i in range(40)]
    out = ae.fundir_entidades(buckets)
    assert len(out['itens']) == ae.TOP_N


# --------------------------------------------------------------------------- #
# Montagem da query
# --------------------------------------------------------------------------- #
def test_clausula_metrica_todos_e_uniao_nunca_soma():
    c = ae.clausula_metrica('todos')
    b = c['bool']
    assert b['minimum_should_match'] == 1
    campos = {list(x['term'].keys())[0] for x in b['should']}
    assert campos == {'tem_sinal_precatorio', 'classificacao'}
    # e as lentes simples são term puro
    assert ae.clausula_metrica('possiveis') == {
        'term': {'tem_sinal_precatorio': True}}
    assert ae.clausula_metrica('confirmados') == {
        'term': {'classificacao': 'PRECATORIO'}}


def test_body_estado_aplica_uf_e_filtros():
    body = ae.build_body_estado('SP', {'ano_min': 2020})
    assert body['size'] == 0
    assert body['track_total_hits'] is True
    clauses = body['query']['bool']['filter']
    assert {'term': {'uf': 'SP'}} in clauses
    assert {'range': {'ano_cnj': {'gte': 2020}}} in clauses
    # resumo NÃO leva a lente da métrica
    assert ae.clausula_metrica('todos') not in clauses


def test_body_escopo_aplica_lente_e_blocos():
    body = ae.build_body_escopo('RO', {'tribunal': 'TJRO'}, 'confirmados')
    clauses = body['query']['bool']['filter']
    assert {'term': {'uf': 'RO'}} in clauses
    assert {'term': {'tribunal': 'TJRO'}} in clauses
    assert {'term': {'classificacao': 'PRECATORIO'}} in clauses

    aggs = body['aggs']
    assert aggs['por_classe']['terms']['field'] == 'classe_nome'
    # classe VAZIA não é "um tipo de processo" — sai do gráfico
    assert aggs['por_classe']['terms']['exclude'] == ['']
    assert aggs['por_ano']['terms']['field'] == 'ano_cnj'
    assert aggs['por_tribunal']['terms']['field'] == 'tribunal'
    assert aggs['por_classificacao']['terms']['field'] == 'classificacao'
    # nested da entidade devedora, com reverse_nested pra contar PROCESSOS
    ent = aggs['entidades']
    assert ent['filter'] == {'term': {'tem_ente_publico_passivo': True}}
    nomes = ent['aggs']['nested']['aggs']['passivo']['aggs']['nomes']
    assert nomes['terms']['field'] == 'participacoes.nome.raw'
    assert nomes['terms']['size'] == ae.ENTIDADES_TERMS_SIZE > ae.TOP_N
    assert nomes['aggs']['processos'] == {'reverse_nested': {}}
    # polo passivo, MENOS advogados (advogado do passivo representa o devedor)
    passivo = ent['aggs']['nested']['aggs']['passivo']['filter']['bool']
    assert passivo['filter'] == [{'term': {'participacoes.polo': 'passivo'}}]
    assert passivo['must_not'] == [{'term': {'participacoes.eh_advogado': True}}]


def test_preenchido_desconta_string_vazia():
    """`exists` sozinho mentiria: RO tem 69,6% de classe_nome = ''."""
    f = ae._preenchido('classe_nome')['filter']['bool']
    assert f['filter'] == [{'exists': {'field': 'classe_nome'}}]
    assert f['must_not'] == [{'term': {'classe_nome': ''}}]


# --------------------------------------------------------------------------- #
# agg_estado fim-a-fim (ES + cobertura mockados)
# --------------------------------------------------------------------------- #
@patch('search.agg_estado._cobertura_por_tribunal', return_value={'TJSP': 12.3})
@patch('search.agg_estado._cobertura_por_uf', return_value={'SP': 12.3})
@patch('search.agg_estado.get_es')
def test_agg_estado_payload_completo(mock_get_es, _cu, _ct):
    es = _mock_es(
        _resp_estado(volume=4253470, valor=1302599875615.84,
                     valor_conhecido=969590, possiveis=70259, confirmados=4378,
                     todos=71972, tribunais=('TJSP',)),
        _resp_escopo(
            volume=71972,
            classes=(('PRECATÓRIO', 32182), ('REQUISIÇÃO DE PEQUENO VALOR', 10188)),
            classe_preenchida=69088,
            # 9900 = CNJ malformado (existe de verdade no ES de SP)
            anos=((2024, 900), (2011, 136), (2023, 700), (9900, 4)),
            tribunais=(('TJSP', 71972, 70259, 4378),),
            classificacoes=(('NAO_LEAD', 59629), ('PRECATORIO', 4378)),
            classif_preenchida=71951,
            entidades=ENTIDADES_REAIS,
            participacoes_docs=20332, ente_docs=9896, advogados=4210),
    )
    mock_get_es.return_value = es

    out = ae.agg_estado('sp', {}, 'todos')

    # 1 round-trip ES (msearch), 2 sub-buscas
    assert es.msearch.call_count == 1
    corpo = es.msearch.call_args.kwargs['body']
    assert len(corpo) == 4          # header+body ×2

    assert out['uf'] == 'SP'
    assert out['uf_nome'] == 'São Paulo'
    assert out['metrica'] == 'todos'
    assert out['filtros']['uf'] == 'SP'
    assert out['gerado_em']

    r = out['resumo']
    assert r['volume'] == 4253470
    assert r['possiveis'] == 70259
    assert r['sinal_processado'] is True
    assert r['confirmados'] == 4378
    assert r['todos'] == 71972
    assert r['escopo_volume'] == 71972
    assert r['cobertura_pct'] == 12.3
    assert r['tribunais'] == 1
    assert r['valor'] == 1302599875615.84

    # por tipo de processo: top + cauda + vazios
    t = out['por_tipo_processo']
    assert t['itens'][0] == {'tipo': 'PRECATÓRIO', 'volume': 32182}
    assert t['sem_dado'] == 71972 - 69088
    assert t['outros'] == 69088 - (32182 + 10188)

    # série temporal ordenada por ANO (o ES devolve por contagem) e SEM lixo
    assert [i['ano'] for i in out['por_ano']['itens']] == [2011, 2023, 2024]
    assert out['por_ano']['fora_da_faixa'] == 4      # o bucket 9900 sai da série

    # tribunal com métricas + cobertura do cache
    tr = out['por_tribunal']['itens'][0]
    assert tr['tribunal'] == 'TJSP' and tr['cobertura_pct'] == 12.3
    assert tr['possiveis'] == 70259 and tr['confirmados'] == 4378

    # classificação
    assert out['por_classificacao']['itens'][0]['classificacao'] == 'NAO_LEAD'
    assert out['por_classificacao']['sem_dado'] == 71972 - 71951

    # entidade devedora: fundida, sem PF, com estatística do que foi tratado
    e = out['por_entidade_devedora']
    assert e['itens'][0]['entidade'] == 'Instituto Nacional do Seguro Social - INSS'
    assert e['itens'][0]['volume'] == 44363
    assert 'Daniel Gerber' not in [i['entidade'] for i in e['itens']]
    assert e['descartados_nao_ente'] == 1
    assert e['buckets_fundidos'] == 2
    assert e['advogados_excluidos'] == 4210
    assert e['docs_com_ente_publico'] == 9896
    assert e['flag_processada'] is True
    assert e['nota']


@patch('search.agg_estado._cobertura_por_tribunal', return_value={})
@patch('search.agg_estado._cobertura_por_uf', return_value={})
@patch('search.agg_estado.get_es')
def test_cobertura_amostra_em_todo_bloco_parcial(mock_get_es, _cu, _ct):
    """Sem `cobertura_amostra` o front não consegue dizer "amostra de X%"."""
    es = _mock_es(
        _resp_estado(volume=1000, valor_conhecido=28),
        _resp_escopo(volume=200, classe_preenchida=150, ano_preenchido=200,
                     classif_preenchida=100, participacoes_docs=20,
                     entidades=(('MUNICIPIO DE SAO PAULO', 5, 5),)),
    )
    mock_get_es.return_value = es
    out = ae.agg_estado('SP')

    for bloco in ('por_tipo_processo', 'por_ano', 'por_tribunal',
                  'por_classificacao', 'por_entidade_devedora'):
        cob = out[bloco]['cobertura_amostra']
        assert set(cob) == {'campo', 'docs_com_o_campo', 'total_do_escopo',
                            'total_do_estado', 'pct'}, bloco
        assert cob['total_do_estado'] == 1000, bloco
        assert cob['total_do_escopo'] == 200, bloco

    # os pcts contam a verdade: classe 75%, entidade 10%, valor 2,8%
    assert out['por_tipo_processo']['cobertura_amostra']['pct'] == 75.0
    assert out['por_entidade_devedora']['cobertura_amostra']['pct'] == 10.0
    assert out['por_classificacao']['cobertura_amostra']['pct'] == 50.0
    assert out['resumo']['cobertura_valor']['pct'] == 2.8


@patch('search.agg_estado._cobertura_por_tribunal', return_value={})
@patch('search.agg_estado._cobertura_por_uf', return_value={})
@patch('search.agg_estado.get_es')
def test_todos_e_uniao_nunca_soma_no_payload(mock_get_es, _cu, _ct):
    """Medido: 47.720 confirmados, só 6.421 com sinal ⇒ união ≠ soma."""
    es = _mock_es(
        _resp_estado(volume=1000, possiveis=70259, confirmados=4378, todos=71972),
        _resp_escopo(volume=71972),
    )
    mock_get_es.return_value = es
    r = ae.agg_estado('SP')['resumo']
    assert r['todos'] == 71972
    assert r['todos'] != r['possiveis'] + r['confirmados']
    assert r['todos'] >= max(r['possiveis'], r['confirmados'])
    # e o escopo `todos` bate com a união, não com a soma
    assert r['escopo_volume'] == r['todos']


@patch('search.agg_estado._cobertura_por_tribunal', return_value={})
@patch('search.agg_estado._cobertura_por_uf', return_value={})
@patch('search.agg_estado.get_es')
def test_sinal_nao_processado_vira_null_nao_zero(mock_get_es, _cu, _ct):
    es = _mock_es(
        _resp_estado(volume=1000, possiveis=0, sinal_conhecido=0, confirmados=12),
        _resp_escopo(volume=12),
    )
    mock_get_es.return_value = es
    r = ae.agg_estado('SP')['resumo']
    assert r['possiveis'] is None and r['sinal_processado'] is False
    assert r['confirmados'] == 12


@patch('search.agg_estado._cobertura_por_tribunal', return_value={})
@patch('search.agg_estado._cobertura_por_uf', return_value={})
@patch('search.agg_estado.get_es')
def test_estado_sem_participacoes_indexadas_e_honesto(mock_get_es, _cu, _ct):
    """RO: zero participação indexada — vazio é "não indexamos", não "não há"."""
    es = _mock_es(
        _resp_estado(volume=1072089, possiveis=119586, confirmados=1948,
                     todos=119586, tribunais=('TJRO',)),
        _resp_escopo(volume=119586, participacoes_docs=0, ente_flag=1987,
                     ente_docs=0, entidades=()),
    )
    mock_get_es.return_value = es
    e = ae.agg_estado('RO')['por_entidade_devedora']
    assert e['itens'] == []
    assert e['cobertura_amostra']['pct'] == 0.0
    assert 'indexad' in e['nota'].lower()


@patch('search.agg_estado._cobertura_por_tribunal', return_value={})
@patch('search.agg_estado._cobertura_por_uf', return_value={})
@patch('search.agg_estado.get_es')
def test_metrica_troca_a_lente_do_escopo(mock_get_es, _cu, _ct):
    es = _mock_es(_resp_estado(), _resp_escopo())
    mock_get_es.return_value = es
    ae.agg_estado('SP', {}, 'confirmados')
    corpo = es.msearch.call_args.kwargs['body']
    body_escopo = corpo[3]
    assert {'term': {'classificacao': 'PRECATORIO'}} in body_escopo['query']['bool']['filter']
    # o body do resumo (corpo[1]) NÃO leva a lente
    assert {'term': {'classificacao': 'PRECATORIO'}} not in corpo[1]['query']['bool']['filter']


@patch('search.agg_estado._cobertura_por_tribunal', return_value={})
@patch('search.agg_estado._cobertura_por_uf', return_value={})
@patch('search.agg_estado.get_es')
def test_cache_evita_segunda_ida_ao_es(mock_get_es, _cu, _ct):
    es = _mock_es(_resp_estado(), _resp_escopo())
    mock_get_es.return_value = es
    ae.agg_estado('SP', {}, 'todos')
    ae.agg_estado('sp', {}, 'todos')
    assert es.msearch.call_count == 1
    # métrica diferente = chave diferente
    ae.agg_estado('SP', {}, 'possiveis')
    assert es.msearch.call_count == 2


@patch('search.agg_estado.get_es')
def test_uf_invalida_nao_chama_es(mock_get_es):
    with pytest.raises(ae.UfInvalida):
        ae.agg_estado('XX')
    mock_get_es.assert_not_called()


@patch('search.agg_estado.get_es')
def test_erro_do_es_sobe_como_excecao(mock_get_es):
    es = MagicMock()
    es.msearch.return_value = {'responses': [
        {'error': {'type': 'search_phase_execution_exception'}},
        {'hits': {'total': {'value': 0}}, 'aggregations': {}},
    ]}
    mock_get_es.return_value = es
    with pytest.raises(RuntimeError):
        ae.agg_estado('SP')


# --------------------------------------------------------------------------- #
# View — status codes (nunca 500 cru)
# --------------------------------------------------------------------------- #
def _request_fake(**params):
    return SimpleNamespace(GET=_qd(**params))


def test_view_uf_invalida_400():
    from dashboard import overview_views as cv
    resp = cv.comercial_estado.__wrapped__.__wrapped__(_request_fake(), 'XX')
    assert resp.status_code == 400


@patch('search.agg_estado.agg_estado', side_effect=ConnectionError('ES fora'))
def test_view_es_fora_503(_mock):
    from dashboard import overview_views as cv
    resp = cv.comercial_estado.__wrapped__.__wrapped__(_request_fake(), 'SP')
    assert resp.status_code == 503


@patch('search.agg_estado.agg_estado')
def test_view_passa_filtros_e_metrica(mock_agg):
    from dashboard import overview_views as cv
    mock_agg.return_value = {'uf': 'SP'}
    req = _request_fake(metrica='possiveis', ano_min='2020', uf='RJ')
    resp = cv.comercial_estado.__wrapped__.__wrapped__(req, 'SP')
    assert resp.status_code == 200
    args = mock_agg.call_args.args
    assert args[0] == 'SP'                 # uf da ROTA manda
    assert args[1]['ano_min'] == 2020
    assert args[2] == 'possiveis'


def test_rota_resolve():
    from django.urls import resolve, reverse
    url = reverse('dashboard:overview-estado', kwargs={'uf': 'SP'})
    assert url.endswith('/api/overview/estado/SP/')
    assert resolve(url).func.__name__ == 'comercial_estado'
