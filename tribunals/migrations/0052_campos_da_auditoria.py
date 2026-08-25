"""Três campos que a auditoria de completude (24-25/08/2026) provou faltarem.

Nenhuma das três operações reescreve tabela — e isso é o ponto, porque
`tribunals_process` tem ~102 M linhas e `tribunals_processoparte` ~84 M, as
duas sob escrita constante (os drainers aplicam ~126 mil events/h).

  · `process.grau` — ADD COLUMN varchar(4) NOT NULL DEFAULT '' + DROP DEFAULT.
    Metadata-only no PG11+ (default constante). O Datajud entrega `grau` em
    20/20 dos `_source` sondados ao vivo e **5 dos 20 eram `JE`** — Juizado
    Especial, que paga por RPV e não por precatório. Sem a coluna, o funil de
    produto mistura dois produtos com prazos e preços diferentes.

  · `processoparte.fonte` — ADD COLUMN varchar(16) NULL, sem default:
    metadata-only em qualquer versão. NULL = legado/enricher; 'djen' = linha
    promovida de `Movimentacao.destinatarios`. A DJEN grava destinatário +
    polo + advogado + OAB em TODA comunicação e isso nunca virou entidade:
    84,8% do acervo (9.467 de 11.160 na amostra, ≈ 86,7 M processos) tem parte
    no JSONB e nenhuma `ProcessoParte`. A coluna é o que permite a tela dizer
    "veio da publicação, não do cadastro" (sem CPF/CNPJ), o rollback por faixa
    e a medição do antes/depois.

  · `process.segredo_justica` — ALTER COLUMN DROP NOT NULL. Catalog-only.
    O `default=False` do BooleanField virou uma AFIRMAÇÃO que ninguém tinha
    feito: `segredo_justica=true` em **0 de 91.638.494** documentos do índice
    (`_count`, não `exists`) e 0 em 120.000 processos amostrados no banco.
    Para 102 M de processos dizíamos "não corre em segredo" sem perguntar uma
    vez — enquanto o e-SAJ responde a página "informe a senha... segredo de
    justiça" em 10 de 11 sondas ao vivo de processos TJSP marcados `ok` SEM
    NENHUMA PARTE (≈ 302 k processos). NULL passa a significar "não
    perguntamos"; False, "perguntamos e a fonte disse que não".
    Linhas existentes continuam `False` — este migration não reescreve 102 M
    de linhas para consertar o passado, e essa dívida está registrada em
    `.ia/ENRICHMENT.md`.

O `SET lock_timeout` é obrigatório e não decorativo: as três operações pedem
ACCESS EXCLUSIVE em tabelas quentes. Sem teto, o ALTER entra na fila atrás de
uma transação longa e passa a BLOQUEAR todo mundo que chegar depois dele —
foi assim que um `DROP INDEX` non-concurrent derrubou o site em julho. Com
teto, o migration falha limpo em 5 s e se tenta de novo. `atomic=True`
(default) mantém o SET válido para as três operações.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0051_indices_fantasma'),
    ]

    operations = [
        migrations.RunSQL(
            sql="SET lock_timeout = '5s';",
            reverse_sql="SET lock_timeout = '5s';",
        ),
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
    ]
