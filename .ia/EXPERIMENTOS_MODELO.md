# Experimentos de modelo — extrator de precatórios (ciclo pós-gate v2)

> 4 experimentos em curso sobre o extrator (`.ia/MODELOS.md`). Cada um tem
> **critério de sucesso pré-registrado** — decidido ANTES de rodar, pra não
> repetir o Goodhart do DSPy/GEPA (`.ia/RAG_EVAL_OBS.md`). Nenhum resultado
> promove modelo sem passar no seu critério + κ humano (§12.0: nunca direto
> em prod).
>
> **Dependência comum (RESOLVIDA em 31/07):** os experimentos 3 e 4 treinam SOBRE
> o "modelo vencedor". O vencedor É a **v2.1** (Qwen2.5-7B, gate PASS macro
> 91,76) — o A/B Qwen3-8B reprovou. Nada mais os bloqueia tecnicamente.

| # | experimento | hipótese em 1 linha | critério pré-registrado | status (triado 02/09/2026) |
|---|---|---|---|---|
| 1 | DAPT | pré-treino no dialeto do acervo melhora o SFT | SFT-sobre-DAPT ≥ **+2pp macro** vs SFT direto | 🔴 **BLOCK (03/08)** — v2 +1,08pp / v1 +0,06pp; `partes` piora. Morto, ver §1 |
| 2 | Especialistas | adapter por classe fraca vence o multi-task | ganha em **≥2 das 3** classes fracas por **≥3pp** (detalhe pré-registrado em §2.2) | 🔵 **gate EM EXECUÇÃO (R115, 02/09)** — critério e procedência commitados antes de medir |
| 3 | DPO-κ | preferências da fila κ matam o chute | falsa-afirmação **−50%** sem perder cobertura **>2pp** | 🟡 dataset PRONTO (2.688 pares); nunca treinou |
| 4 | Destilação 3B | classes fortes cabem num aluno 3B | aluno retém **≥97%** do professor nas fortes | 🟡 harness pronto (dry-run ok); nunca rotulou |

> **Triagem 02/09/2026.** O "vencedor" que 3 e 4 esperavam JÁ EXISTE desde 31/07
> (**v2.1**, gate PASS macro 91,76) — a dependência que os travava caiu. O que
> travou de verdade foi INFRA: o **pod 4090 e o pod 3090 foram destruídos** e
> levaram junto o treino da v2.2 (dados no NAS, `adapter_v22` não existe). Nenhum
> destes 3 pendentes depende de mais pesquisa: dependem de subir GPU e rodar.

---

## 0. Treinos-base em curso — v2.1 × A/B Qwen3-8B (definem o "vencedor")

Os dois candidatos a modelo-base dos experimentos 3 e 4 treinam em paralelo
sobre o MESMO dataset v2.1:

| treino | base | onde | custo | progresso (29/07) | ETA |
|---|---|---|---|---|---|
| **v2.1** | Qwen2.5-7B-Instruct | llmsv2 (3090 própria) | — | época ~0,31 · dev loss 0,021 | quinta |
| **A/B** | Qwen3-8B | pod 4090 | $0,31/h (~$5,4 total) | época ~0,67 · dev loss 0,0106 | amanhã meio-dia |

**Dataset v2.1** (md5 e commit no registry `.ia/MODELOS.md`):

- **211.928 exemplos** por-documento — **37.146 docs longos recuperados via
  janelamento** no gerador (o lever nº 1 apontado pelo gate v2: ACORDAO 16%
  era efeito dos 3.227 dropados >4096 tok).
- Mix estratificado de treino: **24.367** (caps por tarefa, ouro-first).
- Colisões de split v1×v2: **ZERO** (hash-CNJ preservado).
- Guard all-masked: drop caiu de **3.227 → 10** (janelamento resolveu).

⚠️ **Anti-conclusão precoce:** o dev 0,0106 do Qwen3-8B é menor que o 0,021
do v2.1 no mesmo ponto, MAS **loss entre tokenizers diferentes NÃO é
comparável** (vocabulários e segmentações distintas → escalas distintas de
NLL por token). O juiz é o **gate** (`eval_gate_v2.py`, mesmo TEST held-out
por hash-CNJ), nunca a loss.

## 1. DAPT — Domain-Adaptive PreTraining

**Hipótese.** O texto dos autos (OCR ruidoso, tabelas DEPRE, fórmulas
cartoriais) é um dialeto que a base Qwen viu pouco; continuar o pré-treino
(next-token, sem rótulo) nesse dialeto reduz a perplexidade de base e faz o
MESMO SFT render mais — especialmente nas classes com pouco gold (cessão,
herdeiros), onde o prior importa mais que o dado supervisionado.

**Setup.** Corpus REAL gerado: **232.130 documentos INTEIROS**
(decriptados + de-overlap dos chunks), **1,16GB**, separador `<|endofdoc|>`:
zordon `scripts/gerar_dapt_corpus.py` → `~/fichaparte_tmp/dapt_corpus.txt`
+ stats tsv. Composição por classe × tribunal: **TJSP 86%**, TRF1 24,6M
tokens.

> **ACHADO (medido):** a régua "chars/4 ≈ tokens" SUBESTIMA em texto
> jurídico — no tokenizer do Qwen deu **~2,39 chars/token** (OCR ruidoso,
> nomes próprios, números longos fragmentam mais). O corpus de 1,16GB são
> **484,6M tokens reais**, não ~280M. Cap aplicado em **250M tokens**
> (61.035 blocos de 4096) pra caber no budget.

Treino (em curso, pod 3090 **$0,19/h**): LoRA causal-LM sobre
Qwen2.5-7B-Instruct, **r=64 all-linear (161,5M treináveis)** — DAPT pede
mais capacidade que o SFT r=32 —, packing 4096, **1 época = 3.814 steps**,
throughput medido **903 tok/s** → ETA **~76,5h (~$15)**. Supervisor com
auto-resume (lição da guerra de orquestradores, `.ia/LABLOG.md` 29/07).
Depois: SFT v2.1 idêntico (mesmo mix, seed, maxseq) sobre o checkpoint DAPT.

**Critério de sucesso (pré-registrado).** SFT-sobre-DAPT ≥ **+2pp macro**
vs SFT direto no MESMO test held-out por hash-CNJ (gate `eval_gate_v2.py`).
Abaixo disso o custo (horas de GPU a cada ciclo de dado) não se paga.

**Status: 🔴 BLOCK (03/08/2026, trainpod 4090, 294 min de eval em 3.727 held-out
`test_mix_v21`).** O critério NÃO foi atingido e o critério não foi afrouxado:

| | SFT-direto (v2.1) | DAPT+SFT | Δ |
|---|---|---|---|
| macro v1 | 0,8730 | 0,8736 | **+0,06pp** |
| macro v2 | 0,9176 | 0,9284 | **+1,08pp** |

Por-campo v1: natureza / cessao / **valor_oficio (0,9235)** idênticos;
ente_devedor +0,8pp; **`partes` −0,6pp (0,5139→0,5081) — o DAPT PIOROU o campo
fraco**, que era justamente a hipótese. `adapter_dapt` não promovido; **v2.1
segue campeão**. O adapter continua no NAS (`out/adapter_dapt`) só como
candidato a base do Analista-7B (`.ia/ANALISTA.md`) — para o extrator, morreu.

**A lição que vale mais que o experimento:** `valor_oficio` acerta **92% no
held-out limpo** e deu **0 nos 28 ofícios do processo real de 1,5 GB**. Logo o
gargalo do valor não é capacidade do modelo — é **documento real + roteamento/
janelamento + verificação verbatim**. O conserto do valor↔parte é **SDK (#86/P4)**,
não retrain.

## 2. Especialistas — adapter por classe fraca

**Hipótese.** ACORDAO/SENTENCA (docs longos), CESSAO e herdeiros perdem no
multi-task por interferência: são <5% do mix e têm formato/registro muito
diferente das classes dominantes. Um adapter LoRA POR CLASSE, treinado só na
fatia dela (com o dado v2.1 janelado + gold reforçado), aprende o template
sem competir por capacidade.

**Setup.** **7 adapters, treino SEQUENCIAL fracas-primeiro** (se o pod
morrer, as fracas — que são o ponto do experimento — já saíram), sobre a
mesma base e receita do v2 (QLoRA r=32), cada um treinado só na fatia da
tarefa com o dado v2.1 janelado. 1ª tarefa: **acordao, 3 épocas** (fatia
pequena → épocas extras cabem). Pod 3090 **$0,186/h**, ETA total **~36h
(~$7-8)**, fim previsto quinta. Serving roteado por `doc_classe`
(llama.cpp troca de adapter é barata; já roteamos grammar por tarefa).
Baseline de comparação: o modelo multi-task v2.1 avaliado na MESMA fatia
do test.

**Critério de sucesso (pré-registrado).** O adapter ganha da fatia
multi-task em **≥2 das 3 classes fracas** por **≥3pp** cada (F1/acc da
classe no gate). 1 de 3 = interferência não era o problema (é o dado) →
mata o experimento e investe em gold.

**Status: 🟠 TREINADO, NÃO GATEADO até 02/09/2026 — gate EM EXECUÇÃO (R115).** Os 7
adapters estão no NAS (`llmsv2:/mnt/nas-data/voyager-train/out/adapter_esp_{acordao,
cessao,decisao,herdeiros,oficio,pagamento,partes_doc}`, 02/08). A GPU foi paga e o
experimento nunca foi respondido: **nunca rodou a comparação por-classe contra a
mesma fatia do test** — que é o experimento inteiro. Em 02/09 a 3090 do llmsv2 foi
liberada e o gate foi disparado com o critério abaixo, **pré-registrado e commitado
antes da primeira medição**.

### 2.1 Procedência do dado (auditada em 02/09/2026, ANTES de medir)

Medido na fonte, não na doc:

| achado | evidência |
|---|---|
| split é limpo por hash-CNJ nos 3 níveis | `train_esp_*` ∩ `dev_esp_*` = **0 CNJ** em todas as 7 tarefas; `dev_esp_*` ∩ `train_mix_v21` = **0**; `train_esp_*` ∩ `test_mix_v21` = **0** |
| ⚠️ **`dev_esp_*` NÃO é held-out honesto** | foi o `--dev_file` do treino (`train_summary.json`), com `load_best_model_at_end=True` + `metric_for_best_model=eval_loss` sobre 400 exemplos dele → **serviu de seleção de modelo**. Medir o especialista ali é Goodhart (o erro do DSPy/GEPA). **Descartado do gate.** |
| a fatia de treino do especialista ≈ a fatia que o v2.1 já viu | `train_esp_<t>` tem a MESMA contagem de `train_mix_v21` filtrado por tarefa em 6 de 7 (só `cessao` difere: 1.099 vs 1.352). Ou seja: o especialista **não tem mais dado**, tem menos concorrência — que é exatamente a hipótese da interferência |
| tudo é `source=v2` (por-documento) | os 7 conjuntos `esp` são 100% `v2`; nenhum exemplo `v1` por-processo |

**Consequência:** o gate mede em `data/test_mix_v21.jsonl` (`split=test`, o MESMO
held-out por hash-CNJ onde o v2.1 tirou macro 91,76), nunca em `dev_esp_*`.

### 2.2 Critério pré-registrado (R115, 02/09/2026 — commitado antes de rodar)

**Conjunto.** Para cada tarefa `t`: linhas de `test_mix_v21.jsonl` com `source=v2`,
`campo=t` e `doc_classe` ∈ classes vistas no `train_esp_t`. Ordem canônica
(`cnj`,`janela`) → embaralho `seed=20260902` → cap **N=800** (o que houver, se menos).
O slice é gravado UMA vez em `data/esp_gate/slice_<t>.jsonl` e **os dois modelos leem
o mesmo arquivo, na mesma ordem, com o mesmo batching** → pareamento exato por exemplo.

**Modelos.** A = `out/adapter_v21` (campeão, baseline) · B = `out/adapter_esp_<t>` ·
C = `Qwen2.5-7B-Instruct` **sem adapter** (só controle). Greedy (`do_sample=False`),
`max_new=512`, `maxseq=4096`, 4-bit nf4 — idêntico ao `eval_gate_v2.py`.

**Métrica.** Scorer = `score_v2()` do `eval_gate_v2.py` **sem alteração** (mesma
canonicalização de nome/valor/data/enum, mesmo F1 de entidade). Por exemplo:
`acerto_doc` = média das métricas pontuadas daquele exemplo ∈ [0,1]; gold vazio sai do
numerador e entra na conta de abstenção. **Primária da tarefa = média de `acerto_doc`.**
Secundárias sempre reportadas: métrica por campo, JSON inválido, falsa-abstenção.

**Significância (pareada, sem afrouxamento posterior).**
- **McNemar exato** (binomial bicaudal) sobre `doc_perfeito` (todos os campos do exemplo corretos);
- **bootstrap pareado** 10.000 reamostragens (`seed=20260902`) na diferença de médias → IC 95%.

**Decisão por especialista — PASS exige as TRÊS:**
1. Δ(B−A) na primária **≥ +3,0pp**;
2. limite inferior do IC 95% pareado **> 0**;
3. McNemar **p < 0,05** a favor de B — ou, se `doc_perfeito` for degenerado
   (discordantes b+c < 25), o item 2 basta **e o fato é declarado no veredito**.

Qualquer outro resultado é **BLOCK**. Explicitamente: **empate não promove** (Δ entre
−3 e +3pp = BLOCK) — adapter por classe custa roteamento, VRAM e um artefato a mais
por ciclo de dado; empate não paga esse custo. Δ ≤ −3pp com IC excluindo 0 = BLOCK
com autópsia "o especialista PIORA".

**Veredito da família (o experimento nº 4).** Mantido o critério original de 31/07 nas
MESMAS 3 classes: **família PASS ⟺ ≥2 de {acordao, cessao, herdeiros} passam**.
Especialista que passe fora dessas 3 é registrado como PASS individual e **não**
ressuscita a família.

**Regra de parada (economia de GPU, fixada antes):** roda `acordao` primeiro. Se o IC
95% da diferença **excluir +3pp** (isto é, dá para descartar o ganho exigido), o gate
PARA e reporta — 16h de GPU economizadas. Ordem se seguir: acordao → decisao → oficio →
pagamento → partes_doc → cessao → herdeiros.

**Controles — se qualquer um falhar, PARAR: a régua está torta, não o modelo.**
- **C1 · reprodução histórica.** O baseline A no slice de `acordao` tem de reproduzir o
  gate publicado do v2.1 (ACÓRDÃO 98-100, `LABLOG` 31/07) dentro de ±5pp. Se A vier
  muito abaixo, nenhum veredito desta rodada vale.
- **C2 · separação base×FT.** O modelo C (base sem adapter) tem de ficar **≥20pp abaixo**
  de A no mesmo slice. Se base ≈ FT, a métrica não está medindo fine-tune nenhum.

**Checkpoint.** Cada tarefa grava `out/esp_gate/preds_<tag>.jsonl` (incremental,
resume-safe) e `out/esp_gate/resultado_<t>.json` assim que fecha — sessão que cair não
leva o trabalho junto (lição da v2.2).

### 2.3 Resultados medidos (checkpoint incremental — 02/09/2026, 3090 do llmsv2)

> Gravados em `llmsv2:/mnt/nas-data/voyager-train/out/esp_gate/resultado_<tarefa>.json`
> assim que cada tarefa fecha. Harness commitado no git do harness (`32f8a32`).

**Vazão medida:** 13,5 min por modelo em 800 exemplos (bs=8, 4-bit, 3090) — **~1,7 s/exemplo**,
não os ~4,6 s/ex do gate de 31/07. A varredura dos 7 especialistas custa **~3h**, não 19h.

#### acordao — 🔴 BLOCK

| | v2.1 (baseline) | adapter_esp_acordao | Δ |
|---|--:|--:|--:|
| `acerto_doc` (n=707 pareados) | **98,40%** | 98,28% | **−0,12pp** |
| `doc_perfeito` | 97,45% | 96,89% | −0,56pp |

IC 95% bootstrap pareado (10k): **[−0,85, +0,64] pp** — exclui o +3pp exigido com folga.
McNemar exato: b=11 / c=7, **p=0,48** (b+c=18 → discordância degenerada, declarado).
Critério 1 (Δ≥3pp) ✗ · 2 (IC>0) ✗ · 3 (significância) ✗ → **BLOCK**, sem afrouxamento.

Por campo (n): data 100→100 (45) · desfecho 99,64→98,91 (549) · orgao_julgador
98,64→98,13 (589) · **relator 93,85→96,15 (130, +2,30pp)**. JSON inválido 1→1;
falsa-abstenção 0,71%→0,71%. O único campo onde o especialista sobe é `relator` — e
+2,3pp em n=130 não sustenta o critério nem sozinho.

**Controles — os dois passaram, a régua está de pé:**
- **C1 (reprodução histórica):** o v2.1 no slice reproduz o gate publicado de 31/07
  (ACÓRDÃO 98-100): desfecho 99,64 · orgao 98,64 · data 100 · relator 93,85 — dentro
  da tolerância ±5pp. A régua concorda com a régua antiga.
- **C2 (separação base×FT):** base Qwen2.5-7B **sem adapter** = 57,97% contra 96,86% do
  v2.1 no MESMO subconjunto (n=200) → **gap 38,89pp** (exigido ≥20). A métrica mede
  fine-tune de verdade; um empate A×B não é cegueira da régua.

**Leitura:** `acordao` deixou de ser classe fraca com o janelamento (v2.1 já faz 98,4%,
teto 100). A hipótese da interferência não tem onde ganhar aqui — e o adapter dedicado,
com 3 épocas só nessa fatia, **empata dentro de ±0,7pp**. Não é que o especialista seja
ruim: é que não sobra o que ganhar.

## 3. DPO-κ — preferências da fila de divergência

**Hipótese.** O resíduo dominante do extrator não é formato (grammar
resolve), é COMPORTAMENTO: chutar valor plausível sem evidência e (o
simétrico) abster com evidência visível. A fila κ (19.563 casos onde as
fontes de rótulo divergiram) + a revisão humana da amostra κ são exatamente
o dado de preferência que o SFT não expressa — DPO ensina "prefira abster a
chutar; confie na estrutura" sem re-rotular o corpus.

**Setup.** zordon `eval/build_dpo_pairs.py` →
`eval/dpo_pairs_v1.jsonl` (**2.688 pares**; jsonl fora do git, script
versionado). Pares (prompt = input EXATO do contrato, reconstruído pela
mesma `_janelas_do_doc` do gerador; chosen/rejected serializados como o
SFT):

- **(a) fonte forte vs fraca — 1.289**: vara estrutural do cabeçalho vs
  vara plausível trocada (decisao 1.163), papéis cedente/cessionário
  trocados na escritura (cessao 116), espólio-estrutural vs texto-livre
  (herdeiros 10).
- **(b) erros confirmados pela revisão humana — 27**: chosen = resposta
  corrigida (correções parseadas da `observacao` do revisor e VERIFICADAS
  no texto; sem fonte → não emite), rejected = o que o modelo respondeu.
- **(c) abstenção — 1.372**: campo comprovadamente SEM evidência (guards
  de ausência: sem moeda → valor, sem menção a juiz → juiz, sem
  referente/em-favor → beneficiário) ou abstenção certificada pelo
  revisor; chosen = null, rejected = valor plausível amostrado de OUTROS
  docs (train split; sempre ausente do texto).

Por tarefa: decisao 1.200, oficio 1.200, pagamento 159, cessao 116,
herdeiros 10, partes_doc 7 (cap 1.200/tarefa; b e c-humano nunca dropados).
Split por hash-CNJ preservado (train 1.069 / dev 673 / test 946). Treino:
DPO (TRL) LoRA sobre o SFT vencedor, β≈0,1, 1 época, só split train.
O dataset cresce com as marcações humanas (revisor validando 60 agora;
re-rodar o builder é idempotente).

**Critério de sucesso (pré-registrado).** No test do gate v2 + protocolo de
abstenção: **falsa-afirmação cai ≥50%** (campo preenchido sem evidência)
**sem perder cobertura >2pp** (campos corretamente preenchidos que virem
null). Retenção: macro v1 ≥85 se mantém.

**Status.** Dataset v1 pronto (yield acima; auditoria de skips em
`eval/relatorio_dpo_pairs.md`). Dispara quando os gates (v2.1 / A/B)
definirem o modelo base do DPO.

## 4. Destilação 3B — aluno barato para as classes fortes

**Hipótese.** Nas classes fortes (ALVARA/PAGAMENTO 99-100, DECISAO 99,
DESPACHO 94-97, PROCURACAO 72+, OFICIO 84-92) o teto é o DADO, não a
capacidade do 7B — um aluno Qwen2.5-3B-Instruct treinado nos rótulos do
professor roda com ~metade da VRAM/latência (pods menores, throughput 2×
no backlog de extração recorrente) sem perda material.

**Setup.** zordon `eval/build_distill_labels.py`: varre N CNJs do corpus
(SEMPRE fora do split de TEST — é o held-out do gate professor×aluno),
monta o input exato do contrato v2.1 nas classes fortes e extrai com o
professor num llama.cpp (`--server`, grammar GBNF por tarefa, temp 0);
saída no MESMO formato do SFT (`rotulo_fonte=["professor_<tag>"]`).
`--resume` idempotente (timer/pod), `--dry-run` mock testado ponta a ponta
(72 rótulos de 15 CNJs, test split pulado, resume ok). Treino do aluno:
Qwen2.5-3B-Instruct, MESMA receita do 7B (QLoRA r=32, maxseq 4096), no pod.

**Critério de sucesso (pré-registrado).** O aluno retém **≥97% do score do
professor** em CADA classe forte no mesmo test held-out (ex.: professor 99
→ aluno ≥96). Classe que cair abaixo fica FORA do rollout do 3B (uso
por-classe, igual à política do v2).

**Status.** Harness pronto e testado (dry-run). Rotulagem dispara assim que
o professor vencedor (v2.1 vs A/B Qwen3-8B) for definido — rotular antes
seria destilar um professor que pode morrer no gate.

---

## Onde vive cada coisa

| o quê | onde |
|---|---|
| Pares DPO (builder + relatório) | zordon `eval/build_dpo_pairs.py` + `eval/relatorio_dpo_pairs.md`; dados `eval/dpo_pairs_v1.jsonl` (fora do git) |
| Harness de destilação | zordon `eval/build_distill_labels.py`; grammars `eval/grammars_v2_tmp/grammars/v2/` |
| Corpus DAPT | zordon `scripts/gerar_dapt_corpus.py` → `~/fichaparte_tmp/dapt_corpus.txt` |
| Treino/gates | llmsv2 `/mnt/nas-data/voyager-train/` (`train_peft_v2.py`, `mix_datasets.py`, `eval_gate_v2.py`) |
| Registry/model cards | `.ia/MODELOS.md` — atualizar na promoção/morte de cada experimento |
