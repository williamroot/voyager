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

> **`worker_diarios` (2026-08-20; agendamento LIGADO em 24/08/2026)** —
> consumidor da fila `diarios` (DJE/TJSP, DEJT, STF). Nasceu **ocioso de
> propósito** e passou a receber trabalho em 24/08/2026, quando
> `DIARIOS_SCHEDULER_ENABLED=1` com `DIARIOS_FONTES_AGENDADAS=tjsp-dje` e
> orçamento de 8 unidades/24 h. Vazão medida: **90 itens/s por réplica**
> (213.630 itens em 2.129 s de CPU). Ver `.ia/DIARIOS.md` §13. A unidade de
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

**LIGADA desde 24/08/2026**, uma fonte de cinco: `DIARIOS_SCHEDULER_ENABLED=1`,
`DIARIOS_FONTES_AGENDADAS=tjsp-dje`, `DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE=8`
(nos `.env` da `.102` **e** da `.103`). Runbook completo, com os números medidos
e os 10 critérios de parada, em [`DIARIOS.md` §13](DIARIOS.md#13-ligada--o-que-foi-ligado-em-24082026-com-que-números-e-quando-desligar).

Quem roda o quê: o cron `diarios_tick_todas` (10 min) vive no **scheduler da
`.103`** e só enfileira; o `tick` em si roda no **`worker_default` da `.102`**,
e é lá que ficam o orçamento e as guardas. Coleta é `worker_diarios` (2
réplicas, só na `.102`). Mudou `.env`? **`up -d --force-recreate`** — `restart`
NÃO relê o `.env`.

Aqui só o que se digita às 3h da manhã quando alguém do outro lado reclama:

```bash
# PARAR AGORA (efeito em segundos, chave no Redis, sem deploy)
# ATENÇÃO: a fonte é POSICIONAL. `--pausar` não existe e o argparse recusa tudo.
docker compose exec web python manage.py diarios_pausar --tudo
docker compose exec web python manage.py diarios_pausar dejt        # ou só uma fonte
docker compose exec web python manage.py diarios_pausar --listar
docker compose exec web python manage.py diarios_pausar --religar --tudo

# Ver o estado do catálogo/coleta por fonte
docker compose exec web python manage.py diarios_status

# O sinal que aperta PRIMEIRO é o Elasticsearch, não o disco do banco nem a RAM:
curl -s 192.168.30.128:9200/_cat/allocation?v                  # DESLIGA em >= 85%
curl -s '192.168.30.128:9200/_cat/thread_pool/write?v'         # DESLIGA se `rejected` > 0
#   e a fila `es_index`: teto automático em 5.000 jobs (DIARIOS_FILA_ES_MAX)
```

Números do regime medido em 24/08/2026 com a coleta de 1 dia rodando (8
passadas de 60 s): índice a 103-1.268 docs/s, `write.rejected` **0** em 8/8,
pico da fila `es_index` **3.364** (67% do teto), busca com mediana **0,0325 s**
e pior máximo **0,295 s**, site 18/18 em HTTP 200.

⚠ **`docker logs` aceita UM container.** `docker logs -f a b` falha, manda o
`usage` pro stderr e **não devolve log nenhum** — e um `grep` em cima volta
vazio, que se lê como "o job nunca rodou". Em 24/08 isso quase inverteu a
conclusão sobre a 3ª porta (freada virou "morta"). Use laço, um por vez, e
valide a sonda contra um período que você SABE ter linha:

```bash
for c in voyager-worker_default-1 voyager-worker_default-2; do
  docker logs --since 55m $c 2>&1
done | grep -E 'tick_todas|orçamento'
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
   **Exceção medida (24/08/2026)**: run cujo job ainda está no `StartedJobRegistry`
   é POUPADO até 7h (`WATCHDOG_RUN_ZOMBIE_COM_JOB_S`). Dia grande passa de 1h com
   frequência — 11 de 170 runs que fecharam `success` em 12h (6,5%), o pior um
   TJSP de **357 min** —, e marcá-los "worker crashou" enchia o censo de fantasma
   e APAGAVA os `erros` já gravados no run. O `erros` agora é append.
2. **Backfill perdido**: pra cada tribunal ativo com `backfill_concluido_em IS NULL`, se nenhum job dele em execução → `tick_backfill_retroativo.delay(sigla)`. Recupera quando redis perdeu state ou backfill morreu.
3. **Daily atrasado**: pra tribunal com backfill ok mas sem `IngestionRun success` há >26h → `run_daily_ingestion.delay(sigla)`.
4. **Dia de recuperação órfão**: `ressuscitar_dias_de_recuperacao()` devolve pra
   `djen_backfill` o dia avulso (`f2:<SIGLA>:<dia>`) que ficou `failed` sem
   ninguém refazer. Teto `RECUP_POR_TIQUE=200`/tique, janela `RECUP_JANELA_DIAS=7`.
5. **Gate de completude** (desde 24/08/2026): `conferir_dias_fechados()` pega os
   dias que fecharam `success` desde o tique anterior e enfileira a conferência
   contra a FONTE na fila `djen_audit` — 20 baratos (`count_window`, 1
   requisição) e 1 caro (paginação até esgotar) por tique. Divergência vira
   `cobertura_divergente` no `IngestionRun.erros` + ERRO no log. Detalhe e custo
   medido em [`INGESTION.md`](INGESTION.md#success-não-é-prova--a-integridade-tem-que-ser-medida-24082026).

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

### O teto de 64 é MÁXIMO, não meta — e conferência divide a mesma banda

Medido em 24/08/2026, 17:05: com a frota em **42 streams** (14 réplicas x
`DJEN_PAGINAS_PARALELAS`=3) mais até 7 paginações do gate de completude e 3
provas manuais (`djen_provar_dias`), a DJEN começou a devolver `500 servidor via
cortex`, o contador de 5xx chegou a **40** e o **circuito abriu**.

O que aconteceu em seguida é o conserto de 19/08 funcionando, e vale registrar
como comportamento ESPERADO:

```
fila: 0 jobs · agendados (adiado por circuito): 358 · em execução: 0
```

**Nenhum dia foi perdido** — todos viraram `adiado:<sigla>:<dia>` com 330-450 s
de espera, e o `djen:circuit_open` tinha TTL de 145 s quando foi conferido. O
gate, no mesmo período, devolveu `{'skip': 'djen_circuito_aberto'}` em todas as
conferências, **sem marcar o dia como conferido** — elas voltam sozinhas.

Lição operacional: os 64 do teto são o ponto onde o estrago já é certo, não o
alvo. Conferência (gate, provas) usa a MESMA banda da coleta, e coleta tem
prioridade — por isso `GATE_PAGINADOS_EM_VOO`=2 e por isso a prova manual é
serial. Se precisar rodar prova em massa, pare/reduza a frota antes.

### A fila conta ids, não trabalho — faxina de casca

Sintoma: um tribunal não anda, a fila mostra jobs dele e o `FailedJobRegistry`
não cresce. Meça antes de concluir qualquer coisa:

```bash
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py djen_faxina_fila'
# fila `djen_backfill`: 1.945 ids · com payload 1.601 · CASCA 344 (17,7%)
#    adiado  TJRS      120     ← 100% do pendente do tribunal
#    bfd     TJAM      120
```

Conserto (remove a casca e reenfileira o dia de verdade; `--frente` põe na
frente da fila):

```bash
ssh 100.100.144.57 'docker exec voyager-web-1 \
  python manage.py djen_faxina_fila --consertar --frente'
```

⚠️ **Cuidado com `djen_censo_falhas --apagar`**: os ids do DJEN são
determinísticos, então o id que está no cemitério pode ser o MESMO que está
vivo na fila. Desde 24/08 o comando se recusa a apagar o hash nesse caso (e
diz quantos poupou) — mas versão antiga, ou limpeza feita à mão com
`delete_job=True`, produz exatamente as 344 cascas medidas naquele dia.

### Priorizar dias na fila SEM enfileirar de novo (24/08/2026)

Quando o trabalho já está enfileirado e o problema é **vazão**, reenfileirar é o
erro: duplica requisição contra o CNJ e não adianta um segundo. Medido em
24/08: os 156 dias que a tela contava como "nunca refeitos" já estavam **todos**
na `djen_backfill` (120 do TJRS como `adiado:`), em posição mediana **638 de
1.964** — ou seja, horas de espera atrás de backfill histórico.

O que resolve é reordenar. O `job_id` é determinístico, então dá pra tirar o id
da posição atual (LREM) e pô-lo na frente (LPUSH), sem criar job nenhum:

```python
q = django_rq.get_queue('djen_backfill')
started = set(StartedJobRegistry(queue=q).get_job_ids())   # não mexa em quem já roda
for jid in reversed(alvos):          # o último a entrar na frente sai primeiro
    q.remove(jid)                    # LREM: some da posição atual
    q.push_job_id(jid, at_front=True)
```

Feito em 24/08 com 151 + 54 jobs: fila continuou com **1.946** (nada duplicado)
e a concorrência contra a DJEN não mudou — continua `14 réplicas x
DJEN_PAGINAS_PARALELAS 3 = 42`, dentro do teto de 64. **Reordenar é o único
acelerador que não viola a desigualdade.**

⚠️ Nunca mova job que está no `StartedJobRegistry`: ele já está nas mãos de um
worker, e o LPUSH criaria uma segunda execução do mesmo dia.

### Provar que um dia fechou ÍNTEGRO (não só `success`)

`success` diz que o coletor terminou, não que trouxe tudo. Pra provar, pagine a
fonte na força bruta e confronte — os três lados na mesma tela:

```bash
# um dia específico
ssh 100.100.144.57 'docker exec voyager-web-1 \
  python -u manage.py djen_provar_dias --tribunal TJDFT --dia 2026-08-21'

# amostra ALEATÓRIA (semente declarada) dos dias fechados nas últimas 24h
ssh 100.100.144.57 'docker exec voyager-web-1 \
  python -u manage.py djen_provar_dias --n 6 --seed 20260824 --horas 24'
```

⚠️ **É caro e demora**: a prova baixa o dia inteiro (TJDFT 2026-08-21 = 822,6 MB
em ~250 requisições, dezenas de minutos). Rode DETACHED e leia depois — `docker
exec` sem `-d` morre junto com a sessão ssh:

```bash
ssh 100.100.144.57 'docker exec -d voyager-web-1 sh -c \
  "cd /app && python -u manage.py djen_provar_dias --n 6 --seed 20260824 \
   --json /tmp/prova.json > /tmp/prova.log 2>&1"'
ssh 100.100.144.57 'docker exec voyager-web-1 tail -30 /tmp/prova.log'
```

A prova é SERIAL (1 conexão) de propósito: o teto contra o CNJ é
`réplicas x DJEN_PAGINAS_PARALELAS <= 64` e a frota já usa 42 (14 x 3).

### O gate de completude achou divergência — e agora?

```bash
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py shell -c "
from tribunals.models import IngestionRun
qs = IngestionRun.objects.filter(fonte=\"djen\",
     erros__contains=[{\"erro\":\"cobertura_divergente\"}]).order_by(\"-finished_at\")[:20]
for r in qs: print(r.tribunal_id, r.janela_inicio, r.erros[-1])"'
```

1. `gap > 0` (a fonte tem mais que nós) ⇒ o dia perdeu conteúdo. Confirme com
   `djen_provar_dias --tribunal X --dia Y` (que também diz quanto está no BANCO)
   e reenfileire: `f2:<SIGLA>:<dia>` via `reprocessar_janela`;
2. `gap < 0` (temos mais que a fonte) ⇒ normal em dia reeditado pelo tribunal, ou
   publicação que saiu do ar. Não é perda; é informação;
3. `veredito: indecidivel_no_teto_de_10k` ⇒ o `count` bateu o teto e a
   conferência se absteve. A paginação chega nele pelo racionamento (1/tique) ou
   à mão, pelo comando.

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

### Reciclar a frota inteira sem derrubar produção (medido em 24/08/2026)

Runbook do que fazer quando o alerta acima acende com atraso de DIAS. Todos os
números abaixo foram medidos na `.102` (50 GiB de RAM, 24 vCPU, 240 containers)
e na `.103` (24 GiB, 28 containers) em 24/08/2026 — não são estimativas.

#### 1. Prove que o código está velho ANTES de reiniciar 240 containers

`docker ps` mostra a idade do CONTAINER, não a do código que está em memória —
e são coisas diferentes: o `git pull` no host entra pelo bind mount `.:/app`,
o processo Python não. A prova barata é uma **sonda enfileirada na própria
fila**, que roda dentro do work-horse e devolve o `sys.modules` real:

```python
# roda no web (.103), enfileira em TODAS as filas com at_front=True
INICIO = ("(__import__('time').time()"
          "-float(open('/proc/uptime').read().split()[0])"
          "+float(open('/proc/'+str(__import__('os').getppid())+'/stat')"
          ".read().rsplit(') ',1)[1].split()[19])"
          "/__import__('os').sysconf('SC_CLK_TCK'))")   # start do worker PAI
MODS = ("[(n,__import__('os').path.getmtime(m.__file__))"
        " for n,m in list(__import__('sys').modules.items())"
        " if getattr(m,'__file__',None) and str(m.__file__).startswith('/app/')"
        " and str(m.__file__).endswith('.py')]")
q.enqueue('builtins.eval', f"({INICIO},{MODS})", at_front=True, job_timeout=30)
```

Módulo com `mtime > início do processo` = código velho **em memória**. Saída
real de 24/08, antes da reciclagem:

| fila | worker nasceu | módulos velhos |
|---|---|---|
| `enrich_*` (16 filas), `datajud`, `pdf_download`, `varredura` | 18/08 00:29 | **14** — `core.settings`, `tribunals.models`, `djen.ingestion`, `djen.client`, `djen.jobs`, `diarios.base`, `diarios.models`, `api.*`, `dashboard.*` |
| `classificacao`, `manual`, `monitoring` (.103) | 14/08 17:36 | **21** (os 14 acima + `enrichers.pje/esaj`, `djen.proxies`, `search.client`, …) |
| `es_index`, `default` | 21/08 03:01 | 1–2 |

> **Duas ressalvas honestas da sonda.** (a) Ela só enxerga o que o worker
> importou **no boot**; módulo importado dentro do job é carregado fresco pelo
> fork e não aparece — a lista é piso, não teto. (b) `at_front=True` é
> obrigatório: `enrich_tjrj` tinha 411.603 jobs na frente. Fila cujos workers
> estão todos ocupados (`started == réplicas`) não responde em 120 s — isso é
> fila cheia, não worker morto.

A prova mais dura veio da fila `leads_consumo`: a sonda estourou com
`FileNotFoundError: '/app/search/agg_comercial.py'` — arquivo **renomeado em
13/08** e ainda em `sys.modules` do worker 11 dias depois.

#### 2. `up --force-recreate`, não `docker restart`

`docker restart` recarrega o CÓDIGO (bind mount) mas mantém a IMAGEM do
container. Medido: os ~170 containers de enriquecimento de 18/08 rodavam a
imagem `5d536a9d`, anterior ao rebuild de 20/08 20:18 que trouxe o PyMuPDF —
`import fitz` dentro deles dava `ModuleNotFoundError`. Use:

```bash
cd ~/voyager && docker compose -f docker-compose-workers.yml \
  up -d --no-deps --force-recreate --timeout 90 worker_tjrj worker_datajud
```

- `--no-deps` — não arrasta serviço nenhum junto.
- `--force-recreate` — pega imagem nova **e** relê o `.env` (o `restart` não relê).
- `--timeout 90` — **este é o número que salva trabalho.** É o grace do SIGTERM,
  e o RQ faz *warm shutdown*: termina o job em execução e só então sai. Com 90 s,
  as 16 filas `enrich_*` (146 jobs em voo) foram recicladas com **0 jobs
  abandonados** — `FailedJobRegistry` de todas continuou em 0. O preço é a onda
  demorar: as ondas com jobs longos levaram 95–105 s em vez de 10 s.
- Confira antes que `replicas:` no compose bate com o `docker ps` atual —
  `up` aplica o compose. Em 24/08 batia em todos os 26 serviços.

#### 3. Ordem e tamanho das ondas

Nunca `compose restart` global (incidente OOM de 06/2026). Ondas por SERVIÇO,
com trava de memória, nesta ordem:

| # | serviços | containers |
|---|---|---|
| 1 | `monitoring`, `djen_audit`, `pdf_download`, `varredura` | 20 — filas ociosas, valida o procedimento |
| 2 | `tjdft`, `tjac`, `tjro`, `tjap`, `tjpa` | 28 |
| 3 | `tjal`, `tjma`, `tjmt`, `tjpe`, `tjce` | 68 |
| 4 | `trf1`, `trf3`, `trf5`, `tjmg`, `tjsp` | 26 — gargalos declarados, onda própria |
| 5 | `tjrj` (24), `datajud` (24) | 48 |
| 6 | `es_index`, `diarios`, `classificacao` | 34 |
| 7 | **`worker_default` SOZINHO** | 2 |
| 8 | **`worker_ingestion` SOZINHO, `--timeout 600`** | 14 |

Os passos 7 e 8 são separados **de propósito**: o `worker_ingestion` roda com
`--with-scheduler`, obrigatório pro `enqueue_in` do adiamento por circuito
aberto (sem ele, 4.189 dias viraram limbo em 19/08). Derrubar os dois juntos
tira o watchdog e o scheduler do RQ ao mesmo tempo.

#### 4. Trava de memória — os números medidos

O medo é o OOM de host (06/2026). O que a medição mostrou é o **contrário** do
esperado: reciclar **libera** memória, porque o processo velho devolve o que
inchou e o swap dele volta pro pool.

```
.102 antes (14:38)  avail 15.453 MiB · used 34.733 · swap usado 7.389 · load1 9,96
.102 depois (15:05) avail 16.472 MiB · used 33.713 · swap usado    24 · load1 7,63
```

**7,3 GiB de swap devolvidos** — os 240 workers tinham ~100 MiB paginados cada.
Custo por onda, medido: **-64 a -426 MiB** de `MemAvailable` para ondas de 8 a
44 containers (~10–30 MiB por container: o worker novo é residente, o velho
estava metade em swap). Ondas de `tjdft`/`tjce`/`ingestion` deram delta
**positivo** (o container velho estava inchado: `tjdft` a 317 MiB, `ingestion`
a 257 MiB, contra ~120 MiB de um worker fresco).

Pisos usados no script (`~/onda.sh` nos dois hosts), que ABORTAM a onda:

| trava | valor | por quê |
|---|---|---|
| `MemAvailable` | **6.000 MiB** | a maior onda (44 containers) custa ~3,3 GiB no pior caso `mem_limit`; 6 GiB é ~2× isso |
| `SwapFree` | **300 MiB** | swap cheio + rajada de anon = OOM de host, não de container |
| `load1` | **40** | 24 vCPU; o normal sob carga é 8–16, e as ondas levaram a 16,6 no pico |

Tempo total: **240 containers da `.102` em 27 min** (14:38→15:05), sendo 10 min
só do `worker_ingestion` (o grace de 600 s). A `.103` (16 workers + 5 drainers
+ scheduler) levou **6 min**. Nenhuma fila parou: `djen_backfill` continuou
drenando (3.038 → 2.103) durante a janela.

#### 5. Validar — e o que NÃO acreditar

```bash
# o watchdog tem que rodar ONDE ele roda de verdade (fila default, na .102);
# rodá-lo dentro do web (.103) mede o mtime do disco ERRADO
docker exec -w /app voyager-web-1 python -c "
import sys; sys.path.insert(0,'/app')
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
import django_rq, time, json
j = django_rq.get_queue('default').enqueue('djen.jobs.watchdog_ingestao', at_front=True)
time.sleep(20); j.refresh(); print(json.dumps(j.result, default=str, indent=1))"
```

Resultado real de 24/08, depois das duas passadas:

```
frota: {"checado": true, "total": 256, "velhos": 0, "atraso_horas": 0.0}
                       antes:  total 250, velhos 215, atraso_horas 150.8
```

Três coisas que enganam, e enganaram de verdade neste dia:

1. **`total` sobe depois de reciclar.** Foi de 250 pra 256 porque 6 containers
   da `.103` (`worker_leads_consumo` ×4 e os órfãos `worker_monitoring` /
   `worker_pdf_download`, todos de 12/08 e fora do compose) **não apareciam** em
   `Worker.all()`. Estavam vivos e consumindo fila. O censo da frota não é o
   censo dos containers: confira `docker ps -q | wc -l` nos dois hosts.
2. **`velhos: 0` pode ser mentira.** O `birth_date` do RQ é UTC ingênuo e o
   container roda em UTC-3 — até 24/08 o watchdog contava todo worker como
   nascido **3 h no futuro** (ponto cego de 3 h; `atraso_horas` sempre 3 h a
   menos). Corrigido em `_epoca_utc`. Se você está lendo isto numa versão
   anterior, some 3 h ao `atraso_horas` antes de acreditar.
3. **Chegar a `velhos: 0` exige que ninguém dê `git pull` no meio.** Em 24/08
   dois deploys entraram às 15:13 e 15:31, no meio da reciclagem, e a frota
   voltou pra "velha" por 4 módulos. Antes da última passada, espere o disco
   parar: `find ~/voyager -name '*.py' -newermt '-3 minutes'` tem que sair vazio.

Custo total em trabalho perdido, medido nos registries antes/depois: **8 jobs**
abandonados em ~240 reinícios (3 `djen_backfill`, 3 `leads_consumo`, 1
`pdf_download`, 1 `default`), todos re-enfileiráveis — `enrich_*` ficou em 0
graças ao warm shutdown. Comparar com os 3.007 dias-tribunal parados do
incidente de 21/08 é o que dimensiona a decisão.

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

## Enriquecimento "lento": a fila não é o backlog (runbook 2026-08-25)

Sintoma que chega: "1,89 M de jobos nas filas `enrich_*` e só 145 rodando".
Antes de escalar worker, rode as três sondas abaixo — em 25/08/2026 elas
mostraram que **nada estava parado** e que dois terços do trabalho era cópia.

**1. Workers ociosos? (quase sempre a resposta é não)**
```bash
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python -c "
import sys;sys.path.insert(0,\"/app\")
import os,django;os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\",\"core.settings\");django.setup()
import django_rq;from rq import Worker
w=Worker.all(connection=django_rq.get_connection(\"default\"))
e=[x for x in w if any(q.startswith(\"enrich_\") for q in x.queue_names())]
print(len(e),\"workers de enrich,\",sum(1 for x in e if x.get_state()==\"busy\"),\"ocupados\")"'
# 25/08: 146 de 146 ocupados. "145 rodando" É a frota inteira, não um sintoma.
```

**2. O número redondo (~99.9xx) é `QUEUE_HIGH_WATER`, não o backlog.**
A fila é um buffer alimentado por `_reabastecer_impl`; o backlog de verdade é
`Process.enriquecimento_status='pendente'` no Postgres. Fila cheia significa
"o refill encheu até o teto", jamais "há só isso pra fazer".

**3. Quanto da fila é CÓPIA do mesmo processo?** (a sonda que achou o gargalo)
```bash
# censo COMPLETO da fila (não amostra): ids x process_id distintos
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python /tmp/dup_todas.py'
```
Em 25/08: 1.400.007 jobos para 321.130 processos distintos nas 14 filas que o
refill administra — **77,1% de cópia**, com `enrich_tjmt` a 70,4 cópias por
processo e um único pid do TJRO enfileirado **1.926 vezes**. Causa: a 100k de
profundidade a fila tem 20–74 h de espera (medir com o `enqueued_at` do job na
frente), e o `Process` só sai de `pendente` quando o drainer aplica — então o
refill re-seleciona a mesma cabeça do índice a cada 2 min. Corrigido com
profundidade-alvo medida + ZSET `enr:inflight:<sigla>`; ver `.ia/ENRICHMENT.md`.

**Espera real de cada fila (o número que dimensiona tudo):**
```bash
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python /tmp/sonda7.py'
# imprime enqueued_at do job na frente e no fim de cada fila enrich_*
```

### Tribunal que rende zero: separar "fonte bloqueada" de "rota errada"

`erro` em 100% NÃO quer dizer que o tribunal bloqueou. Teste as duas rotas
antes de pausar — o `curl` é a prova barata:
```bash
ssh 100.98.141.91 'docker exec voyager-worker_default-1 sh -c "
  curl -s -o /dev/null -w \"direto=%{http_code}\\n\" --max-time 25 <URL_DO_TRIBUNAL>
  curl -s -o /dev/null -w \"cortex=%{http_code}\\n\" --max-time 40 \
       -x http://cortex-http.was.dev.br:44383 <URL_DO_TRIBUNAL>"'
```
- **200 no Cortex, 403/erro no pool** ⇒ rota errada. Ligue o residencial pra
  esse tribunal: `manage.py enrich_cortex <SIGLA>` (chave `enrich:cortex_only`).
  Foi o caso do **TJAP** (pool 0 de 5 úteis a 7,91 s; Cortex 5 de 5 a 0,95 s).
- **403 nos dois** ⇒ bloqueio total. Kill switch: `manage.py enrich_pausa <SIGLA>`.
  Foi o caso do **TJRO** — `ok = 0` em 1,09 M de processos, desde sempre.

> Não deixar o tribunal bloqueado martelando não é higiene, é **vazão**: cada
> job gasta até 10 IPs do pool (`MAX_PROXY_ROTATIONS`) e cada 403 marca o IP
> como ruim por 120 s. TJRO+TJAP+TJAL queimavam ~58 mil IP/h, o que sustenta
> ~1.930 IPs marcados a qualquer instante. O pool medido: **2.500 na lista, 289
> saudáveis, 2.211 no `bad_zset`, `degraded=1`**. O pool é COMPARTILHADO — o
> desperdício de um tribunal derruba a coleta de todos.

**Saúde do pool (sempre olhar antes de culpar o tribunal):**
```bash
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python /tmp/px2.py'
# proxies na lista / saudaveis agora / bad_zset / degraded / fail_streak
```

### Purgar fila envenenada de cópias (não perde trabalho)

Depois de corrigir o refill, a fila antiga continua com as cópias já
enfileiradas — elas só saem sendo executadas (20–74 h de lixo). Esvaziar é
seguro: o backlog é o status no Postgres, e o refill repõe em 2 min.
```bash
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python -c "
import sys;sys.path.insert(0,\"/app\")
import os,django;os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\",\"core.settings\");django.setup()
import django_rq
for q in [\"enrich_tjmt\",\"enrich_tjac\"]:
    print(q, django_rq.get_queue(q).empty())"'
```
⚠️ Em fila de milhões NÃO use `q.empty()` (Lua gigante trava o Redis) — use o
loop `LPOP count=10000` + `UNLINK` do incidente Datajud de 02/07 acima.

### TJSP com 0,6% de `ok`: o tribunal tinha um SEGUNDO sistema (25/08/2026)

Sintoma que chegou: `enrich_tjsp` com **10.750 `nao_encontrado`/h e 0,6% de
`ok`**, no maior tribunal do acervo (≈ 17,25 M) e o nº 1 do produto. Fila
saudável (1,0 cópia), 146 de 146 workers `busy`, `FailedJobRegistry` em 0,
`enrich:pausados` vazio, e o mesmo enricher acertando 6 de 6 numa amostra.

A hipótese que chegou junto — "são processos novos demais, ainda não
consultáveis" — **foi refutada**. A matriz ano × desfecho AO VIVO (62
requisições, 3 s entre elas, amostra de semente 20260825) mostra que o ano não
discrimina nada; quem discrimina é o **primeiro dígito do sequencial do CNJ**:

```
estrato (prefixo, ano)   DETALHE_c/PARTES   LISTA   SEGREDO   NAO_EXISTE   n
prefixo 4 · 2026                        0       0         0            8   8
prefixo 4 · 2025                        0       0         0            8   8
prefixo 2 · 2025 (2º grau)              4       4         0            0   8
prefixo 1 · 2026                        5       1         2            0   8
prefixo 1 · 2024                        8       0         0            0   8
prefixo 1 · 2021                        7       1         0            0   8
prefixo 0 · 2025                        8       0         0            0   8
controle: marcados `ok` hoje            5       0         1            0   6
```

**O TJSP roda `eproc` em paralelo ao e-SAJ.** Os processos nascidos no eproc
têm sequencial de CNJ começando em `4`, e o e-SAJ simplesmente não os tem —
16 de 16 devolveram a MESMA página determinística de 70.439 bytes. Tamanho
medido (amostra de 15.443 linhas TJSP, semente 20260825, sobre os 16.326.948
com status):

| prefixo | % do TJSP | estimativa | `ok` | `nao_encontrado` | `pendente` |
|---|---:|---:|---:|---:|---:|
| 1 | 49,7% | 8.110.083 | 14,5% | 13,8% | 71,8% |
| 0 | 26,5% | 4.333.624 | 13,8% | 11,8% | 74,4% |
| **4** | **18,0%** | **2.940.182** | **0,1%** | 37,2% | 62,7% |
| 2 | 4,7% | 770.727 | 5,6% | 12,2% | 82,2% |

⚠️ O corte é `prefixo 4` **E** `ano >= 2025`. Prefixo 4 de 2013 ESTÁ no e-SAJ
e dá `ok` (33 de 33 numa janela de 45 min). Generalizar o prefixo apaga
processo bom.

**Como descobrir isso em QUALQUER tribunal — o método, que é o que vale:** o
`link` da própria publicação DJEN denuncia o sistema. Prefixo 4 aponta
`https://eproc1g.tjsp.jus.br/...` e `eproc2g`; prefixo 0/1 aponta
`https://www.dje.tjsp.jus.br`. É uma consulta por tribunal, recortada:

```sql
-- amostra pequena, com recorte; NUNCA GROUP BY no acervo
SELECT left(link, 60), count(*)
FROM tribunals_movimentacao
WHERE processo_id = ANY(<pks de uma amostra do tribunal>)
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

**Pergunta aberta, e é de mapa, não de parser: quantos outros dos 59 tribunais
migraram para eproc (ou outro sistema) sem que a gente notasse?** Barato de
checar com o método acima. Não foi feito nesta missão.

**O que está em produção desde 25/08/2026:** o refill fecha a faixa `eproc` em
LOTE (`_separar_fora_da_fonte`), sem gastar requisição nem IP do pool
COMPARTILHADO. A recusa é **contada, nunca muda**:

```bash
# quantas consultas foram poupadas, por tribunal e motivo
ssh 100.98.141.91 'docker exec -w /app voyager-worker_default-1 \
  python manage.py enrich_fora_do_esaj'
# e o refill loga em ERROR a cada passada em que o número cresce:
#   FORA DA FONTE: 2.940.182 processos TJSP nao foram consultados — ...
```

⚠️ Por que em LOTE no refill e não job a job: sem rede, 40 réplicas de
`worker_tjsp` publicam **milhares de events por segundo** num stream que tem
`MAXLEN` e é **compartilhado com todos os tribunais** — o TJSP expulsaria os
events de TRF1/TJMG antes de o drainer aplicá-los. O teto é
`RECUSA_FORA_DA_FONTE_BATCH` (10.000 por passada por tribunal ⇒ 300 mil/h,
≈ 10 h para os 2,94 M).

**O efeito medido em produção (25/08/2026, deploy 01:56 UTC na `.102`):**

Régua: `Process` distintos com `enriquecido_em` na janela
(`proc_enriquecido_em_idx`), **nunca** `docker logs` longo.

| | antes | depois (~1 h) |
|---|---:|---:|
| **TJSP `ok`** (janela de 10 min) | **0,6%** | **92,3%** (893 de 967) |
| TJSP `ok`/h | ~66 | **~5.355** (**81×**) |
| TJSP desfechos/h | 10.816 | 5.802 (o resto era refugo do eproc) |
| `segredo_justica = true` no índice | **0 de 92.778.138** | **136** |
| profundidade dos 4 streams do drainer | — | **2 entries** (não inunda) |
| pool ProxyScrape (saudáveis de 2.500) | 1.802 | 1.443 ⚠️ |

O total de desfechos do TJSP **caiu** e isso é o conserto, não o custo: os
10.750/h de antes eram quase todos requisição gasta para ouvir "não existe".
Agora são ~5.800/h de trabalho real com 92,3% de aproveitamento.

⚠️ **O pool caiu de 1.802 para 1.443 saudáveis, e não é o TJSP** — que gerou
13 `erro` em 10 min. O queimador da janela é o **TJPE: 635 `erro` em 10 min**
(3.810/h × até 8 IPs = ~30 mil IP/h), do mesmo tipo do TJRO/TJAP de 25/08
(AWS WAF, §"AWS WAF challenge" em ENRICHMENT.md). Fica como próximo alvo.

E a primeira passada do recuperador apareceu no log — pela primeira vez desde
que ele foi escrito:

```
tick_reenrich_esaj_legacy TJSP: reset 2000 (teto 2000)
ERROR reenrich_legado: TJSP bateu o teto do tick (2,000 de 4,329 elegiveis no lote)
      — ainda ha legado preso; acumulado devolvido ate agora: 2,000
ERROR FORA DA FONTE: 7,024 processos TJSP nao foram consultados — a faixa `eproc`
      nao existe na consulta publica deste tribunal
```

⚠️ Duas ressalvas honestas sobre esses números:
- **`fail_streak` do pool não é métrica de enrichment.** Só o cliente DJEN
  chama `mark_ok()`; os enrichers chamam `mark_bad()` e nunca zeram o contador.
  Use `saudáveis`/`bad`, não o streak.
- **A fatia medida ainda não é o regime permanente.** Nos 10 min do "depois" a
  fronteira estava nos prefixos 9 e 7, que juntos são 0,62% do TJSP. Prova que
  ela destravou; o regime de longo prazo será dominado pelos prefixos 1 e 0
  (76,2% do tribunal), cuja taxa medida ao vivo foi 30 de 32 com cadastro.
- Enquanto a fila do TJSP estiver acima da profundidade-alvo, o refill a
  **pula** — e então quem recusa a faixa eproc é o guard por job, não o lote.
  Foi o que produziu os 7.024 acima, a ~58/s, **sem** encher o stream.

**A porta do eproc existe e é pública, mas está fechada para nós:**
`eproc1g.tjsp.jus.br/...consulta_publica` → 302 → `eproc-consulta.tjsp.jus.br/
consulta_1g/`, HTTP 200, campo `txtNumProcesso`, **sem login** — atrás de
**Cloudflare Turnstile** (`data-sitekey="0x4AAAAAABC6galUOytMs1K-"`) com
verificação no servidor (`controlador_ajax.php?acao_ajax=verifica_estado_captcha`).
Não é o `if(false)` do PJe. Solver de captcha **não está autorizado** (decisão
de produto, 25/08/2026). A alternativa legítima é a 2ª porta do Datajud — que
hoje **exclui** os 16 tribunais com enricher (`reabastecer_fila_datajud`), e é
por isso que esses ≈ 2,94 M não têm porta nenhuma.

### O estoque de `nao_encontrado` do TJSP: o recuperador nunca rodou

2,59 M processos do TJSP em `nao_encontrado` terminal. Da amostra (n=2.678,
semente 20260825), o mês do `enriquecido_em`: 2026-05 **457** · 2026-06
**1.781** · 2026-07 214 · 2026-08 226 ⇒ **83,6% foram queimados ANTES do fix
de 2026-07-06** (o "200 ambíguo terminal"). E a sonda ao vivo prova que estão
vivos: nos prefixos 0/1, **30 de 32 devolveram cadastro completo ou lista
AGORA**, com classes reais.

`tick_reenrich_esaj_legacy` foi escrito exatamente para devolvê-los — e era
guardado por `pend >= REENRICH_PENDENTE_FLOOR` com FLOOR = **20.000**. O TJSP
tem **12.101.245** `pendente`. **A condição é inalcançável desde a primeira
execução: o recuperador era no-op por construção justamente para o tribunal
que ele nomeia**, e nunca acendeu luz. Mesma família do `for pagina in
range(1, 11)`.

Hoje o floor virou **teto por lote**. O teto efetivo por tick é o MENOR entre
o passo da fase (`REENRICH_LEGACY_POR_TICK`, default 2.000), a vazão real da
fila do tribunal medida pelo `FinishedJobRegistry`, e `REENRICH_RESET_BATCH`
(50.000). Encostar no teto é **ERRO com o número acumulado por tribunal**.

**Abrir a fase (só depois de medir):**
```bash
# 1) régua ANTES — vazão por enriquecido_em e saúde do pool
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python /tmp/e3_fluxo.py'   # ok/nao_enc por tribunal
ssh 100.100.144.57 'docker exec -w /app voyager-web-1 python /tmp/px2.py'        # saudáveis/bad_zset/fail_streak
# 2) abrir um passo no .env da .102 e force-recreate (restart NÃO relê .env)
echo 'REENRICH_LEGACY_POR_TICK=5000' >> ~/voyager/.env
docker compose -f docker-compose-workers.yml up -d --force-recreate worker_default
# 3) régua DEPOIS, 30 min mais tarde. A boa de referência: 1.926 de 2.500 IPs
#    saudáveis. Se `bad_zset` subir, VOLTE o passo — não suba a TTL do inflight.
```
Nunca "religa tudo": 2,59 M voltando de uma vez é incidente.

### e-SAJ: cinco páginas com HTTP 200, e o código tratava três como a mesma

`classificar_resposta` (`enrichers/esaj.py`) separa `detalhe`, `nao_existe`,
`segredo`, `lista` e `ambiguo`. As fixtures REAIS de cada uma estão em
`tests/fixtures/tjsp/`. Duas armadilhas medidas, ambas **teste de substring
numa página inteira** (ver `.ia/PATTERNS.md`):

- `'classeProcesso' in html` casa a **classe CSS** `<div class="classeProcesso">`
  da página de LISTA ⇒ a lista virava "detalhe" e o processo era gravado `ok`
  com o cadastro inteiro vazio. Hoje a lista é SEGUIDA pelo link do nosso
  número (1º grau `a.linkProcesso`; 2º grau rádio `processoSelecionado`).
- A frase *"É necessário informar uma senha para acessar processo em segredo de
  justiça"* está dentro de `<form id="popupSenha" style="display: none;">` em
  **TODA** página de detalhe — 36 das 62 páginas baixadas na sonda a contêm, e
  **33 dessas 36 são detalhes normais COM partes**. Detector por frase marcaria
  segredo em processo bom e o **esconderia do funil**: pior que não detectar.

Detector de segredo (estrutural e conservador): chegou ao `show.do`,
`id="containerDadosPrincipaisProcesso"` presente e **VAZIO**, `id="popupSenha"`
presente, e **nenhum** de `classeProcesso`/`assuntoProcesso`/`foroProcesso`/
`varaProcesso`/`numeroProcesso`/`tablePartesPrincipais`. Página de 32.963 bytes
na captura.

**Régua do antes/depois** (o ganho é o número de `True`, que era **0 em
91.638.494** documentos do índice):
```bash
curl -s '192.168.30.128:9200/voyager-processos/_count' -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"segredo_justica":true}}}'
# e no banco, com recorte (nunca GROUP BY no acervo):
#   WHERE tribunal_id='TJSP' AND segredo_justica IS TRUE
```
⚠️ `Process.segredo_justica` ainda **não é nullable** (o ALTER espera janela de
lock limpa): `False` em processo nunca enriquecido continua significando "não
perguntamos", não "perguntamos e não é". Só o `True` é medida.

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

## ⚠️ ESTADO CONHECIDO: assunto e `grau` corrigidos no Postgres, VELHOS no índice (desde 2026-08-25)

**Se você medir a TELA e concluir que o conserto do assunto não funcionou, leia
isto antes de abrir investigação.**

Em 25/08/2026 duas correções passaram a valer no Postgres e **não** chegaram ao
Elasticsearch:

| o quê | no Postgres | no índice `voyager-processos` |
|---|---|---|
| `assunto_codigo` / `assunto_nome` (folha, não hierarquia) | corrigido no drainer + backfill | **valor antigo** |
| `Process.grau` (`G1`/`G2`/`JE`/`SUP`/`TR`/`TRU`) | passa a ser gravado pelo Datajud | **campo ausente** |
| `ProcessoParte` vinda do DJEN (`fonte='djen'`) | backfill | **ausente** até o reindex |

**Por quê:** `bulk_update()` e `.update()` **não disparam `post_save`**, e
`ProcessoParte` **não tem signal de propósito** (ver o comentário em
`search/signals.py`) — todo caminho que escreve em lote tem de reindexar
explicitamente. Não é defeito novo; é a consequência conhecida do desenho, e
está aqui para não ser rediagnosticada.

**Agravante medido no mesmo dia:** `es_backfill_processos` **só reindexa o que
está AUSENTE** do índice (`gate.ausentes_no_bloco`). Documento presente com
conteúdo velho ele **pula**. Ou seja, o backfill em curso **não** leva dado
novo de carona — quem já está indexado precisa de reindex direcionado.

⇒ Enquanto esta seção existir, **a busca mostra o assunto antigo**. O conserto
é o reindex por faixa contígua de `Process.id`, e o custo está medido em
`.ia/SEARCH_SCHEMA.md` §"Linha de base do índice de processos". **Apague esta
seção quando o reindex tiver passado** — e não antes.

## 🔴 INCIDENTE: um lote sem teto travou três jobs por 12,7 h (27/08/2026)

`backfill_sinal_precatorio --tribunais TJSP` com o default `--batch 20000`.
Um único `UPDATE` rodou **45.705 s = 12,7 horas**:

```sql
UPDATE tribunals_process p SET tem_sinal_precatorio = EXISTS (
  SELECT 1 FROM tribunals_movimentacao m
   WHERE m.processo_id = p.id AND m.texto ~* '<padrão>')
 WHERE p.id = ANY(<20.000 ids>)
```

**Por que só no TJSP:** o custo do `EXISTS` é proporcional ao número de
**movimentações** do processo, não ao de processos. O mesmo `--batch 20000` que
fecha em segundos no TJPR virou meio dia no TJSP. Teto dimensionado por
processo, custo pago por movimentação — a conta não fecha e ninguém percebe até
travar.

**O que ele travou** (todos em `wait_event_type=Lock`, atrás dos row-locks das
20.000 linhas):

```
DROP INDEX CONCURRENTLY proc_atualizado_em_idx ....... 12,6 h esperando
UPDATE tribunals_process SET assunto_id = …  .........  2,6 h
UPDATE tribunals_process SET classificacao = … (×2) ..  1,5 h cada
```

**Como achar:** `pg_stat_activity` ordenado por `now() - query_start`, e olhar
`wait_event_type`. Quem está `Lock` é vítima; quem está `NULL` há horas é o réu.

**Cura:** `SELECT pg_cancel_backend(<pid>)` no réu — cancela a consulta, faz
rollback do lote e solta os locks. **Não** use `pg_terminate_backend` primeiro:
cancelar basta e não derruba a conexão.

**Conserto durável:** `--batch` default caiu para 2.000 e o lote passou a rodar
com `SET LOCAL statement_timeout` (default 120 s) e `lock_timeout` (5 s), os
dois ajustáveis por flag. Com teto, o lote morre sozinho e o comando registra o
erro — em vez de virar bloqueio invisível de meio dia. Regra nº 7.

**E a lição do `DROP INDEX CONCURRENTLY`:** ele espera **snapshot**, não lock —
`lock_timeout` não o interrompe. Num banco que sempre tem alguma consulta longa,
ele pode nunca ganhar (esperou 12,6 h). Quando o alvo é um índice **inválido**
(que ninguém usa em consulta mas é mantido em toda escrita), o caminho é
cancelar e fazer `DROP INDEX` comum com `lock_timeout` curto **e repetição** —
assim nunca se entra na fila do lock, que é o que a regra do auto-jam proíbe.

## `sync_es`: os dois lados de PROCESSO eram vazamento (consertado 27/08/2026)

O tique incremental Postgres→ES tem três lados. O das **movimentações** já tinha
aprendido a lição (teto alto + freio pela fila). Os dois de **processo** não, e
por isso o dado ficava certo no banco e velho na tela.

**O que estava medido antes do conserto:**

```
proc_novos ......... 7,72 M de pks atrás, teto batido em 6 de 6 tiques
                     COM a fila `es_index` em ZERO e 4 jobs rodando
                     (prova: 60 pks ACIMA do watermark → 57 AUSENTES do índice;
                      60 pks ABAIXO, controle → 60 presentes)
proc_atualizados ... DIVERGIA. Andava 45 s de relógio por tique de 600 s;
                     idade 127,92 → 128,08 → 128,26 h em três tiques seguidos
```

O `proc_atualizados` é o que carrega **toda escrita em lote** — reclassificação,
partes, assunto, `classe_codigo`, `grau`. Enquanto ele divergia, nada disso
chegava à busca, e o próprio log dizia com essas palavras: *"Toda escrita em
lote posterior a isso está FORA da busca."*

**Causa raiz, por `EXPLAIN` em produção:** não havia índice em `atualizado_em`.
O plano era `Parallel Seq Scan` + `Sort` sobre 102 M linhas, custo 3,7 M, **a
cada 10 minutos**, com 14,3 M linhas casando o filtro. E como o custo é a
varredura e não o `LIMIT`, o teto de 10.000 não economizava nada — só cortava.

**O conserto, em três partes:**

1. `proc_atualizado_em_idx` em `atualizado_em`, criado **`CONCURRENTLY`** (tabela
   quente; o `ALTER` normal vira auto-jam — ver a seção do ALTER abaixo).
   Migration `0053`, `atomic = False`, `IF NOT EXISTS`, `SeparateDatabaseAndState`
   para o ESTADO do Django não divergir do banco.
2. Tetos subiram e **viraram env**, ajustáveis sem deploy:

   | env | default | por quê |
   |---|---:|---|
   | `SYNC_ES_LIMITE_PROC_NOVOS` | 60.000 | limitado pelo `computar_sinal` INLINE (~7 ms/proc): 60 k ≈ 7 min de tique. Acima de ~85 k estoura a janela e atrasa os outros dois syncs, que rodam depois |
   | `SYNC_ES_LIMITE_PROC_ATUALIZADOS` | 200.000 | não computa nada, só consulta e enfileira. 1,2 M/h cobre a escrita em lote medida no pico |
   | `SYNC_ES_LIMITE_MOVS` | 400.000 | inalterado |
   | `SYNC_ES_FILA_ALTA` | 150.000 | inalterado |

3. **Freio pela fila nos dois lados de processo**, que só existia nas
   movimentações. Sem ele, subir o teto trocaria "atraso no watermark" por
   "atraso na fila" — o mesmo dado invisível, num número maior.

**Se a idade voltar a crescer:** não suba o teto às cegas. Meça primeiro qual dos
dois está segurando — `_fila_es()` alta significa que o gargalo é o **dreno**
(mais workers `es_index`), e fila baixa com teto batido significa que o gargalo é
o **teto** (suba a env). Os dois casos gritam ERROR no log com o número real.

## 🔴 INCIDENTE: coluna NOT NULL nova parou a ingestão do dia inteiro (25/08/2026)

**Sintoma na tela:** o KPI "PUBLICADAS EM 24H" mostrou **2.491** com **−99,8% vs
24h anteriores**. O número estava CERTO. A queda era real.

**Medido:** publicações por dia de disponibilização —

```
D-2 (24/08, seg) .... 1.529.530     ✅
D-1 (25/08, ter) ....        32     🔴   <- o dia inteiro
D-5 (21/08, sex) .... 1.453.803     ✅
D-6 (20/08, qui) .... 1.350.343     ✅
```

E a coleta NÃO tem atraso de um dia — a mediana de `inserido_em` das
publicações de um dia X cai em **05:30–06:00 UTC do próprio dia X**. Então às
21h de 25/08 já deveria haver ~1,4 M. Havia 32.

**Causa:** `IngestionRun` da janela `2026-08-25` com **10.410 `failed` e zero
`success`**, todos com a mesma mensagem:

```
null value in column "grau" of relation "tribunals_process"
violates not-null constraint
```

A migration `0052` usa o idioma padrão do Django:

```sql
ADD COLUMN IF NOT EXISTS "grau" varchar(4) DEFAULT '' NOT NULL;
ALTER COLUMN "grau" DROP DEFAULT;      -- ← a mina
```

Isso é correto **quando todo escritor conhece a coluna**. Não era o caso: os
workers seguiam com o código anterior à 0052 (bind mount entrega o arquivo,
Python não recarrega — ver a seção do worker com código velho). Código velho
monta o INSERT **sem** a coluna; sem `DEFAULT` no banco, o Postgres recusa.

**Cura (30 s, sem deploy, sem reiniciar nada):**

```sql
ALTER TABLE tribunals_process ALTER COLUMN grau SET DEFAULT '';
```

Tabela quente ⇒ o `ALTER` toma `ACCESS EXCLUSIVE` e bate em `lock timeout`.
**Não suba o `lock_timeout`** — repita com `lock_timeout='2s'` e 1,5 s de pausa
até passar (pegou na 22ª tentativa). Subir o teto é como se compra o auto-jam
da seção seguinte.

**Efeito medido:** último `failed` 00:19:34, primeiro `success` 00:19:36,
publicações de 25/08 de **32 → 3.032 → 14.472** nos minutos seguintes.

### A regra que fica

**Coluna NOT NULL nova em tabela que já tem escritor em produção NUNCA fica sem
`DEFAULT` no banco.** O `DROP DEFAULT` do Django presume deploy atômico, e aqui
não existe deploy atômico: web, scheduler, 5 drainers e dezenas de workers
recarregam em momentos diferentes. Deixe o `DEFAULT` no banco — ele custa nada
e é a única coisa que protege o escritor atrasado.

E o alarme funcionou: **o −99,8% no KPI foi o que denunciou**. A métrica usa
`data_disponibilizacao` e não `inserido_em` justamente para não ser mascarada
pelo backfill — sem essa escolha, os 8 M ingeridos teriam escondido o dia morto.

## ALTER TABLE em tabela quente — o auto-jam, e por que o gate não basta (2026-08-25)

**Incidente.** Três ALTERs metadata-only da migration `0052` (`ADD COLUMN`
nullable, `ADD COLUMN NOT NULL DEFAULT ''`, `DROP NOT NULL`) — nenhum reescreve
tabela, todos "baratos". Aplicados por um laço de retry em **autocommit** com
`SET lock_timeout` solto, um dos ALTERs ficou preso atrás de uma transação
exploratória de 88 min. Resultado medido em produção:

```
antes do cancel: 79 backends ativos · 63 sessões esperando Lock
depois:          12 backends ativos ·  0 sessões esperando Lock
```

**ACCESS EXCLUSIVE enfileira.** Enquanto o ALTER espera, todo mundo que chega
DEPOIS dele espera junto — inclusive quem só queria um `SELECT`. Um ALTER preso
não é um ALTER lento: é um bloqueio de leitura para o banco inteiro. Mesmo
mecanismo do `DROP INDEX` non-concurrent da seção acima.

**Cura:** `pg_cancel_backend(<pid do ALTER>)` (SIGINT). Nunca
`pg_terminate_backend`, e **jamais** `KILL <db>` no pgbouncer — esse PAUSA o
banco (a cura do auto-jam seria `RESUME`).

**Três coisas aprendidas, em ordem de utilidade:**

1. **`SET LOCAL` dentro da transação do migration, nunca `SET` num laço.**
   Um cliente que morre (ssh ou `docker exec` derrubado) **deixa o backend
   rodando server-side, órfão** — e órfão não tem quem o cancele. O `migrate`
   do Django roda tudo numa transação; `SET LOCAL lock_timeout` como primeira
   operação falha limpo em segundos, com zero dano colateral (medido: 8,5 s,
   `esperando Lock` intacto em 0).

2. **Migration que pode parar no meio tem que ser idempotente.** A primeira
   tentativa deixou `processoparte.fonte` NO BANCO sem a `0052` constar em
   `django_migrations`. Nesse estado o `migrate --noinput` do entrypoint do
   `web` estoura `column already exists` **no próximo boot** — o estado
   meio-aplicado é pior que o jam, porque só explode depois, no host que serve
   o produto. `SeparateDatabaseAndState` + `ADD COLUMN IF NOT EXISTS` resolve.

3. **Gate por `pg_locks` ajuda, e ainda assim não basta.** "Esperar o banco
   ficar limpo" é critério inalcançável: há warm jobs crônicos de 40+ min
   lendo `tribunals_movimentacao` que **não** bloqueiam `tribunals_process`.
   O gate certo é preciso — quem segura lock **na tabela alvo** há mais de
   ~45 s:

   ```sql
   SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
   WHERE l.relation = 'tribunals_process'::regclass AND l.granted
     AND a.pid <> pg_backend_pid() AND a.state <> 'idle'
     AND now() - a.xact_start > interval '45 seconds';
   ```

   Mas com os drainers aplicando **~126 mil events/h**, o `ROW EXCLUSIVE` das
   escritas conflita com ACCESS EXCLUSIVE e há **sempre** alguém segurando:
   o gate abriu e o ALTER ainda perdeu a corrida na janela de 3 s.

   ⇒ **A forma que funciona é janela de manutenção**, como na seção de
   deduplicação de partes: **pare os drainers e o scheduler**, aplique os
   ALTERs (segundos), religue. Não existe atalho sem parar escritor.

> `application_name`, não `client_addr`, é o que identifica a origem de uma
> sessão aqui. Como tudo passa pelo pgbouncer, que roda no host do banco,
> **todo** backend aparece com `client_addr=127.0.0.1` — inclusive os nossos
> containers. Concluir "é sessão local/humana" do endereço é errado.

## Backfill de partes do DJEN — shard dirigido por faixa de pk (2026-08-25)

`backfill_partes_djen` promove `Movimentacao.destinatarios` a
`Parte`/`ProcessoParte`. **Zero requisição de rede** — o dado já é nosso.

```bash
# SEMPRE detached: cliente que morre deixa backend órfão server-side
CID=$(docker compose -f docker-compose-workers.yml ps -q worker_default | head -1)
docker exec -d $CID sh -c "python -u manage.py backfill_partes_djen \
    --de 47071017 --ate 48814388 --shard tjpr --max-segundos 1800 -v 2 \
    > /tmp/bf_tjpr.log 2>&1"
docker exec -i $CID tail -3 /tmp/bf_tjpr.log

# PARAR AGORA — chave no Redis, efeito em segundos, sem deploy nem restart
docker exec -i $CID python manage.py backfill_partes_djen --pausar
```

### Por que faixa de pk e não `--tribunal`

`proc_tribunal_id_idx` é `btree(tribunal_id)` — **uma coluna**, apesar de o
model declarar `(tribunal, -id)`. Ver `DATA_MODEL.md`. Com ele,
`WHERE tribunal_id=X ORDER BY id LIMIT 1` varre e ordena (1.318 s medidos).
Faixa fechada de pk usa a PK e é `Index Cond` puro.

### Mapa tribunal × faixa de pk

Os processos estão **agrupados por tribunal em faixas contíguas de pk** — o que
torna o shard dirigido barato e o reindex do ES por faixa viável. Levantado por
amostra em 25/08/2026: 60 âncoras uniformes em `id ∈ [0, 104.602.261]`, blocos
de 200 pks lidos pela PK. Faixas com 100% de tribunal **sem enricher** (os que
têm 0% de parte e nunca terão outra porta):

| faixa de `Process.id` | tribunal |
|---|---|
| 10.460.226 – 12.203.597 | TRF4 |
| 13.946.968 – 20.920.452 | TRF6 / TRF4 |
| 27.893.936 – 29.637.307 · 33.124.049 – 34.867.420 | TJBA |
| 36.610.791 – 38.354.162 | TJGO |
| 41.840.904 – 43.584.275 | TJPI |
| **47.071.017 – 48.814.388 · 54.044.501 – 55.787.872 · 61.017.985 – 62.761.356** | **TJPR** |
| **52.301.130 – 54.044.501 · 57.531.243 – 59.274.614** | **TJSC** |
| 55.787.872 – 57.531.243 | TJRN |
| 59.274.614 – 61.017.985 | TRT1 |
| **66.248.098 – 67.991.469 · 90.655.292 – 92.398.663 · 97.628.776 – 99.372.147** | **TJRS** |
| 69.734.840 – 71.478.211 · 74.964.953 – 76.708.324 | TRT7 |
| 76.708.324 – 80.195.066 | TRT2 |

Refazer o mapa: `scripts/` — 60 âncoras, `SELECT tribunal_id … WHERE id >= a
ORDER BY id LIMIT 200`, cada uma na sua transação com `SET LOCAL
statement_timeout`.

### Custo medido e o que vigiar

| medida | valor |
|---|---|
| piloto, faixa fria, `--carga 0.5` | **8,02 s / 1.000 processos** |
| faixa contígua quente | **~3,0 s / 1.000** |
| linhas por processo | 4,5 – 4,8 |
| 4 shards simultâneos | `esperando LOCK` 0 · tx mais velha < 30 s · busca 1,8 ms |

⚠️ **A estimativa de leitura seca mente por 6,4×**: 1,26 s/1.000 não incluía a
varredura de `Movimentacao` por processo nem a escrita. Estimativa de custo que
não percorre o caminho inteiro é da mesma família de tudo o que este documento
cataloga.

⚠️ **Nada disto chega ao índice.** `bulk_create` não dispara `post_save` e
`ProcessoParte` não tem signal. **Anote as faixas de pk tocadas** — o comando
as imprime (`faixa de pk TOCADA`) — e passe-as ao reindex dirigido.

## MEÇA O BANCO ANTES DE PUXAR CÓDIGO (2026-08-25)

Regra irmã da de baixo, e esta evitou uma queda de produção em 25/08/2026.

A migration `0052` estava **meio-aplicada**: `ProcessoParte.fonte` já existia no
banco, `Process.grau` **ainda não**. E `origin/main:tribunals/models.py` já
declarava `grau`. Um `git pull --ff-only` na `.103` naquele instante faria
**todo `SELECT` do Django** emitir `UndefinedColumn` — o ORM monta a lista de
colunas a partir do model, então a query mais banal quebra. No `web`, no
`scheduler` e nos 5 drainers, ao mesmo tempo.

Nada no `git` avisa: o pull está correto, o código está correto, os testes
passam. **O que está errado é a ORDEM entre schema e código**, e ela só é
visível olhando o banco.

⇒ Antes de puxar código que declara campo novo, **confira a coluna no banco**:

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'tribunals_process' AND column_name = 'grau';
```

Regra geral: **migration entra ANTES do código que a usa**, e quem faz deploy
confere o schema, não só o `git log`. Quando os dois não puderem ser
simultâneos, o código novo tem que tolerar a coluna ausente — foi o que
`datajud/ingestion.py::coluna_grau_existe()` passou a fazer, mantendo o resto
da gravação viva e emitindo WARNING em vez de derrubar 24 réplicas.

## `git diff` na prod compara com o HEAD DELA, não com a `main` (2026-08-25)

Armadilha de diagnóstico que custou três briefings errados. A `.103` estava em
`3207fcb`, **30 commits atrás** da `main`. Um `git diff` lá acusou
`djen/scheduler.py` e `search/jobs.py` modificados, e a leitura natural — "a
produção roda código não commitado, não pode dar pull" — estava **errada**: o
diff era contra o HEAD **da própria máquina**, não contra a `main`. Conferido
depois por **md5 arquivo a arquivo**, o conteúdo era byte-a-byte idêntico ao da
`main`; os arquivos que travavam o `git pull` eram não rastreados e iguais.

⇒ Para saber se a prod tem código que a `main` não tem, **compare conteúdo**
(`md5sum` do arquivo na prod × `git show main:<arquivo> | md5sum`), nunca o
`git status`/`git diff` de uma árvore atrasada. É a mesma família de "comparar
com o estado errado e concluir com confiança" dos índices fantasma da migration
`0051` e dos triggers `pp_total_ins`/`pp_total_del` ausentes: o instrumento
responde outra pergunta, e a resposta parece a que você queria.

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

### Incidente: OOM do work-horse na ingestão DJEN (24/08/2026) — CORRIGIDO

**Sintoma.** `djen_backfill` com **703** falhas no `FailedJobRegistry`, das quais
**342 (48,6%)** eram `Work-horse terminated unexpectedly; waitpid returned 9` —
SIGKILL do OOM killer. 333 no **TJDFT**, 9 no TJAM; a `djen_ingestion`, 0. No
banco, **1.876** runs do DJEN marcados pelo watchdog como "worker crashou" em 7
dias. 197 das 342 eram o MESMO dia sendo refeito ~12×/h: morre, nunca vira
`success`, watchdog devolve, morre de novo.

**Causa (medida, não estimada).** A publicação do TJDFT tem **56 KB de texto**
(14.651 publicações = 822,6 MB num único dia). A `itensPorPagina=1000` isso são
55 MB de JSON por requisição; com 3 páginas paralelas e a leva anterior ainda
viva, o pico de RSS medido foi **957 MB** contra `mem_limit: 1g`. Controle com
`DJEN_PAGINAS_PARALELAS=1`: 640 MB. O erro era contar memória em PÁGINAS.

**Cura.** Dois controles, e a distinção importa:

* `DJEN_BYTES_EM_VOO` (64 MB) é a **previsão** — quantos itens devem caber numa
  página. Ela acerta o caso comum e erra o extremo, porque a publicação varia
  38 vezes dentro do mesmo tribunal;
* `DJEN_BYTES_MAX_RESPOSTA` (48 MB) é o **teto duro** — a resposta que passa
  disso é recusada durante o download, a página encolhe e o MESMO offset é
  relido. Nada é descartado; o número real vira
  `resposta_acima_do_teto_de_bytes` no run.

Ver [`INGESTION.md`](INGESTION.md#o-oom-do-tjdft--resolvido-em-24082026) para a
mecânica completa (sonda, decaimento, recorte de sobreposição, teto herdado).

**Como conferir em produção:**

```bash
# 1. o cemitério — censo COMPLETO por função/exceção/dia (não "os últimos N":
#    o registry é ordenado por EXPIRAÇÃO e ler o começo aponta pro alvo errado)
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py djen_censo_falhas djen_backfill'

# 2. quem passou do alerta de RSS (700 MB) — com o número real, no run
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py shell -c "
from tribunals.models import IngestionRun
qs = IngestionRun.objects.filter(fonte=\"djen\", erros__contains=[{\"erro\":\"memoria_acima_do_alerta\"}])
print(qs.count())"'

# 3. o tamanho da página que a calibração escolheu naquele tribunal
ssh 100.98.141.91 'docker logs --since 1h voyager-worker_ingestion-1 2>&1 | grep itensPorPagina | tail'
```

**Se voltar a acontecer:** o botão é `DJEN_BYTES_EM_VOO` no `.env` dos workers.
⚠️ Ele é botão de **VAZÃO**, não de pico: medido em 24/08, baixar de 64 MB pra
24 MB deixou o pico IGUAL (311 MB) e derrubou a página do TJDFT pro piso de 25
itens — 586 requisições e ~3 h para UM dia-tribunal, que é perda de acervo pelo
relógio. Quem segura o pico é a sonda + o teto de crescimento
(`PAGE_SIZE_SONDA`, `FATOR_CRESCIMENTO` em `djen/client.py`). Subir `mem_limit`
é último recurso — a `.102` já travou por OOM **de host** em 2026-06-08.

⚠️ `git pull` no host **não** recarrega o Python: `docker restart` nas réplicas
de `worker_ingestion` é obrigatório (ver o incidente de 21/08 acima).

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

#### O outro deadlock: `_flush_resumo` (24/08/2026)

O de 18/08 era no INSERT (`bulk_create`). Sobrou o do UPDATE, e ele era o
maior: **203 de 703 falhas** no censo da `djen_backfill` (28,9%), **98 runs
`failed` em 24 h** no banco (18,2% das falhas do período). Assinatura:

```
File ".../django/db/models/query.py", line 924, in bulk_update
django.db.utils.OperationalError: deadlock detected
CONTEXT:  while locking tuple (1126731,22) in relation "tribunals_process"
```

Cura (`djen/ingestion.py::_gravar_lote_resumo`): lote de 500 por transação,
`SELECT ... ORDER BY id FOR NO KEY UPDATE` antes de escrever (o EXPLAIN é
`LockRows -> Index Scan using tribunals_process_pkey`, ordem crescente
garantida sem depender do plano do UPDATE), retry de até 5 tentativas com
backoff, e o número real gravado no run. Mais: só é reescrito quem ganhou
movimentação NOVA. Detalhes e números em `INGESTION.md`.

Medido em produção: 98 runs `failed` por deadlock nas 24 h anteriores ao
deploy contra **0** nos 46 min seguintes (49 runs, 830.653 publicações), e 24
dias-tribunal que morriam fecharam `success` na primeira passada.

⚠️ **Não meça isso pelo `FailedJobRegistry`.** Os ids são determinísticos
(`f2:`/`bfd:`), então um dia re-enfileirado renova `started_at` e MANTÉM o
`exc_info` velho: 17 entradas pareciam falhas novas pós-deploy e todas eram
jobs `started`/`finished` com traceback antigo. Falha real = job em estado
`failed` COM `ended_at` posterior ao deploy — ou, mais simples, o `status` do
`IngestionRun` no banco.

**Como conferir se voltou** — os dois lados, o do banco e o do run:

```bash
# runs que falharam por deadlock na última hora (tem que ser 0)
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py shell -c "
from tribunals.models import IngestionRun as R
from django.utils import timezone; from datetime import timedelta
print(R.objects.filter(status=\"failed\", started_at__gte=timezone.now()-timedelta(hours=1))
      .extra(where=[\"erros::text ILIKE %s\"], params=[\"%deadlock%\"]).count())"'

# deadlocks que o RETRY venceu (não falham o run, mas contam a contenção)
# procure erros__contains=[{"erro":"deadlock_em_tribunals_process"}]
```

Se o `esgotou_tentativas` aparecer `true` com frequência, a contenção passou do
que o retry cobre — olhe QUEM mais escreve em `tribunals_process` na janela
(drainers de enriquecimento, datajud) antes de subir o teto de tentativas.

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
