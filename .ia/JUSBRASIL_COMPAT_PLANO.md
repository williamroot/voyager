# Plano: API compatível Jusbrasil/Digesto + Elasticsearch + PDF storage

> Especificação alvo: https://api.jusbrasil.com.br/docs/diarios_oficiais/busca.html
> e módulo de Monitoramento (webhook). Decisão: compatibilidade **completa**,
> **exceto busca por comarca** (granularidade por tribunal).

Este doc é o **plano de implementação** — o de/para conceitual está em
[`JUSBRASIL_COMPAT.md`](JUSBRASIL_COMPAT.md) (gerado na análise prévia).
Aqui focamos em **o quê, por quem, em que ordem, com quais testes**.

## Metas

1. **Busca rápida e compatível**: substituir a busca híbrida Postgres
   (tsquery + ILIKE) por **Elasticsearch** — aceita a query DSL nativa do
   Jusbrasil (`POST /doc/buscar` com body ES 7.14) sem tradução.
2. **Armazenar PDFs**: baixar o `Movimentacao.link` (PDF da DJEN) pra MinIO
   (S3-compat local) e expor `cached_docurl` — análogo ao Jusbrasil.
3. **API 100% compatível**: endpoints espelhando Jusbrasil em
   `/api/v1/diarios-oficiais/*` + `/api/monitoramento/*` (webhook push).
4. **Sem quebrar clientes atuais**: rotas `/api/v1/movimentacoes/` etc
   mantidas; rotas Jusbrasil-compat são **adicionais**.

## Não-metas (decisões)

- **Busca por comarca**: fora de escopo. Granularidade = tribunal. Documentado.
- **`prev_page`/`next_page`/`num_pag_original`**: DJEN não dá página do
  diário — sempre `null`.
- **`proc_alt`/`proc_apens`**: não existe no Voyager (processos pré-CNJ) —
  sempre `null`.
- **Multi-tenancy**: mantido single-tenant.

## Apps novos (4)

| App | Função |
|---|---|
| `search/` | Wrapper Elasticsearch + mappings + indexação write-through |
| `pdf_storage/` | Download e armazenamento de PDFs no MinIO |
| `monitoring/` | Monitoramento push (webhook): terms, persons, procs, detections |
| `mcp_server/` | MCP server (tools/resources) pra LLMs conversarem sobre processos |

Não declaram models de domínio (só em `tribunals/`). O `search/` e
`pdf_storage/` têm models próprios auxiliares (status de indexação/download).

## Containers novos (2)

| Container | Imagem | Mem | Volume |
|---|---|---|---|
| `elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:8.14.0` | 4GB heap | `es_data` |
| `minio` | `minio/minio:latest` | 1GB | `minio_data` |

ES single-node em dev; em prod, decisão de cluster size após medir volume
(ver Fase A · Gate 3).

---

## De/para (resumo — ver JUSBRASIL_COMPAT.md pra detalhes)

| Jusbrasil | Voyager | Notas |
|---|---|---|
| `source` (id caderno) | `FonteDiario.source_id` → `Tribunal.sigla` | 1:1 por tribunal (sem comarca) |
| `publish_date` | `Movimentacao.data_disponibilizacao` | ISO UTC |
| `available_at` / `detected_at` | `Movimentacao.inserido_em` | ISO UTC |
| `doc_id` / `recorte_id` | `Movimentacao.id` | bigint → str |
| `body` | `Movimentacao.texto` | direto |
| `docurl` | `Movimentacao.link` | direto |
| `cached_docurl` | URL MinIO (PDF baixado) | novo |
| `prev/next_page`, `num_pag_original` | `null` | limitação |
| `proc` | `Process.numero_cnj` | direto |
| `proc_alt`, `proc_apens` | `null` | não existe |
| `advs` | `ProcessoParte` (papel=ADVOGADO) serializado | serializer |
| `partes` | `ProcessoParte` serializado | serializer |
| `assunto` | `Process.assunto_nome` | direto |
| `assunto_norm` | `Movimentacao.assunto_norm` (novo jsonb) | classificador heurístico |
| `periodico_*_slug` | derivado de `Tribunal.sigla` | helper |
| `secao_diario` | `Movimentacao.nome_orgao` | aproximação |
| `processo_id` | `Process.id` | direto |
| Busca ES `POST /doc/buscar` | ES query DSL nativa | passthrough pro cluster |
| Envelope `hits/_shards` | envelope ES real | nativo do cluster |
| `from/size` | `from/size` ES | nativo |
| Auth Bearer | Bearer + Api-Key | permission custom |
| Monitoramento webhook | app `monitoring/` | models + scheduler + webhook delivery |

---

## Fases, gates e agentes especialistas

Cada fase tem: **escopo**, **agente especialista** (subagent Task),
**testes de validação** e **gates de saída**. As fases A-D são
pré-requisito da E (API). F pode rodar em paralelo a partir de C. G é
paralela a partir de B.

### Gate global 0 — Decisões (já tomadas)

- [x] Granularidade: item DJEN (não página)
- [x] Compat: completa exceto comarca
- [x] ES: cluster dedicado
- [x] PDF: MinIO (S3-compat local)
- [x] Namespace: rotas novas, sem quebrar atuais
- [x] Auth: Bearer + Api-Key
- [x] MCP: server exposto em `/mcp` (SSE), tools delegam pra API REST
- [x] MCP auth: campo `ApiClient.mcp_token` (UUID), rate limit por cliente
- [x] MCP classificação: gated por setting `MCP_ENABLE_CLASSIFICACAO`

### Fase A — Infra: Elasticsearch + MinIO

**Duração**: 1-2 dias · **Bloqueia**: C, D, E, F

**Escopo**:
1. `docker-compose.yml`: add `elasticsearch` (8.14, single-node,
   `discovery.type=single-node`, `xpack.security.enabled=false` em dev,
   `ES_JAVA_OPTS=-Xms2g -Xmx2g`, healthcheck `curl localhost:9200/_cluster/health`)
   e `minio` (`minio/minio server /data --console-address :9001`,
   `MINIO_ROOT_USER=voyager`, `MINIO_ROOT_PASSWORD=...`, healthcheck
   `curl localhost:9000/minio/health/ready`).
2. `docker-compose-prod.yml` + `docker-compose-workers.yml`: add mesmos
   serviços (em prod, ES com `ES_JAVA_OPTS=-Xms4g -Xmx4g`, 2+ nodes
   quando decidir — ver Gate 3).
3. `requirements.txt`: `elasticsearch>=8.14,<9`, `django-storages[s3]>=1.14`,
   `boto3>=1.34`, `mcp>=1.0`.
4. `core/settings.py`:
   - `ELASTICSEARCH_URL = env('ELASTICSEARCH_URL', default='http://elasticsearch:9200')`
   - `ELASTICSEARCH_INDEX_PREFIX = env('ELASTICSEARCH_INDEX_PREFIX', default='voyager')`
   - `ES_TIMEOUT = env.int('ES_TIMEOUT', default=30)`
   - `MINIO_ENDPOINT = env('MINIO_ENDPOINT', default='minio:9000')`
   - `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET_PDFS`
   - `AWS_S3_ENDPOINT_URL = MINIO_ENDPOINT` (django-storages)
   - `STORAGES['pdfs'] = {'BACKEND': 'storages.backends.s3.S3Storage', ...}`
5. `INSTALLED_APPS`: add `'search'`, `'pdf_storage'`, `'monitoring'`, `'mcp_server'`.
6. `RQ_QUEUES`: add `'es_index'` (fila dedicada pra indexação, não competir
   com `default`), `'pdf_download'`.
7. `.env.example`: documentar vars novas.

**Agentes especialistas** (Task, paralelo):
- **infra-compose**: edita `docker-compose*.yml` + `.env.example`
- **infra-settings**: edita `core/settings.py` + `requirements.txt`

**Testes de validação (Gate A)**:
- [ ] `docker compose up -d elasticsearch minio` sobe healthy
- [ ] `curl http://localhost:9200/_cluster/health` → `"status":"green"`
- [ ] `curl http://localhost:9000/minio/health/ready` → 200
- [ ] `python -c "from elasticsearch import Elasticsearch; es=Elasticsearch(settings.ELASTICSEARCH_URL); print(es.info())"` → versão 8.14
- [ ] `python manage.py check` → 0 errors
- [ ] `pytest tests/ -x` → tudo passa (sem regressão)

**Gate A · Saída**: ES e MinIO acessíveis; settings carregam; app boot OK.

### Fase B — Models: FonteDiario + migration assunto_norm

**Duração**: 0.5 dia · **Depende**: — · **Bloqueia**: C, E, G

**Escopo**:
1. `tribunals/models.py`: add `FonteDiario`:
   ```python
   class FonteDiario(models.Model):
       source_id = models.IntegerField(primary_key=True)
       tribunal = models.OneToOneField(Tribunal, on_delete=models.CASCADE, related_name='fonte_diario')
       diario_slug = models.CharField(max_length=64)   # 'dje-trf1', 'comunica', ...
       orgao_slug = models.CharField(max_length=64)     # 'trf1', 'tjsp', ...
       caderno_slug = models.CharField(max_length=64, blank=True)
       nome = models.CharField(max_length=200)          # 'TRF - 1ª Reg.'
       class Meta:
           ordering = ['source_id']
   ```
2. `tribunals/models.py`: add campo em `Movimentacao`:
   ```python
   assunto_norm = models.JSONField(default=list, blank=True)
   ```
   Indexável? Não inicialmente (jsonb pequeno, lookup por par não é caso de uso).
3. Migration `0043_fonte_diario_e_assunto_norm.py`: cria tabela + add campo.
4. Seed `FonteDiario` dos tribunais ativos (data migration). Mapeamento:
   - TRF1=1, TRF3=59, TRF4=60, TRF5=61, TRF2=74, TRF6=(a descobrir), TJSP=18,
     TJMG=154, TJAL=(a descobrir), TJMA=(a descobrir), TRTs=84+, STF=25, STJ=81.
   - Pra TRTs: 1 FonteDiario por TRT ativo (ex: TRT6=87).
   - **Exceção comarca**: TJMG no Jusbrasil tem 1 source_id por comarca
     (centenas). Decisão: 1 source_id representativo (154 = "MG - TJ - Editais")
     pra TJMG inteiro. Documentado.
5. Admin: `FonteDiarioAdmin` (list display, search por tribunal/nome).

**Agente especialista**:
- **models-fonte-diario**: edita `tribunals/models.py`, cria migration, seed, admin.

**Testes de validação (Gate B)**:
- [ ] `python manage.py migrate tribunals` → 0 erros
- [ ] `FonteDiario.objects.count()` == número de tribunais ativos
- [ ] `Movimentacao._meta.get_field('assunto_norm')` existe, default=list
- [ ] `pytest tests/test_fonte_diario.py` (novo) → passa:
  - test_seed_cobertura_tribunais_ativos
  - test_source_id_unico_por_tribunal
  - test_assunto_norm_default_lista_vazia
- [ ] `pytest tests/ -x` → sem regressão

**Gate B · Saída**: FonteDiario populada; assunto_norm existe.

### Fase C — Indexação Elasticsearch (write-through)

**Duração**: 1.5-2 dias · **Depende**: A, B · **Bloqueia**: E, F

**Escopo**:
1. `search/apps.py`: app config + `ready()` conecta signals.
2. `search/client.py`: singleton `get_es()` → `Elasticsearch(settings.ELASTICSEARCH_URL)`.
3. `search/mappings.py`: define mapping do index `voyager-movimentacoes`:
   ```python
   MOV_MAPPING = {
       "settings": {
           "analysis": {
               "analyzer": {
                   "portuguese_asciifolding": {
                       "tokenizer": "standard",
                       "filter": ["lowercase", "asciifolding"],
                   }
               }
           }
       },
       "mappings": {
           "properties": {
               "id":              {"type": "long"},
               "tribunal":        {"type": "keyword"},
               "source":          {"type": "integer"},
               "publish_date":    {"type": "date"},
               "available_at":    {"type": "date"},
               "detected_at":     {"type": "date"},
               "body":            {"type": "text", "analyzer": "portuguese_asciifolding"},
               "docurl":          {"type": "keyword", "index": False},
               "cached_docurl":   {"type": "keyword", "index": False},
               "proc":            {"type": "keyword"},
               "proc_alt":        {"type": "keyword"},
               "proc_apens":      {"type": "keyword"},
               "advs":            {"type": "text", "analyzer": "portuguese_asciifolding"},
               "partes":          {"type": "text", "analyzer": "portuguese_asciifolding"},
               "assunto":         {"type": "text", "analyzer": "portuguese_asciifolding"},
               "assunto_norm":    {"type": "nested", "properties": {
                                       "tipo": {"type": "integer"},
                                       "subtipo": {"type": "integer"}}},
               "processo_id":    {"type": "long"},
               "classe_nome":    {"type": "keyword"},
               "secao_diario":    {"type": "keyword"},
               "periodico_diario_slug": {"type": "keyword"},
               "periodico_orgao_slug":  {"type": "keyword"},
               "periodico_caderno_slug": {"type": "keyword"},
               "ativo":           {"type": "boolean"},
               "recorte_id":      {"type": "long"},
           }
       }
   }
   ```
   Index `voyager-processos` (mapping menor: id, tribunal, source, proc,
   classe_nome, assunto, partes, advs, processo_id).
4. `search/documents.py`: `movimentacao_to_doc(mov) -> dict` monta o doc
   no formato ES (busca `FonteDiario` p/ source, serializa advs/partes).
5. `search/index.py`: `ensure_index(name, mapping)` — create if not exists.
6. `search/signals.py`: `post_save` em `Movimentacao` →
   `indexar_movimentacao.delay(mov.pk)` (fila `es_index`). `post_delete` →
   `es_delete.delay(mov.id)`. Mesmo pra `Process` (index `voyager-processos`).
   - Guard: só dispara se `mov._state.adding` é False OU `texto` mudou
     (`tracked_fields`) — evita reindexar em every save sem mudança.
7. `search/jobs.py`: `indexar_movimentacao(pk)`, `desindexar_movimentacao(id)`,
   `indexar_processo(pk)`, `desindexar_processo(id)`. Idempotentes.
8. `search/management/commands/reindexar_movimentacoes.py`: bulk reindex
   (scroll `Movimentacao` com `select_related('processo','tribunal')`,
   bulk `es.helpers.bulk`). Flags `--tribunal=`, `--desde=`, `--limit=`,
   `--batch-size=1000`. Barra de progresso.
9. `search/management/commands/reindexar_processos.py`: análogo.

**Agentes especialistas** (paralelo):
- **es-mappings**: `search/mappings.py` + `search/index.py` + `ensure_index`
- **es-signals**: `search/signals.py` + `search/jobs.py`
- **es-documents**: `search/documents.py` (serialização ORM→ES doc)
- **es-backfill**: `search/management/commands/reindexar_*.py`

**Testes de validação (Gate C)**:
- [ ] `python manage.py es_ensure_indexes` → cria 2 indexes
- [ ] `curl http://localhost:9200/voyager-movimentacoes/_mapping` → schema OK
- [ ] Criar `Movimentacao` via ORM → `es_index` job indexa
- [ ] `curl http://localhost:9200/voyager-movimentacoes/_count` → count sobe
- [ ] `curl http://localhost:9200/voyager-movimentacoes/_search?q=body:precatório` → hit
- [ ] Deletar `Movimentacao` → doc some do ES
- [ ] `python manage.py reindexar_movimentacoes --tribunal=TRF1 --limit=100` → 100 docs
- [ ] `pytest tests/test_es_indexacao.py` (novo):
  - test_signal_indexa_on_create
  - test_signal_desindexa_on_delete
  - test_signal_nao_reindexa_se_texto_nao_mudou
  - test_document_tem_source_id_da_fonte_diario
  - test_document_serializa_advs_e_partes
  - test_reindexar_command_batch
- [ ] `pytest tests/ -x` → sem regressão

**Gate C · Saída**: write-through funcionando; backfill command pronto.

### Fase D — PDF Storage (MinIO)

**Duração**: 1.5 dias · **Depende**: A · **Paralelo a**: C

**Escopo**:
1. `pdf_storage/apps.py`.
2. `pdf_storage/models.py`:
   ```python
   class PdfArquivo(models.Model):
       movimentacao = models.OneToOneField(Movimentacao, on_delete=models.CASCADE, related_name='pdf')
       arquivo = models.FileField(storage='pdfs', upload_to='movimentacoes/%Y/%m/')
       tamanho_bytes = models.PositiveBigIntegerField(default=0)
       hash_sha256 = models.CharField(max_length=64, blank=True)
       baixado_em = models.DateTimeField(auto_now_add=True)
       STATUS_PENDENTE='pendente'; STATUS_OK='ok'; STATUS_ERRO='erro'
       status = models.CharField(max_length=10, choices=..., default=STATUS_PENDENTE)
       erro = models.TextField(blank=True)
       tentativas = models.PositiveSmallIntegerField(default=0)
   ```
3. `pdf_storage/storage.py`: helper `get_pdf_storage()` →
   `storages.backends.s3.S3Storage` configurado pro MinIO bucket.
   `ensure_bucket()` cria bucket se não existe (via boto3).
4. `pdf_storage/signals.py`: `post_save` em `Movimentacao` → se `link`
   presente e `PdfArquivo` não existe → `baixar_pdf_movimentacao.delay(pk)`
   (fila `pdf_download`).
5. `pdf_storage/jobs.py`: `baixar_pdf(mov_pk)`:
   - `requests.get(mov.link, timeout=30, stream=True)` — retentativas 3x
   - salva no MinIO (path `movimentacoes/{YYYY}/{MM}/{mov.id}.pdf`)
   - calcula sha256, tamanho
   - cria `PdfArquivo(status=ok)` (idempotente: se já existe, skip)
   - em erro: `PdfArquivo(status=erro, erro=..., tentativas++)`
6. `pdf_storage/management/commands/baixar_pdfs_pendentes.py`: backfill
   retroativo dos `Movimentacao` com `link` mas sem `PdfArquivo`.
   Flags `--tribunal=`, `--desde=`, `--limit=`.
7. `pdf_storage/urls.py`: view `serve_pdf(request, mov_id)` → redirect pro
   MinIO presigned URL (ou proxy se bucket privado). Usado pra `cached_docurl`.
8. Helper `pdf_storage/urls.py::cached_docurl_for(mov) -> str|None`:
   retorna URL Voyager pra PDF se `PdfArquivo` existe.

**Agentes especialistas** (paralelo):
- **pdf-models**: `pdf_storage/models.py` + `storage.py`
- **pdf-jobs**: `pdf_storage/signals.py` + `jobs.py`
- **pdf-backfill**: `pdf_storage/management/commands/baixar_pdfs_pendentes.py`
- **pdf-serve**: `pdf_storage/urls.py` + `cached_docurl_for`

**Testes de validação (Gate D)**:
- [ ] `python manage.py ensure_minio_bucket` → bucket criado no MinIO
- [ ] Criar `Movimentacao(link='http://...')` → job `pdf_download` dispara
- [ ] `PdfArquivo.objects.filter(movimentacao=mov).exists()` → True após job
- [ ] `cached_docurl_for(mov)` → URL não-None
- [ ] Re-salvar `Movimentacao` (mesmo link) → NÃO re-baixa (idempotente)
- [ ] `Movimentacao` sem `link` → não dispara download
- [ ] `python manage.py baixar_pdfs_pendentes --tribunal=TRF1 --limit=10` → 10 PDFs
- [ ] `pytest tests/test_pdf_storage.py` (novo):
  - test_signal_dispara_download_se_link
  - test_signal_nao_dispara_se_sem_link
  - test_idempotente_nao_rebaixa
  - test_status_erro_em_falha
  - test_cached_docurl_retorna_url
- [ ] `pytest tests/ -x` → sem regressão

**Gate D · Saída**: PDFs baixando e servindo cached_docurl.

### Fase E — API Jusbrasil-compat (Busca + Get + Fontes + Tipos)

**Duração**: 2-3 dias · **Depende**: A, B, C, D

**Escopo**:
1. `api/permissions.py`: `HasAPIKeyOrBearer` — aceita:
   - `Authorization: Bearer <token>` (lookup `ApiClient.api_key`)
   - `Authorization: Api-Key <key>` (atual, via `rest_framework_api_key`)
   - `X-API-Key: <key>` (legacy leads)
2. `api/diarios_views.py`:
   - `DiarioBuscarView` (POST `/api/v1/diarios-oficiais/doc/buscar`):
     - Recebe body JSON (query ES 7.14).
     - Valida schema mínimo: `from`, `size`, `query` obrigatórios.
       `from >= 0`, `size` 1-10000 (default 10). Rejeita >10000 com 400.
     - Repassa query pro `es.search(index='voyager-movimentacoes', body=...)`.
     - Retorna resposta ES **nativa** (envelope `took/timed_out/_shards/hits`).
     - Compat `_type`: ES 8 não retorna `_type` — injeta `"_type":"_doc"`
       em cada hit pra compat com Jusbrasil.
     - Fallback: se ES fora → 503 com body `{"error":"elasticsearch_unavailable"}`
       (não degrada pra Postgres — manter compat de envelope).
   - `DiarioGetView` (GET `/api/v1/diarios-oficiais/doc/get/<int:id>`):
     - `es.get(index='voyager-movimentacoes', id=id)`.
     - Retorna `{_index, _type:"_doc", _id, _version, found:true, _source:{...}}`.
     - 404 ES → `found:false` (compat Jusbrasil, **não** 404 HTTP).
   - `FontesRecortesView` (GET `/api/v1/diarios-oficiais/fontes_recortes`):
     - Retorna `{str(source_id): "nome", ...}` de `FonteDiario.objects.all()`.
   - `TiposNormView` (GET `/api/v1/monitoramento/proc/tipos_norm_andamentos_movs`):
     - Retorna lista `[[tipo, subtipo, "nome"], ...]` da fixture estática
       em `tribunals/tipos_norm.py`.
   - `StatusCoberturaView` (GET `/api/base-judicial/tribproc/status_cobertura?area=`):
     - Retorna matriz de cobertura por área (`federal`/`estadual`/`trabalhista`/`superior`).
     - Derivado de `Tribunal` ativos + `FonteDiario`.
3. `api/urls.py`: add rotas.
4. `api/serializers.py`: add `DiarioDocSerializer` (helper que monta
   `_source` no formato Jusbrasil a partir do ORM, pra uso no fallback
   ou no `GET /doc/get` se ES não tiver o doc).
5. Atualizar `api/filters.py`: `MovimentacaoFilter` continua servindo a
   rota `/api/v1/movimentacoes/` (não mexer — compat com atuais).

**Agentes especialistas** (paralelo depois do scaffolding):
- **api-permissions**: `api/permissions.py`
- **api-buscar**: `DiarioBuscarView` (passthrough ES)
- **api-get-fontes-tipos**: `DiarioGetView`, `FontesRecortesView`, `TiposNormView`, `StatusCoberturaView`
- **api-urls-serializers**: `api/urls.py` + `DiarioDocSerializer`

**Testes de validação (Gate E)**:
- [ ] `curl -X POST -H "Authorization: Bearer <key>" .../doc/buscar -d '{"from":0,"size":2,"query":{"query_string":{"query":"precatório"}}}'` → 200, envelope ES
- [ ] Response tem `hits.hits[]._source.body`, `publish_date`, `source`, `docurl`
- [ ] `curl .../doc/get/<id>` → `found:true`, `_source` completo
- [ ] `curl .../doc/get/999999999` → `found:false` (não 404)
- [ ] `curl .../fontes_recortes` → `{"1":"TRF - 1ª Reg.", ...}`
- [ ] `curl .../tipos_norm_andamentos_movs` → `[[23,1,"Disponibilizada a Intimação"], ...]`
- [ ] `curl .../status_cobertura?area=federal` → matriz com TRFs
- [ ] Auth Bearer funciona; Api-Key funciona; X-API-Key funciona
- [ ] Auth inválida → 403
- [ ] `from=10001` → 400
- [ ] ES fora → 503
- [ ] `pytest tests/test_api_diarios.py` (novo):
  - test_buscar_retorna_envelope_es
  - test_buscar_filtro_por_source
  - test_buscar_filtro_por_data_range
  - test_buscar_paginacao_from_size
  - test_buscar_query_string_com_aspas
  - test_buscar_size_max_10000
  - test_buscar_es_fora_retorna_503
  - test_get_found_true
  - test_get_found_false_nao_404
  - test_fontes_recortes_formato_dict
  - test_tipos_norm_lista_tuplas
  - test_auth_bearer_aceito
  - test_auth_api_key_aceito
  - test_auth_x_api_key_aceito
  - test_auth_invalida_403
  - test_status_cobertura_areas
- [ ] `pytest tests/ -x` → sem regressão

**Gate E · Saída**: API de busca 100% compatível com Jusbrasil.

### Fase F — Monitoramento (webhook push)

**Duração**: 3-4 dias · **Depende**: C, E · **Paralelo a**: G

**Escopo**:
1. `monitoring/apps.py`.
2. `monitoring/models.py`:
   ```python
   class MonitoredTerm(models.Model):
       term = models.CharField(max_length=1024)
       source_ids = models.JSONField(default=list)  # [1, 59, ...]
       is_active = models.BooleanField(default=True)
       is_reviewed = models.BooleanField(default=True)
       created_at = models.DateTimeField(auto_now_add=True)
       user_creator = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
       # validação: min 3 chars, sem vírgula/exclamação/igual

   class MonitoredPerson(models.Model):
       nome = models.CharField(max_length=255, blank=True)
       documento = models.CharField(max_length=20, blank=True)  # CPF/CNPJ
       oab = models.CharField(max_length=20, blank=True)
       tribunais = models.JSONField(default=list)  # [sigla, ...]
       is_monitored_diario = models.BooleanField(default=True)
       is_monitored_tribunal = models.BooleanField(default=False)
       is_advogado = models.BooleanField(default=False)
       is_active = models.BooleanField(default=True)
       created_at = models.DateTimeField(auto_now_add=True)

   class MonitoredProcess(models.Model):
       cnj = models.CharField(max_length=25)
       tribunais = models.JSONField(default=list)
       is_active = models.BooleanField(default=True)
       created_at = models.DateTimeField(auto_now_add=True)

   class Detection(models.Model):
       target_type = models.CharField(max_length=20, choices=[...])  # term|person|proc
       target_id = models.BigIntegerField()  # FK lógico pra MonitoredTerm/Person/Process
       movimentacao = models.ForeignKey(Movimentacao, on_delete=models.CASCADE)
       snippet = models.TextField()
       detected_at = models.DateTimeField(auto_now_add=True)
       entregue_em = models.DateTimeField(null=True, blank=True)
       class Meta:
           constraints = [UniqueConstraint(fields=['target_type','target_id','movimentacao'], name='uniq_detection')]
           indexes = [models.Index(fields=['target_type','target_id','-detected_at'])]

   class WebhookConfig(models.Model):
       cliente = models.ForeignKey(ApiClient, on_delete=models.CASCADE, related_name='webhooks')
       url = models.URLField(max_length=2500)
       secret = models.CharField(max_length=64)  # HMAC signing
       evento_types = models.JSONField(default=list)  # ['term','person','proc']
       is_active = models.BooleanField(default=True)
       created_at = models.DateTimeField(auto_now_add=True)
   ```
3. `monitoring/jobs.py`:
   - `varredura_diaria()`: pra cada target ativo, query ES sobre movs do
     último dia (`publish_date >= now-1d`), cria `Detection` (idempotente
     via constraint), enfileira `entregar_detection.delay(det.pk)`.
   - `entregar_detection(det_pk)`: monta payload recorte Jusbrasil
     (schema completo), POST HMAC-signed pro `WebhookConfig.url`, retry 5x
     com backoff exponencial (RQ). Marca `entregue_em` em 200.
4. `monitoring/payload.py`: `build_recorte_payload(mov, target) -> dict`
   — monta o schema de recorte Jusbrasil (todos os campos da doc).
5. `monitoring/views.py` + `api/monitoring_views.py`: CRUD endpoints:
   - `POST/GET/PATCH/DELETE /api/monitoramento/monitored_term`
   - `POST/GET/PATCH/DELETE /api/monitoramento/monitored_person`
   - `POST/GET/PATCH/DELETE /api/monitoramento/proc`
   - `GET /api/monitoramento/monitored_term/<id>/detections` (histórico)
6. `monitoring/scheduler.py`: cron diário 05:00 `varredura_diaria.delay()`
   registrado em `djen/scheduler.py::register_all`.
7. `monitoring/admin.py`: registro dos models pra inspeção.

**Agentes especialistas** (paralelo):
- **mon-models**: `monitoring/models.py` + migration + admin
- **mon-jobs**: `monitoring/jobs.py` + `payload.py` (varredura + entrega)
- **mon-views**: `api/monitoring_views.py` + rotas CRUD
- **mon-scheduler**: `monitoring/scheduler.py` + integração com `register_all`

**Testes de validação (Gate F)**:
- [ ] `POST /monitored_term` cria termo; `GET` lista
- [ ] Termo < 3 chars → 400
- [ ] Termo com vírgula → 400
- [ ] `varredura_diaria()` com mov que casa termo → cria `Detection`
- [ ] Re-rodar `varredura_diaria()` não duplica Detection (idempotente)
- [ ] `entregar_detection` POSTa pro webhook URL (mock server no teste)
- [ ] HMAC signature presente no header `X-Voyager-Signature`
- [ ] Webhook retorna 200 → `Detection.entregue_em` setado
- [ ] Webhook retorna 500 → retry 5x, depois marca erro
- [ ] `POST /proc` com CNJ inválido → 400
- [ ] `pytest tests/test_monitoring.py` (novo):
  - test_create_monitored_term_valido
  - test_create_monitored_term_invalido_curto
  - test_create_monitored_term_invalido_caracteres
  - test_varredura_cria_detection_por_termo
  - test_varredura_cria_detection_por_person
  - test_varredura_cria_detection_por_proc
  - test_varredura_idempotente
  - test_entregar_detection_hmac
  - test_entregar_detection_retry_5x
  - test_entregar_detection_marca_entregue_em_200
  - test_webhook_config_is_active_false_nao_entrega
  - test_crud_monitored_term
  - test_crud_monitored_person
  - test_crud_monitored_process
- [ ] `pytest tests/ -x` → sem regressão

**Gate F · Saída**: Monitoramento push funcional end-to-end.

### Fase G — Classificador de tipos normalizados

**Duração**: 1 dia · **Depende**: B · **Paralelo a**: E, F

**Escopo**:
1. `tribunals/tipos_norm.py`: fixture `TIPOS_NORM` (lista de tuplas
   `(tipo, subtipo, "nome")` da doc Jusbrasil) + `classificar_tipo_norm(mov) -> list[(int,int)]`.
   Heurística:
   - `tipo_comunicacao` + `texto` → regex/keywords.
   - Ex: "intimação" → (23,1); "citação" + "positiva" → (30,1); "sentença" +
     "procedente" → (17,1); "acórdão" → (25,1); "transitado em julgado" →
     (18,1); "audiência" + "designada" → (2,1); "despacho" → (21,0);
     "extinção" → (6,5); "homologação" + "acordo" → (7,3).
   - Retorna `[]` se incerto (compat spec: "Quando não disponível, lista vazia").
2. `search/jobs.py::indexar_movimentacao`: ao indexar, populate
   `assunto_norm` se vazio (lazy enrichment no ES doc).
3. Management command `popular_assunto_norm` (one-shot backfill dos
   históricos): `Movimentacao.objects.filter(assunto_norm=[])` →
   batch update.
4. Endpoint `GET /tipos_norm_andamentos_movs` (já em Fase E) serve a
   fixture + um flag indicando quais o Voyager classifica automaticamente.

**Agente especialista**:
- **classificador-tipos**: `tribunals/tipos_norm.py` + command + integração ES

**Testes de validação (Gate G)**:
- [ ] `classificar_tipo_norm(mov_intimacao)` → `[(23,1)]`
- [ ] `classificar_tipo_norm(mov_sentenca_procedente)` → `[(17,1)]`
- [ ] `classificar_tipo_norm(mov_despacho_generico)` → `[]`
- [ ] `GET /tipos_norm_andamentos_movs` → lista completa (42 tuplas)
- [ ] `popular_assunto_norm` command roda sem erro em batch
- [ ] `pytest tests/test_tipos_norm.py` (novo):
  - test_classificar_intimacao
  - test_classificar_citacao_positiva
  - test_classificar_sentenca_procedente
  - test_classificar_acordao
  - test_classificar_transitado_julgado
  - test_classificar_audiencia_designada
  - test_classificar_despacho_generico_retorna_vazio
  - test_fixture_42_tuplas
- [ ] `pytest tests/ -x` → sem regressão

**Gate G · Saída**: `assunto_norm` populado e servido.

### Fase H — Refinamentos, docs e deploy

**Duração**: 2-3 dias · **Depende**: A-G

**Escopo**:
1. **OpenAPI**: `SPECTACULAR_SETTINGS` add tag "Diários Oficiais (Jusbrasil-compat)".
   Decorar views com `@extend_schema`.
2. **Docs**:
   - Atualizar `.ia/API.md` com rotas Jusbrasil-compat + de/para.
   - Atualizar `.ia/ARCHITECTURE.md` (apps novos: `search`, `pdf_storage`, `monitoring`).
   - Atualizar `.ia/OPS.md` (containers novos, healthchecks, runbooks ES/MinIO).
   - Atualizar `.ia/DATA_MODEL.md` (FonteDiario, PdfArquivo, models de monitoring, assunto_norm).
   - Atualizar `.ia/DECISIONS.md` (ADR: "Por que ES + MinIO").
   - Atualizar `.ia/ROADMAP.md`.
3. **Deploy**:
   - `docker-compose-prod.yml`: add ES (4GB heap), MinIO.
   - `docker-compose-workers.yml`: add workers `worker_es_index` (8 réplicas),
     `worker_pdf_download` (4 réplicas).
   - Runbook: reindex backfill (horas/dias), PDF backfill (dias), tamanho
     do cluster ES (monitorar `_cluster/health`).
4. **Limitações documentadas** (no OpenAPI description + `.ia/API.md`):
   - `prev_page`/`next_page`/`num_pag_original`: sempre `null`.
   - `proc_alt`/`proc_apens`: sempre `null`.
   - Busca por comarca: não suportada (granularidade = tribunal).
   - `assunto_norm`: heurístico (cobertura parcial; `[]` quando incerto).
   - `cached_docurl`: disponível só se PDF baixado (job async pós-ingestão).
5. **Performance**:
   - Monitorar lag de indexação ES (fila `es_index` depth vs ingestão DJEN).
   - Se ES não acompanhar: escalar `worker_es_index`.
   - PDF download: se backlog grande, escalar `worker_pdf_download`.
6. **Smoke test end-to-end** (script `scripts/smoke_jusbrasil_compat.sh`):
   - Auth Bearer → 200
   - Busca "precatório" → hits com body
   - GET doc → found
   - Fontes recortes → dict
   - Tipos norm → lista
   - POST monitored_term → criado
   - Webhook delivery (mock) → Detection entregue

**Agente especialista**:
- **docs-deploy**: atualiza `.ia/*`, `docker-compose-*`, smoke script

**Testes de validação (Gate H)**:
- [ ] `pytest tests/ -x` → tudo passa (suite completa + novos testes)
- [ ] `python manage.py check` → 0 errors
- [ ] `ruff check` → 0 erros
- [ ] `bash scripts/smoke_jusbrasil_compat.sh` (em ambiente dev completo) → 0/6 pass
- [ ] OpenAPI `/api/v1/schema/` tem tag "Diários Oficiais"
- [ ] `.ia/API.md` atualizado
- [ ] `.ia/ARCHITECTURE.md` lista apps novos
- [ ] `docker compose -f docker-compose-prod.yml config` → válido

**Gate H · Saída**: pronto pra deploy.

---

### Fase I — MCP Server (clientes conversarem sobre processos)

**Duração**: 2-3 dias · **Depende**: E, G · **Paralelo a**: F

> Um **MCP (Model Context Protocol)** server expõe os dados do Voyager
> como *tools* pra LLMs/agentes de clientes. O cliente aponta o MCP pro
> Voyager e o agente dele "fala dos processos" — busca, detalhe, partes,
> movimentações, monitoramento — sem precisar implementar HTTP. O MCP
> encapsula auth, paginação e schema. Pensado pra clientes que usam
> Cursor/Claude/ChatGPT/etc. e querem que o agente acesse dados jurídicos
> com linguagem natural.

**Escopo**:
1. **App nova `mcp_server/`**: server MCP standalone (FastMCP/`mcp` SDK)
   que roda como endpoint HTTP/SSE dentro do container `web` (rota
   `/mcp` via ASGI sub-app) ou container dedicado `mcp` (decidir no Gate I).
   - Em `core/urls.py`: mount `/mcp/` → `mcp_server.app:app` (ASGI).
   - Em `docker-compose*.yml`: opção de container `mcp` dedicado (porta 9000).
2. **Auth MCP**: cada `ApiClient` pode gerar um **MCP token** (campo novo
   `ApiClient.mcp_token`, UUID). O MCP server valida o token no header
   `Authorization: Bearer <mcp_token>` ou query param `?token=` (SSE). Sem
   token → 403. Reusa `tribunals.ApiClient`.
3. **Tools expostas** (cada uma = uma function MCP com docstring → schema
   auto-gerado):

   | Tool | Descrição | Inputs | Output |
   |---|---|---|---|
   | `buscar_diarios` | Busca textual em diários (Jusbrasil-compat) | `query: str`, `tribunal: str?`, `data_inicio: str?`, `data_fim: str?`, `size: int=10` | lista de `{id, tribunal, publish_date, body_snippet, docurl, proc}` |
   | `get_documento` | Detalhe de uma publicação | `doc_id: int` | `{id, body, publish_date, source, docurl, cached_docurl, proc, advs, partes, assunto, assunto_norm}` |
   | `get_processo` | Dados completos de um processo | `cnj: str` | `{numero_cnj, tribunal, classe, assunto, orgao_julgador, valor_causa, partes, total_movimentacoes, ultima_movimentacao_em, classificacao, score}` |
   | `list_movimentacoes` | Movimentações de um processo | `cnj: str`, `limit: int=50`, `offset: int=0` | lista de `{id, data_disponibilizacao, tipo_comunicacao, nome_orgao, snippet}` |
   | `get_partes` | Partes de um processo | `cnj: str` | lista de `{nome, documento, tipo, polo, papel, oab}` |
   | `listar_fontes` | Diários/tribunais cobertos | — | `{source_id: nome, ...}` |
   | `status_cobertura` | Cobertura por área | `area: str` (federal/estadual/trabalhista/superior) | matriz de cobertura |
   | `monitorar_termo` | Cria monitoramento de termo (push) | `term: str`, `tribunais: list[str]?` | `{id, status}` |
   | `monitorar_processo` | Cria monitoramento de processo | `cnj: str` | `{id, status}` |
   | `listar_detections` | Detecções recentes do cliente | `desde: str?`, `limit: int=50` | lista de `{recorte_id, proc, snippet, detected_at, entregue}` |
   | `get_pdf` | URL do PDF armazenado | `doc_id: int` | `{cached_docurl, tamanho_bytes, baixado_em}` ou `{available: false}` |
   | `classificacao_lead` | Score/classificação de um processo (se autorizado) | `cnj: str` | `{classificacao, score, versao}` ou 403 se não autorizado |

4. **Resources expostas** (dados contextuais que o agente pode referenciar):
   - `voyager://processo/{cnj}` → detalhe do processo (JSON)
   - `voyager://movimentacao/{id}` → detalhe da publicação (JSON)
   - `voyager://parte/{id}` → detalhe da parte (JSON)
   - `voyager://fontes` → lista de fontes (JSON)
5. **Implementação**: usar `mcp` Python SDK (`pip install mcp`):
   ```python
   # mcp_server/server.py
   from mcp import Server
   from mcp.types import Tool, TextContent

   server = Server("voyager")

   @server.list_tools()
   async def list_tools() -> list[Tool]:
       return [
           Tool(name="buscar_diarios", description="Busca textual em diários oficiais",
                inputSchema={"type":"object","properties":{
                    "query":{"type":"string"},
                    "tribunal":{"type":"string"},
                    "size":{"type":"integer","default":10}},
                    "required":["query"]}),
           # ... demais tools
       ]

   @server.call_tool()
   async def call_tool(name: str, arguments: dict) -> list[TextContent]:
       # valida MCP token → ApiClient
       # chama a lógica de cada tool (reusa api/diarios_views.py + serializers)
       ...
   ```
6. **Transport**: SSE (Server-Sent Events) em `/mcp/sse` + POST `/mcp/messages`
   (padrão MCP HTTP). Compatível com Claude Desktop, Cursor, etc.
7. **Reusabilidade**: as tools chamam as **mesmas funções** da API REST
   (`api/diarios_views.py`, `api/serializers.py`) — não duplicam lógica.
   Helper `mcp_server/delegates.py` wraps as views internamente.
8. **Rate limit MCP**: reusar `ApiClient` + novo campo `mcp_rate_limit_rpm`
   (default 60). Token-bucket Redis (análogo ao `datajud/ratelimit.py`).
9. **Doc MCP**: `mcp_server/README.md` com instruções de configuração pra
   Claude Desktop (`mcpServers: {"voyager": {"url": "https://voyager.was.dev.br/mcp/sse", "token": "..."}}`),
   Cursor, e clientes custom. Endpoint `GET /mcp/.well-known/mcp.json`
   serve o descriptor (tools, versão, auth).
10. **Logs/observabilidade**: cada call_tool loga `(cliente, tool, args, latency)`
    em structlog. Métricas Prometheus `mcp_tool_calls_total{tool,cliente}`.

**Agentes especialistas** (paralelo):
- **mcp-scaffold**: `mcp_server/` app, `server.py`, mount ASGI, requirements
- **mcp-tools-busca**: tools `buscar_diarios`, `get_documento`, `listar_fontes`, `status_cobertura`
- **mcp-tools-processo**: tools `get_processo`, `list_movimentacoes`, `get_partes`, `classificacao_lead`
- **mcp-tools-monitor**: tools `monitorar_termo`, `monitorar_processo`, `listar_detections`
- **mcp-auth-ratelimit**: `ApiClient.mcp_token`, validação, token-bucket Redis
- **mcp-docs**: `README.md`, descriptor endpoint, exemplos Claude/Cursor

**Testes de validação (Gate I)**:
- [ ] `GET /mcp/.well-known/mcp.json` → descriptor com 12 tools listadas
- [ ] `POST /mcp/messages` com `initialize` → resposta de capabilities
- [ ] `tools/list` → 12 tools com `inputSchema` válido (JSON Schema)
- [ ] `tools/call buscar_diarios {"query":"precatório","size":2}` → 2 resultados
- [ ] `tools/call buscar_diarios` sem `query` → erro de schema
- [ ] `tools/call get_documento {"doc_id":<id_existente>}` → documento completo
- [ ] `tools/call get_documento {"doc_id":999999}` → `{available:false}` (não erro)
- [ ] `tools/call get_processo {"cnj":"<cnj_existente>"}` → processo + partes
- [ ] `tools/call get_processo {"cnj":"invalido"}` → erro de schema
- [ ] `tools/call list_movimentacoes {"cnj":"<cnj>","limit":5}` → 5 movs
- [ ] `tools/call get_partes {"cnj":"<cnj>"}` → lista de partes
- [ ] `tools/call listar_fontes {}` → dict source_id→nome
- [ ] `tools/call monitorar_termo {"term":"precatório"}` → cria MonitoredTerm
- [ ] `tools/call listar_detections {"limit":10}` → lista (vazia se sem monitoramento)
- [ ] `tools/call get_pdf {"doc_id":<id_com_pdf>}` → `{cached_docurl, tamanho_bytes}`
- [ ] `tools/call get_pdf {"doc_id":<id_sem_pdf>}` → `{available:false}`
- [ ] `tools/call classificacao_lead {"cnj":"<cnj>"}` → `{classificacao, score}` ou 403
- [ ] Auth: sem token → 403; token inválido → 403; token válido → 200
- [ ] Rate limit: 61 calls/min → 429
- [ ] Resources: `voyager://processo/{cnj}` retorna JSON do processo
- [ ] Logs: cada call registrada em structlog
- [ ] `pytest tests/test_mcp_server.py` (novo):
  - test_mcp_initialize_retorna_capabilities
  - test_list_tools_retorna_12_tools
  - test_tool_buscar_diarios_retorna_resultados
  - test_tool_buscar_diarios_sem_query_erro_schema
  - test_tool_get_documento_existente
  - test_tool_get_documento_inexistente_available_false
  - test_tool_get_processo_com_partes
  - test_tool_list_movimentacoes_paginacao
  - test_tool_get_partes
  - test_tool_listar_fontes
  - test_tool_status_cobertura
  - test_tool_monitorar_termo_cria
  - test_tool_monitorar_processo_cria
  - test_tool_listar_detections
  - test_tool_get_pdf_com_pdf
  - test_tool_get_pdf_sem_pdf
  - test_tool_classificacao_lead_autorizado
  - test_tool_classificacao_lead_nao_autorizado_403
  - test_auth_sem_token_403
  - test_auth_token_invalido_403
  - test_auth_token_valido_200
  - test_rate_limit_429_acima_teto
  - test_resource_processo_retorna_json
  - test_resource_movimentacao_retorna_json
  - test_descriptor_well_known
  - test_logs_cada_call_registrada
- [ ] `pytest tests/ -x` → sem regressão

**Gate I · Saída**: MCP server funcional; clientes podem configurar no
Claude Desktop / Cursor e conversar sobre processos via linguagem natural.

---

## Ordem de execução (dependências)

```
A (infra)
 ├── B (models) ────┐
 │                  ├── G (classificador) ──┐
 ├── C (ES index) ──┤                      │
 │                  ├── E (API busca) ──────┤── I (MCP) ──┐
 │                  │                      │             │
 └── D (PDF) ───────┘                      │             │
                          F (monitoramento) ┘             │
                                                         │
                                              H (docs/deploy)
```

- **A** bloqueia tudo (infra).
- **B** não depende de A (só código); pode rodar em paralelo.
- **C** depende de A + B.
- **D** depende de A (MinIO); paralelo a C.
- **E** depende de A + B + C + D.
- **F** depende de C + E (usa ES + auth).
- **G** depende de B (assunto_norm); paralelo a E/F.
- **I** depende de E + G (usa API + classificação); paralelo a F.
- **H** depende de tudo.

**Paralelização máxima**: A || B → (C || D || G) → E → (F || I) → H.

## Estimativa total

| Cenário | Duração |
|---|---|
| MVP (A+B+C+D+E, sem monitoramento/MCP) | 5-7 dias |
| + MCP (A+B+C+D+E+G+I) | 8-11 dias |
| Completo (A-I) | 13-18 dias |

## Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|---|---|
| Volume ES: ~600M movs históricos | Reindex backfill demora dias | Batch 1000, scroll, throttling; indexar só ativos primeiro |
| PDF: 600M links × ~50KB = 30GB+ | Storage | MinIO com volume adequado; download só ativos primeiro |
| ES query DSL incompat (sintaxe 7.14 vs 8.14) | Busca quebra | Testes com payloads reais da doc; ES 8 é compat na query DSL |
| `_type` deprecated em ES 8 | Response sem `_type` | Injetar `"_type":"_doc"` no serializer (Fase E) |
| ES fora | API de busca cai | 503 explícito (não degrada pra Postgres — quebra envelope) |
| Fila `es_index` inunda | Lag de indexação | Fila dedicada; escalar workers; monitorar depth |
| Webhook delivery falha | Detection perdida | Retry 5x + marca erro; reprocessar manualmente |
| `assunto_norm` heurístico baixa cobertura | Cliente confunde | Retornar `[]` quando incerto (compat spec) |
| MinIO bucket privado vs público | cached_docurl inacessível | Presigned URL com expiração 7d; documentar |

## Settings novos (resumo)

| Setting | Default | Função |
|---|---|---|
| `ELASTICSEARCH_URL` | `http://elasticsearch:9200` | Cluster ES |
| `ELASTICSEARCH_INDEX_PREFIX` | `voyager` | Prefixo dos indexes |
| `ES_TIMEOUT` | 30 | Timeout ES (segundos) |
| `MINIO_ENDPOINT` | `minio:9000` | Endpoint MinIO |
| `MINIO_ACCESS_KEY` | — | Access key |
| `MINIO_SECRET_KEY` | — | Secret key |
| `MINIO_BUCKET_PDFS` | `voyager-pdfs` | Bucket de PDFs |
| `AWS_S3_ENDPOINT_URL` | `MINIO_ENDPOINT` | django-storages |
| `MCP_RATE_LIMIT_RPM` | 60 | Rate limit por cliente no MCP (token-bucket Redis) |
| `MCP_ENABLE_CLASSIFICACAO` | `False` | Se `True`, tool `classificacao_lead` é exposta (gate de autorização) |

## Filas RQ novas

| Fila | Função | Workers (dev) |
|---|---|---|
| `es_index` | Indexação write-through | 4 |
| `pdf_download` | Download de PDFs | 2 |
| `monitoring` | Varredura + webhook delivery | 2 |

(MCP não usa fila RQ — é síncrono no request do agente.)

## Management commands novos

| Command | Função |
|---|---|
| `es_ensure_indexes` | Cria indexes ES com mappings |
| `reindexar_movimentacoes` | Backfill ES (bulk) |
| `reindexar_processos` | Backfill ES processos |
| `ensure_minio_bucket` | Cria bucket MinIO |
| `baixar_pdfs_pendentes` | Backfill PDFs |
| `popular_assunto_norm` | Backfill assunto_norm |
| `varredura_diaria` (via scheduler) | Monitoramento push |
| `gerar_mcp_token` | Gera `ApiClient.mcp_token` pra um cliente |