# Operação

Runbooks específicos por situação. Para troubleshooting geral, comece por `djen_status` (CLI) ou `/dashboard/workers/` (UI).

## Stacks

| Ambiente | Compose | Hostname | Tunnel |
|---|---|---|---|
| Dev local | `docker-compose.yml` | `localhost` | — |
| Prod | `docker-compose-prod.yml` | `voyager.was.dev.br` | Cloudflare Tunnel |

Em prod o `nginx` não expõe porta no host; tudo passa pelo serviço `cloudflared` (token em `CLOUDFLARE_TUNNEL_TOKEN` no `.env`). `web → ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` precisam incluir o domínio público.

## Inventário de máquinas (prod)

| Hostname TS | IP LAN | Subnet | Papel | Compose |
|---|---|---|---|---|
| `voyager` | `192.168.30.103` | nova | Servidor principal — web, nginx, cloudflared, scheduler, drainers, worker_manual/classificacao/leads_consumo | `docker-compose-prod.yml` |
| `voyager-db` | `192.168.30.101` | nova | Postgres 16 nativo + pgbouncer (`:6432`) | — |
| `voyager-redis` | `192.168.30.100` | nova | Redis 7 nativo | — |
| `voyager-workers` | `192.168.30.102` | nova | Workers RQ — conecta em DB/Redis via **LAN** | `docker-compose-workers.yml` |
| `voyager-workers-2` | `192.168.30.104` | nova | 2º host de workers RQ (subnet nova) — conecta em DB/Redis via **LAN**. Sucedeu o `voyager-workers-aux`. | `docker-compose-workers.yml` |
| `voyager-workers-aux` | `192.168.1.24` | antiga (pve antigo) | **Desativado** (offline desde ~2026-06-09) — era worker auxiliar na subnet antiga via Tailscale. | `docker-compose-workers.yml` |

**Fleet de app são 3 hosts**: `voyager` (.103, web) + `voyager-workers` (.102) + `voyager-workers-2` (.104). Os dois de workers ficam na subnet nova e conectam DB/Redis via **LAN**. O drainer do stream **só roda no `voyager`** (.103), não nos hosts de workers.

`voyager-workers-2` (.104) entrou em 2026-06 pra somar capacidade na subnet nova, no lugar do `voyager-workers-aux` — a VM antiga (VMID 100, pve antigo, subnet `192.168.1.x`) que somava workers via Tailscale (`DATABASE_URL=postgres://...@100.68.5.114:6432/voyager`, `REDIS_URL=redis://100.98.86.54:6379/0`) e está offline desde ~2026-06-09.

**Histórico**:
- **2026-05-24** — migração de subnet `192.168.1.x` → `192.168.30.x` (mesmos hosts lógicos, IPs novos). Tailscale dos 4 hosts novos inalterado. VM antiga `voyager-workers` (VMID 100, pve antigo) ficou viva como auxiliar `voyager-workers-aux` — re-keyed no Tailscale (era `voyager-workers` 100.115.193.26 → renomeado pra `voyager-workers-aux`, mesmo IP 100.115.193.26; a **nova** voyager-workers `.102` perdeu sua identidade Tailscale no processo e precisa reauth manual).
- **2026-05-12** — topologia anterior usava `.30` (web all-in-one), `.82` (db), `.219` (redis), `.177`/`.184`/`.115` (3 hosts de workers). Consolidou em 4 hosts dedicados.

> **Nota (histórica — `voyager-workers-aux` desativado ~2026-06-09):** quando ainda
> estava vivo, o aux na LAN `192.168.1.24` **não era alcançável direto** do laptop em
> `192.168.1.x` (subnet física diferente, mesmo CIDR) — acessava via Tailscale
> (`ssh ubuntu@voyager-workers-aux`) ou jump pelo pve antigo (`ssh -J root@pve ubuntu@192.168.1.24`).
> O sucessor `voyager-workers-2` (.104) está na subnet nova e é acessível como os demais
> (`ssh ubuntu@voyager-workers-2`).

## Workers em prod (configuração atual — 2026-05-14)

**`.103` (host web)** via `docker-compose-prod.yml`:
```
web                       1   Django + Gunicorn
scheduler                 1   APScheduler + ThreadPoolExecutor(20). Warm jobs inline.
worker_manual             2   fila 'manual' (cliques on-demand)
worker_classificacao      8   fila 'classificacao' (batch ML, hot reload v6)
worker_leads_consumo      4   fila 'leads_consumo' (consumo Juriscope async, idempotente)
enrichment_drainer_p0..p3 4   drainer do stream Redis (RODA SÓ AQUI — não nas auxiliares)
nginx                     1   reverse proxy
cloudflared               1   tunnel pra voyager.was.dev.br
```

Workers de ingestão e enrich pesados (TRF1/TRF3/DJEN/Datajud/TJMG) **não** rodam
no `.103`. Ficaram consolidados no `.102`.

> **Nota:** `worker_warm` foi removido em 2026-05-06. Os jobs de warm de cache
> (KPIs, charts, partes, estatísticas, filtros, MV refresh, **leads charts**)
> passaram a rodar inline no thread pool do `scheduler`, sem fila RQ. Ver ADR-017.

> **Nota (2026-05-20):** adicionado `warm_tribunal_status` (scheduler inline
> 15min) — pré-aquece a página `/dashboard/tribunais/status/` (status / linha do
> tempo por tribunal). Computa todos os tribunais numa passada; chave de cache
> `tribunal_status:v1`. Exige rebuild de `web`+`scheduler` (mudou view/queries/
> scheduler — não é hot-deploy só de dashboard).

> **Nota (2026-05-18):** adicionado `warm_leads_charts` (scheduler 30min) — pré-
> aquece os widgets da `/dashboard/leads/`. Exige rebuild de `web`+`scheduler`
> (mudou job/scheduler, não é hot-deploy só de dashboard). No mesmo dia: limpeza
> one-time de 982 `LeadConsumption.resultado='VALIDADO'` → `'validado'` (path
> legado pré-`lote_id`; valor canônico é lowercase). Path ativo já rejeita
> casing inválido — sem recorrência esperada.

**`.102` (`voyager-workers`) e `.104` (`voyager-workers-2`)** — ambos via `docker-compose-workers.yml` (config **idêntica** nos dois hosts; cada um roda ~320 réplicas). Substituíram o par `.102` + `voyager-workers-aux` (subnet antiga, desativado ~2026-06-09):
```
                     réplicas  mem_limit  fila
worker_trf1            24       768m      enrich_trf1
worker_trf3            72       768m      enrich_trf3    (gargalo — prioridade)
worker_trf5            24       768m      enrich_trf5
worker_tjmg            72       768m      enrich_tjmg    (gargalo — prioridade)
worker_tjma             8       768m      enrich_tjma
worker_tjsp            40       640m      enrich_tjsp    (maior volume — e-SAJ)
worker_tjdft            8       512m      enrich_tjdft
worker_tjal            24       640m      enrich_tjal    (e-SAJ via pool ProxyScrape — ADR-021, 2026-06-17)
worker_djen_audit       6       512m      djen_audit
worker_datajud         24       512m      datajud
worker_ingestion        8       1g        djen_ingestion + djen_backfill
worker_default          2       512m      default
worker_classificacao    8       1g        classificacao  (carrega modelo ML)
```

Total por host: **320 containers** (304 + tjal 8→24 em 2026-06-17). Com os 2 hosts (`.102` + `.104`): ~640 workers RQ.

> **Incidente OOM 2026-06-08** (commit `6c3a784`): a `.102` (56GB) **travou por
> OOM** — o config pedia ~608 réplicas **sem `mem_limit`**, então 1 worker que
> inchava (BS4 em página PJe grande) derrubava o **host inteiro** em vez de só o
> container. No dashboard `/dashboard/workers/` isso aparece como filas sumindo
> (trf1 zerou) e contagens despencando, conforme o RQ expira o heartbeat dos
> workers da VM travada. Correções: (1) `mem_limit` por serviço — Docker mata só
> o worker que estoura, RQ re-enfileira, host nunca congela; (2) scale ~608→304
> por host (densidade ~200MB/worker, headroom pros picos); (3) `.102` +8GB RAM
> (56→62GB, **reboot obrigatório** — sem hotplug). A `aux` (55GB) rodava o mesmo
> config antigo e código stale (`f271830`) — re-deployada no mesmo commit.
> **Lição**: nunca rodar workers sem `mem_limit`; o teto transforma OOM-de-host
> em OOM-de-container (recuperável). Re-escalar gargalo (trf3/tjmg/tjsp) conforme
> backlog, monitorando `free -h` na VM antes de subir réplicas.

### Resize da VM voyager-workers (VMID 100) — 2026-05-14

Antes: 32GB RAM / 24 vCPUs / NUMA 16GB×2 — RAM crítica em 90% pós-scale.
Agora: **56GB RAM** / 24 vCPUs / NUMA 28GB×2.

Procedimento (Proxmox CLI no host pve):
```bash
ssh root@<pve>
qm shutdown 100 --timeout 60      # falha se Docker travar; cair pra qm stop
qm stop 100                        # hard stop (workers re-enfileiram jobs via RQ retry)
qm set 100 -memory 57344
qm set 100 -numa0 cpus=0-11,hostnodes=1,memory=28672,policy=preferred
qm set 100 -numa1 cpus=12-23,hostnodes=1,memory=28672,policy=preferred
qm start 100
# após boot:
ssh ubuntu@192.168.30.102 'cd ~/voyager && docker compose -f docker-compose-workers.yml up -d'
```

⚠️ Hotplug CPU/memory **não habilitado** nessa VM — reboot é obrigatório
pra resize. Pra futuro: `qm set 100 -hotplug disk,network,usb,memory,cpu`.

### Quando ressubir worker_datajud

Antes de rodar reclassificação em massa (`reclassificar_recentes` ou
`reclassificar_trf1_bulk`), volte o pool pra 90 réplicas:
```bash
ssh ubuntu@192.168.30.102 'cd ~/voyager && \
  docker compose -f docker-compose-workers.yml up -d --scale worker_datajud=90 worker_datajud'
```
Isso vai puxar ~7GB de RAM — só faça quando o backlog de enrich estiver
drenado, senão estoura RAM (.36 fica em 90% pós-rebalanceamento).

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
leads_consumo    — consumo Juriscope async (POST /leads/consumed/, idempotente por lote_id)
```

> **Removida:** fila `warm` foi eliminada em 2026-05-06. Warm jobs rodam inline
> no scheduler. Não existe mais `worker_warm` nem entrada `warm` em `RQ_QUEUES`.

## Deploy em prod

> Quick-start abreviado em [`DEPLOY.md`](../DEPLOY.md) (raiz) — host table + comandos prontos.

Fleet de app = **3 hosts**: `voyager` (web) + `voyager-workers` + `voyager-workers-2`.
Acesso via hostname Tailscale (`ssh ubuntu@voyager` etc.). Sempre `git pull --ff-only`
nos três; o rebuild depende do que mudou.

```bash
# web (.103) — rebuild da imagem web
ssh ubuntu@voyager 'cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-prod.yml build web && \
  docker compose -f docker-compose-prod.yml up -d'

# workers (.102 e .104) — rebuild só se código de worker/model/migration mudou
for H in voyager-workers voyager-workers-2; do
  ssh ubuntu@$H "cd ~/voyager && git pull --ff-only && \
    docker compose -f docker-compose-workers.yml build && \
    docker compose -f docker-compose-workers.yml up -d --force-recreate"
done
```

**Mudança só de dashboard (template/CSS/JS/view):** afeta apenas o `web`. Rebuilde só o
`voyager` (ou use o "Light hot-deploy" abaixo) e faça apenas `git pull --ff-only` nos
hosts de workers — não rebuilde ~320 containers à toa (risco de OOM, ver incidente 2026-06-08).

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

> **A DJEN é Cortex-only — o pool NÃO substitui.** O WAF da DJEN bloqueia ~100%
> dos IPs datacenter do ProxyScrape (medido 2026-06-17: 0/29, HTTP 403). Se os
> `failed` vierem de `403`/`ProxyError` e não de `504`, suspeite do **gateway
> Cortex** (`cortex-http.was.dev.br:44383`), não da DJEN nem do pool — ele
> **flapa** (caiu 100% por ~15min em 2026-06-17 e voltou). Teste rápido:
> ```bash
> docker compose -f docker-compose-prod.yml exec -T web python -c "
> import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
> import requests; from djen.proxies import ProxyScrapePool, cortex_proxy_url
> cx=cortex_proxy_url(ProxyScrapePool.singleton())
> r=requests.get('https://comunicaapi.pje.jus.br/api/v1/comunicacao',
>   params={'siglaTribunal':'TRF1','pagina':1,'itensPorPagina':5,'dataDisponibilizacaoInicio':'2024-01-02','dataDisponibilizacaoFim':'2024-01-03'},
>   proxies={'http':cx,'https':cx}, headers={'User-Agent':'voyager-ingestion/0.1'}, timeout=(8,30))
> print('cortex->DJEN', r.status_code)"
> ```
> `200` = Cortex ok (o problema é a DJEN mesmo). `ProxyError` = Cortex caído →
> a ingestão DJEN fica parada até o gateway voltar (o pool não cobre).

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

## Incidente: reabastecer saturou o DB (2026-07-01)

Sintoma: web em timeout (gunicorn travado em queries), `pg_stat_activity` com
dezenas de queries de 60–3000s empilhadas. Gatilho: ao adicionar 8 tribunais
novos (backlog de milhões cada), o `reabastecer_filas_enriquecimento`:
1. **rodava concorrente** — o pending-scan era lento e os runs do scheduler (2min)
   se sobrepunham; cada um passava o teste `len(queue) < high_water` → estouro
   (`enrich_tjmt` chegou a **387k** jobs p/ teto 100k);
2. **re-enfileirava PENDENTE já-em-fila** — o status só vira OK quando o drainer
   (async) aplica, então re-selecionava os mesmos a cada ciclo;
3. **pending-scan sem índice** — `WHERE tribunal_id=X AND status=PENDENTE` pegava o
   índice de status (milhões) e filtrava tribunal → **388s** por scan.

Fix (commits 2a81d3a + 0038):
- **lock Redis** no reabastecer (`cache.add('lock:reabastecer_enriquecimento')`) — 1 run por vez;
- query **sargável** (sem `ORDER BY pk`, que varria o espaço global de pk);
- **índice composto** `proc_trib_enriq_idx (tribunal, enriquecimento_status)` (migration 0038, `AddIndexConcurrently`) → pending-scan **388s → 0,09s**.

Alívio imediato (se recorrer): matar zumbis/queries longas + esvaziar filas —
`pg_terminate_backend` das ativas > 90s; `django_rq.get_queue('enrich_<sigla>').empty()`.
Diagnóstico do hog: seção "Storm de statement_timeout" acima.

Monitorar backfill: `pg_stat_activity` (nenhuma query deve passar de ~30s) +
profundidade das filas `enrich_*` (bounded pelo `QUEUE_HIGH_WATER=100k`). Escalar
os `worker_<sigla>` novos conforme o DB aguentar.

## Migration com AddIndexConcurrently — NÃO deixe o entrypoint do web rodar junto

**Incidente 2026-07-01:** rodei `migrate` (com `AddIndexConcurrently`) detached
DENTRO do container `web` e, logo depois, `restart web`. O restart matou o processo
do migrate → `CREATE INDEX CONCURRENTLY` abortou → deixou **índice inválido**
(`indisvalid=false`). Aí o **entrypoint do web roda `migrate --noinput` a cada
start** → tentou recriar o índice → `relation "..." already exists` → migrate falha
→ **web em crash-loop → site 502**.

Regras pra índice concurrent em tabela grande/quente (`tribunals_process` ~600M):
- **Nunca** rode o build via `exec -d web` e depois reinicie o `web` (mata o build).
- Rode o `CREATE/REINDEX INDEX CONCURRENTLY` num **container separado**
  (`docker compose run -d --rm --entrypoint python web ...`) que sobrevive a restarts,
  OU aplique a migration como `--fake` e construa o índice manualmente.
- `DROP INDEX` (ACCESS EXCLUSIVE) é **starvado** pelas leituras dos ~640 workers de
  enrichment (ACCESS SHARE). Prefira **`REINDEX INDEX CONCURRENTLY`** (SHARE UPDATE
  EXCLUSIVE, compatível com leituras) pra consertar husk inválido — e pause
  `scheduler`+drainers e mate transações longas (`xact_start` > ~10s) enquanto roda,
  senão o CONCURRENTLY espera indefinidamente por snapshots.
- Recuperar do crash-loop: `migrate --fake tribunals <NNNN>` (num container one-off
  `run --rm --entrypoint python`) → `restart web` (sobe limpo) → conserta o índice
  com REINDEX CONCURRENTLY.

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

## Deduplicação de Partes / índices únicos inválidos

`tribunals_parte` pode inflar se um índice único parcial virar inválido
(`indisvalid=false`) — o upsert do drainer deixa de deduplicar. Diagnóstico:

```bash
docker compose -f docker-compose-prod.yml exec -T web python manage.py check_parte_indexes
```

Exit 1 + linhas `INVÁLIDO:` → rodar a remediação (janela de manutenção):

1. `pg_dump` de `tribunals_parte` + `tribunals_processoparte` (backup).
2. Parar os drainers: `docker compose -f docker-compose-prod.yml stop enrichment_drainer enrichment_drainer_p0 enrichment_drainer_p1 enrichment_drainer_p2 enrichment_drainer_p3`.
3. `python manage.py dedup_partes --group all --dry-run` (conferir contagens), depois sem `--dry-run` (leva horas; resumível por grupo).
4. `python manage.py migrate tribunals` — recria os índices únicos e verifica `indisvalid`.
5. `python manage.py check_parte_indexes` → deve dar `OK`.
6. Recalcular `Parte.total_processos` (UPDATE agregando `tribunals_processoparte`).
7. Religar os drainers (`up -d` dos mesmos serviços).

Causa raiz conhecida: `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS` que
falha na validação deixa um husk inválido e o `IF NOT EXISTS` faz retries
pularem — ver `.ia/ENRICHMENT.md`.

## Donut "Distribuição por tipo" das Partes com rótulos sem sentido

Sintoma: `/dashboard/partes/` mostra dezenas/centenas de fatias no donut
"Distribuição por tipo" com nomes de papel processual (`agvdo/agvte`,
`apelda/apelte`, `rqte/qlte`, `procuradoria`, ...) em vez das 4 categorias
canônicas (Advogado / Pessoa Jurídica / Pessoa Física / Sem Identificação).

Causa raiz (corrigida 2026-06-10): `enrichers/esaj.py` (TJSP/TJAL) gravava o
**papel** processual cru em `Parte.tipo`. O código já emite `papel` separado +
`tipo` canônico. A limpeza histórica **não é um UPDATE simples**: como o e-SAJ
mascara doc, o lookup de dedupe sem-doc usava `tipo` na chave → a mesma entidade
(INSS, Fazenda) virou N Partes (uma por papel). Recategorizar pra `desconhecido`
colide na `uniq_parte_sem_doc_nem_oab (nome,tipo)`. Por isso o command
`recategorizar_tipo_partes` faz um **dedup-merge** (FASE 1: funde por nome,
repointa ProcessoParte; FASE 2: normaliza tipo; FASE 3: recalcula
total_processos) — é **operação de janela de manutenção** (1ª execução
2026-06-10: 1,53M → 0 não-canônico, ~265k Partes fundidas).

```bash
# 1) JANELA: pare os drainers e o scheduler (warm jobs pesados saturam IO e
#    arrastam o merge; drainers escrevendo PP durante o merge = race).
docker compose -f docker-compose-prod.yml stop scheduler \
  enrichment_drainer enrichment_drainer_p0 enrichment_drainer_p1 \
  enrichment_drainer_p2 enrichment_drainer_p3
#    Mate warm jobs longos órfãos (pgbouncer mantém a query mesmo após stop):
#    pg_terminate_backend dos client backends com query 'WITH ativo'/'MIN(...tribunal_movimentacao'.

# 2) BACKUP DIRECIONADO (reversível, barato — não precisa pg_dump de 36GB):
#    bkp_retipo_losers_parte / _survivors / _losers_pp / _withdoc (ver histórico do commit).

# 3) Dry-run e run (DETACHED — exec -d; senão ssh-timeout orfaniza a query
#    server-side via pgbouncer):
docker compose -f docker-compose-prod.yml exec -T web python manage.py recategorizar_tipo_partes --dry-run
docker compose -f docker-compose-prod.yml exec -d web sh -c \
  'python manage.py recategorizar_tipo_partes > /tmp/retipo.log 2>&1'
#    Acompanhe: docker compose exec -T web sh -c 'tail -f /tmp/retipo.log'

# 4) Religue scheduler + drainers; reaqueça o donut:
docker compose -f docker-compose-prod.yml start scheduler enrichment_drainer \
  enrichment_drainer_p0 enrichment_drainer_p1 enrichment_drainer_p2 enrichment_drainer_p3
docker compose -f docker-compose-prod.yml exec -T web python -c \
  "import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup(); \
   from dashboard import queries; print(queries.compute_distribuicao_tipos_partes())"
```

Gotchas aprendidos (já tratados dentro do command):
- **`statement_timeout=20s`** default da conexão (pgbouncer) cancela os statements
  pesados → o command usa `SET LOCAL statement_timeout` alto em `transaction.atomic`
  (único que cola sob transaction-pooling).
- **`ANALYZE _retipo_map`** após criar o índice — sem stats o planner seq-scaneia
  os ~19GB de `tribunals_processoparte` por batch (horas).
- **`_pp_del`** deleta também o loser que colidiria com o PP do próprio survivor
  (mesmo bug latente existe no `dedup_partes`).

Validação: `SELECT tipo, count(*) FROM tribunals_parte GROUP BY tipo` deve
voltar **só** `pf`/`pj`/`advogado`/`desconhecido`. Backlog do stream cresce na
janela (drainers parados) e drena depois (~4k/min). Tabelas `bkp_retipo_*`
podem ser dropadas após confirmar o resultado.

## 500 na página de detalhe de parte mega-agregada (INSS, União, Fazenda)

Sintoma: `/dashboard/partes/<id>/` retorna `Internal Server Error` pra partes
gigantes (ex: pk 47 = INSS, 3.4M `ProcessoParte`). No log do `web`:
`SystemExit: 1` via `gunicorn handle_abort` (timeout 60s) numa query
`GROUP BY` sobre `tribunals_processoparte`.

Causa: `parte_detail` fazia 4 agregações `GROUP BY` full-scan (10-26s cada p/
o INSS) + lista ordenada por join (`-processo__ultima_movimentacao_em`, ~11s)
sincronamente no worker. Corrigido (2026-06-10): guard
`PARTE_DETAIL_AGG_LIMIT` (50k) em `dashboard/views.py` — acima do teto, omite
donuts/counts/chips e ordena a lista por `-id` (instantâneo via índice).
Hot-deploy só de `web` (view + template, sem migration).

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
ssh ubuntu@192.168.30.103 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python manage.py shell -c \"
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
ssh ubuntu@192.168.30.103 \
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

## Dashboard de saúde do pipeline — operação

### Refresh manual da MV `mv_pipeline_diario`

Se o heatmap estiver desatualizado (ou após restaurar dump):

```bash
docker compose -f docker-compose-prod.yml exec -T web python -c \
  "from django.db import connection; c=connection.cursor(); c.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario')"
```

O refresh automático roda às 03:00 via `refresh_materialized_views` (scheduler inline). Ver ADR-017.

### MV `mv_ingestion_rate_hora` (gráfico "Velocidade de ingestão")

Alimenta o gráfico de movs inseridas/hora da overview. **Não** entra no
`refresh_materialized_views` diário — tem refresh dedicado
`refresh_ingestion_rate_hora` (scheduler inline, ~30min, janela de 4d). Um
gráfico rolante de 24-72h não pode depender de refresh 1x/dia.

> **Incidente 2026-05-28**: a MV ficou 41h velha (estava no refresh diário de
> 7d, que estourava o `statement_timeout` de 3600s sob carga). Resultado: o
> gráfico mostrou "Sem ingestão nas últimas 24h" enquanto a ingestão rodava
> normal (11M+ movs inseridas/24h). Não era parada de ingestão — era MV stale.
> Correção: refresh dedicado 30min + janela 7d→4d (migration 0034) + read
> resiliente (mostra "métrica defasada", não falso "sem ingestão").

Refresh manual (a MV é tabela pequena; o custo é o scan de 4d, ~min):

```bash
docker compose -f docker-compose-prod.yml exec -T web python -c \
  "from django.db import connection; c=connection.cursor(); c.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY mv_ingestion_rate_hora')"
```

Diagnóstico de staleness (compara MV vs inserts ao vivo):

```bash
docker compose -f docker-compose-prod.yml exec -T web python -c "
import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from django.db import connection
from django.utils import timezone
from datetime import timedelta
from tribunals.models import Movimentacao
with connection.cursor() as c:
    c.execute('SELECT max(hora) FROM mv_ingestion_rate_hora'); mx=c.fetchone()[0]
print('MV max(hora):', mx, '| idade(h):', (timezone.now()-mx).total_seconds()/3600 if mx else None)
print('inserido_em última 1h:', Movimentacao.objects.filter(inserido_em__gte=timezone.now()-timedelta(hours=1)).count())
"
```

### Storm de `statement_timeout` nos warm jobs (queries pesadas)

Sintoma: logs do `scheduler` cheios de `canceling statement due to statement
timeout` em vários warm jobs (`warm_kpis`, `warm_charts_*`, `warm_tribunal_status`),
páginas com cache frio/pending. **Não é DB sem recurso** — em geral são poucas
queries de agregação carésimas que não terminam e estrangulam as outras (poucas
conexões ativas, muitas idle).

Diagnóstico (achar a query que segura o DB):
```bash
docker compose -f docker-compose-prod.yml exec -T web python -c "
import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from django.db import connection
with connection.cursor() as c:
    c.execute(\"SELECT now()-query_start dur, wait_event_type, left(regexp_replace(query,E'\\\\s+',' ','g'),80) FROM pg_stat_activity WHERE state='active' AND datname='voyager' AND pid<>pg_backend_pid() ORDER BY query_start LIMIT 20\")
    [print(r) for r in c.fetchall()]
"
```
Várias linhas com a MESMA query + `IPC/MessageQueueSend` = 1 query com parallel
workers (leader + N). Use `EXPLAIN` (sem ANALYZE) da query suspeita pra ver o plano.

> **Recorrência 2026-06-10** (commit 5e98dd4): `compute_estatisticas_por_tribunal`
> (warm `warm_estatisticas_tribunal`) ficou pra trás do fix de 2026-05-29 — ainda
> fazia `COUNT` e `MIN/MAX(data) GROUP BY tribunal_id` em ~614M (full scan
> paralelo ~20min, EXPLAIN cost 16M). Rodando a cada ciclo de warm, saturava o
> IO e fazia o **login** (POST grava sessão) estourar o timeout do gateway —
> sintoma "timeout ao acessar/logar" sem nada errado no health. Fix: lê totais da
> `mv_tribunal_kpis`, movs 30d da `mv_volume_diario` (`dia<=hoje` corta data-lixo
> do Datajud ano 2913), e primeira/última via seek no índice
> `(tribunal_id, data_disponibilizacao)` — espelha o `compute_tribunal_status`.
> Resultado: 20min → 4s. (Resta o COUNT de `meio_completo` sem filtro em ~614M,
> menor — otimizar igual se o storm voltar.) Diagnóstico do hog: ver query
> `MIN(...tribunal_movimentacao` longa em `pg_stat_activity`; mate o leader
> client-backend com `pg_terminate_backend` pra alívio imediato.

Causas-raiz já corrigidas (2026-05-29) — padrão a evitar em queries de dashboard:
1. **`DISTINCT` da linha inteira**: `Process.filter(...).distinct().count()` virava
   `SELECT DISTINCT process.*` (32 colunas) + Sort gigante. Use
   `COUNT(DISTINCT processo_id)` sobre a tabela já filtrada.
2. **Filtro não-sargável**: `data_disponibilizacao__date__gte=<date>` faz
   `CAST(... AS date) >=` e **ignora o índice btree** → scan de ~600M. Use
   `data_disponibilizacao__gte=<datetime>` (sargável).
3. **`COUNT(*)` exato em tabelão**: headline KPI usa `pg_class.reltuples`
   (`queries._reltuples`), não COUNT exato em ~600M.
4. **`TruncMonth`/`TruncDate` ao vivo em ~600M**: servir de MV. Hoje:
   - diário (`volume_temporal` <=365d) ← `mv_volume_diario`
   - mensal (`volume_temporal` None + `compute_tribunal_status`) ← `mv_volume_mensal`
   Ambas no `refresh_materialized_views` diário. 1º refresh pós-migration é
   não-concorrente (MV `WITH NO DATA`); os readers caem pra live até popular.

### Significado das cores

| Cor | Significado | Threshold |
|---|---|---|
| Verde | Volume normal | ≥ 60% do baseline |
| Amarelo | Volume abaixo da mediana | ≥ 20% e < 60% do baseline |
| Vermelho | Anomalia / possível falha | < 20% do baseline (dia útil) |
| Cinza | Sem atividade esperada | Fim de semana ou sem baseline ainda |

Baseline = mediana das últimas 4 ocorrências do mesmo tipo de dia (seg/ter/.../dom) por tribunal/fonte.

### Falso-vermelho em feriado forense

Feriados forenses (Corpus Christi, feriados estaduais, recesso) **não estão** em calendário —
o sistema não distingue "dia sem atividade esperada" de "dia com falha de ingestão".
Resultado: dia de feriado com volume zero fica **vermelho**.

Isso é um **falso-positivo aceito** (calendário de feriados está fora de escopo).
Ao ver vermelho num feriado conhecido: ignore ou correlacione com o heatmap de outros tribunais
(se todos ficaram vermelhos no mesmo dia → provável feriado).

## Adicionar tribunal novo (TJMG, TJSP, etc)

1. Criar `Tribunal` (sigla, sigla_djen, data_inicio_disponivel, ativo=True)
2. Verificar se Datajud tem index — `api_publica_<sigla>` (ex: `api_publica_tjmg`)
3. Verificar se DJEN aceita a sigla (manualmente: `curl -G 'https://comunicaapi.pje.jus.br/api/v1/comunicacao' --data-urlencode 'siglaTribunal=TJMG' --data-urlencode 'dataDisponibilizacaoInicio=2026-04-15' --data-urlencode 'dataDisponibilizacaoFim=2026-04-15' --data-urlencode 'pagina=1'`)
4. Backfill DJEN — APScheduler já enfileira automaticamente (1 cron diário + tick_backfill_retroativo)
5. Datajud sync acontece automaticamente via auto-enqueue (`_enfileirar_todos_enrichments` na ingestão DJEN), mas pra acelerar vale enfileirar em massa
6. Classificação roda automaticamente após Datajud sync; cron `reclassificar_recentes` cobre o restante
7. Sem ground truth do tribunal novo, modelo TRF1 é aplicado mas precision real é desconhecida — calibration plot na `/dashboard/leads/` revela depois que Juriscope começar a consumir + marcar `validado/sem_expedicao`

**Caveat estaduais**: o patch de `datajud.sync_processo` agora popula `Process.classe_codigo` quando vazio — necessário pra TJ* funcionarem. Se o tribunal novo não estiver no Datajud, classe fica vazia e classificador retorna NAO_LEAD em todos.
