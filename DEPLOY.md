# Deploy

> **Doc canônica:** ver `.ia/OPS.md` para inventário de hosts, configuração de workers, runbooks completos, e estado atual da topologia.
> Este arquivo é um quick-start abreviado.

## Hosts (LAN 192.168.30.x — migrado 2026-05-24)

| Hostname Tailscale | IP LAN | Subnet | Compose | Papel |
|---|---|---|---|---|
| `voyager` | `192.168.30.103` | nova | `docker-compose-prod.yml` | web, scheduler, nginx, cloudflared, drainers, worker_manual/classificacao/leads_consumo |
| `voyager-workers` | `192.168.30.102` | nova | `docker-compose-workers.yml` | worker_ingestion / enrich_trf1/trf3/trf5/tjmg / datajud / djen_audit / default / classificacao |
| `voyager-workers-2` | `192.168.30.104` | nova | `docker-compose-workers.yml` | Segundo host de workers (subnet nova) — soma capacidade ao pool. Substituiu o antigo `voyager-workers-aux`. |
| `voyager-db` | `192.168.30.101` | nova | — | Postgres 16 nativo + pgbouncer (`:6432`). Sem container do projeto. |
| `voyager-redis` | `192.168.30.100` | nova | — | Redis 7 nativo. Sem container do projeto. |
| `voyager-workers-aux` | `192.168.1.24` (pve antigo) | antiga | `docker-compose-workers.yml` | **Desativado** (offline desde ~2026-06-09). Workers auxiliares na subnet antiga, conectava DB/Redis via **Tailscale**. Sucedido pelo `voyager-workers-2`. |

Acesso SSH (de fora da LAN ou do laptop em outra subnet): `ssh ubuntu@voyager` (hostname Tailscale resolve; idem `voyager-workers`, `voyager-workers-2`).
Repositório nos hosts: `/home/ubuntu/voyager`.
Endpoint público: `https://voyager.was.dev.br` via Cloudflare Tunnel (`cloudflared` no `voyager`).

## Deploy padrão

São **3 hosts de app**: `voyager` (web) + `voyager-workers` + `voyager-workers-2`
(workers). Sempre faça `git pull --ff-only` nos três pra manter o repo em sincronia.
O rebuild depende do que mudou (ver "O que precisa de rebuild" abaixo).

```bash
# Host web (.103) — rebuild da imagem web
ssh ubuntu@voyager 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-prod.yml build web && \
  docker compose -f docker-compose-prod.yml up -d'

# Hosts workers (.102 e .104) — rebuild só se código de worker mudou
for H in voyager-workers voyager-workers-2; do
  ssh ubuntu@$H "cd ~/voyager && git pull --ff-only && \
    docker compose -f docker-compose-workers.yml build && \
    docker compose -f docker-compose-workers.yml up -d --force-recreate"
done
```

`web` roda `migrate --noinput` + `collectstatic` no entrypoint. Migrations grandes deixam o healthcheck `unhealthy` temporariamente — esperar.

> **Atenção ao rebuild de workers:** são ~320 containers por host. Rebuildar à toa
> consome RAM e pode causar OOM (vide incidente 2026-06-08 no `.ia/OPS.md`). Só
> rebuilde workers quando código de worker/model/migration de fato mudou.

### O que precisa de rebuild

| Mudança | `voyager` (web) | `voyager-workers` / `-2` |
|---|---|---|
| Template/CSS/JS do dashboard (`dashboard/templates`, `static`) | rebuild `web` (ou hot-deploy, abaixo) | só `git pull` (workers não servem dashboard) |
| View/query do dashboard (`dashboard/*.py`) | rebuild `web` | só `git pull` |
| Model / migration | rebuild `web` + `migrate` | rebuild + `--force-recreate` (worker usa o schema) |
| Job/enricher/djen/scheduler | rebuild `web` (scheduler roda lá) | rebuild + `--force-recreate` |

### Light hot-deploy (só dashboard, sem rebuild de imagem)

Pra mudança **só de template/CSS/JS** — afeta apenas o `web` no `voyager`:

```bash
ssh ubuntu@voyager 'cd ~/voyager && git pull --ff-only && \
  CID=$(docker compose -f docker-compose-prod.yml ps -q web) && \
  docker cp dashboard/. $CID:/app/dashboard/ && \
  docker compose -f docker-compose-prod.yml restart web'
```

Nesse caso, nos hosts de workers basta `git pull --ff-only` (sincroniza o repo, sem rebuild).

## `.env` em prod

Não é commitado. Existe em `/home/ubuntu/voyager/.env` em cada host de app.

**Hosts da subnet nova (`voyager`, `voyager-workers`, `voyager-workers-2`)** — usa IPs LAN:
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
ssh ubuntu@voyager-workers-2 'docker compose -f ~/voyager/docker-compose-workers.yml ps'

# Confirmar que um arquivo recém-mudado está no container web (ex.: template)
ssh ubuntu@voyager 'CID=$(docker compose -f ~/voyager/docker-compose-prod.yml ps -q web) && docker exec $CID grep -c "<termo da sua mudança>" /app/dashboard/templates/dashboard/base.html'

# Status agregado de ingestão
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python manage.py djen_status'
```

## Logs

```bash
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml logs -f web nginx'
ssh ubuntu@voyager 'docker compose -f ~/voyager/docker-compose-prod.yml logs -f scheduler'
ssh ubuntu@voyager-workers 'docker compose -f ~/voyager/docker-compose-workers.yml logs -f worker_trf3'
ssh ubuntu@voyager-workers-2 'docker compose -f ~/voyager/docker-compose-workers.yml logs -f worker_trf3'
```

Detalhes (runbooks, dedup, resize de VM, rollback v7, etc.) em `.ia/OPS.md`.
