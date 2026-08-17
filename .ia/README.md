# Documentação técnica do Voyager

Esta pasta concentra a documentação **destilada** do projeto — o que um engenheiro novo precisa saber pra ser produtivo em algumas horas, sem ler 100k linhas de código.

## Índice

| Documento | Quando ler |
|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | Sempre: visão de alto nível, motivação, escopo, terminologia |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Antes de modificar a estrutura geral, adicionar app, mexer em fluxos |
| [`DATA_MODEL.md`](DATA_MODEL.md) | Antes de criar/alterar models ou migrations |
| [`INGESTION.md`](INGESTION.md) | Antes de mexer em DJEN, jobs, scheduler, proxies |
| [`ACERVO_CNJ.md`](ACERVO_CNJ.md) | **Completude do acervo**: por que só tínhamos 13% do país, varredura do Datajud (esqueleto nacional) e hidratação |
| [`DIARIOS.md`](DIARIOS.md) | **Terceira porta**: diários próprios (DJE/TJSP, DEJT, STF, DOEs de entes) — o que cada fonte dá e o que NÃO dá, contrato do coletor, dedupe entre portas, **kill switch** e runbook de ligar em produção |
| [`ENRICHMENT.md`](ENRICHMENT.md) | Antes de adicionar enricher de tribunal novo (PJe ou outro sistema) |
| [`CLASSIFICACAO.md`](CLASSIFICACAO.md) | Antes de mexer em classificação ML de leads, modelo, features, API Juriscope |
| [`EXTRACAO_ROADMAP.md`](EXTRACAO_ROADMAP.md) | Plano faseado de qualidade+throughput da extração (multi-GPU, sub-janela, join, gestor orientado a eventos) |
| [`EXTRACAO_DO_ACERVO.md`](EXTRACAO_DO_ACERVO.md) | Rodar a extração a partir do acervo vetorizado (por CNJ, sem arquivo) |
| [`MODELOS.md`](MODELOS.md) | Registry + model cards dos modelos de extração (versões, hashes, gates) — atualizar a cada treino/promoção |
| [`EXPERIMENTOS_MODELO.md`](EXPERIMENTOS_MODELO.md) | Os 4 experimentos em curso do extrator (DAPT, especialistas, DPO-κ, destilação 3B): hipótese, setup, critério pré-registrado, status |
| [`FICHA_PARTE.md`](FICHA_PARTE.md) | Extração entity-centric v2: ficha por parte (valores/pagamentos) + camada jurimetria |
| [`TREINAMENTOS.md`](TREINAMENTOS.md) | **Ponto de entrada único dos treinos** — o que treinamos e por quê, linha do tempo dos modelos, experimentos, harness/hiperparâmetros, onde roda (GPUs/pods), estado atual, como retomar. Consolida MODELOS+EXPERIMENTOS+LABLOG. |
| [`GPU_MACOS.md`](GPU_MACOS.md) | Nó GPU Apple Silicon (Mac mini M4) servindo os GGUF do extrator via Metal — desempenho medido, serving, LaunchDaemons, gotchas do macOS |
| [`LABLOG.md`](LABLOG.md) | Diário de bordo dos experimentos de ML — cronológico, avanços E incidentes com lição; atualizar a cada ciclo |
| [`ANALISTA.md`](ANALISTA.md) | Missão Analista: substituir o GPT do JuriscopeIA (colheita, shadow, bake-off de professores, Analista-7B) — **repo deles é read-only absoluto** |
| [`JURIMETRIA.md`](JURIMETRIA.md) | Plano/arquitetura de jurimetria (tracks processual, resultado, precatório) + estado dos dados |
| [`ACCOUNTS.md`](ACCOUNTS.md) | Antes de mexer em convites, sistema de cadastro, captura de IP |
| [`DASHBOARD.md`](DASHBOARD.md) | Antes de criar páginas, alterar tema, adicionar componentes |
| [`OVERVIEW_MAPA.md`](OVERVIEW_MAPA.md) | Módulo **Overview** (ex-"comercial", renomeado em 13/08/2026): mapa de precatórios, página por estado, entidades ("quem deve") |
| [`SEARCH_SCHEMA.md`](SEARCH_SCHEMA.md) | Schema canônico dos índices ES (processos/movimentações), write-through, plano de reindex — **antes de adicionar campo em doc builder** |
| [`API.md`](API.md) | Antes de criar/alterar endpoints REST |
| [`PATTERNS.md`](PATTERNS.md) | Sempre: padrões de código, anti-padrões, decisões idiomáticas |
| [`OPS.md`](OPS.md) | Quando rodar/operar o sistema, runbooks, troubleshooting |
| [`DEPLOY.md`](DEPLOY.md) | Antes de deployar em prod — passo a passo destilado (hot / rebuild web / rebuild full) + prompt pronto |
| [`DECISIONS.md`](DECISIONS.md) | ADRs — por que escolhemos cada caminho |
| [`ROADMAP.md`](ROADMAP.md) | O que está pendente, escopo futuro |

## Como atualizar

Estes arquivos são **fonte de verdade técnica**. Quando você muda código que afeta um deles, **atualize na mesma PR**. Não deixe pra depois.

Manter em pt-BR. Direto e curto. Exemplos > prosa. Diagramas em ASCII art.
