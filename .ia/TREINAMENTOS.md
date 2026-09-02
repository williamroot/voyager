# TREINAMENTOS — memória consolidada dos treinos de modelo

> **Ponto de entrada único** sobre TUDO que envolve treinar/servir os modelos do
> extrator. Consolida e cruza: registry (`MODELOS.md`), experimentos em curso
> (`EXPERIMENTOS_MODELO.md`), diário cronológico (`LABLOG.md`), extração
> entity-centric (`FICHA_PARTE.md`). Se você é um agente começando: **leia isto
> primeiro**, depois abra o específico.
>
> Última consolidação: **2026-09-02** (triagem de pendências de ML).

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
| **v2.2 (herdeiros)** | 31/07→ | ⚫ **NÃO FECHOU** | Gold re-rotulado com professor (**DeepSeek** venceu o bake-off); docs com herdeiro 25%→49% (363 recuperados). **O dado está pronto no NAS (`data/*_v22.jsonl`); o `adapter_v22` NÃO existe** — o pod 4090 foi destruído com o treino em voo. Retomar = subir GPU e rodar (não é pesquisa nova). |
| **DAPT** | 02-03/08 | 🔴 **BLOCK** | SFT-sobre-DAPT não bate SFT-direto: v2 macro +1,08pp, v1 +0,06pp (critério ≥+2pp) e **`partes` PIORA** (0,5139→0,5081). Pré-treino no dialeto próprio não paga o custo. Ver `EXPERIMENTOS_MODELO.md` §1. |

**Métrica barata mente (meta-lição recorrente — já bateu 5×):** bit-index sample,
herdeiros gold, vetorizados SET, docs-as-exhaustion, embed-probe. **Decidir só no
ground-truth de saída**, nunca numa proxy. Todo gate roda no MESMO held-out por
hash-CNJ (`eval_gate_v2.py`).

---

## 3. Experimentos — situação REAL (detalhe em `EXPERIMENTOS_MODELO.md`)

Nenhum está em curso. Um morreu no gate, um está pago e não medido, dois nunca
dispararam:

| # | Experimento | Hipótese | Critério pré-registrado | Status (02/09) |
|---|---|---|---|---|
| 1 | **DAPT** (pré-treino continuado) | adaptar ao texto dos autos (OCR/tabelas DEPRE) antes do SFT sobe tudo | bater SFT-direto por ≥+2pp no mesmo held-out | 🔴 **BLOCK (03/08)** — +1,08pp; morto |
| 2 | **Destilação 3B** | nas classes fortes (ALVARA/PAGAMENTO 99-100) um 3B basta | aluno ≥ professor−2pp nas fortes | 🟡 harness pronto; **nunca rotulou** (professor definido desde 31/07) |
| 3 | **DPO-κ** | preferências da fila κ matam o chute | falsa-afirmação **−50%** sem perder cobertura >2pp | 🟡 dataset pronto (2.688 pares); **nunca treinou** |
| 4 | **Especialistas LoRA** (adapter por classe fraca) | ACORDAO/CESSAO/herdeiros ganham com adapter dedicado | bater multi-task por classe no gate | 🟠 7 adapters treinados; **gate NUNCA rodado** — GPU paga, pergunta sem resposta |

**Regra dos experimentos:** nada é promovido sem passar os **gates** (mesmo TEST
held-out, precisão@cobertura por campo). Gate BLOCK = modelo morre com autópsia
documentada (não repetir o caminho). Mortos: **DAPT** (03/08), prompt DSPy/GEPA
natureza (Goodhart), quantização bit-index (recall BLOCK), pgvectorscale SBQ
(limite estrutural), A/B Qwen3-8B.

**Corolário que este ciclo escreveu:** gate BLOCK é resultado e é barato; o caro é
o experimento que fica **sem veredito**. Adapter treinado e nunca gateado (nº 4)
custou GPU e não respondeu nada — pior que um BLOCK, que pelo menos fecha a
pergunta.

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
| ~~**trainpod** (QuickPod)~~ | RTX 4090 24GB | treinos pesados (v2.2, A/B, DAPT) | — | 🔴 **MORTO** (02/09: `Permission denied (publickey)` — pod destruído; levou a v2.2) |
| **pod showcase** (QuickPod) | RTX 5090 32GB | **serve** os GGUF (não treina) | ~$0,50/h | ver `~/VOYAGER_ACCESS.md` §4.1 |
| ~~**pod 3090** (QuickPod)~~ | RTX 3090 | DAPT + especialistas | — | 🔴 **MORTO** (adapters salvos no NAS antes de cair) |
| **voyager-worker-mac** (lab, Tailscale) | Apple **M4** 10-core, 24GB unificados | **serve** GGUF via Metal (não treina) | $0 (hardware próprio) | 🔴 **offline há 4d em 02/09** — `ssh davicordeiro@192.168.200.37` · [`GPU_MACOS.md`](GPU_MACOS.md) |

**Só sobrou uma GPU viva: a 3090 do `llmsv2`** (02/09: 4 MiB usados, ollama
desligado, `zordon-extrair` sem timer). Todo trabalho de GPU pendente
(gate dos especialistas, retreino v2.2, DPO-κ, destilação 3B, servir o GGUF pra
showcase) cabe nela — a alternativa é subir pod novo e **sincronizar checkpoint
pro NAS**, que é a lição que a v2.2 pagou.

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

## 6. Estado ATUAL dos treinos (verificado 2026-09-02 na fonte, não na UI)

⚠️ **Nada está treinando.** Verificado por SSH no harness
(`llmsv2:/mnt/nas-data/voyager-train/out/`) e nos pods:

| Run | Estado | Evidência |
|---|---|---|
| **v2.1** (Qwen2.5-7B QLoRA) | ✅ CAMPEÃO, empacotado | `out/extrator-v21-Q4_K_M.gguf` + `adapter_v21` |
| **A/B Qwen3-8B** | 🔴 reprovou o gate → arquivado | +1,6 macro < 2 |
| **7 Especialistas LoRA** | 🟠 treinados, **sem gate** | `out/adapter_esp_*` (7 dirs, 02/08) |
| **DAPT** | 🔴 **BLOCK** | `out/adapter_dapt` (02/08); veredito 03/08 |
| **v2.2 (herdeiros)** | ⚫ **nunca produziu adapter** | `data/*_v22.jsonl` existem; **`out/adapter_v22` não existe** |
| DPO-κ · Destilação 3B | 🟡 dataset/harness prontos, nunca dispararam | — |

**Infra:** `trainpod` (4090) e o pod 3090 **não respondem mais** (chave recusada /
destruídos). O `voyager-worker-mac` que servia os GGUF está **offline há 4 dias** —
`/dashboard/ia/showcase` não tem modelo atrás dele. A **3090 do llmsv2 está LIVRE**
(4 MiB usados, ollama desligado): é a GPU disponível hoje, e é onde o gate dos
especialistas e o retreino da v2.2 caberiam sem custo de pod.

> **Lição operacional deste ciclo:** treino em pod efêmero sem checkpoint
> sincronizado para o NAS = trabalho que evapora com o pod. A v2.2 tem o dado
> caro (relabel com professor, κ 9/10) e não tem o modelo. Sincronizar
> checkpoint → NAS é pré-requisito de qualquer treino novo em pod.

---

## 7. Modelo servido em produção (o que a showcase usa)

- **Campeão v2.1** GGUF Q4_K_M (`0012607b1634e7b8f96c8f6a9d7bad21`). Desde
  **2026-08-07** servido no **`voyager-worker-mac`** (Mac mini M4, Metal) em
  `100.105.16.107:8003`, junto de v1 (`:8001`) e v2 (`:8002`) — o pod 5090
  (`159.48.242.22:3200x`) saiu do ar. Ver [`GPU_MACOS.md`](GPU_MACOS.md).
  ⚠️ **02/09/2026: o Mac está OFFLINE (last seen 4d) e as 3 portas não respondem
  — a showcase está sem modelo.** Religar o Mac ou subir `llama-server` na 3090
  livre do llmsv2 antes de prometer demo.
  ⚠️ ~1 ordem de grandeza mais lento (22,9 t/s vs ~150-200 da 5090).
- A tela `/dashboard/ia/showcase` chama via proxy Django (`settings.SHOWCASE_MODELOS`).
- **v2.2** entra no slot `:32004` quando gatear (`/opt/extrator/add_v22.sh`).
- **Tier-2 modelo-decisor** (classificador híbrido, task #85) existe atrás de
  flag `ARBITRO_MODELO`, com testes, e está **OFF**. A decisão de manter OFF
  repousa num A/B de **1 documento** (0 ganho, 50% mais lento) — pela regra da
  casa isso é *dica*, não gatilho. Reavaliar exige lote rotulado (κ de
  segmentação), não outra rodada de opinião.

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
