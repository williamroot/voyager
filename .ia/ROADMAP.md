# Roadmap

Itens pendentes ou planejados, organizados por prioridade.

## Concluído (recentes)

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

## Não-objetivos (declarados fora de escopo)

- Login em PJe pra baixar autos (PDFs)
- Multi-tenancy / múltiplas organizações
- Filtragem por termos na ingestão
- Frontend SPA externo
- Mobile app nativo
