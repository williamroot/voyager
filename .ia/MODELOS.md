# Registro de Modelos — extração de metadados dos autos

> Model cards versionados + registry. Regra: **código e docs no git; pesos e
> datasets NUNCA** — aqui vive o hash (md5) + caminho que amarra cada versão.
> Atualizar este arquivo **na mesma PR** de qualquer treino/promoção/morte.

## Registry

| modelo | versão | status | artefato | onde |
|---|---|---|---|---|
| extrator-precatorio | **v1** | 🟡 empacotado (gate PASS, não deployado) | GGUF Q4_K_M `01cd53ff…ebf2` | llmsv2 `/mnt/nas-data/voyager-train/out/` |
| extrator-precatorio | **v2 "Ficha da Parte"** | 🟡 gate PARCIAL (29/07) — classes fortes prontas | `out/adapter_v2` + GGUF v2 (empacotando) | llmsv2 idem |
| classificador-leads | v7 | 🟢 ativa | — | ver `.ia/CLASSIFICACAO.md` |
| emulador-autos (GBM) | — | ⚪ planejado | — | ver `.ia/EMULADOR_AUTOS.md` |

Status: 🟢 ativa · 🟡 empacotado/shadow · 🔵 treinando · 🔴 morto (gate BLOCK) · ⚪ planejado

Mortos com autópsia (não repetir o caminho): prompt DSPy/GEPA natureza (Goodhart,
`.ia/RAG_EVAL_OBS.md`), quantização bit-index (gate recall BLOCK,
`.ia/QUANTIZACAO_INDICE_VETORIAL.md`), pgvectorscale SBQ (limite estrutural >930d,
`.ia/PGVECTORSCALE_SBQ.md`).

---

## Model card — extrator-precatorio **v1** (28/07/2026)

**Tarefa.** Extração por-processo de 5 campos dos autos (RAG): natureza, valor_oficio,
cessao, ente_devedor, partes. Roteado por campo; serve com grammar GBNF.

**Base + receita.** Qwen2.5-7B-Instruct, QLoRA 4-bit nf4, LoRA r=32/α=32 (80,7M
treináveis), maxseq 4096, bs1×accum16, lr 2e-4 cosine, 3 épocas, grad checkpointing.
Stack: transformers 4.46 + peft 0.19 + bitsandbytes 0.50 + trl 0.12 (Unsloth
descartado — conflito de versões). 1× RTX 3090, ~10,4h. Train loss 0,53→0,158;
dev 0,093→0,073 (best-model por dev).

**Dataset.** `train_extracao_v2.jsonl` md5 `277f07b5cc2a09e58166a339095faed1` —
10.850 exemplos-ouro triangulados (Falcon∩texto, regex e-SAJ, LLM∩Falcon; ente via
llm_solo), split por hash de CNJ. Gerador: zordon `eval/build_train_extracao.py`.
Fila κ: 76 (não usados no treino).

**Gate (TEST held-out por CNJ, n=3.804) — baseline → FT:**

| campo | base | FT |
|---|--:|--:|
| natureza | 82 | **99,1** |
| valor_oficio | 72 | **91,8** |
| cessao | 98 | **100** |
| ente_devedor | 92 | **94,7** |
| partes | 46 | **54,0** |
| **macro** | 78 | **87,9** |

**Artefatos (llmsv2 `/mnt/nas-data/voyager-train/`):**
- GGUF Q4_K_M 4,68GB: `out/precatorio-extrator-q4_k_m.gguf` md5 `01cd53ff77ae9f76d5c360a4bec1ebf2` (serve em ~6GB VRAM)
- Adapter LoRA: `out/adapter/adapter_model.safetensors` md5 `8ae52f83125a8928da742bd681ee9656`
- `out/Modelfile` (ChatML, temp 0) + `grammars/*.gbnf` (5)
- Registro: `ollama create precatorio-extrator -f out/Modelfile`

**Limitações conhecidas.** partes=54% é CONTEÚDO/cobertura, não formato (grammar:
0,533→0,529; só 2,5% JSON inválido) → motivou a v2. Sem exemplos de abstenção no
gold. Dataset ~75% TJSP + 20% TRF1. Falcon usado só como professor (treino).

---

## Model card — extrator-precatorio **v2 "Ficha da Parte"** (em treino, 28/07/2026)

**Mudança de formulação.** Extração **POR-DOCUMENTO** condicionada a `doc_classe`
(1 doc → registros JSON) + **merge determinístico** downstream
(`zordon acervo/ficha_parte.py`, 24 testes) que resolve entidades (CPF chave forte,
espólio ligado) e monta o ledger por parte: papel, valor a receber, **recebido[]
(alvarás)**, saldo DERIVADO, eventos + camada jurimetria (juiz POR DECISÃO, vara,
relator, desfecho por grau). Design: `.ia/FICHA_PARTE.md`.

**Dataset.** `train_fichaparte_full.jsonl` md5 `177f4f0ed7ba2a9768f90df28d14e4ed` —
196.360 exemplos por-documento (104.869 ouro / 82.268 prata / 9.223 abstenção),
55.847 CNJs, corpus vetorizado inteiro; κ=13.470 fora do treino. Formato cru
`{input,target,tarefa,split,concordancia}`. Gerador: zordon
`eval/build_train_fichaparte.py`. Pré-requisito F0: classe ALVARA no
`doc_classificador` → 171.667 alvarás reclassificados.

**Mix de treino.** Caps por tarefa no train (ouro primeiro, round-robin tribunal):
oficio≤5k, decisao≤5k, acordao≤2k, raras inteiras + v1×0,3 (retenção) =
17.500 → 14.273 pós-guard all-masked (>4096 tokens). Dev 10.945; test 72.818
(integral, sem cap). Colisão de split v1×v2: ZERO.

**Receita.** Idêntica à v1 exceto `--epochs 2` (amostras/época ~2× v1).
`train_peft_v2.py` + `mix_datasets.py` + `eval_gate_v2.py` (F1 de entidade
pós-canonicalização, métricas por doc_classe, gate de abstenção) +
`grammars/v2/*.gbnf` (7, reconciliadas contra o jsonl real, validadas no
llama-server).

**GATE EXECUTADO (29/07, n=3.762 TEST held-out, 4,8h) — veredito PARCIAL.**
✅ Prontas: ALVARA 100/100/100 (beneficiário/valor/data), PAGAMENTO 100/99,5/100,
DECISAO juiz 99,0/vara 99,4/data 99,5 (jurimetria), DESPACHO 94-97, PROCURACAO
F1-entidade 72,4 + papel 77,6 (partes saltou de 54 → ~72-78), OFICIO benef 84/
natureza 92/valor_individual 79. Retenção v1: macro 87,2 (≥85 ✓, natureza 99,55).
Abstenção: 0% falsa-abstenção em todas as classes ✓✓.
❌ Reprovadas (causa = DADO, prevista nos riscos): ACORDAO 16-27 e SENTENCA 60-80
(docs longos — os 3.227 dropados >4096 tok; treino não viu, teste vê truncado),
CESSAO 25-45 (269 exemplos de treino), herdeiros/óbito 11-31, casamento ~0.
MACRO v2 bruto 66,5 (puxado pelas fracas). Critério partes ≥75: 72,4 → não promove
o modelo INTEIRO; uso por-classe (fortes) possível após κ humano.
Levers v2.1 confirmados pelo gate: janelar docs longos no gerador + gold melhor
de cessão/herdeiros/casamento. A/B de base (Qwen3-8B) disparado na sequência.

**Critérios de promoção (pré-registrados).** (a) partes: F1 de entidade ≥0,75 no
test v2; (b) retenção v1: macro ≥85 (não regredir natureza/cessao/valor/ente);
(c) abstenção: falsa-abstenção ≤5%; (d) κ humano em amostra de fichas completas
antes de qualquer deploy (§12.0 — nunca direto em prod).

**Riscos registrados no lançamento.** Drop de 3.227 exemplos longos (viés contra
acórdãos/escrituras grandes; fix futuro = janelar no gerador); partes_doc só 2,3k
no train; decisao/pagamento 100% TJSP; natureza do ofício em texto livre com ruído
de regex (normalizar no merger).

---

## Onde vive cada coisa (fora deste repo)

| o quê | onde | versionamento |
|---|---|---|
| Harness de treino (train_peft*, mix, gates, grammars) | llmsv2 `/mnt/nas-data/voyager-train/` | git local do diretório (scripts; sem out/data/logs) |
| Geradores de dataset + extração/merger | zordon `~/zordon/` (repo git) | commit por ciclo |
| **SDK standalone** (pdf→ficha, sem infra) | local `~/projetos/extrator-precatorio-sdk` (git main) | 53 testes; GGUF plugável via fetch_model.sh + md5 |
| Pesos/GGUF/datasets | llmsv2 NAS + zordon eval/ | NÃO versionados — md5 neste arquivo |
| Memória operacional da sessão | `~/.claude/.../memory/` | fora do repo (contexto do agente) |
