"""Catraca: coluna NOT NULL nova em tabela quente NUNCA fica sem `DEFAULT`.

POR QUE ESTE TESTE EXISTE (e por que ele é uma CATRACA, não um ideal)
---------------------------------------------------------------------
A regra já estava escrita em `.ia/OPS.md` desde **25/08/2026**, com o preço
pago na frente dela: a migration 0052 acrescentou `Process.grau` com o idioma
padrão do Django (`ADD COLUMN ... DEFAULT '' NOT NULL` + `DROP DEFAULT`), os
workers seguiram com o código anterior — o bind mount `.:/app` entrega o
arquivo, mas o Python não reimporta o módulo — e o dia 25/08 fechou com
**10.410 `IngestionRun` `failed`** e **32 publicações** contra 1,5 M do dia
anterior. A cura foi um `ALTER COLUMN grau SET DEFAULT ''` de 30 segundos.

Seis dias depois, a migration 0054 repetiu **o mesmo idioma** em quatro
colunas (`classe_cnj_codigo`, `classe_cnj_nome`, `fase_codigo`, `fase_nome`) e
fechou a terceira porta: 255 de 377 `EdicaoDiario` do `tjsp-dje` em `falha`,
215 delas com `null value in column "classe_cnj_codigo"`, mais 1.043
`IngestionRun` da fonte só em 01/09/2026.

A regra não falhou por ser desconhecida — falhou por não ser **cobrada**.
É isso que este arquivo faz.

O QUE ELE NÃO É
---------------
Não é "toda coluna NOT NULL precisa de DEFAULT". Não precisa, e para algumas
seria pior: `numero_cnj`, `tribunal_id`, `processo_id`, `parte_id` são
identidade e chave — um INSERT que os omita TEM que explodir, e um `DEFAULT`
ali trocaria um erro alto por uma linha órfã silenciosa. Por isso o teste é
uma catraca sobre o estado de hoje: ele congela a lista das colunas NOT NULL
sem `DEFAULT` que as quatro tabelas mais quentes têm, e reprova quem
**acrescentar** uma. Quem acrescentar tem duas saídas legítimas:

  1. pôr `DEFAULT` no banco (o caso comum: texto, contador, flag) — e a lista
     encolhe sozinha; ou
  2. se a coluna é identidade/chave e o INSERT sem ela DEVE falhar, incluí-la
     aqui **com o motivo escrito**, o que torna a escolha consciente.

A régua é o BANCO DE TESTE, que o pytest constrói rodando as migrations —
então o que este teste mede é o que as migrations declaram, que é justamente
onde a 0054 errou. Divergência entre as migrations e a `.101` é outro
problema, e tem outra ferramenta: `manage.py auditar_schema`.
"""
import pytest
from django.db import connection

#: Só as tabelas com escritor em produção fora do ciclo de deploy — as que
#: têm worker antigo escrevendo enquanto a migration já rodou. Tabela fria
#: não entra: o custo do falso positivo não se paga.
TABELAS_QUENTES = (
    'tribunals_process',
    'tribunals_movimentacao',
    'tribunals_processoparte',
    'tribunals_parte',
)

#: Congelado em 02/09/2026, conferido por COLUNA (`pg_attrdef`), nunca por
#: nome de migration. Toda linha aqui é uma coluna que HOJE aceita apenas
#: INSERT que a nomeie. A maioria é identidade, chave, carimbo de tempo
#: (`auto_now_add`, sempre preenchido pelo ORM) ou campo que nasceu junto com
#: a tabela — nenhum escritor antigo pode existir para uma coluna que sempre
#: esteve lá. O perigo é sempre a coluna NOVA, e é ela que a catraca pega.
SEM_DEFAULT_CONHECIDAS = {
    ('tribunals_movimentacao', 'ativo'),
    ('tribunals_movimentacao', 'assunto_norm'),
    ('tribunals_movimentacao', 'codigo_classe'),
    ('tribunals_movimentacao', 'data_disponibilizacao'),
    ('tribunals_movimentacao', 'destinatario_advogados'),
    ('tribunals_movimentacao', 'destinatarios'),
    ('tribunals_movimentacao', 'external_id'),
    ('tribunals_movimentacao', 'hash'),
    ('tribunals_movimentacao', 'inserido_em'),
    ('tribunals_movimentacao', 'link'),
    ('tribunals_movimentacao', 'meio'),
    ('tribunals_movimentacao', 'meio_completo'),
    ('tribunals_movimentacao', 'motivo_cancelamento'),
    ('tribunals_movimentacao', 'nome_classe'),
    ('tribunals_movimentacao', 'nome_orgao'),
    ('tribunals_movimentacao', 'numero_comunicacao'),
    ('tribunals_movimentacao', 'processo_id'),
    ('tribunals_movimentacao', 'status'),
    ('tribunals_movimentacao', 'texto'),
    ('tribunals_movimentacao', 'tipo_comunicacao'),
    ('tribunals_movimentacao', 'tipo_documento'),
    ('tribunals_movimentacao', 'tribunal_id'),
    ('tribunals_parte', 'documento'),
    ('tribunals_parte', 'nome'),
    ('tribunals_parte', 'oab'),
    ('tribunals_parte', 'primeira_aparicao_em'),
    ('tribunals_parte', 'tipo'),
    ('tribunals_parte', 'tipo_documento'),
    ('tribunals_parte', 'total_processos'),
    ('tribunals_parte', 'ultima_aparicao_em'),
    ('tribunals_process', 'assunto_codigo'),
    ('tribunals_process', 'assunto_nome'),
    ('tribunals_process', 'atualizado_em'),
    ('tribunals_process', 'classe_codigo'),
    ('tribunals_process', 'classe_nome'),
    ('tribunals_process', 'enriquecimento_erro'),
    ('tribunals_process', 'enriquecimento_status'),
    ('tribunals_process', 'enriquecimento_tentativas'),
    ('tribunals_process', 'inserido_em'),
    ('tribunals_process', 'juizo'),
    ('tribunals_process', 'numero_cnj'),
    ('tribunals_process', 'orgao_julgador_codigo'),
    ('tribunals_process', 'orgao_julgador_nome'),
    ('tribunals_process', 'total_movimentacoes'),
    ('tribunals_process', 'tribunal_id'),
    ('tribunals_processoparte', 'inserido_em'),
    ('tribunals_processoparte', 'papel'),
    ('tribunals_processoparte', 'parte_id'),
    ('tribunals_processoparte', 'polo'),
    ('tribunals_processoparte', 'processo_id'),
}

#: As cinco que a 0057 devolveu para o banco. Ficam explícitas porque são o
#: caso que gerou o incidente: se alguma voltar a perder o `DEFAULT`, o
#: escritor atrasado volta a quebrar e a mensagem tem que dizer isso.
EXIGEM_DEFAULT = (
    ('tribunals_process', 'classe_cnj_codigo'),
    ('tribunals_process', 'classe_cnj_nome'),
    ('tribunals_process', 'fase_codigo'),
    ('tribunals_process', 'fase_nome'),
    ('tribunals_process', 'grau'),
)

SQL = """
SELECT cl.relname, a.attname, pg_get_expr(ad.adbin, ad.adrelid)
FROM pg_class cl
JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef ad ON ad.adrelid = cl.oid AND ad.adnum = a.attnum
WHERE cl.relkind = 'r' AND cl.relname = ANY(%s) AND a.attnotnull
  AND a.attname <> 'id'
ORDER BY 1, 2
"""


def _colunas():
    """(tabela, coluna) -> expressão do DEFAULT no banco (None se não tem)."""
    with connection.cursor() as c:
        c.execute(SQL, [list(TABELAS_QUENTES)])
        return {(t, col): default for t, col, default in c.fetchall()}


@pytest.mark.django_db
def test_controle_positivo_a_sonda_enxerga_as_tabelas():
    """Sonda que devolve vazio precisa provar que sabe devolver não-vazio.

    Sem isto, um `relname` errado (renome de tabela, `db_table` mudado) faria
    o teste passar por não achar NADA — o modo de falha mais caro que existe,
    porque ele ENCERRA a investigação em vez de abri-la.
    """
    achadas = {t for t, _ in _colunas()}
    assert achadas == set(TABELAS_QUENTES), (
        f'a sonda não enxergou {set(TABELAS_QUENTES) - achadas} — o teste '
        f'abaixo estaria medindo o vazio e passando por isso'
    )


@pytest.mark.django_db
def test_as_cinco_colunas_do_incidente_tem_default_no_banco():
    """A 0057, medida no banco: `''` em cada uma das cinco.

    Não basta o model declarar `blank=True` — o `DEFAULT` do model é do
    Django, mora no processo Python e some junto com ele. O que protege o
    escritor atrasado é o `DEFAULT` do POSTGRES.
    """
    colunas = _colunas()
    for chave in EXIGEM_DEFAULT:
        assert chave in colunas, f'{chave} sumiu da tabela — migration nova?'
        default = colunas[chave]
        assert default is not None, (
            f'{chave[0]}.{chave[1]} está NOT NULL SEM DEFAULT. Todo INSERT '
            f'que não nomeie a coluna passa a falhar — e worker com código '
            f'anterior à migration não nomeia. Foi exatamente isso que '
            f'derrubou o dia 25/08/2026 (10.410 runs failed) e fechou a '
            f'terceira porta em 31/08 (255 de 377 edições em falha). '
            f'Ver tribunals/migrations/0057 e .ia/OPS.md.'
        )
        assert default.startswith("''"), (
            f'{chave[0]}.{chave[1]} tem DEFAULT {default!r}; esperado a '
            f'string vazia, que é o mesmo valor que o model já manda'
        )


@pytest.mark.django_db
def test_catraca_nenhuma_coluna_nova_sem_default_nas_tabelas_quentes():
    """A lista de NOT NULL-sem-DEFAULT só pode ENCOLHER.

    Se este teste reprovou por causa de uma migration sua: a coluna nova
    precisa de `ALTER COLUMN ... SET DEFAULT <valor>` no banco — no MESMO
    migration, nunca "depois do deploy", porque entre o `migrate` e o restart
    da frota existe uma janela de dias em que os escritores antigos ainda
    estão de pé. Se a coluna é identidade/chave e o INSERT sem ela DEVE
    explodir, acrescente-a a `SEM_DEFAULT_CONHECIDAS` com o motivo escrito.
    """
    atual = {k for k, default in _colunas().items() if default is None}
    novas = atual - SEM_DEFAULT_CONHECIDAS
    assert not novas, (
        f'coluna(s) NOT NULL SEM DEFAULT em tabela quente: {sorted(novas)}. '
        f'Leia a docstring deste teste antes de mexer na lista.'
    )
    # O outro lado: quem ganhou DEFAULT tem que sair da lista, senão ela
    # apodrece e para de significar alguma coisa.
    sumidas = SEM_DEFAULT_CONHECIDAS - atual
    assert not sumidas, (
        f'{sorted(sumidas)} já não é NOT NULL-sem-DEFAULT (ganhou default, '
        f'virou nullable ou sumiu). Tire da lista SEM_DEFAULT_CONHECIDAS — '
        f'lista desatualizada é catraca que não trava mais nada.'
    )
