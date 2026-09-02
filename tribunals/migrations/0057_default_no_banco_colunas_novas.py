"""Devolve o `DEFAULT ''` que a 0054 tirou — e que a 0052 já tinha custado caro.

O QUE QUEBROU (medido em 01-02/09/2026)
---------------------------------------
`EdicaoDiario` do `tjsp-dje`, em produção, com a terceira porta ligada desde
24/08 (`DIARIOS.md` §13):

    falha ......... 257     pendente ..... 50     ok ...... 62
    ├─ 215 · null value in column "classe_cnj_codigo" ... violates not-null
    ├─  40 · null value in column "grau"               ... violates not-null
    └─   2 · segmentação abaixo do piso  ← outra natureza, não é isto

E o outro lado, na mesma janela: **1.043 `IngestionRun` do `tjsp-dje` em
01/09 e 32 em 02/09** com `classe_cnj_codigo` na mensagem de erro. A porta
não estava devagar: estava fechada por um INSERT.

A CAUSA, CONFERIDA COLUNA A COLUNA EM `information_schema`
----------------------------------------------------------
A 0054 fez o idioma padrão do Django — `ADD COLUMN ... DEFAULT '' NOT NULL`
seguido de `DROP DEFAULT`:

    classe_cnj_codigo  is_nullable=NO  column_default=None   ← sem rede
    classe_cnj_nome    is_nullable=NO  column_default=None   ← sem rede
    fase_codigo        is_nullable=NO  column_default=None   ← sem rede
    fase_nome          is_nullable=NO  column_default=None   ← sem rede
    fase_em            is_nullable=YES column_default=None
    grau               is_nullable=NO  column_default=''     ← COM rede

Coluna NOT NULL sem `DEFAULT` só aceita INSERT que **nomeie** a coluna. O
Django nomeia todas as colunas do model **carregado em memória** — e o
`worker_diarios` da `.102` tem `StartedAt = 2026-08-24T20:06:49Z`, sete dias
antes da 0054 (aplicada em 2026-08-31 15:19:05 UTC). O bind mount `.:/app`
entrega o arquivo novo; o processo Python não reimporta o módulo. Logo o
INSERT sai sem a coluna e o Postgres recusa — a edição inteira falha, porque
`bulk_create` é um statement só.

A prova de que é o DEFAULT que separa "sobrevive" de "quebra" está na tabela
acima: `grau` nasceu com o mesmo idioma na 0052 e **derrubou o dia 25/08**
(10.410 runs `failed`, 32 publicações no dia contra 1,5 M do dia anterior).
A cura de então foi `ALTER COLUMN grau SET DEFAULT ''` na mão, em 30 s, e
`grau` parou de falhar na hora — as 40 falhas de `grau` que sobraram aqui são
mais antigas que essa cura. Seis dias depois, a 0054 repetiu o idioma.

POR QUE ISTO, E NÃO SÓ REINICIAR A FROTA
-----------------------------------------
Reiniciar é necessário e não é a cura. Entre `migrate` e restart existe um
intervalo — nesta casa ele é de dias, porque web, scheduler, 5 drainers e
~240 workers recarregam em momentos diferentes e alguns só no próximo
`--force-recreate`. O `DEFAULT` fecha o intervalo: ele **só é usado por quem
não conhece a coluna**, que é exatamente o escritor atrasado que queremos que
sobreviva. Código atual continua mandando o valor explícito e nada muda para
ele — o `DEFAULT` nunca entra no caminho de quem nomeia a coluna.

A regra já estava escrita em `.ia/OPS.md` desde 25/08 ("coluna NOT NULL nova
em tabela que já tem escritor em produção NUNCA fica sem `DEFAULT` no banco").
Ela não foi seguida porque nada a cobrava. Agora cobra:
`tests/test_default_no_banco.py` congela a lista das colunas NOT NULL sem
`DEFAULT` das quatro tabelas quentes e reprova qualquer migration que
acrescente uma.

`grau` ENTRA AQUI DE PROPÓSITO
------------------------------
Em produção `grau` já tem o `DEFAULT` — mas por um `ALTER` de incidente, que
**nenhuma migration registra**. Um banco novo criado a partir das migrations
nasceria sem ele: prod e migrations divergindo em silêncio, que é a doença
que a 0056 acabou de inventariar. O `SET DEFAULT` de `grau` aqui é no-op em
produção e alinhamento em qualquer ambiente novo.

CUSTO
-----
`ALTER COLUMN ... SET DEFAULT` é catálogo puro: não lê, não reescreve e não
valida nenhuma das ~104 M linhas — o `pg_attrdef` ganha uma linha e acabou.
O que ele pede é `ACCESS EXCLUSIVE` por um instante, e ACCESS EXCLUSIVE
**enfileira**: enquanto o ALTER espera, todo SELECT que chega depois espera
atrás dele (63 sessões travadas em 25/08/2026). Daí `SET LOCAL lock_timeout`
DENTRO da transação do migration: se não pegar o lock em 3 s, o migration
falha inteiro e se tenta de novo. **Não suba o teto** — subir o teto é
comprar o auto-jam. As quatro colunas seguintes saem de graça: a primeira já
segura o lock pela transação toda.
"""
from django.db import migrations

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s';"

COLUNAS = ('classe_cnj_codigo', 'classe_cnj_nome', 'fase_codigo', 'fase_nome', 'grau')

SQL_APLICA = LOCK_TIMEOUT + '\n' + '\n'.join(
    f'ALTER TABLE "tribunals_process" ALTER COLUMN "{c}" SET DEFAULT \'\';'
    for c in COLUNAS
)

# Reverter é voltar a bomba para a mesa. Existe para o migration ser
# reversível, não porque alguém deva rodá-lo.
SQL_REVERTE = LOCK_TIMEOUT + '\n' + '\n'.join(
    f'ALTER TABLE "tribunals_process" ALTER COLUMN "{c}" DROP DEFAULT;'
    for c in COLUNAS
)


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0056_schema_real'),
    ]

    operations = [
        # Só banco: o Django não representa DEFAULT de banco no estado, então
        # não há `state_operations` a fazer. O model já declara `blank=True`
        # e continua mandando '' explicitamente.
        migrations.RunSQL(sql=SQL_APLICA, reverse_sql=SQL_REVERTE),
    ]
