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
- Modo normal: sorteia Cortex (residencial) vs Pool (datacenter ProxyScrape) por request, com `random() < DJEN_CORTEX_RATIO` (default 0.5). Cada request sai por IP diferente — pool já randomiza internamente, Cortex tem rotação no gateway. Diversifica fontes pra contornar ondas de WAF que bloqueiam só datacenter ou só residencial.
- Modo `prefer_cortex=True` (fila `manual`): tenta Cortex primeiro (latência baixa pro user esperando feedback). Pool é fallback.
- Em retry, `prefer_other_than=last_failed_source` força a fonte oposta.
- Pool armazenado em Redis (`voyager:proxies:scrape:list`), bad TTL 600s.

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

**Cap rígido de 10k**: A DJEN para de paginar em 100 pgs × 100 = 10.000 itens por janela. Estratégia adaptativa em duas camadas:

- **Multi-dia que capou** (`paginas_lidas == 100 && novas+dup >= 10k && days >= 1`): divide em 2 metades e re-processa recursivamente, propagando `forcar_uf_em_1d=True`.
- **1-dia**: probe via `count_only`. Se `count >= 10k` OU se vier de split (`forcar_uf_em_1d=True`), vai direto pra `_ingest_day_por_uf` (paraleliza por 27 `ufOab`). A flag existe porque `count_only` pode mentir sob WAF/proxy ruim — payload truncado com `count` pequeno faria o caminho normal re-cap ar e perder dados.

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
| `watchdog_ingestao()` | `default` | 2min |
| `sincronizar_movimentacoes(process_id)` | `default` | 5min |

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
   - `watchdog_ingestao` a cada 5min
3. `Scheduler(connection, interval=30).run()` — loop infinito

`run_daily_ingestion` faz no-op silencioso se `Tribunal.backfill_concluido_em IS NULL` — evita brigar com backfill em andamento.

## Watchdog de ingestão

`djen.jobs.watchdog_ingestao` roda a cada 5min e faz auto-heal:

1. **Mata zumbis**: `IngestionRun.status=running` e `finished_at IS NULL` há >1h → marca FAILED + grava motivo. Worker que crashou e deixou rastro não trava o sistema.
2. **Re-enfileira backfill**: pra cada tribunal ativo com `backfill_concluido_em IS NULL`, se nenhum job dele em `djen_backfill` (pending nem started) → `run_backfill.delay(sigla)`. Se redis perdeu state ou backfill morreu, recupera sozinho.
3. **Re-enfileira daily**: pra tribunal com backfill ok mas sem `IngestionRun success` há >26h → `run_daily_ingestion.delay(sigla)`.

Detecção de "já tem job pra essa sigla" usa `job.args[0]` como chave — evita duplicar quando um backfill está realmente em curso.

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

## Materialized View `mv_pipeline_diario`

Criada na migration `0029`. Agrega contagens de Process por tribunal, dia e fonte de enriquecimento.

```sql
-- formato LONG: uma linha por (tribunal_id, dia, fonte)
SELECT tribunal_id, data_enriquecimento_datajud::date AS dia,
       'datajud'::text AS fonte, COUNT(*)::int AS processos
  FROM tribunals_process
 WHERE data_enriquecimento_datajud IS NOT NULL GROUP BY 1,2
UNION ALL
SELECT tribunal_id, enriquecido_em::date, 'pje', COUNT(*)::int
  FROM tribunals_process WHERE enriquecido_em IS NOT NULL GROUP BY 1,2
UNION ALL
SELECT tribunal_id, classificacao_em::date, 'classif', COUNT(*)::int
  FROM tribunals_process WHERE classificacao_em IS NOT NULL GROUP BY 1,2;
```

Colunas: `tribunal_id` (sigla), `dia` (date), `fonte` (text: `'datajud'` | `'pje'` | `'classif'`), `processos` (int).
Índice único em `(tribunal_id, dia, fonte)` — permite `REFRESH CONCURRENTLY`.

**Nota:** DJEN **não está** na MV. É lido live de `IngestionRun` com
`MAX(janela_fim)` por tribunal/dia para não duplicar overlap de janelas
(dois runs com janela sobreposta contam o mesmo dia duas vezes se somados).

### Refresh

| Job | Schedule | Como |
|---|---|---|
| `refresh_materialized_views` | cron 03:00 diário | `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario` |
| `warm_pipeline_diario` | a cada 1h (inline no scheduler) | re-aquece cache da MV após VACUUM |

Ambos rodam inline no `ThreadPoolExecutor(20)` do scheduler (`.32`) — sem fila RQ. Ver ADR-017.

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
