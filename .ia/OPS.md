# Operação

Runbooks específicos por situação. Para troubleshooting geral, comece por `djen_status` (CLI) ou `/dashboard/workers/` (UI).

## Stacks

| Ambiente | Compose | Hostname | Tunnel |
|---|---|---|---|
| Dev local | `docker-compose.yml` | `localhost` | — |
| Prod | `docker-compose-prod.yml` | `voyager.was.dev.br` | Cloudflare Tunnel |

Em prod o `nginx` não expõe porta no host; tudo passa pelo serviço `cloudflared` (token em `CLOUDFLARE_TUNNEL_TOKEN` no `.env`). `web → ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` precisam incluir o domínio público.

## Inventário de máquinas (LAN prod)

| IP | Papel | Compose |
|---|---|---|
| `192.168.1.32` | Servidor principal — web, nginx, cloudflared, scheduler, workers de ingestion | `docker-compose-prod.yml` |
| `192.168.1.28` | Postgres dedicado (sem container do projeto) | — |
| `192.168.1.30` | Redis dedicado (sem container do projeto) | — |
| `192.168.1.36` | Máquina auxiliar de workers (consolidação dos antigos `.177`/`.184`/`.115`) | `docker-compose-workers.yml` |

**Histórico (2026-05-12)**: topologia anterior usava `.30` (web), `.82` (db), `.219` (redis), `.177`/`.184`/`.115` (3 hosts de workers). Atual usa 4 hosts dedicados (web, db, redis, 1 host workers). Comandos antigos com IPs `.30`/`.82`/`.219`/`.177`/`.184`/`.115` precisam ser remapeados.

Máquina auxiliar (`.36`) roda só workers — sem web/db/redis próprios. Conecta no Postgres (`.28`) e Redis (`.30`) via LAN. O drainer do stream de enrichment roda **somente no `.32`**; a auxiliar só publica resultados.

## Workers em prod (configuração atual — 2026-05-14)

**`.32` (host web)** via `docker-compose-prod.yml`:
```
web                       1   Django + Gunicorn
scheduler                 1   APScheduler + ThreadPoolExecutor(20). Warm jobs inline.
worker_manual             2   fila 'manual' (cliques on-demand)
worker_classificacao      8   fila 'classificacao' (batch ML, hot reload v6)
enrichment_drainer_p0..p3 4   drainer do stream Redis (RODA SÓ AQUI — não nas auxiliares)
nginx                     1   reverse proxy
cloudflared               1   tunnel pra voyager.was.dev.br
```

Workers de ingestão e enrich pesados (TRF1/TRF3/DJEN/Datajud/TJMG) **não** rodam
no `.32`. Ficaram consolidados no `.36`.

> **Nota:** `worker_warm` foi removido em 2026-05-06. Os jobs de warm de cache
> (KPIs, charts, partes, estatísticas, filtros, MV refresh) passaram a rodar
> inline no thread pool do `scheduler`, sem fila RQ. Ver ADR-017.

**`.36` (host workers consolidado)** via `docker-compose-workers.yml`:
```
worker_ingestion       4   fila 'djen_ingestion' + 'djen_backfill'
worker_default         2   fila 'default' (catch-all)
worker_djen_audit     10   fila 'djen_audit'
worker_trf1           60   fila 'enrich_trf1'
worker_trf3           50   fila 'enrich_trf3'
worker_tjmg           50   fila 'enrich_tjmg'
worker_datajud        90   fila 'datajud'
worker_classificacao   8   fila 'classificacao' (réplica adicional)
```

Total observado pós-deploy 2026-05-14: ~274 containers vivos.

Page `/dashboard/workers/` mostra estado em tempo real (auto-refresh 5s).

### Filas RQ (`core/settings.py::RQ_QUEUES`)
```
default          — catch-all
djen_ingestion   — daily ingestions
djen_backfill    — backfill de janelas + sync per-CNJ
djen_audit       — auditoria por órgão
enrich_trf1      — PJe scraping TRF1
enrich_trf3      — PJe scraping TRF3
manual           — UI clicks (alta prioridade)
datajud          — Datajud sync per-CNJ
classificacao    — batch ML (TIMEOUT 4h)
```

> **Removida:** fila `warm` foi eliminada em 2026-05-06. Warm jobs rodam inline
> no scheduler. Não existe mais `worker_warm` nem entrada `warm` em `RQ_QUEUES`.

## Deploy em prod

```bash
ssh ubuntu@<server>
cd ~/voyager
git pull --ff-only
docker compose -f docker-compose-prod.yml build web
docker compose -f docker-compose-prod.yml up -d
```

`web` roda `migrate --noinput` + `collectstatic` no entrypoint. Migrations grandes (10+ min) tornam o `healthcheck` `unhealthy` temporariamente — workers ficam em `dependency failed to start` até o web ficar healthy. Não é problema, basta aguardar.

## Comandos do dia-a-dia

```bash
# Status agregado
docker compose exec web python manage.py djen_status

# Logs ao vivo
docker compose logs -f worker_ingestion
docker compose logs -f web

# Acompanhar progresso
watch -n 30 'docker compose exec -T web python manage.py djen_status'

# Forçar refresh do pool de proxies
docker compose exec -T web python manage.py shell -c \
  "from djen.proxies import ProxyScrapePool; print(ProxyScrapePool.singleton().refresh())"
```

## Subir backfill de tribunal novo

```bash
# 1. Liga o tribunal
docker compose exec web python manage.py shell -c \
  "from tribunals.models import Tribunal; Tribunal.objects.filter(sigla='TRF2').update(ativo=True)"

# 2. Descobre o floor
docker compose exec web python manage.py djen_descobrir_inicio TRF2

# 3. Dispara backfill
docker compose exec web python manage.py djen_backfill TRF2

# 4. Acompanha
docker compose exec web python manage.py djen_status
```

Quando `backfill_concluido_em` ficar setado, o cron diário começa a rodar automaticamente (escalonado).

## DJEN está fora do ar (504 em massa)

Sintomas: `djen_status` mostra muitos `failed`, logs cheios de `DJEN 504 após N tentativas`.

Diagnóstico (1 comando):
```bash
docker compose exec web curl -sS --max-time 30 \
  -x http://cortex-http.was.dev.br:44383 \
  'https://comunicaapi.pje.jus.br/api/v1/comunicacao?siglaTribunal=TRF1&pagina=1&itensPorPagina=10&dataDisponibilizacaoInicio=2024-01-01&dataDisponibilizacaoFim=2024-01-05' \
  -o /dev/null -w "status=%{http_code} time=%{time_total}s\n"
```

Se 504 persistente → a DJEN está fora do ar. Pra economizar recursos:

```bash
docker compose exec web python manage.py shell -c \
  "import django_rq; [django_rq.get_queue(q).empty() for q in ('djen_backfill','djen_ingestion')]"
docker compose stop worker_ingestion
```

Quando voltar (200 OK):
```bash
docker compose start worker_ingestion
docker compose exec web python manage.py djen_backfill TRF1
docker compose exec web python manage.py djen_backfill TRF3
```

`run_backfill` é resilient — pula chunks `success`, retenta `failed`.

## Watchdog de ingestão

Cron `*/5 * * * *` em `djen.jobs.watchdog_ingestao`. Faz auto-heal de 3 cenários:

1. **Zumbis**: `IngestionRun.status=running` e `finished_at IS NULL` há mais de 1h → marca FAILED + grava motivo. Worker que crashou e deixou rastro não trava o sistema.
2. **Backfill perdido**: pra cada tribunal ativo com `backfill_concluido_em IS NULL`, se nenhum job dele em `djen_backfill` (pending nem started) → `run_backfill.delay(sigla)`. Recupera quando redis perdeu state ou backfill morreu.
3. **Daily atrasado**: pra tribunal com backfill ok mas sem `IngestionRun success` há >26h → `run_daily_ingestion.delay(sigla)`.

Rodar manualmente (heal imediato sem esperar 5min):
```bash
docker compose -f docker-compose-prod.yml exec web python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings')
django.setup()
from djen.jobs import watchdog_ingestao
print(watchdog_ingestao())
"
```

Output: `{'zumbis_matados': N, 're_backfill': [...], 're_daily': [...]}`.

## Backfill de partes em batch

Re-enfileira processos pendentes/erro pra fila do tribunal:

```bash
# Todos pendentes do TRF3 (vai pra enrich_trf3)
docker compose -f docker-compose-prod.yml exec web python manage.py \
  enriquecer_pendentes --tribunal TRF3 --limit 0

# Reprocessar erros (proxy ruim, rate limit) do TRF1
docker compose -f docker-compose-prod.yml exec web python manage.py \
  enriquecer_pendentes --tribunal TRF1 --status erro --limit 0
```

## Schema drift detectado

Sintomas: drift alert vermelho no `/dashboard/ingestao/` ou em `djen_status`.

1. Abrir alerta no admin (`/admin/tribunals/schemadriftalert/`).
2. Olhar campo `exemplo` (1 item DJEN com texto truncado em 500 chars).
3. Atualizar `djen/parser.py`:
   - **extra_keys**: ou (a) adicionar à `EXPECTED_KEYS` pra silenciar; ou (b) adicionar coluna em `Movimentacao` + migration + mapeamento em `parse_item`.
   - **missing_keys**: ajustar parser pra tolerar (`item.get(...)` já tolera) + remover de `EXPECTED_KEYS`.
4. Marcar alerta como resolvido (admin action).
5. Constraint partial reabre se a divergência voltar.

## Rebuild após mudança de model/migration

✅ **TODOS** os containers que rodam Python precisam ser rebuildados:

```bash
docker compose build web worker_ingestion worker_default scheduler
docker compose up -d --force-recreate web worker_ingestion worker_default scheduler
docker compose exec web python manage.py migrate
```

❌ Esquecer de rebuildar `worker_ingestion` (caso clássico) — o worker continua com schema antigo, `bulk_create` envia campos NULL → `IntegrityError` em todos os runs.

## Light hot-deploy (só dashboard/web)

Pra mudança em template/views/CSS/JS sem rebuild de imagem:

```bash
CID=$(docker compose ps -q web)
docker cp dashboard/. $CID:/app/dashboard/
docker compose restart web
```

⚠️ Workers continuam com código antigo. Não use isso pra mudança em models/migrations/jobs.

## Static 404 mesmo arquivo existindo

Ver `OPS-static.md` (TODO). Causa comum: `STATICFILES_STORAGE = CompressedManifestStaticFilesStorage` exige `staticfiles.json` que não foi gerado em DEBUG. Voyager configura:

```python
WHITENOISE_USE_FINDERS = DEBUG  # serve via finders no DEBUG (sem manifest)
WHITENOISE_AUTOREFRESH = DEBUG
```

E `nginx.conf` faz `proxy_pass` de `/static/` pro web (cache 30d). Em prod (DEBUG=False), considere voltar pra serving direto pelo nginx + montar volume `static` no `web` em `/app/staticfiles`.

## 502 após force-recreate

Causa: nginx cacheou IP antigo do `web`. Solução já em `nginx.conf`:

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
set $upstream "web:8000";
proxy_pass http://$upstream;
```

Se ainda assim acontecer: `docker compose restart nginx`.

## Disco

Estimativas com cobertura de TRF1+TRF3 ~60%:

| Item | Espaço |
|---|---|
| Total atual (60% TRF1+TRF3) | ~9.5 GB |
| TRF1+TRF3 100% (5 anos cada) | ~16 GB |
| 1 TRF médio (5 anos) | 6-10 GB |
| TJSP (5 anos) | 30-50 GB (volume ~5x) |
| **7 tribunais ativos completos** | ~70-100 GB |

53% do espaço é TOAST (texto comprimido), 30% índices, 17% heap. ~7.8 KB por movimentação.

Otimizações se apertar:
- Drop do índice trigram `mov_texto_trgm` (~700MB) — perde busca por substring exata
- Particionar `tribunals_movimentacao` por mês (planejado)
- Cold storage de movs >2 anos pra outra tabela ou S3

## Migrar dados local → prod

`pg_dump` custom format streamado direto pro postgres do servidor:

```bash
# 1. Para serviços que escrevem no DB do servidor (mantém postgres up)
ssh ubuntu@<server> "cd ~/voyager && docker compose -f docker-compose-prod.yml \
  stop web worker_default worker_ingestion worker_trf1 worker_trf3 scheduler"

# 2. Dump local → arquivo no servidor
docker compose exec -T postgres pg_dump -U voyager -Fc -Z 6 voyager \
  | ssh ubuntu@<server> "cat > /tmp/voyager.dump"

# 3. Copia pra dentro do container postgres do server e restaura
ssh ubuntu@<server> "cd ~/voyager && \
  docker cp /tmp/voyager.dump \$(docker compose -f docker-compose-prod.yml ps -q postgres):/tmp/voyager.dump && \
  docker compose -f docker-compose-prod.yml exec -T postgres pg_restore \
    -U voyager -d voyager --clean --if-exists --no-owner --no-acl -j 4 /tmp/voyager.dump"

# 4. Religa serviços
ssh ubuntu@<server> "cd ~/voyager && docker compose -f docker-compose-prod.yml up -d"
```

Tempo típico: 11GB local → 2.7GB compressed → ~5min transfer + 20min restore (paralelo `-j 4`).

## Cloudflare Tunnel quebrado / 502 / CSRF falhou

Sintomas:
- `liveness` retorna 502 → `cloudflared` ou `nginx` desconectou
- "Verificação CSRF falhou" no login → `X-Forwarded-Proto` chegou como `http`

Diagnóstico:
```bash
ssh ubuntu@<server> "cd ~/voyager && docker compose -f docker-compose-prod.yml logs --tail=20 cloudflared"
```

Espera ver `Registered tunnel connection` em 4 PoPs.

CSRF falha foi resolvida em `infra/nginx.conf`: `proxy_set_header X-Forwarded-Proto https;` (hardcoded) — o nginx em prod só recebe via tunnel, sempre HTTPS no edge.

## Backups

```bash
# Postgres (manual)
docker compose exec -T postgres pg_dump -U voyager voyager | gzip > backups/voyager-$(date +%F).sql.gz

# Restore
gunzip -c backups/voyager-2026-04-25.sql.gz | docker compose exec -T postgres psql -U voyager -d voyager
```

`pg_dump` automático diário ainda **não implementado** (planejado em `default` queue). Roadmap em [`ROADMAP.md`](ROADMAP.md).

## Health endpoints

| Endpoint | Uso | Comportamento |
|---|---|---|
| `/api/v1/health/liveness/` | Docker HEALTHCHECK, k8s liveness | Sempre 200 se processo respira |
| `/api/v1/health/` | Monitoring externo, k8s readiness | 200 se OK; 503 se DB/Redis fora ou lag>36h em algum tribunal ativo. Drift alerts não trippam 503 (só aparecem no payload) |

Drift e lag em monitoring externo (Slack/Sentry) — não afetam disponibilidade da API/dashboard.

## Classificação de leads — operação

Pipeline ML que classifica processos como Precatório/Pré/Direito Creditório. Detalhe completo em [`CLASSIFICACAO.md`](CLASSIFICACAO.md).

### Disparar batch manual (re-classificar tudo)

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python manage.py shell -c \"
from tribunals.jobs import reclassificar_recentes
job = reclassificar_recentes.delay(dias=7, paralelizar=True)
print(f'job: {job.id}')
\""
```

Job principal splitta em batches → vão pra fila `classificacao` → workers paralelos drenam.

### Auditar fila datajud / classificacao

```python
import django_rq
qd = django_rq.get_queue('datajud')        # fila Datajud
qc = django_rq.get_queue('classificacao')  # fila ML batch
print(f'datajud: pending={len(qd):,} failed={qd.failed_job_registry.count}')
print(f'classificacao: pending={len(qc):,} failed={qc.failed_job_registry.count}')
```

### Re-enfileirar failures

Há um script comum `/tmp/requeue_failed_datajud.py` que pega todos `failed_job_registry`, extrai `process_id` dos args e re-enfileira na fila `datajud`. Útil quando rolling restart deixa jobs órfãos como AbandonedJobError.

### Gerar API key pra novo cliente externo

Via Django admin (`/admin/tribunals/apiclient/`):
1. Add ApiClient → digita só `nome` (ex: 'cliente_x')
2. Salvar — `api_key` é gerada automaticamente (`secrets.token_urlsafe(32)`)
3. Copiar key e enviar pro cliente

Cliente usa header `X-API-Key: <key>` em todas requests pra `/api/v1/leads/*`.

### Cliente atual: Juriscope
- API key: armazenada em `.env` deles (não documentar aqui)
- Capacidade: ~5.000 leads/dia
- Endpoint chamado tipicamente em cron diário (madrugada)

## Validação humana, shadow mode e retreino v7

Sistema entregue em Wave 0-5 (T4-T22). Detalhe técnico em
[`CLASSIFICACAO.md`](CLASSIFICACAO.md), regras de negócio em
[`REGRAS_NEGOCIO_VALIDACAO.md`](REGRAS_NEGOCIO_VALIDACAO.md), procedimento de
deploy em [`V7_DEPLOY_DECISION.md`](V7_DEPLOY_DECISION.md).

### Settings novos (em `core/settings.py`)

| Setting | Default | Função |
|---|---|---|
| `CLASSIFICADOR_RELOAD_TTL` | 60 | Segundos entre tentativas de hot reload da `ClassificadorVersao(ativa=True)` |
| `SHADOW_SAMPLE_RATE` | 0.10 | Fração [0,1] das classificações que disparam shadow async. 0 desliga |
| `VALIDACAO_LOTES_SEMANAIS_ENABLED` | True | Liga/desliga cron semanal de mining FN + criar lote |

### Crons novos (scheduler do `.30`)

| Job | Schedule | Fila | Função |
|---|---|---|---|
| `gerar_lotes_semanais_fn` | dom 02:00 | default | minera FN por tribunal e cria `AmostraValidacao(estrategia='fn_candidatos')` |
| `comparar_shadow_daily` | 04:00 diário | default | roda `comparar_shadow('v_ativa', 'v_shadow', dias=7)` por par de versões |

### CSVs de ground truth (versionados em git)

Desde 2026-05-12, CSVs de ground truth ficam em `data_ground_truth/` (versionados):

| Arquivo | Tipo | Usado por |
|---|---|---|
| `leads_trf1.csv` (396k) | label=1, peso 1.0 | `exportar_labels_retreino`, `treinar_classificador_v7`, `minerar_fn` (E4/E5) |
| `leads_trf1_recuperados_1327.csv` | label=1, peso 2.0 | mesmo + `gerar_lote_validacao --estrategia recuperados` |
| `leads_trf1_falsos_consumidos_1327.csv` | label=0, peso 2.0 | mesmo + gate de regressão FP no v7 |
| `leads_trf1_precatorio_1336.csv` | label=1 (N1), peso 2.0 | `exportar_labels_retreino` |
| `leads_trf3.csv` + `leads_trf3_precatorio_500.csv` | label=1, peso 2.0 | idem |
| `leads_trf3_top1000_recentes.csv`, `lista_5000_*.csv`, `poc_*.csv` | candidatos/POC | mining + análise |

**Em prod**: o `git pull` na `web` (.32) e em `workers` (.36) já traz os CSVs — não precisa `scp` manual. Total ~13MB, cabe no repo sem LFS. CSVs de runtime (geram em treino/mining) continuam em `data/` (gitignored).

### Management commands novos

| Comando | Função |
|---|---|
| `minerar_fn` | Roda E1-E6 sobre universo, gera CSV `fn_candidatos_<sigla>_<data>.csv` |
| `gerar_lote_validacao` | Cria lote manual (`--estrategia X --tribunal Y --tamanho N`) |
| `gerar_lotes_semanais_fn` | Pipeline completo: mining + criação de lotes (usado pelo cron) |
| `exportar_labels_retreino` | Consolida fontes de label (humano + Juriscope + CSVs) em dataset com `sample_weight` por origem |
| `treinar_classificador_v6` | Treino v6 (TRF1) — usado em 2026-05-08 |
| `treinar_classificador_v7` | Treino v7: 24 features + sample_weight + 6 gates + grid de thresholds + opcionalmente deploy |
| `setup_validacao_groups` | Cria grupos `validadores_leads`, `revisores_seniores`, `auditores_leads`, `model_admins` e aplica permissions |

### Páginas de dashboard novas

| URL | Descrição |
|---|---|
| `/dashboard/leads/visibilidade/` | Overview com 8 KPIs + 5 charts (histograma score, calibração, funil, top FN, shadow status) + heatmap tribunal × ano CNJ |
| `/dashboard/leads/validacao/` | Lista de lotes ativos do usuário; botão "criar lote" (precisa `can_publish_model`) |
| `/dashboard/leads/validacao/<id>/` | Fila de anotação 1-por-vez com hotkeys; navega item por item |
| `/dashboard/leads/validacao/<id>/concluido/` | Sumário pós-finalização do lote |

Acesso: `can_view_validacao_dashboard` (visibilidade/overview); `can_validate_lead`
(anotação). Decorators em `dashboard/views.py`.

### Procedimento deploy v7 (sumário)

Detalhes em [`V7_DEPLOY_DECISION.md`](V7_DEPLOY_DECISION.md). Visão de alto nível:

1. Treinar v7 com `treinar_classificador_v7` — gera relatório de 6 gates.
2. Se Pass 6/6 (ou Warn com dupla aprovação) → criar `ClassificadorVersao(versao='v7', shadow=True)`.
3. Aguardar 7 dias com `SHADOW_SAMPLE_RATE=0.10`. Cron `comparar_shadow_daily` produz relatório.
4. Sign-off humano (review disagreements + KS + agreement).
5. Flip de `ativa=True` (`ClassificadorVersao.objects.filter(versao='v7').update(ativa=True)`) — propaga em ≤ 60s via hot reload.

### Rollback v7

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.models import ClassificadorVersao
from django.db import transaction
with transaction.atomic():
    ClassificadorVersao.objects.filter(versao='v7').update(ativa=False)
    ClassificadorVersao.objects.filter(versao='v6').update(ativa=True)
print('rollback aplicado')
\""
```

Workers detectam em ≤ 60s. Re-classificar últimas 24h: `reclassificar_recentes.delay(dias=1, paralelizar=True)`.

## Adicionar tribunal novo (TJMG, TJSP, etc)

1. Criar `Tribunal` (sigla, sigla_djen, data_inicio_disponivel, ativo=True)
2. Verificar se Datajud tem index — `api_publica_<sigla>` (ex: `api_publica_tjmg`)
3. Verificar se DJEN aceita a sigla (manualmente: `curl -G 'https://comunicaapi.pje.jus.br/api/v1/comunicacao' --data-urlencode 'siglaTribunal=TJMG' --data-urlencode 'dataDisponibilizacaoInicio=2026-04-15' --data-urlencode 'dataDisponibilizacaoFim=2026-04-15' --data-urlencode 'pagina=1'`)
4. Backfill DJEN — APScheduler já enfileira automaticamente (1 cron diário + tick_backfill_retroativo)
5. Datajud sync acontece automaticamente via auto-enqueue (`_enfileirar_todos_enrichments` na ingestão DJEN), mas pra acelerar vale enfileirar em massa
6. Classificação roda automaticamente após Datajud sync; cron `reclassificar_recentes` cobre o restante
7. Sem ground truth do tribunal novo, modelo TRF1 é aplicado mas precision real é desconhecida — calibration plot na `/dashboard/leads/` revela depois que Juriscope começar a consumir + marcar `validado/sem_expedicao`

**Caveat estaduais**: o patch de `datajud.sync_processo` agora popula `Process.classe_codigo` quando vazio — necessário pra TJ* funcionarem. Se o tribunal novo não estiver no Datajud, classe fica vazia e classificador retorna NAO_LEAD em todos.
