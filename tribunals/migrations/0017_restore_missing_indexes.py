"""Restaura indexes/constraints que ficaram declarados nos models mas
não estavam presentes em produção (postgres em 192.168.1.82 foi
populado de um dump que perdeu indexes; django_migrations já marcava
0001..0016 como aplicadas, mas as ALTER TABLE ADD INDEX/CONSTRAINT
correspondentes não chegaram).

Idempotente: usa `IF NOT EXISTS` em todos os CREATE. Em DBs que já têm
todos os indexes (dev, novos restores via schema), vira no-op.

Não-atômica: `CREATE INDEX CONCURRENTLY` não pode rodar dentro de
transação. Em troca, não bloqueia leitura/escrita das tabelas durante
a build do índice (importante: temos 2.7M+ rows em movimentacao e
3.7M+ em processoparte).
"""
from django.db import migrations


SQL_STATEMENTS = [
    # --- tribunals_process ---
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_tribunal_numero_cnj_idx "
    "ON tribunals_process (tribunal_id, numero_cnj);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_tribunal_ult_mov_idx "
    "ON tribunals_process (tribunal_id, ultima_movimentacao_em DESC);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_inserido_em_idx "
    "ON tribunals_process (inserido_em);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_enriquecido_em_idx "
    "ON tribunals_process (enriquecido_em);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_classe_codigo_idx "
    "ON tribunals_process (classe_codigo);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_classe_id_idx "
    "ON tribunals_process (classe_id);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_assunto_id_idx "
    "ON tribunals_process (assunto_id);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_orgao_julgador_codigo_idx "
    "ON tribunals_process (orgao_julgador_codigo);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_ano_cnj_idx "
    "ON tribunals_process (ano_cnj);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_tribunal_ano_cnj_idx "
    "ON tribunals_process (tribunal_id, ano_cnj);",

    # --- tribunals_parte: indexes simples ---
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS parte_nome_idx "
    "ON tribunals_parte (nome);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS parte_documento_idx "
    "ON tribunals_parte (documento);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS parte_oab_idx "
    "ON tribunals_parte (oab);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS parte_tipo_idx "
    "ON tribunals_parte (tipo);",

    # --- tribunals_parte: partial unique constraints (críticos pro upsert idempotente) ---
    # CREATE UNIQUE INDEX CONCURRENTLY (sem ALTER ADD CONSTRAINT) — Postgres
    # respeita unicidade direto via index. Django reflete como UniqueConstraint
    # tendo o mesmo nome.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uniq_parte_documento_real "
    "ON tribunals_parte (documento) "
    "WHERE documento <> '' AND documento NOT LIKE '%X%' "
    "AND documento NOT LIKE '%x%' AND documento NOT LIKE '%*%';",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uniq_parte_documento_mascarado "
    "ON tribunals_parte (nome, documento) "
    "WHERE documento LIKE '%X%' OR documento LIKE '%x%' OR documento LIKE '%*%';",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uniq_parte_sem_doc_nem_oab "
    "ON tribunals_parte (nome, tipo) "
    "WHERE documento = '' AND oab = '';",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uniq_parte_oab "
    "ON tribunals_parte (oab) "
    "WHERE oab <> '';",

    # --- tribunals_processoparte ---
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS pp_parte_polo_idx "
    "ON tribunals_processoparte (parte_id, polo);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS pp_processo_polo_idx "
    "ON tribunals_processoparte (processo_id, polo);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS pp_papel_idx "
    "ON tribunals_processoparte (papel);",
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uniq_processo_parte_polo_papel_principal "
    "ON tribunals_processoparte (processo_id, parte_id, polo, papel) "
    "WHERE representa_id IS NULL;",

    # --- tribunals_movimentacao ---
    # mov_processo_data_disp_idx ja foi criado fora da migration (debug
    # urgente do detail page), mas mantemos IF NOT EXISTS aqui pra
    # garantir idempotencia em outros DBs.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS mov_processo_data_disp_idx "
    "ON tribunals_movimentacao (processo_id, data_disponibilizacao DESC);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS mov_tribunal_data_disp_idx "
    "ON tribunals_movimentacao (tribunal_id, data_disponibilizacao DESC);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS mov_tribunal_ativo_idx "
    "ON tribunals_movimentacao (tribunal_id, ativo);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS mov_hash_idx "
    "ON tribunals_movimentacao (hash);",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS mov_classe_id_idx "
    "ON tribunals_movimentacao (classe_id);",
]


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cur:
        for stmt in SQL_STATEMENTS:
            cur.execute(stmt)


def reverse(apps, schema_editor):
    # Reverso é no-op: indexes são declarados nos models. Se quiser
    # remover, é via deletar do model + makemigrations gerar AlterIndexes.
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('tribunals', '0016_process_ultima_sinc_djen_em'),
    ]

    operations = [
        migrations.RunPython(forward, reverse, elidable=False),
    ]
