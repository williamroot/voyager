"""Separa `classe` (o que o CNJ CADASTRA) de `fase` (o que o tribunal PUBLICA).

O VEREDITO QUE MOTIVOU (medido em 31/08/2026, pendência #105)
-------------------------------------------------------------
Rotulávamos 2.337.739 processos com a classe `12078` (*Cumprimento de Sentença
contra a Fazenda Pública*). Cruzando por CNJ contra o `voyager-acervo` — o
esqueleto que veio do Datajud —, o CNJ diz OUTRA classe numa fatia grande do
que rotulamos. Amostra **uniforme por pk** (semente 20260831, 40.000 pks,
39.101 existentes, 861 com o rótulo = 2,20%):

    concorda (CNJ também diz 12078) ............ 608
    discorda (CNJ diz outra classe) ............ 222   26,7% dos conferíveis
    CNJ não tem o CNJ ........................... 31    3,6%

A pergunta era: rótulo errado, ou dois campos colididos? A resposta veio de
canais INDEPENDENTES do campo que gerou o rótulo (usar `codigo_classe` da
movimentação como prova seria circular):

    o TEXTO da publicação diz verbatim
    "CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA (12078) Nº <o CNJ>" .. 207 (93,2%)
    partes do PJe: EXEQUENTE × ente público no polo passivo ................. 10  (4,5%)
    movimento do Datajud de fase de execução ................................  2  (0,9%)
    SEM evidência nenhuma (abstenção) .......................................  3  (1,4%)

⇒ **98,6% das discordâncias são COLISÃO DE CAMPO, não rótulo errado.** O mesmo
processo é, ao mesmo tempo, `Procedimento do Juizado Especial Cível` no
cadastro do CNJ e `Cumprimento de Sentença contra a Fazenda Pública` na fase
que o tribunal publica — e a segunda é a que o produto vende.

Controle negativo da régua (400 processos que NÃO rotulamos 12078): o canal
verbatim dispara em 6 (1,5%). 93,2% contra 1,5% é 62× de separação.

O QUE ESTA MIGRATION FAZ
------------------------
Cria as duas colunas que faltavam, sem tocar em `classe_codigo` (que continua
sendo o campo de compatibilidade que todo mundo já lê):

  · `classe_cnj_codigo` / `classe_cnj_nome` — a classe CADASTRAL. Escritor
    único: a porta do Datajud. É o campo que casa com o `voyager-acervo`.
  · `fase_codigo` / `fase_nome` / `fase_em` — a classe com que o tribunal
    PUBLICOU o processo mais recentemente. `fase_em` é a data da publicação
    que provou a fase: sem ela, uma recoleta de dia antigo rebaixaria a fase
    atual (o backfill de 2023 sobrescreveria o cumprimento de 2026).

POR QUE NENHUM ÍNDICE NOVO
--------------------------
`tribunals_process` tem ~104 M linhas. Um btree em `fase_codigo` custa horas de
`CREATE INDEX CONCURRENTLY` e ~2-3 GB, num banco que a casa já classifica como
disk-I/O-bound. A camada de agregação/filtro é o Elasticsearch — os dois campos
entram no `voyager-processos` (`search/mappings.py`), que é onde a tela e a API
os consultam. Se algum dia um caminho de código precisar filtrar por eles NO
POSTGRES, o índice nasce aí, medido, e não por precaução.

POR QUE `RunSQL` IDEMPOTENTE (a lição da 0052)
----------------------------------------------
A 0052 parou no meio em produção e deixou coluna no banco sem a linha em
`django_migrations` — o `migrate` do boot seguinte teria estourado "column
already exists". `ADD COLUMN IF NOT EXISTS` torna cada passo re-executável.
`ADD COLUMN` com DEFAULT constante é metadata-only no PG 11+: não reescreve as
104 M linhas. `SET LOCAL lock_timeout` porque ACCESS EXCLUSIVE ENFILEIRA —
enquanto o ALTER espera, todo SELECT que chega depois espera também (63 sessões
travadas em 25/08/2026). Se não pegar o lock em 3s, falha e se tenta de novo.
"""
from django.db import migrations, models

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s';"

SQL_APLICA = f"""
{LOCK_TIMEOUT}
ALTER TABLE "tribunals_process" ADD COLUMN IF NOT EXISTS "classe_cnj_codigo" varchar(20)  DEFAULT '' NOT NULL;
ALTER TABLE "tribunals_process" ADD COLUMN IF NOT EXISTS "classe_cnj_nome"   varchar(255) DEFAULT '' NOT NULL;
ALTER TABLE "tribunals_process" ADD COLUMN IF NOT EXISTS "fase_codigo"       varchar(20)  DEFAULT '' NOT NULL;
ALTER TABLE "tribunals_process" ADD COLUMN IF NOT EXISTS "fase_nome"         varchar(255) DEFAULT '' NOT NULL;
ALTER TABLE "tribunals_process" ADD COLUMN IF NOT EXISTS "fase_em"           timestamptz  NULL;
ALTER TABLE "tribunals_process" ALTER COLUMN "classe_cnj_codigo" DROP DEFAULT;
ALTER TABLE "tribunals_process" ALTER COLUMN "classe_cnj_nome"   DROP DEFAULT;
ALTER TABLE "tribunals_process" ALTER COLUMN "fase_codigo"       DROP DEFAULT;
ALTER TABLE "tribunals_process" ALTER COLUMN "fase_nome"         DROP DEFAULT;
"""

SQL_REVERTE = f"""
{LOCK_TIMEOUT}
ALTER TABLE "tribunals_process" DROP COLUMN IF EXISTS "fase_em";
ALTER TABLE "tribunals_process" DROP COLUMN IF EXISTS "fase_nome";
ALTER TABLE "tribunals_process" DROP COLUMN IF EXISTS "fase_codigo";
ALTER TABLE "tribunals_process" DROP COLUMN IF EXISTS "classe_cnj_nome";
ALTER TABLE "tribunals_process" DROP COLUMN IF EXISTS "classe_cnj_codigo";
"""


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0053_indice_atualizado_em'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=SQL_APLICA, reverse_sql=SQL_REVERTE),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='process', name='classe_cnj_codigo',
                    field=models.CharField(blank=True, max_length=20),
                ),
                migrations.AddField(
                    model_name='process', name='classe_cnj_nome',
                    field=models.CharField(blank=True, max_length=255),
                ),
                migrations.AddField(
                    model_name='process', name='fase_codigo',
                    field=models.CharField(blank=True, max_length=20),
                ),
                migrations.AddField(
                    model_name='process', name='fase_nome',
                    field=models.CharField(blank=True, max_length=255),
                ),
                migrations.AddField(
                    model_name='process', name='fase_em',
                    field=models.DateTimeField(blank=True, null=True),
                ),
            ],
        ),
    ]
