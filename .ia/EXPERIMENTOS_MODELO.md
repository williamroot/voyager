# Experimentos de modelo — extrator de precatórios (ciclo pós-gate v2)

> 4 experimentos em curso sobre o extrator (`.ia/MODELOS.md`). Cada um tem
> **critério de sucesso pré-registrado** — decidido ANTES de rodar, pra não
> repetir o Goodhart do DSPy/GEPA (`.ia/RAG_EVAL_OBS.md`). Nenhum resultado
> promove modelo sem passar no seu critério + κ humano (§12.0: nunca direto
> em prod).
>
> **Dependência comum:** os experimentos 3 e 4 treinam SOBRE o "modelo
> vencedor" — o que sair dos gates em andamento (v2.1 re-treino janelado
> vs A/B de base Qwen3-8B). Os experimentos 1 e 2 são caminhos alternativos
> caso v2.1/A/B não resolvam as classes fracas.

| # | experimento | hipótese em 1 linha | critério pré-registrado | status |
|---|---|---|---|---|
| 1 | DAPT | pré-treino no dialeto do acervo melhora o SFT | SFT-sobre-DAPT ≥ **+2pp macro** vs SFT direto | 🔵 corpus pronto (gerador), treino aguarda GPU |
| 2 | Especialistas | adapter por classe fraca vence o multi-task | ganha em **≥2 das 3** classes fracas por **≥3pp** | ⚪ aguarda dataset v2.1 + GPU |
| 3 | DPO-κ | preferências da fila κ matam o chute | falsa-afirmação **−50%** sem perder cobertura **>2pp** | 🟡 dataset PRONTO (2.688 pares), aguarda vencedor |
| 4 | Destilação 3B | classes fortes cabem num aluno 3B | aluno retém **≥97%** do professor nas fortes | 🟡 harness pronto (dry-run ok), aguarda vencedor |

---

## 1. DAPT — Domain-Adaptive PreTraining

**Hipótese.** O texto dos autos (OCR ruidoso, tabelas DEPRE, fórmulas
cartoriais) é um dialeto que a base Qwen viu pouco; continuar o pré-treino
(next-token, sem rótulo) nesse dialeto reduz a perplexidade de base e faz o
MESMO SFT render mais — especialmente nas classes com pouco gold (cessão,
herdeiros), onde o prior importa mais que o dado supervisionado.

**Setup.** Corpus ~280M tokens (~1,2GB) amostrado do acervo, estratificado
por `doc_classe` × tribunal, documentos INTEIROS reconstruídos dos chunks
(de-overlap), separador `<|endofdoc|>`: zordon `scripts/gerar_dapt_corpus.py`
→ `~/fichaparte_tmp/dapt_corpus.txt` + stats tsv. Treino: LoRA causal-LM
sobre Qwen2.5-7B-Instruct, packing 4096, 1 época (~budget de horas de 3090,
não de épocas); depois o SFT v2.1 idêntico (mesmo mix, seed, maxseq) sobre o
checkpoint DAPT.

**Critério de sucesso (pré-registrado).** SFT-sobre-DAPT ≥ **+2pp macro**
vs SFT direto no MESMO test held-out por hash-CNJ (gate `eval_gate_v2.py`).
Abaixo disso o custo (horas de GPU a cada ciclo de dado) não se paga.

**Status.** Corpus: gerador pronto no zordon (roda bounded no DB sensível).
Treino: aguarda janela de GPU (3090 llmsv2 compartilhada) após o gate v2.1.

## 2. Especialistas — adapter por classe fraca

**Hipótese.** ACORDAO/SENTENCA (docs longos), CESSAO e herdeiros perdem no
multi-task por interferência: são <5% do mix e têm formato/registro muito
diferente das classes dominantes. Um adapter LoRA POR CLASSE, treinado só na
fatia dela (com o dado v2.1 janelado + gold reforçado), aprende o template
sem competir por capacidade.

**Setup.** 3 adapters (acordao, cessao, herdeiros) sobre a mesma base e
receita do v2 (QLoRA r=32), treinados na fatia da classe; serving roteado
por `doc_classe` (llama.cpp troca de adapter é barata; já roteamos grammar
por tarefa). Baseline de comparação: o modelo multi-task v2.1 avaliado na
MESMA fatia do test.

**Critério de sucesso (pré-registrado).** O adapter ganha da fatia
multi-task em **≥2 das 3 classes fracas** por **≥3pp** cada (F1/acc da
classe no gate). 1 de 3 = interferência não era o problema (é o dado) →
mata o experimento e investe em gold.

**Status.** Aguarda o dataset v2.1 final (janelas p/ docs longos já no
gerador) e GPU. Não pré-julgar: o gate v2 apontou que a causa é DADO —
este experimento só decide DEPOIS do re-treino v2.1.

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
