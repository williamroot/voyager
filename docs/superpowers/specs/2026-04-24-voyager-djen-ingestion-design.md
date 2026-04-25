# Voyager — DJEN Ingestion & Dashboard

**Data:** 2026-04-24
**Status:** Design aprovado, pendente review por agentes
**Autor:** William Alves (em colaboração com Claude)

## Sumário

Voyager é um sistema Django/DRF para ingestão completa do **Diário de Justiça Eletrônico Nacional (DJEN)** organizado por tribunal, com armazenamento normalizado a nível de processo + movimentações, busca textual via Postgres, dashboard interno com gráficos e API REST autenticada por API key.

**Tribunais no go-live:** TRF1 e TRF3 (configuração permite ligar TRF2/4/5/6/TJSP depois sem mudança de código).

**Backfill:** completo, desde o primeiro dia em que cada tribunal aparece na DJEN (descoberto via busca binária no boot).

## 1. Arquitetura geral

### 1.1 Stack

- **Linguagem/Framework:** Python 3.12, Django 5.x, Django REST Framework
- **Banco:** Postgres 16 (com `pg_trgm` e `unaccent`)
- **Fila/Cache:** Redis 7
- **Worker:** django-rq + rq-scheduler
- **Frontend:** Tailwind CSS, HTMX 2, Alpine.js 3, Apache ECharts 5, Lucide Icons (build via Vite)
- **Web server:** Gunicorn + Nginx (TLS termination, static, allowlist em paths sensíveis)
- **Dependências:** `requirements.txt` + `requirements-dev.txt` (sem uv/poetry)
- **Configuração:** `core/settings.py` único, lendo via `django-environ` do `.env`

### 1.2 Serviços (docker-compose)

```
web         gunicorn (DRF + dashboard + admin)
worker_ingestion  rqworker djen_ingestion djen_backfill   (replicas escaláveis)
worker_default    rqworker default                          (manutenção/exports)
scheduler   rqscheduler                                     (crons no Redis)
postgres    postgres:16-alpine
redis       redis:7-alpine (AOF on)
nginx       nginx:alpine
```

Volumes: `postgres_data`, `redis_data`, `static`. Healthchecks em `postgres`, `redis`, `web`.

### 1.3 Apps Django

```
voyager/
├── core/         settings.py (único), urls.py, middleware.py, healthcheck
├── tribunals/    Tribunal, Process, Movimentacao, IngestionRun, SchemaDriftAlert
├── djen/         client, proxies, parser, ingestion, jobs, scheduler config, mgmt commands
├── api/          viewsets DRF, serializers, filters, paginação, API-key auth
└── dashboard/    views, templates HTMX, charts ECharts, login
```

### 1.4 Fluxo de dados

```
[ rq-scheduler ]
       │ enqueue 1 job/tribunal/dia
       ▼
[ worker_ingestion ]──HTTP──▶ DJEN API (via ProxyScrapePool, fallback Cortex)
       │
       │ parse + drift detection
       │
       ▼
   Postgres ──── upsert Process / bulk_create Movimentacao (ignore_conflicts)
                 update IngestionRun metrics
                 create SchemaDriftAlert se necessário

Cliente HTTP ──▶ Nginx ──▶ Gunicorn ──▶ DRF (HasAPIKey)        ──▶ Postgres
Browser      ──▶ Nginx ──▶ Gunicorn ──▶ Dashboard (sessão)     ──▶ Postgres + materialized views
```

## 2. Modelo de dados

```python
# tribunals/models.py

class Tribunal(models.Model):
    sigla = models.CharField(max_length=10, primary_key=True)   # 'TRF1', 'TRF3'
    nome = models.CharField(max_length=200)
    sigla_djen = models.CharField(max_length=20)                # parâmetro siglaTribunal da API
    ativo = models.BooleanField(default=True)
    overlap_dias = models.PositiveIntegerField(default=3)       # sobreposição da ingestão diária
    data_inicio_disponivel = models.DateField(null=True, blank=True)
    backfill_concluido_em = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Process(models.Model):
    numero_cnj = models.CharField(max_length=25)                # NNNNNNN-DD.AAAA.J.TR.OOOO
    tribunal = models.ForeignKey(Tribunal, on_delete=PROTECT, related_name='processos')
    primeira_movimentacao_em = models.DateTimeField(null=True)
    ultima_movimentacao_em  = models.DateTimeField(null=True)
    total_movimentacoes = models.PositiveIntegerField(default=0)
    inserido_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['tribunal', 'numero_cnj'], name='uniq_proc_tribunal_cnj'),
        ]
        indexes = [
            Index(fields=['tribunal', 'numero_cnj']),
            Index(fields=['tribunal', '-ultima_movimentacao_em']),
            Index(fields=['inserido_em']),
        ]


class Movimentacao(models.Model):
    processo = models.ForeignKey(Process, on_delete=CASCADE, related_name='movimentacoes')
    tribunal = models.ForeignKey(Tribunal, on_delete=PROTECT, related_name='movimentacoes')
    external_id = models.CharField(max_length=64)               # 'id' do payload DJEN
    data_disponibilizacao = models.DateTimeField()
    inserido_em = models.DateTimeField(auto_now_add=True)

    tipo_comunicacao = models.CharField(max_length=120, blank=True)
    tipo_documento   = models.CharField(max_length=120, blank=True)
    nome_orgao       = models.CharField(max_length=255, blank=True)
    nome_classe      = models.CharField(max_length=255, blank=True)
    codigo_classe    = models.CharField(max_length=20, blank=True)
    link             = models.URLField(max_length=500, blank=True)
    destinatarios    = models.JSONField(default=list)
    texto            = models.TextField(blank=True)

    search_vector = SearchVectorField(null=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['tribunal', 'external_id'], name='uniq_mov_tribunal_extid'),
        ]
        indexes = [
            Index(fields=['processo', '-data_disponibilizacao']),
            Index(fields=['tribunal', '-data_disponibilizacao']),
            Index(fields=['inserido_em']),
            GinIndex(fields=['search_vector']),
            GinIndex(name='mov_texto_trgm', fields=['texto'], opclasses=['gin_trgm_ops']),
        ]


class IngestionRun(models.Model):
    tribunal = models.ForeignKey(Tribunal, on_delete=PROTECT)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20)                    # running|success|failed
    janela_inicio = models.DateField()
    janela_fim    = models.DateField()
    paginas_lidas = models.PositiveIntegerField(default=0)
    movimentacoes_novas = models.PositiveIntegerField(default=0)
    movimentacoes_duplicadas = models.PositiveIntegerField(default=0)
    processos_novos = models.PositiveIntegerField(default=0)
    erros = models.JSONField(default=list)


class SchemaDriftAlert(models.Model):
    tribunal = models.ForeignKey(Tribunal, on_delete=PROTECT)
    detectado_em = models.DateTimeField(auto_now_add=True)
    tipo = models.CharField(max_length=20)        # 'extra_keys' | 'missing_keys' | 'type_mismatch'
    chaves = models.JSONField()                   # ex: ['novo_campo_x']
    exemplo = models.JSONField()                  # 1 item da resposta que disparou
    ingestion_run = models.ForeignKey(IngestionRun, on_delete=SET_NULL, null=True)
    resolvido = models.BooleanField(default=False)
    resolvido_em = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=['tribunal', 'tipo'],
                condition=Q(resolvido=False),
                name='uniq_alerta_aberto_por_tipo_tribunal',
            ),
        ]
```

### 2.1 Decisões de modelagem

1. **Dedupe por `(tribunal_id, external_id)`** com `UniqueConstraint`. Ingestão usa `bulk_create(..., ignore_conflicts=True)` — idempotente, sem race entre workers. Métricas de "novos vs duplicados" exigem 1 SELECT extra por batch (`SELECT external_id WHERE tribunal=X AND external_id IN (...)`).
2. **Sem coluna `payload` JSONB** (decisão explícita): em vez de guardar payload bruto, mapeamos apenas o conjunto `EXPECTED_KEYS` e disparamos `SchemaDriftAlert` quando a resposta DJEN tem chaves novas/faltantes.
3. **`tribunal` denormalizado em Movimentacao** (FK redundante com `processo.tribunal`): evita JOIN nas queries dominantes ("movimentações do TRF1 nos últimos N dias"). Validar consistência em `Movimentacao.save()` (`assert self.tribunal_id == self.processo.tribunal_id`).
4. **Busca textual dupla**: `SearchVectorField` para full-text com ranking; `pg_trgm` GIN sobre `texto` para substring/termos curtos. Trigger SQL atualiza `search_vector` em INSERT/UPDATE de `texto`/`nome_orgao`/`nome_classe` com config `portuguese`.
5. **`Tribunal` como entidade**: liga/desliga via `ativo`, configura `overlap_dias` por tribunal sem mudança de código, guarda `data_inicio_disponivel` e `backfill_concluido_em`.
6. **`primeira/ultima_movimentacao_em` em Process**: agregado denormalizado, atualizado em batch ao final de cada chunk de ingestão (UPDATE com subquery para os processos tocados). Evita `MAX/MIN` em runtime.
7. **`inserido_em` em Process e Movimentacao**: requisito do usuário (data de inserção independe da data DJEN).

### 2.2 Tribunais inicialmente cadastrados (data migration)

| sigla | sigla_djen | nome                                          | ativo |
|-------|------------|-----------------------------------------------|-------|
| TRF1  | TRF1       | Tribunal Regional Federal da 1ª Região        | True  |
| TRF3  | TRF3       | Tribunal Regional Federal da 3ª Região        | True  |
| TRF2  | TRF2       | Tribunal Regional Federal da 2ª Região        | False |
| TRF4  | TRF4       | Tribunal Regional Federal da 4ª Região        | False |
| TRF5  | TRF5       | Tribunal Regional Federal da 5ª Região        | False |
| TRF6  | TRF6       | Tribunal Regional Federal da 6ª Região        | False |
| TJSP  | TJSP       | Tribunal de Justiça do Estado de São Paulo    | False |

## 3. Pipeline de ingestão

### 3.1 DJENClient (`djen/client.py`)

- `iter_pages(tribunal, data_inicio: date, data_fim: date)` → gerador de páginas.
- GET `https://comunicaapi.pje.jus.br/api/v1/comunicacao` com `siglaTribunal`, `dataDisponibilizacaoInicio`, `dataDisponibilizacaoFim`, `pagina`, `itensPorPagina=100`.
- Retry exponencial em 429/5xx: `min(60, 3 * 2**attempt + jitter)`, máx 5 tentativas. Cada retry rota proxy novo.
- 4xx não-429: levanta `DjenClientError`, sem retry.
- Sleep 1.0s entre páginas (config `DJEN_PAGE_SLEEP_SECONDS`).
- Timeouts: connect=10s, read=60s.
- Headers: `User-Agent: voyager-ingestion/0.1 (+contato)`, `Accept: application/json`.
- Logs estruturados por request: `tribunal`, `pagina`, `proxy_id`, `status_code`, `latency_ms`.

### 3.2 Proxies (`djen/proxies.py`)

```python
class ProxyScrapePool:
    """Pool rotativo via API ProxyScrape, recarregado periodicamente, compartilhado entre workers via Redis."""
    def get(self) -> str: ...
    def mark_bad(self, url: str): ...        # remove da rotação por N min
    def refresh(self): ...                   # busca lista nova da API ProxyScrape


class CortexFallback:
    """Proxy fixo (settings.CORTEX_PROXY_URL). Acionado após N falhas seguidas do ProxyScrapePool."""
```

- Pool armazenado em Redis (chave `voyager:proxies:scrape:list` + `voyager:proxies:scrape:bad:<url>` com TTL).
- Job `refresh_proxy_pool` na fila `default`, agendado a cada 15 min.
- Marcação de "bad" automática em erros de conexão ou 429 repetido.

### 3.3 Parser + drift (`djen/parser.py`)

```python
EXPECTED_KEYS = {
    'id', 'numero_processo', 'numeroprocessocommascara',
    'siglaTribunal', 'nomeOrgao', 'tipoComunicacao', 'tipoDocumento',
    'data_disponibilizacao', 'texto', 'destinatarios',
    'nomeClasse', 'codigoClasse', 'link',
}

def parse_item(item: dict, tribunal: Tribunal, run: IngestionRun) -> tuple[str, Movimentacao] | None:
    keys = set(item.keys())
    extra = keys - EXPECTED_KEYS
    missing = EXPECTED_KEYS - keys
    if extra:
        SchemaDriftAlert.objects.update_or_create(
            tribunal=tribunal, tipo='extra_keys', resolvido=False,
            defaults={'chaves': sorted(extra), 'exemplo': item, 'ingestion_run': run},
        )
    if missing:
        SchemaDriftAlert.objects.update_or_create(
            tribunal=tribunal, tipo='missing_keys', resolvido=False,
            defaults={'chaves': sorted(missing), 'exemplo': item, 'ingestion_run': run},
        )

    cnj = normalizar_cnj(item.get('numeroprocessocommascara') or item.get('numero_processo') or extrair_de_texto(item.get('texto', '')))
    if not cnj:
        run.erros.append({'pagina': run.paginas_lidas, 'erro': 'cnj_indisponivel', 'external_id': item.get('id')})
        return None

    return cnj, Movimentacao(
        tribunal=tribunal,
        external_id=item['id'],
        data_disponibilizacao=parse_dt(item['data_disponibilizacao']),
        tipo_comunicacao=item.get('tipoComunicacao', ''),
        tipo_documento=item.get('tipoDocumento', ''),
        nome_orgao=item.get('nomeOrgao', ''),
        nome_classe=item.get('nomeClasse', ''),
        codigo_classe=item.get('codigoClasse', ''),
        link=item.get('link', ''),
        destinatarios=item.get('destinatarios', []),
        texto=item.get('texto', ''),
    )
```

### 3.4 `ingest_window(tribunal, data_inicio, data_fim)` (`djen/ingestion.py`)

Coração da ingestão. Cria 1 `IngestionRun` por janela. Loop por página, batch de 500 movimentações:

```
1. run = IngestionRun.objects.create(tribunal, janela_inicio, janela_fim, status='running')
2. cnjs_tocados = set()
3. para cada página em client.iter_pages(...):
     items = página
     parsed = [parse_item(i, tribunal, run) for i in items]
     parsed = [p for p in parsed if p is not None]

     # 4a. upsert Process em lote (1 SELECT + bulk_create ignore_conflicts)
     cnjs_pagina = {cnj for cnj, _ in parsed}
     existentes = dict(Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina).values_list('numero_cnj', 'pk'))
     novos = [Process(tribunal=tribunal, numero_cnj=c) for c in cnjs_pagina - existentes.keys()]
     Process.objects.bulk_create(novos, ignore_conflicts=True, batch_size=500)
     # re-query pra pegar os IDs criados
     procs = dict(Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina).values_list('numero_cnj', 'pk'))
     cnjs_tocados |= cnjs_pagina

     # 4b. associa Movimentacao ao processo
     movs = [Movimentacao(processo_id=procs[cnj], **mov.__dict__) for cnj, mov in parsed]

     # 4c. métricas exigem detecção pré-insert (porque ignore_conflicts não retorna count real)
     ext_ids = [m.external_id for m in movs]
     ja_existem = set(Movimentacao.objects.filter(tribunal=tribunal, external_id__in=ext_ids).values_list('external_id', flat=True))
     novos_count = len(ext_ids) - len(ja_existem)

     # 4d. bulk_create idempotente
     Movimentacao.objects.bulk_create(movs, batch_size=500, ignore_conflicts=True)

     run.movimentacoes_novas += novos_count
     run.movimentacoes_duplicadas += len(ja_existem)
     run.processos_novos += len(novos)
     run.paginas_lidas += 1
     run.save(update_fields=['movimentacoes_novas', 'movimentacoes_duplicadas', 'processos_novos', 'paginas_lidas'])

4. atualizar Process.primeira/ultima_movimentacao_em e total_movimentacoes (UPDATE agregado SQL filtrando por cnjs_tocados)
5. run.status = 'success'; finished_at = now()
6. notificar Slack se houve drift_alerts ou run.status == 'failed'
```

Em caso de exceção: `run.status='failed'`, `erros.append(traceback)`, re-raise pra RQ marcar o job como failed (job de backfill é resumível na próxima execução; diário também).

### 3.5 Jobs RQ (`djen/jobs.py`)

```python
@job('djen_ingestion', timeout=7200)
def run_daily_ingestion(tribunal_sigla: str):
    t = Tribunal.objects.get(sigla=tribunal_sigla, ativo=True)
    if not t.backfill_concluido_em:
        logger.info(f"[{t.sigla}] backfill em andamento, pulando diário")
        return
    fim = date.today()
    inicio = fim - timedelta(days=t.overlap_dias)
    ingest_window(t, inicio, fim)


@job('djen_backfill', timeout=86400)
def run_backfill(tribunal_sigla: str, force_inicio: date | None = None):
    t = Tribunal.objects.get(sigla=tribunal_sigla)
    inicio = force_inicio or t.data_inicio_disponivel
    if not inicio:
        raise ValueError("rode `djen_descobrir_inicio` primeiro ou passe --inicio")
    fim = date.today()
    for chunk_inicio, chunk_fim in chunk_dates(inicio, fim, days=30):
        if IngestionRun.objects.filter(
            tribunal=t, status='success',
            janela_inicio=chunk_inicio, janela_fim=chunk_fim,
        ).exists():
            continue
        ingest_window(t, chunk_inicio, chunk_fim)
    Tribunal.objects.filter(pk=t.pk).update(backfill_concluido_em=timezone.now())


@job('default')
def refresh_proxy_pool():
    ProxyScrapePool.singleton().refresh()
```

### 3.6 Scheduler (`djen/scheduler.py`)

Registra crons no boot do container `scheduler`:

- `run_daily_ingestion('TRF1')` — diário 04:00 (America/Sao_Paulo)
- `run_daily_ingestion('TRF3')` — diário 04:30 (escalonado)
- `refresh_proxy_pool()` — a cada 15 min

Tribunais ativados depois entram automaticamente no schedule pois ele é rebuilt no boot lendo `Tribunal.objects.filter(ativo=True)`.

### 3.7 Descoberta do `data_inicio_disponivel`

Management command `djen_descobrir_inicio TRIBUNAL`:

1. Probes em janelas curtas (7 dias) começando de 2022-01-01.
2. Busca binária pra achar a primeira data com `count > 0`.
3. Salva em `Tribunal.data_inicio_disponivel`.
4. Idempotente; só atualiza se ainda for NULL ou `--force`.

Justificativa do floor 2022-01-01: DJEN nacional foi instituída pela Resolução CNJ 455/2022 e os tribunais aderiram progressivamente.

### 3.8 Management commands

```
python manage.py djen_descobrir_inicio TRF1 [--force]
python manage.py djen_backfill TRF1 [--inicio YYYY-MM-DD]
python manage.py djen_run_now TRF1 [--dias N]      # roda 1 ingestão sob demanda
python manage.py djen_status                        # último run por tribunal + drift alerts abertos
```

### 3.9 Ordem de subida operacional

1. `docker compose up -d` (todos os serviços).
2. `docker compose exec web python manage.py migrate` (data migration cadastra os 7 tribunais).
3. `python manage.py djen_descobrir_inicio TRF1`
4. `python manage.py djen_descobrir_inicio TRF3`
5. `python manage.py djen_backfill TRF1` (em background, fila `djen_backfill`)
6. `python manage.py djen_backfill TRF3` (em background, fila `djen_backfill`)
7. Scheduler diário já está rodando; pula tribunais com `backfill_concluido_em IS NULL`. Quando o backfill termina, atualiza o campo e o diário assume.

## 4. API REST (DRF)

### 4.1 Autenticação

`djangorestframework-api-key`. Header `Authorization: Api-Key <key>`. Sem rate limit (decisão explícita do usuário). Chaves criadas/revogadas via Django Admin.

```python
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework_api_key.permissions.HasAPIKey'],
    'DEFAULT_PAGINATION_CLASS': 'api.pagination.DefaultPagination',
    'PAGE_SIZE': 50,
}
```

Dashboard usa sessão Django, não API key — separação clara entre os dois canais.

### 4.2 Endpoints (versionados em `/api/v1/`)

| Método | Path                                         | Descrição                                                |
|--------|----------------------------------------------|----------------------------------------------------------|
| GET    | `/api/v1/tribunais/`                         | Lista tribunais (sigla, nome, ativo, datas)              |
| GET    | `/api/v1/tribunais/{sigla}/`                 | Detalhe + estatísticas                                   |
| GET    | `/api/v1/processos/`                         | Listagem paginada com filtros                            |
| GET    | `/api/v1/processos/{id_or_cnj}/`             | Detalhe (lookup aceita id numérico ou CNJ)               |
| GET    | `/api/v1/processos/{id_or_cnj}/movimentacoes/` | Movimentações do processo                              |
| GET    | `/api/v1/movimentacoes/`                     | Busca + filtros                                          |
| GET    | `/api/v1/movimentacoes/{id}/`                | Detalhe                                                  |
| GET    | `/api/v1/ingestion-runs/`                    | Histórico de execuções                                   |
| GET    | `/api/v1/health/`                            | Healthcheck (sem API key)                                |
| GET    | `/api/v1/schema/`                            | OpenAPI 3.1 (drf-spectacular)                            |
| GET    | `/api/v1/docs/`                              | Swagger UI                                               |

### 4.3 Filtros (django-filter)

```python
class ProcessFilter(filters.FilterSet):
    tribunal = filters.CharFilter(field_name='tribunal__sigla')
    tribunal__in = filters.BaseInFilter(field_name='tribunal__sigla')
    numero_cnj = filters.CharFilter(lookup_expr='exact')
    inserido_em__gte = filters.DateTimeFilter()
    inserido_em__lte = filters.DateTimeFilter()
    ultima_movimentacao_em__gte = filters.DateTimeFilter()
    sem_movimentacoes = filters.BooleanFilter(method='filter_sem_movs')
    ordering_fields = ('inserido_em', 'ultima_movimentacao_em', 'total_movimentacoes')


class MovimentacaoFilter(filters.FilterSet):
    tribunal = filters.CharFilter(field_name='tribunal__sigla')
    tribunal__in = filters.BaseInFilter(field_name='tribunal__sigla')
    processo = filters.NumberFilter()
    numero_cnj = filters.CharFilter(field_name='processo__numero_cnj')
    data_disponibilizacao__gte = filters.DateTimeFilter()
    data_disponibilizacao__lte = filters.DateTimeFilter()
    inserido_em__gte = filters.DateTimeFilter()
    inserido_em__lte = filters.DateTimeFilter()
    tipo_comunicacao = filters.CharFilter()
    nome_classe = filters.CharFilter()
    codigo_classe = filters.CharFilter()
    q = filters.CharFilter(method='filter_search')
    ordering_fields = ('data_disponibilizacao', 'inserido_em')
```

### 4.4 Busca textual (`?q=...`)

```python
def filter_search(self, qs, name, value):
    value = value.strip()
    if not value:
        return qs
    if len(value.split()) >= 3:
        query = SearchQuery(value, config='portuguese', search_type='websearch')
        return qs.filter(search_vector=query).annotate(
            rank=SearchRank('search_vector', query)
        ).order_by('-rank', '-data_disponibilizacao')
    return qs.filter(texto__icontains=value).order_by('-data_disponibilizacao')
```

### 4.5 Serializers

Dois pares (List/Detail) para Process e Movimentacao. Detail estende List adicionando os campos pesados (`texto`, `destinatarios`, etc.). ViewSets escolhem via `get_serializer_class()`.

### 4.6 Paginação

- `LimitOffsetPagination` (default 50, max 200) para tribunais/processos/runs.
- `CursorPagination` ordenando por `('-data_disponibilizacao', '-id')` para `movimentacoes/` (volume alto, ordenação estável).

### 4.7 Performance

- `select_related('tribunal', 'processo')` em todos os list/retrieve.
- `only(...)` em list serializers.
- `EXPLAIN ANALYZE` documentado em `docs/runbook/queries-quentes.md` para regressões.

### 4.8 OpenAPI

`drf-spectacular` em `/api/v1/schema/` e `/api/v1/docs/`. Swagger UI também exige API key.

### 4.9 Erros

`{"detail": "...", "code": "..."}`. 401 sem chave, 403 com chave revogada, 404 lookup inválido, 400 erro de validação, 500 logado com `request_id`.

## 5. Dashboard

### 5.1 Stack visual

- Django templates + HTMX 2 + Alpine.js 3 (interações pontuais).
- Tailwind CSS via build Vite no stage `frontend` do Dockerfile.
- Apache ECharts 5 (npm, bundlado).
- Lucide Icons (SVG inline).
- Visual moderno tipo Linear/Vercel: dark mode default, tipografia Inter, cards `rounded-2xl border border-zinc-800/40`.

### 5.2 Autenticação

Sessão Django, login em `/dashboard/login/`. Único perfil de leitura. Usuários criados via `createsuperuser` ou admin. Sem self-signup.

### 5.3 Páginas

| Path                              | Descrição                                  |
|-----------------------------------|--------------------------------------------|
| `/dashboard/`                     | Visão geral (KPIs + gráficos globais)      |
| `/dashboard/tribunais/<sigla>/`   | Drill-down por tribunal                    |
| `/dashboard/processos/`           | Listagem com busca + filtros               |
| `/dashboard/processos/<id>/`      | Timeline de movimentações                  |
| `/dashboard/movimentacoes/`       | Busca livre + filtros                      |
| `/dashboard/ingestao/`            | Saúde operacional (runs, drift, proxies)   |
| `/dashboard/login/`               | Login                                      |

### 5.4 Visão geral

KPIs (4 cards animados): total processos, total movs, movs em 24h, última atualização. Cada um com sparkline (30 pontos) e delta vs período anterior.

Gráficos:
1. **Volume diário por tribunal** — line empilhado, botões 7d/30d/90d/365d.
2. **Distribuição por tribunal** — donut, click filtra os outros widgets via HTMX.
3. **Top tipos de comunicação** — horizontal bar, top 15.
4. **Heatmap calendar (último ano)** — densidade diária; gaps em vermelho indicam falha de ingestão.
5. **Top 10 órgãos julgadores** — horizontal bar.

### 5.5 Drill-down por tribunal

Mesmos widgets escopados ao tribunal + dois extras:
- Heatmap "tipo × dia da semana".
- Cobertura temporal: barra `data_inicio_disponivel → hoje` com gaps em vermelho onde falta `IngestionRun(success)`.

Cards: data início, último run, status backfill, drift alerts abertos.

### 5.6 Listagem de processos

Sticky filters à esquerda (tribunal, ranges de data, "tem N+ movs"); busca CNJ no topo; tabela densa com paginação cursor; toggle "modo cartão" (Alpine).

### 5.7 Detalhe de processo

Header com metadados; **timeline vertical** das movimentações (data, tipo, órgão, primeiras 200 chars, expand pra texto completo); filtros inline com swap HTMX só da lista.

### 5.8 Busca livre de movimentações

Filtros + campo `q` (mesma lógica da API). Highlight dos termos. Botão "exportar CSV" enfileira job na fila `default`; download em `/dashboard/exports/` quando pronto (polling HTMX a cada 5s).

### 5.9 Saúde operacional

Tela única com:
- Tabela de runs recentes (status badge, métricas, expand de erros).
- Cards vermelhos para drift alerts abertos com botão "marcar resolvido".
- Status do `ProxyScrapePool` (saudáveis vs ruins, último refresh) e Cortex on/off.
- Tamanho das filas RQ + workers ativos. Auto-refresh HTMX 10s.

### 5.10 Padrões HTMX

```html
<form hx-get="{% url 'dashboard:overview' %}"
      hx-trigger="change delay:300ms, submit"
      hx-target="#widgets-area"
      hx-select="#widgets-area"
      hx-push-url="true">
  ...filtros...
</form>

<script>
  htmx.on('htmx:afterSwap', (e) => {
    e.detail.target.querySelectorAll('[data-echart]').forEach(initChart);
  });
</script>
```

URL sincronizada com filtros (back/forward funciona, link compartilhável), sem SPA boilerplate.

### 5.11 Performance

Materialized views Postgres atualizadas a cada 15 min via job `refresh_dashboard_views` na fila `default`:

- `mv_movs_por_dia_tribunal (dia, tribunal_id, count)`
- `mv_movs_por_tipo_tribunal (tipo_comunicacao, tribunal_id, count)`
- `mv_movs_por_orgao_tribunal_90d (nome_orgao, tribunal_id, count)`

Cache de fragmentos (`{% cache 300 ... %}`) em widgets que não dependem de filtros.

### 5.12 Acessibilidade

Foco visível, contraste AA, atalhos `g h`, `g i`, `/`. Layout responsivo (filtros viram drawer em mobile).

## 6. Operação, observabilidade e infra

### 6.1 docker-compose

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment: [POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD]
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./infra/pg_init:/docker-entrypoint-initdb.d:ro
    healthcheck: pg_isready
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--maxmemory", "512mb", "--maxmemory-policy", "allkeys-lru", "--appendonly", "yes"]
    volumes: [redis_data:/data]
    healthcheck: redis-cli ping
  web:
    build: { context: ., target: runtime }
    command: gunicorn core.wsgi:application -b 0.0.0.0:8000 -w 4 -k gthread --threads 4 --access-logfile -
    depends_on: { postgres: { condition: service_healthy }, redis: { condition: service_healthy } }
    env_file: .env
    healthcheck: curl -f http://localhost:8000/api/v1/health/
  worker_ingestion:
    build: { context: ., target: runtime }
    command: python manage.py rqworker djen_ingestion djen_backfill
    deploy: { replicas: 2 }
    depends_on: [redis, postgres]
    env_file: .env
  worker_default:
    build: { context: ., target: runtime }
    command: python manage.py rqworker default
    depends_on: [redis, postgres]
    env_file: .env
  scheduler:
    build: { context: ., target: runtime }
    command: python manage.py rqscheduler --interval=30
    depends_on: [redis, postgres]
    env_file: .env
  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./infra/nginx.conf:/etc/nginx/nginx.conf:ro
      - static:/var/www/static:ro
      - ./infra/certs:/etc/nginx/certs:ro
    depends_on: [web]

volumes: { postgres_data: {}, redis_data: {}, static: {} }
```

`docker-compose.prod.yml` faz override (replicas maiores, restart `unless-stopped`, log rotation, mem_limit).

### 6.2 Dockerfile (multi-stage)

```dockerfile
FROM node:20-alpine AS frontend
WORKDIR /front
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y build-essential libpq-dev && apt-get autoremove -y
COPY --from=frontend /front/dist /app/static/dist
COPY . .
RUN python manage.py collectstatic --noinput
USER 1000
CMD ["gunicorn", "core.wsgi:application", "-b", "0.0.0.0:8000"]
```

### 6.3 Configuração

`core/settings.py` único, todas as variáveis lidas de `.env` via `django-environ`:

```python
import environ
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env()
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('DJANGO_SECRET_KEY')
DEBUG = env.bool('DJANGO_DEBUG', default=False)
ALLOWED_HOSTS = env.list('DJANGO_ALLOWED_HOSTS', default=[])
DATABASES = {'default': env.db('DATABASE_URL')}
RQ_QUEUES = {
    'default':         {'URL': env('REDIS_URL')},
    'djen_ingestion':  {'URL': env('REDIS_URL'), 'DEFAULT_TIMEOUT': 7200},
    'djen_backfill':   {'URL': env('REDIS_URL'), 'DEFAULT_TIMEOUT': 86400},
}

DJEN_BASE_URL = env('DJEN_BASE_URL', default='https://comunicaapi.pje.jus.br/api/v1/comunicacao')
DJEN_REQUEST_TIMEOUT_CONNECT = env.int('DJEN_REQUEST_TIMEOUT_CONNECT', default=10)
DJEN_REQUEST_TIMEOUT_READ = env.int('DJEN_REQUEST_TIMEOUT_READ', default=60)
DJEN_PAGE_SLEEP_SECONDS = env.float('DJEN_PAGE_SLEEP_SECONDS', default=1.0)
DJEN_MAX_RETRIES = env.int('DJEN_MAX_RETRIES', default=5)

PROXYSCRAPE_API_KEY = env('PROXYSCRAPE_API_KEY')
PROXYSCRAPE_REFRESH_SECONDS = env.int('PROXYSCRAPE_REFRESH_SECONDS', default=900)
CORTEX_PROXY_URL = env('CORTEX_PROXY_URL', default='')
CORTEX_FALLBACK_ENABLED = env.bool('CORTEX_FALLBACK_ENABLED', default=True)

SLACK_WEBHOOK_URL = env('SLACK_WEBHOOK_URL', default='')
SLACK_NOTIFY_DRIFT = env.bool('SLACK_NOTIFY_DRIFT', default=True)
SLACK_NOTIFY_FAILED_RUN = env.bool('SLACK_NOTIFY_FAILED_RUN', default=True)

SENTRY_DSN = env('SENTRY_DSN', default='')
SENTRY_ENVIRONMENT = env('SENTRY_ENVIRONMENT', default='production')
SENTRY_TRACES_SAMPLE_RATE = env.float('SENTRY_TRACES_SAMPLE_RATE', default=0.05)
```

`.env.example` versionado, `.env` no `.gitignore`.

### 6.4 Logging

`structlog` + `python-json-logger`. JSON em prod, texto colorido em dev. Campos padrão: `timestamp`, `level`, `event`, `logger`, `tribunal`, `ingestion_run_id`, `pagina`, `proxy`, `status_code`, `latency_ms`, `request_id`.

`RequestIdMiddleware` injeta `X-Request-Id` em todo request, propaga pra logs e response header.

### 6.5 Erros e métricas

- **Sentry**: `sentry-sdk[django]`, captura exceções, breadcrumbs, performance dos endpoints e jobs RQ. Tags `tribunal` e `job_kind`.
- **Healthcheck** `/api/v1/health/`:

```json
{
  "db": "ok",
  "redis": "ok",
  "tribunais": [
    {"sigla": "TRF1", "ultimo_run": "2026-04-24T04:12:00Z", "status": "success", "lag_horas": 13.2}
  ],
  "drift_alerts_abertos": 0,
  "fila_djen_ingestion": 3,
  "fila_djen_backfill": 0
}
```

Status 503 se algum tribunal ativo `lag_horas > 36` ou `drift_alerts_abertos > 0`. Sem API key.

- **Prometheus**: `django-prometheus` em `/metrics` (sem API key, restrito por IP no nginx). Métricas custom: `voyager_djen_pages_total`, `voyager_djen_movimentacoes_inseridas_total`, `voyager_djen_request_duration_seconds`, `voyager_djen_429_total`, `voyager_proxy_pool_healthy`.

### 6.6 Backups

- **Postgres**: `pg_dump` diário (job RQ ~03:00) → `/backups/voyager-YYYYMMDD.sql.gz`, retenção 30d. Cópia opcional pra S3 (`BACKUP_S3_BUCKET`).
- **Redis**: AOF on; RDB diário adicional. Filas reconstruíveis.
- **Restore documentado** em `docs/runbook/restore.md`.

### 6.7 Migrations & zero-downtime

- CI roda `python manage.py makemigrations --check --dry-run`.
- Convenção: nunca dropar coluna em uma única deploy (etapa 1: nullable + parar de escrever; etapa 2: drop). Documentado em `CONTRIBUTING.md`.
- `IngestionRun` com particionamento Postgres por mês quando passar de 100k linhas (lazy).

### 6.8 CI/CD (GitHub Actions)

1. `lint` — `ruff check` + `ruff format --check`.
2. `test` — `pytest -q --cov=voyager --cov-fail-under=80` com Postgres + Redis em service containers.
3. `build` — imagem multi-stage com cache GHCR.
4. `migrate-check` — Postgres efêmero, todas migrations from scratch.
5. Deploy manual em tag `v*` → `docker compose pull && docker compose up -d` via SSH action.

### 6.9 Testes

- `pytest` + `pytest-django` + `factory_boy` + `responses`.
- Camadas: unit (parser, dedupe, drift, proxy pool), integration (`ingest_window` end-to-end com Postgres real, DJEN mockado via `responses`), api (DRF com `APIClient`), smoke (`djen_run_now TRF1 --dry-run` em staging).
- Cobertura mínima 80% global, 95% em `djen/` e `tribunals/models.py`.

### 6.10 Segurança

- Secrets só em env. CI usa GitHub secrets.
- Postgres com role do app (sem SUPERUSER); migrations com role separado.
- API keys: hash do segredo no banco (lib `djangorestframework-api-key` já faz).
- Headers: HSTS, `SECURE_SSL_REDIRECT=True`, `SESSION_COOKIE_SECURE`.
- `/admin/` restrito por IP no nginx.
- Dependências: `pip-audit` no CI, Dependabot habilitado.

### 6.11 Estrutura final do repo

```
voyager/
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── CONTRIBUTING.md
├── docker-compose.yml
├── docker-compose.prod.yml
├── Dockerfile
├── .env.example
├── manage.py
├── core/
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   ├── asgi.py
│   └── middleware.py
├── tribunals/
│   ├── models.py
│   ├── admin.py
│   └── migrations/
├── djen/
│   ├── client.py
│   ├── proxies.py
│   ├── parser.py
│   ├── ingestion.py
│   ├── jobs.py
│   ├── scheduler.py
│   └── management/commands/
│       ├── djen_descobrir_inicio.py
│       ├── djen_backfill.py
│       ├── djen_run_now.py
│       └── djen_status.py
├── api/
│   ├── viewsets.py
│   ├── serializers.py
│   ├── filters.py
│   ├── pagination.py
│   ├── permissions.py
│   └── urls.py
├── dashboard/
│   ├── views.py
│   ├── urls.py
│   ├── templates/
│   └── static/
├── frontend/                       (Vite + Tailwind + ECharts + Alpine)
├── infra/
│   ├── nginx.conf
│   ├── pg_init/01-extensions.sql   (CREATE EXTENSION pg_trgm; unaccent;)
│   └── certs/
├── docs/
│   ├── superpowers/specs/
│   └── runbook/
└── tests/
```

### 6.12 Runbook (`docs/runbook/`)

- `restore.md` — restore Postgres/Redis.
- `backfill.md` — rodar/retomar backfill.
- `drift.md` — passos quando aparece `SchemaDriftAlert` (analisar, atualizar mapeamento, migration, marcar resolvido).
- `proxies.md` — saúde do pool, alternar Cortex.
- `escalar-workers.md` — aumentar replicas no compose.
- `queries-quentes.md` — `EXPLAIN ANALYZE` dos endpoints chave.

## 7. Riscos e mitigações

| Risco | Impacto | Mitigação |
|-------|---------|-----------|
| Volume DJEN inesperado (TJSP) | Backfill demora dias | Worker scaling via replicas; chunks de 30d com retomada idempotente |
| DJEN muda schema sem aviso | Dados perdidos silenciosamente | `SchemaDriftAlert` + notificação Slack + dashboard de saúde |
| ProxyScrape sem créditos / fora do ar | Ingestão para | Fallback Cortex automático após N falhas |
| 429 mesmo com proxies | Backoff travado | Retry exponencial com jitter, máx 5; depois falha o run e tenta no próximo ciclo |
| Race entre workers no upsert de Process | Duplicatas | `UniqueConstraint(tribunal, numero_cnj)` + `bulk_create(ignore_conflicts=True)` |
| Search vector lento em volume grande | API e dashboard travam | Trigger SQL atualiza vector no insert; GIN index; tsquery+rank apenas para queries longas, trigram pra termos curtos |
| Dashboard agrega tudo em runtime | Tela lenta com 365d × 7 tribunais | Materialized views refrescadas a cada 15 min |

## 8. Open questions / itens não decididos

- Notificação Slack: webhook único ou canal por tipo (drift × falha de run)? Default: webhook único, configurável depois se necessário.
- Particionamento de `Movimentacao`: quando ligar? Default: lazy, quando dor aparecer (>50M rows ou queries lentas).
- Exportação CSV: qual o limite? Default: sem limite, mas job no worker_default com timeout de 30 min.
- 2FA no admin: futuro, fora do escopo do MVP.

## 9. Não-objetivos (escopo declarado fora)

- Extração de valores monetários do `texto` via regex (falcon faz isso; voyager apenas armazena texto cru).
- Filtragem por termos no momento da ingestão (voyager armazena tudo).
- Webhooks/subscriptions para clientes da API (fase futura).
- CRUD público da API (read-only).
- Multi-tenancy / múltiplas organizações (single-tenant por enquanto).
