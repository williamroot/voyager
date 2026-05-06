# Arquitetura

## Apps Django

```
voyager/
├── core/         Settings (único arquivo), URLs raiz, middleware de RequestId,
│                 healthcheck split (liveness/readiness), error handlers (404/500/403)
├── tribunals/    MODELS centralizados — toda entidade do domínio fica aqui:
│                 Tribunal, Process, Movimentacao, IngestionRun, SchemaDriftAlert,
│                 Parte, ProcessoParte. Migrations + admin.
├── djen/         Ingestão da DJEN.
│                 - client.py: HTTP retry+backoff, rotação de proxies (Cortex/ProxyScrape)
│                 - parser.py: ParsedItem dataclass + drift detection
│                 - ingestion.py: chunk_dates + ingest_window + Process aggregates
│                 - jobs.py: run_daily_ingestion, run_backfill (resilient + retry-failed)
│                 - scheduler.py: register_all (cancel-then-recreate, idempotente)
│                 - proxies.py: ProxyScrapePool (Redis-shared) + Cortex fallback
│                 - management/commands/: descobrir_inicio, backfill, run_now, status,
│                   register_schedules_and_run
├── enrichers/    Consulta pública por tribunal (sem login, só metadata + partes).
│                 - trf1.py: PJe consulta pública (form fPP, parser BS4)
│                 - parsers.py: helpers (CPF/CNPJ/OAB regex, role, valor BRL, data BR,
│                   classificar_tipo_parte)
│                 - jobs.py: enriquecer_processo (fila default)
├── api/          DRF read-only.
│                 - viewsets.py: Tribunal/Process/Movimentacao/IngestionRun/Health
│                 - filters.py: ProcessFilter, MovimentacaoFilter (q + tribunal + ranges)
│                 - serializers.py: List+Detail por entidade
│                 - pagination.py: cursor pra movs, limit/offset pra resto
└── dashboard/    Server-rendered.
                  - views.py: overview, processos, movs, partes, ingestao, enriquecer
                  - queries.py: agregações (kpis, distribuição, top X) + filtros globais
                  - templates/: base + páginas + _partials/ (page_header, badge, chip,
                    modal, toast, kpi, dropdown, etc.)
                  - templatetags/voyager_extras.py: relative_dt, format_int, type_classes,
                    query_string, is_in_list
                  - static/dashboard/: voyager-identity.css + favicon.svg + voyager-patch.svg
```

## Containers (docker-compose)

```
postgres          Postgres 16 + extensions (pg_trgm, unaccent) + triggers customizados
redis             Redis 7 (AOF, noeviction — pra preservar pool de proxies)
web               Gunicorn + Django (DRF + dashboard + admin)
worker_ingestion  rqworker djen_ingestion djen_backfill (replicas escaláveis)
worker_default    rqworker default (manutenção, exports, enrichers, refresh proxy pool)
scheduler         APScheduler (BlockingScheduler + ThreadPoolExecutor(20)).
                  Roda todos os crons: ingestão diária, backfill, watchdog, proxies,
                  enrichers, datajud, classificação. Warm jobs do dashboard rodam
                  INLINE no thread pool — sem fila RQ, sem worker externo.
nginx             Reverse proxy + cache de /static/ + resolver dinâmico do Docker DNS
```

## Fluxo de ingestão

```
[scheduler container] cron
    ↓ enqueue diário 04:00 (TRF1) / 04:30 (TRF3)
[fila djen_ingestion] ──┐
                        ↓
[worker_ingestion] run_daily_ingestion(tribunal)
    ├── pula se backfill_concluido_em IS NULL
    ├── janela = (hoje - overlap_dias, hoje)
    └── ingest_window(tribunal, inicio, fim):
         ├── cria IngestionRun(status=running)
         ├── for items in DJENClient.iter_pages(...):  ← Cortex 80% / ProxyScrape 20%
         │   └── _process_page(items, tribunal, run, cnjs):
         │       ├── parse_item → ParsedItem (+ drift alert se chaves novas)
         │       ├── upsert Process (bulk_create ignore_conflicts)
         │       ├── bulk_create Movimentacao ignore_conflicts (idempotente)
         │       └── trigger SQL atualiza Process.total/primeira/ultima_mov
         ├── status='success', finished_at=now
         └── retorna métricas

[manual] djen_backfill <sigla> →
    fila djen_backfill → run_backfill:
        ├── chunks = chunk_dates(data_inicio_disponivel, hoje, days=30)
        ├── for chunk:
        │   ├── pula se já existe IngestionRun(status=success)
        │   ├── apaga IngestionRun(status=failed) anterior (retry policy)
        │   └── try ingest_window; except → log + continue
        └── seta backfill_concluido_em se todas success
```

## Fluxo de enriquecimento (sob demanda)

```
[user] clica "↻ Atualizar dados públicos" no /dashboard/processos/<id>/
    ↓
[POST] /dashboard/processos/<id>/enriquecer/ → enriquecer_processo.delay(pk)
    ↓
[fila default] enriquecer_processo:
    ├── Trf1Enricher.enriquecer(processo)
    │   ├── GET listView.seam → ViewState + form_fields + script_id dinâmico
    │   ├── POST CNJ → linkAdv (detalhe)
    │   ├── GET detalhe → BeautifulSoup
    │   ├── _extrair_dados (div.propertyView): classe, assunto, autuação, valor, juízo
    │   ├── _extrair_partes (poloAtivo, poloPassivo, outros)
    │   │   └── _parse_polo: pula header, descarta concatenado, extrai principal+reps
    │   └── transaction.atomic:
    │       ├── apaga ProcessoParte antigos do processo
    │       ├── upsert Parte (dedupe por documento OU oab)
    │       ├── cria ProcessoParte (constraint partial: dedupe só onde representa IS NULL)
    │       └── seta enriquecido_em
    ↓
[user] recarrega — vê classe + assunto + órgão + partes em cards (polo ativo/passivo/outros)
```

## Isolamento

- **`tribunals/`** é o **único app que define models do domínio**. Outros apps importam, nunca declaram.
- **`djen/`** depende de `tribunals/` (lê/escreve Process e Movimentacao). Não tem acesso direto a `enrichers/` nem a `api/`.
- **`enrichers/`** depende de `tribunals/`. Não tem acesso a `djen/` (exceto pra reusar `ProxyScrapePool`).
- **`api/`** e **`dashboard/`** dependem de `tribunals/` e podem disparar jobs de `djen/` ou `enrichers/`. Nunca declaram models nem fazem ingestão direta.

Detalhes adicionais em [`PATTERNS.md`](PATTERNS.md).
