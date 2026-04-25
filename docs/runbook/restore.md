# Runbook — Restore Postgres

## Backup
`pg_dump` diário (a fazer — agendar via `default` queue).

## Restaurar

```bash
# 1) Parar serviços que escrevem
docker compose stop web worker_ingestion worker_default scheduler

# 2) Restaurar dump
docker compose exec -T postgres psql -U voyager -d voyager < /backups/voyager-YYYYMMDD.sql

# 3) Subir
docker compose start web worker_ingestion worker_default scheduler

# 4) Validar
docker compose exec web python manage.py djen_status
curl -fsS http://localhost/api/v1/health/
```
