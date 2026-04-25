# Runbook — Proxies

## Arquitetura
- **ProxyScrape** (rotativo, residencial) — pool primário.
  - Lista atualizada a cada 15 min via `refresh_proxy_pool` na fila `default`.
  - Pool armazenado em Redis em `voyager:proxies:scrape:list`.
  - Proxies marcados ruins ficam em `voyager:proxies:scrape:bad:<url>` com TTL `PROXY_BAD_TTL_SECONDS` (default 600s).
- **Cortex** (residencial fixo) — fallback acionado quando o pool fica vazio.
  - Configurável via `CORTEX_PROXY_URL` + `CORTEX_FALLBACK_ENABLED`.

## Diagnosticar

```bash
docker compose exec web python manage.py djen_status
# vai mostrar:
#   ProxyScrape: total=N bad=B saudaveis=S
```

Ou no dashboard `/dashboard/ingestao/`.

Verificar Redis direto:
```bash
docker compose exec redis redis-cli
> GET voyager:proxies:scrape:list
> KEYS voyager:proxies:scrape:bad:*
```

## Forçar refresh

```bash
docker compose exec web python manage.py shell -c "from djen.proxies import ProxyScrapePool; print(ProxyScrapePool.singleton().refresh())"
```

## Desabilitar Cortex (caso de incidente)

`.env`:
```
CORTEX_FALLBACK_ENABLED=false
```
Reiniciar workers: `docker compose restart worker_ingestion`.
