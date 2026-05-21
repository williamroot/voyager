# Deduplicação de `Parte` + Recriação de Índices Únicos — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Colapsar as ~80M linhas duplicadas de `tribunals_parte` (real: ~3-4M entidades) de volta às entidades reais, repontando as 30,7M FKs de `ProcessoParte`, e recriar os 3 índices únicos parciais **válidos** — sem unificar homônimos errado.

**Architecture:** Um management command `dedup_partes` faz a deduplicação **set-based em SQL** (loop Python em 80M linhas é inviável), em lotes resumíveis, com `--dry-run`. Duas operações: (A) colapso por chave de constraint byte-idêntica (oab / doc real / `(nome,doc)` mascarado); (B) absorção masked→real quando nome bate exato + máscara casa + há exatamente 1 candidato. Depois, uma migration recria os 3 índices `CONCURRENTLY` **dropando o husk inválido antes** e **verificando `indisvalid`**.

**Tech Stack:** Django 5, PostgreSQL 16, management commands, SQL set-based (CTE + window functions + `translate`/`LIKE`), `psql`.

---

## Causa raiz (confirmada — Phase 1-3 do debugging fechadas)

`pg_index` em prod: `uniq_parte_oab`, `uniq_parte_documento_real`,
`uniq_parte_documento_mascarado` estão com **`indisvalid=false, indisready=false`**
— cascas mortas. Só `uniq_parte_sem_doc_nem_oab` é válido.

**Mecanismo:** migration `0017_restore_missing_indexes.py` roda
`CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS`. Na 1ª execução a tabela já
tinha duplicatas (populada de dump sem índices — vide docstring da própria
0017) → o build concurrent **falhou na validação** → deixou índice inválido.
Re-execuções veem o husk via `IF NOT EXISTS` e **pulam** → a migration fica
marcada como aplicada, os índices nunca enforçam.

Índice inválido não enforça unicidade, e `bulk_create(ignore_conflicts=True)`
do drainer (`ON CONFLICT DO NOTHING`) não tem constraint válida pra conflitar
→ **todo insert passa**. O drainer re-insere as partes de cada processo a cada
re-enriquecimento → acumulação infinita.

**Evidência:** `oab='RS65244A'` aparece 48.376× (impossível sob unique
válido). 44M advogados / 540k OABs distintas. 76,4M linhas com doc mascarado.
Máscaras revelam só 3 dígitos (CPF `DDD.XXX.XXX-XX`) ou 3-4 (CNPJ).

## Princípio de correção — anti-homônimo (NÃO-NEGOCIÁVEL)

A deduplicação tem **dois tipos de operação**, ambas com salvaguardas explícitas.

### A) Colapso por chave byte-idêntica (3 grupos)

| Grupo | Chave de colapso | Risco de homônimo |
|---|---|---|
| `oab` | `oab` exato (`oab <> ''`) | **Zero** — OAB (UF+número) é identificador nacional único |
| `doc_real` | `documento` exato (CPF/CNPJ sem máscara) | **Zero** — CPF/CNPJ é identificador nacional único |
| `doc_masc` | `(nome, documento)` exato (doc mascarado) | Limitado ao que a constraint `uniq_parte_documento_mascarado` **já** definia como identidade — só remove cópias literais da mesma chave |

Survivor de cada grupo = **`MIN(id)`** (linha mais antiga). Determinístico.

### B) Absorção masked→real (`masc_to_real`)

Funde uma Parte de **doc mascarado** numa Parte de **doc real** correspondente.
A chave forte é o **nome completo byte-idêntico** (máscaras só revelam 3 dígitos).

Funde **somente** quando **TODAS** valem:
1. `nome` **byte-idêntico** entre a Parte mascarada e a real.
2. O `documento` real **casa posição-a-posição** com a máscara. Em SQL:
   `real.documento LIKE translate(masc.documento,'Xx*','___')`.
3. **Existe exatamente UM** candidato real satisfazendo 1+2. Se houver **2+**
   (dois homônimos reais, ambos compatíveis) → **AMBÍGUO → NÃO funde**. Se 0 →
   mantém a mascarada separada.

Direção sempre masked→real (doc real = identidade mais forte; survivor = a real).
Roda **depois** do colapso de `doc_real` e `doc_masc`.

**Risco residual aceito:** Parte mascarada = pessoa A, único real do mesmo nome
= pessoa B (homônimo, 3 dígitos iguais, doc real de A ausente do banco) → funde
A→B errado. Probabilidade baixa; nível de confiança aprovado pelo dono do produto.

**Proibido em qualquer caso:** fusão por nome só, similaridade, fuzzy, trigram,
soundex; fundir `masc_to_real` com 2+ candidatos.

## File structure

| Caminho | Responsabilidade | Ação |
|---|---|---|
| `tribunals/management/commands/dedup_partes.py` | Command set-based: colapso (3 grupos) + absorção masc_to_real, em lotes, `--dry-run` | **Criar** |
| `tribunals/management/commands/check_parte_indexes.py` | Verifica `indisvalid` dos 5 índices únicos; exit 1 se algum inválido | **Criar** |
| `tribunals/migrations/00NN_recriar_indices_unicos_parte.py` | Drop dos 3 husks inválidos + `CREATE ... CONCURRENTLY` + verificação `indisvalid` | **Criar** |
| `tests/test_dedup_partes.py` | Testes do command em fixture pequena | **Criar** |
| `.ia/ENRICHMENT.md` | Documenta a dedup + a armadilha do `CONCURRENTLY IF NOT EXISTS` | **Modificar** |
| `.ia/OPS.md` | Runbook: como rodar a dedup, `check_parte_indexes` | **Modificar** |

Sem mudança nos models `Parte`/`ProcessoParte` — as `UniqueConstraint` já estão
declaradas corretas; o problema é só o estado físico do DB.

## Pré-requisitos

- SSH `ubuntu@100.100.144.57` (web/.32, Tailscale) e `ubuntu@100.124.92.20` (db).
- Janela de manutenção: drainers de enrichment **parados** durante a dedup.
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
c.execute('''SELECT t.relname, i.relname, idx.indisvalid
  FROM pg_index idx JOIN pg_class i ON i.oid=idx.indexrelid JOIN pg_class t ON t.oid=idx.indrelid
  WHERE t.relname IN ('tribunals_parte','tribunals_processoparte') AND NOT idx.indisvalid''')
for r in c.fetchall(): print(r)
print('--- fim ---')
\""
```

Expected: lista os 3 husks de `tribunals_parte`. **Anotar se `uniq_processo_parte_polo_papel_principal` também aparece** — o repoint da Task 3 já é à prova de colisão nos dois casos, mas registre.

- [ ] **Step 1.2: Snapshot de contagens (baseline)**

Run:
```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python -c \"
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from django.db import connection
c=connection.cursor()
for label,sql in [('parte_total','SELECT count(*) FROM tribunals_parte'),
 ('processoparte_total','SELECT count(*) FROM tribunals_processoparte')]:
    c.execute(sql); print(label, c.fetchone()[0])
\""
```

Anotar abaixo (baseline pra validar o resultado):
```
baseline parte_total: ___
baseline processoparte_total: ___
```

---

## Task 2: Command `dedup_partes` — esqueleto

**Files:**
- Create: `tribunals/management/commands/dedup_partes.py`
- Test: `tests/test_dedup_partes.py`

- [ ] **Step 2.1: Escrever os testes de roteamento (TDD)**

```python
# tests/test_dedup_partes.py
import pytest
from django.core.management import call_command
from tribunals.models import Tribunal, Process, Parte, ProcessoParte

pytestmark = pytest.mark.django_db


def _proc(n='1'):
    t, _ = Tribunal.objects.get_or_create(sigla='TRF1', defaults={'sigla_djen': 'TRF1', 'nome': 'TRF1'})
    return Process.objects.create(tribunal=t, numero_cnj=f'{n:0>7}-00.2024.4.01.0000')


def test_dedup_oab_colapsa_para_min_id():
    p1 = Parte.objects.create(nome='ADV UM', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='ADV UM VARIANTE', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='ADV UM', oab='SP111', tipo='advogado')
    call_command('dedup_partes', '--group', 'oab')
    restantes = list(Parte.objects.filter(oab='SP111'))
    assert len(restantes) == 1 and restantes[0].id == p1.id


def test_dedup_nao_funde_oabs_diferentes():
    Parte.objects.create(nome='JOSE DA SILVA', oab='SP111', tipo='advogado')
    Parte.objects.create(nome='JOSE DA SILVA', oab='SP222', tipo='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert Parte.objects.filter(nome='JOSE DA SILVA').count() == 2
```

- [ ] **Step 2.2: Rodar — deve falhar (command não existe)**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -v`
Expected: FAIL — `Unknown command: 'dedup_partes'`.

- [ ] **Step 2.3: Escrever o esqueleto do command**

```python
# tribunals/management/commands/dedup_partes.py
"""Deduplica tribunals_parte. Causado por índices únicos parciais que
ficaram INVÁLIDOS (CREATE UNIQUE INDEX CONCURRENTLY que falhou — ver
migration 0017).

Set-based em SQL: Python loop em ~80M linhas é inviável. Idempotente e
resumível — re-rodar após interrupção recalcula e continua.

Anti-homônimo: ver "Princípio de correção" no plano. Colapso só por chave
EXATA; absorção masc_to_real só com 1 candidato. Survivor = MIN(id) /
sempre a Parte de doc real.
"""
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

logger = logging.getLogger('voyager.dedup_partes')

# Grupos de colapso por chave byte-idêntica: nome -> (predicado WHERE, PARTITION BY)
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
ORDEM_ALL = ['oab', 'doc_real', 'doc_masc', 'masc_to_real']


class Command(BaseCommand):
    help = 'Deduplica tribunals_parte (anti-homônimo). Ver plano dedup-partes.'

    def add_arguments(self, parser):
        parser.add_argument('--group', choices=ORDEM_ALL + ['all'], default='all')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200_000)

    def handle(self, *args, **opts):
        grupos = ORDEM_ALL if opts['group'] == 'all' else [opts['group']]
        for g in grupos:
            if g == 'masc_to_real':
                self._merge_masc_to_real(dry_run=opts['dry_run'], batch=opts['batch_size'])
            else:
                self._dedup_grupo(g, dry_run=opts['dry_run'], batch=opts['batch_size'])

    def _dedup_grupo(self, grupo, *, dry_run, batch):
        raise CommandError(f'[{grupo}] colapso não implementado — ver Task 3')

    def _merge_masc_to_real(self, *, dry_run, batch):
        raise CommandError('[masc_to_real] não implementado — ver Task 4')
```

- [ ] **Step 2.4: Rodar — command reconhecido, colapso ainda não**

Run: `docker compose exec web pytest tests/test_dedup_partes.py::test_dedup_oab_colapsa_para_min_id -v`
Expected: FAIL com `CommandError: [oab] colapso não implementado — ver Task 3` (não mais "Unknown command").

- [ ] **Step 2.5: Commit**

```bash
git add tribunals/management/commands/dedup_partes.py tests/test_dedup_partes.py
git commit -m "feat(dedup): esqueleto do command dedup_partes"
```

---

## Task 3: Colapso por chave — `_apply_dedup_map` + `_dedup_grupo`

**Files:**
- Modify: `tribunals/management/commands/dedup_partes.py`
- Modify: `tests/test_dedup_partes.py`

`_apply_dedup_map` é o motor compartilhado: dado um TEMP TABLE `_dedup_map
(loser_id, survivor_id)`, repointa `ProcessoParte` à prova de colisão e deleta
as Partes-loser. `_dedup_grupo` constrói o map via `min(id) OVER (PARTITION BY)`.

- [ ] **Step 3.1: Escrever os testes de repoint + colisão**

```python
# adicionar em tests/test_dedup_partes.py
def test_dedup_repoint_processoparte():
    proc = _proc(n='100')
    p1 = Parte.objects.create(nome='ADV', oab='RS9', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV', oab='RS9', tipo='advogado')
    pp = ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    pp.refresh_from_db()
    assert pp.parte_id == p1.id
    assert not Parte.objects.filter(id=p2.id).exists()


def test_dedup_collisao_processoparte_nao_duplica():
    proc = _proc(n='200')
    p1 = Parte.objects.create(nome='ADV', oab='RS8', tipo='advogado')
    p2 = Parte.objects.create(nome='ADV', oab='RS8', tipo='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p1, polo='ativo', papel='advogado')
    ProcessoParte.objects.create(processo=proc, parte=p2, polo='ativo', papel='advogado')
    call_command('dedup_partes', '--group', 'oab')
    assert ProcessoParte.objects.filter(processo=proc).count() == 1
    assert ProcessoParte.objects.get(processo=proc).parte_id == p1.id


def test_doc_masc_colapsa_nome_e_doc_identicos():
    p1 = Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    qs = Parte.objects.filter(nome='MARIA SOUZA', documento='639.XXX.XXX-XX')
    assert qs.count() == 1 and qs.first().id == p1.id


def test_doc_masc_nao_funde_nomes_diferentes_mesma_mascara():
    Parte.objects.create(nome='MARIA SOUZA', documento='639.XXX.XXX-XX', tipo='pf')
    Parte.objects.create(nome='MARIA SANTOS', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'doc_masc')
    assert Parte.objects.filter(documento='639.XXX.XXX-XX').count() == 2
```

- [ ] **Step 3.2: Rodar — deve falhar**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -k "repoint or collisao or doc_masc" -v`
Expected: FAIL com `CommandError: [oab] colapso não implementado`.

- [ ] **Step 3.3: Implementar `_apply_dedup_map` e `_dedup_grupo`**

Substituir o método `_dedup_grupo` (esqueleto de 1 linha) por estes DOIS métodos:

```python
    def _apply_dedup_map(self, *, label, dry_run, batch):
        """Consome a TEMP TABLE _dedup_map(loser_id, survivor_id) já criada
        e indexada por loser_id. Repointa ProcessoParte (à prova de colisão
        com uniq_processo_parte_polo_papel_principal) e deleta as Partes-loser.
        """
        with connection.cursor() as cur:
            cur.execute('SELECT count(*), min(loser_id), max(loser_id) FROM _dedup_map')
            total, lo, hi = cur.fetchone()
        self.stdout.write(f'[{label}] losers a colapsar: {total or 0:,}'
                          + ('  (DRY-RUN)' if dry_run else ''))
        if dry_run or not total:
            return
        t0 = time.time()
        cursor_id = lo
        while cursor_id <= hi:
            fim = cursor_id + batch
            with transaction.atomic():
                with connection.cursor() as c2:
                    # 1) Apaga ProcessoParte-loser que colidiria com uma
                    #    ProcessoParte-survivor já existente no mesmo processo.
                    c2.execute("""
                        DELETE FROM tribunals_processoparte ppl
                        USING _dedup_map m, tribunals_processoparte pps
                        WHERE ppl.parte_id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                          AND pps.processo_id = ppl.processo_id
                          AND pps.parte_id = m.survivor_id
                          AND pps.polo = ppl.polo AND pps.papel = ppl.papel
                          AND pps.representa_id IS NOT DISTINCT FROM ppl.representa_id
                    """, [cursor_id, fim])
                    # 2) Repointa o restante.
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
            self.stdout.write(f'[{label}] lote {cursor_id:,}–{fim:,} ok '
                              f'({time.time() - t0:.0f}s)')
            cursor_id = fim
        self.stdout.write(self.style.SUCCESS(f'[{label}] concluído'))

    def _dedup_grupo(self, grupo, *, dry_run, batch):
        where, partition = GRUPOS[grupo]
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute(f"""
                CREATE TEMP TABLE _dedup_map AS
                SELECT id AS loser_id,
                       min(id) OVER (PARTITION BY {partition}) AS survivor_id
                FROM tribunals_parte WHERE {where}
            """)
            cur.execute('DELETE FROM _dedup_map WHERE loser_id = survivor_id')
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
        self._apply_dedup_map(label=grupo, dry_run=dry_run, batch=batch)
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
```

- [ ] **Step 3.4: Rodar os testes**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -k "repoint or collisao or doc_masc or oab" -v`
Expected: PASS — repoint, colisão, doc_masc colapsa, doc_masc não funde nomes diferentes, oab colapsa, oab não funde diferentes.

- [ ] **Step 3.5: Commit**

```bash
git add tribunals/management/commands/dedup_partes.py tests/test_dedup_partes.py
git commit -m "feat(dedup): colapso set-based por chave + repoint collision-safe"
```

---

## Task 4: Absorção `masc_to_real` (com trava de 1 candidato)

**Files:**
- Modify: `tribunals/management/commands/dedup_partes.py`
- Modify: `tests/test_dedup_partes.py`

- [ ] **Step 4.1: Escrever os testes da absorção**

```python
# adicionar em tests/test_dedup_partes.py
def test_masc_to_real_funde_quando_um_candidato():
    """Mascarada absorvida pela real: nome igual + dígitos batem + 1 candidato."""
    real = Parte.objects.create(nome='ANA LIMA', documento='639.979.036-40', tipo='pf')
    masc = Parte.objects.create(nome='ANA LIMA', documento='639.XXX.XXX-XX', tipo='pf')
    proc = _proc(n='300')
    pp = ProcessoParte.objects.create(processo=proc, parte=masc, polo='ativo', papel='autor')
    call_command('dedup_partes', '--group', 'masc_to_real')
    pp.refresh_from_db()
    assert pp.parte_id == real.id
    assert not Parte.objects.filter(id=masc.id).exists()


def test_masc_to_real_nao_funde_com_dois_candidatos():
    """2 reais homônimos compatíveis com a máscara → AMBÍGUO → não funde."""
    Parte.objects.create(nome='JOSE SILVA', documento='639.111.222-33', tipo='pf')
    Parte.objects.create(nome='JOSE SILVA', documento='639.444.555-66', tipo='pf')
    masc = Parte.objects.create(nome='JOSE SILVA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'masc_to_real')
    assert Parte.objects.filter(id=masc.id).exists()  # mascarada intacta
    assert Parte.objects.filter(nome='JOSE SILVA').count() == 3


def test_masc_to_real_nao_funde_digitos_divergentes():
    """Real com 3 primeiros dígitos diferentes da máscara → não funde."""
    Parte.objects.create(nome='PEDRO ROCHA', documento='100.111.222-33', tipo='pf')
    masc = Parte.objects.create(nome='PEDRO ROCHA', documento='639.XXX.XXX-XX', tipo='pf')
    call_command('dedup_partes', '--group', 'masc_to_real')
    assert Parte.objects.filter(id=masc.id).exists()
```

- [ ] **Step 4.2: Rodar — deve falhar**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -k masc_to_real -v`
Expected: FAIL com `CommandError: [masc_to_real] não implementado`.

- [ ] **Step 4.3: Implementar `_merge_masc_to_real`**

Substituir o método `_merge_masc_to_real` (esqueleto de 1 linha) por:

```python
    def _merge_masc_to_real(self, *, dry_run, batch):
        """Absorve Parte mascarada na Parte de doc real correspondente.
        Só funde com nome byte-idêntico + máscara casando + EXATAMENTE 1
        candidato real. `translate(doc,'Xx*','___')` vira o pattern LIKE.
        """
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            # masc_id -> real_id, só onde há exatamente 1 real candidato.
            cur.execute("""
                CREATE TEMP TABLE _dedup_map AS
                SELECT masc_id AS loser_id, real_id AS survivor_id FROM (
                    SELECT m.id AS masc_id, min(r.id) AS real_id, count(*) AS n
                    FROM tribunals_parte m
                    JOIN tribunals_parte r
                      ON r.nome = m.nome
                     AND r.id <> m.id
                     AND r.documento <> ''
                     AND r.documento NOT LIKE '%X%' AND r.documento NOT LIKE '%x%'
                     AND r.documento NOT LIKE '%*%'
                     AND r.documento LIKE translate(m.documento, 'Xx*', '___')
                    WHERE m.documento LIKE '%X%' OR m.documento LIKE '%x%'
                       OR m.documento LIKE '%*%'
                    GROUP BY m.id
                ) cand
                WHERE n = 1
            """)
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
        self._apply_dedup_map(label='masc_to_real', dry_run=dry_run, batch=batch)
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
```

- [ ] **Step 4.4: Rodar todos os testes**

Run: `docker compose exec web pytest tests/test_dedup_partes.py -v`
Expected: PASS — todos (oab, doc_real implícito, doc_masc, repoint, colisão, masc_to_real funde com 1, não funde com 2, não funde dígitos divergentes).

- [ ] **Step 4.5: Commit**

```bash
git add tribunals/management/commands/dedup_partes.py tests/test_dedup_partes.py
git commit -m "feat(dedup): absorção masc_to_real com trava de candidato único"
```

---

## Task 5: Migration de recriação dos índices únicos

**Files:**
- Create: `tribunals/migrations/00NN_recriar_indices_unicos_parte.py` (N = próximo nº; `ls tribunals/migrations/ | tail -3`)

- [ ] **Step 5.1: Criar a migration**

```python
# tribunals/migrations/00NN_recriar_indices_unicos_parte.py
"""Recria os 3 índices únicos parciais de tribunals_parte que ficaram
INVÁLIDOS (CONCURRENTLY que falhou na 0017; IF NOT EXISTS perpetuou o husk).

Pré-requisito: rodar `dedup_partes` ANTES. Aborta com erro claro se ainda
houver duplicata (o CREATE UNIQUE INDEX não valida).

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
            cur.execute(f'DROP INDEX IF EXISTS {nome}')
            cur.execute(create_sql)
            cur.execute(
                "SELECT idx.indisvalid FROM pg_index idx "
                "JOIN pg_class i ON i.oid = idx.indexrelid "
                "WHERE i.relname = %s", [nome])
            row = cur.fetchone()
            if not row or not row[0]:
                raise RuntimeError(
                    f'{nome}: indisvalid=false — ainda há duplicata. '
                    f'Rode dedup_partes antes.')


def reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('tribunals', '0017_restore_missing_indexes')]
    operations = [migrations.RunPython(forward, reverse, elidable=False)]
```

⚠️ Ajustar `dependencies` pra a **última** migration de `tribunals` e renomear
o arquivo com o número correto.

- [ ] **Step 5.2: Verificar que o Django reconhece**

Run: `docker compose exec web python manage.py makemigrations --check --dry-run`
Expected: "No changes detected".

- [ ] **Step 5.3: Commit**

```bash
git add tribunals/migrations/00NN_recriar_indices_unicos_parte.py
git commit -m "feat(dedup): migration recria índices únicos de Parte com verificação indisvalid"
```

---

## Task 6: Command `check_parte_indexes` (hardening)

**Files:**
- Create: `tribunals/management/commands/check_parte_indexes.py`

- [ ] **Step 6.1: Criar o command**

```python
# tribunals/management/commands/check_parte_indexes.py
"""Verifica que os índices únicos de tribunals_parte/processoparte estão
VÁLIDOS. Exit 1 se algum inválido/faltando — pra runbook e monitoramento.
Índice único inválido não enforça nada e deixa o upsert do drainer
duplicar Partes silenciosamente.
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
        problemas = []
        for nome in ESPERADOS:
            if nome not in estado:
                problemas.append(f'FALTANDO: {nome}')
            elif not estado[nome]:
                problemas.append(f'INVÁLIDO: {nome}')
        for p in problemas:
            self.stderr.write(self.style.ERROR(p))
        if problemas:
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
git commit -m "feat(dedup): command check_parte_indexes"
```

---

## Task 7: Execução em prod — janela de manutenção

**Files:** nenhum — operação. Branch mergeada na `main` e `web` rebuildado antes.

⚠️ **Destrutivo e irreversível.** Confirmar baseline da Task 1.

- [ ] **Step 7.1: Snapshot de segurança (pg_dump das 2 tabelas)**

```bash
ssh ubuntu@100.124.92.20 "pg_dump -h localhost -U voyager -d voyager \
  -t tribunals_parte -t tribunals_processoparte -Fc -Z6 \
  -f /tmp/parte_pp_pre_dedup_$(date +%F).dump && ls -la /tmp/parte_pp_pre_dedup_*.dump"
```
Expected: arquivo .dump criado.

- [ ] **Step 7.2: Parar os drainers de enrichment**

```bash
ssh ubuntu@100.100.144.57 "cd ~/voyager && docker compose -f docker-compose-prod.yml \
  stop enrichment_drainer enrichment_drainer_p0 enrichment_drainer_p1 \
       enrichment_drainer_p2 enrichment_drainer_p3"
```
Expected: 5 containers parados.

- [ ] **Step 7.3: Dry-run**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py dedup_partes --group all --dry-run"
```
Expected: imprime losers de `oab`, `doc_real`, `doc_masc`, `masc_to_real`. Sanidade vs baseline.

- [ ] **Step 7.4: Rodar a dedup (background — leva horas)**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py dedup_partes --group all 2>&1 | tee /tmp/dedup_partes.log"
```
Ordem automática: oab → doc_real → doc_masc → masc_to_real. Resumível: re-rodar continua.

- [ ] **Step 7.5: Aplicar a migration de recriação dos índices**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml \
  exec -T web python manage.py migrate tribunals"
```
Expected: roda sem erro. Se abortar com `indisvalid=false` → re-rodar Step 7.4.

- [ ] **Step 7.6: Verificar índices**

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
Expected: 5 containers `Up`.

- [ ] **Step 7.9: Verificação final**

```bash
ssh ubuntu@100.100.144.57 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python -c \"
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from tribunals.models import Parte
from django.db import connection
print('Parte total agora:', Parte.objects.count())
c=connection.cursor()
c.execute('SELECT oab,count(*) FROM tribunals_parte WHERE oab<>chr(39)||chr(39) GROUP BY oab HAVING count(*)>1 LIMIT 1')
print('oab duplicado?', c.fetchall() or 'NENHUM')
\""
```
Expected: `Parte total` caiu pra ~3-4M; `oab duplicado? NENHUM`.

---

## Task 8: Documentação

**Files:**
- Modify: `.ia/ENRICHMENT.md`, `.ia/OPS.md`

- [ ] **Step 8.1: `.ia/ENRICHMENT.md` — armadilha + dedup**

Adicionar ao fim da seção "Dedupe de partes":
```markdown
### Armadilha: CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS

Os 3 índices únicos parciais de `Parte` ficaram **inválidos** em 2026
(migration 0017): `CREATE UNIQUE INDEX CONCURRENTLY` falha na validação
se a tabela já tem duplicatas, deixando `indisvalid=false`; o `IF NOT
EXISTS` fez re-execuções pularem o husk. Índice inválido não enforça — o
`bulk_create(ignore_conflicts)` do drainer parou de deduplicar e a tabela
inflou de ~4M pra ~84M linhas.

Corrigido pelo command `dedup_partes` (colapso por chave + absorção
masc_to_real com trava de candidato único) + migration que **dropa** o
husk e **verifica `indisvalid`**. Monitorar com `manage.py
check_parte_indexes`.
```

- [ ] **Step 8.2: `.ia/OPS.md` — runbook**

Adicionar seção "Deduplicação de Partes / índices únicos inválidos": como
rodar `check_parte_indexes`, e o procedimento da Task 7 (parar drainers →
`dedup_partes` → `migrate` → recalcular total → religar).

- [ ] **Step 8.3: Commit**

```bash
git add .ia/ENRICHMENT.md .ia/OPS.md
git commit -m "docs: dedup de Partes + armadilha do CONCURRENTLY IF NOT EXISTS"
```

---

## Critério de conclusão

1. `check_parte_indexes` → `OK — 5 índices únicos válidos`.
2. `tribunals_parte` caiu de ~84M pra ~3-4M linhas.
3. Nenhum `oab` nem `documento` real com count > 1.
4. `ProcessoParte` não perdeu participação real.
5. Drainers religados, sem re-duplicar (`check_parte_indexes` de novo após 1h).
6. `tests/test_dedup_partes.py` passando (9 testes).

## Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Fundir homônimos no colapso | Colapso só por chave EXATA; oab/CPF são IDs nacionais únicos |
| Fundir homônimos no masc_to_real | Trava: só com 1 candidato real; 2+ = ambíguo = não funde (Step 4.3) |
| Repoint cria ProcessoParte duplicado | `_apply_dedup_map` deleta a PP-loser colidente ANTES do UPDATE |
| `CREATE UNIQUE INDEX` falha de novo | Migration verifica `indisvalid` e aborta com erro claro |
| Dedup interrompida no meio | Command resumível (recalcula a cada run) |
| Perda de dados | `pg_dump` das 2 tabelas antes (Step 7.1) |
| Drainer re-duplicando durante a dedup | Drainers parados na janela (Step 7.2) |
| `total_processos` errado pós-dedup | Recalculado no Step 7.7 |
