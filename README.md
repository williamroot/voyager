# Voyager

Sistema de ingestão e consulta do **Diário de Justiça Eletrônico Nacional (DJEN)** organizado por tribunal. Coleta automaticamente todas as movimentações publicadas, normaliza por processo, enriquece com dados da consulta pública dos tribunais (partes, advogados, classe, valor da causa), e expõe tudo via API REST + dashboard mission-control.

> **Conceito:** *Deep Data Network — Mission Control.* Cada tribunal é uma "probe" transmitindo telemetria; o dashboard é a estação de controle.

## Status atual

- **Tribunais ativos:** TRF1 e TRF3 com enricher PJe (TRF2/4/5/6/TJSP cadastrados, prontos pra ligar)
- **Cobertura:** desde **dez/2020** (TRF1) e **jan/2021** (TRF3) até hoje
- **Volume típico:** ~1.2M movimentações, ~900k processos, ~10 GB Postgres
- **Em prod:** https://voyager.was.dev.br via Cloudflare Tunnel
- **Auto-heal:** watchdog a cada 5min mata zumbis e re-enfileira backfill/daily se sumiu da fila

## Stack

```
Python 3.12 · Django 5 · DRF · django-rq + rq-scheduler
Postgres 16 (pg_trgm + unaccent + tsvector + triggers)
Redis 7 (AOF + noeviction)
HTMX + Alpine.js + Apache ECharts + Tailwind (CDN)
Gunicorn + Nginx (resolver dinâmico)
ProxyScrape (rotativo) + Cortex (residencial fixo) — fallback inteligente
Sentry · Prometheus · structlog
```

## Arquitetura

```
voyager/
├── core/         settings.py único + urls + middleware (RequestId, Prometheus)
├── tribunals/    Tribunal · Process · Movimentacao · IngestionRun · SchemaDriftAlert
│                 · Parte · ProcessoParte · ClasseJudicial · Assunto
├── djen/         cliente HTTP DJEN, pool de proxies, parser, ingestion, jobs RQ,
│                 scheduler (cancel-and-recreate idempotente), watchdog de auto-heal,
│                 management commands
├── enrichers/    BasePjeEnricher genérico + Trf1Enricher / Trf3Enricher (subclasses
│                 de ~15 linhas), parser BS4 de partes com handling de doc mascarado,
│                 jobs RQ com filas per-tribunal
├── accounts/     Sistema de convites (Invite) com captura IP/UA/classificação
│                 ip-api.com, signup público em /invite/<token>/
├── api/          DRF viewsets, filtros, paginação cursor, API key auth
└── dashboard/    Templates HTMX + ECharts + tema dark/light com tokens CSS,
                  + páginas /workers/ /tribunais/ /invites/, identidade visual
                  Voyager (mission patch, telemetry, pulsar bullets, star-field)
```

Detalhes técnicos em [`.ia/`](.ia/) — visão geral, decisões, runbook, padrões.

## Como subir (dev)

```bash
git clone git@github.com:williamroot/voyager.git
cd voyager
cp .env.example .env
# edite PROXYSCRAPE_API_KEY e CORTEX_PROXY_URL
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Subindo o backfill de TRF1+TRF3 (o watchdog faz isso sozinho a cada 5min, mas pode disparar manualmente):

```bash
docker compose exec web python manage.py djen_descobrir_inicio TRF1
docker compose exec web python manage.py djen_descobrir_inicio TRF3
docker compose exec web python manage.py djen_backfill TRF1
docker compose exec web python manage.py djen_backfill TRF3
```

Bulk enqueue de enriquecimento:
```bash
docker compose exec web python manage.py enriquecer_pendentes --tribunal TRF3 --limit 0
```

Acompanhando: `/dashboard/workers/` (auto-refresh) ou `docker compose logs -f worker_trf1`.

## Deploy em prod

Stack separada em `docker-compose-prod.yml` com tuning de Postgres, gunicorn, Cloudflare Tunnel e workers per-tribunal (4×TRF1 + 4×TRF3 + 2×ingestion + scheduler + watchdog).

```bash
ssh ubuntu@<server>
cd ~/voyager
git pull --ff-only
docker compose -f docker-compose-prod.yml build web
docker compose -f docker-compose-prod.yml up -d
```

`web` aplica migrações e `collectstatic` no entrypoint. Domínio público via Cloudflare Tunnel — `CLOUDFLARE_TUNNEL_TOKEN` no `.env` do servidor. Detalhes em [`.ia/OPS.md`](.ia/OPS.md).

## Endpoints

| Path | Descrição |
|------|-----------|
| `/dashboard/` | Visão geral, charts, KPIs |
| `/dashboard/tribunais/` | Cards por tribunal (processos, movs, cobertura, status enriquecimento) |
| `/dashboard/processos/` | Lista de processos com filtros |
| `/dashboard/processos/<id>/` | Detalhe + timeline + partes + botão "Atualizar dados públicos" |
| `/dashboard/movimentacoes/` | Busca textual + filtros chips |
| `/dashboard/partes/` | Partes/advogados/empresas |
| `/dashboard/partes/<id>/` | Perfil + 3 charts (tribunal/papel/polo) + lista filtrada |
| `/dashboard/ingestao/` | Saúde operacional, drift alerts, runs |
| `/dashboard/workers/` | Filas RQ + workers conectados, auto-refresh 5s |
| `/dashboard/invites/` | (superuser) Gerar/revogar convites de cadastro |
| `/invite/<token>/` | (público) Aceitar convite, criar conta |
| `/api/v1/...` | REST API (HasAPIKey) — Swagger em `/api/v1/docs/` |
| `/admin/` | Django Admin |
| `/django-rq/` | Filas RQ |
| `/metrics` | Prometheus (restrito por IP no nginx) |
| `/api/v1/health/liveness/` | Liveness probe simples |
| `/api/v1/health/` | Readiness rico (lag por tribunal, drift, filas) |

## Convites de acesso

Sem auto-cadastro público. Superuser gera link em `/dashboard/invites/`:
- Token uso único, validade 7 dias
- Convidado escolhe username/senha em `/invite/<token>/`
- Captura IP + User-Agent + classificação ip-api.com (país, ISP, mobile/hosting/proxy)

Documentação: [`.ia/ACCOUNTS.md`](.ia/ACCOUNTS.md).

## Testes

```bash
docker compose exec web pytest
```

## Convenções

- **PEP 8 estrito**, imports sempre no topo, ordem stdlib → 3rd → local
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`)
- **Mensagens em pt-BR**, imperativo, presente
- Padrões detalhados em [`docs/code-guidelines.md`](docs/code-guidelines.md) e [`.ia/PATTERNS.md`](.ia/PATTERNS.md)

## Licença

Proprietária — uso interno.

---

[Spec original do projeto](docs/superpowers/specs/2026-04-24-voyager-djen-ingestion-design.md) · [Guidelines de código](docs/code-guidelines.md) · [Documentação técnica `.ia/`](.ia/)
