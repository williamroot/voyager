"""Remove do ESTADO três índices que o model declarava e o banco nunca teve.

Nenhum `CREATE`/`DROP` roda aqui — `database_operations=[]` é o ponto. O banco
já está certo; quem estava errado era o model, e isso custou caro:

    tribunals_movimentacao tem 9 índices e NENHUM cobre `texto`, `search_vector`
    ou `hash` (conferido por coluna em pg_index, 20/08/2026).

O que a declaração falsa produziu, em dois lugares independentes:

  · `diarios/base.py::fingerprint_ato` escreveu no docstring que `hash` "já é
    indexada", e `espelhadas_no_lote` nasceu em cima disso: EXPLAIN de custo
    73.427.276 por lote no TJSP, dentro do caminho de escrita da ingestão de
    diários, sem teto de espera.
  · `api/filters.py` escreveu que o ILIKE usava o índice GIN trigram. Sem
    recorte, custo 111.195.298 — Seq Scan em 1,39 bilhão de linhas / 815 GB no
    caminho da requisição.

E criar os índices não é a saída:

  · `mov_texto_trgm`: GIN trigram sobre 815 GB de texto.
  · `mov_search_vector_gin`: a coluna existe, não tem trigger que a preencha e
    está NULL em 99,8% da tabela — cheia só até id≈4.876.372 (13/03/2024), que
    são 2.753.688 linhas de 1.385.659.648. Indexar isso indexaria o vazio.
  · `hash`: btree em 1,39B de linhas num Postgres já disk-I/O-bound, para
    parear campos que nem casam entre si (sha1 de 40 chars do fingerprint vs
    hash opaco de 30 chars da API do DJEN).

A busca textual passou para o `voyager-movimentacoes-v2`, que é onde o índice
existe de verdade — e que desde 18/08 tem o acervo inteiro
(`search/busca_api.ids_por_texto`).

A coluna `search_vector` fica: derrubá-la em 1,39B de linhas é decisão de
operação, não de refactor, e ninguém mais a consulta.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0050_indices_io')]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveIndex(
                    model_name='movimentacao', name='mov_search_vector_gin'),
                migrations.RemoveIndex(
                    model_name='movimentacao', name='mov_texto_trgm'),
                migrations.RemoveIndex(
                    model_name='movimentacao', name='tribunals_m_hash_c1d24b_idx'),
            ],
        ),
    ]
