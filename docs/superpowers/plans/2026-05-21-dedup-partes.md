# Deduplicação de `Parte` + Recriação de Índices Únicos — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Colapsar as ~80M linhas duplicadas de `tribunals_parte` (real: ~3-4M entidades) de volta às entidades reais, repontando as 30,7M FKs de `ProcessoParte`, e recriar os 3 índices únicos parciais **válidos** — de forma que não unifique homônimos errado.

**Architecture:** Um management command `dedup_partes` faz a deduplicação **set-based em SQL** (Python loop em 80M linhas é inviável), em lotes resumíveis, com `--dry-run`. A unificação é feita **exclusivamente por igualdade exata** da chave de cada constraint (oab; documento real; `(nome, documento)` mascarado) — nunca por similaridade. Depois, uma migration recria os 3 índices `CONCURRENTLY` **dropando o husk inválido antes** e **verificando `indisvalid` no fim**.

**Tech Stack:** Django 5, PostgreSQL 16, management commands, SQL set-based (CTE + window functions), `psql`.

---

## Causa raiz (confirmada — Phase 1-3 do debugging fechadas)

`pg_index` em prod: `uniq_parte_oab`, `uniq_parte_documento_real`,
`uniq_parte_documento_mascarado` estão com **`indisvalid=false, indisready=false`**
— cascas mortas. Só `uniq_parte_sem_doc_nem_oab` é válido.

**Mecanismo:** migration `0017_restore_missing_indexes.py` roda
`CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS`. Na 1ª execução a tabela já
tinha duplicatas (populada de dump sem índices — vide docstring da própria
0017) → o build concurrent **falhou na validação** → deixou índice inválido.
Re-execuções da migration veem o husk via `IF NOT EXISTS` e **pulam** → a
migration fica marcada como aplicada, os índices nunca enforçam.

Índice inválido não enforça unicidade, e `bulk_create(ignore_conflicts=True)`
do drainer (`ON CONFLICT DO NOTHING`) não tem constraint válida pra conflitar
→ **todo insert passa**. O drainer re-insere as partes de cada processo a cada
re-enriquecimento → acumulação infinita.

**Evidência:** `oab='RS65244A'` aparece 48.376× (impossível sob unique
válido). 44M advogados / 540k OABs distintas. 76,4M linhas com doc mascarado.

## Princípio de correção — anti-homônimo (NÃO-NEGOCIÁVEL)

A deduplicação **só** colapsa linhas cuja chave de constraint é **byte-idêntica**:

| Grupo | Chave de colapso | Risco de homônimo |
|---|---|---|
| `oab` | `oab` exato (`oab <> ''`) | **Zero** — OAB (UF+número) é identificador nacional único; OABs iguais = mesmo advogado |
| `doc_real` | `documento` exato (CPF/CNPJ sem máscara) | **Zero** — CPF/CNPJ é identificador nacional único |
| `doc_masc` | `(nome, documento)` exato (doc mascarado) | Limitado ao que a constraint `uniq_parte_documento_mascarado` **já** definia como identidade. Colapsar `(nome, doc)` byte-idêntico **não cria nenhuma fusão nova** além da que o sistema já pretendia — só remove cópias literais da mesma chave |

**Proibido neste plano:**
- Fusão por nome só, por similaridade de nome, fuzzy match, trigram, soundex.
- Fusão masked→real (absorver Parte mascarada numa Parte de doc real). Isso
  envolve `real_casa_com_mascara` (nome + dígitos visíveis correspondentes) —
  uma chave mais fraca, com risco de homônimo real. **Fica fora deste plano.**
  Já existe o command `consolidar_partes_mascaradas` pra isso; rodá-lo é uma
  decisão separada e revisável, depois.
- Survivor de cada grupo = **`MIN(id)`** (linha mais antiga). Determinístico.

## File structure

| Caminho | Responsabilidade | Ação |
|---|---|---|
| `tribunals/management/commands/dedup_partes.py` | Command set-based de dedup por grupo, em lotes, `--dry-run` | **Criar** |
| `tribunals/management/commands/check_parte_indexes.py` | Verifica `indisvalid` dos 4 índices únicos; exit 1 se algum inválido | **Criar** |
| `tribunals/migrations/00NN_recriar_indices_unicos_parte.py` | Drop dos 3 husks inválidos + `CREATE ... CONCURRENTLY` + verificação `indisvalid` | **Criar** |
| `tests/test_dedup_partes.py` | Testes do command em fixture pequena | **Criar** |
| `.ia/ENRICHMENT.md` | Documenta a dedup + a armadilha do `CONCURRENTLY IF NOT EXISTS` | **Modificar** |
| `.ia/OPS.md` | Runbook: como rodar a dedup, `check_parte_indexes` | **Modificar** |

Sem mudança nos models `Parte`/`ProcessoParte` — as `UniqueConstraint` já estão
declaradas corretas; o problema é só o estado físico do DB.

## Pré-requisitos

- SSH `ubuntu@100.100.144.57` (web/.32, Tailscale) e `ubuntu@100.115.193.26` (workers/.36).
- Janela de manutenção: os drainers de enrichment ficam **parados** durante a dedup.
- Branch nova `fix/dedup-partes`.

---

## Task 1: Pré-voo — investigação read-only em prod

**Files:** nenhum (só leitura).

- [ ] **Step 1.1: Confirmar validade dos índices das DUAS tabelas**

Run:
```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python -c \"
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from django.db import connection
c=connection.cursor()
c.execute('''SELECT t.relname, i.relname, idx.indisvalid, idx.indisready
  FROM pg_index idx JOIN pg_class i ON i.oid=idx.indexrelid JOIN pg_class t ON t.oid=idx.indrelid
  WHERE t.relname IN ('tribunals_parte','tribunals_processoparte') AND NOT idx.indisvalid ORDER BY 1,2''')
for r in c.fetchall(): print(r)
print('--- fim (linhas acima = índices inválidos) ---')
\""
```

Expected: lista os 3 husks de `tribunals_parte`. **Anotar se `uniq_processo_parte_polo_papel_principal` (de `tribunals_processoparte`) também aparece** — isso decide se o repoint precisa de proteção extra contra colisão (Task 3 já cobre os dois casos, mas registre o achado).

- [ ] **Step 1.2: Snapshot de contagens (baseline pra validar o resultado depois)**

Run:
```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python -c \"
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from django.db import connection
c=connection.cursor()
for label,sql in [
 ('parte_total','SELECT count(*) FROM tribunals_parte'),
 ('processoparte_total','SELECT count(*) FROM tribunals_processoparte'),
 ('distinct_oab','SELECT count(DISTINCT oab) FROM tribunals_parte WHERE oab<>chr(39)||chr(39)'),
]:
    c.execute(sql); print(label, c.fetchone()[0])
\""
```

Anotar os 3 números neste arquivo abaixo. São o baseline:
```
baseline parte_total: ___
baseline processoparte_total: ___
baseline distinct_oab: ___
```

- [ ] **Step 1.3: Confirmar que `consolidar_partes_mascaradas` e `consolidar_partes_sem_doc_em_real` NÃO serão tocados**

Run: `ls /home/will/projetos/voyager/tribunals/management/commands/ | grep consolidar`
Expected: os 2 arquivos existem. Este plano **não os modifica nem os roda** — registro só pra deixar claro o escopo.

---

## Task 2: Command `dedup_partes` — esqueleto + survivor map por grupo

**Files:**
- Create: `tribunals/management/commands/dedup_partes.py`
- Test: `tests/test_dedup_partes.py`

- [ ] **Step 2.1: Escrever o teste do roteamento de grupo (TDD)**

```python
# tests/test_dedup_partes.py
import pytest
from django.core.management import call_command
from tribunals.models import Tribunal, Process, Parte, ProcessoParte

pytestmark = pytest.mark.django_db


def _proc(sigla='TRF1', n='1'):
    t, _ = Tribunal.objects.get_or_create(sigla=sigla, defaults={'sigla_djen': sigla, 'nome': sigla})
    return Process.objects.create(tribunal=t, numero_cnj=f'{n:0>7}-00.2024.4.01.0000')


def test_dedup_oab_colapsa_para_min_id():
    """3 Partes com o mesmo OAB → sobra 1 (a de menor id)."""
    p1 = Parte.objects.create(nome='ADV UM', oab='SP111', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV UM VARIANTE', oab='SP111', tipo='advogado')
    p3 = Parte.objects.create(nome='ADV UM', oab='SP111', tipo='advogado')
    call_command('dedup_partes', '--group', 'oab')
    restantes = list(Parte.objects.filter(oab='SP111'))
    assert len(restantes) == 1
    assert restantes[0].id == p1.id  # survivor = MIN(id)


def test_dedup_nao_funde_oabs_diferentes():
    """OABs diferentes (homônimo de nome) NÃO colapsam."""
    Parte.objects.create(nome='JOSE DA SILVA', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='JOSE DA SILVA', oab='SP222', tipo='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert Parte.objects.filter(nome='JOSE DA SILVA').count() == 2
```

- [ ] **Step 2.2: Rodar o teste — deve falhar (command não existe)**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -v`
Expected: FAIL — `Unknown command: 'dedup_partes'`.

- [ ] **Step 2.3: Escrever o command com o grupo `oab`**

```python
# tribunals/management/commands/dedup_partes.py
"""Deduplica tribunals_parte colapsando linhas com chave de constraint
idêntica. Causado por índices únicos parciais que ficaram INVÁLIDOS
(CREATE UNIQUE INDEX CONCURRENTLY que falhou — ver migration 0017).

Set-based em SQL: Python loop em ~80M linhas é inviável. Idempotente e
resumível — re-rodar após interrupção continua de onde parou (sempre
recalcula grupos com count > 1).

Anti-homônimo: colapsa SÓ por igualdade EXATA da chave. Survivor = MIN(id).
Nunca funde por nome/similaridade. Fusão masked→real fica fora (ver
consolidar_partes_mascaradas).
"""
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

logger = logging.getLogger('voyager.dedup_partes')

# Cada grupo: (nome, predicado WHERE da constraint, colunas-chave do PARTITION BY)
GRUPOS = {
    'oab': ("oab <> ''", 'oab'),
    'doc_real': (
        "documento <> '' AND documento NOT LIKE '%X%' "
        "AND documento NOT LIKE '%x%' AND documento NOT LIKE '%*%'",
        'documento',
    ),
    'doc_masc': (
        "(documento LIKE '%X%' OR documento LIKE '%x%' OR documento LIKE '%*%')",
        'nome, documento',
    ),
}


class Command(BaseCommand):
    help = 'Deduplica tribunals_parte por chave de constraint (anti-homônimo).'

    def add_arguments(self, parser):
        parser.add_argument('--group', choices=list(GRUPOS) + ['all'], default='all')
        parser.add_argument('--dry-run', action='store_true',
                            help='Só conta o que seria colapsado, não escreve.')
        parser.add_argument('--batch-size', type=int, default=200_000,
                            help='Partes-loser processadas por transação.')

    def handle(self, *args, **opts):
        grupos = list(GRUPOS) if opts['group'] == 'all' else [opts['group']]
        for g in grupos:
            self._dedup_grupo(g, dry_run=opts['dry_run'], batch=opts['batch_size'])

    def _dedup_grupo(self, grupo: str, *, dry_run: bool, batch: int):
        # Esqueleto. Implementação real do colapso vem na Task 3.
        raise CommandError(f'[{grupo}] colapso ainda não implementado — ver Task 3')
```

- [ ] **Step 2.4: Rodar o teste — `test_dedup_oab_colapsa` ainda falha (colapso na Task 3), mas o command É reconhecido**

Run: `docker compose exec web pytest tests/test_dedup_partes.py::test_dedup_oab_colapsa_para_min_id -v`
Expected: FAIL com `CommandError: [oab] colapso ainda não implementado — ver Task 3` (não mais "Unknown command"). Confirma que o esqueleto carrega.

- [ ] **Step 2.5: Commit**

```bash
git add tribunals/management/commands/dedup_partes.py tests/test_dedup_partes.py
git commit -m "feat(dedup): esqueleto do command dedup_partes (grupos + contagem)"
```

---

## Task 3: Command `dedup_partes` — colapso set-based collision-safe

**Files:**
- Modify: `tribunals/management/commands/dedup_partes.py`

O colapso, por grupo, em SQL. Usa tabela temporária de mapeamento
`loser_id → survivor_id`, repointa `ProcessoParte` de forma à prova de
colisão com a constraint `uniq_processo_parte_polo_papel_principal`, e só
então deleta as Partes-loser (FK é `on_delete=PROTECT` — deletar antes do
repoint dá erro).

- [ ] **Step 3.1: Escrever o teste de repoint + collision-safety**

```python
# adicionar em tests/test_dedup_partes.py
def test_dedup_repoint_processoparte():
    """ProcessoParte aponta pro survivor após colapso; loser some."""
    proc = _proc(n='100')
    p1 = Parte.objects.create(nome='ADV', oab='RS9', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV', oab='RS9', tipo='advogado')
    pp = ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    pp.refresh_from_db()
    assert pp.parte_id == p1.id            # repontado pro survivor
    assert not Parte.objects.filter(id=p2.id).exists()


def test_dedup_collisao_processoparte_nao_duplica():
    """Se o mesmo processo tem 2 ProcessoParte (uma por Parte duplicada)
    no mesmo polo/papel, o repoint colapsa pra UMA ProcessoParte."""
    proc = _proc(n='200')
    p1 = Parte.objects.create(nome='ADV', oab='RS8', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV', oab='RS8', tipo='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p1, polo='ativo', papel='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert ProcessoParte.objects.filter(processo=proc).count() == 1
    assert ProcessoParte.objects.get(processo=proc).parte_id == p1.id
```

- [ ] **Step 3.2: Rodar — deve falhar**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -k repoint -v`
Expected: FAIL com `CommandError: colapso ainda não implementado`.

- [ ] **Step 3.3: Implementar o colapso — substituir o corpo de `_dedup_grupo`**

Substituir o método `_dedup_grupo` inteiro (o esqueleto de 2 linhas que só
levanta `CommandError`) por:

```python
    def _dedup_grupo(self, grupo: str, *, dry_run: bool, batch: int):
        where, partition = GRUPOS[grupo]
        cols = [c.strip() for c in partition.split(',')]

        with connection.cursor() as cur:
            # Mapa loser→survivor numa TEMP TABLE. survivor = MIN(id) do grupo.
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute(f"""
                CREATE TEMP TABLE _dedup_map AS
                SELECT id AS loser_id,
                       min(id) OVER (PARTITION BY {partition}) AS survivor_id
                FROM tribunals_parte
                WHERE {where}
            """)
            cur.execute('DELETE FROM _dedup_map WHERE loser_id = survivor_id')
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
            cur.execute('SELECT count(*) FROM _dedup_map')
            total = cur.fetchone()[0]
            self.stdout.write(f'[{grupo}] losers a colapsar: {total:,}'
                              + ('  (DRY-RUN)' if dry_run else ''))
            if dry_run or total == 0:
                cur.execute('DROP TABLE IF EXISTS _dedup_map')
                return

            t0 = time.time()
            # Lotes por loser_id pra transações curtas e resumíveis.
            cur.execute('SELECT min(loser_id), max(loser_id) FROM _dedup_map')
            lo, hi = cur.fetchone()
            cursor_id = lo
            while cursor_id <= hi:
                fim = cursor_id + batch
                with transaction.atomic():
                    with connection.cursor() as c2:
                        # 1) Apaga ProcessoParte-loser que COLIDIRIA com uma
                        #    ProcessoParte-survivor já existente no mesmo
                        #    processo (mesmo polo/papel/representa).
                        c2.execute("""
                            DELETE FROM tribunals_processoparte ppl
                            USING _dedup_map m, tribunals_processoparte pps
                            WHERE ppl.parte_id = m.loser_id
                              AND m.loser_id >= %s AND m.loser_id < %s
                              AND pps.processo_id = ppl.processo_id
                              AND pps.parte_id = m.survivor_id
                              AND pps.polo = ppl.polo
                              AND pps.papel = ppl.papel
                              AND pps.representa_id IS NOT DISTINCT FROM ppl.representa_id
                        """, [cursor_id, fim])
                        # 2) Repointa o restante pro survivor.
                        c2.execute("""
                            UPDATE tribunals_processoparte ppl
                            SET parte_id = m.survivor_id
                            FROM _dedup_map m
                            WHERE ppl.parte_id = m.loser_id
                              AND m.loser_id >= %s AND m.loser_id < %s
                        """, [cursor_id, fim])
                        # 3) Deleta as Partes-loser do lote.
                        c2.execute("""
                            DELETE FROM tribunals_parte p
                            USING _dedup_map m
                            WHERE p.id = m.loser_id
                              AND m.loser_id >= %s AND m.loser_id < %s
                        """, [cursor_id, fim])
                self.stdout.write(
                    f'[{grupo}] lote {cursor_id:,}–{fim:,} ok '
                    f'({time.time() - t0:.0f}s)')
                cursor_id = fim
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
        self.stdout.write(self.style.SUCCESS(f'[{grupo}] concluído'))
```

- [ ] **Step 3.4: Rodar todos os testes do command**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -v`
Expected: PASS — os 4 testes (oab colapsa, oab não funde diferentes, repoint, colisão).

- [ ] **Step 3.5: Commit**

```bash
git add tribunals/management/commands/dedup_partes.py tests/test_dedup_partes.py
git commit -m "feat(dedup): colapso set-based collision-safe + repoint de ProcessoParte"
```

---

## Task 4: Teste do grupo `doc_masc` (anti-homônimo explícito)

**Files:**
- Modify: `tests/test_dedup_partes.py`

- [ ] **Step 4.1: Escrever os testes de doc mascarado**

```python
# adicionar em tests/test_dedup_partes.py
def test_doc_masc_colapsa_nome_e_doc_identicos():
    """Mesma (nome, doc mascarado) → colapsa."""
    p1 = Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    qs = Parte.objects.filter(nome='MARIA SOUZA', documento='639.XXX.XXX-XX')
    assert qs.count() == 1 and qs.first().id == p1.id


def test_doc_masc_nao_funde_nomes_diferentes_mesma_mascara():
    """Mesma máscara, nomes diferentes (homônimo NÃO) → NÃO colapsa."""
    Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    Parte.objects.create(nome='MARIA SANTOS', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    assert Parte.objects.filter(documento='639.XXX.XXX-XX').count() == 2


def test_doc_real_nao_some_no_grupo_masc():
    """doc real não é tocado pelo grupo doc_masc."""
    Parte.objects.create(nome='X', documento='111.222.333-44', tipo='pf')
    Parte.objects.create(nome='X', documento='111.222.333-44', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    assert Parte.objects.filter(documento='111.222.333-44').count() == 2  # intacto
```

- [ ] **Step 4.2: Rodar**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -v`
Expected: PASS — todos (o command já cobre `doc_masc` via `GRUPOS`).

- [ ] **Step 4.3: Commit**

```bash
git add tests/test_dedup_partes.py
git commit -m "test(dedup): cobre doc mascarado + garante não-fusão de homônimos"
```

---

## Task 5: Migration de recriação dos índices únicos

**Files:**
- Create: `tribunals/migrations/00NN_recriar_indices_unicos_parte.py` (N = próximo número; rodar `ls tribunals/migrations/ | tail -3` pra achar)

A migration **dropa o husk inválido** (`DROP INDEX` — `IF NOT EXISTS` foi o
que perpetuou o bug), recria `CONCURRENTLY`, e **verifica `indisvalid`** —
falhando alto se algum índice não validar.

- [ ] **Step 5.1: Criar a migration**

```python
# tribunals/migrations/00NN_recriar_indices_unicos_parte.py
"""Recria os 3 índices únicos parciais de tribunals_parte que ficaram
INVÁLIDOS (CREATE UNIQUE INDEX CONCURRENTLY que falhou na 0017 porque a
tabela tinha duplicatas; IF NOT EXISTS fez re-execuções pularem o husk).

Pré-requisito: rodar `dedup_partes` ANTES — senão o CREATE UNIQUE INDEX
falha de novo (duplicatas ainda presentes). Esta migration aborta com erro
claro se ainda houver duplicata.

Não-atômica: CONCURRENTLY não roda em transação.
"""
from django.db import migrations

INDICES = [
    ('uniq_parte_documento_real',
     "CREATE UNIQUE INDEX CONCURRENTLY uniq_parte_documento_real "
     "ON tribunals_parte (documento) "
     "WHERE documento <> '' AND documento NOT LIKE '%%X%%' "
     "AND documento NOT LIKE '%%x%%' AND documento NOT LIKE '%%*%%'"),
    ('uniq_parte_documento_mascarado',
     "CREATE UNIQUE INDEX CONCURRENTLY uniq_parte_documento_mascarado "
     "ON tribunals_parte (nome, documento) "
     "WHERE documento LIKE '%%X%%' OR documento LIKE '%%x%%' "
     "OR documento LIKE '%%*%%'"),
    ('uniq_parte_oab',
     "CREATE UNIQUE INDEX CONCURRENTLY uniq_parte_oab "
     "ON tribunals_parte (oab) WHERE oab <> ''"),
]


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cur:
        for nome, create_sql in INDICES:
            # Dropa o husk inválido (sem IF NOT EXISTS no CREATE — IF NOT
            # EXISTS foi o que mascarou o bug original).
            cur.execute(f'DROP INDEX IF EXISTS {nome}')
            cur.execute(create_sql)
            # Verifica que o índice validou. Se não, aborta a migration.
            cur.execute(
                "SELECT idx.indisvalid FROM pg_index idx "
                "JOIN pg_class i ON i.oid = idx.indexrelid "
                "WHERE i.relname = %s", [nome])
            row = cur.fetchone()
            if not row or not row[0]:
                raise RuntimeError(
                    f'{nome} criado mas indisvalid=false — ainda há '
                    f'duplicatas. Rode dedup_partes antes.')


def reverse(apps, schema_editor):
    pass  # índices declarados no model; remoção via model + makemigrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('tribunals', '0017_restore_missing_indexes')]
    operations = [migrations.RunPython(forward, reverse, elidable=False)]
```

⚠️ Ajustar a dependência `('tribunals', '0017_...')` pra a **última**
migration de `tribunals` (rodar `ls tribunals/migrations/`), e renomear o
arquivo com o número correto.

- [ ] **Step 5.2: Verificar que a migration parseia e o Django a reconhece**

Run: `docker compose exec web python manage.py makemigrations --check --dry-run`
Expected: "No changes detected" (a migration é RunPython pura, não muda model state).

- [ ] **Step 5.3: Commit**

```bash
git add tribunals/migrations/00NN_recriar_indices_unicos_parte.py
git commit -m "feat(dedup): migration recria índices únicos de Parte com verificação de indisvalid"
```

---

## Task 6: Command `check_parte_indexes` (hardening)

**Files:**
- Create: `tribunals/management/commands/check_parte_indexes.py`

Um check rápido pra runbook/monitoramento: detecta índice único inválido
antes que ele cause acúmulo silencioso de novo.

- [ ] **Step 6.1: Criar o command**

```python
# tribunals/management/commands/check_parte_indexes.py
"""Verifica que os índices únicos de tribunals_parte/processoparte estão
VÁLIDOS. Exit 1 se algum estiver inválido — serve pra runbook e cron de
monitoramento. Um índice único inválido não enforça nada e deixa o
upsert do drainer duplicar Partes silenciosamente.
"""
from django.core.management.base import BaseCommand
from django.db import connection

ESPERADOS = [
    'uniq_parte_oab', 'uniq_parte_documento_real',
    'uniq_parte_documento_mascarado', 'uniq_parte_sem_doc_nem_oab',
    'uniq_processo_parte_polo_papel_principal',
]


class Command(BaseCommand):
    help = 'Verifica indisvalid dos índices únicos de Parte/ProcessoParte.'

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT i.relname, idx.indisvalid FROM pg_index idx "
                "JOIN pg_class i ON i.oid = idx.indexrelid "
                "WHERE i.relname = ANY(%s)", [ESPERADOS])
            estado = dict(cur.fetchall())
        invalidos, faltando = [], []
        for nome in ESPERADOS:
            if nome not in estado:
                faltando.append(nome)
            elif not estado[nome]:
                invalidos.append(nome)
        for nome in invalidos:
            self.stderr.write(self.style.ERROR(f'INVÁLIDO: {nome}'))
        for nome in faltando:
            self.stderr.write(self.style.ERROR(f'FALTANDO: {nome}'))
        if invalidos or faltando:
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            f'OK — {len(ESPERADOS)} índices únicos válidos.'))
```

- [ ] **Step 6.2: Verificar que carrega**

Run: `docker compose exec web python manage.py check_parte_indexes --help`
Expected: imprime o help sem erro de import.

- [ ] **Step 6.3: Commit**

```bash
git add tribunals/management/commands/check_parte_indexes.py
git commit -m "feat(dedup): command check_parte_indexes pra detectar índice único inválido"
```

---

## Task 7: Execução em prod — janela de manutenção

**Files:** nenhum — operação. Branch já mergeada na `main` e deployada (`web` rebuildado).

⚠️ **Destrutivo e irreversível.** Confirmar baseline da Task 1 antes.

- [ ] **Step 7.1: Snapshot de segurança do `tribunals_parte` + `processoparte`**

```bash
ssh ubuntu@100.124.92.20 "pg_dump -h localhost -U voyager -d voyager \
  -t tribunals_parte -t tribunals_processoparte -Fc -Z6 \
  -f /tmp/parte_pp_pre_dedup_$(date +%F).dump && ls -la /tmp/parte_pp_pre_dedup_*.dump"
```
Expected: arquivo .dump criado (host db = `voyager-db`, Tailscale `100.124.92.20`).

- [ ] **Step 7.2: Parar os drainers de enrichment (senão a dedup persegue alvo móvel)**

```bash
ssh ubuntu@100.100.144.57 "cd ~/voyager && docker compose -f docker-compose-prod.yml \
  stop enrichment_drainer enrichment_drainer_p0 enrichment_drainer_p1 \
       enrichment_drainer_p2 enrichment_drainer_p3"
```
Expected: 5 containers parados. (Os workers de scrape em `.36` podem continuar — eles só publicam no stream Redis, que será drenado depois.)

- [ ] **Step 7.3: Dry-run da dedup — conferir as contagens antes de escrever**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py dedup_partes --group all --dry-run"
```
Expected: imprime `[oab] losers...`, `[doc_real] losers...`, `[doc_masc] losers...`. Somados ≈ 80M. Sanidade: bate com o baseline da Task 1.

- [ ] **Step 7.4: Rodar a dedup pra valer (background — leva horas)**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py dedup_partes --group all 2>&1 | tee /tmp/dedup_partes.log"
```
Acompanhar via `/tmp/dedup_partes.log` — cada lote imprime progresso. Resumível: se cair, re-rodar o mesmo comando continua (recalcula grupos com count>1).

- [ ] **Step 7.5: Aplicar a migration de recriação dos índices**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py migrate tribunals"
```
Expected: a migration `00NN_recriar_indices_unicos_parte` roda sem erro. Se abortar com `indisvalid=false` → ainda há duplicata → re-rodar Step 7.4.

- [ ] **Step 7.6: Verificar os índices**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py check_parte_indexes"
```
Expected: `OK — 5 índices únicos válidos.`

- [ ] **Step 7.7: Recalcular `Parte.total_processos`**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python -c \"
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from django.db import connection
with connection.cursor() as c:
    c.execute('''UPDATE tribunals_parte p SET total_processos = COALESCE(s.n,0)
                 FROM (SELECT parte_id, count(DISTINCT processo_id) n
                       FROM tribunals_processoparte GROUP BY parte_id) s
                 WHERE s.parte_id = p.id''')
print('total_processos recalculado')
\""
```

- [ ] **Step 7.8: Religar os drainers**

```bash
ssh ubuntu@100.100.144.57 "cd ~/voyager && docker compose -f docker-compose-prod.yml \
  up -d enrichment_drainer enrichment_drainer_p0 enrichment_drainer_p1 \
        enrichment_drainer_p2 enrichment_drainer_p3"
```
Expected: 5 containers `Up`. Eles drenam o backlog do stream — agora com os índices válidos, sem re-duplicar.

- [ ] **Step 7.9: Verificação final**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python -c \"
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from tribunals.models import Parte
from django.db import connection
print('Parte total agora:', Parte.objects.count())
c=connection.cursor()
c.execute('SELECT oab,count(*) FROM tribunals_parte WHERE oab<>chr(39)||chr(39) GROUP BY oab HAVING count(*)>1 LIMIT 1')
print('algum oab duplicado?', c.fetchall() or 'NENHUM')
\""
```
Expected: `Parte total` caiu pra ~3-4M; `algum oab duplicado? NENHUM`.

---

## Task 8: Documentação

**Files:**
- Modify: `.ia/ENRICHMENT.md`, `.ia/OPS.md`

- [ ] **Step 8.1: `.ia/ENRICHMENT.md` — documentar a armadilha**

Adicionar ao fim da seção "Dedupe de partes":
```markdown
### Armadilha: CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS

Os 3 índices únicos parciais de `Parte` ficaram **inválidos** em 2026
(migration 0017): `CREATE UNIQUE INDEX CONCURRENTLY` falha na validação
se a tabela já tem duplicatas, deixando um índice `indisvalid=false`; o
`IF NOT EXISTS` fez re-execuções pularem o husk. Índice inválido não
enforça — o `bulk_create(ignore_conflicts)` do drainer parou de
deduplicar e a tabela inflou de ~4M pra ~84M linhas.

Corrigido pelo command `dedup_partes` + migration de recriação (que
**dropa** o husk e **verifica `indisvalid`**). Monitorar com
`manage.py check_parte_indexes`.
```

- [ ] **Step 8.2: `.ia/OPS.md` — runbook**

Adicionar uma seção "Deduplicação de Partes / índices únicos inválidos"
com: como rodar `check_parte_indexes`, e o procedimento da Task 7
(parar drainers → `dedup_partes` → `migrate` → religar).

- [ ] **Step 8.3: Commit**

```bash
git add .ia/ENRICHMENT.md .ia/OPS.md
git commit -m "docs: dedup de Partes + armadilha do CONCURRENTLY IF NOT EXISTS"
```

---

## Critério de conclusão

1. `check_parte_indexes` → `OK — 5 índices únicos válidos`.
2. `tribunals_parte` caiu de ~84M pra ~3-4M linhas.
3. Nenhum `oab` com count > 1; nenhum `documento` real com count > 1.
4. `ProcessoParte` não perdeu nenhuma participação real (count estável module colisões legítimas colapsadas).
5. Drainers religados, processando sem re-duplicar (rodar `check_parte_indexes` de novo após 1h).
6. Testes `tests/test_dedup_partes.py` passando.

## Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Fundir homônimos | Colapso só por chave EXATA; oab/CPF são IDs nacionais únicos; doc_masc só colapsa `(nome,doc)` byte-idêntico — ver "Princípio de correção" |
| Repoint cria ProcessoParte duplicado | Step 3.3 deleta a ProcessoParte-loser colidente ANTES do UPDATE |
| `CREATE UNIQUE INDEX` falha de novo | Migration verifica `indisvalid` e aborta com erro claro → re-rodar dedup |
| Dedup interrompida no meio | Command é resumível (recalcula grupos com count>1 a cada run) |
| Perda de dados | `pg_dump` das 2 tabelas antes (Step 7.1) |
| Drainer re-duplicando durante a dedup | Drainers parados na janela (Step 7.2) |
| `total_processos` fica errado | Recalculado no Step 7.7 |
