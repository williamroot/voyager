"""O índice que a API de leads precisa existe E é o que o planner escolhe.

Em 09/09/2026 `?nivel=PRE_PRECATORIO&tribunal=TJSP` não respondia. O plano em
produção era `Index Scan using proc_tribunal_ult_mov_idx` com `Index Cond:
tribunal_id = 'TJSP'` e TODO o resto em `Filter` — o planner caminhava 31 M de
linhas do TJSP, da movimentação mais recente para trás, atrás das 35.994 do
nível pedido.

Declarar o índice não basta: o repositório já viveu o caso de índice com o
nome certo e as colunas erradas (`proc_tribunal_id_idx`, ver DATA_MODEL.md), que
passa por `\\di` e por `makemigrations`. Por isso aqui se checa **a coluna** e
**o plano**, não o nome.

`enable_seqscan = off` porque a base de teste é pequena: sem isso o planner
varre a tabela e o teste não afirma nada sobre a escolha do índice. O que se
quer provar é que, QUANDO indexar valer a pena, é este o índice usado.
"""
import pytest
from django.db import connection

from tribunals.models import Process, Tribunal

INDICE = 'proc_leads_api_idx'


def _colunas(nome: str) -> list[str]:
    with connection.cursor() as cur:
        cur.execute("""
            SELECT (SELECT array_agg(a.attname ORDER BY k.ord)
                      FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                      LEFT JOIN pg_attribute a
                             ON a.attrelid = i.indrelid AND a.attnum = k.attnum)
              FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
             WHERE ic.relname = %s
        """, [nome])
        linha = cur.fetchone()
        return list(linha[0]) if linha and linha[0] else []


@pytest.mark.django_db
def test_indice_tem_as_colunas_na_ordem_do_order_by():
    """Líder `(tribunal, classificacao)` para virar Index Cond; o resto na
    ordem exata do `ORDER BY` da view, para o scan já sair ordenado."""
    assert _colunas(INDICE) == [
        'tribunal_id', 'classificacao', 'ultima_movimentacao_em',
        'classificacao_score', 'id',
    ]


@pytest.mark.django_db
def test_indice_e_parcial_nas_tres_classificacoes():
    """PARCIAL de propósito: 2,8% das 126 M linhas têm classificação de lead.
    Se alguém tirar a condição, o índice infla ~7 GB sem servir a mais nada."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT pg_get_expr(i.indpred, i.indrelid)
              FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
             WHERE ic.relname = %s
        """, [INDICE])
        predicado = (cur.fetchone() or [None])[0]

    assert predicado, f'{INDICE} deixou de ser parcial'
    for classif in ('PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO'):
        assert classif in predicado


@pytest.mark.django_db
def test_planner_escolhe_o_indice_na_consulta_da_api():
    """A consulta da view, com o mesmo filtro e a mesma ordenação."""
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Process.objects.create(
        tribunal=t, numero_cnj='1-1.2024.8.26.0100',
        classificacao=Process.CLASSIF_PRE_PRECATORIO, classificacao_score=0.9)

    qs = (Process.objects
          .filter(classificacao=Process.CLASSIF_PRE_PRECATORIO,
                  classificacao_score__gte=0.2, tribunal_id='TJSP')
          .order_by('-ultima_movimentacao_em', '-classificacao_score', '-id'))

    with connection.cursor() as cur:
        cur.execute('SET LOCAL enable_seqscan = off')
        plano = qs[:3].explain()

    assert INDICE in plano, plano
    # E sem `Sort`: o índice já entrega a ordem — é isso que segura o LIMIT
    # pequeno em tabela grande.
    assert 'Sort' not in plano, plano
