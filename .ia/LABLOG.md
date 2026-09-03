# LABLOG — diário de bordo dos experimentos de ML

> Cronológico, **avanços E regressos**. Regra da casa: documentar rico —
> incidente sem lição registrada é incidente que repete. Estado atual dos
> experimentos: `.ia/EXPERIMENTOS_MODELO.md`; registry: `.ia/MODELOS.md`.

Formato: cada entrada = o que aconteceu + (quando for regresso) a **lição**.

---

## 03/09 — herdeiros com o gold v2.2: BLOCK no gate, hipótese CONFIRMADA (+17,88pp só do rótulo)

O dono do produto escolheu, entre quatro usos da única GPU viva, **retreinar
`herdeiros` com o gold v2.2** — preferiu o conserto de raiz a religar a showcase.
Deu certo como ciência e **reprovou como produto**, e as duas coisas são o resultado.

### Primeiro medir o gold, depois queimar GPU

A ordem era essa e ela evitou trabalho perdido. O gold v2.2 tem **50,08%** (train) e
**51,95%** (test) de `herdeiros` vazio contra os **73,44%/75,88%** do v2.1 — os nomes
de herdeiro vão de 384→**894** e 270→**809**. E não é rótulo inventado: **96,4%** dos
nomes novos aparecem **literalmente** na janela (2,0% sem suporte). O relabel também
**tira**: 30 linhas foram de preenchido→vazio, além das 168 vazio→preenchido.

### O buraco na régua do achado de ontem

Ao reproduzir o R115 apareceu isto no `score_v2`: `if not gv: continue` — **com gold
de lista vazio a lista não é pontuada**. Ou seja, `herdeiros.f1_entidade` mede só os
130 exemplos de gold cheio e **emitir herdeiro onde o gold não tem nenhum sai de
graça**. Métrica de recall vestida de F1.

Régua nova (`herd_score.py`, sem tocar no `score_v2`): **F1 por exemplo em todos os
539**, vazio×cheio = 0. Ela **reproduz o R115 exatamente** (27,92 / 36,44 / 40,01 e os
ICs) — e mostra que o achado dele **não só sobrevive como cresce** sob o gold
consertado: dos **383 falsos positivos** da base crua sob o gold velho, **263 viram
verdadeiros** sob o novo, com grounding de **98,45%**. Não era tagarelice; era o gold
que estava calado. O campeão em produção tem **recall de 18,75%**.

### O experimento: mesma linha, mesma receita, só o rótulo muda

Dois adapters QLoRA idênticos (mesmas 497 linhas na mesma ordem, mesmo dev recortado
por hash-CNJ, mesma seed), divergindo **só no rótulo**. Na régua v2.2:

| | F1/ex | micro-R | `f1_entidade` | `falecido` |
|---|--:|--:|--:|--:|
| rótulo velho (`adapter_herd_v21`) | 61,64% | 31,63% | 31,05% | 85,87% |
| **rótulo novo (`adapter_herd_v22`)** | **79,53%** | **70,79%** | **71,10%** | **92,17%** |

**Δ = +17,88pp, IC 95% pareado [+13,72, +21,97], McNemar p=2,7×10⁻¹³.** Contra o
campeão v2.1: **+21,39pp**. Contra a base crua (que ontem ganhava): **+6,59pp**,
IC [+2,86, +10,27]. Controle de protocolo ✅ (meu `herd_v21` 61,64 ≈ `esp_herdeiros`
60,68 de 02/08).

**Os 7 especialistas moveram −0,52 a +0,02pp no mesmo campo. O rótulo move 18.** A
hipótese "o teto é o DADO" deixou de ser leitura de resíduo e virou ablação pareada.

### E mesmo assim: 🔴 BLOCK

O critério pré-registrado (`2b68c55`, antes de treinar) exigia **quatro** condições.
Passaram três com folga — bate o v2.1, bate a base crua, e **melhora** `falecido`
(+6,95pp). Reprovou a quarta: **emissão indevida 12,86% contra o teto de 9,07%**
(base + 3pp). Não afrouxei, como o R115 não afrouxou o C2 que falhou em duas tarefas.
**O adapter não vai para produção.**

- 🔬 **Autópsia na população INTEIRA (36 casos, não amostra):** ~27 (75%) são **erro
  real de papel** — co-exequente de ação coletiva, **advogado com número de OAB**,
  inventariante, o **polo passivo** ("Requerido: Aldo Luis Pessagno") e um vazamento
  do próprio falecido; 3 (8%) são gold ainda calado (o texto diz "HERDEIROS DE X:" e
  o gold está vazio); 6 (17%) ambíguos no próprio documento.
- ⚠️ **A amostra pequena mentiu de novo — 7ª vez.** Nos 8 primeiros casos a leitura
  dava **38% de gold calado**, e isso quase virou "a guarda reprovou por culpa do
  gold". Nos 36: **8%**. **Lição, de novo: se for cortar, corte com seed — e quando o
  resíduo cabe inteiro na mão, leia o resíduo inteiro.**
- 🎯 **A causa tem nome e endereço, e é dado outra vez.** O gold v2.2 traz
  `{nome, papeis:[herdeiro|inventariante|conjuge|sucessor]}` — foi a correção de 31/07
  para não excluir a viúva-inventariante. **Mas o alvo do treino no mix é lista chapada
  de nomes** (894 de 894 `str`, nenhum `dict`): os papéis são **descartados ao montar o
  mix**. O modelo recebe "às vezes emite, às vezes não" sem a razão junto e aprende o
  atalho *nome perto de espólio = herdeiro*. **Próximo lever: levar o papel para dentro
  do alvo e re-rodar esta mesma ablação de 2h26** — é `mix_datasets`, não modelo.
- 🧭 **Dependência de régua, declarada:** sob o gold v2.1 o sinal inverte na primária
  (E fica −18,76pp do campeão) e se mantém na métrica do R115 (+22,21pp). Estava
  previsto no pré-registro; fica registrado, não corrigido a posteriori.
- 🛠️ **Bug de harness consertado de passagem:** `train_peft_v2.py` chamava
  `trainer.train(resume_from_checkpoint=True)` — com diretório vazio o transformers
  4.46 levanta `ValueError` e o supervisor entraria em **loop de falha** no primeiro
  treino. Agora só retoma se existe checkpoint.
- 💰 **Custo: 2h26 de 3090** (2 treinos de 63 min + 2 gerações de ~9 min). O retreino
  multi-task completo (~28h) **não foi gasto**: a regra de parada dizia que ele só vem
  depois de um veredito, e o veredito é BLOCK. **A GPU está livre** — a showcase, que
  era o próximo uso na fila, pode entrar.

## 02/09 — Triagem das 7 pendências de ML + o buraco da vara (task #86)

Sete tarefas de ML/extração abertas há semanas. O trabalho do dia foi **decidir
quais ainda existem** — e uma delas rendeu conserto.

### O que a triagem achou (evidência, não impressão)

- **#23 (features extras no modelo DC→precatório)** — já respondida pela #25 e o
  veredito é NEGATIVO: Cox com as mesmas features deu **C-index 0,688**, igual ao
  KM servido, e só `log_valor` era novo (`.ia/JURIMETRIA.md` §Freshness). Mas o
  dataset **já emite** tribunal/valor/órgão/classe/assunto
  (`scripts/treinar_sobrevivencia_dataset.py`) e o fit **ignora tudo**
  (`feats = ['ente_tipo','natureza']`). Ou seja: a pergunta do #23 continua
  literalmente sem resposta — falta 1 linha de código e 1 varredura dupla de DB.
- **#45 (Qwen2.5-7B + grammar no `llm.py`)** — meio obsoleta. O extrator que roda
  de verdade (`zordon acervo/extrator_metadados.py`) **já** usa
  `qwen2.5:7b-instruct-q4_K_M`; quem ficou no `gpt-oss:20b` é o `acervo/llm.py`
  do caminho RAG — e esse caminho é o que o **DSPy tentou melhorar e levou BLOCK**.
  O que sobra de válido é a outra metade do enunciado (grammar/JSON-Schema, enum
  fechado, gate de confiança): nenhum dos dois caminhos tem, os dois usam JSON
  livre. Metade morta, metade viva.
- **#47 (`stream_extract.py` + watchdog)** — não existe em lugar nenhum
  (`git ls-tree origin/main` do zordon: só `stream_backfill.py`). Vale, mas
  **está atrás de #48**: orquestrar volume antes de medir precisão é fabricar
  59k linhas em que ninguém confia.
- **#48 (precisão@cobertura vs Juriscope)** — não existe harness. É a mais
  importante das três, e o gate do DAPT explica por quê: `valor_oficio` acerta
  **92% no held-out limpo** e deu **0 nos 28 ofícios do processo real de 1,5 GB**.
  Held-out não é campo. Sem #48 não existe número que diga se a extração presta
  no acervo de verdade — e é isso que #47 iria multiplicar por 59 mil.
- **#61 (DSPy F3-4)** — a F2 fechou **BLOCK** (Goodhart: +30,8pp vs Falcon,
  −37,5pp vs texto, McNemar p≈1e-23). A metade "promover" morreu com ela. A
  metade que sobrevive (**adjudicar os 63 resíduos `falcon_disagree` com κ** para
  estimar o ε do Falcon) não é DSPy — é a régua, e vale por si.
- **#85 (classificador híbrido)** — **feita**: Tier-1 regex-guia + Tier-2 árbitro
  on-device com enum fechado e abstenção (`pipeline._arbitrar_classes`,
  `classificador.classificar_pagina_hibrido`), testes cobrindo ligado/desligado/
  abstém. Fica OFF — mas o A/B que sustenta esse OFF é de **1 documento**.
- **#86** — parcialmente feita, com um buraco de verdade. Ver abaixo.

### O buraco: a vara nunca foi guardada

A passada verbatim (`extrator/verbatim.py`, commit d8f3951) guardava valor, data,
nome e CPF/CNPJ/OAB — e deixava passar **três literais** rotulados no código como
"semântico": `oficio.vara_expedidora`, `decisao.vara_comarca`,
`acordao.orgao_julgador`. Não são semânticos: são cópia do cabeçalho do ato. E são
**o erro nº 1 já medido**: "vara plausível trocada" é o maior balde do dataset
DPO-κ — **1.163 dos 2.688 pares**. Ou seja, sabíamos qual era o erro dominante e
tínhamos deixado exatamente esse campo sem guarda.

**Medido no PDF real dos autos 0027335-97.2021.8.26.0053** (150 págs → 49 docs):

| cenário | atribuições de vara | sobrevivem |
|---|--:|--:|
| modelo diz a vara CERTA em todo doc | 15 | **2** |
| modelo diz vara de OUTRO processo | 15 | **0** |

Só **2 dos 15 documentos escrevem a vara**; nos outros 13 o modelo a transportava
de outro lugar. E o custo disso na ficha é **zero**: o merger dedupa vara por nome
canônico, então a ficha continua com `15ª Vara da Fazenda Pública` — muda só o
`doc_ids`, de 15 documentos alegados para os 2 que provam.

**O risco real não era deixar passar, era abster demais.** A primeira versão
reprovou o próprio controle: `Décima Quinta Vara` não casava com `15ª Vara`
(a canonização NFKD transforma `15ª` em `15A`). Fix: ordinal reduzido a número
dos **dois lados** (`15ª`→`15`, `Décima Quinta`→`15`, `QUINTA`→`5`). Controle com
as 6 grafias que aparecem no PDF real: **6/6 sobrevivem**; 3 varas de outro
processo: **3/3 caem**.

### A cobertura virou contrato (é o que faltava)

`guardar_saida` era uma cadeia de `if/elif` — e cadeia de `if` não tem como provar
que cobre tudo. Virou tabela (`GUARDAS_ESCALARES`) mais dois registros explícitos
(`CAMPOS_COMPOSTOS`, `CAMPOS_SEMANTICOS`), e `tests/test_verbatim_cobertura.py`
exige que a união dos três seja **exatamente** `ABSTENCAO_VAZIA`: tarefa ou campo
novo sem decisão **quebra a suíte**. Em cima disso, para cada um dos 30 campos
escalares: mutação (literal fabricado cai + deixa flag) e **CONTROLE** (literal
verdadeiro sobrevive) — sem o controle, uma guarda que abstém de tudo "passaria".
Gate conferido por mutação: desligar `orgao_verbatim` → 7 falhas; tirar um campo
da tabela → 2 falhas.

- 💥 **Achado de manutenção: a suíte do SDK estava VERMELHA na `main` desde
  04/08** (3 testes). Os 7 leitores dedicados entraram e o invariante
  `test_instrucoes_cobrem_as_7_tarefas` passou a falhar — e ficou falhando.
  **Lição: invariante que falha e ninguém conserta deixa de ser invariante; vira
  ruído que esconde o próximo.** Os 3 voltaram a afirmar o contrato novo (14
  tarefas = 7 roteadas + 7 leitores; 6 chamadas ao LLM asseridas UMA A UMA;
  homologação implicada do ofício com proveniência explícita). Suíte:
  **158 passando/3 falhando → 256 passando/0 falhando**.

### O que a triagem achou de infra (pior que o ML)

- **`trainpod` (4090) e o pod 3090 estão destruídos.** O pod 4090 levou junto o
  treino da **v2.2 (herdeiros)**: o dado caro está no NAS
  (`data/herd_gold_v22.jsonl`, relabel com professor DeepSeek, κ 9/10),
  **`out/adapter_v22` não existe**. **Lição: treino em pod efêmero sem checkpoint
  sincronizado pro NAS é trabalho que evapora com o pod.**
- **Os 7 adapters especialistas (`out/adapter_esp_*`) foram treinados e o gate
  NUNCA rodou.** GPU paga, pergunta sem resposta. **Um BLOCK é um resultado; um
  experimento sem veredito é só custo.**
- **`voyager-worker-mac` offline há 4 dias** e as 3 portas do llama-server mudas →
  `/dashboard/ia/showcase` está sem modelo atrás. A **3090 do llmsv2 está livre**
  (4 MiB, ollama desligado): é a única GPU viva.
- **A extração recorrente não roda mais**: sem timer `zordon-extrair` no llmsv2,
  `zordon-db-1` do host de observabilidade parado há 5 semanas, ollama do llmsv2
  fora. `.ia/MODELOS.md`/`EXPERIMENTOS_MODELO.md`/`TREINAMENTOS.md` diziam
  "🔵 treinando" para tudo isso — **doc que descreve um estado que morreu é pior
  que doc ausente: convida a retomar um experimento já enterrado.** Corrigidos.

## 01/08 — Showcase do Extrator: pipeline completo + jurimetria na ficha

Tela investidor `/dashboard/ia/showcase` (IA LABS) fechada ponta-a-ponta, validada
no doc real 0027335 (150 págs, e-SAJ TJSP) no **modelo real** (v2.1), ~10s, on-device.

- ✅ **Segmentação estrutural** (SDK): fatiar autos combinados por FRONTEIRA (carimbo
  e-SAJ `TJ/SP - COMARCA` + reset do contador `Página:0001`), não por página. 63→41
  docs coerentes; beneficiário+valor param de virar órfãos.
- ✅ **Classificador híbrido**: regex herda sinais do Falcon (piso/GUIA) + hook Tier-2
  do NOSSO modelo (decisor, atrás de flag `ARBITRO_MODELO`). **A/B do Tier-2 no doc
  real: 0 ganho + 50% mais lento → mantido OFF** (métrica barata; decidir em lote).
- ✅ **Completude de partes**: capa e-SAJ (benef+CPF, adv+OAB, tipo, valor global),
  executada (Estado de SP), cessionário com CNPJ. **Vínculo pessoa↔documento = MODELO
  lê o contexto; mecânica só confirma verbatim; abstém se não bater** (mata o erro do
  Falcon de atribuir por proximidade). Filtro anti-parte-fantasma (rótulo ecoado).
- ✅ **Guarda verbatim** em TODO literal (valor/data/nome/doc): existe no texto-fonte
  ou é derrubado. Semântico (natureza/papel/desfecho) fica com o modelo.
- ✅ **Jurimetria na ficha**: bloco `estagio` — estágio do crédito (DC/PRE/EMITIDO/
  PAGO/MORTO), homologação (sim/não/não-identificada), **linha do tempo** com marcos
  ancorados (distribuição→trânsito→sentença→cumprimento→ofício→pagamento), próximos
  passos, e **status do valor por parte** (EXPEDIDO/HOMOLOGADO/REQUERIDO/…). No doc
  real: PRECATÓRIO EXPEDIDO, JOSE MARIO=EXPEDIDO R$18.910,46, homologação=desconhecido.
- ✅ **Wizard 3 passos** (Enviar→Modelo→Ficha) + **ZIP do TJSP** (vários PDFs → 1 ficha)
  + **export MD/PDF/JSON** (WeasyPrint 63.1).
- 🔧 **Gotcha ops**: `llama-server -c 16384 --parallel 4` = 4096 tok/slot → HTTP 400
  em doc denso. Fix barato: `--parallel 2` = 8192/slot, mesma VRAM.
- 📌 **Regra do usuário fixada**: precisão > velocidade em TODO dado; abster > chutar.
- 🧭 **Tópico aberto**: TJSP separa o crédito por INCIDENTE (vários CNJ:
  conhecimento→cumprimento→precatório) — unificar antes de decidir estágio (candidato
  a ROADMAP; morde a tela Estágio do Crédito).

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
- 🧪 **Mini bake-off herdeiros (4 professores Ollama Cloud, 27 docs)**: F1 vs gold —
  **deepseek-v4-pro 43,9%** > glm-5.2 37,8% > gpt-oss:120b 36,8% > kimi-k3 0%
  (quebrou, 27 erros de formato). 0 leak de falecido em todos. Vencedor = **DeepSeek**
  (não foi o GLM). **MAS** o F1 de 44% é enganoso — ver spot-check abaixo.
- 💡 **Spot-check reverteu a leitura (o achado do ciclo)**: nas 12 discordâncias
  DeepSeek×gold, o **GOLD é que está ERRADO** — rotulava o **inventariante como
  herdeiro** (≥4 casos: "sua inventariante Gertudes" virou herdeira) e **perdia
  herdeiros reais** (achou "Diego Luiz" que o gold omitiu). O DeepSeek exclui o
  inventariante e acha os que faltavam → está **mais certo que a régua**. **Lição: F1
  contra gold ruim mede o gold, não o modelo — só o olho humano no resíduo desambigua.**
  Ressalva: "excluir inventariante" é agressivo demais (viúva costuma ser inventariante
  E herdeira) → v2.2 extrai **partes COM PAPEL** ({nome, papeis:[herdeiro|inventariante|
  conjuge|sucessor]}), não lista pura, e o merge decide.
- 🏁 **Gate v2.1 (Qwen2.5-7B, ft_v21, n=3.727): MACRO v2 91,76%** — os buracos do v2
  FORAM tapados: ACÓRDÃO 16-27→98-100, SENTENÇA 60-80→97-100, CESSÃO 25-45→98-99,
  PROCURAÇÃO partes.f1 72,4→87,2. **Melhora clara — o janelamento funcionou.**
- ⚖️ **Desempate Qwen2.5-v21 vs Qwen3-8B (mesmo test)**: Qwen3 macro 93,36 (+1,60),
  partes.f1 89,7 (+2,5), partes.papel 76,9 (+10,1). **Critério pré-registrado (+2pp
  macro OU +3pp partes.f1, sem regressão >1pp): Qwen3 NÃO passa limpo** (macro +1,6<2;
  partes.f1 +2,5<3; regride DOC_PESSOAL papel −1,3). **Decisão: fica no Qwen2.5-7B**
  (menor/mais barato). **Lição: o ganho do A/B era o DADO (janelamento), não o modelo —
  provado: Qwen2.5+v21 (91,76) quase alcançou Qwen3+v21 (93,36).**
- 🔴 **Herdeiros continua quebrado nos DOIS** (v2.1 óbito 8,9/habilit 34,5; Qwen3
  20,6/54) — mas mede contra gold ruim; o relabel DeepSeek v2.2 é a cura.
- 🛠️ **Relabel herdeiros v2.2 (DeepSeek, 1.189 docs, 3 erros)**: docs com herdeiro
  **25%→49%** (585), **363 RECUPERADOS** (eram vazios, agora têm herdeiro com suporte no
  texto). Amostra κ = 9/10 recuperados corretos (1 borda: cedente marcada herdeiro).
  Papéis: herdeiro 1627, sucessor 444, inventariante 331, falecido 290, cônjuge 213.
  Gold + `train_mix_v22`/`test_mix_v22` no NAS (`data/herd_gold_v22.jsonl`). O TEST
  também foi corrigido → gate v2.2 re-avalia v2.1 no test novo p/ comparação JUSTA de
  herdeiros. Treino v2.2 no pod 4090 (não na 3090 — reservada). GGUF do v2.1 (campeão)
  empacotando em paralelo. **Regra reforçada: rótulo primeiro, modelo depois.**

- 🧭 **META-LIÇÃO do ciclo (repetiu 3×): a métrica barata mente — valide contra
  ground-truth antes de agir.** (1) bit-index: amostra pequena de recall mentiu → gate
  no corpus todo pegou (BLOCK). (2) herdeiros: F1 contra gold mentiu (gold rotulava
  inventariante como herdeiro) → spot-check humano no resíduo pegou. (3) vetorização:
  SET `vetorizados` defasado dizia "trabalho esgotado, 96/h" → medir docs-persistidos
  (3.312/h) evitou matar frota produtiva. **Regra: contadores Redis/amostras pequenas =
  dica, NUNCA gatilho de decisão irreversível. O gatilho é a verdade-de-campo.**

## 02/09 — gate dos 7 especialistas: BLOCK nos sete, e a base crua bate o campeão

- 🔴 **Experimento nº 4 RESPONDIDO — BLOCK nos 7.** Os `adapter_esp_*` estavam treinados
  desde 02/08 e **nunca gateados**: um mês de GPU paga sem pergunta respondida. Fechou em
  **~3h20** na 3090 do llmsv2. Δ contra o v2.1 no domínio de cada um: **de −0,52pp a
  +0,02pp**, e o **IC 95% pareado exclui o +3pp exigido nas SETE**. Família (≥2 de
  {acordao, cessao, herdeiros}): **0 de 3**. Tabela completa em
  `.ia/EXPERIMENTOS_MODELO.md` §2.3.
- 📌 **Critério pré-registrado e COMMITADO ANTES de medir** (`c714f8f`), com procedência
  auditada na fonte antes de qualquer geração. Nada foi afrouxado depois — inclusive a
  barra do controle C2, que **duas tarefas não limparam** e ficou registrado assim.
- 🧪 **Procedência: o dev set dos especialistas MENTIRIA.** `data/esp/dev_esp_*` foi o
  `--dev_file` do próprio treino, com `load_best_model_at_end=True` +
  `metric_for_best_model=eval_loss` → **serviu de seleção de modelo**. Medir ali seria o
  Goodhart do DSPy/GEPA de novo. O gate mediu no `test_mix_v21` (`split=test`), o MESMO
  held-out do macro 91,76, com **0 CNJ** em comum com o treino dos dois modelos.
  **Lição: "held-out" não é o arquivo chamado dev — é o arquivo que o treino não olhou.**
- 🥇 **O ACHADO: em `herdeiros`, a base SEM fine-tune bate o campeão.**
  `herdeiros.f1_entidade` (n=130): v2.1 **27,92%** · especialista **36,44%** · **Qwen2.5-7B
  base, cru, 40,01%** — base×v2.1 = **+12,09pp, IC [+3,42, +20,82] (exclui o zero)**.
  Confirma com número o diagnóstico de 31/07: com **73-76% do gold vazio**, o SFT
  **ensinou o modelo a não emitir herdeiros**. O ganho do especialista é recuperação
  parcial de dano próprio — e nem alcança a base. **Conserto = rótulo (o gold v2.2 já
  existe no NAS), não adapter.** O especialista ainda PIORA `falecido` (−1,88pp, IC
  [−3,77, −0,21]).
- 📉 **A amostra pequena mentiu de novo — 6ª vez.** Com o controle em 200 exemplos o gap
  do achado parecia **+18,4pp** (n=30) e o `falecido` da base parecia 82,98%. Ampliado
  para os 539 do slice: `falecido` da base caiu para **61,92%** e o gap desceu para
  **+12,09pp** (n=130). O sinal sobreviveu, **menor**. Causa do viés: o slice é ordenado
  **por tamanho de prompt** (para o batching), então "os 200 primeiros" são os textos
  mais curtos = amostra enviesada para fácil. **Lição: subconjunto de um arquivo ordenado
  NÃO é amostra aleatória — se for cortar, corte com seed, não com `head`.**
- 🧯 **Guerra de orquestradores, de novo (a lição de 29/07 não estava no código).** Um
  comando de lançamento que falhou no meio ainda subiu o driver em background: **dois
  drivers no mesmo alvo**, escrevendo no MESMO `preds_*.jsonl` e ocupando 2× a VRAM.
  Pego em ~2 min pela conferência de `nvidia-smi --query-compute-apps`. Fix durável:
  **`flock` no driver** (1 dono por gate). **Lição: "1 dono por treino" só vale quando é
  o programa que garante, não o operador.**
- 🐛 **Bug meu na régua, achado antes de virar veredito:** o controle (base sem adapter,
  200 exemplos) entrava na interseção do pareamento e **encolhia a comparação A×B de 707
  para 138 exemplos**. Corrigido para o controle tomar subconjunto próprio; re-pontuar
  não custou GPU porque **geração e pontuação são scripts separados** — decisão de design
  que se pagou no mesmo dia.
- 🧱 **Barra de controle absoluta não serve para tarefa de teto.** C2 exigia base ≥20pp
  abaixo do v2.1; passou em 5 de 7 e falhou em `cessao` (+15,35) e `decisao` (+11,83) —
  onde o v2.1 está em 98,3% e 99,5% e a base crua já faz 83% e 88%: **não existem 20pp
  para ganhar**. A barra **não foi movida** (os dois vereditos ficam com a ressalva); a
  correção — medir controle em **redução de erro**, não em pontos absolutos — fica
  pré-registrada para o próximo gate.
- 💰 **Custo real ≠ custo estimado:** a varredura foi orçada em 19h e saiu em **~3h20**
  (**1,7 s/exemplo**, não 4,6). Foi essa medição, feita na primeira tarefa, que justificou
  gatear os 7 em vez de parar no primeiro BLOCK: a economia que justificava parar não
  existia. **Lição: reorçar com a vazão medida antes de decidir cortar escopo.**
- ✅ **Consequência de produto:** o v2.1 segue campeão **sozinho**; o roteamento de
  adapter por `doc_classe` no serving **não precisa ser construído** — o BLOCK compra essa
  simplicidade. A 3090 está **livre** e pode voltar a servir a showcase.

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
