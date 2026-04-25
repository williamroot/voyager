# Voyager

Sistema de ingestão e consulta do **Diário de Justiça Eletrônico Nacional (DJEN)** organizado por tribunal. Coleta automaticamente todas as movimentações publicadas, normaliza por processo, enriquece com dados da consulta pública dos tribunais (partes, advogados, classe, valor da causa), e expõe tudo via API REST + dashboard mission-control.

> **Conceito:** *Deep Data Network — Mission Control.* Cada tribunal é uma "probe" transmitindo telemetria; o dashboard é a estação de controle.

## Status atual

- **Tribunais ativos:** TRF1 e TRF3 (TRF2/4/5/6/TJSP cadastrados, prontos pra ligar)
- **Cobertura:** desde **dez/2020** (TRF1) e **jan/2021** (TRF3) até hoje
- **Volume típico:** ~1.2M movimentações, ~900k processos, ~9 GB Postgres com a cobertura atual

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
│                 · Parte · ProcessoParte
├── djen/         cliente HTTP DJEN, pool de proxies, parser, ingestion, jobs RQ,
│                 scheduler (cancel-and-recreate idempotente), management commands
├── enrichers/    Consulta pública dos tribunais (TRF1 implementado), parser BS4
│                 de partes/advogados/metadata, jobs RQ
├── api/          DRF viewsets, filtros, paginação cursor, API key auth
└── dashboard/    Templates HTMX + ECharts + tema dark/light com tokens CSS
                  + identidade visual Voyager (mission patch, telemetry,
                  pulsar bullets, star-field, scanlines)
```

Detalhes técnicos em [`.ia/`](.ia/) — visão geral, decisões, runbook, padrões.

## Como subir

```bash
git clone git@github.com:williamroot/voyager.git
cd voyager
cp .env.example .env
# edite PROXYSCRAPE_API_KEY e CORTEX_PROXY_URL
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Subindo o backfill de TRF1+TRF3:

```bash
docker compose exec web python manage.py djen_descobrir_inicio TRF1
docker compose exec web python manage.py djen_descobrir_inicio TRF3
docker compose exec web python manage.py djen_backfill TRF1
docker compose exec web python manage.py djen_backfill TRF3
```

Acompanhando:

```bash
docker compose exec web python manage.py djen_status
docker compose logs -f worker_ingestion
```

## Endpoints

| Path | Descrição |
|------|-----------|
| `/dashboard/` | Visão geral, charts, KPIs |
| `/dashboard/processos/` | Lista de processos com filtros |
| `/dashboard/processos/<id>/` | Detalhe + timeline + partes (botão "Atualizar dados públicos" no TRF1) |
| `/dashboard/movimentacoes/` | Busca textual + filtros chips |
| `/dashboard/partes/` | Partes/advogados/empresas |
| `/dashboard/ingestao/` | Saúde operacional, drift alerts, runs |
| `/api/v1/...` | REST API (HasAPIKey) — Swagger em `/api/v1/docs/` |
| `/admin/` | Django Admin |
| `/django-rq/` | Filas RQ |
| `/metrics` | Prometheus (restrito por IP no nginx) |
| `/api/v1/health/liveness/` | Liveness probe simples |
| `/api/v1/health/` | Readiness rico (lag por tribunal, drift, filas) |

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
