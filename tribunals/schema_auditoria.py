"""Compara o que as migrations DECLARAM com o que o banco TEM — por COLUNA.

Por que existe
--------------
`makemigrations` compara o model com o **ESTADO** (a soma das migrations),
nunca com o banco. Um objeto criado à mão, perdido num `pg_restore`, ou que
falhou no meio de um `CREATE INDEX CONCURRENTLY` fica invisível para sempre:
o estado continua afirmando que ele existe, e o código passa a ser escrito em
cima dessa afirmação.

Já custou caro três vezes nesta casa:

  · `proc_tribunal_id_idx` — o model declara ``(tribunal, -id)``; o banco tem
    ``btree(tribunal_id)``, **uma coluna**. Mesmo nome, colunas diferentes:
    `\\di` responde "existe" e `makemigrations` não vê. Um `ORDER BY id LIMIT 1`
    ficou 1.318 s e enfileirou 63 sessões atrás de um ALTER (ver `OPS.md`).
  · migration `0051` — três índices de `tribunals_movimentacao` declarados e
    ausentes viraram premissa em `api/filters.py` (Seq Scan de custo
    111.195.298 no caminho da requisição) e em `diarios/base.py` (73.427.276
    por lote, no caminho de ESCRITA).
  · trigger `process_set_ano_cnj` — sumiu num restore; 65 M de linhas nasceram
    com `ano_cnj` NULL antes de a `0042` recriá-lo.

A régua, e por que ela é por COLUNA
-----------------------------------
Conferir por **nome** é o que engana: o nome pode existir apontando para outras
colunas (`proc_tribunal_id_idx`) ou o objeto pode existir com outro nome
(`proc_classe_id_idx` no lugar do `tribunals_p_classe__05f562_idx` que o Django
geraria) — assinatura de DDL feito à mão com a migration marcada depois.

Então tudo aqui é comparado por **definição**: tabela, coluna+tipo, lista
ordenada de colunas do índice (+ predicado parcial), colunas da constraint.

Campo de CONTROLE (obrigatório)
-------------------------------
`controle_pk` conta as PKs declaradas que foram encontradas no banco. Ele
**tem que dar 100%**: PK é o objeto que certamente existe em toda tabela viva.
Se ele não fecha, a régua está torta e a medição inteira é lixo — quem chama
`inventariar()` deve recusar o resultado (é o que o command faz).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db.migrations.loader import MigrationLoader

#: apps do projeto — models de terceiros (auth, contenttypes, sessions) ficam
#: de fora porque não são nossos para consertar.
APPS_DO_PROJETO = {
    'tribunals', 'accounts', 'djen', 'datajud', 'diarios', 'diarios_entes',
    'dashboard', 'api', 'core', 'search', 'enrichers', 'pdf_storage',
    'monitoring', 'mcp_server',
}


def _normalizar_tipo(t: str) -> str:
    """Reduz os apelidos do Postgres à mesma grafia (só a base, sem tamanho)."""
    t = (t or '').lower().strip()
    for de, para in (
        ('character varying', 'varchar'), ('timestamp with time zone', 'timestamptz'),
        ('timestamp without time zone', 'timestamp'), ('double precision', 'float8'),
        ('integer', 'int4'), ('bigint', 'int8'), ('boolean', 'bool'),
        ('bigserial', 'int8'), ('serial', 'int4'), ('character', 'char'),
    ):
        t = t.replace(de, para)
    return t.split('(')[0].strip()


@dataclass
class Achado:
    """Uma divergência entre o declarado e o banco."""
    tipo: str          # 'tabela'|'coluna'|'indice'|'fk'|'unique'|'check'|
                       # 'tipo_divergente'|'nome_colidido'|'nome_diferente'|'invalido'
    tabela: str
    objeto: str        # nome declarado (ou None quando o Django não nomeia)
    detalhe: str
    gravidade: str = 'ausente'   # 'ausente' | 'aviso'

    def como_linha(self) -> str:
        return f'{self.gravidade:8s} {self.tipo:16s} {self.tabela}.{self.objeto} — {self.detalhe}'


@dataclass
class Inventario:
    achados: list[Achado] = field(default_factory=list)
    controle_pk_esperadas: int = 0
    controle_pk_encontradas: int = 0
    modelos: int = 0
    tabelas_no_banco: int = 0

    @property
    def controle_ok(self) -> bool:
        return (self.controle_pk_esperadas > 0
                and self.controle_pk_encontradas == self.controle_pk_esperadas)

    def ausentes(self) -> list[Achado]:
        return [a for a in self.achados if a.gravidade == 'ausente']

    def por_tipo(self) -> dict[str, int]:
        d: dict[str, int] = {}
        for a in self.ausentes():
            d[a.tipo] = d.get(a.tipo, 0) + 1
        return d

    def como_dict(self) -> dict:
        return {
            'modelos': self.modelos,
            'tabelas_no_banco': self.tabelas_no_banco,
            'controle_pk': f'{self.controle_pk_encontradas}/{self.controle_pk_esperadas}',
            'controle_ok': self.controle_ok,
            'ausentes': len(self.ausentes()),
            'por_tipo': self.por_tipo(),
            'achados': [
                {'tipo': a.tipo, 'tabela': a.tabela, 'objeto': a.objeto,
                 'detalhe': a.detalhe, 'gravidade': a.gravidade}
                for a in self.achados
            ],
        }


# --------------------------------------------------------------------------- #
# o que as migrations DECLARAM
# --------------------------------------------------------------------------- #

def estado_declarado(connection) -> dict:
    """Estado final das migrations em disco — o que o Django ACHA que existe."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    state = loader.project_state(loader.graph.leaf_nodes())
    apps = state.apps
    modelos: dict[str, dict] = {}
    for model in apps.get_models(include_auto_created=True):
        meta = model._meta
        if meta.app_label not in APPS_DO_PROJETO or not meta.managed:
            continue
        colunas, fks, indices = {}, [], []
        for f in meta.local_fields:
            colunas[f.column] = _normalizar_tipo(f.db_type(connection))
            if f.remote_field and f.remote_field.model and f.db_constraint:
                alvo = f.remote_field.model._meta
                fks.append({'coluna': f.column, 'para_tabela': alvo.db_table,
                            'para_coluna': f.target_field.column})
            if f.db_index and not f.primary_key:
                indices.append({'nome': None, 'colunas': [f.column],
                                'condicao': None, 'expressao': False})
        for ix in meta.indexes:
            indices.append({
                'nome': ix.name,
                'colunas': [meta.get_field(fn.lstrip('-')).column for fn in (ix.fields or [])],
                'condicao': str(ix.condition) if ix.condition else None,
                'expressao': bool(getattr(ix, 'expressions', None)),
            })
        constraints = []
        for c in meta.constraints:
            tipo = type(c).__name__
            item = {'nome': c.name, 'tipo': tipo, 'colunas': [], 'expressao': False}
            if tipo == 'UniqueConstraint':
                item['colunas'] = [meta.get_field(fn).column for fn in (c.fields or [])]
                item['expressao'] = bool(getattr(c, 'expressions', None))
            constraints.append(item)
        for ut in (meta.unique_together or ()):
            constraints.append({'nome': None, 'tipo': 'unique_together',
                                'colunas': [meta.get_field(fn).column for fn in ut],
                                'expressao': False})
        modelos[meta.db_table] = {
            'app': meta.app_label, 'model': meta.object_name,
            'pk': meta.pk.column if meta.pk else None,
            'colunas': colunas, 'fks': fks, 'indices': indices,
            'constraints': constraints,
        }
    return modelos


# --------------------------------------------------------------------------- #
# o que o banco TEM
# --------------------------------------------------------------------------- #

_SQL_COLUNAS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
"""

#: colunas do índice pela POSIÇÃO em `indkey` — é isto que pega o
#: `proc_tribunal_id_idx` (nome certo, uma coluna só em vez de duas).
_SQL_INDICES = """
SELECT t.relname, ic.relname, i.indisunique, i.indisprimary, i.indisvalid,
       pg_get_expr(i.indpred, i.indrelid),
       (SELECT array_agg(COALESCE(a.attname, '(expr)') ORDER BY k.ord)
          FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
          LEFT JOIN pg_attribute a
                 ON a.attrelid = i.indrelid AND a.attnum = k.attnum)
  FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = 'public'
"""

_SQL_CONSTRAINTS = """
SELECT t.relname, co.conname, co.contype, co.convalidated,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(co.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a
            ON a.attrelid = co.conrelid AND a.attnum = k.attnum)
  FROM pg_constraint co JOIN pg_class t ON t.oid = co.conrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = 'public'
"""


def estado_do_banco(connection) -> dict:
    """Catálogo real: colunas, índices (com colunas e predicado) e constraints."""
    colunas: dict[str, dict[str, str]] = {}
    indices: dict[str, list[dict]] = {}
    constraints: dict[str, list[dict]] = {}
    with connection.cursor() as cur:
        cur.execute(_SQL_COLUNAS)
        for tabela, coluna, tipo in cur.fetchall():
            colunas.setdefault(tabela, {})[coluna] = _normalizar_tipo(tipo)
        cur.execute(_SQL_INDICES)
        for tabela, nome, uniq, pk, valido, cond, cols in cur.fetchall():
            indices.setdefault(tabela, []).append(
                {'nome': nome, 'unico': uniq, 'pk': pk, 'valido': valido,
                 'condicao': cond, 'colunas': list(cols or [])})
        cur.execute(_SQL_CONSTRAINTS)
        for tabela, nome, tipo, validada, cols in cur.fetchall():
            constraints.setdefault(tabela, []).append(
                {'nome': nome, 'tipo': tipo, 'validada': validada,
                 'colunas': list(cols or [])})
    return {'colunas': colunas, 'indices': indices, 'constraints': constraints}


# --------------------------------------------------------------------------- #
# a comparação
# --------------------------------------------------------------------------- #

def inventariar(connection) -> Inventario:
    """Compara declarado × banco por COLUNA e devolve o inventário completo."""
    decl = estado_declarado(connection)
    real = estado_do_banco(connection)
    inv = Inventario(modelos=len(decl), tabelas_no_banco=len(real['colunas']))

    for tabela, m in sorted(decl.items()):
        cols_banco = real['colunas'].get(tabela)
        if cols_banco is None:
            inv.achados.append(Achado('tabela', tabela, tabela,
                                      f"declarada por {m['app']}.{m['model']}"))
            continue
        idx_banco = real['indices'].get(tabela, [])
        con_banco = real['constraints'].get(tabela, [])

        # --- colunas -------------------------------------------------------
        for coluna, tipo in m['colunas'].items():
            if coluna not in cols_banco:
                inv.achados.append(Achado('coluna', tabela, coluna, f'tipo {tipo}'))
            elif tipo and cols_banco[coluna] != tipo:
                inv.achados.append(Achado(
                    'tipo_divergente', tabela, coluna,
                    f'declarado {tipo}, no banco {cols_banco[coluna]}'))

        # --- CONTROLE: a PK tem que estar lá -------------------------------
        if m['pk']:
            inv.controle_pk_esperadas += 1
            if any(c['tipo'] == 'p' and c['colunas'] == [m['pk']] for c in con_banco):
                inv.controle_pk_encontradas += 1

        # --- índices (por lista ORDENADA de colunas + predicado) -----------
        vistos = set()
        for ix in m['indices']:
            if ix['expressao'] or not ix['colunas']:
                continue
            if any(c not in cols_banco for c in ix['colunas']):
                continue  # a coluna nem existe: já contado acima
            chave = (tuple(ix['colunas']), ix['condicao'])
            if chave in vistos:
                continue
            vistos.add(chave)
            n = len(ix['colunas'])
            servem = [i for i in idx_banco
                      if i['colunas'][:n] == ix['colunas'] and i['valido']]
            servem += [c for c in con_banco
                       if c['tipo'] in ('u', 'p') and c['colunas'][:n] == ix['colunas']]
            nome = ix['nome'] or '+'.join(ix['colunas'])
            if not servem:
                homonimo = next((i for i in idx_banco if i['nome'] == ix['nome']), None)
                if homonimo is not None:
                    # o caso `proc_tribunal_id_idx`: existe, com o nome certo
                    # e as colunas erradas — o pior dos três, porque a
                    # conferência por nome responde "ok".
                    inv.achados.append(Achado(
                        'nome_colidido', tabela, nome,
                        f"declarado {ix['colunas']}, no banco {homonimo['colunas']}"))
                else:
                    inv.achados.append(Achado(
                        'indice', tabela, nome,
                        f"colunas {ix['colunas']}"
                        + (f" WHERE {ix['condicao']}" if ix['condicao'] else '')))
            elif ix['nome'] and not any(s['nome'] == ix['nome'] for s in servem):
                inv.achados.append(Achado(
                    'nome_diferente', tabela, nome,
                    f"existe como {[s['nome'] for s in servem][:2]}", gravidade='aviso'))

        # --- FKs (por COLUNA, nunca por nome) ------------------------------
        for fk in m['fks']:
            if not any(c['tipo'] == 'f' and c['colunas'] == [fk['coluna']]
                       for c in con_banco):
                inv.achados.append(Achado(
                    'fk', tabela, fk['coluna'],
                    f"-> {fk['para_tabela']}({fk['para_coluna']})"))

        # --- unique / check ------------------------------------------------
        for c in m['constraints']:
            if c['tipo'] in ('UniqueConstraint', 'unique_together'):
                if c['expressao'] or not c['colunas']:
                    continue
                if any(x not in cols_banco for x in c['colunas']):
                    continue
                achou = any(k['tipo'] == 'u' and k['colunas'] == c['colunas']
                            for k in con_banco)
                achou = achou or any(i['unico'] and i['colunas'] == c['colunas']
                                     and i['valido'] for i in idx_banco)
                if not achou:
                    inv.achados.append(Achado(
                        'unique', tabela, c['nome'] or '+'.join(c['colunas']),
                        f"colunas {c['colunas']}"))
            elif c['tipo'] == 'CheckConstraint':
                if not any(k['tipo'] == 'c' and k['nome'] == c['nome']
                           for k in con_banco):
                    inv.achados.append(Achado('check', tabela, c['nome'], 'ausente'))

        # --- índices INVÁLIDOS (CREATE INDEX CONCURRENTLY que morreu) ------
        for i in idx_banco:
            if not i['valido']:
                inv.achados.append(Achado(
                    'invalido', tabela, i['nome'],
                    f"colunas {i['colunas']} — o planner NÃO usa, mas a escrita "
                    f'mantém; sobra de CREATE INDEX CONCURRENTLY', gravidade='aviso'))
        # --- FKs NOT VALID (criadas mas ainda não varridas) ----------------
        for c in con_banco:
            if c['tipo'] == 'f' and not c['validada']:
                inv.achados.append(Achado(
                    'nao_validada', tabela, c['nome'],
                    f"colunas {c['colunas']} — falta VALIDATE CONSTRAINT",
                    gravidade='aviso'))

    return inv
