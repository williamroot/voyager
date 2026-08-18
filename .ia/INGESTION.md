# Ingestão DJEN

> ⚠️ **O DJEN é a porta ESTREITA.** Ele é veículo de *comunicação*: só entra
> processo que teve intimação publicada em diário. Medido em 14/08/2026, isso
> nos dava **~13% do acervo declarado ao CNJ** (343M docs).
>
> As outras duas portas:
> - **Varredura do Datajud** — o *esqueleto* nacional (sem parte, sem valor):
>   [`ACERVO_CNJ.md`](ACERVO_CNJ.md).
> - **Diários próprios** — texto verbatim de *antes* de o tribunal aderir ao
>   DJEN (o TJSP só entrou em **14/03/2025**, a Justiça do Trabalho em
>   **01/08/2024**, e o STF nunca entrou): [`DIARIOS.md`](DIARIOS.md).
>
> **Cuidado ao mexer em cobertura/lag:** desde 2026-08-16 `IngestionRun` tem o
> campo `fonte` (default `'djen'`) e TODA query de cobertura do DJEN filtra por
> ele. Sem esse filtro, um run do DJE/TJSP no dia X faz o `_dia_coberto` do DJEN
> pular a ingestão daquele dia — perda silenciosa. Ver `DIARIOS.md` §3.

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
- Modo normal: sorteia Cortex (residencial) vs Pool (datacenter ProxyScrape) por request, com `random() < DJEN_CORTEX_RATIO` (**default 0.0 desde 29/07/2026** — ProxyScrape é o PRIMÁRIO, tem mais banda; Cortex só explícito via `prefer_cortex=True`, ou fallback: pool vazio/degradado e retry pós-falha do pool). Subir o ratio via env se uma onda de WAF queimar o datacenter.
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

### Circuit-breaker (5xx em massa) — incidente 2026-07-10

O DJEN (governo) devolve `500 {"message":"O sistema está muito ocupado"}` sob carga.
Em **2026-07-10** ele passou a 500ar de forma persistente; com **40k jobs `backfill_dia`
× 8 retries** martelando, viramos parte do problema (congestão auto-infligida) — a
ingestão parou ~59h e a MV `mv_ingestion_rate_hora` ficou defasada.

Fix (`djen/client.py`): **circuit-breaker fleet-wide via cache/Redis**.
- Cada `5xx` → `_record_5xx()` conta na janela (`DJEN_CIRCUIT_5XX_WINDOW=120s`).
  Ao atingir `DJEN_CIRCUIT_5XX_THRESHOLD=15` → **abre** o circuito (`djen:circuit_open`)
  por `DJEN_CIRCUIT_COOLDOWN=300s`.
- Com o circuito aberto, `_fetch` **fast-fail** com `DjenBusyError` (subclasse de
  `DjenServerError`) **sem tocar no DJEN**. Um `200` fecha o circuito (`_record_success`).
- `backfill_dia` trata `DjenBusyError` como **adiar** (return `{'skip':'circuito_aberto'}`,
  não empilha no FailedRegistry); `tick_backfill_retroativo` **não alimenta** a fila com
  o circuito aberto. Resultado: a fila drena como skips, o DJEN respira, e a ingestão
  **retoma sozinha** quando ele volta.
- Runbook: fila `djen_backfill` inflada + 5xx em massa = DJEN sobrecarregado. Não é
  nosso bug. Opcional acelerar: esvaziar `djen_backfill` + limpar FailedRegistry
  (`q.empty()` + `FailedJobRegistry.remove(..., delete_job=True)`) — os dias são
  re-enfileirados pelo tick quando o DJEN normaliza.

### Watermark por tribunal no tick de backfill — incidente 2026-07-29

Com ~25 tribunais em backfill simultâneo (TRTs/TST ativados 07-05) numa fila
`djen_backfill` única, o teto **global** (`BACKFILL_WATERMARK=4000`) não dava
fairness: quem enchia a fila primeiro monopolizava a FIFO e os demais viam
`fila>=4000` → "aguardando" pra sempre; a fronteira recente dos TRTs congelou
(lag 20d). Fix (`djen/jobs.py::tick_backfill_retroativo`):

- `BACKFILL_WATERMARK_POR_TRIBUNAL=200` — limite operacional é por tribunal;
  o global (agora 8000) é só sanidade (27×200=5.400 < 8.000).
- Jobs do tick usam **job_id determinístico** `bfd:<sigla>:<dia>` — contagem
  por tribunal via `get_job_ids()` (LRANGE barato, sem fetch de payload) e
  dedupe natural de re-enfileiramento entre ticks. O fan-out do daily continua
  com id aleatório + `at_front` (não conta no cap por tribunal).

**Frontend correlato** (`base.html::buildIngestaoRateChart` + `queries.ingestion_rate_por_hora`):
quando a MV está defasada, mostra o **último snapshot conhecido** (janela ancorada em
`max(hora)`, não em `NOW`) com selo **"⚠ defasado há Xh"** — em vez de esconder o gráfico.

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

**O dia sai FLAT** (desde 18/08/2026). `iter_pages` pagina até a página voltar
incompleta, sem teto — medido no TJSP: 262 páginas, 261.076 itens, ids todos
distintos, zero página incompleta no meio.

⚠️ **Não existe "cap rígido de 10k".** O `count` da DJEN satura em 10.000 porque
por baixo é o `max_result_window` do Elasticsearch: é um **PISO** ("tem pelo
menos 10 mil"), não um teto de paginação. Construir lógica em cima dele foi a
origem das duas maiores perdas de acervo do projeto.

O caminho antigo — fatiar o dia em 27 requisições por `ufOab` — está atrás da
escotilha `DJEN_ESTRATEGIA_UF` (padrão OFF) porque tem **defeito próprio**:

- **é cego a publicação sem advogado com OAB.** Provado na unidade em cinco
  tribunais: TJPE 2025-08-13 tem 2.853 itens sem OAB e são exatamente as 2.853
  que faltavam; TJMA 911 = 911; TJRN, STJ e TJPB idem. 2% a 10% de todo dia
  acima de 10.000 — e o conserto do teto de páginas (17/08) NÃO recupera esses;
- **custa 27× mais requisição** à API do CNJ (rate-limit de 20/s);
- **uma fatia perdida derrubava o dia inteiro em silêncio** — ver abaixo.

**Janela paralela** (`DJEN_PAGINAS_PARALELAS`, 8). A página é offset puro, então
buscar 8 de cada vez não muda o que volta — só o relógio. Serial, o canário do
TJSP (261.076 publicações) levou 163 minutos: 1,61 pg/min. O teto de memória
continua sendo a janela em voo, nunca o dia (acumular o dia matou os workers com
OOM em 17/08).

A janela também enxerga o que a versão serial não podia: **página incompleta
seguida de página com dado = paginação mentiu**, e isso é ERRO, não `return`.

**Fatia perdida = run FAILED.** O limiar era 14 de 27 fatias, o que trata a
fatia do DF (77% do dia no TJDFT) igual à de um estado com 40 itens. Resultado
medido: 1.232 runs `success` com `uf_fetch` dentro, cobrindo 1.165
dias-tribunal — e `_dia_coberto` pula dia com run `success`, então esses dias
ficariam fora de qualquer backfill futuro. Hoje uma fatia basta pra falhar.

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

### Cobertura e `_dia_coberto`

`_dia_coberto` e `_dias_cobertos` (`djen/jobs.py`) determinam se um dia já foi
processado — usado pelo backfill pra pular janelas feitas e pelo watchdog de
re-enfileiramento.

**Regra de horizonte recente (bug latente fechado):** dias dentro de
`hoje − overlap_dias` só contam cobertos se o `success` teve dados reais
(`novas | duplicadas | paginas > 0`) — **mas essa exigência de dados se aplica
apenas a dias úteis** (`weekday() < 5`). Exemplos:

| Situação | Resultado |
|---|---|
| Dia útil recente, `success` com `novas=0` (vazio) | **não coberto** — backfill re-tentará |
| Sábado ou domingo recente, `success` vazio | **coberto** — DJEN não publica fim de semana; sem retry desnecessário |
| Qualquer dia fora do horizonte, `success` (qualquer) | **coberto** — backfill histórico de feriado/recesso intacto |

Isso fecha o bug latente onde um `success` vazio sobre-escrevia dias recentes
como cobertos, silenciando lacunas de ingestão que deveriam ser vistas no
heatmap de saúde como vermelho, sem gerar falsos retries em fins de semana.

**Ressalva — feriado forense:** não há calendário de feriados forenses (Corpus
Christi, feriados estaduais, recesso). Um feriado recente com `success` vazio é
tratado como dia útil perdido → o fan-out diário e o `tick_backfill_retroativo`
o re-tentarão repetidamente até ele envelhecer fora do horizonte. Custo baixo
(proxy/worker), não é outage; consistente com a nota "Falso-vermelho em feriado
forense" do `DASHBOARD.md`.

### Refresh

| Job | Schedule | Como |
|---|---|---|
| `refresh_materialized_views` | cron 03:00 diário | `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario` |
| `warm_pipeline_diario` | a cada 1h (inline no scheduler) | re-aquece cache da MV após VACUUM |

Ambos rodam inline no `ThreadPoolExecutor(20)` do scheduler (`.32`) — sem fila RQ. Ver ADR-017.

## Rate limiting / volume

A DJEN aceita **paginação ilimitada** mas tem WAF. Observado:
- Datacenter proxies (ProxyScrape) bloqueados pelo WAF em ~**100%** dos IPs
  (medido 2026-06-17: 0/29 IPs do pool, 28× HTTP 403 + 1× 500). A ingestão DJEN
  é, na prática, **Cortex-only** — o pool **não** serve de fallback (devolve 403).
- Cortex (residencial fixo) sempre aceito → **SPOF**: se o gateway Cortex cair, a
  ingestão DJEN para (o pool não cobre). Ele **flapa** (caiu 100% por ~15min em
  2026-06-17 e voltou). Num incidente de ingestão, priorize a saúde do Cortex —
  o pool não resolve. (e-SAJ TJSP/TJAL é o caso oposto: responde pelo pool. Ver
  `.ia/DECISIONS.md` ADR-006/ADR-021.)
- 504 Gateway Timeout aparece em ondas — backoff longo + esperar é a única coisa a fazer

Volume típico:
- ~3.000-3.500 movs/min em ritmo cruzeiro (com Cortex + alguns proxies bons)
- ~10.000 movs por chunk de 30d em TRFs medianos
- Backfill TRF1 completo (5 anos): ~6-8h
- Backfill TJSP completo: estimado 3-5 dias (volume ~5x maior)
