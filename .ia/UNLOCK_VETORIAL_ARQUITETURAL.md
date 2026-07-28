# Unlock DURÁVEL do teto de escrita da vetorização — doc de decisão (arquitetural)

> **Status:** ESCOPO / decisão. **NADA implementado.** Read-only. Todas as medições
> de estado abaixo colhidas ao vivo em **2026-07-27** contra `zordon-db`
> (`ssh ubuntu@zordon` → `192.168.30.114`), sem tocar índice/writer.
>
> **Escopo:** o próximo degrau é ARQUITETURAL — as duas tentativas de baratear o
> índice *dentro do Postgres* falharam (ver §0). Este doc avalia caminhos que saem
> do regime "HNSW-na-RAM do pgvector" e **recomenda 1**, com plano faseado
> (POC → gate → cutover reversível) e o mesmo gate implacável que pegou os 2 FAILs.

---

## TL;DR — VEREDITO (cético)

**Recomendação: LanceDB como store vetorial dedicado, embarcado e disk-first (Caminho 1),
atrás do MESMO gate de recall (overlap@8 ≥ 0,90 no corpus cheio) e com o halfvec
intacto como fallback até o canário passar.** É o único caminho que (a) tira o índice
do regime RAM-bound de forma que **escala pro full-corpus 85M** sem tocar o contrato
de embedding (bge-m3 1024d fica), (b) preserva a busca híbrida (FTS/RRF/rerank), e (c)
é reversível por flag. Não é grátis (2º store + sync + a busca híbrida passa a cruzar
2 sistemas), mas é o degrau honesto depois de dois FAILs dentro do PG.

**A pergunta destacada — "truncar bge-m3 <930d ressuscita o pgvectorscale 2-bit?" —
tem resposta CÉTICA: provavelmente NÃO vale.** Achado que muda tudo:
**bge-m3 NÃO foi treinado com Matryoshka/MRL** (dense é fixo 1024d, sem front-loading
de informação nas primeiras dimensões — [BAAI/bge-m3 discussão](https://huggingface.co/BAAI/bge-m3/discussions/57)).
Truncar in-place 1024→768 é barato (só re-indexar), mas **não tem garantia teórica de
recall** — cortar dimensões de um modelo não-MRL joga fora informação distribuída, não
redundante. E mesmo se truncar preservasse recall, só reabilita o **SBQ 2-bit** — que
**nunca foi provado passar neste corpus** (bge-m3 já reprovou 1-bit sign-puro com
overlap@8 0,49; §0). Seriam **dois saltos de fé em série** (truncagem preserva recall
E SBQ-2bit passa). É medível e barato de tentar como *side-quest* (§4), mas **não é a
aposta principal** — a prior é ruim demais pra bloquear o caminho durável nele.

---

## §0. Ponto de partida — o que já se sabe (não re-litigar)

| Fato | Fonte |
|---|---|
| Teto de escrita = DB I/O-bound: índice HNSW halfvec **58 GB > RAM útil do host** → `IO/DataFileRead` em cada INSERT no grafo | `QUANTIZACAO_INDICE_VETORIAL.md`, `PARTICIONAMENTO_ACERVO_CHUNK.md` |
| Quantização binária ingênua (`binary_quantize`::bit + Hamming): **BLOCK**, overlap@8 **0,49/0,59** no full-corpus (amostra 328k mentiu 0,881). Causa medida: **sign-bit não acompanha cosine no bge-m3** — o vizinho verdadeiro nem entra no top-1000 do espaço bit; rescore não salva | `QUANTIZACAO_INDICE_VETORIAL.md` §3-BLOCK |
| pgvectorscale StreamingDiskANN + SBQ: **FAIL ESTRUTURAL** — SBQ **2-bit impossível >900 dims** (bge-m3=1024d), só 1-bit permitido = o regime já reprovado. `meta_page.rs:326`. Extensão instalada mas **inerte** | `PGVECTORSCALE_SBQ.md` (FAIL no topo) |
| Particionar `acervo_chunk`: **NÃO** — extend-lock=0 medido, gargalo é cache-miss de I/O; particionar piora a busca (fan-out N-way) | `PARTICIONAMENTO_ACERVO_CHUNK.md` |

**Estado ao vivo (2026-07-27):**
- `acervo_chunk` = **31,0M chunks** (reltuples 30.987.856), crescendo p/ ~85M no full-corpus.
- Índices: `chunk_emb_hnsw_half` **58 GB** (idx_scan=0 — busca estava em janela ociosa),
  `chunk_fts_gin` **17 GB**, `pkey` 1,4 GB, `documento_id` 584 MB. TOAST texto cifrado ~288 GB.
- Host DB: **76 GB RAM** (`free`: total 76, buff/cache 74, available ~50), 18 vCPU,
  `shared_buffers=24 GB`. **Índice halfvec (58 GB) já não cabe folgado + TOAST quente** → o disk-cliff.
- Extensões: `vector 0.8.1`, **`vectorscale 0.9.0` (instalada, INERTE)**, `plpgsql`.
- Embedding: **bge-m3 1024d**, via `acervo/embeddings.py::embed_text` (Ollama local,
  `ZORDON_EMBED_MODEL=bge-m3`, fail-closed host-local).
- Busca: `acervo/search.py::hybrid_search` — **FTS (GIN pt-BR) + ANN (halfvec HNSW) →
  RRF (k0=60, sobre RANKS 0-based de duas listas de pk) → rerank cross-encoder
  (bge-reranker-v2-m3, sidecar local)**. Flag `ZORDON_ANN_MODE` (halfvec|bit) já existe;
  o estágio ANN só produz `ann_ids = [(pk, pos)]` que entram no RRF — **é o único ponto
  a trocar.** `cnj` é filtro opcional; **o caso comum NÃO filtra**.

**Régua que vale (a que pegou os 2 FAILs):** `overlap@8 ≥ 0,90` do candidato vs o
halfvec-índice incumbente, **20 queries reais** (`eval/questions.yaml` + anchors),
**no corpus cheio** (nunca amostra). PASS<0,90 = BLOCK. É a régua deste doc também.

---

## §1. Caminho 1 — Store vetorial dedicado fora do Postgres (LanceDB / Qdrant / Milvus)

**Ideia:** o vetor sai do Postgres pra um store colunar/on-disk desenhado pra "índice
não cabe na RAM". O PG mantém `acervo_chunk` (texto cifrado no TOAST + `search_vector`
FTS + metadados/joins/`documento__status`/`numero_cnj`). A busca vira: FTS no PG + ANN
no store externo → **RRF cruzando os dois** → rerank (inalterado).

### 1a. LanceDB (RECOMENDADO dentro deste caminho — o mais leve)

**O que resolve o teto de escrita:** LanceDB é **embarcado** (biblioteca, sem servidor
stateful — zero 2º daemon a operar), formato **Apache Arrow/Lance colunar on-disk**,
índice **IVF_PQ** (product quantization, *não* sign-bit) **disk-native** — a busca lê do
disco a velocidade ~in-memory (memory-map + SIMD), **o índice não precisa caber na RAM**.
Escrita = append colunar desacoplado, sem grafo HNSW global reescrito por linha → **o
paredão RAM-bound do INSERT no HNSW simplesmente deixa de existir**. Provado a escala:
casos públicos com **700M vetores em prod** e alvo de 10–50B linhas
([Scaling LanceDB 700M](https://sprytnyk.dev/posts/running-lancedb-in-production/),
[LanceDB FAQ](https://docs.lancedb.com/faq/faq-oss)). Nossos 31M→85M ficam bem dentro.

**Quanto sobe o teto:** o teto sai do "58GB>RAM" e passa a ser **disco/CPU de PQ + o
abastecimento da fila / GPU de embed** — o gargalo seguinte que a memória
`vetor-ingest-teto-e-opcoes` já apontava. Não prometo múltiplo exato (medir no POC),
mas a barra ("índice não RAM-bound, escala pro full-corpus 85M") é atendida por design.

**Impacto na busca híbrida (o risco central):** LanceDB **faz hybrid nativo**
(BM25/Tantivy + vetor + **RRFReranker** + cross-encoder plugável —
[LanceDB Hybrid Search](https://docs.lancedb.com/search/hybrid-search)), MAS **não
usamos o hybrid interno dele**: nosso FTS é **pt-BR no GIN do Postgres** (com o texto
cifrado só decifrável app-side) e nosso rerank é o bge-reranker local. **O desenho que
preserva tudo:** LanceDB serve **só o estágio ANN** (retorna `[(chunk_id, rank)]`), e o
`hybrid_search` funde no RRF do PG **exatamente como hoje** — o RRF opera sobre
*ranks de pk*, é agnóstico à origem do ANN. FTS/RRF/rerank/cripto **não mudam**. É a
troca de menor superfície possível: onde hoje o `ann_ids` sai do halfvec, passa a sair
de uma chamada LanceDB por `chunk_id`. **Não quebra o RRF+rerank+FTS.**

**Custo de manter 2 sistemas + sync:** é o preço real. Precisa de:
- **Sync de escrita:** todo `write_chunks_indexed` (delete+insert por documento) tem que
  espelhar o vetor no LanceDB (upsert por `chunk_id`, delete por `documento_id`). Idempotente,
  mas é write-path novo. Risco: **drift PG↔Lance** se um lado falhar → precisa reconciliação
  (job que compara contagens/ids). Um vetor a menos no Lance = recall silenciosamente pior.
- **Sem cripto nativa:** o embedding não é PII (é derivado), mas confirmar política de
  soberania — o **texto** fica cifrado no PG; o Lance guarda só vetor+chunk_id (aceitável).
- **Metadados de filtro:** `documento__status='ready'` e `cnj` hoje são JOIN no PG. No
  desenho "Lance só ANN", o filtro `ready`/`cnj` teria que (i) ser pré-filtro no PG que
  gera uma allowlist de ids (caro no caso sem cnj) OU (ii) desnormalizar `status`/`cnj`
  no Lance e filtrar lá. **(ii) é o certo** — Lance filtra por metadado on-disk barato.
  Isso duplica `status`/`numero_cnj` no Lance (mais sync).

**Esforço/risco:** MÉDIO. Biblioteca embarcada (sem infra nova pesada), mas write-path
de sync + desnorm de filtros + reconciliação é trabalho real. Risco de recall do IVF_PQ:
PQ é quantização **diferente** do sign-bit que falhou (aprende centróides por subvetor,
não sinal por dimensão) + **refine/rescore com vetor cheio** (`refine_factor`) — ataca a
causa-raiz medida. LanceDB reporta >0,95 recall com refine em benchmarks
([IVF_PQ benchmark](https://www.lancedb.com/blog/benchmarking-lancedb-92b01032874a-2)),
mas **em bge-m3/nosso corpus é HIPÓTESE — o gate decide** (a lição dos 2 FAILs).

**Reversibilidade:** ALTA até o cutover. Escreve-se no Lance em paralelo (dual-write),
mede-se o gate, e só flipa a flag `ZORDON_ANN_MODE=lance` quando passar. Halfvec fica
intacto → rollback = flip de volta. Só o DROP final do halfvec é irreversível (e vem por
último, após canário). PG continua fonte de verdade do vetor (coluna `embedding` fica) →
pode reconstruir o Lance a qualquer momento.

### 1b. Qdrant / Milvus (alternativas mais pesadas — não recomendadas primeiro)

- **Qdrant:** servidor stateful (2º daemon a operar), BQ com rescore que a doc diz
  funcionar **≥1024d** ([Qdrant BQ](https://qdrant.tech/articles/binary-quantization/)).
  **Ceticismo forte:** o "≥1024d funciona" da Qdrant é medido com embeddings *desenhados
  pra quantizar* (OpenAI text-embedding-3). **bge-m3 já reprovou sign-bit BQ neste
  corpus** — o algoritmo de BQ da Qdrant é o mesmo sign-bit; o que muda é o rescore de
  vetor-cheio-em-disco + grafo melhor. Pode ajudar, mas **é o mesmo quantizador que
  falhou** — a Qdrant não tem mágica sobre a distribuição do bge-m3. Se for testar
  Qdrant, testar com **scalar/PQ quantization**, não BQ. Peso operacional > LanceDB.
- **Milvus:** etcd + MinIO + (opcional) k8s — **pesado demais** pro time de ops pequeno /
  on-prem. Só entra se formos pra **GPU-CAGRA** (§3) e quisermos o Milvus-GPU como runtime.

**Veredito §1:** **LanceDB** é o candidato — embarcado (mínimas peças móveis, alinhado ao
princípio do projeto), disk-first (mata o RAM-bound), PQ+refine é quantizador diferente
do que falhou, e preserva o RRF+FTS+rerank porque serve só o ANN.

---

## §2. Caminho 2 — Truncar/re-embedar bge-m3 <930d (Matryoshka) — a pergunta destacada

**A hipótese sedutora:** se encolher o vetor pra <930d, o SBQ **2-bit** do pgvectorscale
volta a ser legal (o FAIL foi *só* por dims>900!) → **ressuscita o pgvectorscale sem
trocar de sistema**, só re-indexar. Se for barato e passar, é o caminho mais limpo.

**Por que provavelmente NÃO vale (achado que derruba a sedução):**

1. **bge-m3 NÃO tem MRL.** O dense do bge-m3 é **fixo 1024d, treinado sem Matryoshka**
   — as primeiras 768 dimensões **não** concentram a informação
   ([BAAI/bge-m3 disc.](https://huggingface.co/BAAI/bge-m3/discussions/57);
   [BentoML embeddings 2026](https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models)).
   Truncar 1024→768 in-place **joga fora informação distribuída** (não redundante). A
   perda de recall é **incerta e provavelmente material** — MRL só preserva recall porque
   *treinou* pra isso; sem MRL, truncar é amputar. (Contraste: Granite/Jina-v3 *têm* MRL
   e truncam 768→512 quase de graça — bge-m3 não é esse caso.)

2. **Truncar in-place é barato, MAS re-embedar não.** Truncar os 31M vetores existentes
   pra 768d = uma projeção barata (só re-indexar, sem GPU). **PORÉM** se o gate reprovar a
   truncagem ingênua (provável, item 1), a alternativa é **trocar o modelo de embedding
   por um MRL-nativo e re-vetorizar os ~85M chunks** — isso é **mudar o contrato de
   embedding** (novo modelo, re-embed de tudo, invalida a régua de retrieval já medida,
   re-treina o rerank tuning). Caríssimo. A "truncagem barata" só vale SE a truncagem
   ingênua passar o gate — e a prior disso é ruim.

3. **Mesmo se a truncagem preservar recall, você ainda depende do SBQ-2bit passar.**
   O SBQ-2bit **nunca foi medido neste corpus** (o FAIL foi no build, antes de quantizar).
   bge-m3 já mostrou hostilidade a quantização binária (§0). São **dois saltos de fé em
   série**: (truncar 768d preserva recall) **E** (SBQ-2bit a 768d passa overlap@8≥0,90).
   Multiplicar duas probabilidades incertas = aposta fraca pra ser o caminho principal.

**O que resolve SE funcionar:** encolhe o HNSW halfvec **também** (768d ≈ 44GB vs 58GB —
cabe melhor), e reabilita o pgvectorscale disk-resident (o teto sobe de verdade, fica no
PG, hybrid numa query SQL — o cenário mais limpo operacionalmente). O upside é real; a
prior é que é.

**Esforço/risco:** truncar in-place = BAIXO esforço, ALTO risco de recall. Re-embedar com
modelo MRL = ALTO esforço (contrato novo, 85M re-embed), risco menor de recall mas custo
proibitivo. **Reversibilidade:** truncar é aditivo (coluna/índice novo, halfvec 1024d
intacto) → reversível. Re-embedar é um caminho sem volta prático.

**Veredito §2:** **NÃO é o caminho principal, mas é um side-quest barato e honesto de
FECHAR primeiro** (§5, Fase 0.5): truncar 1024→768 in-place numa amostra + **medir
overlap@8 no full-corpus** vs o halfvec 1024d. Se passar (surpresa boa), aí SIM vale
testar SBQ-2bit@768d e o caminho fica no PG — mais barato que LanceDB. Se reprovar (o
provável), **cruzou-se a Matryoshka da lista** com um teste barato e vai-se pro LanceDB
sem arrependimento. **Fechar essa questão com número é pré-requisito da decisão.**

---

## §3. Caminho 3 — GPU-native (CAGRA/cuVS) ou PQ fora do PG (Faiss IVF-PQ)

**Ideia:** índice ANN construído/servido na **GPU** (NVIDIA cuVS/CAGRA via Milvus-GPU ou
Faiss-GPU) — build de índice em minutos, busca GPU. Temos 3090s.

**O que resolve:** o teto de **escrita** (build de índice GPU é rápido e não é o INSERT-
no-HNSW-por-linha do PG). É o degrau que o `ROADMAP.md` já reserva pro **full-corpus 85M**
"onde nem 96GB segura o índice".

**Por que NÃO agora:**
- **Custo de arquitetura idêntico ao Caminho 1** (tira vetor do PG, 2º store, sync) **+
  uma GPU dedicada** competindo com embed/extração/classificação (a memória
  `g4-gpu-remota-licoes` já ensinou: GPU cara em vetorização é DB-latency-bound; o gargalo
  real era CPU de classificação, não GPU). Colocar o índice na GPU rouba a mesma 3090 do
  `zordon-extrair`.
- **Faiss IVF-PQ (CPU)** é essencialmente **o que o LanceDB já entrega embarcado** (IVF_PQ
  on-disk) sem a operação de um índice Faiss cru (persistência/sync/sharding manuais). Ou
  seja, LanceDB **é** o Caminho 3-CPU num pacote operacionalmente melhor.
- **Milvus-GPU** = etcd+MinIO+GPU, peso máximo.

**Veredito §3:** **degrau FUTURO e condicional** (full-corpus 85M, SE o LanceDB-CPU também
raspar teto). PQ está coberto pelo LanceDB hoje sem GPU. **Não é a aposta agora.**

---

## §4. Comparação lado a lado

| Critério | **1a. LanceDB** | 1b. Qdrant/Milvus | **2. Truncar bge-m3** | 3. GPU-CAGRA |
|---|---|---|---|---|
| Sobe o teto de escrita? | **Sim** (disk-first, não RAM-bound) | Sim (server on-disk) | Só se recall passar (RRESUSCITA PG) | Sim (build GPU) |
| Escala pro full-corpus 85M? | **Sim** | Sim | Talvez (índice menor) | Sim |
| Muda contrato de embedding? | **Não** (bge-m3 1024d fica) | Não | **Sim** se re-embedar (só truncar=não) | Não |
| Preserva RRF+FTS+rerank pt-BR? | **Sim** (serve só ANN → RRF do PG) | Sim (idem) | **Sim** (fica no PG) | Sim (idem) |
| Peças móveis novas | Baixo (lib embarcada) | Alto (daemon/cluster) | **Zero** (fica no PG) | Alto (GPU+store) |
| Recall = risco? | Médio (PQ+refine ≠ sign-bit; gate decide) | Médio-alto (BQ=mesmo que falhou) | **Alto** (bge-m3 sem MRL) + SBQ incerto | Médio (CAGRA+refine) |
| Reversível até cutover? | **Sim** (dual-write + flag) | Sim | Sim (aditivo) | Sim |
| Esforço | Médio (sync+desnorm) | Alto | Baixo (truncar) / Proibitivo (re-embed) | Alto |
| Veredito | **RECOMENDADO** | descartado (peso) | side-quest barato p/ fechar | futuro/condicional |

---

## §5. RECOMENDAÇÃO + plano faseado (POC → gate → cutover reversível)

**Recomendação: LanceDB (Caminho 1a)**, precedido de um teste barato que fecha a
Matryoshka (Caminho 2) — se por milagre a truncagem passar, fica no PG (mais barato) e
não se paga o 2º store. **Justificativa cética:** depois de 2 FAILs *dentro* do PG e um
FAIL estrutural do pgvectorscale a 1024d, o índice **precisa** sair do regime RAM-bound;
LanceDB é a saída de menor superfície (embarcado, disk-first, serve só o ANN, RRF/FTS/
rerank intactos), com quantizador (IVF_PQ) **diferente** do que falhou e refine de vetor
cheio atacando a causa-raiz medida (vizinho verdadeiro fora do candidate-set). Nada disso
é garantia — **o gate no corpus cheio é a única prova**, como foi pros 2 FAILs.

### Fase 0.5 — Fechar a Matryoshka (barato, decide se nem precisamos de LanceDB)
1. Truncar 1024→768 in-place numa amostra + no full via projeção (read-only, tabela de teste).
2. **Gate:** `overlap@8` do halfvec-768 vs halfvec-1024, **no corpus cheio**, 20 queries.
   - PASS (≥0,90) → testar SBQ-2bit@768d no pgvectorscale (inerte já instalado) com o
     mesmo gate. Se PASS → **fica no PG, pare aqui** (caminho mais barato). Documentar.
   - BLOCK → Matryoshka cruzada da lista (o esperado; bge-m3 sem MRL). Ir pra Fase 1.

### Fase 1 — POC LanceDB fora de prod (sandbox/replica)
3. Instalar LanceDB (lib). Ingerir uma **fatia representativa** do corpus (ex. 2–5M
   chunks, estratificada por tribunal/ano — não amostra pequena; a lição do bit foi que
   grafo pequeno mente). Construir IVF_PQ com refine.
4. Desenhar o sync: mapear `chunk_id`/`documento_id`, desnormalizar `status`+`numero_cnj`
   pro filtro on-disk. Provar upsert/delete idempotente.

### Fase 2 — GATE de recall no corpus cheio (condição dura de cutover)
5. Ingerir os **31M** no Lance (dual-write começa aqui, aditivo). Rodar o **mesmo gate**:
   `overlap@8 ≥ 0,90` do (ANN LanceDB → RRF) vs o halfvec-índice incumbente, **20 queries,
   corpus cheio, sem seqscan**. Varrer `nprobes`/`refine_factor` (o análogo do oversample N).
   - **BLOCK <0,90** → tunar PQ (subvetores)/refine; se ainda BLOCK, documentar e **parar**
     (halfvec permanece; próximo degrau = CAGRA-GPU §3 ou re-embed MRL, escopo próprio).
   - **PASS** → seguir. **Não confiar em amostra** — anotar padrão bimodal (queries em 0)
     se aparecer, foi o sintoma do bit.

### Fase 3 — Cutover reversível (só se PASS)
6. Adicionar modo `lance` ao `ZORDON_ANN_MODE` em `search.py` (o estágio ANN chama o Lance
   por `chunk_id` e devolve `[(pk, rank)]`; RRF/rerank/FTS **não mudam**). Cutover = flip da
   flag. Rollback = flip pra `halfvec` (intacto).
7. **Canário/sombra:** comparar resultados de busca real Lance vs halfvec por um período
   (reusa o harness de eval `zordon/eval/` e o `EvalRun`/gate delta já existentes — RAG_EVAL_OBS §11).
8. Job de **reconciliação** de sync PG↔Lance (contagens/ids) rodando antes de confiar.
9. **Só então** dropar `chunk_emb_hnsw_half` (58 GB). A coluna `embedding` no PG **fica**
   (fonte de verdade → reconstrói o Lance a qualquer hora). `vectorscale 0.9.0` inerte pode
   `DROP EXTENSION` (não será usada).

**Reversibilidade global:** até o passo 9, halfvec intacto + dual-write → cutover é flip de
flag, rollback instantâneo. Só o DROP final é irreversível (e vem por último).

---

## §6. O que é HONESTAMENTE incerto (não maquiar)

- **Recall do IVF_PQ no bge-m3/nosso corpus:** HIPÓTESE. PQ ≠ sign-bit e há refine, mas o
  bge-m3 já foi hostil a quantização binária aqui. **Só o gate full-corpus decide** —
  poderia BLOCK como os 2 anteriores. Não repetir o otimismo da amostra.
- **Custo real do sync PG↔Lance:** drift silencioso é o risco operacional novo (um vetor a
  menos = recall pior sem alarme). Reconciliação é obrigatória, não opcional.
- **Truncagem bge-m3 (§2):** a prior "não passa" é forte mas **não medida neste corpus** —
  por isso a Fase 0.5 mede antes de descartar. Se passar, muda a recomendação (fica no PG).
- **Filtro `status`/`cnj` no Lance:** desnormalizar resolve, mas duplica dado e sync. Se
  ficar caro, o pré-filtro-no-PG-gera-allowlist é o fallback (pior no caso sem cnj).
- **Latência da busca cross-store:** hoje o ANN é 1 scan no PG. Cross-store adiciona 1
  round-trip Lance + o join de volta pra decifrar texto no PG. Provavelmente OK (Lance é
  local, ANN já era 110ms), mas **medir** — a busca já é frágil sob carga de batch
  (`db-contencao-busca-vs-batch`). Ironia positiva: tirar o índice vetorial do PG **alivia**
  o disco do primário, o que ajuda a contenção busca×batch de brinde.

---

## Fontes

- bge-m3 sem MRL / dense fixo 1024d: <https://huggingface.co/BAAI/bge-m3/discussions/57> ·
  <https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models> ·
  <https://build.nvidia.com/baai/bge-m3/modelcard>
- MRL (o que preserva recall na truncagem; modelos que TÊM): <https://arxiv.org/pdf/2510.12474> ·
  <https://arxiv.org/pdf/2605.13521> (Granite trunca 768→512/256/128)
- LanceDB hybrid (BM25+vetor+RRF+rerank): <https://docs.lancedb.com/search/hybrid-search>
- LanceDB IVF_PQ on-disk + recall/refine: <https://www.lancedb.com/blog/benchmarking-lancedb-92b01032874a-2> ·
  <https://lancedb.com/docs/indexing/vector-index/>
- LanceDB escala (700M prod, 10–50B alvo): <https://sprytnyk.dev/posts/running-lancedb-in-production/> ·
  <https://docs.lancedb.com/faq/faq-oss>
- Qdrant BQ + oversampling/rescore (≥1024d, mas quantizador = sign-bit): <https://qdrant.tech/articles/binary-quantization/> ·
  <https://qdrant.tech/documentation/manage-data/quantization/>
- Docs internas: `QUANTIZACAO_INDICE_VETORIAL.md` (BLOCK bit) · `PGVECTORSCALE_SBQ.md`
  (FAIL 2-bit>900d) · `PARTICIONAMENTO_ACERVO_CHUNK.md` · `RAG_EVAL_OBS.md` (§11 régua+gate) ·
  memórias `vetor-ingest-teto-e-opcoes`, `quantizacao-indice-vetorial`, `db-contencao-busca-vs-batch`.
