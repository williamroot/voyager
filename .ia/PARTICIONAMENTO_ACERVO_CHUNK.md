# Particionamento de `acervo_chunk` — escopo, medição e veredito

> **Status:** DESIGN / escopo. Nada aplicado em prod. Todas as medições abaixo são
> read-only (`pg_class`/`pg_stat_*`/`pg_statio_*`/`iostat`), colhidas ao vivo em
> **25/jul/2026** contra `zordon-db` (192.168.30.114).

## TL;DR — veredito

**NÃO particionar agora.** A investigação ao vivo **refutou a premissa** de que o
gargalo de escrita é o *relation-extension lock*. Em 24s de amostragem sob carga de
batch (8-9 INSERTs concorrentes), o wait `extend` foi **0 em todas as amostras**; o
wait dominante é **`IO/DataFileRead`** (3-6 backends simultâneos) — leitura aleatória
de páginas de índice/TOAST que não cabem em cache. O lever certo é **RAM/cache +
disco**, não geometria de tabela. Particionar traria custo alto (rebuild de ~54GB de
HNSW por partição, risco real de regressão de busca com fan-out N-way) para atacar um
lock que **não está travando**. As alternativas mais baratas (limpar índice morto,
mais RAM de cache no host do DB, réplica read-only pra tirar a busca do primário, e —
só se ainda apertar — pgvectorscale/DiskANN) resolvem o I/O-bound sem os riscos.

Ordem recomendada: **(0) drop de índice morto → (1) réplica read-only → (2) RAM no
host do DB → (3) pgvectorscale/StreamingDiskANN**. Particionamento fica como *fase 4
condicional* (só quando o full-corpus 1,15M processos entrar e o índice não couber em
nenhuma RAM viável).

---

## 1. Tamanhos reais medidos (25/jul/2026)

```
acervo_chunk       heap      10 GB     reltuples ~28,6 M   (dead 3,2% — vacuum em dia)
  └ TOAST (text cifrado)     288 GB    ← 77% da tabela; é o texto AES-GCM
  └ total (heap+toast+idx)   372 GB
acervo_documento   heap     744 MB     reltuples  ~2,46 M
acervo_processo    heap      19 MB     reltuples  ~150 k
DB inteiro                   402 GB

Índices de acervo_chunk:
  chunk_emb_hnsw_half   54 GB   HNSW   ((embedding)::halfvec(1024)) halfvec_cosine_ops  m=16 efc=64
  chunk_fts_gin         17 GB   GIN    (search_vector)
  chunk_doc_ordinal_idx 1,5 GB  btree  (documento_id, ordinal)   ← idx_scan = 0 (MORTO)
  acervo_chunk_pkey     1,3 GB  btree  (id)
  ...documento_id_...  549 MB   btree  (documento_id)            ← serve todo o delete/lookup
                       ─────
  Índices somados      ~74 GB
```

Notas vs. o briefing:
- Linhas **~28,6M** (não 21,5M) — o corpus cresceu.
- HNSW halfvec **54GB** (não 43GB), GIN **17GB** (não 6GB). Já é **expressão** (halfvec),
  então o INSERT continua gravando `embedding::vector` (fp32) e o PG mantém a projeção
  halfvec no índice automaticamente — **o write path não muda com halfvec**.
- **Host do DB (`zordon-db`)**: 94GB RAM, 18 vCPU, `shared_buffers=24GB`,
  `effective_cache_size=70GB`, `maintenance_work_mem=2GB`, disco QEMU 800G (57% usado,
  328G livres). `buff/cache` ~80GB, `available` 67GB.
- **Working set (372GB tabela) >> RAM (94GB).** HNSW (54GB) + GIN (17GB) = 71GB só de
  índice quente — não cabe junto com o TOAST quente no cache. Daí o `DataFileRead`.

### Distribuição (via `pg_stats`, sem full scan)

Tribunal (em `acervo_processo`, ~150k processos): `TJSP 52%`, `TRF1 44%`, resto <3%
(TRF5, TJAL, TJMG, TJMA, TRF3). **Só 2 tribunais concentram 96%** → chave `tribunal`
daria partições grosseiramente desbalanceadas (2 gigantes + 5 anões).

Ano CNJ (em `processo`): 2024 26%, 2025 16%, 2023 12%, 2022 9%, 2021 9%, 2020 6%, cauda
até 2014. 15 anos distintos. Razoavelmente escalonado, mas **ano NÃO está no chunk** —
precisaria desnormalizar (ver §2).

`documento.status`: `ready 99,6%`. `documento_id` no chunk: correlação física **0,93**
(inserts append-mostly por documento; boa localidade — a tabela já é "quase clusterizada"
por documento sem precisar particionar).

## 2. Padrão de escrita e chaves de partição candidatas

**Padrão real (confirmado no código `acervo/chunk_writer.py::write_chunks_indexed` e no
`pg_stat_activity` ao vivo):** por documento faz
`DELETE FROM acervo_chunk WHERE documento_id=X` seguido de `executemany INSERT` dos
novos chunks (single-pass, `search_vector` inline no INSERT — sem UPDATE por-chunk). Ou
seja, **a unidade de escrita é o documento inteiro**. `n_tup_del=18M`, `n_tup_ins=34,8M`
confirmam o ciclo delete+insert. Isso é importante: qualquer chave de partição **precisa
ser derivável de `documento_id`** pra que delete+insert de um doc caiam sempre na mesma
partição (senão vira cross-partition write, pior).

| Candidata | Prós | Contras | Nota |
|---|---|---|---|
| **hash(documento_id)** N-way | balanceado; delete+insert de 1 doc = 1 partição; sem desnormalizar | **espalha CADA busca ANN por N partições** (a query não filtra por doc) → N sub-buscas HNSW + merge; N índices HNSW a construir/manter | melhor pra escrita, **pior pra busca** |
| **range(documento_id)** | monotônico → partição "quente" recebe quase todo insert novo; frias imutáveis (index-once) | doc novos concentram numa partição só → não distribui o extend/IO; busca ainda cross-partition | bom p/ *index-last*, ruim p/ distribuir escrita |
| **tribunal** | alinha com filtros do domínio | 96% em 2 tribunais → 2 partições gigantes (não resolve nada) + 5 anãs; requer desnormalizar `tribunal` no chunk | desbalanceado demais |
| **ano_cnj (range temporal)** | frias imutáveis; casa com filtro por período | **ano não está no chunk** → desnormalizar (coluna nova + backfill de 28,6M + manter no writer); doc de 2024 nunca muda de ano → estável | viável só com desnormalização |
| **criado_em (range temporal)** | já existe no chunk; partição atual "quente" | `criado_em` do chunk = quando foi vetorizado, não o ano do processo → re-vetorização move o doc de partição (delete numa, insert noutra = cross-partition) | **furado** pelo re-processamento |

**Conclusão de §2:** não existe chave boa. `hash(documento_id)` é a única que respeita o
padrão delete+insert-por-doc sem desnormalizar, mas é justamente a que **mais penaliza a
busca** (fan-out máximo). As temporais (`ano`, `criado_em`) ou não estão no chunk ou são
instáveis sob re-vetorização.

## 3. O ponto mais difícil — migrar 28,6M linhas / 372GB sob escrita contínua

Três abordagens, todas com o mesmo elefante: **recriar o HNSW por partição**.

**(a) `acervo_chunk_new` particionada + copiar em lotes + swap.** Requer dual-write (a
app escreve nas duas) ou congelar escrita durante o copy. Copiar 372GB (dos quais 288GB
são TOAST) leva horas; depois `CREATE INDEX` do HNSW **por partição**. Downtime de
escrita = janela do swap (minutos) + toda a fase de build de índice se não for online.

**(b) pg_partman.** Automatiza criação/retenção de partições, mas **não** resolve a
migração inicial da tabela legada nem o custo do rebuild HNSW. Adiciona dependência +
`shared_preload_libraries` (restart). Útil só depois de já particionado.

**(c) `PARTITION BY` + attach da atual como `DEFAULT` + criar novas.** É o caminho de
menor downtime pra *começar*, mas o Postgres **não converte uma tabela regular em
particionada in-place** — você cria uma tabela particionada nova e faz
`ALTER TABLE ... ATTACH PARTITION acervo_chunk_legacy DEFAULT`. O attach de uma tabela de
372GB como default exige validação de constraint (scan) e, para que o particionamento
sirva a algo, você ainda precisa **mover dados da default pras partições novas** =
delete+insert massivo = re-inserir no HNSW.

### Custo do rebuild HNSW — o número que mata

Referência real do próprio acervo (memory `vetor-ingest-teto-e-opcoes`, 24/jul):
construir o `chunk_emb_hnsw_half` **inteiro** (12,7M vetores na época) levou **~43min**
(2583s) com **8 workers paralelos + `maintenance_work_mem=32GB`**, com **toda escrita
congelada**. Hoje são **28,6M vetores** → extrapolando linear, **~1h35 por build
completo** nas mesmas condições.

Particionar em N partições **não reduz o trabalho total de build** — você constrói N
índices HNSW cujos vetores somam 28,6M. Na prática **piora**: cada build paga overhead
fixo, e `maintenance_work_mem=32GB` por build **não pode rodar N em paralelo** (32GB×N >
94GB de RAM) → você serializa os builds → o rebuild total pode passar de **2-3h** de
escrita congelada. E `zordon-db` teve **2 reboots acidentais** durante a última janela de
build (memory), matando o processo no meio — o risco operacional de uma janela longa é
concreto.

**Downtime estimado realista:** migração big-bang com re-build de HNSW particionado =
**2-4h de escrita congelada** (a extração pode seguir, só lê), na melhor das hipóteses,
com risco de ter que reiniciar do zero se a VM cair. Dual-write reduz o downtime de
*swap* mas não o custo de construir os índices, e adiciona complexidade de app (escrever
consistente em duas tabelas com o mesmo delete+insert idempotente).

## 4. O ganho vem? Escrita (melhora) × Busca (piora) — quantificado

### Escrita
O argumento pró-partição é: (i) cada partição estende seu próprio arquivo → menos
disputa de extend-lock; (ii) índices menores por partição → manutenção mais barata por
insert. **Problema:** a medição ao vivo mostra **extend-lock = 0**. Sobra só (ii) —
inserir num HNSW de 54/N GB é mais barato *por página tocada* que num de 54GB, MAS o
custo do INSERT no HNSW é dominado por **random reads da travessia do grafo** (medido:
`DataFileRead`), e o total de páginas quentes somadas nas N partições é ~o mesmo. O ganho
de escrita do particionamento aqui é **marginal e não ataca a causa medida** (I/O de
cache-miss). Distribuir por `hash(documento_id)` até ajuda a paralelizar, mas o teto real
é o disco servindo cache-miss, que continua o mesmo agregado.

### Busca — onde o risco é real e mensurável
Hoje (`acervo/search.py::hybrid_search`) a busca faz **1 scan ANN no HNSW global** +
**1 scan GIN global**, funde por RRF e re-ranqueia (cross-encoder). O filtro por `cnj`
é opcional; **o caso comum não filtra** → varre o índice inteiro. Latências medidas
(memory `db-contencao-busca-vs-batch`, 24/jul): ANN pura 184ms com `Buffers: hit=463
read=704` (~60% disco) e `hybrid_search` completo travando >40s **sob carga de batch** —
já disk-bound hoje.

Com **N partições e sem filtro de partição** (o caso comum), o planner faz **N Index
Scans HNSW independentes**, cada um retornando `ef_search` candidatos, e faz o merge.
Efeitos:
- **Recall:** HNSW é aproximado *por índice*. Top-K global ≠ união dos top-K/N de cada
  partição — para manter recall equivalente você precisa **aumentar `ef_search` por
  partição**, multiplicando o custo. Risco concreto de **queda de recall** (hoje 100%
  @20 medido) se `ef_search` não for compensado.
- **Latência:** N buscas HNSW + merge. Cada uma paga seu próprio cache-miss de disco. Com
  a busca **já** em >40s sob carga, multiplicar por N o número de scans é **exatamente na
  direção errada** — a decisão operacional vigente ("batch no talo, busca espera") já
  reconhece a busca como frágil. Particionar **agrava** o pior caso da busca pra ganhar
  pouco na escrita.

**Veredito do trade-off:** particionar troca um ganho de escrita *marginal e não-alvo*
por uma piora de busca *real e alinhada com um gargalo já sofrido*. Trade ruim.

## 5. Comparação com as alternativas

| Alternativa | Ataca a causa medida (I/O cache-miss)? | Custo/risco | Recomendação |
|---|---|---|---|
| **Drop `chunk_doc_ordinal_idx`** (0 scans, 1,5GB) | libera 1,5GB de cache + tira 1 índice do write path | ~zero (`DROP INDEX CONCURRENTLY`), reversível | **Fazer já** (fase 0) |
| **Réplica read-only** (streaming) | **sim** — tira a busca (retrieval + hybrid) do primário; primário só escreve, réplica cacheia 54GB HNSW p/ leitura | 1 host ~64GB+; lag irrelevante p/ busca semântica; **não** acelera ingestão | **Alto valor** — resolve a dor da busca de vez |
| **Mais RAM no host do DB** (94→128/192GB) | **sim** — mantém HNSW+GIN+TOAST quente residente, mata o disk-cliff de insert E de busca | 1 upgrade de VM; simples, reversível | **Alto valor**, barato |
| **pgvectorscale / StreamingDiskANN** | **sim, direto** — índice *desenhado* pra ser disk-resident + SBQ (quantização); RRF numa query SQL | troca extensão (licença PostgreSQL, self-host); build/migração de índice | **Se após RAM ainda apertar** |
| **VectorChord** | idem (reusa coluna vector, ~6-14x insert) | `shared_preload_libraries`+restart+superuser+AGPL/ELv2 | alternativa a pgvectorscale |
| **Disco NVMe mais rápido** | **sim** — o gargalo é random read de índice; NVMe corta `r_await` | depende de o backing QEMU já ser SSD (iostat mostra r_await ~0,7-1ms, já rápido) | ganho incerto; medir antes |
| **index-last** (insert sem HNSW, build em lote) | ajuda no **backfill**, não no steady-state live | conflita com acervo LIVE (busca quebra sem índice) → exigiria particionar quente/frio | só no backfill inicial |
| **Particionar** | **não** ataca a causa medida; **piora** busca | rebuild HNSW 2-4h congelado + risco recall/latência | **não agora** |

Observação sobre o disco: o `iostat` do `zordon-db` mostra `r_await 0,65-1,02ms` a
18k reads/s / ~195MB/s com **~50% de utilização** já sob a carga atual — latência
classe-SSD (apesar do flag `rotational=1` do QEMU). Ou seja, **não é HDD lento**; é
**volume de random read** batendo em ~50% util. Isso reforça: o lever é **reduzir
cache-miss** (mais RAM / índice menor via quantização / réplica), não geometria de
tabela.

## 6. Riscos do particionamento (se ainda assim for tentado)

1. **Rebuild HNSW 2-4h de escrita congelada**, com histórico de reboot acidental da VM
   matando builds longos.
2. **Regressão de busca**: fan-out N-way sem filtro de partição → recall cai ou
   latência/`ef_search` multiplica; a busca já trava >40s sob batch hoje.
3. **Chave sem boa opção**: `hash(documento_id)` (melhor p/ escrita) é a pior p/ busca;
   temporais exigem desnormalizar ou são instáveis sob re-vetorização.
4. **Migração big-bang de 372GB** (288GB TOAST) — cópia longa, janela grande, DDL pesado.
5. **Complexidade permanente**: pg_partman/manutenção de partições, planner com N scans,
   monitoramento por-partição — mais peças móveis, contra o princípio "menos peças" do
   projeto (time de ops pequeno).
6. **Não resolve o full-corpus** por si só: 1,15M processos gerariam índice muito além de
   qualquer RAM; ali o caminho é quantização (pgvectorscale/CAGRA), não só cortar em N.

## 7. Recomendação final (roadmap)

1. **Fase 0 (hoje, ~zero risco):** `DROP INDEX CONCURRENTLY chunk_doc_ordinal_idx`
   (0 scans, 1,5GB) — libera cache e tira um índice do write path. Confirmar que
   `acervo_chunk_documento_id_...` (btree simples) cobre o `DELETE ... WHERE documento_id`
   (cobre — 1,66M scans registrados).
2. **Fase 1 (dor imediata da busca):** **réplica read-only** streaming — roteia
   `hybrid_search`/dossiê/chat pra réplica; primário fica só com escrita+retrieval de
   extração. Separa os dois workloads que hoje colidem no disco. (Não acelera ingestão,
   mas cura a busca — que é a dor operacional aguda.)
3. **Fase 2 (destravar ingestão sem trocar extensão):** **RAM no host do DB** (94→128GB+)
   pra manter HNSW(54)+GIN(17)+hot TOAST residentes; sobe `shared_buffers` proporcional.
   Mata o `DataFileRead` de insert na origem.
4. **Fase 3 (se o índice não couber em nenhuma RAM viável / full-corpus):**
   **pgvectorscale/StreamingDiskANN** ou **VectorChord** — índice disk-resident +
   quantização, RRF numa query SQL. Reduz o índice quente e o custo de insert sem N-way
   fan-out.
5. **Fase 4 (condicional, só no full-corpus 1,15M):** aí sim reavaliar particionamento —
   mas casado com quantização/CAGRA, não como primeira medida.

**Particionar `acervo_chunk` não é a alavanca certa agora.** O gargalo medido é I/O de
cache-miss (não extend-lock), e particionar troca ganho de escrita marginal por
regressão de busca real, ao custo de horas de rebuild de HNSW congelado. RAM + réplica +
(depois) pgvectorscale entregam mais, com menos risco e menos peças móveis.

---

### Anexo — como medir de novo (read-only)

```bash
# do laptop:
ssh ubuntu@zordon 'set -a && . ~/zordon/.env && set +a; \
  PGPASSWORD="$ZORDON_DB_PASSWORD" psql -h "$ZORDON_DB_HOST" -p 5432 -U "$ZORDON_DB_USER" -d "$ZORDON_DB_NAME" -c "<SQL>"'
# host do DB (RAM/disk): ssh ubuntu@zordon 'ssh ubuntu@192.168.30.114 "free -g; iostat -dx 2 2"'
```
- Tamanhos: `pg_relation_size` / `pg_total_relation_size` / `pg_get_indexdef` sobre
  `pg_index`/`pg_class` (§1).
- Contenção de escrita: amostrar `pg_stat_activity` (wait_event `extend` vs
  `DataFileRead` vs `Lock/transactionid`) a cada 2s (mostrou extend=0, DataFileRead 3-6).
- Índice morto: `pg_stat_user_indexes.idx_scan` (mostrou `chunk_doc_ordinal_idx`=0).
- Cache-hit por índice: `pg_statio_user_indexes` (HNSW hit ~95% → ~5% disco).
