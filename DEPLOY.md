# Deploy

> **Doc canônica:** ver `.ia/OPS.md` para inventário de hosts, configuração de workers, runbooks completos, e estado atual da topologia.
> Este arquivo é um quick-start abreviado.

## Hosts (LAN 192.168.30.x — migrado 2026-05-24)

| Hostname Tailscale | IP LAN | Subnet | Compose | Papel |
|---|---|---|---|---|
| `voyager` | `192.168.30.103` | nova | `docker-compose-prod.yml` | web, scheduler, nginx, cloudflared, drainers, worker_manual/classificacao/leads_consumo |
| `voyager-workers` | `192.168.30.102` | nova | `docker-compose-workers.yml` | worker_ingestion / enrich_trf1/trf3/trf5/tjmg / datajud / djen_audit / default / classificacao |
| `voyager-db` | `192.168.30.101` | nova | — | Postgres 16 nativo + pgbouncer (`:6432`). Sem container do projeto. |
| `voyager-redis` | `192.168.30.100` | nova | — | Redis 7 nativo. Sem container do projeto. |
| `voyager-workers-aux` | `192.168.1.24` (pve antigo) | antiga | `docker-compose-workers.yml` | Workers auxiliares — conecta DB/Redis via **Tailscale** (100.68.5.114, 100.98.86.54). Soma ~463 workers ao pool. |

Acesso SSH (de fora da LAN ou do laptop em outra subnet): `ssh ubuntu@voyager` (hostname Tailscale resolve).
Repositório nos hosts: `/home/ubuntu/voyager`.
Endpoint público: `https://voyager.was.dev.br` via Cloudflare Tunnel (`cloudflared` no `voyager`).

## Deploy padrão

```bash
# Host web (.103)
ssh ubuntu@voyager 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-prod.yml build web && \
  docker compose -f docker-compose-prod.yml up -d'

# Host workers (.102) — só se código de worker mudou
ssh ubuntu@voyager-workers 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-workers.yml build && \
  docker compose -f docker-compose-workers.yml up -d --force-recreate'

# Host workers auxiliar antigo (LAN diferente, conecta via Tailscale)
ssh ubuntu@voyager-workers-aux 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-workers.yml build && \
  docker compose -f docker-compose-workers.yml up -d --force-recreate'
```

`web` roda `migrate --noinput` + `collectstatic` no entrypoint. Migrations grandes deixam o healthcheck `unhealthy` temporariamente — esperar.

## `.env` em prod

Não é commitado. Existe em `/home/ubuntu/voyager/.env` em cada host de app.

**Hosts da subnet nova (`voyager`, `voyager-workers`)** — usa IPs LAN:
```
DJANGO_ALLOWED_HOSTS=voyager.was.dev.br,192.168.30.103,localhost,127.0.0.1,web,nginx,voyager
DATABASE_URL=postgres://voyager:<senha>@192.168.30.101:6432/voyager
REDIS_URL=redis://192.168.30.100:6379/0
```

**Host antigo (`voyager-workers-aux`, subnet `192.168.1.x`)** — usa IPs Tailscale (sem rota LAN pra subnet nova):
```
DJANGO_ALLOWED_HOSTS=voyager.was.dev.br,100.100.144.57,localhost,127.0.0.1,web,nginx,voyager
DATABASE_URL=postgres://voyager:<senha>@100.68.5.114:6432/voyager
REDIS_URL=redis://100.98.86.54:6379/0
```

Trocar de subnet → atualizar `.env` e fazer `docker compose ... up -d --force-recreate` (env_file só é relido em container novo).

## Verificar saúde

```bash
# Liveness público
curl -fsS https://voyager.was.dev.br/api/v1/health/liveness/

# Readiness (testa DB + Redis + lag de ingestão)
curl -fsS https://voyager.was.dev.br/api/v1/health/

# Containers
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml ps'
ssh ubuntu@voyager-workers 'docker compose -f ~/voyager/docker-compose-workers.yml ps'

# Status agregado de ingestão
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python manage.py djen_status'
```

## Logs

```bash
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml logs -f web nginx'
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml logs -f scheduler'
ssh ubuntu@voyager-workers 'docker compose -f ~/voyager/docker-compose-workers.yml logs -f worker_trf3'
```

Detalhes (runbooks, dedup, resize de VM, rollback v7, etc.) em `.ia/OPS.md`.
