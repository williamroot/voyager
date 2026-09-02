# pgvectorscale (StreamingDiskANN + SBQ) — escopo do índice vetorial da `acervo_chunk`

> **🛑 REMEDIÇÃO 2026-09-01 (#68) — PARAR. A premissa mudou, e mudou pra pior:
> não há escrita, não há leitura, e não há disco.**
>
> O #68 existe pra destravar o **teto de ESCRITA vetorial**. Fui medir o teto e
> ele não está sendo tocado — nem por baixo, nem por cima:
>
> | o que | medido em 01-02/09/2026 | era (jul/2026) |
> |---|---|---|
> | último `acervo_chunk` gravado | **2026-08-18 13:36 UTC** (15 dias atrás) | fluxo contínuo |
> | fila `vetorizar` / `vetorizar_write` | **0 / 0** (8 writers `active`, sem trabalho) | drenando |
> | `chunk_emb_hnsw_half` — `idx_scan` | **0** | — |
> | `acervo_chunk_documento_id` — `idx_scan` (CONTROLE) | **2.770.454** | — |
> | RAM do host `.114` | **76 GiB** | 94 GB (doc) |
> | `shared_buffers` | 24 GB | 24 GB |
> | índice HNSW halfvec | **84 GB** | 57-58 GB |
> | disco `/` | **712 GB de 787 GB — 96%, 35 GB livres** | ~514 GB livres |
>
> **1. O índice vetorial não é lido.** `chunk_emb_hnsw_half` é VÁLIDO, tem 84 GB e
> **zero scans**, na mesma janela em que o `documento_id` da MESMA tabela levou
> 2.770.454 — o controle que prova que o contador funciona. Confirmei por
> **mutação**: rodei duas buscas vetoriais de verdade e o contador foi **0 → 2**;
> as minhas foram as duas primeiras. Não há réplica servindo leitura
> (`pg_stat_replication` = 0, `pg_is_in_recovery()` = false) e a tabela levou 11
> seq scans no período. O `_cat`-equivalente aqui seria concluir pelo tamanho;
> o número que encerra a investigação é o `idx_scan` com controle ao lado.
>
> **2. A busca vetorial É I/O-bound — mas isso hoje não custa nada, porque
> ninguém busca.** Medido de verdade (não `EXPLAIN` de estimativa): **fria
> 10,42 s** (847 blocos lidos do disco) contra **2,68 ms** quente. É o
> disk-cliff previsto, 3.890×. É também o argumento mais forte a favor do #68 —
> e ele vale zero enquanto o contador de scans for 0.
>
> **3. Não cabe construir nada.** Qualquer caminho do #68 (diskann 1-bit, rebuild
> de halfvec, índice paralelo pra cutover) quer dezenas de GB. **Há 35 GB
> livres.** O disco é o blocker antes da extensão, do recall e da RAM.
>
> **4. O acervo vetorial está pela metade, e a metade que falta não é do DB.**
> Duas amostras `TABLESAMPLE SYSTEM (0.05)` independentes: **69,84%** e **64,93%**
> dos chunks têm `embedding` (controle: `total = com + sem`, `id` não-nulo em
> 100%). São ~35% de 61,6M — **~21M chunks sem vetor**, com a coleta parada há 15
> dias e a fila vazia. O gargalo de hoje é o produtor (frota de embed), não o
> escritor: ver memórias `frota-pods-quickpod-realidade` e
> `pod-showcase-efemero-recuperacao`.
>
> **5. O bloqueio estrutural de 27/07 continua de pé.** `vectorscale 0.9.0` está
> instalada e o AM `diskann` registrado; o fonte em
> `~/pgvectorscale-build/.../meta_page.rs:325` ainda tem
> `bq_num_bits_per_dimension > 1 && num_dimensions_to_index > 930 → error`. E
> **0.9.0 (04/11/2025) segue sendo a última release** — 10 meses sem lançamento.
> A 1024d continua só o 1-bit, que é o regime reprovado (overlap@8 0,49).
>
> ### Gate PRÉ-REGISTRADO (escrito ANTES de medir, pra quando isto voltar)
>
> O gate anterior deu BLOCK porque **a amostra mentiu** (328k deu 0,881; o corpus
> cheio deu 0,49). Este é desenhado pra não cair nisso — e o item 0 é novo: antes
> de medir recall, provar que alguém LÊ.
>
> 0. **Demanda**: `chunk_emb_hnsw_half.idx_scan` > 10.000 numa janela de 7 dias.
>    Otimizar índice com 0 scans é custo puro. **Hoje: 2 (as minhas).**
> 1. **Disco**: ≥ 150 GB livres em `/` na `.114` ANTES do build. **Hoje: 35 GB.**
> 2. **Escrita viva**: `vetorizar_write` com vazão > 0 por 24 h seguidas.
>    **Hoje: 0 há 15 dias.**
> 3. **Recall**: `overlap@8 ≥ 0,90` contra o halfvec incumbente, **no corpus
>    cheio**, com N ≥ 200 queries **amostradas do tráfego real** (não sintéticas),
>    reportando também o pior decil — média alta com cauda zerada foi exatamente
>    o que 6/20 queries com overlap 0 esconderam.
> 4. **Latência**: p95 frio ≤ 2 s (o halfvec frio de hoje é 10,42 s).
> 5. Qualquer item reprovado ⇒ **BLOCK**, sem "quase lá".
>
> **Ação recomendada agora: nenhuma migração.** O item de valor imediato não é o
> #68 — é o disco a 96% num banco de 698 GB (`acervo_chunk` sozinha é 666 GB, dos
> quais **527 GB de TOAST**). E existe um número grande e barato em cima da mesa:
> os 84 GB do índice que ninguém lê valem 11 pontos percentuais de disco. **Não
> executei o `DROP` — é decisão do dono**, e ele custa reconstruir se a busca
> vetorial voltar a ser usada.

> **⛔ ATUALIZAÇÃO 2026-07-27 — FAIL NO BUILD: 2-bit SBQ é IMPOSSÍVEL em 1024d.**
> A extensão **pgvectorscale 0.9.0 foi compilada e instalada** com sucesso na VM do
> DB (.114, PG18.4) — `CREATE EXTENSION vectorscale` OK, AM `diskann` registrado,
> **sem restart** (é access method, não preload; confirmado). MAS o
> `CREATE INDEX CONCURRENTLY ... WITH (num_bits_per_dimension=2)` **abortou na hora**
> com:
> ```
> ERROR: SBQ with more than 1 bit per dimension is not supported for more than 900 dimensions
> ```
> **Não é bug nem tuning** — é um limite ESTRUTURAL codado em
> `src/access_method/meta_page.rs:326` (`bq_num_bits_per_dimension > 1 &&
> num_dimensions_to_index > 930 → error`, comentário: *"limited by SbqMeans fitting
> on a page"* — as estatísticas de quantização por dimensão não cabem numa página
> 8KB acima de ~930 dims a 2 bits). bge-m3 = **1024d** cai fora. **O único regime
> permitido a 1024d é 1-bit** — exatamente o que o §4 estimava <30% e o que o
> bit-puro já reprovou (overlap@8 0,49).
>
> **Como 2-bit era OBRIGATÓRIO (a alavanca de recall; 1-bit é o regime reprovado),
> o veredito é FAIL no estágio de build — nem foi possível construir o índice
> mandado.** O husk inválido foi **dropado** (`DROP INDEX CONCURRENTLY`), o
> **halfvec de 58GB permanece intacto** e é o caminho de prod. A extensão
> `vectorscale 0.9.0` ficou instalada mas **inerte** (nenhum índice a usa; aditivo,
> reversível com `DROP EXTENSION`). Pico de RAM do host durante tudo: **zero
> pressão** (build da extensão é CPU-only; o build do índice morreu em 47s antes de
> quantizar nada; VM ficou em ~50GB livres o tempo todo; guard nunca disparou).
>
> **1-bit NÃO foi construído** — violaria o "2-bit obrigatório" e custaria ~6-12h de
> I/O competindo com a frota pra provavelmente reconfirmar o FAIL do bit-puro. Se
> quiser medir o 1-bit mesmo assim (fechar a questão com número), é decisão do
> usuário; caso contrário o próximo degrau real sai do Postgres (LanceDB / store
> vetorial dedicado / CAGRA-GPU), OU truncar bge-m3 pra <930d (Matryoshka) —
> ambos mudam o contrato e exigem escopo próprio.

> **Status: ESCOPO / design + pesquisa. Build TENTADO 2026-07-27 → FAIL (ver topo).**
> A extensão foi instalada (aditivo); o índice 2-bit é impossível a 1024d.
>
> **Data:** 2026-07-26. **Contexto:** logo após o BLOCK da quantização binária
> ingênua (`binary_quantize`::bit + Hamming), que reprovou o gate de paridade no
> full-corpus (overlap@8 = 0,49/0,59 vs ≥0,90). Ver
> [`QUANTIZACAO_INDICE_VETORIAL.md`](QUANTIZACAO_INDICE_VETORIAL.md) e a memória
> `vetor-ingest-teto-e-opcoes`.

---

## TL;DR — VEREDITO (cético)

**Vale escopar e provavelmente vale TENTAR, mas com expectativa calibrada e o mesmo
gate implacável de hoje.** Nada aqui é blocker de compat.

1. **Compat PG18.4: NÃO é blocker.** pgvectorscale **0.9.0 (2025-11-04)** é a
   primeira release com **suporte a PG18** (e removeu PG13). Convive com pgvector
   (depende dele; `CREATE EXTENSION ... CASCADE` instala/usa o pgvector já presente
   — nosso 0.8.1 serve). O gargalo NÃO é a versão do Postgres.
2. **O gargalo real é operacional + recall.** (a) Não há pacote apt oficial estável
   pra Debian/Ubuntu em prod — o caminho realista é **build from source com
   Rust/cargo-pgrx** no host do DB de prod (`192.168.30.114`), ou container. (b) O
   **recall** volta a ser hipótese-a-testar, não fato: pra 1024-dim o SBQ usa **1
   bit/dimensão por padrão** (o default cai pra 1-bit em ≥900 dims) — MESMO regime
   de bits que falhou hoje. A diferença é que o SBQ é **calibrado** (threshold =
   média por dimensão, não sinal em zero) e o DiskANN **rescoreia com o vetor cheio
   em disco**. Isso é qualitativamente melhor que o sign-bit puro, mas NÃO garante
   passar o gate. **Forçar 2-bit** (`num_bits_per_dimension=2`) é a alavanca de
   recall — e é a config que este plano recomenda testar primeiro.
3. **Probabilidade honesta de passar o gate (overlap@8 ≥ 0,90 no full-corpus):**
   **média — algo como 50-65% com 2-bit + rescore, baixa (<30%) com 1-bit default.**
   Maior que o bit puro (que era ~0%) porque threshold calibrado + rescore de vetor
   cheio atacam exatamente a causa-raiz medida hoje (o vizinho cosine nem entrava no
   candidate-set bit). Mas continua sendo quantização binária num corpus (bge-m3)
   que já provou ser hostil a 1 bit. **Não repetir o otimismo da amostra: gate no
   full-corpus, cético, antes de qualquer cutover.**

**Recomendação:** seguir pro build alongside num ambiente de teste primeiro
(container/replica), medir SBQ 2-bit vs 1-bit no full-corpus com o gate de
overlap@8, e só então decidir cutover. Não é barato (build Rust em prod + índice de
29,9M), mas é o caminho desenhado exatamente pro nosso problema ("HNSW não cabe na
RAM") e ataca a causa que o bit puro não atacava.

---

## 1. Compatibilidade PG18.4 — a primeira pergunta (respondida: OK)

| Item | Fato (fonte primária) |
|---|---|
| pgvectorscale suporta PG18? | **Sim.** Release **0.9.0** (2025-11-04) — *"Added support for PG18 / Removed support for PG13"*. |
| Versão mínima p/ PG18 | **0.9.0** (a mais recente). Releases anteriores (0.4.0→0.8.0) vão até PG17. |
| Convive com pgvector 0.8.1? | Sim. pgvectorscale **depende** do pgvector (usa o tipo `VECTOR`); não conflita. `CREATE EXTENSION vectorscale CASCADE` usa/instala o pgvector presente. Nosso 0.8.1 atende. |
| Matriz de versões | pgvectorscale usa **pgrx** com feature-flag por major (`pg14`…`pg18`). PG18 exige a flag `pg18`, só existente em 0.9.0. |
| Build alongside PG17? | 0.9.0 ainda cobre PG14–18, então não força downgrade de nada. |

**Conclusão:** compat **não é o gargalo**. Estamos em PG18.4 e existe release que
suporta PG18. O gargalo desce um nível: **como instalar** (§2) e **se o recall
passa** (§4).

Fontes:
- Releases: <https://github.com/timescale/pgvectorscale/releases> (0.9.0, 2025-11-04, "Added support for PG18").
- README / install: <https://github.com/timescale/pgvectorscale/blob/main/README.md>
- DeepWiki install: <https://deepwiki.com/timescale/pgvectorscale/1.1-installation-and-setup>

> Ressalva de ceticismo: 0.9.0 é **recente** (nov/2025). Suporte a PG18 é **novo** —
> ler as issues abertas do repo antes de ir a prod (bugs de PG18 podem existir).
> Convém pinar a versão exata testada e não seguir `main`.

---

## 2. Instalação — procedimento e RISCOS (nada executado)

pgvectorscale é extensão **Rust (pgrx)**. Dois caminhos:

### Caminho A — build from source (realista para nosso host bare-metal)
Passos (da doc; **não executar em prod sem replica**):
1. Instalar Rust (`rustup`).
2. `git clone` do repo, `checkout` da tag **0.9.0**.
3. Instalar `cargo-pgrx` **na mesma versão** do pgrx do `Cargo.toml` da 0.9.0.
4. `cargo pgrx init --pg18 $(which pg_config)` — aponta pro Postgres de destino.
5. `cargo pgrx install --release --features pg18` — compila e instala o `.so` +
   scripts SQL na árvore do PG.
6. No banco: `CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;`

### Caminho B — pacote/container
- Existem `.deb` no packagecloud, mas a doc marca como **"primarily for CI/CD"** —
  não é canal de produção estável. Há imagem Docker Timescale (mas o DB de prod é
  bare-metal, não containerizado — trocar isso é escopo maior).

### Fatos que MUDAM o risco (bons)
- **`shared_preload_libraries`: NÃO é mencionado** como requisito. O diskann é um
  **access method** (index type), não um módulo de background — diferente do que se
  assumiu no §8 do doc do bit. **Provável que NÃO precise de restart do PG** só pra
  usar o índice (a extensão instala AM + funções). ⚠️ **VERIFICAR na replica** antes
  de afirmar em prod — se algum GUC `diskann.*` precisar de preload, aí sim exige
  restart.
- `CREATE EXTENSION` normalmente exige **superuser** (ou role com privilégio de
  criar extensão). Confirmar quem tem no host.

### RISCOS por etapa (host de prod serve busca + escrita)
| Etapa | Risco | Mitigação |
|---|---|---|
| Instalar Rust/cargo-pgrx no host de prod | Poluir o host, consumir CPU no build | Buildar em **replica/container idêntico**, copiar só o `.so`+SQL |
| `cargo pgrx install` | Escreve na árvore do PG (`$libdir`, `extension/`) | Aditivo; não toca dados. Fazer off-peak |
| `CREATE EXTENSION vectorscale` | Aditivo (cria AM + funções), reversível com `DROP EXTENSION` | Baixo risco. Testar em replica |
| Restart do PG (SE necessário) | **Janela de indisponibilidade** de busca+escrita | Só se GUC exigir preload — confirmar antes; se precisar, tratar como janela agendada |
| Build do índice diskann em 29,9M | I/O pesado, concorre com a frota (§5) | `CONCURRENTLY`, mem moderada, reduzir embed |

**Regra de ouro (lição do bit):** **buildar e validar numa replica/dump antes**.
Não instalar toolchain Rust direto no `192.168.30.114` de prod sem ter provado o
`.so` numa cópia. O único DDL aditivo/reversível em prod deve ser o
`CREATE EXTENSION` + `CREATE INDEX CONCURRENTLY`.

---

## 3. Índice diskann + SBQ — criação e parâmetros

Sintaxe (cosine, como usamos hoje):
```sql
CREATE INDEX chunk_emb_diskann ON acervo_chunk
USING diskann ( (embedding::vector(1024)) vector_cosine_ops )
WITH (
  storage_layout = 'memory_optimized',  -- default: aplica SBQ (compressed em RAM)
  num_bits_per_dimension = 2,            -- FORÇAR 2-bit (default seria 1 p/ 1024d)
  num_neighbors = 50,                    -- grau do grafo (default 50)
  search_list_size = 100,                -- candidatos no BUILD (default 100)
  max_alpha = 1.2                        -- poda do grafo (default 1.2)
);
```

> ⚠️ **Ponto de atenção crítico p/ 1024-dim:** o default de
> `num_bits_per_dimension` é **"2 para <900 dims, 1 caso contrário"**. Como bge-m3 =
> 1024, **o default é 1 bit** — o MESMO regime de 1 bit/dim que reprovou hoje.
> **Este plano recomenda testar 2-bit explicitamente primeiro** (dobra o footprint
> em RAM mas melhora recall — Timescale reporta 96,5%→98,6% ao ir de 1→2 bit em
> 768d). Se 2-bit não passar o gate, 1-bit certamente não passa.

Parâmetros e o que afetam:

| Param | Efeito | Nota p/ nós |
|---|---|---|
| `storage_layout` | `memory_optimized` (SBQ, compressed) vs `plain` (fp cheio) | `plain` = sem quantização, cabe menos → derrota o propósito. Usar `memory_optimized` |
| `num_bits_per_dimension` | 1 ou 2 bits/dim no SBQ | **A alavanca de recall.** Testar 2 primeiro (default seria 1 p/ nós) |
| `num_neighbors` | grau do grafo (recall × tamanho × build) | ↑ recall, ↑ índice, ↑ build. Começar no default 50 |
| `search_list_size` (build) | qualidade do grafo no build | ↑ recall, ↑ tempo de build |
| `max_alpha` | densidade de arestas long-range | default 1.2 |

**Query-time (por sessão/transação, ajusta recall×latência sem rebuild):**
```sql
SET diskann.query_search_list_size = 100;  -- candidatos na busca (default 100)
SET diskann.query_rescore = 50;            -- nº rescoreado com vetor cheio (0 = off)
```
- `diskann.query_rescore` = o análogo do nosso "N candidatos + rescore halfvec".
  **É o que compra recall de volta** — sobe candidatos rescoreados com o vetor cheio
  (lido do disco). Análogo ao oversampling N do plano do bit. Ajustável em runtime.
- `diskann.query_search_list_size` — quantos nós o grafo visita. ↑ recall, ↑
  latência. Ajustável em runtime (bom p/ tunar sem rebuild).

**Diferença essencial vs o bit que falhou (por que pode funcionar):**
- Bit puro (`binary_quantize`): threshold **fixo em 0** por dimensão → o Hamming não
  acompanhou o cosine no bge-m3; o vizinho cosine nem entrava no top-1000 do espaço
  bit. Rescore não salvava porque **o candidato certo nunca estava no conjunto**.
- SBQ: threshold = **média por dimensão** (calibrado dos dados) + (em 2-bit)
  encoding por z-score em regiões, e o **grafo DiskANN + rescore de vetor cheio em
  disco** melhoram o candidate-set. Ataca exatamente a causa-raiz medida hoje.
- **Mas:** 1024-dim cai no regime de **1 bit** por default (mesma perda de
  informação). O ganho do SBQ sobre o sign-bit é o threshold calibrado e o grafo —
  **hipótese plausível, não garantia.** Só o gate no full-corpus decide.

Fontes:
- SBQ (como funciona): <https://www.tigerdata.com/blog/how-we-made-postgresql-as-fast-as-pinecone-for-vector-data>
- README (params/query GUCs): <https://github.com/timescale/pgvectorscale/blob/main/README.md>
- Recall SBQ: <https://www.tigerdata.com/blog/pgvector-is-now-as-fast-as-pinecone-at-75-less-cost>

---

## 4. Recall — o ponto crítico (HIPÓTESE, não fato)

**O que a Timescale afirma** (tratar como marketing calibrado, não como nossa
medição):
- SBQ atinge **96–99% de recall** vs exato em benchmarks comuns.
- No blog: **50M embeddings Cohere de 768-dim**, 99% recall com latência muito
  menor que Pinecone.
- 1→2 bit em 768d: recall 96,5% → 98,6%.
- Curiosamente reportam que **SBQ funcionou MELHOR em 1536d que em 768d** (mais
  quadrantes) — o que é levemente animador pro nosso 1024d, mas é anedota do vendor.

**O que NÃO temos:** nenhum benchmark público de SBQ com **bge-m3 / 1024-dim**
especificamente. Os números do vendor são **768-dim Cohere** (embeddings diferentes,
distribuição diferente). **bge-m3 já provou ser hostil a 1 bit neste corpus.**

**Ceticismo obrigatório (a lição de hoje):**
- A amostra 328k do bit **mentiu** (0,881 → 0,49 no full). **Recall SBQ TAMBÉM tem
  que ser medido no corpus cheio.** Amostra não vale.
- Régua = **overlap@8 vs halfvec-índice no corpus cheio, ≥ 0,90** — o MESMO gate de
  hoje. Sem mudar a régua pra "passar mais fácil".
- Provável ponto de operação a testar: `num_bits_per_dimension=2` +
  `diskann.query_rescore` alto (100–200) + `query_search_list_size` alto.

**Probabilidade honesta:** 2-bit + rescore agressivo tem chance **real** (o
mecanismo ataca a causa-raiz), mas está longe de garantido num corpus que reprovou 1
bit. Estimativa: **~50-65% de passar o gate com 2-bit**, **<30% com 1-bit default.**

---

## 5. Build do índice — tempo e como não matar a frota

- **Referência:** o HNSW **bit** levou **~7h20** em 29,9M (CONCURRENTLY,
  `maintenance_work_mem` moderado, frota escrevendo). O halfvec (não-concorrente, 8
  workers, mwm=32GB) fez 12,7M em ~43min.
- **DiskANN é diferente:** grafo single-layer desenhado pra disco; o build é
  tipicamente **mais caro que HNSW** por vetor (constrói o grafo + quantiza SBQ), mas
  varia com `num_neighbors`/`search_list_size`. **Estimativa grosseira: 6–12h** pra
  29,9M em CONCURRENTLY com mem moderada. **Medir num piloto** (ex.: 1–2M) antes de
  cravar.
- ⚠️ **Unlogged não suportado** — o build de sanidade não pode usar tabela
  `UNLOGGED` (o teste do bit usou `_qtest_chunk` UNLOGGED). Usar tabela **logged**
  (regular) pro piloto/amostra do diskann.
- **Sem matar a frota (mesmo padrão de hoje):**
  - `CREATE INDEX CONCURRENTLY` — busca segue no `chunk_emb_hnsw_half` (57GB, não se
    toca) durante todo o build.
  - Reduzir embed / pausar writers na cauda (SIGSTOP nos pods não para billing
    QuickPod; parar `vetorizar_writer` + a LAN inline ollama/pc-A520M/will-G7 que
    escrevem chunk direto).
  - `maintenance_work_mem` **moderado** (NÃO 32GB — a lição de 24/jul: build
    agressivo arriscou a VM; e houve 2 reboots acidentais que mataram um build).
  - Off-peak.

---

## 6. Tamanho do índice e RAM — quantificado (cético)

Promessa do vendor: os **vetores comprimidos (SBQ) ficam em RAM** p/ navegação do
grafo; os **vetores cheios ficam em disco (SSD)**, lidos só no rescore → footprint
em RAM pequeno mesmo com corpus grande. É exatamente o design "HNSW não cabe na RAM".

**Estimativa do footprint SBQ em RAM (a parte quente que decide o teto de escrita):**

| Config | bits/vetor (SBQ) | ~bytes/vetor | 29,9M vetores (só SBQ) | + overhead grafo |
|---|---|---|---|---|
| 1-bit (default 1024d) | 1024 | 128 B | **~3,8 GB** | grafo (num_neighbors×id) some GB |
| 2-bit (recomendado) | 2048 | 256 B | **~7,7 GB** | idem |

- Mesmo com grafo + metadados, a **parte residente (SBQ) fica na casa de ~4–12 GB** —
  **cabe folgado** nos 94GB (shared_buffers 24GB + page cache 70GB). Compara com o
  halfvec 57GB que **já raspa o teto**.
- O **índice em disco** é maior (guarda os vetores cheios pro rescore), mas isso é
  disco, não RAM — e é o rescore que puxa esses vetores. É o trade desejado.
- **Ceticismo:** essa é a promessa que faz o design valer a pena SE o recall passar.
  A parte comprimida caber na RAM é o mecanismo que destrava a escrita — mas de nada
  adianta um índice residente que devolve vizinhos errados. **Recall primeiro.**

> Números de tamanho a **medir** no piloto (o vendor cita 768d; extrapolação p/
> 1024d + o overhead real do grafo DiskANN precisam de medição, não fé).

---

## 7. Plano de build + GATE (o processo que funcionou hoje, mesmo o bit falhando)

Espelha o processo do bit (que funcionou **como processo** — o gate pegou a falha
antes do cutover). Cutover reversível, halfvec só cai no fim.

**Fase 0 — replica/sandbox (fora de prod):**
1. Buildar pgvectorscale 0.9.0 (`--features pg18`) numa **replica/container idêntico**
   ao prod. Provar `CREATE EXTENSION vectorscale` + criar diskann numa amostra
   **logged**. Confirmar se precisa restart/preload (esperado que não).

**Fase 1 — piloto de escala (prod, aditivo):**
2. `CREATE EXTENSION vectorscale` em prod (aditivo, reversível).
3. `CREATE INDEX CONCURRENTLY chunk_emb_diskann ... num_bits_per_dimension=2` off-peak,
   frota reduzida, mem moderada. Medir tempo/tamanho reais.
4. Ajustar `diskann.query_rescore` / `query_search_list_size`.

**Fase 2 — GATE no full-corpus (condição dura de cutover):**
5. Rodar o **mesmo gate**: `overlap@8` (bge-m3, top-8) do diskann+SBQ vs o
   halfvec-índice, **20 queries** (`eval/questions.yaml` + anchors), **no corpus
   cheio**, ambos via índice (sem seqscan).
   - **PASS = overlap@8 ≥ 0,90** (mesma barra do bit).
   - **BLOCK = < 0,90** → não faz cutover; tenta variar bits/rescore; se ainda BLOCK,
     documenta e para (halfvec permanece).
6. **Não confiar em amostra.** Full-corpus, cético. Anotar o padrão bimodal (queries
   em 0,000) se aparecer.

**Fase 3 — cutover reversível (só se PASS):**
7. Flag `ZORDON_ANN_MODE` (esqueleto na branch `feat/quantizacao-bit`): adicionar
   modo `diskann` no `search.py` (`ORDER BY embedding <=> query` com o GUC de rescore
   setado). Cutover = flip da flag; reverter = flip de volta pro `halfvec`.
8. Rodar sombra/canário: comparar resultados de busca real diskann vs halfvec por um
   período.
9. **Só então** dropar o `chunk_emb_hnsw_half` (57GB) — e nunca antes do canário
   confirmar. `chunk_emb_hnsw_bit` (9GB, inútil) pode dropar já.

**Reversibilidade:** até o passo 9, halfvec intacto → cutover é flip de flag,
rollback instantâneo. Só o DROP final é irreversível (e vem por último, após
canário).

---

## 8. Esforço / tempo estimado

| Bloco | Estimativa |
|---|---|
| Build/validação em replica (Rust toolchain + `.so` + smoke test) | 0,5–1 dia (primeira vez com pgrx tem fricção) |
| `CREATE EXTENSION` + `CREATE INDEX CONCURRENTLY` piloto (1–2M) | algumas horas |
| Build diskann full-corpus (29,9M, CONCURRENTLY) | **~6–12h** de máquina (medir no piloto) |
| Gate overlap@8 full-corpus + tuning bits/rescore | 0,5–1 dia (iterativo) |
| Cutover flag + canário + drop halfvec | 0,5 dia + janela de observação |
| **Total realista** | **~3–5 dias** de trabalho espalhados (muito é espera de build) |

---

## 9. VEREDITO cético (final)

- **Compat PG18.4 é blocker?** **Não.** 0.9.0 suporta PG18, convive com pgvector
  0.8.1. Podemos parar de tratar a versão do Postgres como o risco.
- **É viável no nosso PG18?** **Sim, tecnicamente.** O risco operacional real é
  **build Rust (pgrx) num host de prod bare-metal** (mitigável via replica/container)
  e confirmar que **não precisa de restart** (provável que não — é access method, não
  preload; **verificar na replica**).
- **Vale tentar?** **Sim, com expectativa calibrada.** É o único caminho no roadmap
  desenhado exatamente pro nosso problema (índice não cabe na RAM → SBQ comprimido
  residente + vetores cheios em disco), e o SBQ **ataca a causa-raiz** que o bit puro
  não atacava (threshold calibrado por dimensão + rescore de vetor cheio, vs sinal em
  zero). O footprint em RAM (~4–12 GB) resolveria o teto de escrita **se** o recall
  passar.
- **Probabilidade honesta de passar o gate?** **Média.** ~50-65% com **2-bit +
  rescore agressivo**; **<30% com 1-bit** (default p/ 1024d — mesmo regime que
  falhou). Maior que o bit puro (~0%), porque o mecanismo é melhor. Mas bge-m3 já
  mostrou que 1 bit/dim não acompanha cosine neste corpus, e 1024d **cai no default
  de 1 bit** — por isso **forçar 2-bit é obrigatório no primeiro teste**.
- **A regra que não muda:** **gate de overlap@8 ≥ 0,90 no CORPUS CHEIO antes de
  qualquer cutover.** A amostra do bit mentiu (0,881 → 0,49). Não repetir o otimismo.
  Se o 2-bit reprovar no full-corpus, **documentar e parar** — halfvec permanece e o
  próximo degrau vira separar o store (LanceDB / CAGRA-GPU), não insistir em
  quantização binária no Postgres.

---

## Fontes

- Releases (PG18 em 0.9.0, 2025-11-04): <https://github.com/timescale/pgvectorscale/releases>
- README (params diskann, query GUCs, build pgrx, num_bits default): <https://github.com/timescale/pgvectorscale/blob/main/README.md>
- Install/setup (versões PG14–18, pgvector dep, unlogged não suportado): <https://deepwiki.com/timescale/pgvectorscale/1.1-installation-and-setup>
- SBQ — como funciona (threshold = média por dim, z-score 2-bit, recall 768d): <https://www.tigerdata.com/blog/how-we-made-postgresql-as-fast-as-pinecone-for-vector-data>
- Recall SBQ 96–99%, 50M/768d Cohere: <https://www.tigerdata.com/blog/pgvector-is-now-as-fast-as-pinecone-at-75-less-cost>
- Repo pgvectorscale: <https://github.com/timescale/pgvectorscale>
- Doc relacionada interna: [`QUANTIZACAO_INDICE_VETORIAL.md`](QUANTIZACAO_INDICE_VETORIAL.md), memória `vetor-ingest-teto-e-opcoes`.
