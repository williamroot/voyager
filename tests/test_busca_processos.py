"""Busca de processos da TELA — normalização de documento, vara, cobertura.

O que estes testes travam (tudo medido no ES de prod em 13/08/2026, os números
estão nas docstrings de `search/busca_ui.py` e `search/busca_api.py`):

  MÁSCARA   O índice guarda `29.979.036/0001-40`; `regexp` de 14 dígitos puros
            devolve ZERO documentos. Quem digita `29979036000140` tem de achar
            os MESMOS 23.217 processos de quem digita com pontuação, com espaço
            sobrando ou com o traço faltando. A normalização é entrada→dígitos→
            máscara canônica, e é o coração desta tela.
  DV        CPF com dígito verificador errado é ERRO DE ENTRADA (400), não
            "nenhum resultado" (200). São conclusões opostas pro usuário: uma
            diz "você digitou errado", a outra diria "essa pessoa não litiga".
  VARA      `orgao_julgador` existe em 100% dos docs e é STRING VAZIA em 74,9%.
            O filtro tem de se auto-excluir do vazio, e o autocomplete não pode
            oferecer "" como se fosse uma vara.
  COBERTURA Toda resposta carrega quanto da base o critério alcança. Buscar por
            CPF varre 0,14% dos 71,3M — sem isso, "0 resultados" mente.
  CURSOR    Paginação por search_after, cursor opaco, forjado ⇒ 400.

Não dependem de ES real: mockam `search.busca_api.get_es` (padrão de
`test_busca_api.py`), inclusive o `_msearch` que a tela usa pra pegar página e
total exato num round-trip só.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from search import busca_api as ba
from search import busca_ui as ui
from search.busca_api import BuscaParamError

URL = 'dashboard:busca-processos'
URL_VARAS = 'dashboard:busca-varas'

CNJ = '1105916-36.2026.8.26.0053'
CNJ_DIGITOS = '11059163620268260053'

#: CNPJ do INSS — o caso real do gate, nas formas que o usuário digita
CNPJ_INSS_DIGITOS = '29979036000140'
CNPJ_INSS_MASCARA = '29.979.036/0001-40'
CPF_VALIDO = '529.982.247-25'
CPF_DV_ERRADO = '529.982.247-26'

CACHE_LOCAL = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                           'LOCATION': 'busca-processos-teste'}}

User = get_user_model()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _qd(**kwargs):
    return SimpleNamespace(get=lambda k, default=None: kwargs.get(k, default))


def _hits(fontes_e_sort, total=0, relation='eq', took=7):
    return {
        'took': took,
        'hits': {'total': {'value': total, 'relation': relation},
                 'hits': [{'_source': src, 'sort': sort}
                          for src, sort in fontes_e_sort]},
    }


def _mock_msearch(*grupos):
    """Cliente ES mockado: um grupo de respostas por CHAMADA de `_msearch`.

    A tela faz até dois round-trips (página+total; depois o contexto do
    documento), e cada um valida que a resposta tem o mesmo tamanho do pedido —
    por isso o mock precisa devolver grupos em sequência, não uma lista só.
    """
    es = MagicMock()
    es.msearch.side_effect = [{'responses': list(g)} for g in grupos]
    return es


def _es_busca(pagina_hits=(), total=0, took=7):
    """Mock do par (página, contagem) que `buscar_processos_ui` faz num msearch."""
    return _mock_msearch([_hits(pagina_hits, took=took),
                          _hits((), total=total, took=1)])


def _bodies_do_msearch(es, n=0):
    """Extrai os corpos das sub-buscas (o payload alterna {} e body)."""
    payload = es.msearch.call_args_list[n].kwargs['body']
    return payload[1::2]


@pytest.fixture(autouse=True)
def cache_local():
    """Cache do endpoint em memória e zerado — o TTL é uma das garantias testadas."""
    with override_settings(CACHES=CACHE_LOCAL):
        from django.core.cache import cache
        cache.clear()
        yield
        cache.clear()


@pytest.fixture(autouse=True)
def cobertura_fixa():
    """Cobertura estável nos testes = os números medidos em 13/08/2026.

    Sem isto cada teste dispararia a medição (e, sem ES, o fallback) — travar
    aqui deixa as asserções de porcentagem legíveis e determinísticas.
    """
    with patch.object(ui, 'cobertura_indice', return_value={
        'total': 71_308_806,
        'participacoes.documento': 102_622,
        'participacoes.nome': 1_357_285,
        'participacoes.oab': 47_846,
        'orgao_julgador': 17_891_683,
        'juizo': 1_299_995,
        'medido_em': '2026-08-13',
        'ao_vivo': False,
    }):
        yield


@pytest.fixture
def logado(db):
    c = Client(SERVER_NAME='localhost')
    c.force_login(User.objects.create_user(username='busca', password='x'))
    return c


# --------------------------------------------------------------------------- #
# MÁSCARA — o coração da tela
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('entrada', [
    CNPJ_INSS_DIGITOS,                  # colado do sistema, sem pontuação
    CNPJ_INSS_MASCARA,                  # colado do PJe, com máscara
    f'  {CNPJ_INSS_MASCARA} ',          # com espaço sobrando (copiar/colar)
    '29.979.036/000140',                # traço faltando
    '29 979 036 0001 40',               # digitado com espaço
    '29.979.036/0001.40',               # separador trocado
])
def test_cnpj_em_qualquer_grafia_vira_o_mesmo_termo(entrada):
    """A máscara é do ÍNDICE. O usuário digita como quiser e tem de achar igual."""
    info = ba.normalizar_documento(entrada)
    assert info['tipo'] == 'cnpj'
    assert info['digitos'] == CNPJ_INSS_DIGITOS
    assert info['mascarado'] == CNPJ_INSS_MASCARA
    # o termo que vai pro ES é o mascarado (o índice NÃO tem a forma crua:
    # medido, `regexp` de 14 dígitos = 0 documentos)
    assert info['termos_exatos'][0] == CNPJ_INSS_MASCARA


def test_cpf_em_qualquer_grafia_vira_o_mesmo_termo():
    for entrada in ('52998224725', CPF_VALIDO, ' 529.982.247-25 ', '529 982 247 25'):
        info = ba.normalizar_documento(entrada)
        assert (info['tipo'], info['mascarado']) == ('cpf', CPF_VALIDO)


def test_cnpj_gera_raiz_e_parciais_lgpd():
    info = ba.normalizar_documento(CNPJ_INSS_DIGITOS)
    assert info['raiz'] == '29979036'
    assert info['raiz_prefixo'] == '29.979.036/'      # matriz + filiais
    assert '29.9**.***/****-**' in info['termos_parciais']
    assert '29.9XX.XXX/XXXX-XX' in info['termos_parciais']


def test_cpf_gera_a_parcial_com_dv_primeiro():
    """`872.***.***-97` revela 3 dígitos + o DV: é a parcial mais informativa."""
    info = ba.normalizar_documento('87212345697', forcar=True)
    assert info['termos_parciais'][0] == '872.***.***-97'
    assert '872.***.***-**' in info['termos_parciais']
    assert '872.XXX.XXX-XX' in info['termos_parciais']


def test_raiz_de_cnpj_com_8_digitos():
    info = ba.normalizar_documento('29979036')
    assert info['tipo'] == 'raiz_cnpj'
    assert info['raiz_prefixo'] == '29.979.036/'
    assert info['termos_exatos'] == []               # raiz não tem forma exata


def test_documento_vira_terms_no_nested():
    filtros = ba.parse_filtros_processos(_qd(documento=CNPJ_INSS_DIGITOS))
    clauses = ba.build_clauses_processos(filtros)
    nested = [c for c in clauses if 'nested' in c]
    assert len(nested) == 1
    assert nested[0]['nested']['path'] == 'participacoes'
    should = nested[0]['nested']['query']['bool']['filter'][0]['bool']['should']
    assert {'terms': {'participacoes.documento':
                      [CNPJ_INSS_MASCARA, CNPJ_INSS_DIGITOS]}} in should
    # sem pedir, NADA de expansão: nem raiz nem mascarado (abster > chutar)
    assert len(should) == 1


def test_raiz_e_parciais_so_entram_quando_pedidas():
    filtros = ba.parse_filtros_processos(_qd(
        documento=CNPJ_INSS_MASCARA, documento_raiz='1', documento_parciais='1'))
    should = (ba.build_clauses_processos(filtros)[0]['nested']['query']
              ['bool']['filter'][0]['bool']['should'])
    assert {'prefix': {'participacoes.documento': '29.979.036/'}} in should
    assert any('terms' in s and '29.9**.***/****-**'
               in s['terms']['participacoes.documento'] for s in should)


def test_raiz_de_8_digitos_expande_sozinha():
    """Digitar só a raiz não tem outra leitura possível — aí a expansão é o pedido."""
    filtros = ba.parse_filtros_processos(_qd(documento='29979036'))
    should = (ba.build_clauses_processos(filtros)[0]['nested']['query']
              ['bool']['filter'][0]['bool']['should'])
    assert should == [{'prefix': {'participacoes.documento': '29.979.036/'}}]


def test_documento_e_polo_no_mesmo_nested():
    """"CNPJ X no polo passivo" tem de ser a MESMA participação.

    Dois nested separados casariam o documento numa parte e o polo em outra do
    mesmo processo — falso positivo que ninguém veria.
    """
    filtros = ba.parse_filtros_processos(
        _qd(documento=CNPJ_INSS_DIGITOS, parte_polo='passivo'))
    nested = [c for c in ba.build_clauses_processos(filtros) if 'nested' in c]
    assert len(nested) == 1
    inner = nested[0]['nested']['query']['bool']['filter']
    assert {'term': {'participacoes.polo': 'passivo'}} in inner


def test_oab_vai_para_nested_separado_da_parte():
    """A OAB é de OUTRA pessoa que não a parte — nested próprio."""
    filtros = ba.parse_filtros_processos(
        _qd(documento=CNPJ_INSS_DIGITOS, oab='sp 123.456'))
    nested = [c for c in ba.build_clauses_processos(filtros) if 'nested' in c]
    assert len(nested) == 2
    assert any({'term': {'participacoes.oab': 'SP123456'}}
               in n['nested']['query']['bool']['filter'] for n in nested)


@pytest.mark.parametrize('entrada,esperado', [
    ('SP123456', 'SP123456'),
    ('sp 123.456', 'SP123456'),
    ('123456/SP', 'SP123456'),
    ('ce5864a', 'CE5864A'),             # formato real do índice (sufixo de letra)
    ('  pe 23255 ', 'PE23255'),
    ('', None),
    ('advogado', None),                 # sem dígito não é OAB
])
def test_normalizar_oab(entrada, esperado):
    assert ba.normalizar_oab(entrada) == esperado


# --------------------------------------------------------------------------- #
# DV — "inválido" e "não encontrado" são coisas DIFERENTES
# --------------------------------------------------------------------------- #
def test_cpf_com_dv_errado_e_erro_de_entrada():
    with pytest.raises(BuscaParamError) as e:
        ba.normalizar_documento(CPF_DV_ERRADO)
    assert str(e.value) == 'cpf_dv_invalido'


def test_cnpj_com_dv_errado_e_erro_de_entrada():
    with pytest.raises(BuscaParamError) as e:
        ba.normalizar_documento('29979036000141')
    assert str(e.value) == 'cnpj_dv_invalido'


@pytest.mark.parametrize('ruim', ['', '123', '5299822472', 'abc',
                                  '999999999999999999'])
def test_documento_de_tamanho_impossivel(ruim):
    with pytest.raises(BuscaParamError) as e:
        ba.normalizar_documento(ruim)
    assert str(e.value) == 'documento_invalido'


def test_cpf_repetido_e_invalido():
    """`111.111.111-11` passa na conta do DV clássico e não existe na vida real."""
    with pytest.raises(BuscaParamError):
        ba.normalizar_documento('11111111111')


def test_forcar_aceita_dv_invalido():
    """0,014% dos documentos do índice têm DV inválido — existe escape hatch."""
    info = ba.normalizar_documento(CPF_DV_ERRADO, forcar=True)
    assert info['dv_valido'] is False
    assert info['mascarado'] == '529.982.247-26'


def test_cpf_valido_nao_encontrado_e_200_com_zero(logado):
    """CPF válido sem processo: 200, total 0 — e a cobertura explicando o zero."""
    with patch('search.busca_api.get_es', return_value=_es_busca(total=0)):
        resp = logado.get(reverse(URL), {'documento': CPF_VALIDO})
    assert resp.status_code == 200
    data = resp.json()
    assert data['total']['value'] == 0
    assert data['results'] == []
    codigos = [a['codigo'] for a in data['avisos']]
    assert 'cobertura_documento' in codigos
    aviso = next(a for a in data['avisos'] if a['codigo'] == 'cobertura_documento')
    assert '0.144' in aviso['mensagem'] or '0,144' in aviso['mensagem']
    assert 'não significa' in aviso['mensagem'].lower()


def test_cpf_com_dv_errado_e_400_com_mensagem_propria(logado):
    """O MESMO fluxo com DV errado tem de dar erro diferente do 'não achei'."""
    with patch('search.busca_api.get_es') as get_es:
        resp = logado.get(reverse(URL), {'documento': CPF_DV_ERRADO})
        assert not get_es.called, 'CPF impossível não deve ir ao Elasticsearch'
    assert resp.status_code == 400
    data = resp.json()
    assert data['erro'] == 'cpf_dv_invalido'
    assert 'dígito verificador' in data['mensagem']


# --------------------------------------------------------------------------- #
# VARA / JUÍZO — 74,9% de string vazia
# --------------------------------------------------------------------------- #
def test_vara_gera_regexp_acento_insensivel():
    """O índice tem "VARA DE EXECUCAO FISCAL" e "1ª Vara de Execução Fiscal"."""
    rx = ba.regexp_contendo('execucao fiscal')
    assert rx.startswith('.*') and rx.endswith('.*')
    assert '[cçCÇ]' in rx                    # o ç de "Execução"
    assert '[aáàâãäªAÁÀÂÃÄ]' in rx           # o ã e o "1ª"
    assert '.*' in rx[2:-2]                  # tokens unidos: mais palavras estreita


def test_vara_casa_orgao_julgador_e_juizo():
    filtros = ba.parse_filtros_processos(_qd(vara='vara federal campinas'))
    clause = [c for c in ba.build_clauses_processos(filtros) if 'bool' in c][0]
    campos = {list(s['regexp'].keys())[0] for s in clause['bool']['should']}
    assert campos == {'orgao_julgador', 'juizo'}
    assert all(s['regexp'][c]['case_insensitive'] is True
               for s in clause['bool']['should'] for c in s['regexp'])


def test_vara_vazia_ou_curta_nao_vira_filtro():
    """Sem isso, `vara=` casaria os 53.417.123 docs com órgão em branco."""
    for termo in ('', '   ', 'a', 'ab'):
        assert 'vara' not in ba.parse_filtros_processos(_qd(vara=termo))
    assert ba.clausula_vara('') is None
    assert ba.clausula_vara('   ') is None


def test_vara_exata_vazia_e_recusada():
    assert ba.clausula_vara_exata('') is None
    assert ba.clausula_vara_exata('   ') is None
    assert 'vara_exata' not in ba.parse_filtros_processos(_qd(vara_exata='  '))


def test_vara_exata_vence_o_texto_livre():
    filtros = ba.parse_filtros_processos(
        _qd(vara='campinas', vara_exata='5ª Vara Federal de Campinas'))
    assert 'vara' not in filtros
    clause = [c for c in ba.build_clauses_processos(filtros) if 'bool' in c][0]
    assert {'term': {'orgao_julgador': '5ª Vara Federal de Campinas'}} \
        in clause['bool']['should']


def test_autocomplete_de_vara_descarta_a_string_vazia():
    resp = {'took': 13, 'aggregations': {
        'orgao_julgador': {'buckets': [
            {'key': '', 'doc_count': 53_417_123},        # o buraco do dado
            {'key': '5ª Vara Federal de Campinas', 'doc_count': 19_382}]},
        'juizo': {'buckets': [{'key': '1ª Vara Cível', 'doc_count': 119_799}]},
    }}
    with patch('search.busca_api.get_es') as get_es:
        get_es.return_value.search.return_value = resp
        out = ui.sugerir_varas('campinas')
    assert [i['valor'] for i in out['itens']] == ['1ª Vara Cível',
                                                  '5ª Vara Federal de Campinas']
    assert out['cobertura']['orgao_julgador']['pct'] == pytest.approx(25.09, abs=0.01)


def test_autocomplete_de_vara_nao_toca_no_es_com_termo_curto():
    with patch('search.busca_api.get_es') as get_es:
        out = ui.sugerir_varas('ca')
        assert not get_es.called
    assert out['buscou'] is False and out['motivo'] == 'termo_curto'


# --------------------------------------------------------------------------- #
# CNJ
# --------------------------------------------------------------------------- #
def test_cnj_com_e_sem_mascara_vira_o_mesmo_term():
    for entrada in (CNJ, CNJ_DIGITOS, f' {CNJ} '):
        filtros = ba.parse_filtros_processos(_qd(cnj=entrada))
        assert {'term': {'proc': CNJ}} in ba.build_clauses_processos(filtros)


def test_cnj_invalido_e_400(logado):
    with patch('search.busca_api.get_es') as get_es:
        resp = logado.get(reverse(URL), {'cnj': '123'})
        assert not get_es.called
    assert resp.status_code == 400
    assert resp.json()['erro'] == 'cnj_invalido'


# --------------------------------------------------------------------------- #
# Caixa única (`q`)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('entrada,campo,tipo', [
    (CNJ, 'cnj', 'cnj'),
    (CNJ_DIGITOS, 'cnj', 'cnj'),
    (CNPJ_INSS_DIGITOS, 'documento', 'cnpj'),
    (CNPJ_INSS_MASCARA, 'documento', 'cnpj'),
    ('529.982.247-25', 'documento', 'cpf'),
    ('29979036', 'documento', 'raiz_cnpj'),
    ('OAB SP 123456', 'oab', 'oab'),
    ('123456/SP', 'oab', 'oab'),
    ('INSTITUTO NACIONAL DO SEGURO SOCIAL', 'parte', 'nome'),
])
def test_classificar_termo(entrada, campo, tipo):
    out = ui.classificar_termo(entrada)
    assert (out['campo'], out['tipo']) == (campo, tipo)


def test_param_explicito_vence_o_q():
    assert ui.aplicar_q(_qd(q=CNPJ_INSS_DIGITOS, documento=CPF_VALIDO)) == {}


def test_q_com_cnpj_vira_filtro_de_documento(logado):
    with patch('search.busca_api.get_es', return_value=_es_busca(total=23_217)) as g:
        resp = logado.get(reverse(URL), {'q': CNPJ_INSS_DIGITOS})
        bodies = _bodies_do_msearch(g.return_value)
    data = resp.json()
    assert data['interpretacao'] == {
        'origem': 'q', 'termo': CNPJ_INSS_DIGITOS, 'campo': 'documento',
        'tipo': 'cnpj', 'valor': CNPJ_INSS_DIGITOS}
    assert data['total']['value'] == 23_217
    corpo = str(bodies[0])
    assert CNPJ_INSS_MASCARA in corpo


# --------------------------------------------------------------------------- #
# Página + total num msearch só (a decisão de latência)
# --------------------------------------------------------------------------- #
def test_pagina_e_total_sao_duas_sub_buscas():
    """`size+sort+track_total_hits` juntos custam 1.315 ms; separados, 281 ms."""
    es = _es_busca(total=4_403_363)
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(parte='INSTITUTO NACIONAL DO SEGURO SOCIAL')
    bodies = _bodies_do_msearch(es)
    assert len(bodies) == 2
    assert 'track_total_hits' not in bodies[0]     # a página NÃO conta
    assert bodies[0]['size'] == ba.SIZE_DEFAULT and 'sort' in bodies[0]
    assert bodies[1]['track_total_hits'] is True  # a contagem NÃO ordena
    assert bodies[1]['size'] == 0 and 'sort' not in bodies[1]
    # e a contagem roda em filter context (sem score = contagem por bitset)
    assert not bodies[1]['query']['bool'].get('must')
    assert out['total'] == {'value': 4_403_363, 'relation': 'eq'}


def test_total_vem_da_sub_busca_de_contagem_nao_da_pagina():
    es = _mock_msearch([_hits([({'proc': CNJ}, [1.0, 9])], total=10_000,
                              relation='gte'),
                        _hits((), total=4_403_363)])
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(parte='inss')
    assert out['total'] == {'value': 4_403_363, 'relation': 'eq'}


# --------------------------------------------------------------------------- #
# Cursor
# --------------------------------------------------------------------------- #
def test_pagina_cheia_gera_cursor_e_cursor_vira_search_after():
    hits = [({'proc': CNJ, 'id': i}, [1000 - i, i]) for i in range(2)]
    es = _mock_msearch([_hits(hits), _hits((), total=99)])
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(parte='inss', size=2)
    assert ba.decode_cursor(out['next_cursor']) == [999, 1]

    es2 = _es_busca(total=99)
    with patch('search.busca_api.get_es', return_value=es2):
        ui.buscar_processos_ui(parte='inss', size=2, cursor=out['next_cursor'])
    assert _bodies_do_msearch(es2)[0]['search_after'] == [999, 1]


def test_ultima_pagina_sem_cursor():
    es = _mock_msearch([_hits([({'proc': CNJ}, [1, 1])]), _hits((), total=1)])
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(size=20)
    assert out['next_cursor'] is None


def test_cursor_forjado_e_400(logado):
    with patch('search.busca_api.get_es') as get_es:
        resp = logado.get(reverse(URL), {'parte': 'inss', 'cursor': '###'})
        assert not get_es.called
    assert resp.status_code == 400
    assert resp.json()['erro'] == 'cursor_invalido'


# --------------------------------------------------------------------------- #
# COBERTURA no payload — a honestidade da tela
# --------------------------------------------------------------------------- #
def test_cobertura_de_documento_no_payload():
    es = _mock_msearch([_hits(()), _hits((), total=0)],          # página+total
                       [_hits((), total=27_044),                  # contexto: raiz
                        _hits((), total=196)])                    # ... e parciais
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(
            filtros=ba.parse_filtros_processos(_qd(documento=CNPJ_INSS_DIGITOS)))
    campo = out['cobertura']['campos']['participacoes.documento']
    assert campo['docs'] == 102_622
    assert campo['pct'] == pytest.approx(0.144, abs=0.001)
    assert out['cobertura']['total_processos'] == 71_308_806
    assert out['cobertura']['limitante']['campo'] == 'participacoes.documento'


def test_cobertura_limitante_e_o_campo_mais_estreito():
    """Combinando nome (100%) com OAB (0,067%), quem manda no universo é a OAB."""
    with patch('search.busca_api.get_es', return_value=_es_busca(total=3)):
        out = ui.buscar_processos_ui(
            parte='fulano',
            filtros=ba.parse_filtros_processos(_qd(oab='SP123456')))
    assert out['cobertura']['campos']['partes']['pct'] == 100.0
    assert out['cobertura']['limitante']['campo'] == 'participacoes.oab'


def test_busca_por_nome_nao_leva_aviso_de_cobertura():
    """`partes` cobre 100% da base: ressalva aqui seria ruído."""
    with patch('search.busca_api.get_es', return_value=_es_busca(total=4_403_363)):
        out = ui.buscar_processos_ui(parte='INSTITUTO NACIONAL DO SEGURO SOCIAL')
    assert out['avisos'] == []
    assert out['cobertura']['limitante'] is None


def test_polo_move_a_busca_do_nome_para_o_nested_e_avisa():
    es = _es_busca(total=12)
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(
            parte='fulano',
            filtros=ba.parse_filtros_processos(_qd(parte_polo='passivo')))
    corpo = _bodies_do_msearch(es)[0]
    assert 'must' not in corpo['query']['bool']         # saiu do campo texto
    assert 'participacoes.nome' in str(corpo['query']['bool']['filter'])
    assert 'parte_estruturada' in [a['codigo'] for a in out['avisos']]
    assert 'participacoes.nome' in out['cobertura']['campos']


def test_alternativas_do_documento_viram_oferta_nao_resultado():
    """Achou 23.217 no CNPJ exato — e CONTA que existem 27.044 com as filiais."""
    es = _mock_msearch([_hits(()), _hits((), total=23_217)],
                       [_hits((), total=27_044), _hits((), total=670)])
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(
            filtros=ba.parse_filtros_processos(_qd(documento=CNPJ_INSS_MASCARA)))
    assert out['total']['value'] == 23_217                 # o resultado NÃO mudou
    assert out['documento']['aplicado'] == ['exato']
    assert out['documento']['alternativas'] == {'raiz': 27_044, 'parciais': 670}
    codigos = [a['codigo'] for a in out['avisos']]
    assert 'documento_raiz_disponivel' in codigos
    assert 'documento_parciais_disponiveis' in codigos


def test_contexto_nao_reconta_o_que_ja_esta_aplicado():
    """Com `documento_raiz=1` a raiz já É o resultado — contá-la de novo é gasto."""
    es = _mock_msearch([_hits(()), _hits((), total=27_044)],
                       [_hits((), total=670)])
    with patch('search.busca_api.get_es', return_value=es):
        out = ui.buscar_processos_ui(filtros=ba.parse_filtros_processos(
            _qd(documento=CNPJ_INSS_MASCARA, documento_raiz='1')))
    assert 'raiz' not in out['documento']['alternativas']
    assert out['documento']['aplicado'] == ['exato', 'raiz']


def test_cobertura_cai_no_fallback_medido_quando_o_es_falha():
    """Número velho identificado > campo ausente: a tela precisa dizer ALGUMA coisa."""
    with patch.object(ui, 'medir_cobertura', side_effect=RuntimeError('ES fora')):
        cob = ui.cobertura_indice(forcar=True)
    assert cob['total'] == ui.COBERTURA_FALLBACK['total']
    assert cob['ao_vivo'] is False and cob['medido_em'] == '2026-08-13'


# --------------------------------------------------------------------------- #
# Endpoint — guardas, degradação, cache
# --------------------------------------------------------------------------- #
def test_urls_resolvem():
    assert reverse(URL) == '/dashboard/api/busca/processos/'
    assert reverse(URL_VARAS) == '/dashboard/api/busca/varas/'


@pytest.mark.django_db
def test_anonimo_redireciona_para_login(client):
    resp = client.get(reverse(URL), {'parte': 'inss'})
    assert resp.status_code == 302
    assert '/login' in resp['Location']


def test_so_get(logado):
    assert logado.post(reverse(URL)).status_code == 405


def test_es_fora_vira_503(logado):
    es = MagicMock()
    es.msearch.side_effect = ConnectionError('boom')
    with patch('search.busca_api.get_es', return_value=es):
        resp = logado.get(reverse(URL), {'parte': 'inss'})
    assert resp.status_code == 503
    assert resp.json()['erro'] == 'elasticsearch_indisponivel'


def test_segunda_chamada_identica_vem_do_cache(logado):
    with patch('search.busca_api.get_es', return_value=_es_busca(total=7)) as g:
        primeira = logado.get(reverse(URL), {'parte': 'inss'})
        segunda = logado.get(reverse(URL), {'parte': 'inss'})
        assert g.return_value.msearch.call_count == 1
    assert primeira.json()['cache'] is False
    assert segunda.json()['cache'] is True
    assert segunda.json()['total'] == primeira.json()['total']


def test_size_e_clampado(logado):
    with patch('search.busca_api.get_es', return_value=_es_busca(total=1)) as g:
        resp = logado.get(reverse(URL), {'parte': 'inss', 'size': '9999'})
        assert _bodies_do_msearch(g.return_value)[0]['size'] == ba.SIZE_MAX
    assert resp.json()['size'] == ba.SIZE_MAX


def test_filtros_combinados_chegam_no_corpo(logado):
    with patch('search.busca_api.get_es', return_value=_es_busca(total=5)) as g:
        logado.get(reverse(URL), {
            'documento': CNPJ_INSS_DIGITOS, 'tribunal': 'TJSP,TRF3', 'uf': 'SP',
            'vara': 'vara federal campinas', 'ano_min': '2020',
            'classificacao': 'precatorio', 'ordenar': 'valor_desc'})
        corpo = _bodies_do_msearch(g.return_value)[0]
    fil = corpo['query']['bool']['filter']
    assert {'terms': {'tribunal': ['TJSP', 'TRF3']}} in fil
    assert {'term': {'uf': 'SP'}} in fil
    assert {'term': {'classificacao': 'PRECATORIO'}} in fil
    assert {'range': {'ano_cnj': {'gte': 2020}}} in fil
    assert corpo['sort'][0] == {'valor_causa': {'order': 'desc', 'missing': '_last'}}


# --------------------------------------------------------------------------- #
# busca exata com aspas
# --------------------------------------------------------------------------- #
def test_aspas_viram_frase_exata():
    """`"William Alves de Souza"` tem que casar a frase, não as palavras soltas.

    Medido em prod (14/08/2026): sem aspas o campo `partes` devolve 923
    processos; com frase exata, 20. Os outros 903 são o mesmo defeito do
    "União Federal" — três pessoas diferentes emprestando uma palavra cada
    ("TIAGO ALVES DE SOUZA, JONATHAN WILLIAM RODRIGUES").
    """
    from search.busca_api import _match_and, entre_aspas
    q = _match_and('partes', '"William Alves de Souza"')
    assert 'match_phrase' in q
    assert q['match_phrase']['partes'] == 'William Alves de Souza'
    # sem aspas segue sendo AND (garimpo)
    q2 = _match_and('partes', 'William Alves de Souza')
    assert 'match' in q2 and q2['match']['partes']['operator'] == 'and'
    assert entre_aspas('sem aspas') is None


def test_aspas_curvas_do_word_tambem_valem():
    """Quem cola de um PDF/Word não digitou aspa reta — falhar aí seria falhar
    justamente com quem tentou ser preciso."""
    from search.busca_api import entre_aspas
    assert entre_aspas('“William Alves”') == 'William Alves'
    assert entre_aspas("'William Alves'") == 'William Alves'
    assert entre_aspas('""') is None          # aspas vazias não viram frase
    assert entre_aspas('"') is None


# --------------------------------------------------------------------------- #
# busca COMPOSTA: "eu" E "o banco" — achar UM processo, não todos os meus
# --------------------------------------------------------------------------- #
def test_frase_mais_termo_viram_duas_condicoes():
    """`"William Alves de Souza" Santander` = frase E termo, ambos obrigatórios.

    É o caso mais natural que existe ("quero MEU processo contra o banco") e
    antes não funcionava: a busca inteira virava UMA frase
    ("William Alves de Souza Santander"), que não existe em lugar nenhum, e o
    usuário recebia zero sem entender por quê.
    """
    from search.busca_api import clausulas_texto, separar_termos
    t = separar_termos('"William Alves de Souza" Santander')
    assert t == [('frase', 'William Alves de Souza'), ('termos', 'Santander')]

    cl = clausulas_texto('partes', '"William Alves de Souza" Santander')
    assert len(cl) == 2
    assert cl[0]['match_phrase']['partes'] == 'William Alves de Souza'
    assert cl[1]['match']['partes']['query'] == 'Santander'


def test_duas_frases_exatas():
    """Dá pra exigir DUAS pessoas exatas no mesmo processo."""
    from search.busca_api import clausulas_texto
    cl = clausulas_texto('partes', '"Maria Silva" "Banco do Brasil"')
    assert len(cl) == 2
    assert all('match_phrase' in c for c in cl)


def test_palavras_soltas_continuam_um_and_so():
    """"Santander S.A." solto é UM and de duas palavras, não duas condições —
    senão a busca simples ficaria mais restritiva do que era."""
    from search.busca_api import clausulas_texto
    cl = clausulas_texto('partes', 'William Alves de Souza')
    assert len(cl) == 1
    assert cl[0]['match']['partes']['operator'] == 'and'


def test_aspas_curvas_tambem_compoem():
    from search.busca_api import separar_termos
    assert separar_termos('“Maria Silva” Bradesco') == [
        ('frase', 'Maria Silva'), ('termos', 'Bradesco')]
