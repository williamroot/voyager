# Voyager

Ingestão completa do **Diário de Justiça Eletrônico Nacional (DJEN)** organizada por tribunal, com armazenamento normalizado a nível de processo + movimentações, busca textual via Postgres, dashboard interno com gráficos e API REST autenticada por API key.

Tribunais ativos no go-live: **TRF1** e **TRF3** (TRF2/4/5/6/TJSP cadastrados como inativos, prontos pra ligar).

## Stack

- Python 3.12 / Django 5 / DRF
- Postgres 16 (`pg_trgm`, `unaccent`)
- Redis 7 (com AOF, `noeviction`)
- django-rq + rq-scheduler (filas: `djen_ingestion`, `djen_backfill`, `default`)
- HTMX + Alpine.js + Apache ECharts + Tailwind CSS (CDN)
- Gunicorn + Nginx
- ProxyScrape (rotativo) + Cortex (fallback) para evitar 429 da DJEN

## Como subir (dev)

```bash
cp .env.example .env
# preencha PROXYSCRAPE_API_KEY (e CORTEX_PROXY_URL se quiser fallback)
docker compose up -d --build
docker compose exec web python manage.py createsuperuser

# descobrir floor histórico de cada tribunal ativo
docker compose exec web python manage.py djen_descobrir_inicio TRF1
docker compose exec web python manage.py djen_descobrir_inicio TRF3

# disparar backfill completo (em background na fila djen_backfill)
docker compose exec web python manage.py djen_backfill TRF1
docker compose exec web python manage.py djen_backfill TRF3
```

- API: `http://localhost/api/v1/` (precisa de `Authorization: Api-Key <chave>`, criada no admin)
- Dashboard: `http://localhost/dashboard/`
- Docs API (Swagger): `http://localhost/api/v1/docs/`
- Admin: `http://localhost/admin/`
- Status operacional CLI: `docker compose exec web python manage.py djen_status`

## Estrutura

```
core/         settings + urls + wsgi/asgi + middleware
tribunals/    Tribunal, Process, Movimentacao, IngestionRun, SchemaDriftAlert
djen/         client, proxies, parser, ingestion, jobs, scheduler, mgmt commands
api/          DRF viewsets + serializers + filters + paginação + API-key
dashboard/    HTMX + ECharts (CDN) + templates
infra/        nginx.conf, pg_init/extensions.sql
docs/         spec + runbook
```

Spec completa: [`docs/superpowers/specs/2026-04-24-voyager-djen-ingestion-design.md`](docs/superpowers/specs/2026-04-24-voyager-djen-ingestion-design.md).

Runbook operacional: [`docs/runbook/`](docs/runbook).
