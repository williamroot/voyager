# Modelo de dados

Todas as entidades em `tribunals/models.py`. Migrations em `tribunals/migrations/`.

## Tribunal

```python
sigla              char(10)    PK    'TRF1', 'TRF3'
nome               char(200)         'Tribunal Regional Federal da 1ª Região'
sigla_djen         char(20)          parâmetro siglaTribunal da API DJEN
ativo              bool              liga/desliga ingestão
overlap_dias       int               sobreposição da ingestão diária (default 3)
data_inicio_disponivel  date NULL    descoberto via djen_descobrir_inicio
backfill_concluido_em   datetime NULL marca quando todos chunks chegaram em success
created_at         datetime
```

Seed automático via `0002_seed_tribunais` — TRF1-6 + TJSP cadastrados, só TRF1+TRF3 com `ativo=True`.

## Process

```python
id                 bigint        PK
numero_cnj         char(25)            CNJ formatado: NNNNNNN-DD.AAAA.J.TR.OOOO
tribunal           FK Tribunal         PROTECT
primeira_movimentacao_em  datetime NULL
ultima_movimentacao_em    datetime NULL
total_movimentacoes       int           default 0

# Enriquecimento (preenchido pelo enricher do tribunal)
classe_codigo            char(20)
classe_nome              char(255)
assunto_codigo           char(20)
assunto_nome             char(255)
data_autuacao            date  NULL
valor_causa              numeric(18,2) NULL
orgao_julgador_codigo    char(20)
orgao_julgador_nome      char(255)
juizo                    char(255)
segredo_justica          bool          default False
enriquecido_em           datetime NULL

inserido_em        datetime  auto_now_add
atualizado_em      datetime  auto_now

constraint:  unique(tribunal, numero_cnj)
indexes:     (tribunal, numero_cnj), (tribunal, -ultima_mov), inserido_em,
             enriquecido_em, classe_codigo, orgao_julgador_codigo
```

**Trigger**: `mov_update_process_agg` (statement-level AFTER INSERT em Movimentacao) recalcula `total_movimentacoes` + `primeira/ultima_movimentacao_em` em batch — 1 UPDATE por bulk_create.

## Movimentacao

```python
id                       bigint    PK
processo                 FK Process   CASCADE
tribunal                 FK Tribunal  PROTECT  (denormalizado pra queries quentes)
external_id              char(64)             id da DJEN (chave de dedupe)
data_disponibilizacao    datetime              data DJEN do item
inserido_em              datetime  auto_now_add (data de ingestão)

tipo_comunicacao         char(120)
tipo_documento           char(120)
nome_orgao               char(255)
id_orgao                 int  NULL
nome_classe              char(255)
codigo_classe            char(20)
link                     url(500)
destinatarios            jsonb                 [{nome:str, ...}]
destinatario_advogados   jsonb
texto                    text                   texto completo da publicação

numero_comunicacao       char(120)
hash                     char(128)
meio                     char(20)               'D'(Diário) etc.
meio_completo            char(120)              'Diário de Justiça Eletrônico Nacional'
status                   char(40)
ativo                    bool       default True
data_cancelamento        datetime  NULL
motivo_cancelamento      text

search_vector            tsvector NULL          atualizado por trigger SQL

constraint:  unique(tribunal, external_id)      idempotência da ingestão
indexes:     (processo, -data_disp),
             (tribunal, -data_disp),
             inserido_em, (tribunal, ativo), hash,
             search_vector (GIN), texto (GIN gin_trgm_ops)
```

**Trigger**: `mov_search_vector_trg` (BEFORE INSERT/UPDATE) constrói tsvector ponderado:
- A: tipo_comunicacao + nome_classe
- B: nome_orgao
- C: texto
- Config: `portuguese`, com `unaccent`

## IngestionRun

```python
id, started_at, finished_at NULL
tribunal             FK
status               char  'running'|'success'|'failed'
janela_inicio, janela_fim  date
paginas_lidas, movimentacoes_novas, movimentacoes_duplicadas, processos_novos  int
erros                jsonb [{erro, detalhe, pagina, ...}]

indexes: (tribunal, -started_at), (status, -started_at), (tribunal, janela_inicio, janela_fim)
```

Auditoria de cada janela de backfill ou ingestão diária.

## SchemaDriftAlert

```python
id, detectado_em, resolvido (bool), resolvido_em
tribunal             FK
tipo                 char  'extra_keys'|'missing_keys'|'type_mismatch'
chaves               jsonb  ['campo_novo_x', ...]
chaves_hash          char(64)  hash sha256 truncado pra constraint
exemplo              jsonb   1 item DJEN truncado (texto≤500 chars)
ingestion_run        FK NULL

constraint:  unique(tribunal, tipo, chaves_hash)  WHERE resolvido=False
indexes:     (resolvido, tribunal)
```

Permite múltiplos alertas abertos por tribunal+tipo se as **chaves** forem diferentes (não sobrescreve evidências).

## Parte

Pessoa física, jurídica ou advogado. **Entidade compartilhada entre processos**.

```python
id, primeira_aparicao_em, ultima_aparicao_em, total_processos
nome                 char(255)
documento            char(20)            CPF/CNPJ formatado
tipo_documento       char(10)            'CPF'|'CNPJ'|''
oab                  char(20)            'SP123456' — só advogados
tipo                 char(20)            'pf'|'pj'|'advogado'|'desconhecido'

constraint:  unique(documento)  WHERE documento != ''
constraint:  unique(oab)        WHERE oab != ''
indexes:     nome, documento, oab, tipo
```

**Dedupe**: chave natural é `documento` (CPF/CNPJ); fallback `oab` pra advogados sem documento; resto cria duplicata aceitável.

## ProcessoParte

```python
id, inserido_em
processo             FK Process    CASCADE
parte                FK Parte      PROTECT
polo                 char  'ativo'|'passivo'|'outros'
papel                char(120)     'AUTOR'|'EXEQUENTE'|'ADVOGADO' etc.
representa           FK self  NULL  advogado → ProcessoParte da pessoa representada

constraint:  unique(processo, parte, polo, papel)  WHERE representa IS NULL
indexes:     (parte, polo), (processo, polo), papel
```

Constraint **partial** porque advogado pode representar 2 réus distintos no mesmo processo — 2 rows válidas com mesmo `(processo, parte, polo, papel)` mas `representa` diferente. Constraint só dedupe entre principais (representa=NULL).

**Trigger**: `pp_total_ins` / `pp_total_del` — recalcula `Parte.total_processos` em INSERT/DELETE statement-level.

## ER (alto nível)

```
Tribunal 1 ──< Process ──< Movimentacao
                  │
                  └──< ProcessoParte >── Parte
                              │
                              └─ representa (self FK)

Tribunal 1 ──< IngestionRun
Tribunal 1 ──< SchemaDriftAlert
```

## Volume / espaço

Com cobertura ~60% TRF1+TRF3 (2020-12 → 2024-10 + 2026 parcial):

| Tabela | Total | Heap | Índices | TOAST |
|---|---|---|---|---|
| `movimentacao` | 9.1 GB | 1.5 GB | 2.7 GB | 4.8 GB |
| `process` | 354 MB | 116 MB | 237 MB | — |
| outras | <5 MB | | | |

~7.8 KB por movimentação total (índices + texto comprimido). Detalhe em [`OPS.md`](OPS.md#disco).
