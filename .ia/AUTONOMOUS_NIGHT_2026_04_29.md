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

## Métricas — atualizadas a cada 5min via monitor

```
ts=04:35:57 lag=142258 backfill_q=14669    [pre 2-replicas drainer]
```

(monitor ativo com timeout 1h; se a sessão sobreviver, mais ticks abaixo.)
