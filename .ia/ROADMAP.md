# Roadmap

Itens pendentes ou planejados, organizados por prioridade.

## Concluído (recentes)

- [x] **Deploy do sistema de validação humana + v7-ready (commit 4c25b7c, 2026-05-14)** — 4 migrations aplicadas em prod (.32), 5 modelos novos, hot reload validado (`hardcoded → v6`), 274 containers vivos entre .32 e .36, 4 grupos de permissão criados via `setup_validacao_groups`. `VALIDACAO_LOTES_SEMANAIS_ENABLED=False` pré-flip (aguardando autorização biz). Procedimento de flip v7 em [`V7_DEPLOY_DECISION.md`](V7_DEPLOY_DECISION.md).
- [x] **Classificador v6** (TRF1 1.05M procs, AUC 0.9610, prec@5000 0.991) ativo desde commit 6cdfff6 (2026-05-08)
- [x] **Sistema de validação humana** end-to-end — `AmostraValidacao` + `AmostraProcesso` + `ProcessoValidacao` + permissions custom, fila 1-por-vez com hotkeys, dupla-anotação 10% + kappa (LGPD/anonimização: fora de escopo nesta versão)
- [x] **Mining de FN candidates** — 6 estratégias E1-E6 + composite suspeita_score, command `minerar_fn`, cron semanal `gerar_lotes_semanais_fn`
- [x] **Hot reload de pesos** — TTL 60s, thread-safe, fallback hardcoded, sem restart de worker
- [x] **Shadow mode** — `ClassificadorVersao.shadow=True` + `ClassificacaoShadowLog` + cron `comparar_shadow_daily` (04:00)
- [x] **Categorização DB-driven** — `ThresholdTribunal` por (tribunal × versao_modelo), compartilhada entre path ativo e shadow
- [x] **Dashboard de visibilidade** — `/dashboard/leads/visibilidade/` com 8 KPIs + 5 charts + heatmap + widget shadow
- [x] **Dashboard de validação** — `/dashboard/leads/validacao/*` (overview + fila + concluído)
- [x] **API stats por tribunal** + calibração por tribunal (drift detection)
- [x] **Heatmap tribunal × ano CNJ** (na tela de visibilidade)
- [x] **Sistema de classificação ML de leads** — Logistic Regression v5, AUC 0.95, precision@5k 93.9%. Pipeline end-to-end (DJEN/Datajud → classifier → API → Juriscope). Detalhe: [`CLASSIFICACAO.md`](CLASSIFICACAO.md)
- [x] **API REST `/api/v1/leads/`** — auth via X-API-Key. GET (lista pendentes), POST consumed, GET stats
- [x] **Tela `/dashboard/leads/`** — KPIs lazy + 5 charts ECharts + tabela paginada + export CSV + chips de filtros
- [x] **Tela `/dashboard/api/`** — docs interativas dos endpoints + métricas do modelo + clientes ativos
- [x] **Tela `/dashboard/consulta-rapida/`** — debug em tempo real DJEN+Datajud sem persistir
- [x] **Card explainability no detalhe do processo** — top features com emoji + tooltip + descrição completa, colapsável
- [x] **Patch `datajud.sync_processo` popula `Process.classe_codigo`** — corrige caso TJMG/TJSP onde nem DJEN nem PJe enricher populavam
- [x] **Detecção página de indisponibilidade TRF1** — markers novos no `_PJE_ERROR_MARKERS` (sleep 30s automático)
- [x] **Fila dedicada `classificacao`** + paralelização batch (`reclassificar_recentes(paralelizar=True)`)
- [x] **Enricher TRF3** via `BasePjeEnricher` (refatorado em `enrichers/pje.py`)
- [x] **Catálogo nacional ClasseJudicial / Assunto** (TPU/CNJ) com FKs em Process/Movimentacao
- [x] **Filas per-tribunal** (`enrich_trf1`, `enrich_trf3`) com 4 workers cada
- [x] **Sistema de convites** (`accounts/`) com captura IP+UA+ip-api.com
- [x] **Watchdog de ingestão** — auto-heal de zumbis e re-enqueue de backfill/daily a cada 5min
- [x] **Deploy em prod** via Cloudflare Tunnel (voyager.was.dev.br)
- [x] **Página /dashboard/workers/** com queue/worker state e auto-refresh HTMX
- [x] **Página /dashboard/tribunais/** com KPIs por tribunal
- [x] **Dedupe mascarado→real** de partes (`real_casa_com_mascara`) + command `consolidar_partes_mascaradas`
- [x] **`enriquecer_pendentes`** — bulk enqueue por tribunal/status
- [x] **Race fix em enricher** — `Process.objects.select_for_update()` no atomic block

## Alta — desbloqueiam casos de uso

- [ ] **Busca POR PARTE ao vivo — deploy e as duas medições que faltam.** Código
      pronto e testado (`enrichers/busca/`, `POST /api/v1/busca/tribunal/`,
      tela `/dashboard/busca-tribunal/`, worker `worker_busca`, migration 0060).
      Falta: (1) deploy + `docker compose -f docker-compose-workers.yml up -d
      worker_busca`; (2) medir o **TRF3** de dentro do container — o host recusa
      conexão fora da malha de proxies (`manage.py busca_parte TRF3 nome "..."`);
      (3) medir **`oab` no TJPA**, que precisa de uma OAB real do PA (a fonte não
      expõe OAB nas partes, e a que testei devolveu 204). Feitas as medições,
      mover os critérios para `criterios_medidos` em `enrichers/busca/registry.py`.
      Do outro lado, o Juriscope troca a busca local dele por esta API.

- [ ] **Backfill TRF1+TRF3 100% até hoje** — em curso (parado em 18/10/2024 — re-disparado pelo watchdog)
- [ ] **`pg_dump` diário automático** — job RQ na fila `default` ~03:00 + retenção 30d local + S3 opcional
- [ ] **2FA no admin** via `django-otp` — pra exposição pública

## Média — qualidade e observabilidade

- [ ] **Materialized views** pro dashboard (`mv_movs_por_dia_tribunal`, etc.) — refresh via job a cada 15min. Reduz tempo de carregamento dos charts.
- [ ] **Notificações Slack** — drift alert + run failed (já tem ENV vars, falta implementar `notifier.py`)
- [ ] **Métricas Prometheus customizadas** — `voyager_djen_pages_total`, `voyager_djen_movimentacoes_inseridas_total`, `voyager_proxy_pool_healthy`
- [ ] **Particionamento `tribunals_movimentacao`** por mês quando passar de ~50M rows
- [ ] **CI** com GitHub Actions: lint (ruff), pytest com coverage 80%+, build de imagem multi-stage com cache GHCR
- [ ] **Sentry tags** customizadas (`tribunal`, `job_kind`) — já tem SDK, precisa integrar

## Baixa — refinamentos

- [x] **Enricher TRF5** (2026-05-20) — herda `BasePjeEnricher` com path `/pjeconsulta/` e UA Firefox (Akamai gate).
- [ ] **Enrichers TRF2/4/6** — todos E-PROC (requer login + 2FA + proxy). Não cabem em `BasePjeEnricher`. Caminhos: (a) port do parser autenticado do JURISCOPE (`falcon/datamodel/processors/trf2.py` como referência), (b) consulta pública com solver de captcha. **DJEN+Datajud dos 3 já ativos** desde 2026-05-24.
- [x] **Enricher TJSP** (2026-05-24) — `enrichers/esaj.py` (classe própria). HTTP puro (sem Selenium): `open.do` → `search.do?NUMPROC` (302) → `show.do`. Selectors portados de `ESAJSPProcessDataProcessor` do JURISCOPE. Doc/CPF mascarados pelo e-SAJ público — preserva nome + OAB.
- [ ] **Cobertura completa de TJs com PJe** — meta de longo prazo: integrar todos os tribunais estaduais que usam PJe. Lista pendente (17, snapshot CNJ 2026): TJAC, TJAM, TJAP, TJBA, TJCE, TJES, TJMT, TJPA, TJPB, TJPE, TJPI, TJRJ, TJRN, TJRO, TJRR, TJSE, TJTO. Antes de cada novo: probar URL pública — se SPA Angular (caso TJDFT), implementar como classe própria nos moldes de `enrichers/tjdft.py`; se PJe clássico JSF/Seam, ~15 linhas de subclasse de `BasePjeEnricher` (passos em `.ia/ENRICHMENT.md`). Fora desta lista (estaduais não-PJe): TJAL/TJMS (e-SAJ), TJGO/TJPR (PROJUDI), TJRS/TJSC (eproc).
- [x] **Enricher TJDFT** (2026-05-26) — `enrichers/tjdft.py` (classe própria). PJe migrou pra SPA Angular + REST API (`pje-consultapublica-api.tjdft.jus.br/v1/`). 4 chamadas JSON por proc: lista→idProcesso, /dados, poloAtivo, poloPassivo, outrosInteressados. Documentos sem máscara; `valor_causa` não disponível na API pública. Validado E2E contra CNJ real (45 ProcessoParte criados, FKs `representa` corretas).
- [ ] **Webhooks** — clientes registram CNJs ou termos e recebem callback HTTP em movimentações novas
- [ ] **Export CSV** — botão na busca de movimentações + endpoint API. Job na fila `default`, link em `/dashboard/exports/`
- [ ] **Saved filters** no dashboard (favoritos do usuário)
- [ ] **Heatmap calendar (último ano)** no overview — densidade diária, gaps em vermelho
- [ ] **Banner de "transmission delay"** quando filas RQ acumulam muito
- [ ] **Easter eggs** — Konami code, splash screen com animação Voyager
- [ ] **Dark/light auto** — sync com `prefers-color-scheme` em mudança ao vivo (já é initial; falta listener)

## Tecnical debt

- [ ] **Migrar de CDN pra Vite bundle** — Tailwind/HTMX/Alpine/ECharts hoje vêm de CDN. Pra ambientes sem internet, precisa bundlar local. Stage `frontend` no Dockerfile já existe (removido temporariamente), basta restaurar.
- [ ] **Volumes nomeados pro static** em prod — montar volume `static` em `/app/staticfiles` no `web`, voltar `nginx` pra `alias /var/www/static/` (em vez de proxy_pass)
- [ ] **`SECRET_KEY` em vault** (Doppler/Vault) em vez de `.env`
- [ ] **Tests** — atualmente só `tests/test_parser.py` e `tests/test_ingestion_chunks.py`. Faltam: ingestion integration, api endpoints, dashboard views, enricher TRF1 com `responses` mockado.
- [ ] **Passada de a11y: contraste WCAG AA nos resíduos de cor de marca** — decisão do dono (2026-08-12): passada dedicada depois, **não corrigir avulso**. Os 22 pontos de `text-mission` em texto pequeno já foram curados no commit da auditoria de UX (→ `text-mission-fg`, 3,56:1 → 5,18:1); sobrou o abaixo. Números **já medidos** (WCAG 2.x, card claro = branco, card escuro = `rgb(17,17,18)`) — quem pegar **não precisa remedir**. Régua: 4,5:1 pra texto <24px, 3:1 pra texto grande/UI.
  - **`text-pulsar` em texto corrido — 5 usos, 3,77:1 no claro.** `dashboard/templates/dashboard/mcp_setup.html:189-192` (4 `<code>` de resource URI, wrapper `text-xs`) + `dashboard/templates/dashboard/leads/visibilidade.html:86` (link dentro de `<p>`). Cura = trocar por **`text-pulsar-fg`** → **5,48:1**. O token **já está registrado** no `tailwind.config` e no `--c-pulsar-fg` (claro 4 120 87 / escuro 134 239 172, 13,44:1) — é troca de 1 palavra por linha, zero CSS novo.
  - **`.kpi-tile--fn .kpi-tile-value` com `--c-golden` — 2,94:1 no claro.** `dashboard/static/dashboard/voyager-identity.css:1644`. Texto de 1,55rem/600 = texto grande, régua 3:1 → **falha até a régua frouxa** (yellow-600 sobre branco). Cura provável = criar o par **`golden-fg`** (ex.: yellow-700 `161 98 7` = 4,92:1 no claro, mas **só 3,83:1 no card escuro** — precisa de valor por tema, não serve o mesmo nos dois). Hoje **não existe nenhum uso de `text-golden`/`bg-golden` como utility em template** — o único consumidor é esta regra CSS, então dá pra curar sem tocar em template.
  - **Chips de tag "Extração" ficaram a 4,47:1 (faltam 0,03).** `dashboard/templates/dashboard/_partials/_command_data.html:233` e `_vetorizacao_data.html:160`. O texto já foi pra `text-mission-fg`, mas o fundo `bg-mission/12` escurece o cálculo: 3,07 → **4,47:1** (o valor sobre branco puro seria 5,18). Cura de 1 caractere = **`bg-mission/12` → `bg-mission/10`** (**4,58:1**) ou `/08` (4,71:1). Não fiz porque a autorização era só do `text-*`. No escuro está folgado (9,89:1).
  - **`text-mission` em chip 11px e em chips de filtro Alpine — 2,96:1 no claro** (texto de marca sobre `bg-mission/15`). `dashboard/templates/dashboard/vetorizacao.html:139` (`text-[11px] font-medium text-mission`), `_partials/_command_data.html:158` e `_partials/_vetorizacao_data.html:79` (estado ativo `:class="… 'bg-mission/15 text-mission'"`). Nos 2 chips de filtro o **gêmeo da linha de cima já usa `text-accent-fg`** (`bg-accent-fg/15 text-accent-fg`) — a assimetria é só porque `mission-fg` não existia. Cura = `text-mission-fg` + baixar o tint pra `/10`.
  - Fora de escopo desta dívida (medidos e **OK**, não mexer): ícone `voy_icon … text-mission` (3,56:1 ≥ 3:1 de non-text), `kpi-num text-mission` em `text-xl/2xl/3xl` semibold (texto grande, 3:1), `hover:`/`focus:text-mission`, bullets `◉` decorativos do `mcp_setup.html`, e `pale-blue` no claro (5,17:1, passa AA).

## Classificação de leads (ML)

Sistema operando em produção (v5, AUC 0.95). Ver [`CLASSIFICACAO.md`](CLASSIFICACAO.md) pra detalhes técnicos.

### Curto prazo
- [ ] Drenar batch inicial — `reclassificar_recentes` em curso (~2.4M procs, ETA dias)
- [ ] **Validar precision real em produção** — esperar Juriscope marcar `POST /leads/consumed/` por algumas semanas, ver calibration plot na `/dashboard/leads/`
- [ ] **Ground truth TRF3** — já temos 347 (amostra) → pedir lista maior pra re-treinar v6 multi-tribunal

### Médio prazo
- [ ] **Adaptação justiça estadual (TJMG/TJSP)** — patch já aplicado: `datajud.sync_processo` popula `Process.classe_codigo` quando vazio. POC TJSP detectou 3 leads em 100 procs. Pra produção: enfileirar Datajud em massa pros tribunais novos
- [ ] **PSI / drift score formal** — shadow mode cobre parte (KS test + agreement), falta métrica PSI canônica
- [ ] **Webhook outbound** — Voyager notifica Juriscope quando novo lead high-confidence aparece (em vez de polling)
- [ ] **Trigger PG UPDATE-block em `ProcessoValidacao`** — hoje imutabilidade é só via `UniqueConstraint`; antes de publish externo do dataset
- [ ] **Focus trap em modais** (modal de criar lote, modal de ajuda) — acessibilidade
- [ ] **CV interno no grid de thresholds v7** — hoje é só holdout único
- [ ] **Numpy no Dockerfile** (não só `requirements.txt`) — estabilidade
- [ ] **Cleanup job de `ClassificacaoShadowLog`** — retention 90 dias
- [ ] **[BIZ] `model_admins ∩ validadores_leads` permitido?** — conflito de interesse documentado em REGRAS_NEGOCIO_VALIDACAO §5
- [ ] **LGPD / anonimização** — fora de escopo nesta versão. Reabrir quando expandir validação além da equipe interna; reativar `usuario_hash` (salt em ENV) + comando `anonimizar_usuario`.

### Longo prazo
- [ ] **Texto dos autos via Juriscope** — features F19/F20 hoje deram peso ~zero porque os termos `'precatório expedido'`/`'rpv expedida'` vivem nos autos completos. Integrar texto dos autos baixados aumentaria precision
- [ ] **Modelo por tribunal** — treinar v6.1 só TRF3, v6.2 só TJMG, etc. Talvez melhor que multi-tribunal único
- [ ] **Active learning** — Juriscope marca FPs (precision real baixa em algum bucket) → aplicar ao re-treino próximo

## Acervo — escala da vetorização / busca vetorial

Gargalo raiz medido (jul/2026): o índice **HNSW do pgvector** na `acervo_chunk` não cabe na RAM → disk-cliff que trava ingest E busca. Teto de escrita medido ~380 procs/h. Plano faseado (parar quando resolver):

- [x] **Fix single-pass** — `search_vector` no INSERT (elimina o double-HNSW-insert do padrão INSERT+UPDATE). **18× mais rápido na escrita** (A/B medido). Novo `acervo/chunk_writer.py`, deployado na frota. Commit `1f0203e`.
- [x] **Autovacuum tunado** na `acervo_chunk` (scale_factor 0.02, cost_limit 1000) — controla o bloat de 25%.
- [x] **RAM do zordon-db 30→94GB** + config (shared_buffers 24GB, effective_cache 70GB) → índice residente.
- [x] **halfvec** (float16) — EM PROD: `chunk_emb_hnsw_half`, busca em `search.py` via `embedding::halfvec(1024)`, fp32 dropado, recall@8 = fp32 (0pp, medido). Índice cresceu p/ **58GB** com o corpus (31M chunks) e **voltou a raspar a RAM (76GB host)** → é o teto atual.
- [x] **quantização binária ingênua** (bit+Hamming+rescore) — **BLOCK** no gate full-corpus (overlap@8 0,49). Ver `QUANTIZACAO_INDICE_VETORIAL.md`.
- [x] **pgvectorscale (StreamingDiskANN + SBQ)** — **FAIL ESTRUTURAL**: SBQ 2-bit impossível >900d (bge-m3=1024d), só 1-bit = regime reprovado. Extensão instalada mas inerte. Ver `PGVECTORSCALE_SBQ.md`.
- [ ] **Próximo degrau é ARQUITETURAL (sai do PG)** — **doc de decisão: `UNLOCK_VETORIAL_ARQUITETURAL.md`**. Recomenda **LanceDB** (embarcado, disk-first, serve só o ANN → RRF/FTS/rerank intactos), precedido de teste barato da Matryoshka (truncar bge-m3 <930d p/ ressuscitar o SBQ — **prior ruim: bge-m3 não tem MRL**). Mesmo gate overlap@8≥0,90 no corpus cheio + cutover reversível por flag.
- [ ] **GPU-CAGRA (NVIDIA cuVS / Milvus-GPU)** — degrau FUTURO/condicional pro **full-corpus (~85M chunks)**, SE o LanceDB-CPU também raspar teto. Custo: 2º store + GPU dedicada (compete c/ embed/extração). PQ já coberto pelo LanceDB sem GPU.
- Pesquisa + trade-offs completos: `UNLOCK_VETORIAL_ARQUITETURAL.md` + memória IA `vetor-ingest-teto-e-opcoes.md`.

## Não-objetivos (declarados fora de escopo)

- Login em PJe pra baixar autos (PDFs)
- Multi-tenancy / múltiplas organizações
- Filtragem por termos na ingestão
- Frontend SPA externo
- Mobile app nativo
