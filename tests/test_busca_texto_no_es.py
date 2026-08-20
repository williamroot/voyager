"""A busca por texto sai do Postgres e vai pro Elasticsearch.

CONTEXTO (20/08/2026). `tribunals_movimentacao` tem 9 índices e NENHUM cobre
`texto` ou `search_vector` — conferido coluna a coluna em `pg_index`. O model
declarava `mov_texto_trgm` e `mov_search_vector_gin`; os dois eram ficção, e a
ficção estava escrita como certeza em dois comentários de código, que é o que
faz uma armadilha dessas ficar invisível para quem lê.

O que havia, a uma querystring de distância:

  · `?q=` na tela e `?q=` na API caíam em `texto__icontains`. EXPLAIN sem
    recorte: Seq Scan, custo 111.195.298, 1,39 bilhão de linhas / 815 GB, no
    caminho da requisição.
  · a API, com 3+ palavras, usava `search_vector` + `SearchRank`. Pior, porque
    RESPONDIA: a coluna existe, não tem trigger que a preencha e está cheia só
    até id≈4.876.372 — 2.753.688 linhas de 1.385.659.648, **0,199% do acervo**.
    Uma busca de três palavras varria 0,2% do país e dizia "encontrei isto".

O índice de verdade é o `voyager-movimentacoes-v2`, e foi para isso que os
179.490.613 documentos que faltavam entraram nele em 18/08.
"""
import re
from unittest.mock import patch

import pytest


# --------------------------------------------------------------------------- #
# Barreira estática: ninguém volta a varrer texto no Postgres pela requisição
# --------------------------------------------------------------------------- #
CAMINHO_DA_REQUISICAO = ['api/filters.py', 'dashboard/views.py']


@pytest.mark.parametrize('arquivo', CAMINHO_DA_REQUISICAO)
def test_nenhuma_varredura_de_texto_no_postgres(arquivo):
    """`texto__icontains` e `search_vector=` não podem voltar por descuido.

    Este teste é a única coisa que separa "corrigido" de "corrigido até alguém
    reintroduzir". Comentário não segura regressão; asserção segura.

    Comentários e docstrings são removidos antes da varredura: a explicação do
    conserto precisa poder CITAR o que foi consertado.
    """
    import ast as _ast
    from pathlib import Path

    fonte = Path(__file__).resolve().parent.parent / arquivo
    arvore = _ast.parse(fonte.read_text())
    for no in _ast.walk(arvore):                       # apaga docstrings
        corpo = getattr(no, 'body', None)   # `Lambda.body` é expressão, não lista
        if (isinstance(corpo, list) and corpo and isinstance(corpo[0], _ast.Expr)
                and isinstance(corpo[0].value, _ast.Constant)
                and isinstance(corpo[0].value.value, str)):
            corpo[0].value.value = ''
    codigo = _ast.unparse(arvore)                      # `#` já não sobrevivem ao parse

    for proibido in ('texto__icontains', 'texto__search', 'search_vector'):
        assert proibido not in codigo, (
            f'{arquivo} voltou a varrer texto no Postgres via `{proibido}` — '
            'sem índice isso é Seq Scan em 1,39B de linhas. Use '
            'search.busca_api.ids_por_texto.')


def test_model_nao_declara_indice_que_o_banco_nao_tem():
    """Índice declarado e ausente é documentação falsa — foi o mecanismo do bug.

    Não dá para checar o banco daqui (o de teste é vazio e criado do zero, então
    ele TERIA os índices). O que dá para travar é o inverso, que é o que
    interessa: estes três não voltam a ser declarados sem alguém decidir criá-los
    de verdade em 1,39 bilhão de linhas.
    """
    from tribunals.models import Movimentacao
    declarados = {c.lstrip('-') for i in Movimentacao._meta.indexes for c in i.fields}
    for col in ('texto', 'search_vector', 'hash'):
        assert col not in declarados, (
            f'`{col}` voltou ao Meta.indexes. O banco não tem esse índice; '
            'criá-lo custa GIN sobre 815 GB / btree sobre 1,39B de linhas. '
            'Ver migration 0051 e search/busca_api.ids_por_texto.')


# --------------------------------------------------------------------------- #
# ids_por_texto: o teto é alerta, e o ES fora do ar não vira Postgres
# --------------------------------------------------------------------------- #
def _resposta_es(n, total):
    return {'hits': {'total': {'value': total},
                     'hits': [{'_source': {'id': i}} for i in range(1, n + 1)]}}


def test_devolve_pks_e_nao_documentos():
    from search.busca_api import ids_por_texto
    with patch('search.busca_api._executar', return_value=_resposta_es(3, 3)):
        r = ids_por_texto('precatório')
    assert r == {'ids': [1, 2, 3], 'total': 3, 'truncado': False}


def test_teto_e_alerta_nunca_corte_mudo():
    from search.busca_api import IDS_TEXTO_TETO, ids_por_texto
    resp = _resposta_es(IDS_TEXTO_TETO, IDS_TEXTO_TETO + 1)
    with patch('search.busca_api._executar', return_value=resp):
        r = ids_por_texto('vista')
    assert r['truncado'] is True, 'bateu o teto e não avisou'


def test_ordena_por_data_e_nao_por_score():
    """A tela é cronológica; devolver por relevância seria embaralhar."""
    from search.busca_api import ids_por_texto
    with patch('search.busca_api._executar', return_value=_resposta_es(1, 1)) as ex:
        ids_por_texto('x')
    corpo = ex.call_args.args[1]
    assert corpo['sort'][0] == {'publish_date': {'order': 'desc', 'missing': '_last'}}
    assert corpo['_source'] == ['id'], 'trazer o body inteiro de 2.000 hits é desperdício'


def test_tem_teto_de_espera():
    """Nada no caminho da requisição sem `request_timeout` (CLAUDE.md, regra 7)."""
    from search.busca_api import ids_por_texto
    with patch('search.busca_api._executar', return_value=_resposta_es(1, 1)) as ex:
        ids_por_texto('x')
    assert ex.call_args.kwargs.get('request_timeout'), 'medição sem teto derrubou o site uma vez'


def test_track_total_hits_nao_conta_o_acervo_inteiro():
    from search.busca_api import IDS_TEXTO_TETO, ids_por_texto
    with patch('search.busca_api._executar', return_value=_resposta_es(1, 1)) as ex:
        ids_por_texto('de')
    assert ex.call_args.args[1]['track_total_hits'] == IDS_TEXTO_TETO + 1


def test_es_fora_do_ar_nao_cai_pro_postgres():
    """Trocar 'não sei' por uma resposta errada — varrendo 815 GB pra errar."""
    from elasticsearch import TransportError

    from search.busca_api import BuscaIndisponivelError, ids_por_texto
    with patch('search.busca_api._executar', side_effect=TransportError('caiu')), \
         pytest.raises(BuscaIndisponivelError):
        ids_por_texto('precatório')


# --------------------------------------------------------------------------- #
# O filtro da API
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_filtro_da_api_nao_gera_ilike_nem_tsquery():
    from tribunals.models import Movimentacao

    from api.filters import MovimentacaoFilter
    f = MovimentacaoFilter()
    with patch('search.busca_api.ids_por_texto',
               return_value={'ids': [7, 9], 'total': 2, 'truncado': False}):
        qs = f.filter_search(Movimentacao.objects.all(), 'q', 'honorários advocatícios sucumbenciais')
    sql = str(qs.query)
    assert 'ILIKE' not in sql.upper() and '@@' not in sql
    assert 'IN (7, 9)' in sql.replace('"', '')


@pytest.mark.django_db
def test_filtro_da_api_repassa_o_tribunal_ao_es():
    """Sem tribunal o ES varre 1,4 bi de docs para depois jogar fora."""
    from tribunals.models import Movimentacao

    from api.filters import MovimentacaoFilter
    f = MovimentacaoFilter(data={'tribunal': 'TJSP'})
    with patch('search.busca_api.ids_por_texto',
               return_value={'ids': [], 'total': 0, 'truncado': False}) as ip:
        f.filter_search(Movimentacao.objects.all(), 'q', 'precatório')
    assert ip.call_args.kwargs['tribunais'] == ['TJSP']


@pytest.mark.django_db
def test_termo_curto_continua_ignorado():
    from tribunals.models import Movimentacao

    from api.filters import MovimentacaoFilter
    qs = Movimentacao.objects.all()
    assert MovimentacaoFilter().filter_search(qs, 'q', 'ab') is qs
