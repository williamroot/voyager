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

A CAUSA, CONFERIDA COLUNA A COLUNA EM `pg_attrdef`
--------------------------------------------------
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
`worker_diarios` da `.102` tinha `StartedAt = 2026-08-24T20:06:49Z`, sete dias
antes da 0054 (aplicada em 2026-08-31 15:19:05 UTC). O bind mount `.:/app`
entrega o arquivo novo; o processo Python não reimporta o módulo. Logo o
INSERT sai sem a coluna e o Postgres recusa — e a edição INTEIRA falha, porque
`bulk_create` é um statement só.

A prova de que é o `DEFAULT` que separa "sobrevive" de "quebra" está numa
única linha do log de produção (02/09, 00:49:01 UTC), no `DETAIL` da própria
exceção: a linha recusada termina em `…, '', null, null, null, null, null)` —
`grau` **preenchido pelo DEFAULT do banco**, e as quatro colunas da 0054
nulas. Mesmo INSERT, mesmo escritor atrasado, dois destinos.

`grau` nasceu com o mesmo idioma na 0052 e derrubou o dia 25/08: **10.410
`IngestionRun` `failed`** e **32** publicações contra 1,5 M do dia anterior.
A cura foi um `ALTER COLUMN grau SET DEFAULT ''` de 30 s. As 40 falhas de
`grau` que sobraram no catálogo são anteriores a essa cura.

POR QUE ISTO, E NÃO SÓ REINICIAR A FROTA
-----------------------------------------
Reiniciar é necessário e não é a cura. Entre `migrate` e restart existe uma
janela — nesta casa ela é de dias, porque web, scheduler, 5 drainers e ~240
workers recarregam em momentos diferentes e alguns só no próximo
`--force-recreate`. O `DEFAULT` fecha a janela: ele **só é usado por quem não
conhece a coluna**, que é exatamente o escritor atrasado que queremos que
sobreviva. Código atual continua mandando o valor explícito e nada muda para
ele — o `DEFAULT` nunca entra no caminho de quem nomeia a coluna.

A regra já estava escrita em `.ia/OPS.md` desde 25/08 ("coluna NOT NULL nova
em tabela que já tem escritor em produção NUNCA fica sem `DEFAULT` no banco").
Ela não foi seguida porque nada a cobrava. Agora cobra:
`tests/test_default_no_banco.py`.

`grau` ENTRA AQUI DE PROPÓSITO
------------------------------
Em produção `grau` já tem o `DEFAULT` — mas por um `ALTER` de incidente, que
**nenhuma migration registra**. Um banco novo criado a partir das migrations
nasceria sem ele: prod e migrations divergindo em silêncio, que é a doença que
a 0056 acabou de inventariar. O `SET DEFAULT` de `grau` aqui é no-op em
produção e alinhamento em qualquer ambiente novo.

POR QUE `RunPython` COM LAÇO, E NÃO `RunSQL` DIRETO
----------------------------------------------------
Duas restrições que colidem, e a colisão é o motivo desta seção existir.

1. `ALTER TABLE ... SET DEFAULT` é catálogo puro — não lê nem reescreve
   nenhuma das ~104 M linhas —, mas pede `ACCESS EXCLUSIVE`, e ACCESS
   EXCLUSIVE **enfileira**: enquanto o ALTER espera, todo SELECT que chega
   depois espera atrás dele (63 sessões travadas em 25/08/2026). Por isso
   `lock_timeout` curto, de 3 s, e **não se sobe o teto** — subir o teto é
   comprar o auto-jam.
2. O `web` de produção roda `migrate --noinput &&  gunicorn` no comando do
   container (`docker-compose-prod.yml`). Migration que estoura por lock
   **impede o `web` de subir**. Um migration de blindagem não pode virar a
   causa de uma indisponibilidade.

A saída é a ordem certa de três coisas:

  a) **Pergunta antes de agir.** Se as cinco colunas já têm `DEFAULT`, a
     migration não emite ALTER nenhum e **não pede lock nenhum** — custo zero
     e risco zero em todo boot posterior ao primeiro. É o que torna seguro
     deixá-la no caminho do `web`.
  b) **Laço com teto curto.** Medido em 02/09/2026: o bloqueador não era um
     UPDATE curto, eram SELECTs de agregação de até **1.081 s** segurando
     `AccessShare` em `tribunals_process` — contra esses, nenhum teto curto
     vence na primeira tentativa; o que vence é chegar na fresta. Daí
     `TENTATIVAS` × 3 s, em autocommit (`atomic = False`), uma transação por
     tentativa.
  c) **Falhar alto no fim.** Esgotadas as tentativas, a migration ESTOURA.
     Não existe caminho que a marque como aplicada sem ter aplicado: coluna
     declarada-e-ausente é a doença que a 0056 inventariou, e trocá-la por um
     `web` que sobe é trocar um problema visível por um invisível. Se isso
     acontecer, aplique-a à mão pelo worker (nunca pelo boot do `web`), que é
     o procedimento da casa:

         docker exec voyager-worker_diarios-1 \
             python manage.py migrate tribunals 0057
"""
import time

from django.db import migrations

LOCK_TIMEOUT = "3s"
TENTATIVAS = 40
PAUSA_S = 0.4

COLUNAS = ('classe_cnj_codigo', 'classe_cnj_nome', 'fase_codigo', 'fase_nome', 'grau')
TABELA = 'tribunals_process'

SQL_FALTANTES = """
SELECT a.attname
FROM pg_class cl
JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef ad ON ad.adrelid = cl.oid AND ad.adnum = a.attnum
WHERE cl.relname = %s AND a.attname = ANY(%s) AND ad.adbin IS NULL
"""


def _sem_default(cursor, colunas):
    """As colunas que AINDA não têm `DEFAULT` no banco. Pergunta por COLUNA
    (`pg_attrdef`), nunca pela presença do nome da migration."""
    cursor.execute(SQL_FALTANTES, [TABELA, list(colunas)])
    return [linha[0] for linha in cursor.fetchall()]


def _aplicar(connection, expressao):
    """`SET DEFAULT <expressao>` nas colunas que faltarem, com teto de espera."""
    with connection.cursor() as cursor:
        faltam = _sem_default(cursor, COLUNAS)
    if not faltam:
        print(f'0057: as {len(COLUNAS)} colunas já estão como se pede — nada a fazer, '
              f'nenhum lock pedido')
        return

    ultimo = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            with connection.cursor() as cursor:
                cursor.execute('BEGIN')
                cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                for coluna in faltam:
                    cursor.execute(
                        f'ALTER TABLE "{TABELA}" ALTER COLUMN "{coluna}" '
                        f'SET DEFAULT {expressao}'
                    )
                cursor.execute('COMMIT')
            print(f'0057: DEFAULT {expressao} em {len(faltam)} coluna(s) '
                  f'na tentativa {tentativa}: {", ".join(faltam)}')
            return
        except Exception as exc:  # LockNotAvailable, quase sempre
            ultimo = exc
            with connection.cursor() as cursor:
                cursor.execute('ROLLBACK')
            time.sleep(PAUSA_S)

    raise RuntimeError(
        f'0057: não consegui ACCESS EXCLUSIVE em {TABELA} em {TENTATIVAS} tentativas '
        f'de {LOCK_TIMEOUT} (último erro: {ultimo}). NÃO subir o lock_timeout — '
        f'ACCESS EXCLUSIVE enfileira e trava leitura. Aplique pelo worker: '
        f'docker exec voyager-worker_diarios-1 python manage.py migrate tribunals 0057'
    )


def aplicar(apps, schema_editor):
    _aplicar(schema_editor.connection, "''")


def reverter(apps, schema_editor):
    """Reverter é pôr a bomba de volta na mesa. Existe para a migration ser
    reversível, não porque alguém deva rodá-lo."""
    connection = schema_editor.connection
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            with connection.cursor() as cursor:
                cursor.execute('BEGIN')
                cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                for coluna in COLUNAS:
                    cursor.execute(
                        f'ALTER TABLE "{TABELA}" ALTER COLUMN "{coluna}" DROP DEFAULT')
                cursor.execute('COMMIT')
                return
        except Exception:
            with connection.cursor() as cursor:
                cursor.execute('ROLLBACK')
            time.sleep(PAUSA_S)
    raise RuntimeError('0057: não consegui o lock para reverter')


class Migration(migrations.Migration):
    # autocommit: o laço precisa de uma transação POR tentativa, e uma
    # transação que já falhou não aceita o próximo ALTER.
    atomic = False

    dependencies = [
        ('tribunals', '0056_schema_real'),
    ]

    operations = [
        # Só banco: o Django não representa DEFAULT de banco no estado, então
        # não há `state_operations` a fazer. O model já declara `blank=True`
        # e continua mandando '' explicitamente.
        migrations.RunPython(aplicar, reverter),
    ]
