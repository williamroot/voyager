# LABLOG — diário de bordo dos experimentos de ML

> Cronológico, **avanços E regressos**. Regra da casa: documentar rico —
> incidente sem lição registrada é incidente que repete. Estado atual dos
> experimentos: `.ia/EXPERIMENTOS_MODELO.md`; registry: `.ia/MODELOS.md`.

Formato: cada entrada = o que aconteceu + (quando for regresso) a **lição**.

---

## 28/07 — treino v1

- ✅ **Treino v1** (Qwen2.5-7B QLoRA, 1× 3090): 10,4h, gate TEST **macro 87,9**
  (+9,9 vs base). Card completo em `.ia/MODELOS.md`.
- 💥 **OOM na eval**: batch de avaliação 2 estourou VRAM → bs **2→1**.
- ⚰️ **Unsloth descartado**: conflito de versões com o stack
  transformers 4.46 + peft 0.19 + trl 0.12. Ficou o stack vanilla.

## 28-29/07 — v2 "Ficha da Parte"

- ✅ **Dataset v2**: 196.360 exemplos por-documento (gerador zordon).
- ✅ **Treino v2**: 13,3h na 3090.
- 🟡 **Gate v2 = PARCIAL**: fortes 100/99 (ALVARA, PAGAMENTO, DECISAO);
  **ACORDAO 16%** — causa = docs longos **dropados** do treino (3.227
  exemplos >4096 tok; treino não viu, teste vê truncado). Lever confirmado:
  **janelar no gerador** (virou o v2.1).
- ✅ **GGUF v2** empacotado (`59db32db…`).

## 29/07 — v2.1 + dia de incidentes (4 lições operacionais)

- ✅ **Dataset v2.1**: 211.928 ex — 37.146 docs longos **recuperados via
  janelamento**; drop all-masked 3.227→10; colisões de split=0.
- 💥 **2 OOMs no treino**: a **extração recorrente ainda estava em voo** na
  janela de treino — o timer systemd foi pausado, mas a execução corrente
  continuou rodando na mesma GPU.
  **Lição: pausar timer ≠ matar execução corrente** — checklist de janela de
  GPU inclui `systemctl stop` E kill do processo em voo.
- 🐛 **Monitor com bug de zsh**: `$VAR-comando` em zsh **não sofre
  word-split** como em bash → o comando montado por variável nunca executava
  → falsos **INACESSIVEL** na telemetria.
  **Lição: função shell em vez de comando-em-variável.**
- 💥 **DAPT: guerra de orquestradores** — 3 "donos" simultâneos (launcher
  manual, timer, retry) disputando o mesmo treino, matando/relançando um ao
  outro.
  **Lição: 1 dono por treino + kill explícito do anterior + supervisor com
  auto-resume** (implementado no pod DAPT).
- 💥 **/tmp tmpfs com quota no zordon matou o log** de um job longo
  (tmpfs cheio → escrita falha → job segue cego).
  **Lição: logs de jobs longos em `/home`, nunca em /tmp.**
- 📏 **Protocolo κ humano calibrado**: régua ✗ = **afirmação falsa** (grave)
  / ? = **abstenção com evidência** (tolerável). Resultado da amostra de 60
  docs no card v2 (`.ia/MODELOS.md`): PROCURACAO 62% com 3 falsas graves
  (CPF trocado ×2, nome truncado+papel errado); DESPACHO/DECISAO/ALVARA 100%.

## 30/07 — incidente de fronteira + A/B + missão Analista

- 🚨 **Push indevido a repo de terceiros** (`precatorio-ai-analyzer`) —
  **REVERTIDO** com force pra `0baafbe`.
  **Lição/REGRA: repos de terceiros = read-only ABSOLUTO; todo trabalho em
  repo próprio** (`~/projetos/analista-lab`). Detalhes: `.ia/ANALISTA.md`.
- 📊 **A/B Qwen3-8B**: dev loss 0,0106 (vs 0,021 do v2.1 no mesmo ponto) —
  **não comparável entre tokenizers**; o juiz é o gate
  (`.ia/EXPERIMENTOS_MODELO.md` §0).
- 🧪 **Bake-off de professores em curso** (Ollama Cloud, 4 modelos × 10
  sessões, régua = respostas humanas E6): `.ia/ANALISTA.md` §A3.
- 🖥️ **IA LABS + Sala de Controle no ar** (acompanhamento dos treinos/pods).

## 31/07 — v2.1 fechado + incidente enriquecimento + diagnóstico herdeiros

- ✅ **v2.1 (Qwen2.5-7B) treino OK**: train 0,0056 / eval 0,007 (5× melhor que v1),
  2 épocas, 11,4h. Sobreviveu a **3 OOMs** — causa sempre a mesma: **ollama servindo
  na MESMA 3090 que treina**. Cura: supervisor auto-resume + travar ollama.
  **Lição: não treine onde serve; treinos novos nascem em pod.**
- ☁️ **Gate v2.1 migrado 3090→pod 4090** (pra liberar a 3090): mover treino EM CURSO
  não vale (overhead > trabalho restante); mover **gate/próximo treino** vale.
- 🚨 **Incidente: enriquecimento parado 7 dias** — consumer group Redis
  `enrichment-drainer` sumiu; drainers em loop `NOGROUP` sem recriar → 1,6M jobs
  represados, ~1M resultados trimados (perda). Fix runtime + **drainer auto-cura**
  (commit fcc0850, deployado). **Lição: consumer de stream tem que recriar o grupo no
  NOGROUP, não só no boot.** Memória: `incidente-drainer-nogroup`.
- 🔬 **Diagnóstico herdeiros (o aprendizado do ciclo)**: F1 ~20% NÃO é OCR nem falta
  de docs. **73-76% do gold de herdeiros está VAZIO** (`herdeiros:[]`) mesmo com texto
  real (~4k chars). Causa: (a) doc-classificador super-inclui capa/certidão como
  HABILITACAO_HERDEIROS; (b) **gold estrito (dupla-concordância) abstém** na decisão
  difícil "quais nomes, no meio de advogado/tabelião/falecido, são herdeiros" → zera a
  lista. O modelo **aprendeu a não emitir herdeiros** (73% dos exemplos ensinam vazio)
  e a métrica mede só a minoria. **Lição: antes de "mais dado", medir a QUALIDADE do
  rótulo — dado ruim em volume piora.**
- 🎯 **Plano v2.2 herdeiros**: (1) **re-rotular com professor** (Ollama Cloud, prompt
  cirúrgico "só herdeiros, excluir advogado/inventariante/tabelião/falecido") — rotular
  offline é onde o modelo grande ajuda (≠ inferência, onde o Qwen3 não resolveu); (2)
  apertar a fonte; (3) rebalancear (upsample não-vazios); (4) métrica honesta (F1 nos
  não-vazios + acerto de abstenção separados). Professor por **mini bake-off específico
  de herdeiros** (≠ bake-off do Analista, que é parecer) — por dado, não por fama.

---

## Mortos com autópsia (pré-ciclo — não repetir o caminho)

| experimento | causa da morte | autópsia |
|---|---|---|
| Prompt DSPy/GEPA (natureza) | Goodhart: +30pp na régua Falcon, −37pp vs texto no held-out → gate BLOCK | `.ia/RAG_EVAL_OBS.md` |
| Quantização bit-index (pgvector) | recall: overlap@8 0,49 → gate BLOCK (a amostra pequena mentiu) | `.ia/QUANTIZACAO_INDICE_VETORIAL.md` |
| pgvectorscale SBQ | limite estrutural: não suporta >930 dims | `.ia/PGVECTORSCALE_SBQ.md` |

Padrão comum dos três: a régua barata mentiu e o **gate pré-registrado
segurou**. É por isso que todo experimento novo nasce com critério
pré-registrado (`.ia/EXPERIMENTOS_MODELO.md`).
