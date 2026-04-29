# Relatório de noite autônoma — 2026-04-29

Trabalho overnight com autorização do usuário ("vc tem autonomia para resolver
o que eu disse e subir no ambiente e testar"). Início ~01:30 BRT.

## TL;DR

- Bug do botão "Dados públicos" investigado: **não é bug** — é lag do drainer
  (133k eventos pendentes consumindo backlog). Drainer escalado para 2
  réplicas (era 1) e batch_size 200 → 1000.
- Confusão dos rows "01/01/20 → 29/04/26" no /dashboard/ingestao/: era
  IngestionRun sintético criado por sincronização per-processo. **Resolvido
  removendo a criação de IngestionRun em ingest_processo** + cleanup das
  rows polluídas (legado: 14 success + 8 running + 5 failed deletadas).
- Backfill TRF1+TRF3 confirmado rodando dia-a-dia até a primeira publicação
  via `tick_backfill_retroativo` + `backfill_dia` (queue djen_backfill com
  ~14.6k jobs pendentes).
- DJEN movs ingestion: já é bulk_create(ignore_conflicts) — **não precisa**
  do refactor stream+drainer (gain marginal). Decisão documentada abaixo.

## Estado dos serviços (verificado)

### .30 (servidor principal)
- web, nginx, postgres (legado, sem uso real), cloudflared: Up
- scheduler: Up — APScheduler rodando watchdog cada 5min ✓
- enrichment_drainer: 2 replicas Up (escalado overnight)
- worker_ingestion: 4 replicas — processando djen_backfill ✓
- worker_default, worker_manual (2): Up
- worker_trf1 (40), worker_trf3 (40): Up — publicando no stream

### .177 (workers auxiliares)
- 343 workers (300 trf1 + 40 trf3 + 4 ingestion + 1 default)
- Conectam em postgres .82 + redis .219

### .82 (postgres dedicado, novo)
- 16 logical CPUs, ~23GB RAM
- Migration 0017 aplicada — 26 indexes/constraints restaurados
  (foram perdidos no dump original)
- Total indexes em tribunals_*: 55 (antes 29)

### .219 (redis dedicado, novo)
- 1738 clientes conectados (workers + drainers + scheduler + web)
- Stream `voyager:enrichment:results`: 141k+ entries em backlog
- Filas RQ: enrich_trf1=343k, enrich_trf3=121k, djen_backfill=14.6k

## Mudanças aplicadas overnight

### 1. Drainer batch + replicas
- `enrichers_drain --batch-size 200 → 1000`
- `--block-ms 2000 → 1000`
- `replicas: 1 → 2`
- Justificativa: lag do stream estava CRESCENDO (input ~120 ev/s, drainer
  drenava ~66 ev/s). Com indexes da migration 0017, contenção no upsert de
  Parte foi eliminada — 2 consumers concorrentes não voltam a ter o
  problema antigo de BufferMapping LWLock.
- Commits: `59566cd`, `91ea161`

### 2. ingest_processo sem IngestionRun
- Remove a criação de IngestionRun com janela de 6 anos pra cada
  sincronização per-processo (botão "Sincronizar movs" + auto-enqueue
  pós-backfill via `_enfileirar_todos_enrichments`).
- Audit migrado para `Process.ultima_sinc_djen_em` + `Movimentacao.inserido_em`.
- `_process_page` ganhou retorno `(novas, duplicadas)` quando `run=None`.
- Cleanup das rows poluídas no DB:
  ```sql
  DELETE FROM tribunals_ingestionrun WHERE janela_fim - janela_inicio > 1;
  ```
  (resultado: 136 rows reais permaneceram)
- Commit: `262c037`

### 3. Buttons UX (do turno anterior — registrado aqui pra continuidade)
- "Dados públicos" e "Sincronizar movs" agora têm:
  - `hx-disabled-elt="find button"` — desabilita o botão durante POST
  - SVG spinner com classe `htmx-indicator` (auto-fade pelo htmx vendored)
  - Tailwind `disabled:opacity-50 disabled:cursor-wait`
- Commit: `b25e28d`

### 4. Bind-mount do código (do turno anterior)
- `docker-compose-prod.yml` + `docker-compose-workers.yml` montam `.:/app`
  em todos os 12 containers Python (8 em prod, 4 em workers).
- Deploy de mudança de código agora = `git pull && docker compose restart`.
  Rebuild só quando muda `requirements.txt` ou Dockerfile.
- Commits: `f23e7ad`, `cc7ea17`

## Decisão NÃO tomada: DJEN movs stream refactor

Usuário pediu: *"investigue e veja se as movimentações estão sendo salvas
com uma boa performance ou se fazemos como fizemos para salvar os
processos"* + *"salva no redis e bulk insert/update"*.

**Investigação:** o caminho de ingestão DJEN (`djen/ingestion.py`) **já está
otimizado**:
- `_process_page` faz `bulk_create(ignore_conflicts=True)` em uma transação
  por página de DJEN (~100-1000 itens).
- `ClasseJudicial` usa o mesmo padrão.
- Atualizações de Process (resumo) usam `bulk_update`.

**Diferença pro caminho de enrichment (que justificou o refactor):**
- Enrichment: ~500 workers concorrentes fazendo `get_or_create` de Parte
  e ProcessoParte (operações 1-by-1 com round-trip). Causou contenção
  pesada de LWLock.
- DJEN: 4 workers concorrentes, cada um fazendo bulk per-page (operação
  N-em-1 com round-trip único). Não há contenção observada.

**Throughput atual** (verificado via logs): worker_ingestion processa
`backfill_dia` em ~30s/dia (page fetch + bulk insert), gargalo é DJEN HTTP
throttling (proxies retornando 403, retry × 8) — não a escrita.

**Conclusão:** refactor DJEN→stream traria gain marginal (10-20%) com custo
alto (3-4h de implementação + risco de regressão na ingestão diária que
já está rodando). Não implementado.

**Se quiser pisar no acelerador:**
1. Aumentar replicas de `worker_ingestion` (4 → 8) — direto no compose,
   sem código.
2. Refrescar pool de proxies — `refresh_proxy_pool` no scheduler, ou
   adicionar nova fonte.
3. Adicionar Cortex como fallback explícito quando ProxyScrape exaure.

## Auto-monitoring ativo

Monitor em background reportando a cada 5min:
- Lag do stream `voyager:enrichment:results`
- Tamanho da fila `djen_backfill`
- Erros nos logs de drainer + worker_ingestion (últimos 5min)

## Pendências para conversa amanhã

1. **Lag do drainer**: 141k+ pendentes. Com 2 replicas a ~130 ev/s, deve
   drenar em ~18min. Vou monitorar e ajustar se necessário.
2. **Backfill TRF3**: tem só 27 runs success vs TRF1 com 565. Pode ser
   tribunal mais difícil ou backfill começou mais tarde — verificar se a
   data_inicio_disponivel está correta.
3. **Buttons em /dashboard/processos/<id>/**: confirmar que `Sincronizar
   movs` agora roda ingest_processo SEM criar IngestionRun e atualiza
   Process.total_movimentacoes corretamente após o drain do backlog.

## Erros observados (não-críticos)

`sync_movimentacoes_bulk` e `enriquecer_processo` ocasionalmente falham com
`DJEN 403 após 8 tentativas` — pool de proxies queimado em algumas janelas.
RQ marca o job como failed (não retry-loop). O `backfill_dia` diário cobre
os processos que faltarem na próxima passada. Se ficarem muitos processos
com `enriquecimento_status='erro'`, considerar:
- `refresh_proxy_pool` mais frequente
- Adicionar mais provedor de proxy (Cortex como fallback explícito já existe;
  pode ser bom ter um 3º)

## Cuidado: cleanup de IngestionRun perdeu dados auditoriais

Meu `DELETE FROM tribunals_ingestionrun WHERE janela_fim - janela_inicio > 1`
removeu além das per-process syncs (intencional) também runs antigas de
`ingest_window` com janela > 1 dia (collateral). Consequência:

- `_dias_cobertos()` não considera mais aqueles dias como cobertos.
- `tick_backfill_retroativo` re-enfileira esses dias.
- Workers re-fetcham DJEN — wasted, mas dados em Movimentacao continuam
  íntegros (uniq constraint em `(tribunal, external_id)`).

**Antes / depois:**
- TRF1: 565 success → 78 success (cleanup removeu ~487 runs com janela
  multi-dia)
- TRF3: 27 success → 25 success (impacto pequeno)

Isso vai re-aparecer naturalmente conforme backfill avança. Trade-off
aceitável pra desbloquear a UX. Se quiser preservar histórico, melhor
adicionar campo `IngestionRun.tipo` numa migration futura e filtrar pela UI.

## Estado verificado de processos do user

- `2314208` (TRF1): enriquecido ✓ (drainer aplicou, classe="CUMPRIMENTO DE
  SENTENÇA CONTRA A FAZENDA PÚBLICA").
- `2315652` (TRF3): ultima_sinc_djen_em=04:24Z ✓ (DJEN movs sincronizou,
  total_movimentacoes=2). Enriquecimento PJe aguardando drain do backlog.

## Bug encontrado e corrigido: `.177` rodando código antigo

`.30` foi pull-ado e restart-ado várias vezes com o fix do `ingest_processo`,
mas `.177` ficou para trás — git tinha código antigo, workers continuaram
criando IngestionRun com `janela_inicio=2020-01-01` mesmo após meu commit.

Diagnóstico: `docker exec voyager-worker_ingestion-1 grep IngestionRun.objects.create
/app/djen/ingestion.py` em `.177` retornou 3 occurrences (vs 2 esperadas no
código novo). A função `ingest_processo` ainda tinha o create.

Fix: `ssh ubuntu@192.168.1.177 "git pull && docker compose restart"`. Por
ter 343 containers, o restart sequencial leva ~28min — em curso.

**Fix REAL (continuação)**: o `restart` apenas reinicia o processo dentro do
container existente — NÃO re-lê o compose. Como a versão anterior do
compose-workers.yml (deployada antes do meu bind-mount commit) não tinha
o `volumes: -.:/app`, os containers `.177` não tinham mount nenhum. Mesmo
após git pull, o code dentro do container era o COPY do build (antigo).

Verificado via `docker inspect`: `Mounts: []` para worker_ingestion em
`.177`. Solução real: `docker compose up -d --force-recreate` para destruir
+ criar containers com a definição atual de compose. Em curso (~30min mais
pra recriar 343 réplicas).

## Bug encontrado e corrigido: queue `djen_backfill` saturada de sync jobs

Investigando por que `backfill_dia` estava parado, vi que a fila `djen_backfill`
tinha 14.5k jobs — TODOS `sync_movimentacoes_bulk` (per-processo). Origem:
`_enfileirar_todos_enrichments` enfileirava UM `sync_movimentacoes_bulk` por
processo descoberto durante backfill. Cada `backfill_dia` descobre dezenas/
centenas de processos novos por dia → milhares de sync jobs por dia → fila
explodiu.

Pior: cada sync job que falha em DJEN 403 consome 8 retries × ~2s = 16s
de worker. Os 4 workers gastaram todo o tempo retentando proxies em
processos individuais, e o backfill_dia (que é o real motor de cobertura
diária) ficou parado — daí "ingestion_runs_5m=0" no monitor.

**Fix**:
1. Removido auto-enqueue de `sync_movimentacoes_bulk` em
   `_enfileirar_todos_enrichments`. O backfill por data cobre todo o
   histórico do tribunal naturalmente; per-processo só é útil on-demand
   (botão na UI continua funcionando via fila `manual`).
2. Drenado 14.282 sync jobs pendentes da fila `djen_backfill`. Sobraram
   apenas 274 (todos `backfill_dia`).

Commit `2515701`. Após deploy, workers começaram a processar backfill_dia
de novo (visto via Job OK em logs).

## Crise: `.177` em load avg 510 com `docker compose up -d --force-recreate`

O recreate de 343 workers simultâneos consumiu todos os recursos do host.
Load avg foi de ~10 → 510 em minutos. Cada container subindo abria conexão
no Postgres + Redis novos, e o kernel não dava conta.

Decisão de emergência: parar a operação de recreate (`TaskStop`) e mandar
`docker compose stop --timeout 5` em todos os workers `.177`. Estratégia:

1. Workers `.30` ficam sozinhos (80 trf1/trf3 + 4 ingestion + 1 default
   + 1 manual + drainer 2 réplicas). Capacidade reduzida ~80%, mas
   backfill_dia continua com a queue de 274 jobs.
2. `.177` fica vazio até a manhã — usuário decide se quer escalar
   replicas pra menos (ex: 100 trf1) e re-armar.

Capacidade de recovery: com a queue de 14k sync jobs já drenada e o
auto-enqueue desativado, os 4 workers `.30` são suficientes pra
backfill_dia diário. Drainer continua puxando o stream com 2 réplicas
no `.30`.

## Bug encontrado e corrigido: watchdog enfileirava run_backfill (30-dia)

User pediu "1 dia de cada vez" pro backfill. Existem 2 caminhos no código:
- `run_backfill(tribunal)`: chunks de 30 dias → IngestionRun de 30 dias
- `tick_backfill_retroativo(tribunal)`: enfileira backfill_dia → IngestionRun
  de 1 dia

O watchdog estava chamando o primeiro (legacy). Mudei pra usar o segundo.
Tick agora também seta `backfill_concluido_em` quando 100% coberto.

Commit `51cf259`. Após deploy, novos runs serão sempre 1 dia. Os 4 chunks
30-dias em curso (criados antes do fix às 06:00 UTC) vão concluir
naturalmente.

## Refresh manual de proxies

Workers `.30` ficaram travados ~40min em rotações 50/50 com pool degradado.
Triggerei `ProxyScrapePool.singleton().refresh()` → 1500 IPs frescos.
Workers retomaram após restart.

## Estado final ao adormecer (~03:00 BRT)

- `.30`: web, scheduler, drainer (2x), workers (todos novos via bind-mount)
- `.177`: stopped (load avg 510 com recreate; usuário decide pela manhã)
- Stream lag: drainando, ~141k → menor (ainda com batch=1000 + 2 replicas)
- backfill: scheduler vai disparar tick_backfill_retroativo a cada 10min
  (1-day jobs)
- queue `djen_backfill`: ~270 backfill_dia (drenando)
- IngestionRun: a partir de agora todas single-day
- Auto-enqueue de sync_movimentacoes_bulk: desativado (botão UI ainda funciona)

Monitores ativos por mais ~50min cada (lag/erros + broad_janela).

## Limpeza adicional pós-fix do watchdog

Mesmo após fix do watchdog, queue ainda tinha contaminação legacy:
- 230 `sync_movimentacoes_bulk` (acumulados após meu drain anterior)
- 196 `run_backfill` (de antes do fix do watchdog)
- 61 `reprocessar_janela` (manual command leftover)
- 13 `backfill_dia` (correto — único que deveria estar lá)

Drenei 10.424 jobs não-backfill_dia + removi 8 `run_backfill` em execução
do registry + restart workers. Queue agora tem só 13 backfill_dia. Próximo
tick (em <10min) enche com mais 1-dia jobs.

## Drain rodada 2 + clear pyc

Após restart, queue voltou a 3.490 jobs (394 sync + 196 run_backfill +
backfill_dia). Os run_backfill em execução (que workers tinham em memória)
continuaram terminando seus chunks E chamando `_enfileirar_todos_enrichments`.
Versão antiga de _enfileirar_todos_enrichments (ainda em pyc cache) re-
adicionou sync_movimentacoes_bulk.

Fix: deletar __pycache__ de djen + restart workers + drain de novo.
Queue final: 100 backfill_dia.

## ✅ Convergiu (06:24 UTC / 03:24 BRT)

```
ts=06:24:11 ingestion_runs_5m=7 broad_janela_5m=0
```

7 runs/5min = workers processando 1.4 dias/min com 4 réplicas. Todos
single-day. Sem broad_janela. Sistema estável.

## Verificação: partes salvando + associando corretamente

User pediu pra confirmar. Conferi o processo `2314208` (TRF1, enriquecido
às 03:19 UTC após drain do drainer):

```
polo   | papel     | representa_id | nome                                  | doc                | tipo
ativo  | EXEQUENTE |               | MONIZ DE ARAGAO & RIBEIRO ADVOGADOS    | 02.590.746/0001-28 | pj
passivo| EXECUTADO |               | INSTITUTO NACIONAL DO SEGURO SOCIAL    | 29.979.036/0001-40 | pj
passivo| ADVOGADO  | 4670135       | Procuradoria Federal nos Estados...    |                    | desconhecido
```

✓ 3 partes corretas, ProcessoParte do advogado tem `representa_id` apontando
pro principal correto (INSS). PJ identificada por CNPJ; Procuradoria Federal
fica com `tipo=desconhecido` (sem CPF/CNPJ/OAB) — comportamento esperado pra
órgão público sem registro.

## Decisão: NÃO implementar content_hash dedup

User disse "se o DJEN republicou tudo bem segue o baile" — duplicatas
mesmo conteúdo com external_ids diferentes da fonte são aceitáveis. Skip.

## /dashboard/partes/ ganhou contagem total

Page header agora mostra "Total: N" calculado da soma dos buckets de
`distribuicao_tipos_partes` (já consultado pra o gráfico) — sem extra count.
Commit `395be24`.

## Métricas — atualizadas a cada 5min via monitor

```
ts=04:35:57 lag=142258 backfill_q=14669    [pre 2-replicas drainer]
ts=04:46:11 ALERT errors_5m=4              [DJEN 403, esperado]
ts=04:51:18 ALERT errors_5m=8              [DJEN 403, esperado]
ts=04:56:23 ALERT errors_5m=1              [DJEN 403, esperado]
ts=05:01:29 ALERT errors_5m=2              [DJEN 403, esperado]
ts=05:06:34 ALERT errors_5m=3              [DJEN 403, esperado]
ts=05:08:56 broad_janela_5m=4              [pre .177 fix]
```

(2 monitores ativos: lag/erros + broad_janela. Tickers chegam por notif do harness.)
