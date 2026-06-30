# Deploy em produção

Guia auto-contido de deploy do Voyager. Serve tanto pra operar à mão quanto pra
colar pro Claude rodando em outra máquina (ver § "Prompt pronto" no fim).

Runbook detalhado de operação fica em [`OPS.md`](OPS.md). Aqui é o **passo a
passo de deploy**, destilado.

## Infra

SSH user `ubuntu`, acesso via `~/.ssh/config` (cheque com `ssh -G ubuntu@192.168.30.103 | head -3`).

| Host | IP LAN | Compose | Papel |
|---|---|---|---|
| web (principal) | `192.168.30.103` | `docker-compose-prod.yml` | Gunicorn, nginx, cloudflared, scheduler, worker_manual, worker_classificacao, enrichment_drainer_p0..p3 |
| workers (auxiliar) | `192.168.30.102` | `docker-compose-workers.yml` | workers RQ — ingestion, enrich_*, datajud, classificacao |
| db | `192.168.30.101` | — | Postgres 16 nativo (sem container do projeto) |
| redis | `192.168.30.100` | — | Redis 7 nativo (sem container do projeto) |

> Workers `worker_ingestion / worker_default / worker_djen_audit / worker_trf* /
> worker_tjmg / worker_datajud` **não existem** no `docker-compose-prod.yml` —
> vivem no `.102`. Tentar buildá-los no `.103` falha com `no such service`.

## Fase 1 — Detectar o que mudou

```bash
PROD_SHA=$(ssh ubuntu@192.168.30.103 'cd ~/voyager && git rev-parse HEAD')
git rev-parse HEAD
git diff --name-only "$PROD_SHA"..HEAD
```

Classifique a mudança e escolha a estratégia:

| Tipo | Arquivos típicos | Estratégia |
|---|---|---|
| **Hot deploy** (só web) | `dashboard/templates/**`, `dashboard/static/**`, `dashboard/views.py`, CSS/JS | `docker cp` + `restart web` no `.103` |
| **Rebuild web** | `core/**`, `api/**`, `requirements.txt`, migration nova | `build web` + `up -d` no `.103` |
| **Rebuild full** (workers afetados) | `djen/jobs.py`, `enrichers/**`, `tribunals/jobs.py`, `tribunals/models.py`, ML | `build` + `up -d` no `.103` **E** no `.102` |
| **Migration grande** | toca `movimentacao`/`process` | Avisar: healthcheck fica `unhealthy` 10+ min |

⚠️ **Regra de ouro**: esquecer de rebuildar o worker com schema novo causa
`IntegrityError` em todos os runs. **Em dúvida, rebuild full.**

## Pré-deploy (checar antes de propor o plano)

- [ ] Branch em prod faz `git pull --ff-only` na `main` — sua branch **já está mergeada na `main`**? Se não, merge antes (ou ajuste pra deployar a branch explicitamente).
- [ ] `git status` limpo? Senão, commitar ou abortar.
- [ ] `tribunals/models.py` mudou **sem** migration nova? → **bloquear** e avisar.
- [ ] `/api/v1/health/` está 200? (503 por lag = timing ruim pra deploy.)

## Fase 2 — Propor plano e confirmar

Mostrar arquivos mudados + hosts + comandos, e **esperar confirmação**. Não rodar
nos dois hosts em paralelo — confirmar `.103` primeiro.

## Fase 3 — Executar

### Hot deploy (só dashboard)

```bash
ssh ubuntu@192.168.30.103 'cd ~/voyager && git pull --ff-only && \
  CID=$(docker compose -f docker-compose-prod.yml ps -q web) && \
  docker cp dashboard/. $CID:/app/dashboard/ && \
  docker compose -f docker-compose-prod.yml restart web'
```
⚠️ Workers ficam com código antigo. Não use pra mudança em models/jobs.

### Rebuild web (sem worker)

```bash
ssh ubuntu@192.168.30.103 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-prod.yml build web && \
  docker compose -f docker-compose-prod.yml up -d'
```
`web` roda `migrate --noinput` + `collectstatic` no entrypoint.

### Rebuild full — host principal `.103`

```bash
ssh ubuntu@192.168.30.103 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-prod.yml build \
    web scheduler worker_manual worker_classificacao \
    enrichment_drainer_p0 enrichment_drainer_p1 enrichment_drainer_p2 enrichment_drainer_p3 && \
  docker compose -f docker-compose-prod.yml up -d --force-recreate \
    web scheduler worker_manual worker_classificacao \
    enrichment_drainer_p0 enrichment_drainer_p1 enrichment_drainer_p2 enrichment_drainer_p3 \
    nginx cloudflared'
```
> Lista exata atual: `docker compose -f docker-compose-prod.yml config --services`.

### Rebuild — host auxiliar `.102` (quando worker code mudou)

```bash
ssh ubuntu@192.168.30.102 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-workers.yml build && \
  docker compose -f docker-compose-workers.yml up -d --force-recreate'
```

## Fase 4 — Verificar

```bash
# Healthcheck público (espera 200)
curl -fsS https://voyager.was.dev.br/api/v1/health/liveness/ -o /dev/null -w '%{http_code}\n'

# Status agregado
ssh ubuntu@192.168.30.103 'docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python manage.py djen_status' 2>&1 | tail -20

# Workers vivos
ssh ubuntu@192.168.30.103 'docker compose -f ~/voyager/docker-compose-prod.yml ps' | grep -E 'worker|scheduler' | head -20
```

Migration grande — esperar `readiness` voltar a 200:

```bash
until curl -fsS https://voyager.was.dev.br/api/v1/health/ -o /dev/null -w '%{http_code}\n' 2>/dev/null | grep -q 200; do
  echo "aguardando readiness... $(date +%T)"; sleep 30
done
echo "✔ readiness OK"
```

## Anti-padrões

1. ❌ `docker compose up -d` sem `git pull` antes
2. ❌ Esquecer de rebuildar o `.102` quando worker code mudou
3. ❌ Hot deploy com mudança em `models.py`/`jobs.py`
4. ❌ Deployar com `worker_ingestion` em meio de backfill grande sem checar fila
5. ❌ Deployar nos 2 hosts em paralelo sem confirmar `.103` primeiro

## Rollback

```bash
ssh ubuntu@192.168.30.103 'cd ~/voyager && git log --oneline -5 && \
  git reset --hard <sha-anterior> && \
  docker compose -f docker-compose-prod.yml build web && \
  docker compose -f docker-compose-prod.yml up -d --force-recreate'
```
Migrations **não** dão rollback automático — se o código revertido precisar,
rodar `migrate <app> <migration_anterior>` (perguntar antes).

## Prompt pronto (colar pro Claude de outra máquina)

> Faça o deploy do Voyager em produção seguindo `.ia/DEPLOY.md`. Detecte o escopo
> da mudança (Fase 1), me mostre o plano e espere meu OK antes de tocar o
> servidor. Confirme o `.103` antes do `.102`. Em dúvida entre hot deploy e
> rebuild, prefira rebuild. Se minha branch não estiver na `main`, me avise antes.

> Atalho: se a máquina tiver a skill, basta `/deploy-prod` (mesma lógica deste doc).
