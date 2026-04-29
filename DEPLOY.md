# Deploy

## Infraestrutura

| Host | Função | Compose |
|------|--------|---------|
| `192.168.1.30` | Servidor principal — web, banco, redis, scheduler, nginx, cloudflared, workers locais | `docker-compose-prod.yml` |
| `192.168.1.177` | Workers auxiliares — conecta no postgres/redis do `.30` via LAN | `docker-compose-workers.yml` |

Acesso SSH: `ubuntu@<ip>`  
Repositório: `/home/ubuntu/voyager` (em ambos os hosts)

---

## Deploy completo (ambos os hosts)

### 1 — Servidor principal (192.168.1.30)

```bash
ssh ubuntu@192.168.1.30
cd /home/ubuntu/voyager
git pull origin main
docker compose -f docker-compose-prod.yml build
docker compose -f docker-compose-prod.yml up -d
```

### 2 — Workers auxiliares (192.168.1.177)

```bash
ssh ubuntu@192.168.1.177
cd /home/ubuntu/voyager
git pull origin main
docker compose -f docker-compose-workers.yml build
docker compose -f docker-compose-workers.yml up -d
```

> Sempre fazer o deploy no `.30` primeiro — o `.177` depende do postgres e redis que rodam lá.

---

## Serviços por host

### 192.168.1.30 — `docker-compose-prod.yml`

| Serviço | Réplicas | Descrição |
|---------|----------|-----------|
| `postgres` | 1 | Banco de dados (exposto na LAN em `192.168.1.30:5432`) |
| `redis` | 1 | Broker de filas (exposto na LAN em `192.168.1.30:6379`) |
| `web` | 1 | Gunicorn 4 workers · 4 threads |
| `nginx` | 1 | Reverse proxy, serve static files |
| `cloudflared` | 1 | Túnel Cloudflare → `voyager.was.dev.br` |
| `scheduler` | 1 | APScheduler — dispara ingestão diária + backfill |
| `worker_ingestion` | 4 | Filas `djen_ingestion` + `djen_backfill` |
| `worker_default` | 1 | Fila `default` |
| `worker_trf1` | 40 | Fila `enrich_trf1` |
| `worker_trf3` | 40 | Fila `enrich_trf3` |
| `worker_manual` | 2 | Fila `manual` |
| `enrichment_drainer` | **1** | Consumer único do stream `voyager:enrichment:results` — workers só publicam, drainer aplica writes em bulk |

### 192.168.1.177 — `docker-compose-workers.yml`

| Serviço | Réplicas | Descrição |
|---------|----------|-----------|
| `worker_trf1` | 40 | Fila `enrich_trf1` |
| `worker_trf3` | 40 | Fila `enrich_trf3` |
| `worker_default` | 1 | Fila `default` |
| `worker_ingestion` | 4 | Filas `djen_ingestion` + `djen_backfill` |

---

## Arquivo .env nos servidores

Nenhum `.env` é commitado. Em cada servidor o arquivo `.env` fica em `/home/ubuntu/voyager/.env`.

Use `.env.example` como referência para criar ou atualizar.  
Use `.env.prod` como base para o ambiente de produção (já contém os valores reais usados nos dois servidores).

**Diferença entre os hosts:**

No `.30` o `DATABASE_URL` e `REDIS_URL` apontam para os serviços Docker internos:
```
DATABASE_URL=postgres://voyager:<senha>@postgres:5432/voyager
REDIS_URL=redis://redis:6379/0
```

No `.177` devem apontar para o host principal via LAN:
```
DATABASE_URL=postgres://voyager:<senha>@192.168.1.30:5432/voyager
REDIS_URL=redis://192.168.1.30:6379/0
```

---

## Verificar se está saudável após deploy

```bash
# No .30 — checar todos os containers
ssh ubuntu@192.168.1.30 "docker compose -f /home/ubuntu/voyager/docker-compose-prod.yml ps"

# No .177 — checar workers
ssh ubuntu@192.168.1.177 "docker compose -f /home/ubuntu/voyager/docker-compose-workers.yml ps"

# Testar resposta HTTP
curl -sI https://voyager.was.dev.br/
```

---

## Restart rápido (sem build — só reinicia)

```bash
# .30
ssh ubuntu@192.168.1.30 "docker compose -f /home/ubuntu/voyager/docker-compose-prod.yml up -d"

# .177
ssh ubuntu@192.168.1.177 "docker compose -f /home/ubuntu/voyager/docker-compose-workers.yml up -d"
```

---

## Ver logs em tempo real

```bash
# Web + nginx
ssh ubuntu@192.168.1.30 "docker compose -f /home/ubuntu/voyager/docker-compose-prod.yml logs -f web nginx"

# Scheduler
ssh ubuntu@192.168.1.30 "docker compose -f /home/ubuntu/voyager/docker-compose-prod.yml logs -f scheduler"

# Workers de ingestão
ssh ubuntu@192.168.1.30 "docker compose -f /home/ubuntu/voyager/docker-compose-prod.yml logs -f worker_ingestion"

# Workers no .177
ssh ubuntu@192.168.1.177 "docker compose -f /home/ubuntu/voyager/docker-compose-workers.yml logs -f worker_trf1"
```

---

## Migração de banco

O `web` roda `python manage.py migrate --noinput` automaticamente ao subir. Não é necessário rodar manualmente, mas caso precise:

```bash
ssh ubuntu@192.168.1.30
docker compose -f /home/ubuntu/voyager/docker-compose-prod.yml exec web python manage.py migrate
```
