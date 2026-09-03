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
| 2 | Especialistas | adapter por classe fraca vence o multi-task | ganha em **≥2 das 3** classes fracas por **≥3pp** (§2.2) | 🔴 **BLOCK (02/09)** — 0 de 3; Δ entre −0,52 e +0,02pp, IC exclui o ganho nas 7. Morto, ver §2.3 |
| 3 | DPO-κ | preferências da fila κ matam o chute | falsa-afirmação **−50%** sem perder cobertura **>2pp** | 🟡 dataset PRONTO (2.688 pares); nunca treinou |
| 4 | Destilação 3B | classes fortes cabem num aluno 3B | aluno retém **≥97%** do professor nas fortes | 🟡 harness pronto (dry-run ok); nunca rotulou |
| 5 | **Rótulo v2.2 em `herdeiros`** | o teto de `herdeiros` é o RÓTULO, não o modelo | bater v2.1 **e** a base crua por ≥3pp, sem regredir `falecido`, com guarda anti-alucinação (§5.2) | 🔴 **BLOCK (03/09)** — 3 de 4 critérios passam com folga, a **guarda de emissão indevida reprova** (12,86% vs teto 9,07%). Hipótese CONFIRMADA (+17,88pp só do rótulo), adapter **não promovido**. Ver §5.3 |

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

**Status: 🔴 BLOCK (02/09/2026, R115) — experimento RESPONDIDO e encerrado.** Os 7
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

### 2.3 RESULTADO — 🔴 **BLOCK nos 7**, família BLOCK (02/09/2026, 3090 do llmsv2)

> Artefatos: `llmsv2:/mnt/nas-data/voyager-train/out/esp_gate/resultado_<tarefa>.json`
> (+ `preds_*.jsonl` das 17 execuções, para re-pontuar sem GPU).
> Harness no git do harness: `32f8a32`. Critério commitado ANTES em `c714f8f`.

**Custo real:** 1,7 s/exemplo (13,5 min por modelo em 800), não os ~4,6 s/ex do gate de
31/07 — a varredura dos 7 saiu em **~3h20 de GPU**, não 19h.

#### Tabela do gate — nenhum especialista bate o v2.1 no domínio dele

| tarefa | n pareado | v2.1 | especialista | Δ (pp) | IC 95% pareado (pp) | McNemar (b/c, p) | veredito |
|---|--:|--:|--:|--:|---|---|---|
| **acordao** | 707 | 98,40% | 98,28% | **−0,12** | [−0,85, +0,64] | 11/7, p=0,481 ⚠️ | 🔴 BLOCK |
| **cessao** | 800 | 98,31% | 98,33% | **+0,02** | [−0,62, +0,69] | 13/11, p=0,839 ⚠️ | 🔴 BLOCK |
| **herdeiros** | 478 | 83,71% | 83,20% | **−0,52** | [−2,22, +1,07] | 12/15, p=0,701 | 🔴 BLOCK |
| decisao | 737 | 99,54% | 99,43% | −0,10 | [−0,32, +0,11] | 4/1, p=0,375 ⚠️ | 🔴 BLOCK |
| oficio | 797 | 99,07% | 98,89% | −0,18 | [−0,45, +0,07] | 7/3, p=0,344 ⚠️ | 🔴 BLOCK |
| pagamento | 702 | 98,22% | 98,10% | −0,12 | [−0,69, +0,50] | 9/6, p=0,607 ⚠️ | 🔴 BLOCK |
| partes_doc | 709 | 78,05% | 77,68% | −0,37 | [−1,77, +1,10] | 57/59, p=0,926 | 🔴 BLOCK |

⚠️ = discordância degenerada (b+c<25): o McNemar não tem poder ali e o IC pareado é
quem decide — como estava pré-registrado. **Em NENHUMA tarefa o IC inclui o +3pp
exigido**; em 6 das 7 o IC nem sequer inclui um ganho de +1,1pp. Não é "não deu para
provar o ganho": é **ganho descartado com precisão de décimos de ponto**.

**Família (critério de 31/07: ≥2 de {acordao, cessao, herdeiros} por ≥3pp): 0 de 3 →
🔴 BLOCK.** Nenhum PASS individual fora delas.

#### Controles

**C1 (reprodução histórica) ✅** — o v2.1 no slice de `acordao` reproduz o gate publicado
de 31/07 (ACÓRDÃO 98-100): desfecho 99,64 · orgao_julgador 98,64 · data 100 · relator
93,85, dentro da tolerância ±5pp. A régua nova concorda com a régua antiga.

**C2 (base sem adapter ≥20pp abaixo do v2.1, mesmo subconjunto) — 5 de 7 ✅**

| tarefa | n | base | v2.1 | gap (pp) | passa |
|---|--:|--:|--:|--:|---|
| oficio | 200 | 48,98% | 98,22% | +49,24 | ✅ |
| pagamento | 200 | 56,22% | 99,00% | +42,78 | ✅ |
| acordao | 200 | 57,97% | 96,86% | +38,89 | ✅ |
| partes_doc | 200 | 50,08% | 83,58% | +33,50 | ✅ |
| herdeiros | **539** | 58,58% | 83,71% | +25,13 | ✅ |
| cessao | 800 | 82,96% | 98,31% | +15,35 | ❌ |
| decisao | 200 | 87,67% | 99,49% | +11,83 | ❌ |

`cessao` e `decisao` **não limpam a barra**, e a barra **não foi afrouxada depois do
resultado**: os vereditos dessas duas ficam registrados **com a ressalva** de que ali o
controle não provou separação suficiente. A causa é estrutural e vale anotar para o
próximo gate: são tarefas de **teto** (v2.1 em 98,3% e 99,5%) onde a base crua já faz
83% e 88% — 20pp absolutos é uma barra impossível quando só sobram 12-17pp de espaço.
**Barra de controle tem de ser em redução de erro, não em pontos absolutos** — mas
trocar isso agora seria mover a trave, então fica como correção pré-registrada do
próximo ciclo.

#### O achado que pagou as 3h: em `herdeiros`, **a base crua bate o campeão**

Único campo do gate inteiro com movimento grande, e ele aponta para o **dado**, não para
o adapter:

| `herdeiros.f1_entidade` (n=130 pareados) | valor | vs v2.1 |
|---|--:|---|
| v2.1 (campeão em produção) | **27,92%** | — |
| adapter_esp_herdeiros | **36,44%** | +8,52pp · IC [+2,85, +14,45] · McNemar p=0,049 |
| **Qwen2.5-7B base, SEM fine-tune nenhum** | **40,01%** | **+12,09pp · IC [+3,42, +20,82]** |

O IC do base×v2.1 **exclui o zero**: a base sem treino nenhum extrai herdeiros melhor
que o modelo que está em produção. E o especialista (36,44%) **não alcança a base**
(+3,57pp a favor da base, IC [−4,74, +11,78] — aí sim inconclusivo).

Isso confirma com número o diagnóstico escrito em 31/07 (`LABLOG`): **73-76% do gold de
herdeiros está vazio**, então o SFT ensinou o modelo a **não emitir herdeiros**. O
+8,52pp do especialista não é capacidade nova — é **recuperação parcial de um dano que
o próprio SFT causou**, e ainda assim fica abaixo de não ter treinado. O especialista
também **piora `falecido`**: 91,84% → 89,96% (−1,88pp, IC [−3,77, −0,21], exclui o zero).

⚠️ **Este achado só ficou de pé porque o controle foi ampliado.** Na primeira medição, com
o controle em 200 exemplos (n=30 no campo), o gap base×v2.1 parecia **+18,4pp** e o
`falecido` da base parecia 82,98% — os 200 primeiros do slice são os prompts mais curtos
(o slice é ordenado por tamanho para o batching), uma amostra enviesada para fácil. Com
os 539, o `falecido` da base caiu para 61,92% e o gap do campo desceu para +12,09pp. O
sinal sobreviveu, **menor**. Amostra pequena mentiu de novo — a 6ª vez registrada nesta casa.

#### Procedência: por que o N pequeno do `dev_esp` NÃO contaminou isto

O gate **não usou** `data/esp/dev_esp_*` (onde `herdeiros` tem só 336 exemplos e que foi
o `--dev_file` do próprio treino, com `load_best_model_at_end`). Mediu no
`test_mix_v21.jsonl` (`split=test`), o MESMO held-out do macro 91,76 — e o corte de
`herdeiros` ali tem **539 linhas / 478 pontuáveis / 130 com o campo `herdeiros`**, com
zero CNJ em comum com o treino de qualquer um dos dois modelos.

#### Autópsia — por que a hipótese da interferência morreu

1. **Não sobrou espaço.** Em 6 das 7 tarefas o v2.1 já está em **98-99,5%**. O
   janelamento da v2.1 (31/07) resolveu o que o experimento de 02/08 foi desenhado para
   resolver: ACORDAO e CESSAO deixaram de ser classes fracas ANTES dos adapters
   existirem. O experimento nasceu respondendo a uma pergunta que o dado já tinha fechado.
2. **O especialista não tem mais dado, tem menos concorrência** — e menos concorrência
   não vale nada: a fatia de treino dele é a MESMA que o v2.1 já viu (contagem idêntica
   em 6 de 7 tarefas). Se a interferência multi-task fosse o gargalo, apareceria aqui.
   Não apareceu em nenhuma das sete.
3. **Onde há espaço, o gargalo é o rótulo.** As duas únicas tarefas longe do teto são
   `partes_doc` (78%) e `herdeiros` (83,7% composto / 27,9% no campo) — e em `herdeiros`
   está provado que o problema é o gold vazio, porque **não treinar vai melhor**.
   Adapter nenhum conserta rótulo.

**Consequência prática:** os 7 `adapter_esp_*` **não vão para produção** e o roteamento
por `doc_classe` com troca de adapter **não precisa ser construído** — economia de
complexidade de serving que este BLOCK compra. O v2.1 segue campeão, sozinho.

**O que este gate NÃO mediu** (dito, não estimado): (a) os 7 adapters com **grammar GBNF**
— o gate roda geração livre, como o gate v2.1, então `partes_doc` carrega 4,6% de JSON
inválido nos dois modelos e a grammar mexeria nisso igualmente para ambos; (b)
`herdeiros` sob o **gold v2.2 re-rotulado** (`data/*_v22.jsonl`), que existe e nunca virou
modelo — é justamente a hipótese "o problema é o rótulo" que este gate acabou de reforçar.

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

## 5. Retreino de `herdeiros` com o gold v2.2 — ablação de RÓTULO (R120, 02/09/2026)

**Hipótese.** O teto de `herdeiros` é o **rótulo**, não o modelo. Se for verdade,
o MESMO treino, nas MESMAS linhas, com a MESMA receita, trocando só o rótulo
(v2.1 → v2.2 re-rotulado pelo professor DeepSeek) tem de mover o ponteiro muito
mais que os 7 adapters especialistas moveram (§2.3: −0,52 a +0,02pp).

### 5.0 Medição do gold v2.2 — feita ANTES de treinar (a ordem importa)

Retreinar num gold ainda vazio seria gastar GPU para reproduzir o dano. Medido na
fonte (`llmsv2:/mnt/nas-data/voyager-train/data/`):

| | train_mix v2.1 | train_mix **v2.2** | test_mix v2.1 | test_mix **v2.2** |
|---|--:|--:|--:|--:|
| linhas `campo=herdeiros` | 625 | 625 | 539 | 539 |
| **gold VAZIO** | **73,44%** | **50,08%** | **75,88%** | **51,95%** |
| nomes de herdeiro no gold | 384 | **894** | 270 | **809** |

Os dois arquivos são **alinhados linha-a-linha** (mesmo prompt, mesma ordem): a
troca é de rótulo puro. No train, 366 linhas mudaram — **168 vazio→preenchido** e
**30 preenchido→vazio** (o relabel também TIRA rótulo errado, não só acrescenta).

**Procedência do rótulo novo (o que o torna aceitável):** 96,4% dos nomes de
herdeiro do gold v2.2 aparecem **literalmente** no texto da própria janela
(+1,6-3,0% por todos os tokens); só **2,0%** (train) e **0,6%** (test) não têm
suporte textual. No v2.1 esse número era 100% — o v2.1 nunca chutou, ele **calava**.
Sobrou **1** desvio de schema (`falecido` como lista) em cada arquivo.

**Os 50% que continuam vazios não são o mesmo dano.** Por classe no v2.2:
`CERTIDAO_OBITO` 32,4% vazio, `HABILITACAO_HERDEIROS` 59,8%. E o gold é
`{nome, papeis}`: documento cujo único nome é **inventariante** (331 no relabel)
sai como `herdeiros: []` **por decisão**, não por omissão. Abstenção declarada ≠
abstenção por incapacidade de decidir.

### 5.1 Auditoria da RÉGUA (feita antes do pré-registro, com os modelos que já existiam)

Ao reproduzir o achado do R115 encontrei um buraco no `score_v2`:

```python
if not gv:      # gold da lista vazio
    continue    # <- a lista NAO e' pontuada
```

Com gold vazio, `herdeiros.f1_entidade` **não é calculado** — logo **emitir
herdeiro onde o gold não tem nenhum é de graça**. A métrica do achado de 02/09 é
condicionada aos 130 exemplos de gold não-vazio: ela mede recall e **nunca cobra
falso positivo**. Um modelo tagarela sobe nela sem melhorar.

Régua nova (`herd_score.py`, sem tocar no `score_v2`): **F1 por exemplo definido
em TODOS os 539** — vazio×vazio = 1,0; vazio×cheio = **0,0** (emissão indevida
custa); cheio×vazio = 0,0 — mais micro-P/R/F1 no slice inteiro, taxa de emissão
indevida, e **grounding** (o nome emitido está no texto?), que é livre de régua.

**C1 · reprodução ✅** — a régua nova reproduz o R115 **exatamente** na métrica
dele: v2.1 27,92% · esp 36,44% · base 40,01%, IC base×v2.1 [+3,42, +20,82].

**O que ela mostra — e por que as duas réguas de gold discordam em SINAL:**

| régua do gold | v2.1 (F1/ex) | base crua (F1/ex) | Δ base−v2.1 | IC 95% |
|---|--:|--:|--:|---|
| **v2.1** (75,9% vazio) | 77,42% | 68,46% | **−8,96pp** | [−12,95, −5,01] |
| **v2.2** (52,5% vazio) | 58,14% | **72,93%** | **+14,79pp** | [+11,21, +18,52] |

Sob o gold velho a base parece pior porque emite; sob o gold novo ela é melhor
porque **o que ela emitia era certo**: dos **383 falsos positivos** da base sob o
gold v2.1, **263 viram verdadeiro positivo** sob o v2.2, e o **grounding da base é
98,45%**. Não é tagarelice — é o gold velho que estava calado. Isso **fortalece** o
achado do R115: com a régua consertada o gap base×campeão sobe de +12,09 para
**+30,37pp** na métrica dele, e continua **+14,79pp** na métrica que cobra emissão.

Custo do silêncio do campeão, medido: **recall de 18,75%** (147 de 784 nomes).

### 5.2 Critério pré-registrado (commitado ANTES de treinar E e D)

**Conjunto.** O MESMO `data/esp_gate/slice_herdeiros.jsonl` do R115 (539 linhas,
`split=test`, zero CNJ em comum com qualquer treino). Gold **v2.2** = régua
primária (`test_mix_v22`, alinhado linha-a-linha; **6 linhas com gold ambíguo saem
da análise** → n=533). Tudo é reportado **também** sob a régua v2.1.

**Modelos.** A = `adapter_v21` (campeão) · B = **Qwen2.5-7B base, sem adapter** ·
C = `adapter_esp_herdeiros` (gold v2.1, protocolo 02/08) · **D = `adapter_herd_v21`**
(novo) · **E = `adapter_herd_v22`** (novo). D e E são treinados nas **mesmas 497
linhas, na mesma ordem, com a mesma receita** (QLoRA r=32/α=32, 4 épocas, lr 2e-4
cosine, bs 1×accum 16, maxseq 4096, seed 42) — divergindo **só no rótulo**.
Geração greedy, 4-bit nf4, `max_new=512`, idêntica ao gate.

**Dev.** `data/esp/dev_esp_herdeiros.jsonl` **não pode ser usado**: não tem relabel
v2.2, e o `load_best_model_at_end` selecionaria pelo rótulo que estamos consertando.
O dev de D e E sai do **próprio train**, por hash-CNJ (85 de 459 CNJs, tag
`R120-20260902`), disjunto, cada um com o seu rótulo. **0 CNJ** do train ou do dev
aparece no slice do gate.

**Decisão — PASS de E exige as QUATRO:**
1. Δ(E − A) ≥ **+3,0pp** na primária, IC 95% pareado com limite inferior > 0 — bate o campeão;
2. Δ(E − B) ≥ **+3,0pp** na primária, IC 95% pareado com limite inferior > 0 — **bate a base crua**. Sem isto, treinar é pagar por algo pior que não treinar;
3. **não-regressão** de `falecido`: acc(E) ≥ acc(A) − 2,0pp;
4. **guarda anti-alucinação** (livre de régua): grounding(E) ≥ grounding(B) − 2,0pp **e** emissão indevida(E) ≤ emissão indevida(B) + 3,0pp.

Qualquer outra combinação é **BLOCK**. Empate não promove.

**Ablação de rótulo (o achado, não o gate).** Δ(E − D) na primária com IC 95%
pareado: mesmo código, mesmas linhas, mesma receita, **só o rótulo muda**. Reportada
sempre — inclusive se o gate der BLOCK.

**Consistência de régua (trava anti-Goodhart).** Se E ganhar de A e B nas DUAS
réguas, o achado é robusto. Se ganhar só sob a v2.2, o veredito é registrado como
**dependente de régua** — declarado, não afrouxado.

**Limitação declarada antes de medir.** O gold v2.2 é relabel de um **professor**
(DeepSeek), com spot-check humano κ 9/10 nos recuperados (31/07), não gold humano
exaustivo; E treina em rótulo do MESMO professor que fez a régua. A defesa é (a) a
base crua **nunca viu o professor** e já pontua 72,93% nessa régua — ela não é um
dialeto privado; (b) o grounding, que não depende de gold nenhum.

**Regra de parada.** Se E não bater B, **PARAR** — sem variações de hiperparâmetro
até passar. E se E passar, o próximo custo (retreino multi-task v2.2 completo,
~28h de 3090) só é gasto **depois** deste veredito, nunca antes.


### 5.3 RESULTADO — 🔴 **BLOCK**, e a hipótese **CONFIRMADA** (03/09/2026, 3090 do llmsv2)

> Artefatos: `out/adapter_herd_v22` (md5 `28ddcbff2836b7f89558f6ac7f1f8ded`) e
> `out/adapter_herd_v21`; preds `out/esp_gate/preds_herd_v2{1,2}.jsonl`; resultados
> `out/esp_gate/r120_final_v2{1,2}.json`. Harness no git do harness: `2510121`.
> Critério commitado ANTES em `2b68c55`. **Custo: 2h26 de 3090** (2 treinos de 63 min
> + 2 gerações de ~9 min); a GPU voltou a ficar livre.

#### A ablação de rótulo — o achado

Dois adapters, **mesmas 497 linhas na mesma ordem, mesma receita, mesmo dev, mesma
seed**. Só o rótulo muda:

| régua v2.2 (primária) | F1/ex | micro-F1 | R115 `f1_entidade` | `falecido` |
|---|--:|--:|--:|--:|
| `adapter_herd_v21` (rótulo velho) | 61,64% | 44,36% | 31,05% | 85,87% |
| **`adapter_herd_v22`** (rótulo novo) | **79,53%** | **73,03%** | **71,10%** | **92,17%** |
| **Δ só do RÓTULO** | **+17,88pp** | +28,67 | +40,05 | +6,30 |

**IC 95% pareado [+13,72, +21,97]pp · McNemar b=27/c=111, p=2,7×10⁻¹³.**

Compare com o que a **arquitetura** rendeu no mesmo campo: os 7 adapters
especialistas moveram **−0,52 a +0,02pp** (§2.3). **Trocar o rótulo vale ~18pp;
trocar o modelo valeu zero.** Não é mais hipótese.

**Controle de protocolo ✅** — o `adapter_herd_v21` (meu protocolo) reproduz o
`adapter_esp_herdeiros` (protocolo de 02/08): 61,64% vs 60,68%. O ganho não vem do
jeito de treinar.

#### O gate — 3 de 4 critérios passam, e o quarto reprova

| régua v2.2 | v2.1 (A) | base crua (B) | esp (C) | herd_v21 (D) | **herd_v22 (E)** |
|---|--:|--:|--:|--:|--:|
| **F1/ex (primária)** | 58,14% | 72,93% | 60,68% | 61,64% | **79,53%** |
| micro-P / micro-R | 74,2 / **18,8** | 76,8 / 50,6 | 79,5 / 25,6 | 74,3 / 31,6 | 75,4 / **70,8** |
| `f1_entidade` (R115) | 19,33% | 49,70% | 26,26% | 31,05% | **71,10%** |
| `falecido` | 85,22% | 70,87% | 85,22% | 85,87% | **92,17%** |
| grounding | 96,46% | 98,45% | 96,05% | 94,01% | 97,96% |
| **emissão indevida** | 6,79% | **6,07%** | 8,21% | 10,71% | **12,86%** |

| critério pré-registrado | medido | veredito |
|---|---|---|
| 1 · Δ(E−A) ≥ +3pp, IC>0 | **+21,39pp**, IC [+16,87, +25,80], p=2,2×10⁻¹³ | ✅ |
| 2 · Δ(E−B) ≥ +3pp, IC>0 — **bate a base crua** | **+6,59pp**, IC [+2,86, +10,27], p=0,0081 | ✅ |
| 3 · `falecido` ≥ A − 2pp | 92,17% vs teto de 83,22% — **melhora** +6,95pp | ✅ |
| 4a · grounding ≥ B − 2pp | 97,96% vs teto 96,45% | ✅ |
| **4b · emissão indevida ≤ B + 3pp** | **12,86% vs teto 9,07%** | 🔴 **REPROVA** |

**Veredito: 🔴 BLOCK.** O critério exigia as quatro e **não foi afrouxado depois de
ver o número** — é a regra que o R115 seguiu quando o C2 falhou em duas tarefas.
`adapter_herd_v22` **não vai para produção**.

#### Autópsia — por que ele emite demais (população INTEIRA, não amostra)

Adjudiquei **os 36 casos** em que o gold v2.2 diz `herdeiros: []` e o E emitiu
(não uma amostra — todos):

| o que é | casos | exemplo |
|---|--:|---|
| **erro real de PAPEL** | ~27 (75%) | co-exequente de ação coletiva (`_i=395`: 13 nomes todos "(EXEQUENTE)"), **advogado com OAB** (`_i=349`: "REPRESENTANTES POLO ATIVO … DF21163"), inventariante (`_i=452`), o **polo passivo** (`_i=532`: "Requerido: Aldo Luis Pessagno"), e um vazamento do próprio falecido (`_i=117`) |
| gold ainda calado (modelo certo) | 3 (8%) | `_i=239` "**HERDEIROS DE CYRO RICARDO NUNES:** O 'DE CUJUS' DEIXA TRÊS FILHOS" + os 3 nomes; `_i=262` "os herdeiros LUIZ CARLOS… e MARCIA…"; `_i=175` "Requerente e **Herdeiro**: Sueli…" |
| ambíguo no próprio documento | 6 (17%) | `_i=229`, onde o texto lista os mesmos nomes como falecidos e como "Exequente e Herdeiro" |

⚠️ **A amostra pequena mentiu de novo — 7ª vez nesta casa.** Nos 8 primeiros casos
(seed fixa) a leitura dava **3/8 = 38% de gold calado**, o que quase virou "a guarda
reprovou por causa do gold". Nos **36**, são **3/36 = 8%**. A reprovação é real.

#### A causa, e ela é de novo o dado — mas agora com nome e endereço

O gold v2.2 extrai **`{nome, papeis:[herdeiro|inventariante|conjuge|sucessor]}`** — foi
exatamente a correção de 31/07, para não repetir o erro de excluir a viúva-inventariante.
**Mas o alvo de treino no mix é uma lista chapada de nomes** (`herdeiros: ["A","B"]` —
medido: 894 de 894 são `str`, nenhum `dict`). Os papéis **são jogados fora ao montar o
mix**. O modelo então recebe "às vezes emite, às vezes não" **sem a razão junto**, e
aprende o atalho disponível: *nome perto de um espólio = herdeiro*. Os 27 erros são
todos desse formato.

**Próximo lever (não precisa de GPU para preparar):** levar o papel para dentro do alvo
(`herdeiros: [{nome, papel}]`, com o enum fechado e a regra de abstenção explícita) e
re-rodar **esta mesma ablação de 2h26**. É mudança de `mix_datasets`, não de modelo.

#### Consistência de régua — declarada, não afrouxada

Sob a régua do gold **v2.1** o sinal inverte na primária: E fica **−18,76pp** abaixo do
v2.1 e **−9,80pp** abaixo da base — mas **+22,21pp** e **+10,12pp** acima deles na
métrica do R115. O veredito é **dependente de régua**, como o pré-registro previa. As
duas réguas discordam porque a v2.1 cobra por emitir o que ela mesma não rotulou; a
v2.2 é a adotada pelo motivo medido em §5.1 (263 dos 383 "falsos positivos" da base
viram verdadeiros sob ela, com grounding de 98,45%).

#### O que este ciclo NÃO mediu (dito, não estimado)

- **O v2.2 multi-task completo.** Só a fatia `herdeiros` foi retreinada. As ~28h de
  3090 do retreino do mix inteiro **não foram gastas** — a regra de parada dizia que
  esse custo só vem depois de um veredito, e o veredito é BLOCK.
- **`partes_doc` sob rótulo consertado.** É o outro campo de teto (78%) e ninguém
  re-rotulou. A ablação aqui só autoriza a **suspeita** de que lá também é o rótulo.
- **Grammar GBNF.** Geração livre nos cinco modelos, como no gate v2.1.
- **κ humano no gold v2.2 inteiro.** O que existe é o spot-check de 31/07 (9/10 nos
  recuperados) mais os 36 casos adjudicados aqui — não uma auditoria exaustiva.


---

## Onde vive cada coisa

| o quê | onde |
|---|---|
| Pares DPO (builder + relatório) | zordon `eval/build_dpo_pairs.py` + `eval/relatorio_dpo_pairs.md`; dados `eval/dpo_pairs_v1.jsonl` (fora do git) |
| Harness de destilação | zordon `eval/build_distill_labels.py`; grammars `eval/grammars_v2_tmp/grammars/v2/` |
| Corpus DAPT | zordon `scripts/gerar_dapt_corpus.py` → `~/fichaparte_tmp/dapt_corpus.txt` |
| Treino/gates | llmsv2 `/mnt/nas-data/voyager-train/` (`train_peft_v2.py`, `mix_datasets.py`, `eval_gate_v2.py`) |
| Registry/model cards | `.ia/MODELOS.md` — atualizar na promoção/morte de cada experimento |
