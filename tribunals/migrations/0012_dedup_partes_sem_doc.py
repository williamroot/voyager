"""Funde Partes (nome, tipo) duplicadas onde documento='' e oab=''.

Causa raiz: o caminho 4 do `_upsert_parte` (sem doc nem OAB) era
`Parte.objects.create()` cego antes do fix em `054cb70`. Mesmo após o fix
pra `get_or_create`, o lookup falha quando já existem N rows pré-existentes
violando a unicidade pretendida.

Migra em SQL puro pra evitar carregar 317k+ rows em Python:
1. Tabela temp com (dup_id → keep_id) onde keep = MIN(id) por grupo.
2. Deleta ProcessoParte do dup que conflitaria com (processo, keep_id,
   polo, papel, representa) idêntico do keep — preserva o keep.
3. UPDATE remanescente apontando ProcessoParte.parte_id pro keep_id.
4. DELETE das Partes dup.
5. Adiciona constraint UNIQUE(nome, tipo) WHERE doc='' AND oab=''.
"""
from django.db import migrations, models
from django.db.models import Q


def dedup_forward(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        # Mapa (dup_id → keep_id) por (nome, tipo) — só Partes sem doc nem oab.
        cur.execute("""
            CREATE TEMP TABLE _dedup_map ON COMMIT DROP AS
            SELECT id AS dup_id, keep_id
            FROM (
                SELECT id,
                       MIN(id) OVER (PARTITION BY nome, tipo) AS keep_id
                FROM tribunals_parte
                WHERE documento = '' AND oab = ''
            ) x
            WHERE id <> keep_id;
        """)
        cur.execute("CREATE INDEX ON _dedup_map(dup_id);")
        cur.execute("CREATE INDEX ON _dedup_map(keep_id);")
        cur.execute("SELECT COUNT(*) FROM _dedup_map;")
        n_dups = cur.fetchone()[0]
        print(f'  {n_dups:,} Partes a fundir (sem doc nem oab)')

        # Para evitar colisão na unique partial de ProcessoParte
        # (processo, parte, polo, papel) WHERE representa IS NULL após o
        # UPDATE, deletamos prévia: para cada chave alvo
        # (processo, target_parte_id, polo, papel, representa) mantemos só
        # o ProcessoParte com menor id; o resto vai embora.
        cur.execute("""
            CREATE TEMP TABLE _pp_envolvidos ON COMMIT DROP AS
            SELECT pp.id AS pp_id,
                   pp.processo_id,
                   COALESCE(m.keep_id, pp.parte_id) AS target_parte_id,
                   pp.polo, pp.papel, pp.representa_id
            FROM tribunals_processoparte pp
            LEFT JOIN _dedup_map m ON pp.parte_id = m.dup_id
            WHERE m.dup_id IS NOT NULL
               OR EXISTS (SELECT 1 FROM _dedup_map mk WHERE mk.keep_id = pp.parte_id);
        """)
        cur.execute("CREATE INDEX ON _pp_envolvidos(pp_id);")

        cur.execute("""
            CREATE TEMP TABLE _pp_keepers ON COMMIT DROP AS
            SELECT MIN(pp_id) AS keep_pp_id
            FROM _pp_envolvidos
            GROUP BY processo_id, target_parte_id, polo, papel,
                     COALESCE(representa_id, -1);
        """)
        cur.execute("CREATE INDEX ON _pp_keepers(keep_pp_id);")

        cur.execute("""
            DELETE FROM tribunals_processoparte
            WHERE id IN (SELECT pp_id FROM _pp_envolvidos)
              AND id NOT IN (SELECT keep_pp_id FROM _pp_keepers);
        """)
        deleted_pp = cur.rowcount
        print(f'  {deleted_pp:,} ProcessoParte removidos (manteve menor id por chave alvo)')

        # Re-aponta os ProcessoParte sobreviventes do dup → keep
        cur.execute("""
            UPDATE tribunals_processoparte pp
            SET parte_id = m.keep_id
            FROM _dedup_map m
            WHERE pp.parte_id = m.dup_id;
        """)
        moved_pp = cur.rowcount
        print(f'  {moved_pp:,} ProcessoParte re-apontados pro keep')

        # Deleta as Partes dup
        cur.execute("""
            DELETE FROM tribunals_parte
            WHERE id IN (SELECT dup_id FROM _dedup_map);
        """)
        deleted_partes = cur.rowcount
        print(f'  {deleted_partes:,} Partes deletadas')


def dedup_reverse(apps, schema_editor):
    """Não-reversível — perda de duplicatas é definitiva. Aceita rollback
    apenas no nível schema (a constraint cai), os dados ficam fundidos."""
    pass


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0011_parte_doc_mascarado')]

    operations = [
        migrations.RunPython(dedup_forward, dedup_reverse),
        migrations.AddConstraint(
            model_name='parte',
            constraint=models.UniqueConstraint(
                fields=['nome', 'tipo'],
                condition=Q(documento='') & Q(oab=''),
                name='uniq_parte_sem_doc_nem_oab',
            ),
        ),
    ]
