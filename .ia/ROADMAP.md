# Roadmap

Itens pendentes ou planejados, organizados por prioridade.

## Alta — desbloqueiam casos de uso

- [ ] **Enricher TRF3** (PJe v2) — copy/adapt do `enrichers/trf1.py`
- [ ] **Backfill de partes em batch** — `enriquecer_lote --tribunal TRF1 --limit N` enfileirando jobs
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

- [ ] **Enrichers TRF2/4/5/6** — todos PJe, mesmo padrão TRF1
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
