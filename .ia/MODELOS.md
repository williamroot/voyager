# Registro de Modelos — extração de metadados dos autos

> Model cards versionados + registry. Regra: **código e docs no git; pesos e
> datasets NUNCA** — aqui vive o hash (md5) + caminho que amarra cada versão.
> Atualizar este arquivo **na mesma PR** de qualquer treino/promoção/morte.

## Registry

| modelo | versão | status | artefato | onde |
|---|---|---|---|---|
| extrator-precatorio | **v1** | 🟡 empacotado (gate PASS, não deployado) | GGUF Q4_K_M `01cd53ff…ebf2` | llmsv2 `/mnt/nas-data/voyager-train/out/` |
| extrator-precatorio | **v2 "Ficha da Parte"** | 🟡 gate PARCIAL — empacotado (amostra κ: ver card v2) | GGUF Q4 `59db32db…1de3` · adapter `5206de77…b333` | llmsv2 `out/precatorio-extrator-v2-q4_k_m.gguf` + `Modelfile.v2` |
| extrator-precatorio | **v2.1** | 🟡 **gate PASS macro 91,76 — empacotado (CAMPEÃO)** | GGUF Q4_K_M `0012607b1634e7b8f96c8f6a9d7bad21` · `adapter_v21` | llmsv2 `out/extrator-v21-Q4_K_M.gguf` (4,68GB) |
| extrator-precatorio | **v2.2 (herdeiros) — ablação de rótulo** | 🔴 **gate BLOCK (03/09)** — 3 de 4 critérios passam (bate o v2.1 **+21,39pp** e a base crua **+6,59pp**, e melhora `falecido` +6,95pp), mas **reprova a guarda de emissão indevida** (12,86% vs teto 9,07%). **Hipótese confirmada:** trocar só o RÓTULO vale **+17,88pp**, IC [+13,72, +21,97] | `adapter_herd_v22` (md5 `28ddcbff2836b7f89558f6ac7f1f8ded`) + `adapter_herd_v21` (controle) — **não promovidos** | llmsv2 `out/` · resultados `out/esp_gate/r120_final_v2{1,2}.json` |
| extrator-precatorio | **A/B Qwen3-8B** | 🔴 **gate não passou** (+1,6 macro <2; regride DOC_PESSOAL) → fica no Qwen2.5-7B | `adapter_ab_qwen3_v21` | pod 4090 |
| extrator-precatorio | **especialistas (7 LoRA por classe)** | 🔴 **gate BLOCK (02/09)** — nenhum dos 7 bate o v2.1 no domínio dele: Δ de **−0,52pp a +0,02pp**, IC 95% pareado exclui o +3pp exigido nas SETE. Família 0 de 3. Não promovidos, roteamento por `doc_classe` cancelado | `adapter_esp_{acordao,cessao,decisao,herdeiros,oficio,pagamento,partes_doc}` — **não promovidos** | llmsv2 `out/` · resultados `out/esp_gate/resultado_*.json` |
| extrator-precatorio | **DAPT** | 🔴 **gate BLOCK (03/08)** — SFT-sobre-DAPT não bate SFT-direto: v2 macro **+1,08pp**, v1 **+0,06pp** (critério era ≥+2pp); `partes` até PIORA (0,5139→0,5081) | `adapter_dapt` (r=64 all-linear, 646MB) — **não promovido** | llmsv2 `out/adapter_dapt` |
| classificador-leads | v7 | 🟢 ativa | — | ver `.ia/CLASSIFICACAO.md` |
| emulador-autos (GBM) | — | ⚪ planejado | — | ver `.ia/EMULADOR_AUTOS.md` |
| **estagio-credito (GBM)** | **estagio_v1** | 🟡 gate PASS (EMITIDO p=0,954 · MORTO p=0,926 · thr_morto 0,80) — empacotado, não plugado em views | joblib `6bf0c7a4…9e36` | curiosity `voyager/scripts/estagio/out/` · lib `tribunals/estagio.py` · doc `.ia/ESTAGIO_CREDITO.md` |

**Datasets versionados por hash** (dados fora do git; md5 aqui é o vínculo):

| dataset | md5 | conteúdo | gerador |
|---|---|---|---|
| `train_extracao_v2.jsonl` (v1) | `277f07b5cc2a09e58166a339095faed1` | 10.850 ex-ouro por-processo | zordon `eval/build_train_extracao.py` |
| `train_fichaparte_full.jsonl` (v2) | `177f4f0ed7ba2a9768f90df28d14e4ed` | 196.360 ex por-documento | zordon `eval/build_train_fichaparte.py` |
| dataset **v2.1** | `2b90e642…` | **211.928 ex** (37.146 longos recuperados via janelamento) | zordon commit `bd4b2b9` |
| `herd_gold_v22.jsonl` | 1.189 docs re-rotulados (professor DeepSeek, κ 9/10 nos recuperados) — traz `{nome, papeis}` | fatia `herdeiros` do mix v2.2: vazio **50,08%** (train) / **51,95%** (test) contra 73,44/75,88 do v2.1; 96,4% dos nomes são **literais** no texto | ⚠️ o mix **descarta os `papeis`** e treina lista chapada de nomes — causa medida do BLOCK de 03/09 |
| **DPO pairs v1** (`eval/dpo_pairs_v1.jsonl`) | — (auditoria em `eval/relatorio_dpo_pairs.md`) | 2.688 pares (1.289 forte×fraca / 27 correções humanas / 1.372 abstenção) | zordon `eval/build_dpo_pairs.py` |
| `estagio_labels.jsonl.gz` | `658258043d640b556a6e2c98eb6f2759` | 820.777 rótulos de estágio (supervisão cruzada autos×Falcon, evidências auditáveis) | voyager `scripts/estagio/build_labels.py` (roda no zordon) |
| `estagio_features.csv.gz` | `c80691c8e69de3c23371b3af1529a39d` | 99.667 linhas de features 100% públicas (snapshot 2026-07-30) | voyager `scripts/estagio/build_features.py` (roda no host voyager) |

Status: 🟢 ativa · 🟡 empacotado/shadow · 🔵 treinando · 🟠 artefato sem gate ·
🔴 morto (gate BLOCK) · ⚫ interrompido sem veredito (≠ morto: ninguém mediu) ·
⚪ planejado

Mortos com autópsia (não repetir o caminho): **`herdeiros` treinado em alvo sem papel**
(gate BLOCK 03/09 — o rótulo v2.2 conserta 18pp de silêncio mas, com os `papeis`
descartados no mix, o modelo aprende *nome perto de espólio = herdeiro* e emite
advogado/inventariante/polo passivo: `.ia/EXPERIMENTOS_MODELO.md` §5.3),
**especialistas LoRA por classe**
(gate BLOCK 02/09 — o v2.1 já está em 98-99,5% em 6 das 7 tarefas; interferência
multi-task não era o gargalo, e onde há espaço o gargalo é o RÓTULO:
`.ia/EXPERIMENTOS_MODELO.md` §2.3), **DAPT** (gate BLOCK 03/08 — o
pré-treino no dialeto próprio não paga o custo; `.ia/LABLOG.md`), prompt DSPy/GEPA
natureza (Goodhart, `.ia/RAG_EVAL_OBS.md`), quantização bit-index (gate recall
BLOCK, `.ia/QUANTIZACAO_INDICE_VETORIAL.md`), pgvectorscale SBQ (limite estrutural
>930d, `.ia/PGVECTORSCALE_SBQ.md`), A/B Qwen3-8B (base alternativa não compensa).

---

## Model card — **especialistas LoRA por classe** (treinados 02/08, gate 02/09/2026)

**O que eram.** 7 adapters QLoRA r=32 sobre Qwen2.5-7B-Instruct, um por tarefa
(`acordao, cessao, decisao, herdeiros, oficio, pagamento, partes_doc`), cada um treinado
SÓ na fatia da sua tarefa (2-4 épocas). Hipótese: as classes fracas perdiam no
multi-task por **interferência**, e um adapter dedicado aprenderia o template sem
competir por capacidade. Serving previsto: troca de adapter roteada por `doc_classe`.

**Gate (02/09, R115).** Held-out `test_mix_v21` (`split=test`, o mesmo do macro 91,76),
slice por tarefa gravado uma vez e lido pelos dois modelos → **pareamento exato por
exemplo**. Métrica = `score_v2()` do `eval_gate_v2.py` sem alteração. Significância =
McNemar exato + bootstrap pareado 10k. Critério pré-registrado e **commitado antes de
medir** (`c714f8f`): Δ ≥ +3pp E IC 95% > 0 E significância.

| tarefa | n | v2.1 | especialista | Δ | IC 95% |
|---|--:|--:|--:|--:|---|
| acordao | 707 | 98,40% | 98,28% | −0,12pp | [−0,85, +0,64] |
| cessao | 800 | 98,31% | 98,33% | +0,02pp | [−0,62, +0,69] |
| herdeiros | 478 | 83,71% | 83,20% | −0,52pp | [−2,22, +1,07] |
| decisao | 737 | 99,54% | 99,43% | −0,10pp | [−0,32, +0,11] |
| oficio | 797 | 99,07% | 98,89% | −0,18pp | [−0,45, +0,07] |
| pagamento | 702 | 98,22% | 98,10% | −0,12pp | [−0,69, +0,50] |
| partes_doc | 709 | 78,05% | 77,68% | −0,37pp | [−1,77, +1,10] |

**Veredito: 🔴 BLOCK nos 7. Família BLOCK (0 de 3).** Nenhum IC inclui o +3pp exigido.

**Controles.** C1 (reproduzir o ACÓRDÃO 98-100 do gate de 31/07) ✅. C2 (base sem adapter
≥20pp abaixo do v2.1) ✅ em 5 de 7 — falha em `cessao` (+15,35) e `decisao` (+11,83), que
são tarefas de teto onde a barra absoluta de 20pp é inatingível por construção. Barra
**não** foi afrouxada; a correção (medir em redução de erro) fica pré-registrada para o
próximo ciclo.

**Achado colateral que vale mais que o experimento.** Em `herdeiros.f1_entidade` (n=130):
v2.1 **27,92%** · especialista **36,44%** · **base SEM fine-tune 40,01%** — a base crua
bate o campeão por **+12,09pp (IC [+3,42, +20,82], exclui o zero)**. Confirma o
diagnóstico de 31/07: com 73-76% do gold vazio, o SFT **ensinou o modelo a não emitir
herdeiros**. Conserto = rótulo (o gold v2.2 já existe no NAS), não adapter.

**Limitações do gate.** Geração livre, sem grammar GBNF (igual ao gate v2.1) — daí os
4,6% de JSON inválido em `partes_doc`, presentes nos DOIS modelos. Não mediu o
`herdeiros` sob o gold v2.2 re-rotulado.

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

**Amostra κ humana v2 (60 docs do TEST, pré-análise IA — 29-30/07).**
Régua: ✓ correto · ✗ = **afirmação falsa** (o grave) · ? = abstenção com
evidência (o tolerável). Pré-análise IA: **28✓ / 5✗ / 27?**.

| classe | precisão na amostra | observação |
|---|--:|---|
| DESPACHO / DECISAO / ALVARA | **100%** | consistente com o gate automático |
| PROCURACAO | **62%** | 3 afirmações falsas GRAVES: CPF trocado entre outorgantes (×2), nome truncado + papel errado |

Achado colateral: **ALVARA real ≈100% do subtipo "levantamento"** na amostra
→ v2.1 já treina esse subtipo. As 3 falsas-afirmações de PROCURACAO são
exatamente o alvo do DPO-κ (`.ia/EXPERIMENTOS_MODELO.md` §3); as 27
correções humanas verificadas viraram pares (b) do dataset DPO. Amostra
navegável: `.ia/kappa_amostra_v2.html`.

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
