# Modelo de dados

Entidades de domínio em `tribunals/models.py`. Convites em `accounts/models.py`. Migrations em `<app>/migrations/`.

## Apps

```
tribunals  Tribunal · Process · Movimentacao · IngestionRun · SchemaDriftAlert
           Parte · ProcessoParte · ClasseJudicial · Assunto
           ClassificadorVersao · ClassificacaoLog · ApiClient · LeadConsumption
           AmostraValidacao · AmostraProcesso · ProcessoValidacao
           ClassificacaoShadowLog · ThresholdTribunal
accounts   Invite
```

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
classe_codigo            char(20)              # legacy/cache
classe_nome              char(255)             # legacy/cache
classe                   FK ClasseJudicial NULL PROTECT      # canônico
assunto_codigo           char(20)              # legacy/cache
assunto_nome             char(255)             # legacy/cache
assunto                  FK Assunto NULL PROTECT             # canônico
data_autuacao            date  NULL
valor_causa              numeric(18,2) NULL
orgao_julgador_codigo    char(20)
orgao_julgador_nome      char(255)
juizo                    char(255)
grau                     char(4)               # G1|G2|JE|SUP (Datajud). '' = não sabemos
segredo_justica          bool NULL     default None   # NULL = NÃO PERGUNTAMOS
enriquecido_em           datetime NULL
enriquecimento_status    char(20)      pendente|ok|nao_encontrado|erro
enriquecimento_erro      text

inserido_em        datetime  auto_now_add
atualizado_em      datetime  auto_now

constraint:  unique(tribunal, numero_cnj)
indexes:     (tribunal, numero_cnj), (tribunal, -ultima_mov), inserido_em,
             enriquecido_em, classe_codigo, orgao_julgador_codigo
```

**⚠️ `proc_tribunal_id_idx` NÃO é o índice que o model declara.** `models.py` diz
`Index(fields=['tribunal', '-id'], name='proc_tribunal_id_idx')` — o que existe
no banco com esse nome é **`btree (tribunal_id)`, uma coluna só** (conferido em
`pg_indexes`, coluna a coluna, 25/08/2026). Mesmo nome, colunas diferentes: o
`makemigrations` não vê, porque compara com o ESTADO, não com o banco.

Consequência medida, e ela é cara: **não existe índice que sirva
"filtra por tribunal, ordena por `id`"**. O plano de
`WHERE tribunal_id='TJPR' AND id > X ORDER BY id LIMIT 500` é

```
Limit  (cost=0.57..2916.63 rows=500)
  ->  Index Scan using tribunals_process_pkey  (cost=0.57..39100789.77 rows=6704385)
        Index Cond: (id > 0)
        Filter: ((tribunal_id)::text = 'TJPR'::text)   ← varre a PK inteira filtrando
```

O `LIMIT` faz o custo *estimado* parecer 2.916; o real é a varredura da PK até
achar as linhas do tribunal. Em produção um `ORDER BY id ASC LIMIT 1` desses
ficou **1.318 s** antes de ser cancelado, e ficou na frente de um `ALTER TABLE`
— que, sendo ACCESS EXCLUSIVE, enfileirou 63 sessões atrás de si (auto-jam;
ver `OPS.md`). **Backfill que precisa varrer o acervo entra por faixa fechada
de `Process.id`** (`id >= X AND id < Y`, custo 60.209 para 50.000 ids), nunca
por `tribunal_id` + `ORDER BY id`. Ver
`tribunals/management/commands/backfill_partes_djen.py`.

`proc_enriq_id_idx` **está** correto no banco (`(enriquecimento_status, id DESC)`)
— o defeito é só do `proc_tribunal_id_idx`.

**Trigger `mov_update_process_agg`: declarado na migration 0004, AUSENTE do banco
de produção.** Conferido em `pg_trigger` em 24/08/2026: o único trigger vivo em
`tribunals_process` é `process_set_ano_cnj`; a função
`update_process_aggregates_stmt` existe, órfã (nenhum trigger a chama). Mesma
assinatura do trigger de `ano_cnj` que sumiu num restore — migration não recria
trigger que o banco perdeu, porque compara com o ESTADO, não com o banco.

Consequência: quem mantém `total_movimentacoes` + `primeira/ultima_movimentacao_em`
em produção é **`djen/ingestion.py::_flush_resumo`** (e `datajud/ingestion.py`
no caminho por processo). No banco de TESTE o trigger existe — as migrations
rodam — então setup de teste que grave esses campos direto no `create()` é
sobrescrito pelo trigger e prova o contrário do que pretende.

**`grau`** (migration 0052, 25/08/2026) — `G1`/`G2`/`JE`/`SUP`, do Datajud.
Vazio = não sabemos. Existe porque **`JE` (Juizado Especial) paga por RPV, não
por precatório**: na sonda ao vivo de 20 processos o campo veio em 20/20 dos
`_source` e **5 dos 20 eram `JE`** — e nós descartávamos. Sem ele o funil de
produto mistura dois produtos com prazos e preços diferentes.

**`segredo_justica` é TRI-STATE desde a migration 0052** (25/08/2026):
`NULL` = não perguntamos · `False` = perguntamos e a fonte disse que não ·
`True` = corre em segredo. O `default=False` anterior era uma **afirmação que
ninguém tinha feito**: a auditoria mediu `true` em **0 de 91.638.494**
documentos do índice (`_count`, nunca `exists`) e 0 em 120.000 processos
amostrados no banco, enquanto o e-SAJ devolve a página "informe a senha…
segredo de justiça" em **10 de 11** sondas ao vivo de processos TJSP marcados
`ok` **sem nenhuma parte** (≈ 302 k processos). ⚠️ As linhas anteriores à 0052
continuam `False` — o migration **não** reescreve 102 M de linhas —, então
`False` em processo nunca enriquecido ainda significa "não perguntamos".

**`data_enriquecimento_djen`** significa, desde 24/08/2026, "última vez que o
DJEN trouxe movimentação **NOVA** para este processo". Antes era "última vez que
o DJEN passou por aqui", e renová-la a cada passada era o motivo de reescrever
70,3% das linhas com valor idêntico (ver `INGESTION.md`, "O deadlock em
`tribunals_process`").

### ⚠️ Índice declarado ≠ índice real: confira por COLUNA, nunca por nome

`proc_tribunal_id_idx` é o caso que obriga esta régua a existir:

```
model:  Index(fields=['tribunal', '-id'], name='proc_tribunal_id_idx')
banco:  proc_tribunal_id_idx  ->  ['tribunal_id']      (uma coluna só)
```

**Mesmo nome, conteúdo diferente.** Os três índices fantasma da migration
`0051` eram *ausentes* — conferir por nome ao menos respondia "não existe".
Este **existe, com o nome certo e as colunas erradas**, então a verificação que
todo mundo faz (`\di`, `pg_indexes.indexname`, "o índice está lá?") devolve
**ok**. `makemigrations` também não vê: ele compara com o ESTADO, não com o
banco.

**O que isso custou, medido em 25/08/2026:** sem o `id` no índice,
`WHERE tribunal_id='TJPR' ORDER BY id LIMIT 1` não lê a primeira entrada — cai
em `Index Scan using tribunals_process_pkey … Filter: tribunal_id` (custo
39.100.789) e **varre e ordena**. Um `LIMIT 1` desses ficou **1.318 s** em
produção e enfileirou **63 sessões** atrás de um `ALTER TABLE` que esperava
ACCESS EXCLUSIVE. Um índice errado não deixa a query lenta: deixa o banco
parado.

**A régua**, e ela não é opcional:

```sql
SELECT c.relname AS indice,
       array_agg(a.attname ORDER BY k.ord) AS colunas
FROM pg_index i
JOIN pg_class c  ON c.oid = i.indexrelid
JOIN pg_class t  ON t.oid = i.indrelid
JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE t.relname = 'tribunals_process'
GROUP BY c.relname ORDER BY c.relname;
```

Consequência prática para quem varre o acervo: **entre por faixa fechada de
`Process.id`**, não por `--tribunal`. Faixa de pk usa a PK (`Index Cond: id >= X
AND id < Y`, custo 60.209 para 50.000 ids) e ainda é o que torna o reindex do
ES viável depois, porque o bloco é contíguo.

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

search_vector            tsvector NULL          ⚠️ MORTA — ver abaixo

constraint:  unique(tribunal, external_id)      idempotência da ingestão
indexes:     mov_processo_data_disp_idx   (processo, -data_disp)
             mov_tribunal_data_disp_idx   (tribunal, -data_disp)
             mov_tribunal_ativo_idx       (tribunal, ativo)
             mov_inserido_tribunal_idx    (inserido_em, tribunal)
             mov_data_disp_btree          (data_disp)
             mov_ativo_idx                (ativo)
             mov_cancelados_partial       parcial
             uniq_mov_tribunal_extid      (tribunal, external_id)
```

**Não existe índice em `texto`, `search_vector` nem `hash`** — a lista acima é o
`pg_index` de 20/08/2026, conferido coluna a coluna, e são os 8 índices + a PK
que a tabela tem.

Esta seção afirmava o contrário até 20/08/2026: listava `hash`, `search_vector
(GIN)` e `texto (GIN gin_trgm_ops)`, e descrevia um trigger
`mov_search_vector_trg` que **nunca existiu no banco**. O model declarava os três
índices, também sem que existissem. Foi essa documentação que dois trechos de
código independentes leram e repetiram como certeza:

| onde | o que afirmava | o que era |
|---|---|---|
| `api/filters.py` | "ILIKE %x% usa o índice GIN trigram" | Seq Scan, custo 111.195.298, 1,39B linhas / 815 GB |
| `diarios/base.py` | `hash` "já existe e já é indexada" | custo 73.427.276 por lote, no caminho de ESCRITA |

E a `search_vector` é pior que um índice faltando: a coluna existe, **não há
trigger que a preencha** e ela está NULL em 99,8% da tabela — cheia só até
`id≈4.876.372` (13/03/2024), 2.753.688 linhas de 1.385.659.648. A busca da API
com 3+ palavras usava `SearchRank` sobre ela: varria 0,2% do acervo e devolvia
"encontrei isto", sem uma palavra sobre os outros 99,8%.

**Busca textual é no Elasticsearch** (`search/busca_api.ids_por_texto` →
`voyager-movimentacoes-v2`), que é onde o índice existe de verdade e que desde
18/08 tem o acervo inteiro. Ver ADR-032 e migration `0051_indices_fantasma`.

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
documento            char(20)            CPF/CNPJ formatado (real OU mascarado)
tipo_documento       char(10)            'CPF'|'CNPJ'|''
oab                  char(20)            'SP123456' — só advogados. FORMA CANÔNICA:
                                         UF + número SEM zero à esquerda (+ letra
                                         de sufixo). Ver §OAB: a forma canônica.
tipo                 char(20)            'pf'|'pj'|'advogado'|'desconhecido'

constraint:  unique(documento)         WHERE doc != '' AND NOT LIKE '%X%' AND NOT LIKE '%*%'
                                         (uniq_parte_documento_real)
constraint:  unique(nome, documento)   WHERE doc LIKE '%X%' OR LIKE '%*%'
                                         (uniq_parte_documento_mascarado)
constraint:  unique(oab)               WHERE oab != ''
indexes:     nome, documento, oab, tipo
```

**Dedupe** em 3 caminhos (`enrichers/pje.py::_upsert_parte`):

1. **OAB** (advogados): chave estável e única — precedência.
2. **Doc real** (CPF/CNPJ sem máscara): PK natural global.
3. **Doc mascarado** (TRF3 esconde dígitos como `639.XXX.XXX-XX`):
   - Antes de criar, busca Parte com mesmo nome e doc REAL que case com a máscara via `real_casa_com_mascara` → reusa (mesma entidade vista em TRF1 com doc completo).
   - Senão, dedupe por `(nome, documento)`.
4. **Sem doc nem OAB**: `get_or_create((nome, tipo))` — evita explosão de PJ pública (Procuradoria, Defensoria, etc.).

Detalhes em [`ENRICHMENT.md`](ENRICHMENT.md#dedupe-de-partes-_upsert_parte).

### OAB: a forma canônica (31/08/2026, #96)

`Parte.oab` é **`UF` + número sem zero à esquerda + (opcional) UMA letra de
sufixo**: `PE475`, `AL10715A`. Uma única função escreve essa forma —
`tribunals/services/oab.py::canonizar_oab` — e as **duas** portas de escrita a
chamam:

| porta | lê de | antes de 31/08 |
|---|---|---|
| `enrichers.parsers.parse_oab` | TEXTO da publicação (`"OAB PA 015237"`) | **preservava** o zero |
| `tribunals.services.partes_djen.formatar_oab` | JSON do DJEN (`numero_oab`) | remove o zero desde `55264d3` |

Enquanto foram duas implementações, a mesma inscrição virava **duas** `Parte`,
porque `uniq_parte_oab` é sobre a string. Medido em produção em 31/08/2026:

| medida | consulta | número |
|---|---|---|
| `Parte` com `oab <> ''` | `count(*) FILTER (WHERE oab <> '')` | 943.510 |
| na régua (`^[A-Z]{2}[0-9]+[A-Z]?$`) | idem | 942.086 |
| fora da régua — **abstém** | `oab !~ '^[A-Z]{2}[0-9]+[A-Z]?$'` | 1.427 (1.424 começam com `MT`) |
| grupos em colisão por zero à esquerda | `GROUP BY canon HAVING count(DISTINCT oab) > 1` | 19.481 |
| **linhas a colapsar (teto)** | `sum(n-1)` | **19.493** |
| grupos em colisão **sem** nenhuma forma zero-padded | controle | **0** ⇒ o zero explica 100% |
| linhas zero-padded no total | `dig ~ '^0'` | 29.979 |

O teto **13.045** que estava registrado no `PLANO_ACERVO.md` não bate com
nenhuma data desta régua: reconstruída por `primeira_aparicao_em`, ela vale
**1.134 em 25/08** e **19.344 em 26/08** — o salto é o `backfill_partes_djen`
(`fonte='djen'`, migration 0052) encontrando as linhas zero-padded que o
enricher já tinha criado. Não há data em que a régua passe por 13.045.

**Regra de fusão** (`manage.py dedup_partes --group oab_zero`) — três guardas,
cada uma com número:

1. **UF na chave.** `canon` começa pela UF gravada ⇒ `SP475` e `PE475` nunca
   caem no mesmo grupo. Controle: 19.481 de 19.481 grupos (100%) com UMA UF.
2. **Nome idêntico** (caixa/acento/pontuação normalizados) — 991 grupos
   **abstêm**. O par que motivou a guarda é real: `PE00475` =
   `TANEY QUEIROZ E FARIAS`, `PE475` = `LUZIA HELENA DE VALOIS CORREIA`.
3. **CPF real não divergente** — 57 grupos têm dois CPF reais diferentes;
   1 só é pego aqui (os outros 56 já caem na guarda 2). Nenhum grupo tem o
   MESMO CPF dos dois lados: aqui o CPF **nega** identidade, nunca a prova.

⇒ funde **18.489 grupos / 18.501 linhas**; **abstém em 992** (991 por nome
+ 1 por CPF). Confere: 18.501 + 992 = 19.493.

`SP000` (inscrição só de zeros) **abstém**: `ltrim` devolveria `SP0` e duas
formas de lixo virariam uma chave. São 5 linhas na régua, 1 par —
`MT0`/`MT00000` — e é a diferença entre 19.494 e 19.493.

**Além da fusão**, o comando reescreve a grafia (não funde) de duas
populações, senão a porta de escrita canônica recria a duplicata no
enriquecimento seguinte:

- o *survivor* de grupo cujas duas formas eram zero-padded (`PE0475`+`PE00475`);
- as zero-padded **solitárias** (sem gêmea canônica), **10.485** linhas —
  `--sem-normalizar-solitarias` desliga.

**O que a fusão NÃO faz:** não reindexa o Elasticsearch. `participacoes.oab` é
gravado no doc do processo, então o índice guarda a grafia antiga até o
processo ser reindexado por outra via. Medido em 60 pares (amostra
`md5(canon||'sal96')`, 0 erro de ES, 0 par ausente do índice — controle 100%):
**17 pares (28,3%) têm as DUAS grafias indexadas**, e 1.533 dos 13.275
processos do recorte ficam invisíveis a uma busca pela outra grafia.

## ProcessoParte

```python
id, inserido_em
processo             FK Process    CASCADE
parte                FK Parte      PROTECT
polo                 char  'ativo'|'passivo'|'outros'
papel                char(120)     'AUTOR'|'EXEQUENTE'|'ADVOGADO' etc.
representa           FK self  NULL  advogado → ProcessoParte da pessoa representada
fonte                char(16) NULL   NULL = legado/enricher · 'djen' = destinatário da publicação

constraint:  unique(processo, parte, polo, papel)  WHERE representa IS NULL
indexes:     (parte, polo), (processo, polo), papel
```

**`fonte`** (migration 0052, 25/08/2026) — a DJEN entrega destinatário + polo +
advogado + OAB em **toda** comunicação, e isso nunca virou entidade: 84,8% do
acervo (9.467 de 11.160 na amostra, ≈ 86,7 M processos) tem parte no JSONB de
`Movimentacao` e **nenhuma** `ProcessoParte`. A coluna separa as duas
procedências, e isso importa porque elas **não são equivalentes**: o
destinatário é quem foi intimado *naquela comunicação*, sem CPF/CNPJ e sem
vínculo advogado→representado (`representa` fica NULL). Cobertura ampla e rasa;
o enricher é estreita e profunda. Serve também ao rollback por faixa
(`DELETE ... WHERE fonte='djen'`) e à medição do antes/depois.

Constraint **partial** porque advogado pode representar 2 réus distintos no mesmo processo — 2 rows válidas com mesmo `(processo, parte, polo, papel)` mas `representa` diferente. Constraint só dedupe entre principais (representa=NULL).

**Trigger `pp_total_ins` / `pp_total_del`: declarado na migration 0005, AUSENTE
do banco de produção.** Conferido em `pg_trigger` em 25/08/2026 — o único
trigger não-interno em `tribunals_process*`/`tribunals_parte` é
`process_set_ano_cnj`. É o **terceiro** caso da mesma família nesta semana
(os 3 índices fantasma da migration 0051, o `mov_update_process_agg` acima):
migration compara com o ESTADO, não com o banco, então trigger perdido num
restore nunca é recriado e a declaração vira documentação falsa.

Duas consequências práticas:

  · `Parte.total_processos` **não é mantido em produção** — bate com a medição
    independente de `search/entidades.py`, que achou o campo preenchido em só
    **39,3%** das linhas. Quem mantém as agregações da tela é
    `manage.py rebuild_parte_bridges` (cron diário) sobre `ParteTribunal`/
    `PartePapel`, não este trigger.
  · **No banco de TESTE o trigger existe** (as migrations rodam). Escrita em
    massa de `ProcessoParte` custa, no teste, um recálculo statement-level que
    em produção **não acontece** — quem dimensionar backfill pelo custo medido
    no teste vai errar para o lado errado, e quem assertar `total_processos`
    num teste está provando o contrário do que produção faz.

## ER (alto nível)

```
Tribunal 1 ──< Process ──< Movimentacao
                  │
                  └──< ProcessoParte >── Parte
                              │
                              └─ representa (self FK)

Tribunal 1 ──< IngestionRun
Tribunal 1 ──< SchemaDriftAlert

ClasseJudicial 1 ──< Process
ClasseJudicial 1 ──< Movimentacao
Assunto        1 ──< Process

User           1 ──< Invite (created_by)
User           1 ──< Invite (used_by, OneToOne)
```

## ClasseJudicial / Assunto

Catálogos nacionais (TPU/CNJ). PK = código TPU (`char(20)`).

```python
codigo            char(20)    PK       '1116', '436', etc.
nome              char(255)            'EXECUÇÃO FISCAL', 'PROCEDIMENTO DO JEC' etc.
total_processos   int  default 0       atualizado por migration / refresh manual
indexes:          nome
```

Padronizados pelo CNJ — uma única tabela serve TRF1, TRF3 e qualquer outro tribunal. Resolve discrepância entre PJe (UPPERCASE limpo) e DJEN (CamelCase com acentos quebrados) usando o nome do PJe como canônico (priorizado em `0010_populate_classe_assunto`).

`enrichers/pje.py::_upsert_catalogo` é race-safe via `bulk_create(ignore_conflicts) + get(codigo=)`.

## Invite (`accounts/models.py`)

Convites de cadastro — uso único, validade 7 dias. Token + classificação ip-api do IP que aceitou. Detalhes em [`ACCOUNTS.md`](ACCOUNTS.md).

## Classificação de leads (`tribunals/models.py`)

Sistema de ML pra classificar processos como Precatório / Pré-precatório / Direito Creditório. Detalhe completo em [`CLASSIFICACAO.md`](CLASSIFICACAO.md).

**Process** ganhou 4 campos (migration 0020):
- `classificacao` (CharField indexed, choices PRECATORIO/PRE_PRECATORIO/DIREITO_CREDITORIO/NAO_LEAD)
- `classificacao_score` (FloatField 0..1)
- `classificacao_versao` (CharField, ex: 'v5')
- `classificacao_em` (DateTimeField indexed)

**ClassificadorVersao** — versões treinadas:
- `versao` (unique), `pesos` (JSON dict), `metricas` (JSON), `ativa` (bool)
- `shadow` (bool, indexed) — N versões podem rodar em paralelo registrando `ClassificacaoShadowLog` sem afetar Process. Constraint partial é só sobre `ativa=True` (1 ativa por vez)

**ClassificacaoLog** — histórico de transições (N3→N2→N1):
- `processo` (FK), `classificacao`, `score`, `versao`, `features_snapshot` (JSON), `criada_em`
- Criado apenas quando categoria MUDA

**ApiClient** — credenciais pra integração externa:
- `nome` (unique, ex: 'juriscope'), `api_key` (unique indexed), `ativo`
- Key gerada via Django admin (`secrets.token_urlsafe(32)`)

**LeadConsumption** — registros de consumo via API:
- `processo` (FK), `cliente` (FK), `consumido_em` (auto), `resultado` (choices)
- Resultados: validado/sem_expedicao/erro/pendente/pago/arquivado/cedido
- **Sem unique constraint** — re-consumo permitido (cada chamada = registro novo)
- Indexes: `(cliente, -consumido_em)`, `(cliente, processo)`

**Auxiliar** (não-Django, criada via SQL na migração de seed):
- `lead_trf1 (process_id BIGINT PK, numero_cnj, criado_em)` — ground truth dos 396k leads TRF1 confirmados pelo Juriscope
- `lead_trf3 (...)` — análoga (atualmente 347 amostras)

## Validação humana + shadow + thresholds (`tribunals/models.py`)

Pipeline de revisão manual sobre saída do classificador (T4-T22). Detalhes de regra
em [`REGRAS_NEGOCIO_VALIDACAO.md`](REGRAS_NEGOCIO_VALIDACAO.md) e ADRs 018-022.

### AmostraValidacao

Lote sorteado por estratégia. Seed persistida para reprodutibilidade.

```python
id                 bigint    PK
estrategia         char(30)  choices: top_score | borderline | low_score |
                              falsos_consumidos | recuperados | on_demand |
                              fn_candidatos | shadow_disagree
tribunal           FK Tribunal  NULL PROTECT  (null = lote multi-tribunal)
versao_modelo      char(10)              snapshot 'v6'
criada_por         FK User      NULL SET_NULL
criada_em          datetime  auto_now_add
parametros         jsonb                 configs do sorteio
tamanho_alvo       int
seed               bigint                (auditável; regenerável)
processos          M2M Process through=AmostraProcesso

indexes:  (-criada_em), estrategia
ordering: -criada_em
```

### AmostraProcesso (through M2M)

```python
amostra                    FK AmostraValidacao  CASCADE
processo                   FK Process           CASCADE
ordem                      int                  ordem no lote
score_no_sorteio           float
classificacao_no_sorteio   char(20)
suspeita_score             float  NULL          score do mining (FN/shadow) — NULL se sorteio aleatório
motivos_suspeita           jsonb  NULL          lista de E1..E6 ou outras tags

constraint:  unique(amostra, processo)
indexes:     (amostra, ordem)
ordering:    amostra, ordem
```

### ProcessoValidacao

Decisão humana **imutável**. Append-only via constraint. LGPD/anonimização
fora de escopo desta versão (campo `usuario_hash` no schema, não populado).

```python
processo                   FK Process           CASCADE
amostra                    FK AmostraValidacao  NULL SET_NULL
usuario                    FK User              NULL SET_NULL  (SET_NULL cobre delete admin)
usuario_hash               char(64)                            (reservado, não populado)

resultado                  char(20)  choices: eh_lead | eh_precatorio | eh_pre |
                                              eh_dc | nao_lead | incerto |
                                              precisa_enriquecer | skip
confianca                  char(10)  choices: alta | media | baixa (default alta)
motivo                     text                 confidencial intra-equipe
tempo_segundos             int  NULL

# Snapshot do estado do modelo no momento da anotação
versao_modelo              char(10)
classificacao_no_momento   char(20)
score_no_momento           float
features_snapshot          jsonb

criada_em                  datetime  auto_now_add

# Resolução de divergência em dupla-anotação (10% dos lotes)
label_final                char(20)  NULL  preenchido por revisor sênior
label_final_resolvido_por  FK User  NULL SET_NULL
label_final_resolvido_em   datetime NULL

constraint:  unique(processo, usuario)  ← impede re-anotação
indexes:     (processo, criada_em), (usuario, criada_em), resultado, amostra,
             label_final

permissions (Meta.permissions, custom):
  can_validate_lead              — anotar validações
  can_publish_model              — promover ClassificadorVersao + editar ThresholdTribunal
  can_view_validacao_dashboard   — acessar /dashboard/leads/visibilidade/ e /validacao/
  can_resolve_disagreement       — preencher label_final
  can_view_motivo                — ler motivo de outros anotadores
```

Helper `motivo_visivel_para(user)` (e templatetag `{% motivo_visivel %}`) faz o gating
de `motivo`: autor sempre vê o próprio; outros precisam de `can_view_motivo`.

**Follow-up:** trigger Postgres UPDATE-block (hoje a imutabilidade é garantida só
pela `UniqueConstraint`).

### ClassificacaoShadowLog

Predições de versões `shadow=True` em paralelo à versão `ativa=True`. Não afetam
`Process.classificacao` — comparação A/B via job `comparar_shadow` (cron 04:00).

```python
processo         FK Process       CASCADE
versao_shadow    char(10)
score            float
categoria        char(20)         (mesma choices de Process.classificacao)
criada_em        datetime  auto_now_add  db_index=True

indexes: (versao_shadow, criada_em)  → shadow_log_versao_em_idx
```

Retention 90 dias (cleanup job é follow-up).

### ThresholdTribunal

Thresholds N1/N2/N3 por (tribunal × versao_modelo). DB-driven com fallback aos
defaults hardcoded em código.

```python
tribunal               FK Tribunal      CASCADE  related_name='thresholds'
versao_modelo          char(10)
threshold_precatorio   float
threshold_pre          float
threshold_dc           float
ativo                  bool             default True
atualizado_por         FK User          NULL SET_NULL
atualizado_em          datetime  auto_now

constraint:  unique(tribunal, versao_modelo)
constraint:  unique(tribunal, versao_modelo) WHERE ativo=True   ← partial
```

Lido por `_categorizar(score, features, tribunal_id, versao_modelo)` — usado
tanto pelo path ativo quanto pelo shadow (ADR-022).

### ER (validação humana)

```
Tribunal 1 ──< AmostraValidacao ──< AmostraProcesso >── Process
                                                          │
                                                          └──< ProcessoValidacao
User 1 ──< AmostraValidacao (criada_por)                       │
User 1 ──< ProcessoValidacao (usuario, autor)                  │
User 1 ──< ProcessoValidacao (label_final_resolvido_por)       │
                                                               │
Process ──< ClassificacaoShadowLog                             │
                                                               │
Tribunal 1 ──< ThresholdTribunal  (versionado por versao_modelo)
```

## FonteDiario

Mapeamento `source_id` (Jusbrasil/Digesto) → `Tribunal`. Granularidade: 1 por
tribunal (sem comarca). TJMG usa 1 source_id representativo.

```python
source_id      int          PK    1, 59, 60, ...
tribunal        FK Tribunal  OneToOne
diario_slug     char(64)           'dje-trf1', 'comunica', ...
orgao_slug      char(64)           'trf1', 'tjsp', ...
caderno_slug    char(64)           '' ou 'cad.2-2-inst'
nome            char(200)          'TRF - 1ª Reg.'
```

## Movimentacao.assunto_norm

Classificação normalizada Jusbrasil/Digesto: lista de tuplas `[tipo, subtipo]`.
Populado por classificador heurístico (`tribunals/tipos_norm.py`). `[]` quando
incerto (compat spec: "lista vazia quando não disponível").

## ApiClient.mcp_token

UUID opcional pra auth no MCP server. Gerado via `python manage.py gerar_mcp_token <nome>`.

## PdfArquivo (`pdf_storage/models.py`)

PDF de uma `Movimentacao` baixado e armazenado no MinIO.

```python
movimentacao    FK Movimentacao  OneToOne CASCADE
arquivo         FileField         storage=MinIO
tamanho_bytes   bigint
hash_sha256     char(64)
baixado_em      datetime
status          char(10)          pendente|ok|erro
erro            text
tentativas      smallint
```

## Monitoramento (`monitoring/models.py`)

```python
MonitoredTerm    term, source_ids[], is_active, created_at
MonitoredPerson  nome, documento, oab, tribunais[], is_monitored_diario, is_advogado, is_active
MonitoredProcess cnj, tribunais[], is_active
Detection        target_type (term|person|proc), target_id, movimentacao FK,
                 snippet, detected_at, entregue_em, erro_entrega
                 constraint: unique(target_type, target_id, movimentacao)
WebhookConfig   cliente FK ApiClient, url, secret, evento_types[], is_active
```

## Volume / espaço

Com cobertura ~60% TRF1+TRF3 (2020-12 → 2024-10 + 2026 parcial):

| Tabela | Total | Heap | Índices | TOAST |
|---|---|---|---|---|
| `movimentacao` | 9.1 GB | 1.5 GB | 2.7 GB | 4.8 GB |
| `process` | 354 MB | 116 MB | 237 MB | — |
| outras | <5 MB | | | |

~7.8 KB por movimentação total (índices + texto comprimido). Detalhe em [`OPS.md`](OPS.md#disco).
