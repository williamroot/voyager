"""Índice da API de leads — o que fazia o TJSP nível 2 devolver 500.

**O sintoma (09/09/2026).** `GET /api/v1/leads/?nivel=PRE_PRECATORIO&tribunal=TJSP`
respondia erro depois de meio minuto, com `[CRITICAL] WORKER TIMEOUT` no log do
gunicorn e a linha do traceback sempre no `list(qs[:limit])` de `api/leads.py`.
O mesmo tribunal no nível 1 respondia em 0,9 s. Do outro lado, o pull do
Juriscope fechava o dia com `n2: 0` para o TJSP.

**A causa.** O plano em produção era:

    Limit
      -> Incremental Sort   (cost=27192..17669825)
           -> Index Scan using proc_tribunal_ult_mov_idx
                Index Cond: tribunal_id = 'TJSP'
                Filter: classificacao = 'PRE_PRECATORIO' AND score >= 0.2 AND NOT (...)

O único índice que servia à cláusula `ORDER BY -ultima_movimentacao_em` casa
**apenas `tribunal_id`**. O planner então caminhava o TJSP inteiro — 31 M de
linhas — do mais recente para trás, esperando topar com as 3 linhas pedidas.
Como o `PRE_PRECATORIO` do TJSP são 35.994 linhas ANTIGAS no meio de 31 M, a
caminhada não terminava dentro do tempo do worker. É a armadilha clássica de
`ORDER BY <indexado> LIMIT n` com filtro seletivo fora do índice: quanto MENOR
o `limit`, mais o planner gosta do caminho errado.

**O índice.** Líder `(tribunal, classificacao)` para virar Index Cond, e o
resto na ordem exata do `ORDER BY` da view, para o scan já sair ordenado:

    (tribunal_id, classificacao, ultima_movimentacao_em DESC,
     classificacao_score DESC, id DESC)

**Por que PARCIAL.** 3,56 M das 126 M linhas da tabela têm classificação de
lead — 2,8%. O índice cheio pagaria ~7 GB para responder a uma consulta que só
olha essas. Com a condição, sobram ~3,5 M entradas. O planner **prova** que
`classificacao = 'PRE_PRECATORIO'` implica o `IN` da condição — conferido no
PG 17.9 de produção, em tabela temporária, ANTES de escrever esta migration:

    Limit
      -> Index Only Scan using t_proof_idx
           Index Cond: (tribunal_id = 'TJSP' AND classificacao = 'PRE_PRECATORIO'
                        AND classificacao_score >= 0.2)

Sem sort e sem filtro residual — que é exatamente o que faltava.

**`CONCURRENTLY` + `atomic = False`**: `tribunals_process` é tabela quente e o
`ALTER` normal toma `ACCESS EXCLUSIVE` (o auto-jam de OPS.md, que já derrubou o
site por 50 min).

**`IF NOT EXISTS` e o índice criado à mão**: o build roda em produção num
container separado, fora do caminho do `migrate` — o entrypoint do `web` roda
`migrate --noinput` a cada start, e um `CREATE INDEX CONCURRENTLY` interrompido
por restart deixa índice `indisvalid=false` e põe o web em crash-loop
(incidente de 01/07/2026, OPS.md). Aqui a migration é no-op em produção e vale
para qualquer ambiente novo.

⚠️ Depois do build, CONFIRME a validade — índice inválido é pior que índice
ausente, porque o writer o mantém e o planner o ignora:

    SELECT indisvalid, indisready FROM pg_index
     WHERE indexrelid = 'proc_leads_api_idx'::regclass;
"""
from django.db import migrations, models
from django.db.models import Q

SQL_CRIAR = """
CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_leads_api_idx
    ON tribunals_process (tribunal_id, classificacao,
                          ultima_movimentacao_em DESC,
                          classificacao_score DESC, id DESC)
 WHERE classificacao IN ('PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO');
"""

SQL_REVERTER = 'DROP INDEX CONCURRENTLY IF EXISTS proc_leads_api_idx;'


class Migration(migrations.Migration):
    atomic = False

    dependencies = [('tribunals', '0060_busca_tribunal_run')]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name='process',
                    index=models.Index(
                        fields=['tribunal', 'classificacao',
                                '-ultima_movimentacao_em',
                                '-classificacao_score', '-id'],
                        name='proc_leads_api_idx',
                        condition=Q(classificacao__in=[
                            'PRECATORIO', 'PRE_PRECATORIO',
                            'DIREITO_CREDITORIO']),
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(sql=SQL_CRIAR, reverse_sql=SQL_REVERTER),
            ],
        ),
    ]
