"""Recria os 3 índices únicos parciais de tribunals_parte que ficaram
INVÁLIDOS (CONCURRENTLY que falhou na 0017; IF NOT EXISTS perpetuou o husk).

Pré-requisito: rodar `dedup_partes` ANTES. Aborta com erro claro se ainda
houver duplicata (o CREATE UNIQUE INDEX não valida).

Não-atômica: CONCURRENTLY não roda em transação.
"""
from django.db import migrations

INDICES = [
    ('uniq_parte_documento_real',
     "CREATE UNIQUE INDEX CONCURRENTLY uniq_parte_documento_real "
     "ON tribunals_parte (documento) "
     "WHERE documento <> '' AND documento NOT LIKE '%%X%%' "
     "AND documento NOT LIKE '%%x%%' AND documento NOT LIKE '%%*%%'"),
    ('uniq_parte_documento_mascarado',
     "CREATE UNIQUE INDEX CONCURRENTLY uniq_parte_documento_mascarado "
     "ON tribunals_parte (nome, documento) "
     "WHERE documento LIKE '%%X%%' OR documento LIKE '%%x%%' "
     "OR documento LIKE '%%*%%'"),
    ('uniq_parte_oab',
     "CREATE UNIQUE INDEX CONCURRENTLY uniq_parte_oab "
     "ON tribunals_parte (oab) WHERE oab <> ''"),
]


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cur:
        for nome, create_sql in INDICES:
            cur.execute(f'DROP INDEX IF EXISTS {nome}')
            cur.execute(create_sql)
            cur.execute(
                "SELECT idx.indisvalid FROM pg_index idx "
                "JOIN pg_class i ON i.oid = idx.indexrelid "
                "WHERE i.relname = %s", [nome])
            row = cur.fetchone()
            if not row or not row[0]:
                raise RuntimeError(
                    f'{nome}: indisvalid=false — ainda há duplicata. '
                    f'Rode dedup_partes antes.')


def reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('tribunals', '0029_mv_pipeline_diario')]
    operations = [migrations.RunPython(forward, reverse, elidable=False)]
