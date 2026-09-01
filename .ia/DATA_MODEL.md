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
classe                   FK ClasseJudicial NULL PROTECT      # espelho do código
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

**⚠️ `classe`/`assunto` são o ESPELHO de `classe_codigo`/`assunto_codigo`, não
um segundo fato.** A FK só normaliza a string para dar filtro e join; quem
manda é a coluna de código. Em 31/08/2026, **8.054.334** processos tinham
código e FK NULL, e a FK **não existe no banco** (`pg_constraint` devolve zero
constraints do tipo `f` em `tribunals_process`) — ver a seção
[ClasseJudicial / Assunto](#classejudicial--assunto).

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

> **Resolvido no ESTADO em 01/09/2026 (#111).** A declaração passou a dizer o
> que o banco tem — `models.Index(fields=['tribunal'], name='proc_tribunal_id_idx')`,
> uma coluna — pela migration `0056_schema_real`, que é
> `SeparateDatabaseAndState` com `database_operations=[]`: **nenhum** `DROP`/
> `CREATE` roda no banco (um `DROP INDEX` não-concorrente em 104 M de linhas é
> o incidente de 25/08). O índice de duas colunas continua **não existindo**, e
> continua não sendo necessário: os backfills entram por faixa fechada de pk.
> Quem um dia precisar de `(tribunal, -id)` cria um índice **novo**, com nome
> novo. `proc_tribunal_id_idx` recebe 15,5 M de scans como está.

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

**Não existe índice em `texto`, `search_vector`, `hash` nem `classe_id`** — a
lista acima é o `pg_index` de 20/08/2026, reconferido coluna a coluna em
01/09/2026, e são os 8 índices + a PK que a tabela tem.

⚠️ `mov_ativo_idx` está **`indisvalid = false`** (8 kB, 0 scans): sobra de um
`CREATE INDEX CONCURRENTLY` que morreu. O planner não o usa; a escrita mantém.

⚠️ O índice em `classe_id` era **declarado** no model (`Index(fields=['classe'])`
mais o índice implícito da FK) e nunca existiu. A declaração foi removida em
01/09/2026 e o índice **não** foi criado — a decisão está medida em
[Inventário: declarado × banco](#inventário-declarado--banco-01092026-111).

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

⚠️ **Até 01/09/2026 nada disso existia no banco.** A tabela tinha **só a PK** —
nenhuma FK, nenhum índice e, principalmente, nenhuma unique parcial: a garantia
de "não sobrescreve evidências" era do model, não do Postgres. Criados em
01/09/2026 (#111), sem conflito — 0 duplicatas em 15 linhas. Ver
[Inventário: declarado × banco](#inventário-declarado--banco-01092026-111).

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

   **Em quantos casos a UF falta?** Medido em 943.619 linhas com OAB:

   | | linhas | % |
   |---|---:|---:|
   | sem prefixo de 2 letras (`MS`, `SP0000000A` truncado…) | 369 | 0,039% |
   | prefixo de 2 letras que **não é UF brasileira** | **0** | 0% |
   | DUAS UFs na string (`MT10079GO` — prefixo é o tribunal) | 803 | 0,085% |
   | **UF não provável (união)** | **1.172** | **0,124%** |
   | fora da régua `^[A-Z]{2}[0-9]+[A-Z]?$` — **todas abstêm** | 1.427 | 0,151% |
   | **na régua com UF inválida** | **0** | — controle |

   A régua é mais estreita que o problema de propósito: as 1.427 que ela recusa
   contêm as 1.172 em que a UF não se prova, e **nenhuma** linha entra na fusão
   com UF que não seja uma seccional real. Sem UF não há como provar a pessoa —
   e a resposta é abster, não fundir.
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

### Aplicado em produção — 31/08/2026

`manage.py dedup_partes --group oab_zero --batch-size 100000000`, 8 lotes,
**894 s** de fusão + 25 s de normalização.

| | ANTES | DEPOIS |
|---|---:|---:|
| `Parte` (total) | 21.825.998 | 21.817.013 |
| … com `oab <> ''` | 943.580 | **925.148** |
| … na régua | 942.153 | 923.721 |
| … com zero à esquerda | **29.983** | **999** |
| **advogados distintos (OAB canônica)** | **922.659** | **922.727** | 

O último número é o **controle**: a fusão colapsa GRAFIAS, não entidades — se
ela tivesse apagado advogado, ele cairia. Subiu 68 (inscrições novas chegando
pela ingestão durante a execução), nunca desceu.

Fechamento da conta, do log do comando: régua 942.202 (controle `canon` 100%),
teto 19.498 linhas, **18.505 fundidas**, 1 survivor reescrito, **10.479 de
10.480** solitárias normalizadas (a que faltou é o guard `NOT EXISTS`
funcionando: a forma canônica passou a existir durante a execução).

Teto **depois**: 994 linhas em 994 grupos — 992 abstidas por nome, 1 por CPF,
1 nascida durante a execução. Ou seja o que sobrou é **exatamente o que a regra
recusou**, não resíduo.

**Controle negativo, conferido no banco depois da fusão:** `PE00475`
(TANEY QUEIROZ E FARIAS) e `PE475` (LUZIA HELENA DE VALOIS CORREIA) seguem
duas linhas; e o número 475 existe em `AP`, `BA`, `PE` e `RR` — quatro
advogados diferentes, nenhum fundido.

**Pontes órfãs depois da fusão: 0** em `tribunals_partetribunal` e
`tribunals_partepapel`.

**A porta de escrita foi fechada junto**: os 16 serviços de enricher da `.102`
foram reiniciados entre 14:49:03Z e 14:51:45Z com `parse_oab` canônico. Antes
do restart nasciam **~7 `Parte` zero-padded por hora**; depois, em 31 min,
**0 zero-padded** e **55 canônicas** (o controle positivo — a porta está
escrevendo, só não escreve mais a grafia errada).

**O que a fusão NÃO faz:** não reindexa o Elasticsearch. `participacoes.oab` é
gravado no doc do processo, então o índice guarda a grafia antiga até o
processo ser reindexado por outra via. Medido em 60 pares (amostra
`md5(canon||'sal96')`, 0 erro de ES, 0 par ausente do índice — controle 100%):
**17 pares (28,3%) têm as DUAS grafias indexadas**, e 1.533 dos 13.275
processos do recorte ficam invisíveis a uma busca pela outra grafia. No índice
inteiro, **363.696 processos** carregam ao menos uma `participacoes.oab`
zero-padded, e a fusão tocou **211.337** deles (224.839 `ProcessoParte`
repointadas, de 5.956 losers que tinham alguma — os outros 12.545 eram órfãos
com ZERO `ProcessoParte`). Reindexá-los é dívida em aberto:
`reindexar_processos` ainda não aceita lista de ids.

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

### ⚠️ Não existe FK no BANCO — a FK do model é só do Django (31/08/2026)

> **Atualizado em 01/09/2026 (#111).** As 3 FKs de `tribunals_process` eram a
> ponta: o inventário completo achou **23 objetos declarados e ausentes**, e
> **5 FKs + 7 índices + 1 unique** já foram criados. Ver
> [Inventário: declarado × banco](#inventário-declarado--banco-01092026-111).

```sql
SELECT conname, contype FROM pg_constraint
 WHERE conrelid = 'tribunals_process'::regclass AND contype = 'f';
-- (0 rows)
```

`tribunals_process`, `tribunals_movimentacao`, `tribunals_parte`,
`tribunals_processoparte`, `tribunals_ingestionrun` e `tribunals_tribunal` têm
**zero** constraints do tipo `f`. As tabelas pequenas (`processovalidacao`,
`leadconsumption`, `amostraprocesso`…) têm as suas. Conferido em `pg_constraint`
tabela a tabela.

> ⚠️ `tribunals_parte` e `tribunals_tribunal` estão nessa lista por engano de
> leitura: elas não têm nenhum campo FK, então "zero constraints `f`" ali é o
> valor **correto**, não um achado. As tabelas com FK declarada e ausente eram
> seis: `tribunals_process`, `tribunals_movimentacao`, `tribunals_processoparte`,
> `tribunals_ingestionrun`, `tribunals_schemadriftalert` e `accounts_invite`.
> Contar tabela sem FK declarada como "faltando FK" infla o achado — a régua tem
> que comparar com o que o **estado declara**, não com uma expectativa.

Consequência prática, e ela morde: o Postgres aceita
`Process.classe_id = '99999'` sem linha em `tribunals_classejudicial`. Nada
falha na escrita — o estouro aparece depois, em produção, no `proc.classe` do
ORM. **Todo backfill que fecha FK tem que conferir o catálogo antes de ligar**
(`repop_classe_assunto` faz isso e abstém quando não acha; ver abaixo).

Como a FK é do model e não do banco, a inversa também vale: o
`on_delete=PROTECT` do Django não protege um `DELETE` feito em SQL.

#### Por que ela não existe: a migration foi aplicada, o objeto não nasceu

`0009_classe_assunto` está em `django_migrations` (id 33, aplicada em
2026-04-26 20:25) e declara os três `AddField` de ForeignKey
(`process.classe`, `process.assunto`, `movimentacao.classe`) mais os três
`AddIndex` correspondentes. Nenhum dos seis objetos existe no banco com o nome
que a migration deu:

| declarado pela migration | existe no banco? | o que existe |
|---|---|---|
| FK `process.classe_id` → `classejudicial` | **não** | — |
| FK `process.assunto_id` → `assunto` | **não** | — |
| FK `movimentacao.classe_id` → `classejudicial` | **não** | — |
| índice `tribunals_p_classe__05f562_idx` | não | `proc_classe_id_idx` (mesma coluna) |
| índice `tribunals_p_assunto_da9b3f_idx` | não | `proc_assunto_id_idx` (mesma coluna) |
| índice `tribunals_m_classe__1df891_idx` | **não** | **nada** — `tribunals_movimentacao` não tem NENHUM índice em `classe_id` |

Índice com nome próprio (`proc_*_idx`) no lugar do nome gerado é a assinatura de
DDL feito à mão e migration marcada como aplicada depois. É o mesmo padrão do
incidente "índices declarados e ausentes": `makemigrations` compara o model com
o **estado** das migrations, nunca com o banco, então a ficção não gera diff e
sobrevive indefinidamente. Conferir por **coluna** em `pg_index`/`pg_constraint`,
nunca por nome.

O caso da `tribunals_movimentacao` é pior que o do `Process`, porque lá não há
nem o índice equivalente: filtrar movimentação por classe é Seq Scan em
**1.888 GB**. Não é problema do #104 — a amostra de 510.636 movimentações em 11
janelas não achou uma linha sequer com `classe_id` NULL —, mas é achado e fica
registrado.

#### Deve ser criada? Sim — e hoje ela PASSARIA. Falta a janela.

Medido em 31/08/2026, varrendo as 104.003.151 linhas (65 s):

| | linhas |
|---|---:|
| `classe_id` preenchido sem linha no catálogo | **0** |
| `assunto_id` preenchido sem linha no catálogo | **0** |

E refeito às 22:05, **depois** de o backfill fechar 8,17 M de `classe_id` e
9,77 M de `assunto_id` (14.593.105 linhas com `assunto_id` ao fim): continua
**0 e 0**. O reparo em massa não
criou um único vínculo pendurado — que é exatamente o que a guarda de catálogo
existe para garantir.

Ou seja: a integridade que o Postgres **não** garante está, hoje, de fato
íntegra — pelo caminho de escrita, não pelo banco. Quem a segura é a guarda de
catálogo do `repop_classe_assunto` e o `_upsert_catalogo` do enricher. Basta um
escritor novo gravar `classe_id` sem conferir para o vínculo pendurado entrar
sem nada falhar, e o estouro aparecer depois no `proc.classe` do ORM.

Como criar, quando houver janela (**não executado — depende do general**):

```sql
-- 1. barato: pega ACCESS EXCLUSIVE por instantes, NÃO varre a tabela.
--    O risco não é a duração, é a FILA: um ALTER que espera bloqueia todo
--    mundo atrás dele. Por isso lock_timeout curto e retentar, nunca forçar.
SET lock_timeout = '3s';
ALTER TABLE tribunals_process
  ADD CONSTRAINT tribunals_process_classe_id_fk
  FOREIGN KEY (classe_id) REFERENCES tribunals_classejudicial(codigo)
  DEFERRABLE INITIALLY DEFERRED NOT VALID;   -- é assim que o Django escreve
                                            -- FK no Postgres; divergir aqui
                                            -- recria a ficção ao contrário

-- 2. caro mas manso: SHARE UPDATE EXCLUSIVE, deixa leitura e escrita passarem.
--    Custo esperado ~1-2 min por constraint (a contagem equivalente levou 65 s).
SET lock_timeout = '3s';
ALTER TABLE tribunals_process VALIDATE CONSTRAINT tribunals_process_classe_id_fk;
```

⚠️ Pré-requisitos, todos medidos: (a) `classe_id`/`assunto_id` são
`varchar(20)` e o alvo é a PK dos catálogos — os tipos casam; (b) a coluna é
NULLable e NULL não é checado, então as 66,9 M de linhas sem classe não
atrapalham; (c) o custo em escrita é um lookup na PK de uma tabela de 662
(classes) / 4.115 (assuntos) linhas, que vive em cache — irrelevante perto dos
~25 índices que a linha já paga.

⚠️ **Não fazer com backfill rodando.** Durante a corrida do #104 havia
transação aberta há 18 min na `tribunals_process`; o `ALTER` entraria na fila
atrás dela e todo mundo entraria na fila atrás do `ALTER`. É exatamente a forma
do incidente do `DROP INDEX` não-concurrent que derrubou o site.

⚠️ **Na `tribunals_movimentacao`, não.** Validar FK em 1,55 bi de linhas /
1.888 GB sem sequer um índice em `classe_id` é outro projeto, com outro custo.


### Inventário: declarado × banco (01/09/2026, #111)

As 3 FKs de `tribunals_process` eram a ponta. A varredura completa —
**todas** as migrations do projeto contra a `.101`, comparando por **DEFINIÇÃO
(coluna), nunca por nome** — deu:

```
37 modelos declarados · 50 tabelas no banco
CONTROLE: 37/37 PKs declaradas encontradas (100%)     ← sem isto nada se publica

DECLARADO E AUSENTE ......... 23
  FK ............ 14   em 6 tabelas
  índice ......... 7
  unique ......... 1   uniq_alerta_aberto_tribunal_tipo_chaves
  nome colidido .. 1   proc_tribunal_id_idx
tabela ausente ... 0 · coluna ausente ... 0 · tipo divergente ... 0
```

Tabela ausente 0, coluna ausente 0 e tipo divergente 0 **são o controle
negativo**: o defeito é só de objetos *acessórios* (FK, índice, constraint,
trigger) — as tabelas e colunas em si nunca foram perdidas. É coerente com a
hipótese de schema reconstruído por `CREATE TABLE` à mão nas tabelas antigas,
com as migrations marcadas depois: as 21 tabelas **novas** têm todas as suas FKs.

#### A régua ficou no repositório

```bash
manage.py auditar_schema           # o inventário
manage.py auditar_schema --json
manage.py auditar_schema --reparar-fks   # ADD CONSTRAINT ... NOT VALID
manage.py auditar_schema --validar-fks   # VALIDATE CONSTRAINT
```

`tribunals/schema_auditoria.py` monta o estado final das migrations
(`MigrationLoader`) e o compara com `pg_attribute`/`pg_index`/`pg_constraint`
por lista **ordenada de colunas** (+ predicado parcial). Ele distingue três
casos que a conferência por nome confunde:

| achado | significado |
|---|---|
| `indice`/`fk`/`unique` | declarado e **não existe**, por nenhum nome |
| `nome_diferente` | existe, com as colunas certas e **outro** nome (aviso) |
| `nome_colidido` | existe com o **mesmo nome** e **colunas erradas** ← o pior |
| `invalido` | `indisvalid = false`: o planner não usa, a escrita mantém |
| `nao_validada` | FK criada `NOT VALID`, falta `VALIDATE CONSTRAINT` |

O campo de controle é `controle_pk`: PK é o objeto que certamente existe em toda
tabela viva. Se ele não fecha em 100%, o command **recusa** publicar o
inventário (`CommandError`) — meia régua é pior que régua nenhuma.

#### O que foi criado em 01/09/2026

Tabelas pequenas, lock instantâneo, FK criada **já validada** (0 violações):

| tabela | tamanho | objetos criados |
|---|---:|---|
| `accounts_invite` | 160 kB | 2 FKs (`created_by_id`, `used_by_id` → `auth_user`) |
| `tribunals_ingestionrun` | 109 MB | 1 FK (`tribunal_id`) |
| `tribunals_schemadriftalert` | 13 MB | 2 FKs + 5 índices + **`uniq_alerta_aberto_tribunal_tipo_chaves`** |

⚠️ A unique parcial do `SchemaDriftAlert` **não existia**: a garantia que este
arquivo descrevia ("não sobrescreve evidências") era do model, não do banco. A
tabela tinha só a PK — nenhum índice, nenhuma FK. Criada sem conflito
(0 duplicatas em 15 linhas).

Mais uma, criada `NOT VALID` numa brecha de lock:

| `tribunals_processoparte` | 35 GB | 1 FK `parte_id` → `tribunals_parte(id)`, **NOT VALID** |

`NOT VALID` já vale para toda linha nova; falta só a prova sobre o passado
(`auditar_schema --validar-fks`, que varre sem bloquear escrita). Não rodado
aqui: são 35 GB de varredura num banco que já está disk-I/O-bound com o #105 e
o #106 rodando, e site em pé vale mais.

#### ⚠️ FK sem índice na coluna que referencia é bomba — 13 minutos em produção

A mesma corrida criou `processoparte.representa_id → self` e ela **teve que ser
derrubada em 13 minutos**. A lição não é sobre a FK, é sobre qual lado paga:

> Quem paga o preço de uma FK sem índice é o lado **REFERENCIADO**. Apagar uma
> linha do pai dispara, para **cada** linha, `SELECT 1 FROM filho WHERE fk = $1`.
> Sem índice em `filho.fk`, isso é um **Seq Scan da tabela filha por linha
> apagada**.

`representa_id` é auto-referência e **não tem índice** — então cada
`ProcessoParte` apagada varreria 35 GB. E `enrichers/drainer.py:423` faz
`ProcessoParte.objects.filter(processo_id=pid).delete()` no caminho quente do
drainer (~126 mil events/h).

Derrubada em 17 tentativas de `lock_timeout=3s`. Banco conferido depois:
**0 sessões esperando Lock**, 8 ativas, transação mais velha 36 s.

`auditar_schema --reparar-fks` passou a **recusar** FK cuja coluna não seja a
primeira de um índice válido e não-parcial (`--sem-guarda-de-índice` existe e é
opt-in, com o porquê no docstring). As outras FKs criadas passam na guarda:
`processoparte.parte_id` tem `pp_parte_polo_idx`, `process.classe_id` tem
`proc_classe_id_idx`, e assim por diante.

#### O que ficou pendente, e por quê

| objeto | motivo |
|---|---|
| 3 FKs de `tribunals_process` | **lock**: os shards do #105 seguram `RowExclusiveLock` de forma contínua (transações de 6-54 s). 15 tentativas com `lock_timeout=3s` perderam a corrida, sempre limpo (0 sessões em espera depois). Precisa de **janela**: parar os escritores, aplicar em segundos, religar. |
| `processoparte.processo_id` | referencia `tribunals_process` e pega `SHARE ROW EXCLUSIVE` lá — mesma fila. |
| `processoparte.representa_id` | **bloqueada pela guarda de índice** — ver abaixo. Só depois do índice. |
| `processoparte.parte_id` | criada `NOT VALID`; falta o `VALIDATE` (35 GB de varredura). |
| 3 FKs de `tribunals_movimentacao` | 1,89 TB / 1,55 bi de linhas. `ACCESS EXCLUSIVE` na tabela mais quente do sistema + `VALIDATE` de dias. Decisão de operação, e fora do `--max-bytes` default do command. |
| índice `representa_id` | ver abaixo — deve ser criado, mas `CONCURRENTLY`, fora de janela de backfill. |
| índice `classe_id` da movimentação | **não deve ser criado.** Ver abaixo. |

#### O índice de `classe_id` em `tribunals_movimentacao`: NÃO criar

A pergunta foi medida antes de decidir, dos dois lados:

| medida | valor |
|---|---|
| tabela | **1.893 GB · 1.548.356.480 linhas** |
| `classe_id` preenchido | 99,92% (`null_frac` 0,00078) |
| consultas no código que filtram/juntam por `classe_id` | **0** |
| a única que o TOCA (`repop_classe_assunto --tabela movimentacao`) | `Index Scan using tribunals_movimentacao_pkey`, **custo 5.334** — entra por faixa de pk e **não usaria** o índice |
| custo de criá-lo | ~35-45 GB (extrapolando de `mov_tribunal_ativo_idx`, 15 GB), mantidos no caminho de **escrita** da ingestão diária |
| `WHERE classe_id = '1116'` (hipotética, não existe no código) | Parallel Seq Scan, custo 132.470.277 |

⇒ **Ausência registrada, índice não criado.** Índice que ninguém consulta é
custo puro de escrita — e criar um de 40 GB numa tabela de 1,9 TB para uma
consulta que não existe é o oposto da regra. A declaração no model foi
**removida** (inclusive o índice implícito da FK, via `db_index=False`), porque
declarar o que não existe é o que produziu os três índices fantasma da `0051`.

#### O índice de `representa_id` em `tribunals_processoparte`: DEVE ser criado

Este é o contrário do anterior — declarado, ausente, e com custo **medido** no
caminho de escrita:

```
EXPLAIN UPDATE tribunals_processoparte SET representa_id = NULL
        WHERE representa_id IN (1,2,3);
->  Seq Scan on tribunals_processoparte  (cost=0.00..2509709.83)
```

É exatamente o que o `on_delete=SET_NULL` do Django emite ao apagar uma
`ProcessoParte` — e `enrichers/drainer.py:423` apaga no hot path do drainer.
17,4% das 104 M de linhas têm `representa_id` (`null_frac` 0,826), então a
versão parcial `WHERE representa_id IS NOT NULL` resolveria com ~1/6 do tamanho.

**E ele é PRÉ-REQUISITO da FK**, não um extra: enquanto não existir, a FK
`representa_id → self` transforma cada `DELETE` de `ProcessoParte` num Seq Scan
de 35 GB (ver a caixa acima — foi medido do jeito caro). A ordem é: índice
`CONCURRENTLY` primeiro, FK depois.

**Não criado aqui**: `CREATE INDEX CONCURRENTLY` em 35 GB espera todas as
transações abertas, e há varredores do #105 com `statement_timeout` de 400 s.
Fica como recomendação medida.

#### Dois índices INVÁLIDOS no banco (achado colateral)

`indisvalid = false` = sobra de `CREATE INDEX CONCURRENTLY` que morreu na 2ª
fase. O planner **não** os usa; a escrita **continua** mantendo-os.

| índice | tabela | tamanho | scans |
|---|---|---:|---:|
| `proc_tribunal_numero_cnj_idx` | `tribunals_process` | **7.837 MB** | 0 |
| `mov_ativo_idx` | `tribunals_movimentacao` | 8 kB | 0 |

`proc_tribunal_numero_cnj_idx` é `(tribunal_id, numero_cnj)` — **duplicata** do
`uniq_proc_tribunal_cnj`, que existe, é válido e recebe 2,35 bi de scans. São
7,8 GB de disco e escrita paga por nada. Derrubá-lo é `DROP INDEX CONCURRENTLY`
(o não-concorrente é o incidente de 25/08), e é decisão de operação.

#### Triggers declarados e ausentes (4)

`mov_search_vector_trg` (0001), `mov_update_process_agg` (0004), `pp_total_ins`
e `pp_total_del` (0005). O quinto, `process_set_ano_cnj`, sumiu do mesmo jeito e
foi recriado pela `0042` — o padrão é o mesmo. Quem mantém os agregados hoje é
`djen/ingestion.py::_flush_resumo`; a `search_vector` está morta (ver
[Movimentacao](#movimentacao)). Não recriados aqui: recriar `mov_update_process_agg`
poria um trigger por linha no caminho de escrita de 1,55 bi de movimentações,
que é decisão de arquitetura, não de reparo de schema.


### O buraco das FKs `classe`/`assunto` (#104) — medido em 31/08/2026

```sql
-- a régua da pendência
SELECT count(*) FROM tribunals_process
 WHERE classe_codigo IS NOT NULL AND classe_codigo <> '' AND classe_id IS NULL;
```

| | linhas | |
|---|---:|---|
| `tribunals_process` | 104.003.151 | total |
| `classe_id` NULL com `classe_codigo` | **8.054.334 → 21** | o **antes** e o **depois** de #104 |
| `assunto_id` NULL com `assunto_codigo` | **9.773.928 → 222** | o mesmo defeito, na coluna vizinha, fechado na mesma passada |
| já ligadas (`classe_id` não-nulo) | **29.023.746 → 37.198.560** | +8.174.814 |
| … com `classe_id = classe_codigo` | 37.176.739 | **99,941%** — é o campo de controle |
| … divergentes (`classe_id <> classe_codigo`) | 21.853 → **21.821** | 0,059% · contadas, **intocadas** |
| … FK preenchida com `classe_codigo` vazio | 800 → **800** | idem |
| sem classe nenhuma | 66.804.570 | não é buraco: é ausência de dado |

O controle fecha por soma: 37.176.739 + 21.821 = 37.198.560, exatamente as
ligadas. E os 21.821 divergentes e as 800 com código vazio ficaram **do mesmo
tamanho** depois de 8,17 M de escritas — a prova de que o `COALESCE` não
sobrescreveu FK já fechada.

⚠️ A conta não fecha exatamente com a pendência, e o motivo é medido: fecharam
2.780.743 linhas de `classe` entre 20:37 e 22:05, mas as ligadas subiram
2.825.018. A diferença de **44.275** é ingestão ao vivo (~8 linhas/s) que
chegou com `classe_codigo` durante a corrida e foi fechada pelo próprio shard.

**O buraco se realimenta.** `classe_codigo` é escrito sem fechar a FK por
`datajud/ingestion.py::_meta_updates_from_source`, por `datajud/hidratacao.py`
e pelo caminho do enricher. Não é resíduo histórico de uma migration antiga —
ver a seção "o buraco se realimenta", abaixo, com a prova.

**`Movimentacao.classe_id` NÃO tem o mesmo buraco** — e isso foi medido, não
suposto. Amostra de **510.636** movimentações em 11 janelas de 50.000 pks
espalhadas por toda a faixa (10 M a 1,9 bi): **0** com `codigo_classe`
preenchido e `classe_id` NULL, **0** divergentes, **0** sem código. A diferença
é o caminho de escrita: `djen/ingestion.py`, `datajud/ingestion.py` e
`diarios/base.py` fecham `classe_id` no próprio `bulk_create` da movimentação,
enquanto os escritores de `Process` gravam só a string. Por isso a corrida
rodou apenas com `--tabela process`.

#### O depois: 8.054.334 → 21, e os 21 não são resíduo, são reabertura

A corrida de 31/08 fechou com a **mesma consulta** da pendência (22:05 UTC, um
`Parallel Seq Scan` de 37 s sobre as 104.003.151 linhas):

| | antes (31/08, manhã) | depois (31/08, 22:05 UTC) |
|---|---:|---:|
| `classe_id` NULL com `classe_codigo` | 8.054.334 | **21** |
| `assunto_id` NULL com `assunto_codigo` | 9.773.928 | **222** |

**Os 21 de classe não têm um órfão sequer** — todos os 11 pares
(tribunal, código) que sobraram existem no catálogo. O que eles têm em comum é
`atualizado_em` nos **últimos 30 minutos**, isto é, DEPOIS de o shard daquela
faixa de pk já ter passado: TRF4 12078 (8), TRF2 12078 (4), TJPR 15160/12078/14695,
TJSE 436, TRF4 156/198/436, TRF6 11875/12078. Espalhados por pk 13,1 M a
103,3 M — não é uma faixa esquecida, é o buraco se reabrindo atrás da vassoura.

Os 222 de assunto se separam em dois fatos com donos diferentes:

| o que é | linhas | códigos |
|---|---:|---:|
| **abstenção declarada** — fora do padrão da TPU (`^\d{1,5}$`) | **132** | 6 (`99999999`, `40100001/2/4/9/13`) |
| **abstenção declarada** — código sem nome na linha | **64** | 32 (`15175`, `15176`, `50xxx`…) |
| reabertas ao vivo (código existe no catálogo) | 26 | 19 — 21 delas tocadas nos últimos 45 min |

**196 linhas são abstenção, não pendência.** Criar `99999999` ou inventar nome
para `15175` sujaria de forma permanente um catálogo NACIONAL que é destino de
FK. Ficam contadas, com o motivo separado no JSON do comando
(`orfao_fora_do_padrao_assunto_id`, `orfao_sem_nome_assunto_id`) e como ERROR
no log — teto é alerta, nunca corte mudo.

#### Órfãos da TPU: classe nunca teve; assunto tinha 604.954 e ficou com 196

| coluna | linhas a ligar (antes) | códigos distintos | órfãos do catálogo |
|---|---:|---:|---:|
| `classe_id` | 8.054.281 | 502 (59 tribunais) | **0** (antes) → **0** (depois) |
| `assunto_id` | 9.773.928 | — | 604.954 em 1.255 códigos → **196** em 38 |

**Classe nunca teve órfão** — nem no diagnóstico (502 códigos), nem no meio da
corrida (465 códigos em 2,9 M de linhas pendentes), nem no fim. Todo código de
classe que o acervo grava existe na TPU. O órfão sempre foi de assunto, e
`--criar-catalogo` derrubou 604.954 para 196 criando **1.039** linhas de
catálogo a partir do nome que já estava gravado na própria linha
(`tribunals_assunto`: 3.076 → 4.115). A concentração dos órfãos remanescentes é
TJSP (`99999999`, 120 linhas) e a cauda trabalhista/TRF6.

#### O buraco se realimenta — está medido, não é suposição

Duas medições independentes, a segunda conclusiva:

1. na varredura por faixa de 1 M de pk feita **durante** a corrida, faixas já
   fechadas voltaram a ter linha pendente — 3 em 13 M, 8 em 14 M, 1 em 71 M;
2. no fim, **as 21 linhas de classe que sobraram têm `atualizado_em` posterior
   à passagem do shard**, e nenhuma delas é órfã. A tabela não cresceu no
   intervalo (`count` 104.003.151 e `max(id)` 106.326.832, iguais antes e
   depois): não são linhas novas, são linhas **antigas reescritas** por
   `datajud/ingestion.py::_meta_updates_from_source`, `datajud/hidratacao.py` e
   o caminho do enricher, que gravam `classe_codigo` sem fechar a FK.

**Zero não é estado estável; é uma foto.** Enquanto os três escritores não
fecharem a FK na própria escrita, este backfill é **recorrente**, não um
mutirão único. A taxa medida é baixa (21 linhas em ~30 min de corrida contra
8,05 M reparadas), então uma passada periódica resolve — mas quem olhar a
consulta amanhã e vir um número diferente de zero deve ler isto antes de
concluir que o backfill falhou.

⚠️ O `--ate` do último shard é **106.400.000** contra `max(id) = 106.326.832`.
A margem é de 73 mil pks. Numa próxima passada, calcular o teto a partir de
`max(id)` — o comando já faz isso quando `--ate` é 0.

#### O conserto na ORIGEM (01/09/2026) — e o terceiro culpado que não existia

O tique horário de reparo (`tick_repop_fk_recente`) foi tentado e **falhou**:
filtrar por `atualizado_em` não restringe nada enquanto os shards do
`backfill_fase` carimbam a coluna em milhões de linhas — estourou
`statement_timeout` em três variantes (`min(id)`, `ORDER BY id LIMIT 1`, sem
`ORDER BY`). O job foi **apagado** em 01/09; a razão está em `djen/scheduler.py`.

Em 18 h o buraco reabriu para **8.072** (classe) e **8.343** (assunto). A
decomposição, medida contra `max(id) = 106.326.832` (o topo de 31/08):

| | linhas |
|---|---:|
| processos NOVOS na faixa (pk > 106.326.832) | 153.860 |
| … destes, com `classe_id` NULL | **194** (0,13%) |
| ⇒ linhas ANTIGAS reescritas | **7.837** (97,6%) |
| códigos sem correspondente no catálogo | **0** |

Zero órfãos é o dado que fecha o diagnóstico: o escritor **tinha** com o que
resolver a FK e não resolvia. Não é dado ausente, é vínculo não feito.

**Eram DOIS escritores, não três.** A documentação anterior acusava também "o
caminho do enricher"; conferido linha a linha, o enricher **fecha** a FK nos
dois caminhos (`upsert_catalogo` no single, `_bulk_upsert_catalogos` +
`classe_by_code` no bulk, e `fallback_classe_via_djen` traz `classe_id` da
movimentação). A acusação estava errada e fica registrada como errada.

| escritor | o que fazia | linhas/16 h |
|---|---|---:|
| `datajud/hidratacao.py` | `Process.objects.create(**campos)` sem `classe_id`/`assunto_id` | 194 |
| `datajud/ingestion.py::sync_processo` | `.update(**meta_updates)` sem as FKs | 7.837 |

Os que aparecem na busca por `classe_codigo` e **não** têm o defeito, cada um
conferido: `datajud/varredura.py:181` (escreve no índice ES, onde não há FK),
`enrichers/drainer.py`, `backfill_classe`, `backfill_assunto`,
`preencher_classe_via_djen`, e `djen/ingestion.py` + `diarios/base.py` (criam
`Process` só com CNJ; a classe vai na `Movimentacao`, já com `classe_id`).
`backfill_classe_cnj` mexe só em `classe_cnj_*`, que não tem FK.

**A cura: `tribunals/catalogo.py`.** Um `resolver(qual, codigo)` que consulta o
catálogo e **abstém** quando não acha — FK fica NULL, com log. É *lookup*, não
upsert: este catálogo é NACIONAL e destino de FK, e a primeira corrida do
`repop` criou `99999999` nele em menos de dois minutos quando podia criar à
vontade. E **nunca degrada**: a FK só é escrita quando resolve, então um código
órfão vindo do Datajud não vira `classe_id = NULL` por cima do vínculo que o
enricher fechou.

Custo, medido em produção no `voyager-web-1` **depois** do deploy:

| | |
|---|---|
| 1ª chamada do processo (carrega os dois catálogos) | **92,4 ms**, uma vez a cada 5 min |
| catálogo em memória | 662 classes + 4.295 assuntos = 4.957 linhas |
| chamada quente (`classe` + `assunto`) | **2,32 µs** |
| código órfão, 1ª vez (sonda pela PK) | 9,04 ms — memorizada até o fim da janela |
| código órfão, memorizado | 0,61 µs |
| um `fetch_processo` do CNJ, ao lado do qual isso roda | **20,9 s** (fila do rate-limit) |

Não encarece nada de forma perceptível: 2,32 µs contra os ~21 s que a mesma
`sync_processo` gasta esperando o token do CNJ.

Prova do deploy **dentro do processo** (não pelo disco), 01/09/2026:

| processo | prova |
|---|---|
| `worker_manual` (.103) | `hidratar_processo` de um CNJ do esqueleto nasceu com `classe_id='11875'` e `assunto_id='7769'` |
| `worker_datajud` (.102) | linha criada só com CNJ (`classe_codigo=''`, `classe_id=None`) foi a `classe_codigo='279'`/`classe_id='279'`, `assunto_codigo='5851'`/`assunto_id='5851'` |

### `repop_classe_assunto` — o backfill que fecha as duas FKs

```bash
# medir sem escrever (a régua do antes/depois)
manage.py repop_classe_assunto --de 60000000 --ate 60100000 --sem-reparo --json

# a corrida de 31/08, em 8 shards de faixa DISJUNTA (o comando não coordena
# shards — as faixas TÊM que ser disjuntas, e juntas cobrir (0, max(id)])
#   a1 (          0,   9.000.000]   a6 ( 40.000.000,  52.000.000]
#   a2 (  9.000.000,  13.000.000]   a10( 52.000.000,  60.000.000]
#   a3 ( 13.000.000,  21.000.000]   a8 ( 60.000.000,  70.000.000]
#   a4 ( 21.000.000,  27.250.000]   a7 ( 70.000.000,  90.000.000]
#   a5 ( 27.250.000,  40.000.000]   a9 ( 90.000.000, 106.400.000]
#
# Os shards a8/a9/a10 nasceram DEPOIS, partindo faixas que tinham virado o
# gargalo — dá para fazer isso com a corrida no ar (ver `--de` abaixo).
manage.py repop_classe_assunto --shard a4 --de 0 --ate 27250000 \
    --criar-catalogo --bloco 20000 --sleep 0.2 \
    --freio-ms-kpk 6000 --parar-ms-kpk 20000

# parar TODOS os shards de qualquer lugar, sem deploy
manage.py shell -c "from django.core.cache import cache; \
    cache.set('tribunals:repop_classe_assunto:off', True)"
```

⚠️ **`--de 0` não quer dizer "do começo" — quer dizer "do checkpoint".** É
`cur_pk = o['de'] or (cache.get(wm) or 0)`, e é por isso que todo container da
corrida passa `--de 0`: assim um restart do docker (reboot, OOM,
`unless-stopped`) retoma de onde parou em vez de reescrever a faixa inteira.
O outro lado do mesmo `or` é como se abre um shard NOVO no meio da tabela sem
varrer o que está na frente: `--de 60000000` ANULA o checkpoint. Foi assim que
a faixa (40 M, 70 M] do `a6` — a maior sobra, 999.604 linhas paradas atrás de
uma corrente sequencial — virou `a6` até 60 M mais um `a8` novo de 60 M a 70 M,
com o começo do `a8` plantado direto no Redis
(`SET v:1:tribunals:repop_classe_assunto:wm:process:a8 60000000`, sem TTL — o
`RedisSerializer` do Django grava `int` como texto puro).
`tests/test_repop_classe_assunto.py::test_de_zero_retoma_do_checkpoint_e_de_explicito_o_anula`
prende as duas metades; quebrar qualquer uma delas custa caro em direções
opostas (refazer trabalho × varrer a tabela desde o pk 0).

⚠️ **Shard que termina + `--restart unless-stopped` = loop de restart.** O
comando sai com 0 quando o checkpoint alcança o `--ate`, e o docker religa. A
cada volta ele recarrega os dois catálogos e consulta `max(id)` antes de sair
de novo. Não é caro, mas é ruído permanente e esconde o "acabou": **pare o
container quando a faixa fechar** (`docker stop r104_fk_a3`). Observado em
31/08 com o `a3` entrando em `Restarting (0)` segundos depois de fechar
(13 M-21 M).

Quatro decisões que não são estética:

1. **Varre por faixa de pk.** A versão anterior usava
   `UPDATE … WHERE ctid IN (SELECT ctid … LIMIT n)`. O `LIMIT` faz o Seq Scan
   parar cedo enquanto sobra linha quebrada na frente da tabela; quando a
   frente esvazia, cada iteração varre mais fundo até varrer os 28 GB inteiros.
   Custo quadrático — **em 104,1 M de linhas ele não termina**, e é por isso que
   a pendência mediu 8,2 M em 30/08 e 8,05 M em 31/08 (a diferença é ingestão
   ao vivo, não progresso).
2. **`classe` e `assunto` no MESMO `UPDATE`.** A FK é indexada
   (`proc_classe_id_idx`, `proc_assunto_id_idx`), então o UPDATE **não é HOT**:
   cada linha tocada ganha uma versão nova no heap e uma entrada em cada um dos
   ~25 índices da tabela. Duas passadas custariam isso duas vezes, de graça.
3. **Sem campainha, e isso é medido.** `search/documents.py::processo_to_doc`
   publica `codigo_classe` (de `classe_codigo`), `classe_nome`, `assunto` e
   `assunto_codigo` — **nenhum** vem da FK. Fechar `classe_id` não muda um byte
   do documento no ES, então `atualizado_em` **não** é tocado: tocá-lo custaria
   8,05 M de reindexações para produzir documentos idênticos.
   `tests/test_repop_classe_assunto.py` prende a afirmação — se a FK entrar no
   doc, o teste quebra e a decisão volta à mesa.
4. **`--criar-catalogo` tem guarda de padrão (`^\d{1,5}$`).** Sem ela, dois
   minutos de corrida criaram `Assunto(codigo='99999999', nome='ASSUNTOS
   ANTIGOS DO SAJ - RECONVENÇÃO')` no catálogo **nacional**. A guarda custa
   0,02% (6 códigos, 132 linhas: `99999999` e os `4010000x`) e evita sujeira
   permanente numa tabela que é destino de FK. Código sem nome também não vira
   linha — fica contado como `orfao_sem_nome`.
5. **`lock_timeout` (55P03) é retentado, como o deadlock (40P01).** Doze
   minutos depois de subir, um shard morreu com
   `canceling statement due to lock timeout … while locking tuple` — a linha
   estava com o `backfill_fase` do #105, que faz UPDATEs de 20-30 s na mesma
   tabela. Nada se perde (o checkpoint segura a posição), mas a corrida morre
   sozinha de madrugada e o "acabou" nunca chega. Com dois backfills na mesma
   tabela quente, encontrar lock **é o dia**, não a exceção. Retentar é mais
   barato que subir o `lock_timeout`, que faria o lote segurar as próprias 500
   linhas por mais tempo enquanto espera.

Custo medido em produção (31/08/2026, `.101` sob carga, com os 4 varredores de
fase do #105 rodando na mesma tabela):

| | medido |
|---|---|
| leitura de 50.000 pks (`--sem-reparo`) | 2,8-3,4 s ⇒ **81-99 ms / 1.000 pks** |
| leitura + escrita, 1 shard | **455-1.024 ms / 1.000 pks** · **3,94-4,64 ms por linha** |
| leitura + escrita, 5 shards | até **5.087 ms / 1.000 pks** · ~13,5 ms por linha |
| densidade de linhas quebradas | 5% a **78%** entre blocos VIZINHOS |
| sessões esperando `Lock` durante a corrida | **0** |
| vazão de escrita, 1 shard | 216 linhas/s (com os 4 varredores do #105 no ar) |
| vazão de escrita, 5 shards | **1.042 linhas/s** (471.145 linhas em 452 s, com 2 do #105 no ar) |

⚠️ A primeira leitura desse par disse "paralelizar quase não paga, ~280
linhas/s com 7 shards" — e estava **errada**, porque eu derivei linhas de
*progresso em pk*, e a densidade varia 15x entre faixas. Contando as linhas que
os logs REPORTAM, em duas fotos separadas por 452 s, dá 1.042/s. O fator 4,8x
contra 1 shard é indicativo e não controlado: as duas medições têm carga
externa diferente (4 varredores do #105 na primeira, 2 na segunda).

E a vazão que os logs reportam foi **conferida contra o banco**, num shard só e
numa faixa fechada — porque contagem própria não prova nada. Faixa
(26.180.000, 27.250.000] do `a4`, com os outros 6 shards, os 4 do #105 e o #106
no ar:

| | |
|---|---|
| pendência na faixa, t0 (20:52:51 UTC) | classe 120.805 · assunto 146.026 · **união 150.091** |
| pendência na faixa, t1 (21:04:22 UTC) | **0 · 0 · 0** |
| linhas que o log do `a4` reportou na janela | **157.557** |
| ⇒ vazão de UM shard | **~217 linhas/s** |

Os dois lados fecham: 157.557 escritos ≈ 150.091 fechados na faixa mais a cauda
do bloco anterior. E o **campo de controle** é a própria faixa ir a zero nas
duas colunas. Com 7 shards, a pendência de `classe_id` caiu de **2.945.391**
(20:31) para **2.162.864** (20:50) — 782.527 linhas em 1.140 s, **686
linhas/s** só de `classe`, mais o que fechou de `assunto` na mesma passada.

⚠️ Na primeira tentativa eu calculei essa taxa com horário **estimado** em vez
de lido, e ela deu 178/s — 3,8x menor. Não havia nada errado com a corrida:
estava errado o relógio da conta. `date -u` custa nada; supor a hora custou uma
investigação inteira atrás de um gargalo que não existia.

⚠️ **O freio (`ms` por 1.000 pks) é confundido pela densidade nesta tabela.**
Ela vai de 5% a 78% entre blocos vizinhos, então a métrica não separa "o banco
piorou" de "esta faixa tem mais trabalho". Calibrei errado duas vezes: primeiro
pela medição SEM escrita (o freio dobrou a pausa contra uma corrida saudável),
depois pela medição de UM shard (com cinco em paralelo, um bloco denso bate
5.087 e o `--parar` mataria a corrida de madrugada com o banco perfeitamente
são). Os tetos ficaram folgados de propósito — quem mede degradação de verdade
é `_motivo_de_esperar`, que **lê** as sessões esperando `Lock`.

**O gargalo é WAL, e a conclusão óbvia dele NÃO se confirmou.** Amostrando
`pg_stat_activity` durante a corrida, os UPDATEs do backfill estão em
`LWLock: WALWrite` e `LWLock: WALInsert` — não em CPU e não em lock
(`wait_event_type='Lock'` deu **0**). Faz sentido: a FK é indexada, o UPDATE
não é HOT, e cada linha vira uma tupla nova no heap mais uma entrada em ~25
índices. Daí a hipótese de que `synchronous_commit = off` resolveria. O A/B em
faixas vizinhas e densas de 20.000 pks, com os 7 shards e os 4 varredores do
#105 no ar:

| | linhas/s |
|---|---:|
| `synchronous_commit` on (default) | 119 · 136 |
| `synchronous_commit` off | 152 (uma amostra densa) |

**+19% sobre uma amostra contra duas, dentro do ruído** — não paga trocar
durabilidade do último commit num backfill de 10,7 M de linhas. A flag
`--commit-assincrono` existe, é opt-in e **não foi usada na corrida**; o número
fica registrado para ninguém refazer o teste achando que é de graça.

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
