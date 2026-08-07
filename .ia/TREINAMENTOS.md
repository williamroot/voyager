# TREINAMENTOS — memória consolidada dos treinos de modelo

> **Ponto de entrada único** sobre TUDO que envolve treinar/servir os modelos do
> extrator. Consolida e cruza: registry (`MODELOS.md`), experimentos em curso
> (`EXPERIMENTOS_MODELO.md`), diário cronológico (`LABLOG.md`), extração
> entity-centric (`FICHA_PARTE.md`). Se você é um agente começando: **leia isto
> primeiro**, depois abra o específico.
>
> Última consolidação: **2026-08-01**.

---

## 1. O que estamos treinando e por quê

**Objetivo:** um modelo próprio que lê os autos de um precatório e produz a
**Ficha da Parte** (partes, papéis, valores, cessão, herdeiros, decisões, estágio
do crédito) rodando **on-device**, barato, sem depender de API externa — e com a
regra inviolável: **leitura correta > velocidade; o modelo decide o semântico, a
mecânica confirma o literal verbatim, na dúvida ABSTÉM (nunca chuta).**

**Base:** `Qwen2.5-7B-Instruct` + fine-tune **QLoRA**. Empacota em **GGUF Q4_K_M**
(~4,68GB, serve ~6GB VRAM) e serve via `llama-server` (grammar GBNF por request).

**Por que Qwen2.5-7B e não maior/outro:** o A/B com Qwen3-8B **reprovou o gate**
(ganho <2pp macro, regride DOC_PESSOAL). Modelo maior NÃO resolve valor/natureza
(erro é vazamento de dado, não capacidade); resolve completude de partes — mas o
lever real foi **janelamento de docs longos**, não a base. Ver `LABLOG.md`.

---

## 2. Linha do tempo dos modelos (o que aconteceu)

| Versão | Data | Gate | Lição-chave |
|---|---|---|---|
| **v1** | 28/07 | TEST macro **87,9** (+9,9 vs base) | 1ª geração one-shot. OOM na eval → bs 2→1. Unsloth descartado (conflito de stack). |
| **v2 "Ficha da Parte"** | 28-29/07 | PARCIAL — **ACORDAO 16%** | Causa = docs longos **dropados** do treino (>4096 tok). Lever confirmado: **janelar**. Extração vira por-DOCUMENTO (entity-centric). |
| **v2.1** (CAMPEÃO) | 30-31/07 | **PASS macro 91,76** (loss 0,0082) | Janelamento recuperou 37k exemplos longos (dataset 211.928). Buracos do v2 tapados. GGUF md5 `0012607b1634e7b8f96c8f6a9d7bad21`. |
| **A/B Qwen3-8B** | 31/07 | 🔴 **BLOCK** (+1,6 macro, regride DOC_PESSOAL) | Base alternativa não compensa. Ficamos no Qwen2.5-7B. Serviu pra **matar a dúvida** da base. |
| **v2.2 (herdeiros)** | 31/07→ | 🔵 em treino/gate | Gold de herdeiros re-rotulado com professor (**DeepSeek** venceu bake-off); docs com herdeiro 25%→49% (363 recuperados). Corrige a fraqueza de herdeiros do v2.1. |

**Métrica barata mente (meta-lição recorrente — já bateu 5×):** bit-index sample,
herdeiros gold, vetorizados SET, docs-as-exhaustion, embed-probe. **Decidir só no
ground-truth de saída**, nunca numa proxy. Todo gate roda no MESMO held-out por
hash-CNJ (`eval_gate_v2.py`).

---

## 3. Experimentos em curso (detalhe em `EXPERIMENTOS_MODELO.md`)

| # | Experimento | Hipótese | Critério pré-registrado | Status (01/08) |
|---|---|---|---|---|
| 1 | **DAPT** (pré-treino continuado) | adaptar ao texto dos autos (OCR/tabelas DEPRE) antes do SFT sobe tudo | bater SFT-direto no mesmo held-out | 🔵 **~92%** (step 3502/3814, loss 0,5585) |
| 2 | **Destilação 3B** | nas classes fortes (ALVARA/PAGAMENTO 99-100) um 3B basta | aluno ≥ professor−2pp nas fortes | 🟡 harness pronto; dispara pós-gate |
| 3 | **DPO-κ** | preferências da fila κ matam o chute | falsa-afirmação **−50%** sem perder cobertura >2pp | 🟡 dataset pronto (2.688 pares) |
| 4 | **Especialistas LoRA** (adapter por classe fraca) | ACORDAO/CESSAO/herdeiros ganham com adapter dedicado | bater multi-task por classe no gate | ✅ 7 adapters treinados; comparação pós-gate v2.1 |

**Regra dos experimentos:** nada é promovido sem passar os **gates** (mesmo TEST
held-out, precisão@cobertura por campo). Gate BLOCK = modelo morre com autópsia
documentada (não repetir o caminho). Mortos: prompt DSPy/GEPA natureza (Goodhart),
quantização bit-index (recall BLOCK), pgvectorscale SBQ (limite estrutural).

---

## 4. Como treinamos (harness)

**Onde vive o harness de treino/gate:**
`llmsv2:/mnt/nas-data/voyager-train/` — `train_peft_v2.py` (treino QLoRA),
`mix_datasets.py`, `eval_gate_v2.py` (gate), saídas em `out/`.

**Hiperparâmetros QLoRA (padrão v2.x):** `r=32 / α=32`, 2 épocas, `bs=1 × accum=16`,
`lr=2e-4` cosine, `max_seq=4096`, `transformers 4.46.3` + peft + trl (stack
**vanilla** — Unsloth descartado por conflito). DAPT usa `r=64 all-linear`.

**Datasets versionados por md5** (dados fora do git; hash é o vínculo — ver
`MODELOS.md` §Datasets):
- v1: `train_extracao_v2.jsonl` (10.850 ex por-processo)
- v2: `train_fichaparte_full.jsonl` (196.360 ex por-**documento**)
- **v2.1: 211.928 ex** (37.146 longos recuperados via **janelamento** — o lever)
- DPO: 2.688 pares (forte×fraca + correções humanas + abstenção)

**Fluxo:** gerar dataset (no zordon: `eval/build_*.py`) → treinar (`train_peft_v2.py`)
→ **gate** (`eval_gate_v2.py`, held-out por hash-CNJ) → se PASS, empacotar GGUF Q4_K_M
(llama.cpp) → registrar md5 no `MODELOS.md` → servir no pod.

**Empacotar GGUF:** converter adapter+base → GGUF via llama.cpp `convert` +
`quantize Q4_K_M`. RTX 5090 (Blackwell sm_120) precisa CUDA 12.9 + llama.cpp com
`-DCMAKE_CUDA_ARCHITECTURES=120`.

---

## 5. Onde os treinos rodam (GPUs)

| Onde | GPU | Papel | Custo | Acesso |
|---|---|---|---|---|
| **llmsv2** (lab, Tailscale) | RTX 3090 24GB | harness de treino/gate, mirror dos repos, empacota GGUF | luz | `ssh ubuntu@llmsv2` |
| **trainpod** (QuickPod) | RTX 4090 24GB | treinos pesados (v2.2, A/B, DAPT) | ~$0,19-0,31/h | `ssh trainpod` (config pronta) |
| **pod showcase** (QuickPod) | RTX 5090 32GB | **serve** os GGUF (não treina) | ~$0,50/h | ver `~/VOYAGER_ACCESS.md` §4.1 |
| **pod 3090** (QuickPod) | RTX 3090 | DAPT + especialistas | ~$0,19/h | — |
| **voyager-worker-mac** (lab, Tailscale) | Apple **M4** 10-core, 24GB unificados | **serve** GGUF via Metal (não treina) | $0 (hardware próprio) | `ssh davicordeiro@192.168.200.37` · [`GPU_MACOS.md`](GPU_MACOS.md) |

**Regra ops:** **NÃO treinar onde serve** e **não travar a 3090 do llmsv2** (é
compartilhada com o bge-m3 da vetorização e com outro projeto). Mover treino pra
pod 4090 quando precisar da 3090 livre. Supervisor auto-resume
(`trainer.train(resume_from_checkpoint=True)`) contra OOM/queda.

**Incidente recorrente — OOM na 3090:** ollama subindo na GPU compartilhada estoura
VRAM no checkpoint. Fix: `systemctl disable ollama` + resume do checkpoint.

**Gotcha de serving (llama-server):** `-c N --parallel P` divide o contexto →
`N/P` tok/slot. `-c 16384 --parallel 4` = 4096/slot → **HTTP 400** em doc denso.
Fix barato: baixar `--parallel` (mesma VRAM). Ver `~/VOYAGER_ACCESS.md` §4.1.

**Apple Silicon é ~1 ordem de grandeza mais lento (medido 2026-08-07):** no M4
(10-core GPU), `llama-bench` no Qwen2.5-7B Q4_K_M deu **pp512 244,67 t/s** e
**tg128 22,86 t/s** → ~**25 s por janela** de documento do SDK. O PDF de 1,5 GB
(2.765 docs, ~13 min no pod 5090) daria **~19 h**. Serve pra **lote noturno**, não
pra demo ao vivo. Detalhe em [`GPU_MACOS.md`](GPU_MACOS.md).

---

## 6. Estado ATUAL dos treinos (snapshot 2026-08-01 ~19:53)

Fonte ao vivo: scheduler grava `treinos_status.json`; UI em `/dashboard/treinos`
("Sala de Controle"). Contadores: 1 rodando · 9 concluídos · 4 na fila · ~$0,19/h.

| Run | Estado | Loss | Nota |
|---|---|---|---|
| **v2.1** (Qwen2.5-7B QLoRA) | ✅ done (3045/3045) | **0,0082** | CAMPEÃO, empacotado, servindo |
| **A/B Qwen3-8B** | ✅ done (3044/3045) | 0,0109 | reprovou gate → arquivado |
| **7 Especialistas LoRA** | ✅ done | — | comparação por-classe pós-gate |
| **DAPT** | 🔵 **91,8%** (3502/3814) | 0,5585 | quase fechando |
| DPO-κ · Destilação 3B · Bake-off · Analista | ⏳ fila | — | disparam em sequência |

---

## 7. Modelo servido em produção (o que a showcase usa)

- **Campeão v2.1** GGUF Q4_K_M (`0012607b1634e7b8f96c8f6a9d7bad21`), servido no pod
  5090 (`159.48.242.22:32003`), junto de v1 (`:32001`) e v2 (`:32002`).
- A tela `/dashboard/ia/showcase` chama via proxy Django (`settings.SHOWCASE_MODELOS`).
- **v2.2** entra no slot `:32004` quando gatear (`/opt/extrator/add_v22.sh`).
- **Tier-2 modelo-decisor** (classificador híbrido) existe atrás de flag
  `ARBITRO_MODELO` mas está **OFF** (A/B: 0 ganho + 50% mais lento em 1 doc;
  reavaliar em lote).

---

## 8. Modelos vizinhos (não são o extrator, mas são treino/ML)

- **classificador-leads v7** 🟢 ativo — `.ia/CLASSIFICACAO.md`.
- **estagio-credito (GBM) estagio_v1** 🟡 gate PASS (EMITIDO p=0,954 / MORTO p=0,926,
  thr 0,80), joblib `6bf0c7a4…`, lib `tribunals/estagio.py`, doc `.ia/ESTAGIO_CREDITO.md`.
  Dataset: 820.777 rótulos de estágio (supervisão cruzada autos×Falcon).
- **Analista IA** (missão) — substituir o GPT do Falcon por modelo próprio;
  trabalho em `~/projetos/analista-lab`; repo Falcon = **read-only absoluto**.

---

## 9. Para o agente: como retomar treino numa máquina nova

1. Ler este doc + `EXPERIMENTOS_MODELO.md` + `LABLOG.md` (últimas entradas).
2. Estado ao vivo: `ssh ubuntu@llmsv2 'cd /mnt/nas-data/voyager-train && ls -t out/'`
   e `/dashboard/treinos` (ou o `treinos_status.json` do scheduler).
3. Acessos (pods, chaves, SSH): `~/VOYAGER_ACCESS.md`.
4. **Nunca** promover sem gate PASS no held-out por hash-CNJ. **Nunca** decidir por
   métrica-proxy. **Nunca** treinar na 3090 do llmsv2 sem confirmar que está livre.
5. Registrar todo ciclo em `LABLOG.md` (avanço E regresso, com a lição) e atualizar
   o registry em `MODELOS.md` (md5, gate, status) na mesma PR.
