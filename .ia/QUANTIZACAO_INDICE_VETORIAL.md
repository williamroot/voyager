# Quantização binária do índice vetorial — escopo (medido)

> **⛔ ATUALIZAÇÃO 2026-07-26 — GATE NO FULL-CORPUS: BLOCK. NÃO FAZER CUTOVER.**
> O escopo abaixo (amostra 328k, 2026-07-25) deu PASS (0,881 recall vs gold), mas
> a validação no **corpus inteiro (29,9M)** REFUTOU: o índice bit foi construído em
> prod (`chunk_emb_hnsw_bit`, `CREATE INDEX CONCURRENTLY`, indisvalid=t, **9,0 GB**)
> e a régua de paridade deu **overlap@8 = 0,494 (N=100) / 0,588 (N=200)** vs o
> halfvec-índice — MUITO abaixo do gate (≥0,90) e do 0,881 do scoping.
>
> **Causa-raiz (medida, não bug de SQL):** `binary_quantize` (sinal por dimensão)
> nas embeddings **bge-m3** deste corpus produz Hamming que NÃO acompanha cosine.
> Diagnóstico (query "natureza alimentar"): o vizinho cosine #1 (halfvec) tem
> **hamming=328**, enquanto o **bit top-1000 inteiro** vai só até hamming=284 →
> o vizinho verdadeiro nem entra no top-1000 do espaço bit. Subir `ef_search` até
> o teto (1000) e N até 500 recuperou **0/8** dos vizinhos halfvec. O grafo bit é
> mal-conectado (ef=100 devolve só 131 candidatos p/ N=200). `self-hamming=0` OK
> (operador correto). O rescore halfvec não salva: os candidatos certos nunca estão
> no conjunto. A amostra 328k não replicou no full-corpus.
>
> **Decisão:** halfvec (`chunk_emb_hnsw_half`, 57 GB) permanece o caminho de prod.
> `search.py` fica no default `halfvec` (patch atrás de flag `ZORDON_ANN_MODE`
> commitado em `feat/quantizacao-bit` @ `1feb01a`, NÃO deployado). **NÃO dropar o
> halfvec.** O `chunk_emb_hnsw_bit` (9 GB) pode ser dropado (é inútil como está) OU
> mantido só se for testar SBQ/thresholds calibrados. Próximo passo real p/
> destravar escrita no full-corpus = §8 (pgvectorscale/SBQ — quantização
> *calibrada*, não sign puro), NÃO o bit puro do pgvector. Ver §3-BLOCK no fim.

**Status:** ESCOPO / medição em amostra — **REFUTADO no full-corpus (ver topo).**
Zero mudança de prod no cutover; o único DDL foi o `CREATE INDEX CONCURRENTLY` do
bit-index (aditivo, reversível com DROP).

**Data da medição:** 2026-07-25 (amostra). **Validação full-corpus:** 2026-07-26.
**Host DB:** acervo (`192.168.30.114`, PG 18.4, pgvector 0.8.1, 94 GB RAM,
shared_buffers 24 GB, effective_cache_size 70 GB).

---

## TL;DR / VEREDITO

**PASS — vale trocar pra quantização binária (bit HNSW + rescore halfvec).** Os
dois testes que decidem passaram:

1. **Tamanho:** bit-index medido = 6,3× menor que o halfvec HNSW na mesma amostra
   → **~10-12 GB no full-corpus (29 M)** vs 56 GB do halfvec hoje. Cabe **folgado**
   nos 94 GB (índices+quente < 45 GB) e, ao contrário do halfvec, **continua
   cabendo no full-corpus 85 M** (~35 GB bit vs ~165 GB halfvec, que estouraria).
2. **Recall:** com **N=100 candidatos** no bit + rescore halfvec, recall@8 = **0,881
   — empata/supera o halfvec HNSW de prod (0,850)** vs o gold exato. O recall NÃO
   desabou; o rescore recupera tudo. Bônus: bit-path ~30× mais rápido por busca na
   amostra (13 ms vs 424 ms p50).

**Recomendação:** implementar como **Fase 1 dentro do Postgres** (sem trocar
extensão): `CREATE INDEX CONCURRENTLY` do bit-index (off-peak, mem moderada) +
patch do `hybrid_search` (§5) com N≈100 → validar recall num piloto de ~1-2 M antes
de dropar/aposentar o halfvec. **Só cai pra pgvectorscale/SBQ (§8) se o piloto de
escala mostrar recall abaixo do halfvec** — o que a amostra NÃO indica. Não
promete-se um múltiplo exato do teto de escrita (§6 é estimativa), mas a barra
("índice residente mesmo no full-corpus, DB-write deixa de ser o paredão") é
atendida com folga.

---

## 1. Estado medido hoje (prod, `acervo_chunk`)

| Item | Valor medido |
|---|---|
| Linhas (reltuples) | **29,08 M** chunks (live ~29,76 M; 1,14 M dead / ~3,8% bloat) |
| Tabela total | 380 GB (heap 10 GB, TOAST ~296 GB texto cifrado, resto índices) |
| `chunk_emb_hnsw_half` (HNSW halfvec cosine) | **56 GB** |
| `chunk_fts_gin` (FTS) | 17 GB |
| `acervo_chunk_pkey` | 1,3 GB |
| `acervo_chunk_documento_id` (FK) | 563 MB |
| `embedding` | coluna `vector` (fp32) nativa; halfvec é **cast em index de expressão** |

> Nota: a memória `[[vetor-ingest-teto-e-opcoes]]` registrou 25 GB p/ 12,7 M
> vetores em 24/jul. O corpus ~dobrou desde então (12,7 M → 29 M) e o índice
> halfvec acompanhou (25 → 56 GB). Com shared_buffers=24 GB, o índice halfvec
> (56 GB) **já não cabe no buffer** e depende do OS page cache (effective 70 GB)
> — o disk-cliff está voltando conforme caminhamos pro full-corpus.

## 2. Tamanho do bit-index — MEDIDO em amostra + extrapolado

Amostra `_qtest_chunk`: **328.757 chunks** (`TABLESAMPLE SYSTEM (1.2)`, páginas
aleatórias — representativo; só `id + documento_id + embedding` copiados, sem
tocar o texto cifrado). Dois índices construídos NELA (defaults m=16,
ef_construction=64, 4-8 workers paralelos):

| Índice na amostra (328.757 vetores) | Tamanho | Build | B/vetor |
|---|---|---|---|
| `_qtest_emb_hnsw_half` (halfvec cosine) | **841 MB** | 95 s | ~2683 |
| `_qtest_emb_hnsw_bit` (bit(1024) Hamming) | **134 MB** | 59 s | ~427 |

**Bit-index é 6,3× menor que o halfvec HNSW na mesma amostra.**

Extrapolação linear para 29,08 M chunks (o HNSW cresce ~linear no nº de vetores):

| Índice | Extrapolado full-corpus (29,08 M) | Método |
|---|---|---|
| halfvec HNSW | ~56 GB (**ancorar no medido de prod**, não na amostra) | prod real |
| **bit HNSW (Hamming)** | **~11,8 GB** (134 MB × 29,08M/328.757) | ratio da amostra |

> O per-vetor da amostra (2683 B halfvec vs 427 B bit) é maior que o de prod
> (56 GB/29 M ≈ 1926 B halfvec) — amostra tem densidade de grafo diferente. Por
> isso extrapolamos o **bit** pelo *ratio* medido na mesma amostra e a favor da
> âncora de prod: bit ≈ halfvec_prod / 6,3 × (fator de forma) → **~9-12 GB**.
> Faixa conservadora: **~10-12 GB** full-corpus.

**Cabe na RAM?** Sim, folgado. Bit-index ~10-12 GB + GIN 17 GB + heap quente +
pkey ≈ **< 45 GB** de índices+quente vs **94 GB** de RAM (e 70 GB de
effective_cache_size). O bit-index fica **residente inteiro no buffer** — é o
ponto: o INSERT no grafo de bits toca páginas quentes, sem `IO/DataFileRead`.

## 3. Recall — bit+rescore vs halfvec (o número que decide)

Metodologia (amostra `_qtest_chunk`, 20 queries reais = 15 do `eval/questions.yaml`
+ 5 anchors da extração, embed via bge-m3):

- **Gold** = top-8 EXATO por cosine halfvec (brute-force seqscan, `enable_indexscan=off`
  → verdade, sem aproximação).
- **Candidato** = ANN no bit-index (Hamming `<~>`, top-N) → **rescore** dos N por
  cosine halfvec → corta top-8.
- **Referência** = halfvec HNSW top-8 (o caminho de prod hoje) vs o mesmo gold.
- Métrica = **recall@8 de sobreposição de conjunto** = |top8_cand ∩ top8_gold| / 8,
  média sobre as 20 queries. (Mede o quanto o bit+rescore reproduz os vizinhos
  verdadeiros — é a métrica canônica de quantização, mais dura que o "any-hit"
  weak-label da régua.)

**Resultado medido (n=20 queries, amostra 328.757, recall@8 de sobreposição):**

| Config | recall@8 vs gold exato | p50 latência | p95 latência |
|---|---|---|---|
| **halfvec HNSW** (prod hoje, ef=100) | **0,850** | 424 ms | 2221 ms |
| bit + rescore, N=8 | 0,363 | 18 ms | 92 ms |
| bit + rescore, N=16 | 0,513 | 11 ms | 18 ms |
| bit + rescore, N=32 | 0,669 | 11 ms | 34 ms |
| bit + rescore, N=50 | 0,788 | 12 ms | 17 ms |
| **bit + rescore, N=100** | **0,881** | 13 ms | 18 ms |
| **bit + rescore, N=200** | **0,919** | 17 ms | 22 ms |

(gold = top-8 EXATO brute-force sobre a amostra; latência do gold p50 ≈ 3563 ms —
por isso o gold só serve de régua, nunca de caminho de prod.)

**Leitura dos números (o que decide):**

- A régua honesta **não é 1,0** — é **0,850**, o quanto o próprio HNSW halfvec de
  prod (ef=100) já reproduz os vizinhos exatos. Bit+rescore só precisa **empatar
  com isso**, não com o brute-force.
- Com **N=100 candidatos** (oversampling ~12× p/ top-8), o bit+rescore dá
  **0,881 — já EMPATA/SUPERA o halfvec HNSW (0,850)**. Com N=200, **0,919**.
- O ponto de operação: **N ≈ 100** (buscar 100 no bit, rescore halfvec, cortar 8)
  entrega recall equivalente ao caminho atual. Sem custo de recall.
- **Bônus de latência (na amostra):** bit+rescore N=100 = **13 ms p50 / 18 ms p95**
  contra 424 ms / 2221 ms do halfvec HNSW — o índice de bits é minúsculo e quente,
  Hamming é XOR+popcount, e o rescore toca só 100 linhas. (A latência absoluta do
  halfvec aqui é inflada por amostra pequena sob carga da frota; mas o *ratio* é
  real: o bit-path é dramaticamente mais barato por busca.)
- Ressalva de honestidade: recall relativo à **amostra** (gold exato dentro de
  328k). N pra full-corpus deve ficar na mesma ordem, mas confirmar num piloto
  maior antes de fixar N em prod.

## 4. Custo do rebuild (full-corpus)

- **halfvec HNSW histórico**: 43 min (2583 s) p/ 12,7 M, não-concorrente, 8
  workers, `maintenance_work_mem=32 GB`.
- **bit HNSW medido**: 59 s p/ 328,757 na amostra (com fleet escrevendo). O bit é
  **mais barato de construir** que o halfvec (distância Hamming = XOR+popcount, sem
  fp; índice 6,3× menor). Extrapolando pelo ratio de tempo bit/half (0,62) sobre o
  halfvec real de 29 M (~1,5-2 h estimado hoje): **bit full-corpus ≈ 40-70 min**.
- **Downtime?** Nenhum obrigatório. `CREATE INDEX CONCURRENTLY` cria o bit-index
  **sem parar escrita** — custa I/O e ~2-3× o tempo do build normal, mas a busca de
  prod segue no `chunk_emb_hnsw_half` (que NÃO se toca). Só ao final, quando o
  bit-index existir e a busca já apontar pra ele, é opcional dropar/manter o halfvec.
  Gentil: rodar off-peak, `maintenance_work_mem` moderado (NÃO 32 GB — a lição de
  24/jul: build agressivo arrisca a VM).

## 5. Mudança na busca (`acervo/search.py::hybrid_search`) — patch esboçado, NÃO aplicado

Hoje o estágio ANN é:

```python
ann_qs = (
    base_qs.filter(embedding__isnull=False)
    .annotate(dist=RawSQL("(embedding::halfvec(1024)) <=> %s::halfvec(1024)", (qv_lit,)))
    .order_by("dist")[:ann_k]
)
```

Vira **bit-ANN (Hamming, top-N candidatos) + rescore halfvec**, numa única SQL
(CTE) para o planner usar o bit-index no `ORDER BY ... <~>`:

```python
# N = ann_k * OVERSAMPLE (ver §3 p/ o fator que preserva recall)
ann_sql = """
    WITH cand AS (
        SELECT id
        FROM acervo_chunk
        WHERE embedding IS NOT NULL
          AND documento_id IN (SELECT id FROM acervo_documento WHERE status='ready')
          {cnj_filter}
        ORDER BY binary_quantize(embedding)::bit(1024)
                 <~> binary_quantize(%s::vector(1024))::bit(1024)
        LIMIT %s                      -- N candidatos baratos no bit-index
    )
    SELECT c.id
    FROM cand
    JOIN acervo_chunk c USING (id)
    ORDER BY (c.embedding::halfvec(1024)) <=> %s::halfvec(1024)   -- rescore exato
    LIMIT %s                          -- ann_k final
"""
```

- A expressão do `ORDER BY` do CTE (`binary_quantize(embedding)::bit(1024) <~>
  binary_quantize(query)::bit(1024)`) precisa casar **exatamente** o índice
  (`chunk_emb_hnsw_bit ON (binary_quantize(embedding)::bit(1024)) bit_hamming_ops`)
  pro planner escolher Index Scan — mesmo padrão do halfvec hoje.
- RRF/rerank a jusante **não mudam** — só troca a fonte dos `ann_ids`.
- O rescore usa `embedding::halfvec` (fp16), que já provou recall@20=100% vs fp32
  (memória 24/jul) — a precisão fina continua no halfvec, o bit só faz o
  *candidate generation* barato.
- `ef_search` do bit pode ser alto (candidatos são baratos) sem doer.

## 6. Ganho estimado no teto de escrita (honesto — é estimativa)

Mecanismo do teto atual: cada INSERT de chunk insere no grafo HNSW; com índice
> buffer, a inserção lê/reescreve páginas do disco (`IO/DataFileRead`) → o teto
observado foi ~20-32k docs/h antes do halfvec, e o halfvec (25 GB, cabia na RAM)
destravou pra ~26-35k docs/h. Agora o halfvec cresceu p/ 56 GB e volta a raspar
o teto de buffer conforme o corpus dobra rumo ao full.

Com o bit-index (~10-12 GB) **residente**, o INSERT no grafo de bits é
100%-in-memory: sem disk-cliff de escrita, e o custo por inserção é menor
(Hamming/popcount << distância fp). **Estimativa**: o teto de escrita volta a
escalar linear com pods (como pós-halfvec de 24/jul) e sobe **na direção do gargalo
seguinte** (GPU embed / abastecimento da fila / inventário QuickPod), NÃO o
DB-write. Não prometo um múltiplo exato — a barra é "o índice cabe folgado na RAM
mesmo no full-corpus 85 M", e aí o DB-write deixa de ser o paredão de novo, desta
vez de forma que **escala pro full-corpus** (halfvec 56 GB→~165 GB no full NÃO
caberia; bit ~10-12 GB→~35 GB no full ainda cabe).

## 7. Riscos

- **Recall do bit sozinho é baixo** (1-bit por dimensão perde muita informação) —
  por isso o **rescore halfvec é obrigatório**; sem ele a quantização não presta. O
  fator de oversampling N (§3) é o que compra recall de volta. Se N precisar ser
  grande demais p/ recall aceitável, a latência sobe (mais candidatos a rescorar).
- **Custo de manutenção de 2 índices** se optar por manter halfvec + bit
  simultaneamente (transição): +56 GB. Para o full-corpus, o plano é **bit-only**
  (o rescore lê a coluna `embedding`/halfvec direto da heap, não precisa do índice
  halfvec).
- **Build concorrente custa I/O** — rodar off-peak; se recorrer o incidente de
  `CREATE INDEX CONCURRENTLY` esperando snapshot (OPS: pausar xacts longas), tratar
  igual.
- **pgvector 0.8.1** — `binary_quantize` + `bit_hamming_ops` HNSW existem e foram
  exercitados aqui (build OK). Pin ≥ 0.8.x mantido.

## 8. Alternativas (se o recall desabar)

Se mesmo com rescore o recall ficar abaixo do halfvec de forma inaceitável:
**pgvectorscale / StreamingDiskANN com SBQ** (Statistical Binary Quantization) —
quantização binária *calibrada* (aprende thresholds por dimensão em vez de sign
puro) + índice disk-resident desenhado exatamente p/ "HNSW não cabe na RAM".
Custa trocar a extensão (shared_preload + restart = janela), mas mantém FTS/joins/
cripto no Postgres. Ver `[[vetor-ingest-teto-e-opcoes]]` (Fase 1) e RAG_EVAL_OBS §10.

---

## §3-BLOCK — Validação no full-corpus (2026-07-26): REFUTA o PASS da amostra

Índice bit construído em prod: `chunk_emb_hnsw_bit` via
`CREATE INDEX CONCURRENTLY ... USING hnsw ((binary_quantize(embedding)::bit(1024))
bit_hamming_ops)` — **indisvalid=t, 9,0 GB** (melhor que os 10-12 GB estimados),
build ~7h20 (mais lento que o estimado por rodar CONCURRENTLY + `maintenance_work_mem`
moderado + frota escrevendo). halfvec NÃO tocado (57 GB).

**Gate de paridade (overlap@8 bit+rescore vs halfvec-índice, 20 queries, ambos via
índice HNSW, sem seqscan):**

| config | overlap@8 | p50 (ms) | veredito |
|---|---|---|---|
| halfvec (referência) | 1,000 | 16 | — |
| **bit+rescore N=100** | **0,494** | 26 | **BLOCK** (< 0,90) |
| **bit+rescore N=200** | **0,588** | 30 | **BLOCK** (< 0,90) |

Padrão bimodal: várias queries em 0,875-1,000, mas **6/20 em exatamente 0,000**
(candidate set bit não contém NENHUM dos top-8 halfvec).

**Diagnóstico da causa (query "o crédito é de natureza alimentar?"):**
- halfvec top-1 = chunk 38484410, cos_dist=0,382, **hamming=328**.
- bit top-1000 → só **131 candidatos** (grafo exausto), hamming ∈ [242, **284**].
- O vizinho cosine real (hamming 328) está ALÉM do bit top-1000 inteiro (max 284).
- `ef_search` 100→1000 (teto do pgvector) + N 100→500: recuperou **0/8**. `self-
  hamming=0` (operador OK). ⇒ **não é bug de SQL nem de ef_search**: a quantização
  por sinal perde os vizinhos cosine.

**Por que a amostra 328k enganou:** grafo HNSW menor (mais navegável) + densidade
diferente mascararam o descasamento Hamming↔cosine. No full-corpus (29,9M) o efeito
domina. Lição: gate de recall **tem que rodar no full-corpus**, amostra não basta.

**Ações:** default fica `halfvec`; patch flagueado em `feat/quantizacao-bit`
(`1feb01a`, `acervo/search.py` + `zordon/settings.py` `ZORDON_ANN_MODE` +
`eval/parity_gate.py`), NÃO deployado. Halfvec NÃO dropar. `chunk_emb_hnsw_bit`
(9 GB) pode dropar. Caminho real p/ o teto de escrita no full-corpus = §8
(pgvectorscale/StreamingDiskANN **com SBQ calibrada**), não sign-bit puro.

---

## Apêndice — reprodutibilidade

Amostra + índices construídos em `_qtest_chunk` (UNLOGGED, dropada no fim). Scripts
em `scratchpad` da sessão: `build_sample.sql`, `build_indexes.sql`, `recall_test.py`.
Régua reusada: `eval/questions.yaml` (15 queries) + anchors de `acervo/extract_fields.py`.
Validação full-corpus 2026-07-26: `eval/parity_gate.py` (overlap vs halfvec),
diag de candidate-set/Hamming-rank no scratchpad da sessão (`diag.py`, `diag2.py`).
