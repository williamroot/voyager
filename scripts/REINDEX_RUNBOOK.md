# Runbook — Reindex dos Processos no Elasticsearch

**O quê:** popular o índice `voyager-processos` (estava **0 de ~71 mi**). As
movimentações já têm 12 mi indexadas; os processos, zero — a busca por processo
está cega. Este reindex enche o índice, **tribunal a tribunal**, com throttle pra
não sufocar o DB (que é I/O-sensível).

## Como está rodando (auto-contido)

Roda num container **dedicado** do web (não morre se a sessão do Claude cair):

```bash
# no host de prod (.103):
cd ~/voyager
docker compose run -d --no-deps --name voyager-reindex \
  -e BATCH=2000 -e SLEEP=0.2 \
  web sh /app/scripts/reindex_processos.sh
```

- **Idempotente** (`_id = process.id` → upsert) e **resumível**: cada tribunal
  concluído é gravado em `media/reindex_processos_done.txt`; re-rodar pula os feitos.
- Anti-N+1: o comando faz `prefetch_related('participacoes')` — 1 query por lote,
  não por processo.

## Como ver se está rodando

1. **Gráfico ao vivo** (o melhor): `/dashboard/indexacao/` — barra de Processos e
   "por tribunal" sobem sozinhas (refresh 30s). É o velocímetro.
2. **Container vivo?**
   ```bash
   docker ps --filter name=voyager-reindex
   docker logs --tail 30 voyager-reindex
   ```
3. **Log detalhado** (progresso %, taxa/s, ETA por tribunal):
   ```bash
   tail -f ~/voyager/media/reindex_processos.log
   ```
4. **Contagem direta no ES:**
   ```bash
   curl -s http://192.168.30.128:9200/voyager-processos/_count
   ```
5. **Tribunais já concluídos:** `cat ~/voyager/media/reindex_processos_done.txt`

## Como parar / retomar

- **Parar:** `docker rm -f voyager-reindex` (o tribunal em andamento não é marcado
  como done → retomado do zero DELE no próximo run; os anteriores ficam).
- **Retomar:** re-rodar o mesmo `docker compose run -d …` — pula os tribunais no
  DONE e continua de onde parou.
- **Mais gentil com o DB:** subir o `SLEEP` (ex.: `-e SLEEP=0.5`). Mais rápido:
  baixar (`-e SLEEP=0`) — mas olho na saúde do DB (busca pode travar sob carga).

## Sinais de alerta

- Site/busca lentos durante o reindex → subir `SLEEP` ou parar e rodar fora de pico.
- `bulk HTTP 4xx/5xx` no log → ES sob pressão; o comando segue (idempotente), mas
  vale checar heap/disk do `voyager-es-01` (`/dashboard/indexacao/` mostra o cluster).

## Depois de concluir

- Investigar por que o **write-through** de `Process` não populava (senão volta a
  divergir): `search/jobs.py` (fila `es_index`) + sinais de save. O reindex é o
  backfill; o write-through é o incremental.
