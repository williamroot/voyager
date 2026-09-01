"""A régua que compara migrations × banco por COLUNA — e o que ela pegou (#111).

Medido em produção (`.101`) em 01/09/2026:

    37 modelos · CONTROLE 37/37 PKs (100%) · 23 objetos DECLARADOS E AUSENTES
      FK 14 · índice 7 · unique 1 · nome colidido 1

`SELECT conname FROM pg_constraint WHERE conrelid='tribunals_process'::regclass
AND contype='f'` devolvia **zero linhas** — e o mesmo valia para
`tribunals_movimentacao`, `tribunals_processoparte`, `tribunals_ingestionrun`,
`tribunals_schemadriftalert` e `accounts_invite`, enquanto as 21 tabelas mais
novas tinham as suas. `makemigrations` compara com o ESTADO, não com o banco,
então nada disso aparecia.

O que estes testes garantem
---------------------------
1. o **controle** da régua fecha em 100% (PK é o objeto que certamente existe);
2. o banco construído pelas migrations não tem NENHUM objeto declarado e
   ausente — é o guarda que quebra se alguém declarar um índice/FK que as
   migrations não criam (foi assim que os três índices fantasma da `0051`
   viveram meses);
3. a régua PEGA uma FK derrubada (mutação);
4. a régua PEGA o caso `proc_tribunal_id_idx`: **mesmo nome, colunas erradas** —
   o que passa por `\\di`, por `pg_indexes.indexname` e pelo `makemigrations`;
5. a `0056` recria a FK pequena **por definição**, e é idempotente;
6. as duas declarações que a `0056` acertou continuam acertadas.
"""
import importlib

import pytest
from django.db import connection

from tribunals.models import Movimentacao, Process
from tribunals.schema_auditoria import inventariar

mig0056 = importlib.import_module('tribunals.migrations.0056_schema_real')


def _fks_no_banco(tabela: str) -> set[str]:
    with connection.cursor() as cur:
        cur.execute("""
            SELECT conname FROM pg_constraint
             WHERE conrelid = %s::regclass AND contype = 'f'
        """, [tabela])
        return {r[0] for r in cur.fetchall()}


def _colunas_do_indice(nome: str) -> list[str]:
    with connection.cursor() as cur:
        cur.execute("""
            SELECT (SELECT array_agg(a.attname ORDER BY k.ord)
                      FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                      LEFT JOIN pg_attribute a
                             ON a.attrelid = i.indrelid AND a.attnum = k.attnum)
              FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
             WHERE ic.relname = %s
        """, [nome])
        linha = cur.fetchone()
        return list(linha[0]) if linha else []


# --------------------------------------------------------------------------- #
# 1 + 2 — a régua e o guarda
# --------------------------------------------------------------------------- #

@pytest.mark.django_db(transaction=False)
def test_controle_da_regua_fecha_em_100_por_cento():
    """PK é o objeto que CERTAMENTE existe. Se este não fecha, tudo é lixo."""
    inv = inventariar(connection)
    assert inv.controle_pk_esperadas > 0, 'a régua não achou nenhum modelo'
    assert inv.controle_ok, (
        f'CONTROLE em {inv.controle_pk_encontradas}/{inv.controle_pk_esperadas} '
        '— a régua está torta e nenhum número dela pode ser publicado'
    )


@pytest.mark.django_db(transaction=False)
def test_banco_das_migrations_tem_tudo_que_o_estado_declara():
    """Guarda de CI: declarar objeto que a migration não cria quebra aqui.

    Foi exatamente essa lacuna que deixou `mov_texto_trgm`, `mov_search_vector_gin`
    e o índice de `hash` viverem meses no model sem existirem no banco — e virarem
    premissa em `api/filters.py` (custo 111.195.298) e `diarios/base.py`
    (73.427.276 por lote, no caminho de escrita).
    """
    inv = inventariar(connection)
    ausentes = inv.ausentes()
    assert not ausentes, (
        'objetos declarados que as migrations NÃO criam:\n'
        + '\n'.join('  ' + a.como_linha() for a in ausentes)
    )


# --------------------------------------------------------------------------- #
# 3 — mutação: derruba a FK e a régua tem que acusar
# --------------------------------------------------------------------------- #

@pytest.mark.django_db(transaction=False)
def test_regua_pega_fk_derrubada():
    nomes = _fks_no_banco('tribunals_process')
    assert nomes, 'controle: o banco de teste tem FK em tribunals_process'
    alvo = sorted(nomes)[0]

    antes = [a for a in inventariar(connection).ausentes() if a.tipo == 'fk']
    assert antes == [], f'partiu sujo: {antes}'

    with connection.cursor() as cur:
        cur.execute(f'ALTER TABLE tribunals_process DROP CONSTRAINT "{alvo}"')

    depois = [a for a in inventariar(connection).ausentes()
              if a.tipo == 'fk' and a.tabela == 'tribunals_process']
    assert len(depois) == 1, (
        f'a régua não viu a FK {alvo} sumir — ela é o único instrumento que '
        f'olharia, porque makemigrations compara com o ESTADO. achados: {depois}'
    )


# --------------------------------------------------------------------------- #
# 4 — mutação: MESMO NOME, colunas erradas (o caso `proc_tribunal_id_idx`)
# --------------------------------------------------------------------------- #

@pytest.mark.django_db(transaction=False)
def test_regua_pega_indice_com_nome_certo_e_colunas_erradas():
    """O pior dos três: a conferência por nome responde "existe"."""
    nome = 'proc_enriq_id_idx'          # declarado (enriquecimento_status, -id)
    assert _colunas_do_indice(nome) == ['enriquecimento_status', 'id'], \
        'controle: o índice parte correto no banco de teste'

    antes = [a for a in inventariar(connection).achados if a.tipo == 'nome_colidido']
    assert antes == [], f'partiu sujo: {antes}'

    with connection.cursor() as cur:
        cur.execute(f'DROP INDEX "{nome}"')
        # recria com o MESMO nome e UMA coluna — a assinatura do defeito real
        cur.execute(f'CREATE INDEX "{nome}" ON tribunals_process '
                    '(enriquecimento_status)')

    assert _colunas_do_indice(nome) == ['enriquecimento_status'], 'mutação aplicada'
    achados = [a for a in inventariar(connection).achados if a.tipo == 'nome_colidido']
    assert len(achados) == 1 and achados[0].objeto == nome, (
        'a régua aceitou um índice com o nome certo e as colunas erradas — '
        f'é o defeito do `proc_tribunal_id_idx`. achados: {achados}'
    )
    assert 'enriquecimento_status' in achados[0].detalhe


# --------------------------------------------------------------------------- #
# 5 — a migration 0056 recria a FK pequena, por definição, e é idempotente
# --------------------------------------------------------------------------- #

@pytest.mark.django_db(transaction=False)
def test_0056_recria_fk_pequena_por_definicao_e_e_idempotente():
    tabela = 'tribunals_ingestionrun'
    antes = _fks_no_banco(tabela)
    assert antes, 'controle: o banco de teste tem a FK'

    with connection.cursor() as cur:
        for nome in antes:
            cur.execute(f'ALTER TABLE {tabela} DROP CONSTRAINT "{nome}"')
    assert _fks_no_banco(tabela) == set(), 'mutação aplicada'

    with connection.schema_editor(atomic=False) as se:
        mig0056.recriar_fks_pequenas(None, se)
    recriadas = _fks_no_banco(tabela)
    assert len(recriadas) == 1, f'a 0056 não recriou a FK de {tabela}: {recriadas}'

    # idempotência: rodar de novo não duplica nem estoura
    with connection.schema_editor(atomic=False) as se:
        mig0056.recriar_fks_pequenas(None, se)
    assert _fks_no_banco(tabela) == recriadas


@pytest.mark.django_db(transaction=False)
def test_0056_confere_por_coluna_e_nao_por_nome():
    """FK com OUTRO nome mas a MESMA coluna já satisfaz — e não é duplicada."""
    tabela = 'tribunals_ingestionrun'
    with connection.cursor() as cur:
        for nome in _fks_no_banco(tabela):
            cur.execute(f'ALTER TABLE {tabela} DROP CONSTRAINT "{nome}"')
        cur.execute(f'ALTER TABLE {tabela} ADD CONSTRAINT nome_qualquer_da_casa '
                    'FOREIGN KEY (tribunal_id) REFERENCES tribunals_tribunal (sigla) '
                    'DEFERRABLE INITIALLY DEFERRED')

    with connection.schema_editor(atomic=False) as se:
        mig0056.recriar_fks_pequenas(None, se)

    assert _fks_no_banco(tabela) == {'nome_qualquer_da_casa'}, (
        'a 0056 conferiu por NOME e criou uma segunda FK na mesma coluna'
    )


# --------------------------------------------------------------------------- #
# 5b — o command: NOT VALID primeiro, VALIDATE depois
# --------------------------------------------------------------------------- #

@pytest.mark.django_db(transaction=False)
def test_auditar_schema_repara_em_duas_etapas():
    """`--reparar-fks` cria NOT VALID (não varre); `--validar-fks` valida."""
    from io import StringIO

    from django.core.management import call_command

    alvos = _fks_no_banco('tribunals_process')
    assert alvos, 'controle: o banco de teste tem FK em tribunals_process'
    with connection.cursor() as cur:
        for nome in alvos:
            cur.execute(f'ALTER TABLE tribunals_process DROP CONSTRAINT "{nome}"')

    call_command('auditar_schema', reparar_fks=True, tentativas=3, espera=0,
                 stdout=StringIO(), stderr=StringIO())

    with connection.cursor() as cur:
        cur.execute("""
            SELECT conname, convalidated FROM pg_constraint
             WHERE conrelid = 'tribunals_process'::regclass AND contype = 'f'
        """)
        criadas = dict(cur.fetchall())
    assert len(criadas) == len(alvos), f'faltou recriar: {criadas}'
    assert not any(criadas.values()), (
        'a etapa 1 validou junto — ADD CONSTRAINT sem NOT VALID varre a tabela '
        'INTEIRA segurando ACCESS EXCLUSIVE, que é o auto-jam de 25/08'
    )

    call_command('auditar_schema', validar_fks=True,
                 stdout=StringIO(), stderr=StringIO())
    with connection.cursor() as cur:
        cur.execute("""
            SELECT bool_and(convalidated) FROM pg_constraint
             WHERE conrelid = 'tribunals_process'::regclass AND contype = 'f'
        """)
        assert cur.fetchone()[0] is True, 'a etapa 2 não validou'

    assert [a for a in inventariar(connection).ausentes()
            if a.tipo == 'fk' and a.tabela == 'tribunals_process'] == []


@pytest.mark.django_db(transaction=False)
def test_recusa_fk_em_coluna_sem_indice():
    """FK sem índice na coluna que referencia é bomba — e custou 13 min em prod.

    Criada em 01/09/2026 pelo próprio `--reparar-fks`,
    `processoparte.representa_id -> self` teve que ser derrubada: quem paga é o
    lado REFERENCIADO. Apagar uma `ProcessoParte` dispara
    `SELECT 1 FROM tribunals_processoparte WHERE representa_id = $1` por linha,
    e sem índice isso é Seq Scan de 35 GB — no caminho de
    `enrichers/drainer.py:423`, que apaga no hot path do drainer.
    """
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    alvo = 'tribunals_processopa_representa_id_a5628757_fk_tribunals'
    with connection.cursor() as cur:
        cur.execute('SELECT conname FROM pg_constraint '
                    "WHERE conrelid = 'tribunals_processoparte'::regclass "
                    "AND contype = 'f'")
        antes = {r[0] for r in cur.fetchall()}
        assert any('representa' in n for n in antes), 'controle: a FK existe'
        for nome in antes:
            if 'representa' in nome:
                alvo = nome
                cur.execute('ALTER TABLE tribunals_processoparte '
                            f'DROP CONSTRAINT "{nome}"')
        # e o índice que a tornaria segura também não existe em produção
        cur.execute("""
            SELECT 1 FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
            WHERE t.relname = 'tribunals_processoparte' AND a.attname = 'representa_id'
        """)
        indice = cur.fetchone()
        if indice:
            cur.execute('DROP INDEX IF EXISTS '
                        'tribunals_processoparte_representa_id_a5628757')

    err = StringIO()
    with pytest.raises(CommandError):
        call_command('auditar_schema', reparar_fks=True, tentativas=1, espera=0,
                     stdout=StringIO(), stderr=err)
    assert 'SEM ÍNDICE' in err.getvalue(), (
        'o command criou (ou tentou criar) FK em coluna sem índice: '
        f'{err.getvalue()!r}'
    )
    with connection.cursor() as cur:
        cur.execute('SELECT count(*) FROM pg_constraint '
                    "WHERE conname = %s", [alvo])
        assert cur.fetchone()[0] == 0, 'criou a FK bomba apesar da guarda'


@pytest.mark.django_db(transaction=True)
def test_lock_timeout_nao_mata_a_corrida_e_vira_erro_registrado():
    """`lock_timeout` é TRANSITÓRIO: retenta e termina em ERRO com o número real.

    Sem a leitura do `sqlstate` (psycopg3 não expõe `pgcode`), o 55P03 subia
    como `OperationalError` crua e matava a corrida na PRIMEIRA tabela ocupada —
    que em produção é o caso normal, não a exceção.
    """
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.db import connections

    alvos = {}
    with connection.cursor() as cur:
        cur.execute("""
            SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
             WHERE conrelid = 'tribunals_process'::regclass AND contype = 'f'
        """)
        alvos = dict(cur.fetchall())
    assert alvos, 'controle: o banco de teste tem FK em tribunals_process'

    bloqueador = connections.create_connection('default')
    try:
        with connection.cursor() as cur:
            for nome in alvos:
                cur.execute(f'ALTER TABLE tribunals_process DROP CONSTRAINT "{nome}"')

        bloqueador.set_autocommit(False)
        with bloqueador.cursor() as c:
            # ROW EXCLUSIVE é o que um UPDATE segura — e conflita com o
            # ACCESS EXCLUSIVE do ALTER. É a corrida real do #105.
            c.execute('LOCK TABLE tribunals_process IN ROW EXCLUSIVE MODE')

        err = StringIO()
        with pytest.raises(CommandError):
            call_command('auditar_schema', reparar_fks=True, tentativas=2,
                         espera=0, lock_timeout='200ms',
                         stdout=StringIO(), stderr=err)
        assert 'LOCK NÃO VEIO' in err.getvalue(), (
            'o lock_timeout não foi tratado como transitório — subiu cru e '
            f'matou a corrida. stderr: {err.getvalue()!r}'
        )
    finally:
        bloqueador.rollback()
        bloqueador.close()
        with connection.cursor() as cur:
            for nome, definicao in alvos.items():
                cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s "
                            "AND conrelid = 'tribunals_process'::regclass", [nome])
                if not cur.fetchone():
                    cur.execute(f'ALTER TABLE tribunals_process '
                                f'ADD CONSTRAINT "{nome}" {definicao}')


@pytest.mark.django_db(transaction=False)
def test_auditar_schema_recusa_tabela_acima_do_teto():
    """Teto atingido é ERRO com o número real, nunca um `return` discreto."""
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    with connection.cursor() as cur:
        for nome in _fks_no_banco('tribunals_process'):
            cur.execute(f'ALTER TABLE tribunals_process DROP CONSTRAINT "{nome}"')

    err = StringIO()
    with pytest.raises(CommandError):
        call_command('auditar_schema', reparar_fks=True, max_bytes=0,
                     tentativas=1, espera=0, stdout=StringIO(), stderr=err)
    assert 'FORA DO TETO' in err.getvalue()
    assert _fks_no_banco('tribunals_process') == set(), 'criou apesar do teto'


# --------------------------------------------------------------------------- #
# 6 — as duas declarações que a 0056 acertou
# --------------------------------------------------------------------------- #

def test_movimentacao_nao_declara_indice_em_classe():
    """1,89 TB / 1,55 bi de linhas: índice que ninguém consulta é custo puro.

    A única consulta do código que toca `classe_id` (o `repop_classe_assunto
    --tabela movimentacao`) entra por faixa de pk — `Index Cond` na pkey, custo
    5.334 — e NÃO usaria este índice. Ver a `0056` e `.ia/DATA_MODEL.md`.
    """
    declarados = [tuple(i.fields) for i in Movimentacao._meta.indexes]
    assert ('classe',) not in declarados, (
        'voltou a declarar índice em Movimentacao.classe: são ~35-45 GB '
        'mantidos no caminho de escrita da ingestão, sem uma consulta que o use'
    )
    # o índice IMPLÍCITO da FK conta igual — sem isto o estado continua mentindo
    assert Movimentacao._meta.get_field('classe').db_index is False, (
        'a FK voltou a `db_index=True`: o Django declara um btree em '
        '`classe_id` sobre 1,55 bilhão de linhas'
    )
    # controle positivo: os índices que EXISTEM continuam declarados, e a FK
    # que DEVE ser indexada continua sendo
    assert ('tribunal', '-data_disponibilizacao') in declarados
    assert Movimentacao._meta.get_field('processo').db_index is True


def test_proc_tribunal_id_idx_declara_o_que_o_banco_tem():
    """Mesmo nome, colunas erradas — a declaração passou a dizer a verdade."""
    ix = next(i for i in Process._meta.indexes if i.name == 'proc_tribunal_id_idx')
    assert list(ix.fields) == ['tribunal'], (
        'a declaração voltou a prometer (tribunal, -id); no banco é '
        'btree(tribunal_id), UMA coluna. Índice declarado e ausente já custou '
        '1.318 s numa query e 63 sessões enfileiradas atrás de um ALTER'
    )
    # controle: o irmão que ESTÁ correto no banco continua com as duas colunas
    outro = next(i for i in Process._meta.indexes if i.name == 'proc_enriq_id_idx')
    assert list(outro.fields) == ['enriquecimento_status', '-id']
