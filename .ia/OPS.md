# Operação

Runbooks específicos por situação. Para troubleshooting geral, comece por `djen_status` (CLI) ou `/dashboard/workers/` (UI).

Observabilidade de infra (Prometheus/Grafana/GPU): ver [Observability stack (Fase B)](#observability-stack-fase-b) no fim deste doc.

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
| `voyager-workers-2` | `192.168.30.104` | nova | **DESTRUÍDA 2026-07-17** (`qmdestroy` no gravserver — liberação de RAM; host oversubscrito). Era o 2º host de workers RQ. Recriar = decisão pendente. | — |
| `voyager-workers-aux` | `192.168.1.24` | antiga (pve antigo) | **Desativado** (offline desde ~2026-06-09) — era worker auxiliar na subnet antiga via Tailscale. | `docker-compose-workers.yml` |
| `voyager-worker-mac` | Wi-Fi `192.168.200.37` (estático) · cabo `192.168.1.13` | **ilha** — nenhuma alcança `192.168.30.x` | **Nó GPU** (Mac mini M4, 24GB unificados) — serve os GGUF do extrator via Metal em `100.105.16.107:800{1,2,3}`. **NÃO é worker RQ**: sem Django, sem Docker, não toca Postgres/Redis. Ver [`GPU_MACOS.md`](GPU_MACOS.md). | — (LaunchDaemons) |

> **`voyager-worker-mac` está ilhado** (2026-08-07): tem duas interfaces — Wi-Fi
> `192.168.200.37` (fixado estático; era DHCP e trocava de IP no reboot) e cabo
> `192.168.1.13` (rota default) — e **nenhuma delas alcança `192.168.30.x`**.
> Todo tráfego dele com o Voyager passa por **Tailscale** (`100.105.16.107`).
> É nó de **GPU/serving**, não entra no fleet de app.

**Fleet de app são 2 hosts** (desde 2026-07-17): `voyager` (.103, web) + `voyager-workers` (.102). A `.104` foi destruída em 2026-07-17. Workers conectam DB/Redis via **LAN**. O drainer do stream **só roda no `voyager`** (.103). Na `.102`, `worker_ingestion` roda com **24 réplicas** via `--scale` (2026-07-29, compensando a .104 — **não persistido** no compose; `up -d` sem a flag volta pra 8). Validado 75min sob carga: ~310 runs/h, 0 aberturas de circuit-breaker, ~1,3% de 500 da DJEN, 26GB RAM livres.

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
worker_diarios          2       1g        diarios        (3ª porta — ver nota abaixo)
```

> **`worker_diarios` (2026-08-20)** — consumidor da fila `diarios` (DJE/TJSP,
> DEJT, STF). Sobe **OCIOSO de propósito**: quem enfileira é o agendamento, que
> continua atrás de `DIARIOS_SCHEDULER_ENABLED` (default `False`). A unidade de
> trabalho é um CADERNO inteiro — por isso fila separada da `djen_*`: um job
> desses na fila da ingestão empurraria a fronteira diária pro fim da linha.
> Dimensionamento medido em 20/08/2026 (a conta completa está no comentário do
> `docker-compose-workers.yml`): 44,8 s de CPU por caderno de 4.229 páginas,
> pico de 125 MB de RSS no pipeline, 113 MiB em repouso ⇒ `mem_limit 1g` e 2
> réplicas (2 dos 24 vCPU da `.102`, que já roda com load average 12).
> **Só existe na `.102`.** Requer `pymupdf` na imagem — o `.103` ainda não tem
> (ver `.ia/DIARIOS.md` §8).

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

### Incidente Datajud: API pública throttled + fila explode (2026-07-02)

**Sintoma:** fila `datajud` cresce sem drenar (chegou a 63M jobs, Redis 39G/48G
`noeviction` → risco de apagão geral). `_search`/`_count` da API pública do CNJ
**penduram** (timeout, http=000) — mesmo direto, via Cortex e via pool (todos os
IPs). Endpoints admin dão 403 rápido (chave é válida). Diagnóstico: a **APIKey é
pública/compartilhada, com rate limit GLOBAL por chave**; 640 workers sem pacing
estouraram a quota. **Trocar proxy NÃO resolve** (limite é por chave, não por IP).

**Contenção (nesta ordem):**
```bash
# 1) pausar o enfileiramento (auto-enqueue + reabastecer)
#    setar no .env de TODOS os hosts + force-recreate (restart NÃO relê .env):
echo 'DATAJUD_ENQUEUE_ENABLED=false' >> ~/voyager/.env
docker compose -f docker-compose-prod.yml up -d --force-recreate web scheduler          # .103
docker compose -f docker-compose-workers.yml up -d --force-recreate worker_ingestion worker_default  # .102/.104
# 2) recuar workers (não adianta martelar a chave morta)
docker compose -f docker-compose-workers.yml up -d --scale worker_datajud=4 worker_datajud
# 3) purgar a fila SEM bloquear o Redis (NÃO usar q.empty() — Lua gigante trava tudo):
#    loop LPOP count=10000 + UNLINK (async), com sleep. Ver scripts/_purge2.py do incidente.
```

**Mitigação estrutural (já no código):**
- **Rate-limiter global** `datajud/ratelimit.py` (token-bucket Redis) no `_post` do
  client, sob `DATAJUD_RATE_LIMIT_RPM` (default 100). `<=0` desliga.
- **Escopo:** auto-enqueue e `reabastecer_fila_datajud` só pra tribunais SEM
  enricher (os 16 de `TRIBUNAIS_COM_ENRICHER` pegam classe/assunto do enricher).
- **Bound compartilhado** `_fila_datajud_cheia` (high-water 100k) agora no refill
  **E no auto-enqueue** da ingestão (`djen/ingestion.py`) — antes só o refill
  tinha teto; o auto-enqueue sem bound foi o vetor da explosão de 63M (2026-08-04).
- **Priorização por SPREAD** (2026-08-04): `reabastecer_fila_datajud` deixou de ser
  FIFO/heap (que deixava os TJs gigantes no fim = 33d de defasagem) e dá **cota por
  tribunal elegível** (newest-first) → todo tribunal drena em paralelo, TJSP não
  engole o lote.
- **Watcher** `datajud_queue_watch` (scheduler, 5min): registra profundidade em
  cache `datajud:queue_depth` e loga 🔴 se passar do teto de alerta
  `DATAJUD_QUEUE_CEILING` (300k) — o olho que faltou no 02/07 (fila foi a 63M sem
  ninguém ver).

**Religar quando a API recuperar** (testar `_count` direto; se responder 200):
`DATAJUD_ENQUEUE_ENABLED=true` no .env de TODOS os hosts + **force-recreate**
(restart NÃO relê .env). Se a fila estiver enorme de um incidente anterior, **purgar
primeiro** (senão o refill priorizado nunca assume — pula em fila ≥ high-water). O
rate-limiter + os bounds mantêm sob quota; o watcher avisa se re-explodir.

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
diarios          — coleta de diários próprios (DJE/TJSP, DEJT, STF, DOEs de entes)
```

> **`diarios` ainda NÃO tem worker em prod** (agosto/2026). A fila é separada de
> propósito: a unidade de trabalho é um caderno de até 2.001 páginas / 62 MB, e
> um job desses na fila do DJEN empurraria a fronteira diária para o fim da
> linha. O agendamento nasce desligado (`DIARIOS_SCHEDULER_ENABLED=False`) e há
> **kill switch por fonte**: `manage.py diarios_pausar --tudo` para em segundos,
> sem deploy. Runbook completo em [`DIARIOS.md`](DIARIOS.md).

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

## Diários próprios (3ª porta) — parar e ver

Runbook completo em [`DIARIOS.md`](DIARIOS.md); aqui só o que se digita às 3h da
manhã quando alguém do outro lado reclama:

```bash
# PARAR AGORA (efeito em segundos, chave no Redis, sem deploy)
docker compose exec web python manage.py diarios_pausar --tudo
docker compose exec web python manage.py diarios_pausar dejt        # ou só uma fonte
docker compose exec web python manage.py diarios_pausar --listar
docker compose exec web python manage.py diarios_pausar --religar --tudo

# Ver o estado do catálogo/coleta por fonte
docker compose exec web python manage.py diarios_status
```

Sintoma que mais engana: `por_status.inexistente` crescendo numa fonte que vinha
bem **não** é feriado forense em série — é o layout da fonte tendo mudado. É o
mesmo erro que o `_dia_coberto` do DJEN já pagou, e por isso `inexistente` é
status **terminal**: ele não é retentado, e uma fonte inteira pode se marcar como
"não existe" com o `IngestionRun` verde. Ver `DIARIOS.md` §4 e §8.

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

Cron `*/5 * * * *` em `djen.jobs.watchdog_ingestao` (fila `default`, `timeout=120`).
Faz auto-heal de 5 cenários:

0. **Código velho / cemitério cheio** (desde 21/08/2026): grita se o worker está
   rodando código anterior ao que está no disco, e se o `FailedJobRegistry` da
   `default` passou de `WATCHDOG_FALHAS_ALERTA` (200). Ver incidente abaixo.
1. **Zumbis**: `IngestionRun.status=running` e `finished_at IS NULL` há mais de 1h → marca FAILED + grava motivo. Worker que crashou e deixou rastro não trava o sistema.
2. **Backfill perdido**: pra cada tribunal ativo com `backfill_concluido_em IS NULL`, se nenhum job dele em execução → `tick_backfill_retroativo.delay(sigla)`. Recupera quando redis perdeu state ou backfill morreu.
3. **Daily atrasado**: pra tribunal com backfill ok mas sem `IngestionRun success` há >26h → `run_daily_ingestion.delay(sigla)`.
4. **Dia de recuperação órfão**: `ressuscitar_dias_de_recuperacao()` devolve pra
   `djen_backfill` o dia avulso (`f2:<SIGLA>:<dia>`) que ficou `failed` sem
   ninguém refazer. Teto `RECUP_POR_TIQUE=200`/tique, janela `RECUP_JANELA_DIAS=7`.

**Orçamento de tempo.** O job tem `timeout=120` no RQ, mas confere internamente
`WATCHDOG_ORCAMENTO_S=90` antes de cada etapa: se o tempo acaba, o que ficou de
fora sai como **ERRO com o número real** (`etapas_puladas`) em vez de morrer por
`JobTimeoutException` — que apaga o relatório inteiro. Toda leitura roda sob
`SET LOCAL statement_timeout` de `WATCHDOG_SQL_TIMEOUT_S=20`s
(`_leitura_com_teto`), porque o pgbouncer em transaction-mode descarta `SET`
solto.

Medido em 21/08/2026 com o banco sob carga de batch: as 4 leituras somam **1,6s**
(zumbis 0,02s · 59 tribunais 0,53s · agregação de falhos 0,18s · de sucessos
0,04s) e 200 `enqueue` custam **0,17s** (0,8 ms cada). Orçamento de 90s é ~50×.

Rodar manualmente (heal imediato sem esperar 5min):
```bash
ssh 100.100.144.57 'docker exec voyager-web-1 python -c "
import django, os
os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\",\"core.settings\")
django.setup()
from djen.jobs import watchdog_ingestao
print(watchdog_ingestao())
"'
```

Output — saída REAL de 21/08/2026, 1,38s de execução contra orçamento de 90s.
Quem abre este runbook no meio de um incidente lê o bloco, não o parágrafo:
**a chave de leitura está dentro dele.**

```python
{'zumbis_matados': 4,           # runs travados >1h que o tique matou
 're_backfill': ['TJAM'],       # tribunais cujo tick foi reenfileirado
 're_daily': [],
 'etapas_puladas': [],          # NÃO-VAZIO = banco em contenção; o tique
                                #   terminou pela metade (e disse o que faltou)
 'codigo': {'checado': True, 'modulos_velhos': 0, 'mtime_mais_novo': ...},
                                # modulos_velhos > 0 = deploy que NÃO recarregou
                                #   neste worker (ver a seção abaixo)
 'frota': {'checado': True, 'total': 250, 'velhos': 244, 'atraso_horas': 150.2},
                                # velhos alto COM atraso_horas de dias = a frota
                                #   não reiniciou. Horas soltas após deploy = normal
 'falhas_no_registry': 0,       # > 200 vira ERRO: algo morrendo em série
 'dias_recuperacao_reenfileirados': 200,
 'recuperacao': {'orfaos': 2574,        # órfãos que AINDA NÃO estão a caminho
                                        #   (o que já virou `f2:` na fila sai da conta)
                 'devolvidos': 200,     # enfileirados NESTE tique (teto RECUP_POR_TIQUE)
                 'sobraram': 2374,      # ficam pros próximos tiques
                 'vagas_na_fila': 7532}}
```

> ⚠️ **São DOIS relógios, e confundi-los é o erro fácil às 3h da manhã.**
> `devolvidos`/`sobraram` medem **enfileiramento** — a 200/tique de 5 min,
> 3.007 órfãos levam ~75 min só pra ENTRAR na fila. A **coleta** é outro
> relógio: cada job é um dia inteiro de um tribunal, então `orfaos` no banco só
> cai depois, bem mais devagar. Medido em 21/08: em 24 min a fila foi de 33
> para 1.408 jobs `f2:` enquanto os órfãos no banco caíram só de 3.007 para
> 3.001. **Fila enchendo com órfãos ainda altos é o watchdog funcionando**, não
> quebrado. O ERRO do teto diz as duas coisas na mesma linha, de propósito.

`sobraram > 0` por muitos tiques seguidos, com a fila já drenada, aí sim ⇒ algo
está falhando em série na `djen_backfill` (ver o OOM do TJDFT em
[`INGESTION.md`](INGESTION.md)).

### ⚠️ Deploy que não recarrega — worker rodando código de 7 dias atrás (21/08/2026)

**O incidente mais silencioso do projeto até aqui.** Os dois `voyager-worker_default`
subiram em **14/08 17:40** e nunca mais reiniciaram. O bind-mount `.:/app` faz o
`git pull` do host aparecer dentro do container, mas o processo Python **já tem os
módulos em memória** — `docker restart` é obrigatório e ninguém deu.

Como foi provado (sonda enfileirada na própria fila, sem shell no worker):

```python
q.enqueue('builtins.eval',
          "hasattr(__import__('djen.jobs',fromlist=['x']),"
          "'ressuscitar_dias_de_recuperacao')")   # -> False
q.enqueue('importlib.import_module', 'datajud.jobs')  # -> ImportError real
```

O que custou, medido:

| efeito | número |
|---|---|
| `ressuscitar_dias_de_recuperacao` (deploy 19/08) nunca executou | **3.007** dias-tribunal `failed` parados |
| `datajud/client.py` passou a importar `djen.proxies.sessao_rotativa` (só existia no disco) → `import datajud.jobs` quebrava no work-horse | **4.247** jobs mortos em ~30 ms, `reabastecer_fila_datajud` parado 3 dias |
| falhas acumuladas na `default` em 4 dias | ~**1.390/dia**, ninguém viu |

O RQ traduz o `ImportError` do módulo em `ValueError: Invalid attribute name: X`
— mensagem que esconde a causa. Use a sonda `importlib.import_module` acima pra
ver o erro de verdade.

**Cura:** `docker restart` nos containers do worker. **Prevenção:** duas
travas novas no watchdog, uma local e uma de frota:

1. `_alerta_codigo_velho` — compara o mtime de todo `.py` do projeto carregado
   em `sys.modules` com o `starttime` do processo **pai** (`/proc/<ppid>/stat`;
   o work-horse forkado nasceu agora e não serve de régua). ERRO
   `watchdog: WORKER RODANDO CÓDIGO VELHO` com nome dos módulos e idade.
2. `_alerta_workers_velhos` — o RQ já publica `birth_date` de cada worker no
   Redis. Worker nascido antes do `.py` mais novo do disco está, por
   construção, com código velho. ERRO `watchdog: N de M workers da frota
   nasceram ANTES do código mais novo`, com as filas afetadas. Isto pega o
   resto dos ~300 containers, não só o que roda o watchdog — no dia do
   incidente os workers das filas `datajud` e `es_index` também estavam
   parados no código de antes de 19/08.

> Ressalva honesta do item 2: o mtime é o do disco do host onde o watchdog
> roda. Hosts puxam o repo em momentos diferentes, então é **indício forte, não
> prova** — confira com `docker ps` (coluna `Up N days`) antes de reiniciar.

**Calibração do item 2** (medida logo após o deploy de 21/08): `Worker.all()`
devolveu **251 workers, 244 (97%) nascidos antes do `.py` mais novo**. É
verdade e é inútil como alarme — ninguém reinicia 250 containers a cada commit,
e alarme sempre aceso é alarme gasto. O que separa o incidente do dia-a-dia é a
ESCALA: os `worker_default` estavam com código de SETE dias. Por isso:

| atraso do worker mais antigo | nível |
|---|---|
| < `FROTA_ATRASO_ALERTA_H` (72h) | WARNING — "ainda não recarregaram" |
| ≥ 72h | **ERROR** — deploy sem restart, com as filas afetadas |

```bash
# ver os alertas sem reiniciar nada
ssh 100.98.141.91 'docker logs --since 30m voyager-worker_default-1 2>&1 \
  | grep -E "CÓDIGO VELHO|nasceram ANTES"'

# quem está de pé há mais tempo que o último deploy
ssh 100.98.141.91 'docker ps --format "{{.Names}}\t{{.Status}}" | sort -k2'
```

### Censo e limpeza do FailedJobRegistry

`get_job_ids()` devolve o sorted set na ordem de **expiração**, não de tempo —
olhar "os 5 mais recentes" aponta pro job errado (foi o que fez 11 timeouts de 4
dias atrás parecerem "o watchdog morrendo toda vez"). Sempre censo completo:

```bash
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py djen_censo_falhas'
# depois de medir:
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py djen_censo_falhas default --apagar'
```

O comando separa **id órfão** (o hash do job expirou e sobrou o id no zset) do
job real — medido na `djen_backfill`: 3.952 de 4.297 (92%) eram só isso. Contar
os órfãos como falha é inventar problema.

Descartar o registry não perde trabalho de ingestão: a fonte de verdade do que
precisa voltar é `IngestionRun(status='failed')`, e quem devolve é o watchdog.

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

## Acervo DB (Zordon) fora — VM OOM-kill / IP errado no boot (2026-07-26)

O DB do acervo/RAG (busca + vetorização + `/analisar`) é a **VM 102 `zordon-db`** no
**Proxmox `root@179.127.17.228`** (nó `faec2a4zbp`, 251GB RAM; painel
`https://179.127.17.228:8006`). PG 18.4 em `192.168.30.114:5432`. **Não** é o
`voyager-db` (.101, Postgres do app) — é infra do Zordon.

**Sintoma:** busca/`/analisar` em 504; da LAN, `192.168.30.114` sem ping, sem SSH,
"No route to host" (o host inteiro some, não só o PG). Outros hosts da LAN respondem.

**Causa raiz dupla:**
1. **Morte = OOM-kill do HOST.** No Proxmox, `dmesg -T | grep -i oom` mostra
   `global_oom` matando o kvm da 102. O host fica **oversubscrito** (Σ RAM das VMs >
   físico) e o **KSM** (dedup que segura o overcommit) estava OFF → 1º pico de memória
   mata a MAIOR VM.
2. **Volta difícil = IP errado.** A VM sobe mas o netplan era **DHCP** → pega lease novo
   (ex. `.125`) em vez do `.114`. `qm start` diz "running" e o console (login prompt) OK,
   mas ARP p/ `.114` fica INCOMPLETE. Todo o sistema aponta pra .114 → não acha ninguém.

**Runbook (o guest tem `qemu-guest-agent` → conserta SEM senha):**
```bash
ssh root@179.127.17.228
qm list | grep 102                 # status da VM
qm start 102                       # se stopped
qm agent 102 network-get-interfaces | grep -A2 ip-address   # IP real do guest (agent)
# se IP errado, restaurar .114 ao vivo (sem login):
qm guest exec 102 -- ip addr add 192.168.30.114/24 dev ens18
echo "screendump /tmp/x.ppm" | qm monitor 102   # console (sem serial → screendump)
dmesg -T | grep -iE "oom|killed process"        # confirmar OOM
```
Verificar do zordon: `ping 192.168.30.114` + `SELECT 1` + `pg_is_in_recovery()` (deve
ser `False`; PG é WAL/ACID → crash-recovery automático, sem perda de dado).

**Prevenção aplicada (2026-07-26) — não deve mais repetir:**
- **IP estático `.114`** no netplan do guest (`/etc/netplan/00-installer-config.yaml`,
  `dhcp4:false`, gw .1, DNS 1.1.1.1/8.8.8.8; backup em `/root/netplan-backup-preincidente.yaml`).
  Validado num reboot real: sobe sozinho em .114.
- **VM 80GB** (era 98) + **swap do host 39GB** (era 7, 100% cheio) + **KSM** via
  `ksmtuned` (enabled; liga dinâmico sob pressão — `run=0` com RAM sobrando é normal).
- **Guard de auto-restart**: `vm102-guard.timer` (systemd no Proxmox, 2min) →
  `qm start 102` se cair. Log: `journalctl -t vm102-guard`.

## Vetorização "lenta/parada" — checar os WRITERS antes de escalar GPU (2026-07-27)

**Sintoma:** dashboard mostra vetorização rastejando (`rate_10min`≈0, `done` não avança),
buffer `vetorizar_write` (Redis) parado/subindo, mas as GPUs de embed ocupadas. Instinto
errado = "falta GPU / subir pods". **Quase sempre é o DRENO (writers) desligado.**

**Pipeline:** pods de embed (compute) produzem → buffer Redis `vetorizar_write` →
`vetorizar_writer` (LAN, no zordon) drena → `INSERT acervo_chunk`. Se os writers estão OFF,
os pods embedam pra um buffer que ninguém esvazia → nada chega no DB. **Aconteceu:** os 8
writers ficaram OFF ~1 dia (pausados p/ um build de índice HNSW e nunca religados).

**Diagnóstico (via `ssh ubuntu@zordon`):**
```bash
systemctl is-active zordon-vetorizar-writer@{1..8}.service   # devem estar active
# buffer drenando? (django shell) contar rq:job:vetwrite-* 2x com sleep — deve CAIR
curl -s http://127.0.0.1:8011/api/vetorizacao/fleet | python3 -c 'import sys,json;d=json.load(sys.stdin);print("rate_10min",d["rate_10min"])'
```

**Fix / prevenção (feito):** os writers agora são **systemd `zordon-vetorizar-writer@1..8`**
(`Restart=always`, **enabled** → sobrevivem a reboot e a morte). Nunca mais ficam off
calados. Religar/escalar: `sudo systemctl start zordon-vetorizar-writer@{1..8}`.

**Teto real:** cada `INSERT acervo_chunk` leva **14-40s** (HNSW+FTS, DB I/O-bound) → teto
~30k chunks/h. Mais writers dão retorno **sub-linear** (contendem no mesmo disco); mais pods
de embed = só enche o buffer. **O gargalo é a ESCRITA no DB**, não GPU. Unlock durável =
`.ia/UNLOCK_VETORIAL_ARQUITETURAL.md` (LanceDB). Docs grandes (mediana 18MB/proc, milhares
de chunks) → `docs/h` baixo mas `chunks/h` alto é ESPERADO. Ver memórias
`db-contencao-busca-vs-batch`, `frota-pods-quickpod-realidade`.

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

## VM do Elasticsearch — acesso e expansão de disco

**Não há SSH na VM do ES** (`192.168.30.128`) — `root`, `ubuntu`, `elastic` e
`debian` dão `Permission denied`. O caminho é pelo **hipervisor**:

```bash
ssh root@179.127.17.228          # Proxmox — acesso por chave já configurado
qm list                          # VM 101 = voyager-search (o nó ES)
qm agent 101 ping                # guest-agent ativo ⇒ dá pra executar dentro
qm guest exec 101 -- /bin/bash -c "df -hT /"
```

`qm guest exec` devolve JSON com `out-data`; para ler bonito:

```bash
ssh root@179.127.17.228 'qm guest exec 101 -- /bin/bash -c "COMANDO"' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['out-data'])"
```

### Expandir o disco (feito em 15/08/2026: 1,5 TB → 3 TB)

O dado do ES fica na **raiz** (`/dev/sda2`, ext4, GPT). Depois de aumentar o
disco no Proxmox, o guest **não** cresce sozinho: a GPT continua descrevendo o
disco antigo (`last-lba` velho), então `lsblk` mostra `sda 3T` mas `sda2 1.5T`.

```bash
# tudo ONLINE — não para o ES, não precisa reboot
qm guest exec 101 -- /bin/bash -c "sfdisk -d /dev/sda > /root/sda-tabela-\$(date +%F).bak"
qm guest exec 101 -- /bin/bash -c "growpart --dry-run /dev/sda 2"   # confere antes
qm guest exec 101 -- /bin/bash -c "growpart /dev/sda 2"             # arruma a GPT + estica
qm guest exec 101 --timeout 900 -- /bin/bash -c "resize2fs /dev/sda2"
```

Antes de mexer, confirme que o ext4 aceita crescer montado:

```bash
tune2fs -l /dev/sda2 | grep -iE "reserved gdt|filesystem features"
```

`resize_inode` + `Reserved GDT blocks: 1024` (com bloco de 4K) dão folga até
~8 TiB online. Sem `resize_inode`, seria preciso desmontar — ou seja, parar o ES.

**Conferir pelo lado do ES** (ele relê o filesystem sem restart):

```bash
curl -s 192.168.30.128:9200/_nodes/stats/fs?pretty | grep -A2 total_in_bytes
```

> `cluster/health` volta **yellow** nesse nó e isso é normal: é um nó único, e as
> réplicas não podem ser alocadas nele mesmo. Yellow aqui não é sintoma de
> problema de disco — não confundir os dois durante uma operação.

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

## ⚠️ NUNCA rodar `cache.clear()` em produção

Parece inofensivo e não é: o Redis guarda caches **caros de reconstruir**, que
existem justamente porque a query original é pesada demais pro Postgres
contido. Em 12/08/2026 rodei `cache.clear()` pra invalidar um agregado de 2min
e derrubei junto o `cobertura_enriquecimento:v1` — o "% já analisado" da
dashboard e do mapa virou "não sabemos ainda" em TODAS as UFs, porque quem
repõe é um job de scheduler que só roda de 6 em 6 horas.

Cura: `docker exec voyager-web-1 python manage.py shell -c "from dashboard
import tasks; tasks.warm_cobertura_enriquecimento()"` (rode **detached**, leva
~1min e tem lock próprio). Confirme com `agg_overview._cobertura_por_uf()`
devolvendo 28 UFs.

Em vez de limpar tudo, apague a chave específica —
`cache.delete('comercial:agg:uf:v1')`. ⚠️ **`delete_pattern` NÃO existe** neste
backend (`RedisCache` do Django, não django-redis): a chamada levanta
`AttributeError` e, se estiver dentro de um `try`, você acha que limpou e não
limpou. Monte a chave na mão (o mapa usa
`comercial:agg:uf:f:{md5(json.dumps(filtros, sort_keys=True))}`).

**Gotcha irmão**: o agregado do mapa cacheia por combinação de filtro com TTL
de 2min. Se você medir algo, deployar e medir de novo em menos de 2 minutos,
vai reler o valor ANTIGO e concluir que o deploy não pegou. Já aconteceu.

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

## Observability stack (Fase B)

Prometheus + postgres_exporter + Grafana + Alertmanager + gpu-bridge pro ambiente
de milhões de embeddings (vector store do **acervo Zordon**). Cobre o §2.2 do
`.ia/RAG_EVAL_OBS.md`. Stack **isolado** (project name `voyager-obs`, rede/volumes
próprios, portas não-conflitantes) — **não toca** os compose de prod.

Fonte versionada: `infra/observability/` (compose, prometheus, grafana, alertmanager,
gpu-bridge, postgres-exporter/queries.yaml). Secrets ficam num `.env` **não versionado**
(gitignored) no host de deploy.

### Onde roda / topologia

| Peça | Host | Porta (host) | Observação |
|---|---|---|---|
| Prometheus | `zordon` (192.168.30.117) | **9490** → :9090 | TSDB, retention 15d, `--web.enable-lifecycle` (reload sem restart) |
| Grafana | `zordon` | **9491** → :3000 | dashboard "Infra / Vector Store" (uid `voyager-infra-vs`), datasource provisionada |
| Alertmanager | `zordon` | **9493** → :9093 | receiver default vazio (preencher webhook interno pra notificar) |
| postgres-exporter | `zordon` | interno (:9187) | aponta pro **acervo DB** `192.168.30.114:5432/zordon` como `voyager_exporter` (READ-ONLY) |
| gpu-bridge | `zordon` | interno (:9835) | lê hash Redis `192.168.30.100/3` `vetor:gpu`+`extracao:gpu` → `/metrics` |
| node-exporter | `zordon` | interno (:9100) | telemetria do próprio host do stack |

**Por que `zordon`**: host coordenador de baixo risco (NÃO é o DB, NÃO é o web, NÃO
está no hot-path de enrichment). Alcança o acervo DB e o Redis pela LAN. Foi o único
host-change: **Docker foi instalado nele** (não tinha). Regra de segurança respeitada
— nada roda no host do DB.

> ⚠️ O **acervo DB** que morde (embeddings/HNSW) é o **zordon** (`192.168.30.114:5432`,
> **PostgreSQL 18.4**), NÃO o `voyager-db` (.101, PG16 do DJEN/leads). São bancos
> diferentes. O postgres_exporter aponta só pro acervo.

### URL do Prometheus pro Command Center

O Command Center (camada Voyager) consulta via `/api/v1/query`:

```
http://zordon:9490            (Tailscale, recomendado)
http://192.168.30.117:9490    (LAN)
```

Verificado alcançável do host web (`voyager` .103). Grafana em `http://zordon:9491`
(admin / senha no `.env`). Query de exemplo pros ~8 números-chave:
`GET http://zordon:9490/api/v1/query?query=voyager_gpu_bridge_up`

### Métricas-chave (PromQL) — o que já está VIVO

| # | O que | PromQL |
|---|---|---|
| 1 | cache-hit HNSW (leading indicator disk-cliff) | `rate(hnsw_index_io_blks_hit[15m]) / clamp_min(rate(hnsw_index_io_blks_hit[15m]) + rate(hnsw_index_io_blks_read[15m]), 1)` |
| 2 | tamanho índice HNSW vs shared_buffers | `hnsw_index_io_size_bytes` · `db_shared_buffers_shared_buffers_bytes` |
| 3 | dead tuples (bloat) acervo_chunk | `acervo_table_bloat_n_dead_tup{table_name="acervo_chunk"}` |
| 4 | autovacuum ativo + duração | `autovacuum_progress_running_seconds` |
| 5 | conexões / xact longa | `connections_summary_active` · `connections_summary_max_xact_seconds` · `connections_summary_idle_in_txn` |
| 6 | fila vetorizar_write | `voyager_rq_queue_depth{queue="vetorizar_write"}` |
| 7 | GPU util/vram/stale por host | `voyager_gpu_util` · `voyager_gpu_mem_used_bytes` · `voyager_gpu_stale` |
| 8 | saúde do pipeline de métricas | `voyager_gpu_bridge_up` · `up{job="postgres_acervo"}` |

> **Nota naming**: as métricas de query custom do postgres_exporter v0.16 (via
> `queries.yaml` deprecado) saem **SEM** o prefixo `pg_` (ex: `hnsw_index_io_*`,
> `connections_summary_*`). As built-in do exporter mantêm `pg_`.

Estado ao subir (2026-07-24): `chunk_emb_hnsw_half` = **38 GB** de índice vs
`shared_buffers` = **24 GB** → índice já **maior que a RAM de buffer** (o disk-cliff
que o `.ia/RAG_EVAL_OBS.md` §10 e a memória preveem). O painel de cache-hit é o
alarme antecipado disso.

### Métricas PENDENTES (outra fase / janela)

- **p95/p99 busca vetorial** — BLOQUEADO: `pg_stat_statements` **não** está habilitado
  no acervo DB (`shared_preload_libraries` vazio). Ligar exige `shared_preload_libraries`
  + **restart do Postgres = janela de prod**. Não feito de propósito (regra dura). Quando
  agendar a janela: adicionar `pg_stat_statements` ao preload, `CREATE EXTENSION`, e ligar
  o collector do exporter.
- **custo/h por pod** — PENDENTE: exige QuickPod cost exporter (API → pushgateway).
  Item futuro da Fase B.
- **recall drift** — PENDENTE: depende da **Fase C** (eval harness / `EvalRun`).
  Placeholder documentado em `alerts.yml`.

### Alertas (Alertmanager) — os 7 do §6

Regras em `infra/observability/prometheus/alerts.yml`. Vivos (calculáveis já):

1. **FilaWriteRunawayComAutovacuum** (COMPOSTO) — `vetorizar_write > 20k` **E**
   autovacuum rodando >120s em `acervo_chunk`. Isolados não alertam; juntos = vacuum
   estrangulando o writer. Ação: pausar/segurar o writer ou deixar o vacuum terminar;
   ver "reabastecer saturou o DB" pra padrão de contenção.
2. **HNSWCacheHitCaindo** — cache-hit do índice HNSW < 90% por 15min (só quando há
   leitura de disco). Leading indicator do disk-cliff. Ação: subir RAM/shared_buffers,
   index-last, halfvec, pgvectorscale/VectorChord (ver RAG_EVAL_OBS §10 + memória).
3. **DBSaturado** — active>40 OU xact>90s OU idle-in-txn>10. Ação: `pg_terminate_backend`
   das xact longas; ver "Storm de statement_timeout".
4. **GPUHang** — `voyager_gpu_stale==1` (telemetria > 180s parada) por 5min. GPU travada,
   pod morto ou reporter caído. Ação: checar o pod/host e o `vet_gpu_report`.
5. **GPUBridgeDown** / **PostgresExporterDown** — saúde do próprio pipeline de métricas.

Pendentes (documentados/comentados em `alerts.yml`): **p95 SLO** (dep. pg_stat_statements),
**custo/h pod** (dep. QuickPod exporter), **recall drift** (dep. Fase C).

Notificação: `alertmanager.yml` tem receiver `default` **vazio** (sem SaaS US, soberania).
Pra ligar avisos, preencher `webhook_configs`/`slack_configs` apontando pra endpoint
interno. **Nunca** mandar conteúdo de autos por aqui (só nomes de alerta/labels).

### Usuário read-only do Postgres

Criado no acervo DB (`zordon` @ .114) via `sudo -u postgres psql` (path sancionado):

```sql
CREATE ROLE voyager_exporter WITH LOGIN PASSWORD '***' CONNECTION LIMIT 5;
GRANT pg_monitor TO voyager_exporter;      -- só lê pg_stat_*; nenhum acesso a dados
GRANT CONNECT ON DATABASE zordon TO voyager_exporter;
```

- NÃO é superuser, NÃO tem createrole, só `pg_monitor`. O exporter **nunca** usa superuser.
- (`pg_exporter` é nome reservado no PG18 → por isso `voyager_exporter`.)
- Senha vive só no `.env` do host (`infra/observability/.env`, gitignored).

### Operar (deploy / reload / reverter)

```bash
# ---- deploy (no host zordon) ----
ssh ubuntu@zordon
cd ~/voyager-obs                       # dir sincronizado de infra/observability/
# .env precisa existir com PG_EXPORTER_PASSWORD / GRAFANA_ADMIN_PASSWORD / GPU_BRIDGE_REDIS_PASSWORD
sudo docker compose -p voyager-obs -f docker-compose.observability.yml up -d --build

# ---- status ----
sudo docker compose -p voyager-obs -f docker-compose.observability.yml ps

# ---- reload de alerts/scrape (hot, sem restart) ----
sudo docker exec voyager-obs-prometheus-1 wget -qO- --post-data="" http://localhost:9090/-/reload

# ---- reverter (derruba tudo; NÃO afeta prod, é stack isolado) ----
sudo docker compose -p voyager-obs -f docker-compose.observability.yml down          # mantém volumes
sudo docker compose -p voyager-obs -f docker-compose.observability.yml down -v        # + apaga TSDB/grafana
# opcional: dropar o role read-only
ssh ubuntu@192.168.30.114 "sudo -u postgres psql -d zordon -c 'DROP ROLE IF EXISTS voyager_exporter;'"
```

Atualizar config: editar em `infra/observability/` no repo → `rsync` pra `~/voyager-obs`
no zordon → `up -d` (ou reload pro que é hot). Trocar senha do exporter: `ALTER ROLE
voyager_exporter WITH PASSWORD '...'` + atualizar `.env` + `up -d --force-recreate
postgres-exporter`.

### Incidente: migration `atomic=False` não-registrada → web crash-loop → 502 (2026-08-10)

**Sintoma:** site 502; `docker compose ps web` = `Up 2 seconds` repetindo (restart loop).
Logs do web: `ProgrammingError: column "X" of relation "tribunals_process" already
exists` no `migrate` do entrypoint.

**Causa:** migration com `atomic = False` (necessário p/ `AddIndexConcurrently`)
aplicada MANUALMENTE, mas o registro em `django_migrations` **não gravou** (o processo
do migrate foi interrompido/morto após o DDL commitar). Como o **entrypoint do web roda
`migrate --noinput` a cada start**, ele re-tenta a migração → `AddField`/`CREATE INDEX`
falha ("already exists") → boot falha → restart loop → nginx sem upstream → 502.
⚠️ Pegadinha do `atomic=False`: aplicação PARCIAL — os objetos (coluna/índice) existem
mas a migração fica "não aplicada" pro Django.

**Fix:** marcar como aplicada SEM re-rodar, via container one-off (o web está em loop):
```bash
docker compose -f docker-compose-prod.yml run --rm --entrypoint python web \
  manage.py migrate --fake tribunals <NNNN>
docker compose -f docker-compose-prod.yml up -d --force-recreate web   # sai do loop
curl -s -o /dev/null -w '%{http_code}' https://voyager.was.dev.br/dashboard/   # 302 = ok
```

**Prevenção:** depois de aplicar migration `atomic=False` MANUALMENTE, confirmar
`manage.py showmigrations tribunals` mostra `[X]` (não só que a coluna existe). E
**checar o site logo após qualquer `force-recreate web`** — o 502 aqui ficou mascarado
como "saturação do banco" (o web em loop + build de índice CONCURRENTLY spikam conexões/
IO); o DB estava OK (0 queries ativas). Diag do DB sem depender do pool esgotado:
`docker exec voyager-postgres-1 psql "postgresql://voyager:$PW@192.168.30.101:6432/pgbouncer" -c "SHOW POOLS"`.

### Incidente: fila `es_index` — zumbi + .env + imagem velha no .102 (2026-08-11)

Três causas empilhadas descobertas ao ligar o write-through ES do drainer
(`indexar_processos_bulk`). Sintomas e curas, na ordem em que mordem:

1. **`ModuleNotFoundError: No module named 'elasticsearch'`** nos workers do .102 —
   a imagem local `voyager-web:prod` do .102 era ANTERIOR à entrada da lib no
   requirements (mesma pegadinha do dev container). O .102 builda a PRÓPRIA imagem:
   ```bash
   ssh ubuntu@voyager-workers 'cd ~/voyager && docker build -t voyager-web:prod . && \
     docker compose -f docker-compose-workers.yml up -d --force-recreate worker_es_index'
   ```
2. **`NameResolutionError: 'elasticsearch:9200'`** — o `.env` do .102 NÃO tinha
   `ELASTICSEARCH_URL` e o client caía no default. Fix: `ELASTICSEARCH_URL=http://192.168.30.128:9200`
   no `~/voyager/.env` do .102 + force-recreate.
3. **Worker ZUMBI no .103** — container avulso `worker_es_index` (fora do compose,
   criado manualmente semanas antes) com código stale consumia a fila e envenenava
   ~26 jobs/min por 9 dias. Sintoma: FailedJobRegistry da `es_index` crescendo
   continuamente mesmo com os workers do .102 sãos. Cura: `docker rm -f <id>` +
   requeue (`FailedJobRegistry(queue).requeue(jid)` — o job é idempotente por `_id`).
   ⚠️ Se aparecer worker de fila que "ninguém subiu", procure containers fora do
   compose nos DOIS hosts: `docker ps --format '{{.Names}} {{.CreatedAt}}' | grep -v voyager-`.

**Pendente (recomendação do QA):** watcher do failed-registry da `es_index` (mesmo
padrão do `datajud_queue_watch`) — o zumbi passou 9 dias invisível.

### Gotcha: `docker exec -d` morre no restart do container (2026-08-11)

Jobs longos lançados com `docker exec -d` no web (backfills, reindex) MORREM em
qualquer `restart`/`force-recreate` do web — inclusive deploys de rotina. Já matou o
backfill Fase 0 (1×) e o reindex TJSP (2×) nesta sessão. Regra: antes de restartar o
web, `docker top voyager-web-1 | grep -E 'backfill|reindex'`; se houver job, esperar ou
relançar depois (comandos são resumíveis: backfill é idempotente, reindex tem
`--desde-id`; lance com `> /tmp/<job>.log 2>&1` pra ter o checkpoint).

### Incidente: `Errno 24` (FDs) por cache de proxy — coleta caindo em silêncio (2026-08-17)

**Sintoma.** `worker_djen_audit` no `.102` morrendo em
`ProxyError(... NewConnectionError(... [Errno 24] Too many open files))`, e
**2.981 `IngestionRun` marcados `failed` pelo watchdog** na mesma janela de 7
dias ("status=running sem finished_at por >1h — worker crashou"). Nenhum OOM no
`dmesg`, RAM folgada — não era memória.

**Causa.** `session.get(url, proxies={...})` faz a `requests` guardar um
ProxyManager **por URL de proxy** em `adapter.proxy_manager` — um dict que
nunca encolhe. Giramos sobre centenas de IPs do pool; cada IP queimado deixa
seu conjunto de conexões pendurado. Com `nofile=1024` (default do Docker) o
processo esgota os descritores e passa a falhar **todo** request seguinte,
inclusive os que iriam pra proxy saudável. Em `_ingest_day_por_uf` são 8
fetchers de UF dividindo UMA sessão — 8× mais rápido pra estourar.

Por que é grave e não é "só um erro": a falha some dentro do retry, o run vira
`failed`, o watchdog anota, e o dia fica **coletado pela metade** sem ninguém
olhar. É perda de cobertura silenciosa — o defeito nº 1 da lista do CLAUDE.md.

**Cura (as duas metades, ambas necessárias).**
1. `djen.proxies.sessao_rotativa()` — sessão com `AdaptadorProxyLimitado`, um
   LRU de `MAX_PROXY_MANAGERS=32` que fecha o pool do proxy mais antigo.
   Todo cliente que manda `proxies=` usa ela (djen, datajud, enrichers PJe/
   e-SAJ/TJMT/TJPA, diários). `tests/test_proxy_fd_leak.py` barra a volta do
   `requests.Session()` cru por varredura do fonte.
2. `ulimits.nofile: 65536` nos 20 services de worker que fazem HTTP externo
   (`docker-compose-workers.yml`). Só `worker_ingestion` tinha — alguém já
   havia batido nisso antes e consertou um serviço só.

⚠️ Mudança de `ulimits` **exige recriar o container** (`up -d`, não `restart`).

**Como conferir depois:**
```bash
ssh ubuntu@192.168.30.102 'docker exec voyager-worker_djen_audit-1 sh -c "ulimit -n"'   # 65536
ssh ubuntu@192.168.30.102 'docker logs --since 1h voyager-worker_djen_audit-1 2>&1 | grep -c "Errno 24"'  # 0
```

### Passada de entidades do índice de busca — operar

A extração de OAB/CPF/CNPJ/CNJ do texto das publicações (`es_movs_v2
--entidades`) é **retomável**: todo doc lido recebe o carimbo `ents_v`, então
morrer e terminar se tratam igual — relançar continua de onde parou.

```bash
# ver o que falta (o número que importa)
curl -s -X POST 'http://192.168.30.128:9200/voyager-movimentacoes/_count' \
  -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must_not":[{"exists":{"field":"ents_v"}}],
       "filter":[{"bool":{"should":[{"match_phrase":{"body":"OAB"}},
       {"match_phrase":{"body":"CPF"}},{"match_phrase":{"body":"CNPJ"}},
       {"match":{"body":"advogado"}},{"match_phrase":{"body":"R$"}}],
       "minimum_should_match":1}}]}}}'

# lançar N faixas em paralelo, com relançamento automático (host .103)
nohup ~/voyager/scripts/entidades_supervisor.sh "0 1 2 3 4 5 6 7" \
      > /tmp/entidades_super.log 2>&1 &
tail -f /tmp/entidades_f0.log
```

⚠️ **Não passe de ~8 faixas simultâneas.** Com 16 o circuit-breaker do ES
estourou (heap 7,6 GB de 7,5 GB de limite) e 4 faixas morreram de uma vez. O nó
é único e também serve a busca de produção.

**Carimbo retroativo.** Os docs processados antes do carimbo existir foram
marcados por `_update_by_query` server-side (`ctx._source.ents_v = 1` onde já
havia alguma entidade). Sem isso a passada releria 21M docs à toa.

### Deadlock entre workers de backfill no `bulk_create` (18/08/2026)

Com 8 workers recuperando dias do MESMO tribunal em paralelo:

```
deadlock detected
DETAIL: Process A waits for ShareLock on transaction X; blocked by process B.
        Process B waits for ShareLock on transaction Y; blocked by process A.
CONTEXT: while inserting index tuple in relation "tribunals_process"
```

`cnjs_pagina` é `set`, então a ordem de inserção variava a cada processo.
Bastavam dois lotes com CNJs em comum, inseridos em ordens opostas, pra fechar o
ciclo de espera no índice único. O run falha corretamente (não é perda
silenciosa) — mas joga fora a coleta do dia inteiro: 24 páginas do TJSP
2023-08-24 se perderam assim.

**Cura:** ordem total igual em todo mundo — `sorted()` nos CNJs e sort por
`external_id` nas movimentações (o único `(tribunal, external_id)` tem o mesmo
problema). Sem ordem comum não existe ciclo.

**Conferir depois de mexer em concorrência de ingestão:**
```python
R.objects.filter(status='failed', erros__icontains='deadlock',
                 started_at__gte=<agora-1h>).count()   # tem que ficar em 0
```

### 179 milhões fora do índice com a fila em ZERO (18/08/2026)

**O sintoma era a ausência de sintoma.** A fila `es_index` marcava 0 e eu tinha
reportado "drenou, está em dia". Estava errado.

**Como medir de verdade** (contagem própria não prova nada — compare os dois
lados na MESMA faixa de id, que é a PK e portanto barata):

```python
# 10 janelas de 200k ids espalhadas pelo espaço todo
SELECT count(*) FROM tribunals_movimentacao WHERE id BETWEEN %s AND %s
POST /voyager-movimentacoes/_count  {"query":{"range":{"id":{"gte":a,"lte":b}}}}
```

Resultado: 9 de 10 janelas **idênticas**; a décima (a mais recente) 100% ausente.
O buraco não estava espalhado pela história — estava todo na CAUDA. Busca binária
achou a fronteira em **id 1.273.045.248**; acima dela, 190.320.885 no Postgres
contra 10.830.272 no ES → **179.490.613 publicações fora da busca**.

**Duas causas, e as duas precisam de conserto:**

1. `LIMITE_MOVS_NOVAS = 50.000` com tick de 10 min = teto de 300 mil/h, contra
   uma ingestão que sob recuperação escreve muito mais. Teto que corta calado —
   a regra nº 2 do CLAUDE.md. Corrigido: teto de 400k, freio pela FILA
   (`FILA_ES_ALTA`), e atingir o teto virou ERRO **com o tamanho do atraso**.
2. O watermark estava em 1.506.901.808, MUITO à frente da cobertura real: ele
   avançou sobre jobs que foram enfileirados e nunca chegaram ao ES (purga de
   fila, falhas). Subir o teto **não** recupera isso — precisa de reindex.

**Fechar o buraco** (`scripts/` não versiona isto; é operação pontual):

```bash
# 6 shards sobre [fronteira, topo], cada um com checkpoint próprio
nohup ~/reindex_shards.sh > /tmp/reindex_shards_super.log 2>&1 &
for i in 0 1 2 3 4 5; do tail -1 /tmp/reindex_s$i.log; done
```
Medido: 438 docs/s com um processo (4,7 dias), **~4.100/s com 6 shards**.

⚠️ **Nunca mais leia "fila zerada" como "índice completo".** Leia a fronteira.

### Sem RAM pra comprar: encolher o working set (18/08/2026)

O banco tem **1,7 TB** e ~22 GB de cache — working set **14× maior que a RAM**,
cache hit de **82%** no heap, e **41 de 44** queries ativas esperando I/O. O
disco entrega 2 GB/s (é NVMe, é rápido): o problema nunca foi vazão, foi **o
tamanho do que precisa caber na memória**.

Sem RAM nova, o ganho vem de tirar leitura do sistema, não de aumentar cache.
`pg_stat_statements` mostrou onde ela estava:

| GB do disco | chamadas | o quê |
|---:|---:|---|
| 436.191 | 6.476 | fila do reclassificador (**67 GB por chamada**) |
| 393.605 | 33.420 | contador do dashboard |
| 247.732 | 15.735 | gráfico diário do dashboard |

**Consertos (migration `tribunals/0050_indices_io`):**

* índice **parcial** pra fila do reclassificador. A condição de um índice
  parcial aceita comparação entre colunas da mesma linha — é o que resolve
  `classificacao_em < ultima_movimentacao_em`, que nenhum B-tree comum indexa.
  Ocupa **56 MB** porque guarda só as linhas pendentes;
* índice composto `(classificacao, criada_em)` no log — igualdade primeiro,
  faixa depois;
* drop de 19 GB de índice com **zero** scans na vida do banco.

**Medido depois, com EXPLAIN ANALYZE em produção:**

```
fila do reclassificador ... 3,8 MB e 144 ms    (era 67 GB)
contador do dashboard ..... 32 KB e 1,7 ms
gráfico diário ............ 56 KB e 0,07 ms
```

**Runbook — sempre nesta ordem.** `CREATE/DROP INDEX CONCURRENTLY` espera TODAS
as transações abertas, e o reclassificador segura transações de ~37 min. Ignorar
isso derrubou o site por 50 minutos em 10/08:

```bash
# 1. parar quem segura transação longa
docker compose -f docker-compose-prod.yml stop scheduler          # .103
docker compose -f docker-compose-workers.yml stop worker_classificacao   # .102
# 2. cancelar SELECT longo (read-only, o job re-tenta) e CONFERIR que zerou
#    pg_cancel_backend(pid) para xact_start < now() - interval '30 seconds'
# 3. criar/dropar CONCURRENTLY, com o site sob observação
# 4. religar scheduler + worker_classificacao
```

**Como achar o próximo:** `pg_stat_statements` ordenado por `shared_blks_read`.
Query que lê dezenas de GB por chamada é falta de índice, não falta de hardware.

### `enqueue_in` sem scheduler = limbo silencioso (19/08/2026)

Adiar um job com `enqueue_in` só funciona se **algum worker daquela fila** rodar
com `--with-scheduler`. O RQ não promove agendado sozinho: sem scheduler, o
`ScheduledJobRegistry` só cresce.

Medido: **4.189 dias de recuperação com a hora vencida, parados**. Não viraram
falha — e é por isso que foi pior que falhar:

* nenhum alarme dispara (não há `failed` pra contar);
* o watchdog não pega, porque ele PULA quem está agendado de propósito, pra não
  duplicar o que já está a caminho;
* a fila fica curta e parece saudável.

O conserto anterior (adiar em vez de matar o dia) trocou "morrer" por "sumir em
silêncio", que é exatamente o padrão que este projeto persegue.

**Cura:** `command: python manage.py rqworker --with-scheduler djen_ingestion
djen_backfill` no `worker_ingestion`. Ao subir, ele drena o registro sozinho —
a fila saltou de 2.768 pra 6.936 em segundos.

**Conferir depois de qualquer mudança em worker que use `enqueue_in`:**
```python
from rq.registry import ScheduledJobRegistry
reg = ScheduledJobRegistry(queue=get_queue('djen_backfill'))
# quantos já venceram e continuam parados? tem que ser ~0
sum(1 for j in reg.get_job_ids()[:500]
    if (reg.get_scheduled_time(j) - now).total_seconds() < -60)
```

⚠️ Regra geral: **toda fila que recebe `enqueue_in` precisa de um worker com
`--with-scheduler`.** Hoje é só a `djen_backfill`.
