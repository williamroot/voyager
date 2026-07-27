# RAG Eval + Observabilidade + Command Center — Plano de Implementação

> Fonte de verdade da observabilidade e avaliação do ambiente de milhões de embeddings.
> Origem: síntese de 3 especialistas (avaliação RAG, frameworks, observabilidade) + crítica do usuário
> (Falcon é silver, não gold). Jul/2026.

## 0. Princípios (não-negociáveis)

1. **Soberania BR** — nada que toque conteúdo de autos sai pra SaaS US (sem LangSmith/Arize-cloud/Datadog).
   Tracing/eval = self-hosted. Redação central no OTel Collector.
2. **Reuso > peça nova** — já temos Postgres, Redis, ECharts, telemetria de GPU (hash Redis),
   harness de eval (`zordon/eval/`). Regra: cada componente novo precisa pagar um incidente real.
3. **Falcon = SILVER (testemunha), não GOLD (juiz)** — triangular com texto dos autos + adjudicação
   humana no resíduo. Métrica = concordância + split adjudicado. Gates por **DELTA** (imune ao ruído).
4. **Custom > framework** — sem LangChain/LlamaIndex/Haystack/Ragas. DSPy só offline (compilar prompt).
5. **Delta, não absoluto** — gate de regressão compara antes/depois; nunca cravar "99,5% preciso".
6. **Métrica agregada 100% + trace amostrado** — nunca tracear 100% de milhões.

## 1. Arquitetura (onde cada coisa vive)

```
                    ┌─────────────────────── VISIBILIDADE ───────────────────────┐
  PRODUTO (premium) │  Voyager /dashboard/command  (HTMX + ECharts, server-side)  │  ← a ESTRELA
                    │  "tudo que está sendo processado" + qualidade + custo       │
                    └───────────────▲─────────────────────────▲──────────────────┘
                                    │ JSON endpoints           │ iframe/panel embed (opcional)
                    ┌───────────────┴──────────┐   ┌───────────┴───────────────┐
  OPS (infra deep)  │  Voyager fleet_view etc. │   │  Grafana (infra/alertas)  │
                    └───────────────▲──────────┘   └───────────▲───────────────┘
                                    │                          │ PromQL
        ┌───────────────────────────┼──────────────────────────┼───────────────────┐
  DADOS │ Redis (filas, GPU hash)   │  Postgres (pg_stat, acervo, eval, feedback)   │
        │                           │  Prometheus  ← postgres_exporter + gpu-bridge  │
        │                           │  QuickPod API (crédito/pods)                   │
        └───────────────────────────┴──────────────────────────────────────────────┘
                                    ▲ OTLP (fase 3)
                    ┌───────────────┴──────────┐
  TRACING (fase 3)  │ OTel SDK → Collector      │→ Langfuse self-host (traces LLM/RAG)
                    │ (redige autos, amostra)   │
                    └──────────────────────────┘
```

- **Command Center** = página premium única no Voyager (reusa os componentes ECharts premium já feitos:
  gauges, esteiras, tabela de frota, KPIs). Consolida pipeline + qualidade + custo. Server-rendered HTMX,
  auto-refresh, sem SPA.
- **Grafana** = ferramenta interna de ops (pg_stat, cache-hit HNSW, autovacuum, alertas). NÃO é a estrela.
- **Prometheus** = TSDB + Alertmanager (a única peça de infra realmente nova).

## 2. Catálogo de métricas (fonte → destino)

### 2.1 Pipeline (o "tudo sendo processado") — já temos a maioria
| Métrica | Fonte | Onde |
|---|---|---|
| docs/h vetorizados (ready delta) | Postgres `acervo_documento` | Command Center + snapshot Redis |
| extrações/h | Postgres `acervo_metadadoextraido` | Command Center |
| classificação (doc_classe) em dia + rate | Postgres `acervo_documento.doc_classe` | Command Center |
| fila vetorizar / vetorizar_write | Redis (django_rq) | Command Center |
| gauges "com autos" (vetor/classif/extração) | manifest − missingautos | Command Center (já existe) |
| frota: pods+LAN, GPU util/vram, papel, workers | hash Redis `vetor:gpu`/`extracao:gpu` + rq | Command Center (tabela já existe) |
| crédito/burn/runway QuickPod | API `api.quickpod.org/update/api/me`+`/gpu_pods` | Command Center (NOVO card) |

### 2.2 Infra / vector store (o que morde) — via Prometheus
| Métrica | Fonte (Prometheus) | Alerta |
|---|---|---|
| cache-hit ratio índice HNSW | `postgres_exporter` (`pg_statio_user_indexes`) | **cai = leading indicator disk-cliff** |
| tamanho índice halfvec vs shared_buffers/RAM | `pg_relation_size` + node | — |
| dead tuples / bloat acervo_chunk | `pg_stat_user_tables` | — |
| autovacuum ativo + duração | `pg_stat_progress_vacuum`, `last_autovacuum` | **regra composta c/ fila_write** |
| p95/p99 busca vetorial | `pg_stat_statements` | p95 > SLO |
| conexões / long-running xact | `pg_stat_activity` | DB saturado |
| GPU util/temp/hang | ponte hash Redis → `/metrics` | GPU hang |
| custo/h pods | QuickPod API → pushgateway/exporter | custo > teto |

### 2.3 Qualidade RAG — via harness (§4) + feedback
| Métrica | Fonte | Nota |
|---|---|---|
| recall@k retrieval (cnj-filtered) | eval harness (weak-label triangulado) | a RÉGUA |
| decomposição erro: retrieval vs LLM | eval harness (attribution) | o número que diz onde investir |
| citation accuracy (chat/narrativa) | verificação determinística pós-geração | zero LLM, crítico jurídico |
| extração acc@cobertura por campo×tribunal | eval harness (Falcon triangulado) | estender o que existe |
| distribuição de score de retrieval | agregado Redis/Postgres 100% req | proxy de degradação |
| taxa "sem resultado"/low-conf | agregado | proxy |
| thumbs 👍👎 | Postgres (novo) | ground-truth humano grátis |
| recall drift noturno | job cron vs golden set + k-NN exato | sobe ef_search quando cai |

## 3. O Command Center premium (a estrela)

**Rota:** `GET /dashboard/command` (Voyager, `dashboard/views.py` + template + partial + endpoint JSON).
**Padrão:** server-side HTMX (`hx-get` refresh 5–10s nos blocos leves; 30–60s nos pesados), ECharts pra gráficos,
reusa `dashboard/templates/dashboard/_partials/` já premium. Tema escuro atual. Cloudflare X-Frame ok (mesma origem).

### Layout (top → bottom)
```
┌─ HERO: 3 esteiras (Vetorização · Classificação · Extração) ── % do universo com-autos ──┐
│   cada uma: gauge donut + velocidade real + ETA + sublabel honesto                       │
├─ STRIP KPI (6): docs/h · extrações/h · fila_write · DB conns · crédito/runway · $/1k     │
├─ FROTA: tabela filtrável (pods+LAN) GPU util/vram/temp/papel/workers  [já existe, reusar] │
├─ QUALIDADE (NOVO): recall@k · citation acc · erro retrieval-vs-LLM (donut) · thumbs 👍👎  │
├─ INFRA (NOVO, compacto): cache-hit HNSW · autovacuum status · p95 busca · índice/RAM      │
├─ CUSTO (NOVO): burn/h · runway · gasto acumulado · $/1k chunks · pods ativos              │
└─ GRÁFICOS: throughput 24h (embed/extração/classif) · fila 24h · GPU util fleet 24h        │
```

### Componentes premium (elevar o que já existe)
- **Sparklines** em cada KPI (mini-série 1h) — ECharts `grid` compacto.
- **Semáforo por bloco** (verde/amber/vermelho) ligado aos thresholds de alerta.
- **Skeleton loaders** no HTMX swap (sem "pisca").
- **Tooltip rico** explicando cada número (fórmula + fonte) — combate métrica-vaidade.
- **Densidade**: números grandes (kpi-num), unidades pequenas, mono pra valores.
- **Dark premium** consistente com o tema atual; acentos por pipeline (vetor=verde, extração=laranja, classif=azul).

### Endpoints JSON (Voyager `dashboard/views.py`)
- `command_data` — agrega: pipeline (proxy p/ Zordon `/api/vetorizacao/fleet` já existe) + crédito (QuickPod) +
  qualidade (Zordon `/api/eval/summary` NOVO) + infra (Prometheus `/api/v1/query` NOVO, poucos PromQL) →
  1 JSON. Cache curto (scheduler 5–30s → Redis, padrão que já usam).
- Segurança: chave/endpoints internos; não expor PromQL cru ao browser.

### Fonte de dados (reuso)
- Pipeline/frota: **já existe** (`vetorizacao_fleet_view.py` no Zordon, proxied). Só consolidar + estilizar.
- Crédito: QuickPod API (cachear no scheduler; não bater a cada request).
- Qualidade: novo endpoint no Zordon lendo a tabela de resultados do harness (§4).
- Infra: Prometheus HTTP API (`/api/v1/query`) pros ~8 números-chave.

## 4. A régua de avaliação (triangulada) — `zordon/eval/`

### 4.1 Ground truth como TRIANGULAÇÃO (Falcon=silver)
```
Falcon (silver) ── valor/natureza/ente/data por CNJ (join numero_autos↔numero_cnj)
Autos-texto     ── matcher determinístico (regex moeda/data + fuzzy) sobre chunks do CNJ
Modelo          ── saída da extração (extract_fields)
        └─ concordam ≥2 fontes independentes? → GOLD forte
        └─ divergência → fila resíduo (ProcessoValidacao, estratégia `falcon_disagree`)
                          → humano adjudica: modelo-certo / Falcon-certo / ambos / ambíguo
```
- **Weak-label de retrieval** (a métrica-raiz): valor/data/ente do Falcon → casa no **texto do chunk** →
  "o top-k conteve o chunk-verdade?". Matcher **estrito** → erro do Falcon vira **label faltando** (seguro),
  não label falso. Subproduto: **detector de erro do Falcon** (autos-texto ≠ Falcon → flag).
- **~1M labels de graça**; gate usa **500–1000 CNJs estratificados** (tribunal/ano/natureza/cauda difícil).

### 4.2 Métricas (ordem de ataque)
1. **recall@k cnj-filtered** (o chunk-alvo entrou nos 8 do `extract_fields`?).
2. **decomposição erro** (attribution): extração errou E chunk-verdade estava no contexto → erro-LLM;
   não estava → erro-retrieval. **Entregável de maior valor.**
3. **citation accuracy** (chat de autos + narrativa): parse `[N]`/CNJ → existe nos trechos? determinístico.
4. recall@k **halfvec vs fp32** (amostra) — fecha o risco criado na migração de hoje.
5. extração acc@cobertura por campo×tribunal (ligar valor/pss/ente que já vêm do Falcon).
6. faithfulness (fase tardia): entailment claim-level, judge **local** (gpt-oss), só se **κ≥0,6** vs 100 humanos.

### 4.3 Schema + gates
- Nova tabela `EvalRun` (Zordon): `id, versao_git, ts, dataset_hash, metricas jsonb (recall@k, cit_acc,
  err_retrieval, err_llm, extr_acc por campo), n_cnjs, baseline_bool`. `update_or_create` idempotente.
- Baseline JSON versionado (como `surv_strata.json`).
- **Gate CI/pré-deploy** (roda no set de 1000):
  - BLOCK recall@8 (cnj) cair > 2pp vs baseline · BLOCK extr_acc(valor|natureza|data) cair > 1pp ·
    BLOCK citation_acc < 0.98 · WARN faithfulness cair · WARN p95 retrieval subir > 30%.
- **Drift drill obrigatório**: quebrar de propósito (`_K0`=1000 / ANN off) → confirmar vermelho → confirmar rollback.
- Endpoint `GET /api/eval/summary` (Zordon) → último EvalRun + série → Command Center bloco Qualidade.

### 4.4 Arquivos-âncora
- `zordon/eval/run_eval.py` — estender (métricas + gate + attribution).
- `zordon/eval/build_groundtruth.py` — já faz join Falcon; add weak-label de chunk + triangulação.
- `zordon/acervo/search.py` — alvo do A/B (`_K0=60`, `rerank_k=30`, `ef_search`, halfvec).
- `zordon/acervo/chunker.py` — sondar partição de campos (2800/320).
- `zordon/acervo/extract_fields.py` — instrumentar attribution (quais chunks entraram).
- `zordon/acervo/autos_chat.py` + `voyager/dashboard/jurimetria_narrativa.py` — citation checks.

## 5. Tuning (depois da régua) — trocar defaults cargo-cultados por medidos
- **A/B rerank on/off** (aposta: empata/piora em parte das queries → se empatar, desliga, economiza GPU).
- Varredura `_K0`/`ef_search`/`fts_k`/`ann_k`. halfvec vs fp32. Chunking estrutural p/ ofício/dispositivo.
- Cada mudança passa pelo gate (§4.3). Nada "no feeling".

## 6. Infra observability (Prometheus) — Fase 0
- `postgres_exporter` (aponta pro acervo DB) → Prometheus. Cobre §2.2 quase todo.
- **gpu-bridge**: script que lê hash Redis `vetor:gpu`/`extracao:gpu` → expõe `/metrics` → Prometheus scrape.
- QuickPod cost exporter (cron API → pushgateway).
- Grafana: dashboards de infra + Alertmanager. Reusa Postgres como store de nada novo (Prom tem o seu TSDB).
- **7 alertas** (cada um → runbook no OPS.md): fila_write runaway **E** autovacuum · cache-hit HNSW caindo ·
  p95 busca · DB saturado · GPU hang · custo/h pod · recall drift (do §4).
- **Não alertar**: trace lento isolado, score baixo de 1 query, groundedness absoluto, cada 4xx/5xx.

## 7. Tracing (Fase 3) — OTel desacoplado + Langfuse self-host
- OTel SDK instrumenta o span-tree RAG: `rag.query → embed.query → retrieve.fts → retrieve.ann →
  fusion.rrf → rerank → prompt.build → llm.generate` (atributos: scores por etapa, ef_search, template_version,
  tokens, custo, citações). Nomes `gen_ai.*` onde a semconv cobre (pinar versão); custom no resto.
- **OTel Collector** = ponto central de **redação de autos** + **tail-sampling** (100% ruins + thumbs-down
  vira full-trace; 1% bons). OTLP → **Langfuse self-host** (⚠️ verificar footprint ClickHouse antes de subir).
- Métrica agregada 100% (contadores) ≠ trace amostrado. Nunca confundir.

## 8. Fases (ordem de execução)

| Fase | Entrega | Peça nova | Dep |
|---|---|---|---|
| **A — Command Center premium** | página `/dashboard/command` consolidando pipeline+frota+custo (dados que já existem) + shells de Qualidade/Infra | — | — |
| **B — Infra metrics (Prom)** | postgres_exporter + Prometheus + Grafana + gpu-bridge + 7 alertas | Prometheus | — |
| **C — Régua de avaliação** | run_eval estendido (triangulado, recall@k, decomposição, citation), tabela EvalRun, baseline+gate+drift-drill, endpoint /api/eval/summary | — | — |
| **D — Wire qualidade+infra no Command Center** | bloco Qualidade (de C) + bloco Infra (de B) preenchidos com número real | — | A,B,C |
| **E — Tuning medido** | A/B rerank + varredura params + halfvec-vs-fp32 (via gate) | — | C |
| **F — Tracing** | OTel SDK + Collector (redação) + Langfuse self-host, amostrado | Langfuse/OTel | B |

**Ordem sugerida:** A + B em paralelo (superfícies diferentes) → C → D → E → F.

## 9. O que NÃO fazer (consolidado)
- ❌ LangChain/LlamaIndex/Haystack/Ragas/DeepEval. ❌ LLM-judge p/ retrieval ou extração.
- ❌ Qualquer SaaS US tocando autos. ❌ Reescrever ECharts em Grafana. ❌ 3 stacks de obs sobrepostos.
- ❌ Tracear 100%. ❌ Tunar "no feeling". ❌ Tratar Falcon como gold. ❌ Cravar acurácia absoluta.

## 10. Riscos vivos (criados/expostos nesta sessão)
- **halfvec pode ter custado recall** (fp16) e ninguém mede → Fase C mede antes de confiar cegamente.
- **chunking (2800c) pode partir campos** → estamos embedando milhões com possível defeito → régua deve
  idealmente **gatear o full-corpus (1,15M)** antes de comprometer os ~85M chunks finais.

---

## 11. Fase C IMPLEMENTADA — régua triangulada + números medidos (2026-07-24)

> Implementado no Zordon `feat/extracao-metadados`. Rodado na host `zordon` contra o
> acervo DB real (106.6k processos / 2.18M docs ready / 19.1M chunks), amostra BOUNDED
> estratificada, read-heavy leve, DB sob carga de ingest (fail-soft, sem tocar índice/writer).

### 11.1 Código entregue (arquivos)
- `zordon/eval/matcher.py` — matcher determinístico ESTRITO (moeda BR ±2¢, data dd/mm/aaaa +
  extenso, ente, natureza-keyword). Erro do Falcon → label FALTANDO, nunca falso.
- `zordon/eval/build_groundtruth.py` — **reescrito**: GT direto do acervo via `Processo.falcon_process_id`
  (sem manifest) + Falcon silver + weak-label de chunk-verdade + resíduos `falcon_disagree`.
  Corrige a "casca vazia": o `LEFT JOIN LATERAL ... WHERE valor_principal IS NOT NULL` do código
  antigo dropava TUDO (essa coluna é 100% nula no cohort linkável); GT agora usa
  `datamodel_process.{natureza,valor_acao,data_oficio}` (cobertura ~100%).
- `zordon/eval/metrics.py` — recall@k, decomposição erro retrieval-vs-LLM, percentis, extr_acc@cobertura
  por campo×tribunal, gates delta-based.
- `zordon/eval/timing.py` — instrumentação de tempo/attribution fail-soft (thread-local; no-op em prod).
- `zordon/eval/citation.py` — parse determinístico `[N]`/CNJ (chat + narrativa).
- `zordon/eval/run_eval.py` — **estendido** (compat preservado): subcomandos `baseline|halfvec|chunking|pareto|gate|drift`.
- `zordon/acervo/search.py` — hooks de tempo fail-soft por etapa (embed/fts/ann/rrf/rerank) + param
  `ef_search` (default None = comportamento de prod inalterado; só o eval passa != None).
- `zordon/acervo/extract_fields.py` — attribution fail-soft (quais chunk_ids entraram no contexto do LLM).
- `zordon/acervo/models.py` + migration `0007_evalrun` (aplicada) — tabela `EvalRun` (versionado, idempotente).
- `zordon/acervo/api.py` + urls — `GET /api/eval/summary` (último run + série + baseline; Command Center).

**Instrumentação de attribution/tempo e `ef_search=None` são fail-soft → foram pra prod.**
**Mudança de parâmetros de retrieval (rerank/ef) NÃO foi aplicada — só medida (aguarda decisão).**

### 11.2 Ground truth triangulado (amostra n=600, seed=42, dataset_hash=79f1cd4e…)
- Universo GT-ável (chunks ready + `falcon_process_id`): **36.916** (de 106.6k — o resto **não** linka Falcon).
- **⚠️ Falcon só linka TJSP hoje.** Estratos efetivos = ano×natureza dentro de TJSP. Outros tribunais
  (TJAL/TJMA/…) entram só com weak-label de retrieval, sem GT de campo.
- Cobertura de campo (silver): natureza 596/600, valor_acao 598/600, data_oficio 600/600.
- **Weak-label de retrieval OK em 589/600 (98,2%)** — o matcher estrito achou o valor/data-verdade no
  TEXTO de algum chunk. Base sólida pra recall@k.
- **63 resíduos `falcon_disagree`** (Falcon-natureza ≠ sinal-texto) → fila de adjudicação humana
  (Voyager `ProcessoValidacao`) + detector de erro do próprio Falcon.

### 11.3 Baseline de retrieval (n=150, rerank ON, config de prod)
| métrica | valor |
|---|---|
| recall@1 | **72,4%** |
| recall@4 | **93,8%** |
| recall@8 (o que alimenta a extração) | **95,2%** |
| p50 / p95 por query | **2,70s / 3,27s** |

Latência por etapa (mean, soma sobre as 5 anchor-queries/CNJ):
**rerank 1,73s (67% do wall)** · embed 0,64s · ann 0,11s · fts 0,05s · rrf 0,0001s.
→ o reranker DOMINA a latência.

### 11.4 halfvec vs fp32 (risco da migração de hoje — FECHADO)
n=240 amostras (ANN escopado por CNJ): **recall@8 halfvec = fp32 = 0,9083 · delta = 0,0pp.**
**A migração halfvec (fp16) NÃO custou recall.** Pode confiar. (Cast `::halfvec(1024)` casa o índice HNSW.)

### 11.5 Chunking parte campos? (risco #10)
n=185 CNJs com valor E data achados no texto: **35,1% (65/185) têm valor e data em chunks DIFERENTES.**
Para ~1/3 dos processos, o chunking 2800c separa os dois campos-alvo do ofício. Não quebra a extração
(que vê 8 chunks), mas é sinal real a favor de **chunking estrutural do ofício/dispositivo**.

### 11.6 Fronteira de Pareto (A/B rerank + ef_search; n=40) — MEDIDA, NÃO aplicada
| config | recall@8 | p50 | p95 | throughput |
|---|---|---|---|---|
| rerank_on (prod) | 0,950 | 2,75s | 3,38s | 1.207 q/h |
| **rerank_off** | **0,875** | **0,76s** | **1,05s** | **4.829 q/h** |
| rerank_on ef40 | 0,950 | 2,75s | 3,21s | 1.408 q/h |
| rerank_on ef100 | 0,950 | 2,73s | 3,31s | 1.353 q/h |
| rerank_on ef200 | 0,950 | 2,79s | 3,34s | 1.260 q/h |

**Fronteira (não-dominados): `rerank_off` e `rerank_on_ef40`.**
- **Rerank compra +7,5pp recall@8 (0,875→0,95) a 3,2× de p95 (1,05→3,21s) e 3,4× menos throughput.**
- **`ef_search` (40/100/200) é irrelevante** aqui: recall 0,95 constante, latência ~igual. Default OK,
  nada a ganhar tunando ef.

**Ponto de operação RECOMENDADO (decisão do usuário — quanto de acurácia trocar por tempo):**
- **BATCH (extração de milhões, throughput = $):** `rerank_off`. 3,4× throughput (4.829 vs 1.207 q/h),
  −7,5pp recall. Como a extração vê 8 chunks e o LLM tolera contexto ruidoso, provável que a acurácia
  de campo caia << 7,5pp — **medir a extr_acc em rerank_off antes de cravar** (próximo passo barato).
- **INTERATIVO (chat/busca, p95 = UX):** manter `rerank_on` (ef default). +7,5pp recall vale o p95 de
  ~3,3s num chat. Se UX exigir <1,5s, `rerank_off` é o fallback (−7,5pp).

### 11.7 Extração + decomposição de erro (retrieval-vs-LLM)
Extração via gpt-oss:20b local (regime BATCH). Números medidos:
- **Latência de extração: p50=89s, p95=120s por CNJ; throughput = ~40 docs/h** (gpt-oss:20b na
  3090 compartilhada). Este é o custo dominante do regime batch — o LLM, não o retrieval.
- **Decomposição de erro FUNCIONA** (via attribution dos chunk_ids no contexto): classifica cada
  erro de extração em `erro_llm` (chunk-verdade ESTAVA no contexto, LLM extraiu errado) vs
  `erro_retrieval` (chunk-verdade nunca chegou). Ex. medido: erro observado era `erro_llm` (o chunk
  estava lá) → aponta que o gargalo de acurácia é o LLM/prompt, não o retrieval (coerente com
  recall@8=95%). **É o número que diz ONDE investir.**
- extr_acc por campo×tribunal (natureza/valor/data) instrumentado e persistido; concordância
  (delta-friendly) por campo. Amostra maior em andamento (o LLM a ~90s/CNJ é o gargalo).

### 11.8 Persistência + gate + drift drill
- `EvalRun` gravado (migration `0007_evalrun` aplicada no acervo DB). `GET /api/eval/summary` no ar.
- **Gate delta-based validado** (teste sintético): recall@8 −5pp → BLOCK; extr_acc −5pp → BLOCK;
  citation 0,99 → pass; p95 dentro de 1,3× → pass. Regras: BLOCK recall@8 −2pp · extr_acc −1pp ·
  citation<0,98 · WARN faithfulness · WARN p95 +30%.
- **Drift drill** (`run_eval.py drift`): degrada o retrieval de propósito (rerank OFF) → recall cai
  0,95→0,875 (−7,5pp >> −2pp de BLOCK) → gate acende VERMELHO. Confirma que a quebra aparece.
- Baseline JSON versionado (`--set-baseline` → `eval/baseline.json`, gitignored; o versionado que
  vale é o registro EvalRun com baseline=True).

### 11.9 Decisões que levo pro usuário (o ponto de operação é seu)
1. **rerank em BATCH:** desligar troca −7,5pp recall por 3,4× throughput (40→~136 docs/h só no
   retrieval; mas o LLM a 90s domina o batch, então o ganho real de rerank-off no batch é MENOR do
   que parece — o gargalo é o LLM). Recomendo medir extr_acc com rerank-off antes; provável que a
   acurácia de campo caia << 7,5pp.
2. **rerank em INTERATIVO:** manter ligado (p95 ~3,3s, +7,5pp recall). Só desligar se UX exigir <1,5s.
3. **ef_search:** não mexer — sem ganho medido.
4. **halfvec:** manter — zero perda de recall confirmada.
5. **chunking:** avaliar chunking estrutural do ofício (35% dos campos-alvo caem em chunks distintos).
6. **gargalo de acurácia = LLM** (erro_llm domina, retrieval@8=95%): investir em prompt/modelo de
   extração rende mais que tunar retrieval.

---

## 12. Capítulo DSPy — otimização do prompt de `natureza` (síntese de 2 especialistas + crítica)

> **Tese:** o recall@8 já é 95% → o lever de acurácia é o **LLM/prompt**, não o retrieval (§11.9.6).
> `natureza` é o pior campo (~53%). DSPy compila **offline** uma instrução melhor contra a régua
> triangulada (Falcon=silver) e a gente **cola só a string** no prod (zero dep de runtime).

### 12.0 Barras inegociáveis (o que impede Goodhart)
- **DSPy NUNCA em prod.** `grep -rn dspy acervo/` tem que dar vazio para sempre. Só o **texto
  compilado** (instrução + demos) entra no `_SYSTEM_PROMPT` de `extract_fields.py`.
- **Métrica triangulada, silver-aware.** Nunca "concordância-Falcon pura" (isso compila o erro do
  Falcon). Score por-exemplo em 3 zonas (gold-forte / divergente / Falcon-só), abstenção com
  crédito parcial, e **matriz de confusão assimétrica** (COMUM→ALIMENTAR custa 2× — falso lead-quente
  é o erro caro no negócio de precatório).
- **Teto = (1−ε).** ε = taxa de erro estimada do Falcon em natureza (estimar via os 63 resíduos
  `falcon_disagree` adjudicados + auditoria de amostra gold). Concordância-Falcon acima de (1−ε) =
  otimizar ruído → parar.
- **Compilar no modelo de PROD** (`gpt-oss:20b`, `ZORDON_LLM_MODEL` — **não** qwen2.5:7b; correção
  do especialista). Compilar num modelo e servir noutro quebra a transferência do prompt.
- **Gate = amostra HUMANA (κ), não Falcon.** Sobe-vs-Falcon-mas-cai-vs-humano = **rejeição
  automática**. É a definição operacional de "aprendeu o viés".
- **Soberania:** otimização num pod cloud via tailscale, Ollama local no pod; o **reflection_lm da
  GEPA também local** (lê traces com conteúdo de autos → jamais API cloud US).

### 12.1 Optimizer: **GEPA**, não MIPROv2
O problema de natureza é a *regra de classificação* (instrução), não falta de exemplos. GEPA lê o
**feedback textual** da métrica e reescreve a instrução (MIPROv2 joga esse texto fora). GEPA é ~35×
mais barata em rollouts. `auto="light"` no spike (~1–2k chamadas, ~1–2h GPU).

### 12.2 Métrica (`eval/dspy_opt/metric_natureza.py`) — o coração
Retorna `dspy.Prediction(score∈[0,1], feedback=str)`. Regras (silver-aware):
- pred == Falcon == texto → **1.0**
- resíduo divergente e pred == **texto** (leu os autos contra Falcon suspeito) → **0.9**; pred==Falcon → 0.7; erra ambas → 0.0
- só-Falcon (texto None) e concorda → **0.8** (silver, não 1.0); discorda → 0.1
- só-texto e concorda → 0.85
- **abstenção:** caso genuinamente ambíguo → 1.0; caso decidível → 0.3 (perda pequena, nunca punição de alucinação)
- **assimetria:** erro `pred=ALIMENTAR, gt=COMUM` custa 2× o inverso (empurra abstenção conservadora na fronteira que enche o funil de lixo).

### 12.3 Held-out honesto (o que faltava no código)
- `build_groundtruth.py --n 1000 --seed 42` (estratificado por tribunal/ano/natureza — já faz).
- **NOVO: `split` por hash-do-CNJ** (determinístico, à prova de reembaralho entre iterações):
  `sha256(cnj)%100 → train<40 / dev<65 / test<100` = **40/25/35 → test ≈ 350** (≥250 = poder p/
  cravar +5pp em binário 53%; McNemar pareado antes/depois). Estratificar o próprio split por
  natureza+tribunal. Reportar overlap de ente entre splits (clones do mesmo lote/ente vazam mais que CNJ).
- Enriquecer cada linha do GT com `natureza_texto_signal` (3ª testemunha, já gravado) + flag `decidivel`.
- **CI 95% bootstrap** obrigatório no delta (nunca ponto-estimativa nua): n=350 crava +5pp, é fraco p/ +2pp.

### 12.4 Módulo (`natureza_signature.py`) — determinístico-first + abstenção
`dspy.ChainOfThought(ClassificaNatureza)`; `forward()` respeita **determinístico-first** (se Falcon/regra
já resolveu com confiança, não chama LLM) e normaliza saída p/ `ALIMENTAR|COMUM|ABSTER`. Contexto **roteado**
(3–4 chunks via anchor `natureza` + `natureza_signal`), não os 8 chunks misturados → menos ruído = menos erro_llm.

### 12.5 Extrair o prompt → string (zero runtime dep)
`compiled_natureza.json` (instrução + demos selecionadas) → `extract_prompt.py` imprime `sig.instructions`
+ `predictor.demos` → **substitui exatamente os blocos "CLASSIFICAÇÃO DA NATUREZA" + "EXEMPLOS" do
`_SYSTEM_PROMPT`** (`extract_fields.py:71–117`). Nenhum import novo. Versionar: `compiled_natureza.json`
commitado + `prompt_hash=sha256(instructions)[:16]` no `EvalRun` (reusa `versao_git`, sem `ExtratorVersao` novo).

### 12.6 Gate de aceitação (adaptar `evaluate_gates`)
Rodar `run_eval.py gate` (já chama `extract_fields` real) no **held-out test** antes/depois:
- **PROMOVE** só se `natureza +≥5pp` (ganho buscado, exige > ruído do held-out).
- **BLOCK** se valor/data regredirem >1pp (prompt é um só — natureza mal-escrita contamina os outros).
- **NOVO (lacuna real): gatear cobertura E precisão-sobre-respondidos**, não só `acc_sobre_gt`. Senão o
  otimizador **ganha o gate abstendo mais**. Piso de cobertura ⇒ **decisão de negócio (default 70%)**.
- **Gate WARN:** descolamento acc-vs-Falcon × acc-vs-texto (sintoma canônico de Goodhart) e TJSP × não-TJSP >10pp.

### 12.7 Generalização (risco #1 — GT é TJSP-only, 36,9k de 106,6k)
1. Held-out TJSP nunca visto (test 35%) — overfit *dentro* de SP.
2. **Weak-label cross-tribunal:** rodar candidato em TJMA/TJAL/TRFs onde só há `natureza_signal` (texto,
   sem Falcon). Sobe em SP + **cai** fora → aprendeu o dialeto e-SAJ paulista → **rejeitar**.
3. **Adjudicar os 63 resíduos** (`ProcessoValidacao`/`falcon_disagree`, dupla anotação + κ) → micro-gold
   set + estima ε do Falcon. Determinístico-first permanece a rede (degrada p/ "abster" fora de SP, não chuta).

### 12.8 Onde rodar (sem matar os 11,5k/h)
**Pod cloud dedicado** (3090/A2000, QuickPod, ~$0.10 pela janela), Ollama+gpt-oss:20b, `ZORDON_LLM_BASE_URL`
via **tailscale** (hostname tailscale conta como local → `_is_local_embed_host` feliz; reflection_lm no pod
também). **Off-limits:** a 3090 `llmsv2` do timer `zordon-extrair` (roubar derruba o backlog de 59k).

### 12.9 Fases + decisões de negócio pendentes
| Fase | Entrega | Gate de saída |
|---|---|---|
| 0 Held-out honesto | n=1000 + split CNJ-hash 40/25/35 + enriquecer GT + baseline EvalRun no test | test ≥250 c/ natureza |
| 1 Spike natureza | venv-dspy + módulo + métrica triangulada + GEPA light no pod | prompt compilado extraído |
| 2 Gate | colar no `_SYSTEM_PROMPT`, `run_eval.py gate` no held-out | +≥5pp natureza, valor/data sem regressão, cobertura não cai |
| 3 Generalização | weak-label cross-tribunal + adjudicar 63 resíduos | concordância-texto não cai fora de SP |
| 4 Promover/matar | passou tudo → commit prompt + json versionado; senão descarta (fica o diagnóstico) | EvalRun baseline=True novo |
| 5 Expandir | só se natureza funcionou: repetir p/ `valor`/`data` no MESMO harness | — |

**⚠️ 2 knobs de negócio (defaults assumidos, o usuário overrida no checkpoint κ):**
1. **Piso de cobertura de natureza = 70%** (otimiza precisão sujeita a essa restrição).
2. **Assimetria COMUM→ALIMENTAR = 2×** ALIMENTAR→COMUM (falso lead-quente = erro caro).

