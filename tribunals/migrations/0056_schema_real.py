"""O estado passa a dizer o que o banco TEM — e recria as FKs que sumiram.

Inventário medido em 01/09/2026 (#111), comparando as migrations com a `.101`
por COLUNA, nunca por nome — `tribunals/schema_auditoria.py`, que é a régua e
fica no repositório justamente para isto não voltar a ser um achado pontual:

    37 modelos · CONTROLE: 37/37 PKs encontradas (100%)

    declarado e AUSENTE .......... 23 objetos
      FK ............ 14   em 6 tabelas
      índice ......... 7
      unique ......... 1   (`uniq_alerta_aberto_tribunal_tipo_chaves`)
      nome_colidido .. 1   (`proc_tribunal_id_idx`)
    tabela ausente ... 0 · coluna ausente ... 0 · tipo divergente ... 0

As 3 FKs de `tribunals_process` que abriram a pendência eram a ponta: o banco
tem **zero** constraints do tipo `f` em SEIS tabelas — `tribunals_process`,
`tribunals_movimentacao`, `tribunals_processoparte`, `tribunals_ingestionrun`,
`tribunals_schemadriftalert` e `accounts_invite` —, enquanto as 21 tabelas mais
NOVAS têm as suas. É a assinatura de um schema reconstruído à mão nas tabelas
antigas, com as migrations marcadas depois; a mesma que apagou os triggers
`mov_update_process_agg`, `pp_total_ins`/`pp_total_del` e `process_set_ano_cnj`
(este recriado pela `0042`).

Nenhum `CREATE`/`DROP` de índice roda aqui — `database_operations=[]`
--------------------------------------------------------------------
As três operações de índice são **só de estado**. Aplicá-las no banco faria um
`DROP INDEX` não-concorrente de `proc_tribunal_id_idx` em 104 M de linhas, que
é exatamente o incidente de 25/08 (ACCESS EXCLUSIVE enfileira: o `DROP` esperou
e 63 sessões esperaram atrás dele).

  1. `movimentacao.classe` — índice declarado que nunca existiu, e que **não
     deve ser criado**. Medido: a tabela tem 1,89 TB / 1,55 bi de linhas; a
     única consulta do código que toca `classe_id` entra por faixa de pk
     (`Index Cond` na pkey, custo 5.334) e não usaria o índice; nenhuma outra
     filtra ou junta por ali. Índice que ninguém consulta é custo puro de
     escrita, e aqui seriam ~35-45 GB mantidos no caminho da ingestão diária.
  2. + 3. `proc_tribunal_id_idx` — declarado `(tribunal, -id)`, existe no banco
     como `btree(tribunal_id)`. Mesmo nome, colunas diferentes: é o caso que
     passa por TODA verificação por nome. A declaração passa a dizer a verdade;
     quem precisar de `(tribunal, -id)` cria um índice novo, de propósito.

O que ROda no banco: as FKs pequenas
------------------------------------
`recriar_fks_pequenas` recria, **por definição (coluna), nunca por nome**, as
FKs declaradas e ausentes em tabelas de até 1 GB — que é onde o lock é
instantâneo e a varredura é trivial. Idempotente: se a FK já existe, não faz
nada (e não usa `IF NOT EXISTS`, que esconderia uma FK apontando para outro
lugar).

As tabelas grandes ficam DE FORA, e isso é operação, não deploy:

    manage.py auditar_schema --reparar-fks    # ADD ... NOT VALID  (não varre)
    manage.py auditar_schema --validar-fks    # VALIDATE           (não bloqueia
                                              #                     escrita)

`tribunals_movimentacao` (1,89 TB) exige `--max-bytes` subido de propósito.
"""
import django.db.models.deletion
from django.db import migrations, models

#: FKs declaradas que o banco não tinha em 01/09/2026, com o nome que o Django
#: geraria. Só as de tabela pequena são recriadas aqui (ver TETO_BYTES).
FKS = [
    # (tabela, coluna, tabela_alvo, coluna_alvo, nome)
    ('accounts_invite', 'created_by_id', 'auth_user', 'id',
     'accounts_invite_created_by_id_1b50b3e4_fk_auth_user_id'),
    ('accounts_invite', 'used_by_id', 'auth_user', 'id',
     'accounts_invite_used_by_id_32c9dbc9_fk_auth_user_id'),
    ('tribunals_ingestionrun', 'tribunal_id', 'tribunals_tribunal', 'sigla',
     'tribunals_ingestionr_tribunal_id_378d6673_fk_tribunals'),
    ('tribunals_schemadriftalert', 'tribunal_id', 'tribunals_tribunal', 'sigla',
     'tribunals_schemadrif_tribunal_id_2a841dc4_fk_tribunals'),
    ('tribunals_schemadriftalert', 'ingestion_run_id', 'tribunals_ingestionrun',
     'id', 'tribunals_schemadrif_ingestion_run_id_fc659a7b_fk_tribunals'),
]

#: acima disto o ALTER é decisão de operação (janela), não de deploy.
TETO_BYTES = 1_000_000_000


def recriar_fks_pequenas(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '3s'")
        for tabela, coluna, alvo, col_alvo, nome in FKS:
            cur.execute('SELECT to_regclass(%s)', [tabela])
            if cur.fetchone()[0] is None:
                continue
            # conferência por COLUNA — o nome é o que engana
            cur.execute("""
                SELECT 1 FROM pg_constraint c
                 WHERE c.conrelid = %s::regclass AND c.contype = 'f'
                   AND c.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a
                                          WHERE a.attrelid = %s::regclass
                                            AND a.attname = %s)]::smallint[]
            """, [tabela, tabela, coluna])
            if cur.fetchone():
                continue
            cur.execute('SELECT pg_total_relation_size(%s::regclass)', [tabela])
            if cur.fetchone()[0] > TETO_BYTES:
                continue        # janela de manutenção, via `auditar_schema`
            cur.execute(
                f'ALTER TABLE "{tabela}" ADD CONSTRAINT "{nome}" '
                f'FOREIGN KEY ("{coluna}") REFERENCES "{alvo}" ("{col_alvo}") '
                f'DEFERRABLE INITIALLY DEFERRED')


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0055_seed_stm')]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveIndex(
                    model_name='movimentacao',
                    name='tribunals_m_classe__1df891_idx'),
                # o índice IMPLÍCITO da FK conta igual: sem `db_index=False` o
                # estado continuaria declarando um btree em `classe_id`.
                migrations.AlterField(
                    model_name='movimentacao', name='classe',
                    field=models.ForeignKey(
                        blank=True, db_index=False, null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='movimentacoes',
                        to='tribunals.classejudicial')),
                migrations.RemoveIndex(
                    model_name='process', name='proc_tribunal_id_idx'),
                migrations.AddIndex(
                    model_name='process',
                    index=models.Index(fields=['tribunal'],
                                       name='proc_tribunal_id_idx')),
            ],
        ),
        migrations.RunPython(recriar_fks_pequenas, migrations.RunPython.noop),
    ]
