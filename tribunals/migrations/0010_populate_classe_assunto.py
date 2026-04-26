from django.db import migrations


def populate_forward(apps, schema_editor):
    """Popula catálogos ClasseJudicial e Assunto a partir dos campos
    string já existentes em Process e Movimentacao, depois atribui as FKs.

    Estratégia: Process tem nomes mais limpos (vem do PJe consulta pública,
    UPPERCASE acentuado), enquanto DJEN traz com capitalização errada.
    Por isso priorizamos Process pra escolher o nome canônico.
    """
    ClasseJudicial = apps.get_model('tribunals', 'ClasseJudicial')
    Assunto = apps.get_model('tribunals', 'Assunto')

    with schema_editor.connection.cursor() as cur:
        # 1) Catálogo de classes — prioridade Process (PJe), fallback Mov (DJEN)
        cur.execute("""
            INSERT INTO tribunals_classejudicial (codigo, nome, total_processos)
            SELECT codigo, nome, 0
            FROM (
                SELECT classe_codigo AS codigo,
                       MIN(classe_nome) AS nome,
                       1 AS prioridade
                FROM tribunals_process
                WHERE classe_codigo <> '' AND classe_nome <> ''
                GROUP BY classe_codigo
                UNION ALL
                SELECT codigo_classe AS codigo,
                       MIN(nome_classe) AS nome,
                       2 AS prioridade
                FROM tribunals_movimentacao
                WHERE codigo_classe <> '' AND nome_classe <> ''
                GROUP BY codigo_classe
            ) t
            ORDER BY prioridade
            ON CONFLICT (codigo) DO NOTHING;
        """)

        # 2) Catálogo de assuntos — só Process tem
        cur.execute("""
            INSERT INTO tribunals_assunto (codigo, nome, total_processos)
            SELECT assunto_codigo, MIN(assunto_nome), 0
            FROM tribunals_process
            WHERE assunto_codigo <> '' AND assunto_nome <> ''
            GROUP BY assunto_codigo
            ON CONFLICT (codigo) DO NOTHING;
        """)

        # 3) FKs em Process
        cur.execute("""
            UPDATE tribunals_process
            SET classe_id = classe_codigo
            WHERE classe_codigo <> '' AND classe_id IS NULL;
        """)
        cur.execute("""
            UPDATE tribunals_process
            SET assunto_id = assunto_codigo
            WHERE assunto_codigo <> '' AND assunto_id IS NULL;
        """)

        # 4) FK em Movimentacao (1.2M+ linhas — em batch via processo)
        cur.execute("""
            UPDATE tribunals_movimentacao
            SET classe_id = codigo_classe
            WHERE codigo_classe <> '' AND classe_id IS NULL;
        """)

        # 5) Recalcula total_processos por classe e assunto
        cur.execute("""
            UPDATE tribunals_classejudicial c
            SET total_processos = COALESCE(x.n, 0)
            FROM (
                SELECT classe_id, COUNT(*) AS n
                FROM tribunals_process WHERE classe_id IS NOT NULL
                GROUP BY classe_id
            ) x WHERE x.classe_id = c.codigo;
        """)
        cur.execute("""
            UPDATE tribunals_assunto a
            SET total_processos = COALESCE(x.n, 0)
            FROM (
                SELECT assunto_id, COUNT(*) AS n
                FROM tribunals_process WHERE assunto_id IS NOT NULL
                GROUP BY assunto_id
            ) x WHERE x.assunto_id = a.codigo;
        """)


def populate_backward(apps, schema_editor):
    """Reset apenas as FKs e os catálogos — não toca nos campos legacy."""
    with schema_editor.connection.cursor() as cur:
        cur.execute('UPDATE tribunals_process SET classe_id = NULL, assunto_id = NULL;')
        cur.execute('UPDATE tribunals_movimentacao SET classe_id = NULL;')
        cur.execute('DELETE FROM tribunals_classejudicial;')
        cur.execute('DELETE FROM tribunals_assunto;')


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0009_classe_assunto')]

    operations = [
        migrations.RunPython(populate_forward, populate_backward),
    ]
