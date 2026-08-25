"""Três campos que a auditoria de completude (24-25/08/2026) provou faltarem.

  · `process.grau` — o Datajud entrega `grau` em 20/20 dos `_source` sondados
    ao vivo e **5 dos 20 eram `JE`**. JE = Juizado Especial = paga por RPV,
    não por precatório. Sem a coluna, o funil de produto mistura dois produtos
    com prazos e preços diferentes.

  · `processoparte.fonte` — NULL = legado/enricher; 'djen' = linha promovida
    de `Movimentacao.destinatarios`. A DJEN grava destinatário + polo +
    advogado + OAB em TODA comunicação e isso nunca virou entidade: 84,8% do
    acervo (9.467 de 11.160 na amostra de semente 20260824, ≈ 86,7 M
    processos) tem parte no JSONB e nenhuma `ProcessoParte`. As duas
    procedências não são equivalentes — o destinatário é quem foi intimado
    *naquela comunicação*, sem CPF/CNPJ e sem vínculo advogado→representado —
    e a tela precisa poder dizer isso. Também dá rollback por faixa.

  · `process.segredo_justica` — DROP NOT NULL. O `default=False` era uma
    AFIRMAÇÃO que ninguém tinha feito: `true` em **0 de 91.638.494**
    documentos do índice (`_count`, nunca `exists`) e 0 em 120.000 processos
    amostrados no banco, enquanto o e-SAJ responde a página "informe a
    senha… segredo de justiça" em 10 de 11 sondas ao vivo de processos TJSP
    marcados `ok` SEM NENHUMA PARTE (≈ 302 k). NULL passa a significar "não
    perguntamos". Linhas existentes continuam `False` — este migration NÃO
    reescreve 102 M de linhas, e essa dívida está registrada em
    `.ia/ENRICHMENT.md`.

POR QUE `RunSQL` IDEMPOTENTE E NÃO `AddField` DIRETO
---------------------------------------------------
Porque a primeira tentativa de aplicar isto em produção, em 25/08/2026,
**parou no meio** — e o estado meio-aplicado é o que transforma um migration
barato numa bomba-relógio: `processoparte.fonte` ficou no banco sem que a
0052 constasse em `django_migrations`, e o `migrate` do entrypoint do `web`
teria estourado "column already exists" no próximo boot. `ADD COLUMN IF NOT
EXISTS` + `DROP NOT NULL` (naturalmente idempotente) tornam cada passo
re-executável, então este migration pode ser aplicado, interrompido e
reaplicado sem deixar rastro.

O QUE ACONTECEU, E POR QUE O `lock_timeout` É `SET LOCAL`
--------------------------------------------------------
As três operações pedem ACCESS EXCLUSIVE em tabelas quentes (~102 M e ~84 M
de linhas, sob ~126 mil events/h dos drainers). ACCESS EXCLUSIVE é um lock
que **enfileira**: enquanto o ALTER espera, todo mundo que chega DEPOIS dele
espera também — inclusive quem só queria um SELECT. Foi medido: com um ALTER
preso atrás de uma transação exploratória de 88 min, produção chegou a **63
sessões esperando Lock** e 79 backends ativos. `pg_blocking_pids` apontou o
ALTER como vítima em todos os pares amostrados; cancelá-lo
(`pg_cancel_backend`, NUNCA `pg_terminate_backend` e NUNCA `KILL <db>` no
pgbouncer, que PAUSA o banco) devolveu o número a 0 e os backends a 12.

`SET LOCAL` dentro da transação do migration (`atomic=True`, o default) é o
que garante que o teto acompanhe o ALTER na MESMA transação. Aplicar isto
por um laço de retry em autocommit foi o erro: cada tentativa cancelada deixa
o cliente livre para retentar, mas um cliente que morre (ssh/`docker exec`
derrubado) **deixa o backend rodando server-side, órfão e sem teto** — a
armadilha que o `.ia/OPS.md` já registra para queries longas via pgbouncer.

REGRA DE OPERAÇÃO: só aplique este migration com o banco limpo — nenhuma
transação longa aberta (`SELECT max(now()-xact_start) FROM pg_stat_activity
WHERE state <> 'idle'`). Com transação de 90 min aberta, o ALTER não entra e
o custo de tentar é pago por toda a produção.
"""
from django.db import migrations, models

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s';"

SQL_APLICA = f"""
{LOCK_TIMEOUT}
ALTER TABLE "tribunals_process"       ADD COLUMN IF NOT EXISTS "grau"  varchar(4)  DEFAULT '' NOT NULL;
ALTER TABLE "tribunals_process"       ALTER COLUMN "grau" DROP DEFAULT;
ALTER TABLE "tribunals_processoparte" ADD COLUMN IF NOT EXISTS "fonte" varchar(16) NULL;
ALTER TABLE "tribunals_process"       ALTER COLUMN "segredo_justica" DROP NOT NULL;
"""

SQL_REVERTE = f"""
{LOCK_TIMEOUT}
UPDATE "tribunals_process" SET "segredo_justica" = false WHERE "segredo_justica" IS NULL;
ALTER TABLE "tribunals_process"       ALTER COLUMN "segredo_justica" SET NOT NULL;
ALTER TABLE "tribunals_processoparte" DROP COLUMN IF EXISTS "fonte";
ALTER TABLE "tribunals_process"       DROP COLUMN IF EXISTS "grau";
"""


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0051_indices_fantasma'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=SQL_APLICA, reverse_sql=SQL_REVERTE),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='process',
                    name='grau',
                    field=models.CharField(blank=True, max_length=4),
                ),
                migrations.AddField(
                    model_name='processoparte',
                    name='fonte',
                    field=models.CharField(blank=True, max_length=16, null=True),
                ),
                migrations.AlterField(
                    model_name='process',
                    name='segredo_justica',
                    field=models.BooleanField(blank=True, default=None, null=True),
                ),
            ],
        ),
    ]
