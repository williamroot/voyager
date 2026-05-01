# Roadmap

Itens pendentes ou planejados, organizados por prioridade.

## Concluído (recentes)

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

- [ ] **Enrichers TRF2/5/6** — todos PJe, herdam `BasePjeEnricher` (15 linhas cada)
- [ ] **Enricher TRF4** — eproc, parser próprio
- [ ] **Enricher TJSP** — e-SAJ (sistema diferente, parser próprio)
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

## Classificação de leads (ML)

Sistema operando em produção (v5, AUC 0.95). Ver [`CLASSIFICACAO.md`](CLASSIFICACAO.md) pra detalhes técnicos.

### Curto prazo
- [ ] Drenar batch inicial — `reclassificar_recentes` em curso (~2.4M procs, ETA dias)
- [ ] **Validar precision real em produção** — esperar Juriscope marcar `POST /leads/consumed/` por algumas semanas, ver calibration plot na `/dashboard/leads/`
- [ ] **Ground truth TRF3** — já temos 347 (amostra) → pedir lista maior pra re-treinar v6 multi-tribunal

### Médio prazo
- [ ] **Adaptação justiça estadual (TJMG/TJSP)** — patch já aplicado: `datajud.sync_processo` popula `Process.classe_codigo` quando vazio. POC TJSP detectou 3 leads em 100 procs. Pra produção: enfileirar Datajud em massa pros tribunais novos
- [ ] **PSI / drift score** em tempo real — alerta quando distribuição de scores muda significativamente vs treino
- [ ] **Heatmap tribunal × ano CNJ** — descobrir gap de captura
- [ ] **Hot reload de pesos** — workers leem `ClassificadorVersao.ativa` periodicamente, sem precisar restart pra novo modelo
- [ ] **Webhook outbound** — Voyager notifica Juriscope quando novo lead high-confidence aparece (em vez de polling)

### Longo prazo
- [ ] **Texto dos autos via Juriscope** — features F19/F20 hoje deram peso ~zero porque os termos `'precatório expedido'`/`'rpv expedida'` vivem nos autos completos. Integrar texto dos autos baixados aumentaria precision
- [ ] **Modelo por tribunal** — treinar v6.1 só TRF3, v6.2 só TJMG, etc. Talvez melhor que multi-tribunal único
- [ ] **Active learning** — Juriscope marca FPs (precision real baixa em algum bucket) → aplicar ao re-treino próximo

## Não-objetivos (declarados fora de escopo)

- Login em PJe pra baixar autos (PDFs)
- Multi-tenancy / múltiplas organizações
- Filtragem por termos na ingestão
- Frontend SPA externo
- Mobile app nativo
