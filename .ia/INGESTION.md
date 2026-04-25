# Ingestão DJEN

## API DJEN

```
GET https://comunicaapi.pje.jus.br/api/v1/comunicacao
    ?siglaTribunal=TRF1
    &dataDisponibilizacaoInicio=YYYY-MM-DD
    &dataDisponibilizacaoFim=YYYY-MM-DD
    &pagina=1
    &itensPorPagina=100        ← max
```

Sem auth. Resposta JSON com `count`, `items[]`. Cada item = 1 movimentação.

## Cliente HTTP (`djen/client.py`)

`DJENClient.iter_pages(sigla_djen, data_inicio, data_fim)` — gerador de páginas.

**Por request:**
- timeout: connect=10s, read=60s
- header User-Agent configurável (`DJEN_USER_AGENT`)
- Sleep 1.0s entre páginas (`DJEN_PAGE_SLEEP_SECONDS`)

**Estratégia de proxy híbrida** (`_pick_proxy`):
- 80% Cortex (residencial fixo) + 20% ProxyScrape pool
- Em retry, prefere alternar fonte (`prefer_other_than=last_failed_source`)
- Pool armazenado em Redis (`voyager:proxies:scrape:list`), bad TTL 600s

**Tratamento de erros (cumulativo nos retries):**
- `403/429`: marca proxy bad (se vier do pool) + retry com outro
- `5xx`: backoff longo (factor 3, máx 180s) — não marca proxy bad
- `4xx (não 403/429)`: erro real, sem retry, raise
- `ConnectionError/Timeout/ChunkedEncodingError/ContentDecodingError`: retry, marca proxy bad
- Backoff: `min(60s, 3 × 2^attempt + jitter)` para 403/429; `min(180s, 3×factor × 2^attempt + jitter)` para 5xx
- Máx 8 retries (`DJEN_MAX_RETRIES`)

## Parser (`djen/parser.py`)

`parse_item(item, tribunal, run) → ParsedItem | None`

**Validações:**
- Chaves esperadas: `EXPECTED_KEYS` (frozenset) — qualquer extra/missing dispara `SchemaDriftAlert`
- CNJ obrigatório: extrai de `numeroprocessocommascara` ou `numero_processo` ou regex em `texto`
- `data_disponibilizacao` obrigatória (parseia ISO ou `YYYY-MM-DD HH:MM:SS` ou `YYYY-MM-DDTHH:MM:SS.fffZ`)
- Itens inválidos: skip + append em `run.erros`

**Mapeamento de campos:** todos os 23 campos conhecidos da DJEN viram colunas explícitas em `Movimentacao`.

## ingest_window (`djen/ingestion.py`)

Coração da ingestão. Pra uma janela `(data_inicio, data_fim)`:

1. Cria `IngestionRun(status='running')`
2. `for items in client.iter_pages(...)`: chama `_process_page(items, tribunal, run, cnjs_tocados)`
   - `_process_page` envolto em **`transaction.atomic()`** — todo INSERT da página é atômico
   - Step 1: `Process.objects.bulk_create(novos, ignore_conflicts=True)` por CNJ ainda não conhecido
   - Step 2: re-query pra mapear CNJ → process_id
   - Step 3: `SELECT external_id WHERE tribunal=X AND external_id IN (...)` — conta novos vs duplicados (TOCTOU aceito)
   - Step 4: `Movimentacao.objects.bulk_create(movs, ignore_conflicts=True)`
   - Métricas atualizadas + `run.save(update_fields=...)` incremental (incluindo `erros` pra não perder em SIGKILL)
3. Trigger SQL atualiza `Process.total_movimentacoes`/`primeira/ultima_movimentacao_em` automaticamente
4. `run.status='success'` ou `'failed'` (com traceback no `erros`)

## Jobs RQ (`djen/jobs.py`)

| Job | Fila | Timeout |
|---|---|---|
| `run_daily_ingestion(sigla)` | `djen_ingestion` | 2h |
| `run_backfill(sigla, force_inicio=None)` | `djen_backfill` | 24h |
| `refresh_proxy_pool()` | `default` | 2min |

`run_backfill` é resilient + retry-friendly:

```python
chunks = chunk_dates(inicio, fim, days=30)
for chunk in chunks:
    if IngestionRun(success).exists(janela=chunk): pulados += 1; continue
    IngestionRun.filter(status=failed, janela=chunk).delete()  # retenta
    try: ingest_window(...); completados += 1
    except: falhas += 1; log + continue   # NÃO mata o job inteiro
if all(IngestionRun(success).exists(c) for c in chunks):
    Tribunal.update(backfill_concluido_em=now)
```

## Scheduler (`djen/scheduler.py`)

Container `scheduler` roda `manage.py djen_register_schedules_and_run`. Na boot:

1. **Cancela schedules anteriores** com tag `voyager-cron` (idempotência — sem duplicação a cada restart)
2. Re-registra:
   - `run_daily_ingestion(sigla)` para cada `Tribunal.ativo=True`, escalonados em 30min (TRF1 04:00, TRF3 04:30, ...)
   - `refresh_proxy_pool` a cada 15min
3. `Scheduler(connection, interval=30).run()` — loop infinito

`run_daily_ingestion` faz no-op silencioso se `Tribunal.backfill_concluido_em IS NULL` — evita brigar com backfill em andamento.

## Comandos manuais

```bash
djen_descobrir_inicio <sigla> [--force] [--floor 2022-01-01]
    Busca binária pelo primeiro dia com count>0. Salva em data_inicio_disponivel.

djen_backfill <sigla> [--inicio YYYY-MM-DD] [--sync]
    Enfileira run_backfill (ou roda inline com --sync). Retoma de onde parou.

djen_run_now <sigla> [--dias N] [--inicio ...] [--fim ...]
    Roda ingest_window inline (sem fila, sem checagem de backfill).
    Útil pra debug.

djen_status
    Snapshot CLI: tribunais, último run de cada, drift alerts abertos, status do pool.
```

## Rate limiting / volume

A DJEN aceita **paginação ilimitada** mas tem WAF. Observado:
- Datacenter proxies (ProxyScrape) bloqueados pelo WAF em ~80% dos IPs
- Cortex (residencial fixo) sempre aceito
- 504 Gateway Timeout aparece em ondas — backoff longo + esperar é a única coisa a fazer

Volume típico:
- ~3.000-3.500 movs/min em ritmo cruzeiro (com Cortex + alguns proxies bons)
- ~10.000 movs por chunk de 30d em TRFs medianos
- Backfill TRF1 completo (5 anos): ~6-8h
- Backfill TJSP completo: estimado 3-5 dias (volume ~5x maior)
