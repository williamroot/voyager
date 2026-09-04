# API REST

DRF read-only sob `/api/v1/`. Auth via API key (`Authorization: Api-Key <key>`). Sem rate limit (deliberado).

## Endpoints

| Método | Path | Descrição |
|---|---|---|
| GET | `/api/v1/tribunais/` | Lista |
| GET | `/api/v1/tribunais/<sigla>/` | Detalhe |
| GET | `/api/v1/tribunais/<sigla>/estatisticas/` | Counts + último run + drift |
| GET | `/api/v1/processos/` | Lista paginada (LimitOffset) |
| GET | `/api/v1/processos/<id_or_cnj>/` | Detalhe (lookup numérico ou CNJ) |
| GET | `/api/v1/processos/<id>/movimentacoes/` | Movs do processo (cursor) |
| GET | `/api/v1/movimentacoes/` | Lista paginada (cursor) com `?q=` busca textual |
| GET | `/api/v1/movimentacoes/<id>/` | Detalhe |
| GET | `/api/v1/ingestion-runs/` | Histórico |
| GET | `/api/v1/leads/` | Próximos N leads não consumidos (Juriscope integration) |
| POST | `/api/v1/leads/consumed/` | Enfileira consumo (async, 202) — requer `lote_id` |
| GET | `/api/v1/leads/stats/` | Métricas agregadas pro cliente |
| GET | `/api/v1/busca/processos/<cnj>/` | **Busca v1 (ES)** — doc completo por CNJ exato |
| GET | `/api/v1/busca/processos/<cnj>/movimentacoes/` | Movs do processo (cursor search_after) |
| GET | `/api/v1/busca/processos/` | Busca parte/advogado/valor/documento/OAB/vara + filtros |
| GET | `/api/v1/busca/movimentacoes/` | Full-text no body + highlight (ver seção Busca v1) |
| GET | `/api/v1/health/` | Readiness rico (503 se lag >36h em algum ativo) |
| GET | `/api/v1/health/liveness/` | Liveness simples (200 sempre se up) |
| GET | `/api/v1/schema/` | OpenAPI (drf-spectacular) |
| GET | `/api/v1/docs/` | Swagger UI |

## Filtros (`api/filters.py`)

**ProcessFilter:**
- `tribunal` (= ou `__in` CSV)
- `numero_cnj` exact
- `inserido_em__gte/lte`, `ultima_movimentacao_em__gte/lte`
- `sem_movimentacoes` (bool)

**MovimentacaoFilter:**
- `tribunal`, `processo`, `numero_cnj`
- `data_disponibilizacao__gte/lte`, `inserido_em__gte/lte`
- `tipo_comunicacao` (iexact), `nome_classe` (iexact), `codigo_classe`
- `q` (busca textual — ver abaixo)

## Busca textual (`?q=`)

Híbrido pra economizar full-text quando não vale a pena:

```python
def filter_search(qs, value):
    if len(value) < MIN_SEARCH_LENGTH (3):
        return qs
    if len(value.split()) >= 3:
        # tsquery websearch + rank
        return qs.filter(search_vector=SearchQuery(value, config='portuguese', search_type='websearch'))
                 .annotate(rank=SearchRank(...))
                 .order_by('-rank', '-data_disponibilizacao')
    # ILIKE %x% (usa GIN gin_trgm_ops index pra termos curtos ≥3 chars)
    return qs.filter(texto__icontains=value).order_by('-data_disponibilizacao')
```

## Paginação

- `DefaultPagination(LimitOffsetPagination)`: `default_limit=50`, `max_limit=200` — pra tribunais/processos/runs
- `MovimentacaoCursorPagination(CursorPagination)`: `page_size=50`, `ordering=('-data_disponibilizacao', '-id')` — pra movs (volume alto, ordenação estável)

## Auth — API key

Lib: `djangorestframework-api-key`. Chaves criadas via `/admin/rest_framework_api_key/apikey/`.

```bash
curl -H "Authorization: Api-Key <key>" http://voyager.exemplo.com/api/v1/movimentacoes/?tribunal=TRF1&q=precatório
```

Dashboard usa **sessão Django** — não API key. Separação clara entre os canais.

## Serializers

Pares List/Detail por entidade. ViewSet escolhe via `get_serializer_class`. Detail estende List adicionando campos pesados (`texto`, `destinatarios`, etc.).

## Exemplo

```bash
# Movimentações do TRF1 nos últimos 7 dias com termo "precatório"
curl -sH "Authorization: Api-Key K" \
  "http://localhost/api/v1/movimentacoes/?tribunal=TRF1&q=precatório&data_disponibilizacao__gte=2026-04-18T00:00:00Z" \
  | jq '.results[0]'
```

```json
{
  "id": 123,
  "tribunal": "TRF1",
  "numero_cnj": "1000041-41.2017.4.01.3507",
  "data_disponibilizacao": "2026-04-22T12:00:00Z",
  "inserido_em": "2026-04-23T14:00:00Z",
  "tipo_comunicacao": "Intimação",
  "nome_orgao": "Vara Federal Cível e Criminal..."
}
```

## OpenAPI

Schema gerado automaticamente em `/api/v1/schema/`. Swagger UI em `/api/v1/docs/`.

## Leads API (Juriscope integration)

**Auth diferente**: header `X-API-Key: <key>` (não `Authorization: Api-Key`). Cada cliente tem `tribunals.ApiClient` com chave única (gerar via Django admin). Chave inválida → 403.

Documentação completa + casos de uso: ver [`CLASSIFICACAO.md`](CLASSIFICACAO.md).

### `GET /api/v1/leads/?nivel=PRECATORIO&tribunal=TRF1&limit=5000&min_score=0`

Retorna processos classificados não-consumidos pelo cliente. Filtra `LeadConsumption` via `Exists(OuterRef)` (anti-join — escala com 100k+ consumos).

**Ordenação**: `(-ultima_movimentacao_em, -classificacao_score, -id)` — mais recentes primeiro, score como tiebreaker. Vale pra todos os tribunais. Motivação: pra precatórios, a expedição de ofício mais recente implica ordem orçamentária mais distante (CF art. 100 §5º — expedição após 02/abril vai pro orçamento de ano+2), então recência da última mov é o sinal operacional mais útil pro consumidor.

```json
{
  "count": 5000, "limit": 5000, "nivel": "PRECATORIO",
  "results": [
    {"cnj": "1047957-32.2025.4.01.3300", "tribunal": "TRF1",
     "classificacao": "PRECATORIO", "score": 0.989,
     "classe_nome": "...", "ano_cnj": 2025,
     "link_voyager": "https://voyager.was.dev.br/dashboard/processos/1446087/"}
  ]
}
```

### `POST /api/v1/leads/consumed/` — **assíncrono (202)**

Body (`lote_id` UUID obrigatório):
```json
{"lote_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
 "consumos": [
   {"cnj": "...", "resultado": "validado"},
   {"cnj": "...", "resultado": "sem_expedicao"}
]}
```

Resultados aceitos: `validado`, `sem_expedicao`, `erro`, `pendente`, `pago`, `arquivado`, `cedido`.

Não grava síncrono: **enfileira** o job RQ `registrar_consumo_leads` na fila
`leads_consumo` (worker `worker_leads_consumo`, 4 réplicas) e responde `202`:

```json
{"enfileirado": true, "lote_id": "f47ac10b-...", "recebidos": 4180}
```

Idempotente por `(cliente, processo, lote_id)` via constraint partial
(`uniq_consumo_cliente_proc_lote WHERE lote_id IS NOT NULL`): retry RQ /
reenvio do mesmo lote nunca duplica nem perde. `LeadConsumption.lote_id`
é nullable (NULL = legado pré-cutover).

Erros:
- `403` — X-API-Key inválida/ausente
- `400` — `lote_id` ausente ou não-UUID; `consumos` vazio, não-lista ou >5000
- `503` — enqueue na fila falhou (Redis fora) — **cliente deve retentar**

### `GET /api/v1/leads/stats/`

```json
{
  "pending": {"PRECATORIO": 4521, "PRE_PRECATORIO": 12330, "DIREITO_CREDITORIO": 78112},
  "consumidos_total": 5000, "consumidos_hoje": 5000,
  "consumidos_por_resultado": {"validado": 4180, "sem_expedicao": 720},
  "validados_total": 4180, "taxa_validacao": 0.836,
  "modelo_versao": "v5", "modelo_atualizado_em": "..."
}
```

## API de Busca v1 (`/api/v1/busca/*`) — 100% Elasticsearch

Busca rica pra clientes/integrações. Lê SÓ o ES (`voyager-processos` ~71M docs,
`voyager-movimentacoes` ~1.16B) — Postgres de prod é contido, nunca tocado.
Serviço: `search/busca_api.py` (contrato completo no docstring). Views:
`api/busca_views.py`. Auth: `HasAPIKeyOrBearer` (Bearer | Api-Key | X-API-Key).

**Erros (nunca 500 cru)**: `400 {"erro": "cnj_invalido|cursor_invalido|informe_q_proc_ou_tribunal"}`,
`404 {"erro": "processo_nao_encontrado"}`, `503 {"erro": "elasticsearch_indisponivel"}`.

**Paginação**: cursor opaco (`next_cursor`, search_after em base64) — NUNCA
from/size (índice de 1.16B docs estoura o max_result_window de 10k). Repassar
`cursor=` intocado; `null` = acabou. `total` tem cap ES de 10k (`relation: "gte"`).
`size` clampa em [1, 100] (default 20; movs do processo 50).

| Método | Path | Descrição |
|---|---|---|
| GET | `/api/v1/busca/processos/<cnj>/` | Doc completo por CNJ exato (term `proc`; aceita máscara ou 20 dígitos) |
| GET | `/api/v1/busca/processos/<cnj>/movimentacoes/` | Movs do processo (publish_date desc, cursor) |
| GET | `/api/v1/busca/processos/` | Busca por parte/advogado/valor + filtros combinados |
| GET | `/api/v1/busca/movimentacoes/` | Full-text no body das publicações + highlight |

### `GET /busca/processos/` — params

- **Texto** (match AND, combináveis): `parte=` (campo `partes`), `advogado=`
  (campo `advs`; aceita nome ou nº OAB — o doc traz "Nome (OAB XXX)").
- **Filtros**: `tribunal=` (CSV ⇒ terms), `uf=`, `ano_min/ano_max` (ano_cnj,
  clamp [1990, atual]), `classificacao=`, `tem_sinal_precatorio=`,
  `tem_ente_publico_passivo=`, `segredo_justica=`, `enriquecido=` (bools
  true/false/1/0), `codigo_classe=`, `assunto_codigo=` (TPU),
  `valor_min/valor_max` (valor_causa; negativo ignorado),
  `ultima_mov_gte/lte`, `autuacao_gte/lte` (ISO).
- **`ordenar=`**: `relevancia` (default com texto) | `recente`
  (ultima_movimentacao_em desc, default sem texto) | `valor_desc` | `valor_asc`
  (sem valor vai pro fim). Sort sempre com tiebreaker `id` (search_after estável).
- Inválido degrada limpo: ano clampado, faixa invertida ordenada, enum
  desconhecido cai no default.

```bash
curl -sH 'X-API-Key: K' 'https://voyager.was.dev.br/api/v1/busca/processos/?parte=estado+de+sao+paulo&tribunal=TJSP&tem_sinal_precatorio=1&valor_min=100000&ordenar=valor_desc&size=10'
```
```json
{"versao": "v1", "total": {"value": 10000, "relation": "gte"},
 "results": [ { ...doc ES do processo (proc, tribunal, uf, partes, advs,
                valor_causa, ano_cnj, classificacao, ...)... } ],
 "size": 10, "next_cursor": "WzEyMy45LDQ1Nl0", "ordenar": "valor_desc",
 "filtros": {"tribunal": ["TJSP"], "tem_sinal_precatorio": true,
             "valor_min": 100000.0, "parte": "estado de sao paulo"}}
```

### `GET /busca/movimentacoes/` — params

`q=` full-text no `body` (match AND) com highlight (`<em>`, 3×200 chars).
Exige pelo menos um de `q`/`proc`/`tribunal` (400 senão). Filtros: `tribunal=`
(CSV), `proc=` (CNJ), `tipo_comunicacao=`, `codigo_classe=`,
`publicado_gte/lte` (publish_date, ISO). `ordenar=`: `relevancia` (default com
q) | `recente`. A lista NÃO devolve o `body` inteiro (payload) — texto completo
via `/busca/processos/<cnj>/movimentacoes/` ou `/diarios-oficiais/doc/get/<id>`.

```bash
curl -sH 'X-API-Key: K' 'https://voyager.was.dev.br/api/v1/busca/movimentacoes/?q=precat%C3%B3rio&tribunal=TJSP&publicado_gte=2026-07-01'
```
```json
{"versao": "v1", "total": {"value": 10000, "relation": "gte"},
 "results": [{"id": 1271133182, "proc": "1105916-36.2026.8.26.0053",
              "tribunal": "TJSP", "publish_date": "2026-07-15T03:00:00+00:00",
              "tipo_comunicacao": "Intimação", "nome_orgao": "...",
              "classe_nome": "...", "codigo_classe": "...", "docurl": "...",
              "highlight": ["...apresentação do <em>precatório</em>..."]}],
 "size": 20, "next_cursor": "...", "ordenar": "relevancia",
 "filtros": {"tribunal": ["TJSP"], "publicado_gte": "2026-07-01", "q": "precatório"}}
```

### Filtros ESTRUTURADOS (13/08/2026) — documento, OAB, vara, CNJ

Aditivos, aceitos também em `/busca/processos/` (sem breaking change):

| Param | Campo ES | Nota |
|---|---|---|
| `cnj=` | `proc` (term) | com ou sem máscara |
| `documento=` | `participacoes.documento` (nested) | CPF/CNPJ digitado de qualquer jeito |
| `documento_raiz=1` | idem, `prefix` na raiz | CNPJ: matriz + filiais |
| `documento_parciais=1` | idem, `terms` LGPD | inclui `872.***.***-97` etc. |
| `documento_forcar=1` | — | aceita DV inválido |
| `parte_polo=` `parte_papel=` | `participacoes.polo/papel` | move `parte=` pro nested |
| `oab=` | `participacoes.oab` | `SP123456`/`123456/SP`/`sp 123.456` |
| `vara=` | regexp em `orgao_julgador` OU `juizo` | acento- e caixa-insensível |
| `vara_exata=` | term nos mesmos campos | valor do autocomplete |

**A máscara é do ÍNDICE.** Medido: `term participacoes.documento =
"29979036000140"` → **0 docs**; `"29.979.036/0001-40"` → **23.217**. `regexp` de
11/14 dígitos puros → 0. Por isso `normalizar_documento` gera a forma mascarada
canônica. DV inválido é **400** (`cpf_dv_invalido`), não "0 resultados".

### Limitações (ago/2026)

- **Cobertura dos campos estruturados** (medido em 71.308.806 processos):
  `partes`/`advs` 100% · `orgao_julgador` não-vazio **25,09%** · `juizo`
  não-vazio **1,82%** · `participacoes.nome` **1,90%** ·
  `participacoes.documento` **0,14%** · `participacoes.oab` **0,067%**.
  Buscar por CPF e achar 0 NÃO é "não tem processo". Quem serve TELA usa
  `/dashboard/api/busca/processos/`, que devolve essa cobertura no payload.
- Campos novos (data_autuacao, segredo_justica, enriquecido, assunto_codigo...)
  só existem em docs reindexados pós-7d03bab — filtrar por eles restringe ao subset.
- `valor_causa` tem outliers de digitação (o serviço não higieniza).

## Busca POR PARTE ao vivo no tribunal (`/api/v1/busca/tribunal/*`)

As buscas acima leem o ÍNDICE. Esta fala com a **consulta pública do tribunal**,
ao vivo, e existe pelo motivo medido: no ES, `participacoes.documento` cobre
**0,14%** e `participacoes.oab`, **0,067%**. Buscar CPF no índice e achar zero é
ausência de dado, não de processo — a única fonte que responde "quais processos
esta pessoa tem" é o formulário do tribunal.

Serviço: `enrichers/busca/` (contrato no `base.py`; catálogo do que cada fonte
aceita no `registry.py`). Views: `api/busca_tribunal_views.py`. Auth: a mesma
`HasAPIKeyOrBearer`. Matriz medida por tribunal × critério, com fixture de cada
desfecho: `.ia/ENRICHMENT.md` §"Busca POR PARTE".

**Assíncrona**, e não por gosto: uma página do e-SAJ vai de 0,2 s a 71 s, um
POST no PJe leva ~20 s, e "em todos" fala com nove fontes. Um job por tribunal,
fila `busca_ao_vivo`.

| Método | Path | Descrição |
|---|---|---|
| POST | `/api/v1/busca/tribunal/` | cria a busca ⇒ **202** com `run_id` (ou **200** com a resposta em cache) |
| GET | `/api/v1/busca/tribunal/<run_id>/` | andamento + resultados parciais |
| GET | `/api/v1/busca/tribunal/catalogo/` | o que cada fonte aceita, o teto dela e **desde quando foi medida** |

### POST — corpo

```json
{"criterio": "documento|nome|oab|advogado",
 "valor": "111.444.777-35",
 "tribunais": ["TJSP","TJMG"] | "todos",
 "forcar": false}
```

`criterio` fora da lista, CPF com DV quebrado, raiz de CNPJ (8 dígitos, que
nenhum formulário de tribunal aceita) e nome com menos de 4 letras são **400 com
mensagem humana** — recusados antes de gastar IP do pool. Tribunal fora do
catálogo é 400 dizendo quais existem.

`forcar: true` ignora o cache de **6 h** (mesma pergunta + mesmo escopo). Teto de
**20 buscas/minuto por ApiClient**: cada uma pode virar nove jobs de scraping no
pool que o enriquecimento em massa também usa.

### GET — a resposta, e por que ela é assim

```json
{"versao": "busca-tribunal-1", "run_id": "...", "status": "running|concluido|erro",
 "criterio": "documento", "valor": "111.444.777-35", "encontrados": 250,
 "novos_no_acervo": 61,
 "resultados": [{"numero_cnj": "...", "tribunal": "TJSP", "classe": "...",
                 "assunto": "...", "orgao": "...", "distribuicao": "...",
                 "url_fonte": "...", "partes_na_lista": ["AUTOR", "RÉU"]}],
 "por_tribunal": {"TJSP": {"status": "ok", "paginas_lidas": 10, "encontrados": 250,
                           "total_declarado": 1000, "total_e_teto": true,
                           "truncado": true, "motivo_truncagem": "...", "levou_s": 88.2}},
 "avisos": [{"codigo": "truncado", "tribunal": "TJSP", "mensagem": "..."}],
 "erros": [], "em_cache": false}
```

O estado é **por tribunal** porque as quatro maneiras de não achar nada são
coisas diferentes, e achatá-las num status único é exatamente o erro que esta
feature existe para não cometer:

| `status` do tribunal | significa | aviso |
|---|---|---|
| `ok` / `vazio` | a fonte respondeu; achou / não achou | — |
| `criterio_indisponivel` | **a fonte não tem esse campo** (o TJPA não busca por nome de advogado) | `criterio_indisponivel` |
| `refinar` | a fonte se RECUSOU a responder: "muitos processos, refine sua busca" | `refinar` |
| `fonte_indisponivel` | não respondeu (WAF, timeout, outra página) — re-tentável | `fonte_indisponivel` |
| `erro` | falha inesperada naquele tribunal; os outros seguem | — |

Outros avisos: `truncado` (bateu o teto da fonte, o de páginas ou o de tempo —
sempre com o número real), `fonte_inconsistente` (o TRF5 anuncia 30 resultados
mostrando 1) e `nao_verificado` (aquele critério nunca foi exercitado ao vivo
naquele tribunal — vale para o TRF3 inteiro e para OAB/advogado no TRF1 e TRF5).

`status` do run é `concluido` mesmo com tribunal indisponível: a busca
aconteceu, e o que deu errado está dito por tribunal. Só vira `erro` quando
**nenhuma** fonte respondeu.

### O que ela faz com o que acha

Tudo que é achado e ainda não está no acervo entra: `hidratar_cnj` cria o
`Process`, puxa os movimentos do Datajud e enfileira o enricher do tribunal
(fila `busca_hidratacao`, separada porque o rate limit do Datajud é global). A
segunda busca igual sai do índice, de graça. Teto de **500 processos por
consulta**; passar disso é ERRO registrado em `erros[]` com o número real, nunca
um corte mudo.

### Tetos, em um lugar só

| teto | valor | onde |
|---|---|---|
| páginas por tribunal | 10 (≈250 no e-SAJ) | `enrichers/busca/jobs.py` |
| tempo por tribunal | 180 s | idem |
| ingestão por consulta | 500 processos | `enrichers/busca/ingestao.py` |
| cache da mesma pergunta | 6 h | `api/busca_tribunal_views.py` |
| buscas por minuto/cliente | 20 | idem |
| **teto da FONTE** | 1.000 (e-SAJ) · 30 (PJe) · sem teto (TJPA, TJMT) | medido — `registry.py` |

## Busca de processos da TELA (`/dashboard/api/busca/*`)

JSON interno (**sessão logada**, `@require_GET`). Mesmo serviço da v1
(`search/busca_api.py`) + envelope de honestidade em `search/busca_ui.py`
(contrato completo no docstring de lá). Views: `dashboard/busca_views.py`.

| Método | Path | Descrição |
|---|---|---|
| GET | `/dashboard/api/busca/processos/` | Busca completa + `cobertura` + `avisos` |
| GET | `/dashboard/api/busca/varas/?q=` | Autocomplete de vara/órgão julgador |

Aceita TODOS os params da v1 mais `q=` (caixa única: detecta CNJ / CPF / CNPJ /
raiz / OAB / nome e ecoa em `interpretacao`). Além do envelope v1, a resposta
traz `cobertura` (quanto da base cada critério alcança), `documento`
(alternativas contadas: raiz do CNPJ, mascarados por LGPD) e `avisos` (frases
prontas). Erros 400 vêm com `mensagem` humana; ES fora → 503. Cache 60 s por
querystring; cobertura cacheada 6 h; varas 15 min.

**Latência medida no ES de prod (13/08/2026, frio/morno):** CNJ exato 10/10 ms ·
nome "INSTITUTO NACIONAL DO SEGURO SOCIAL" (4.403.363 hits) 91/86 ms · CNPJ do
INSS nas 3 grafias 37-47 ms · OAB 27/24 ms · vara+tribunal 98/80 ms ·
autocomplete de vara 14-347 ms (termo genérico "vara civel" 902 ms frio, 18 ms
morno). Pico observado: **1.690 ms** na medição de cobertura (1× a cada 6 h).

## Mapa Comercial — agregações ES (`/dashboard/api/overview/*`)

JSON interno do dashboard (**sessão logada**, não API key). 100% Elasticsearch —
nenhuma leitura no Postgres (só o cache de cobertura). Contrato completo no topo
de `search/agg_overview.py` (mapa) e `search/agg_estado.py` (página de estado).

| Método | Path | Descrição |
|---|---|---|
| GET | `/dashboard/api/overview/mapa` | Agregado por UF + total + score de foco |
| GET | `/dashboard/api/overview/tribunais?uf=SP` | Drill-down por tribunal da UF |
| GET | `/dashboard/api/overview/top?metric=score&n=10` | Ranking Top-N por UF |
| GET | `/dashboard/api/overview/estado/<uf>/` | **Página dedicada do estado** (blocos "explodidos") |

Filtros (querystring, comuns aos 4): `uf, tribunal, classificacao,
tipo(potencial|confirmado), tem_sinal, ano_min, ano_max, valor_min, valor_max,
codigo_classe, natureza` — saneados em `agg_overview.parse_filtros` (ano futuro
clampa, valor negativo cai fora, enum desconhecido vira default; nunca 500).

### `GET /dashboard/api/overview/estado/<uf>/`

`<uf>` = 27 siglas + `FED`. UF inválida → **400**; ES fora → **503**.
Param extra: `metrica=possiveis|confirmados|todos` (default `todos`) — a LENTE
dos blocos de detalhe; `todos` é UNIÃO (`bool.should`), **nunca soma**.
1 request ES (`_msearch` de 2 sub-buscas), cache 5min (2min com filtro).

Blocos: `resumo` (volume/possíveis/confirmados/todos/valor/cobertura/nº tribunais),
`por_tipo_processo` (classe_nome), `por_ano` (ano_cnj), `por_tribunal`,
`por_classificacao`, `por_entidade_devedora` (nested `participacoes` polo passivo,
grafias fundidas por normalização + filtro de ente público).

**Honestidade obrigatória**: todo bloco traz `cobertura_amostra`
`{campo, docs_com_o_campo, total_do_escopo, total_do_estado, pct}` — o front
escreve "amostra de X% dos processos" quando `pct < 100`. Campos parciais
medidos em 12/08/2026: `participacoes` (RO = 0 indexadas), `valor_causa` (2,8%
da base), `classe_nome` presente em 100% mas **vazia** em 69,6% dos docs de RO.

Latências medidas (ES de prod, cache frio): SP 215ms · RO 199ms · MG 84ms ·
FED ~2-5s (21,5M docs; amortizado pelo cache). Cache quente: <1ms.

## API Jusbrasil/Digesto-compat (Diários Oficiais)

Endpoints compatíveis com a API Jusbrasil/Digesto de Diários Oficiais. Spec:
https://api.jusbrasil.com.br/docs/diarios_oficiais/busca.html

### Auth

Aceita 3 formas (em ordem de precedência):
1. `Authorization: Bearer <token>` — lookup em `ApiClient.api_key`
2. `Authorization: Api-Key <key>` — padrão `rest_framework_api_key`
3. `X-API-Key: <key>` — legacy leads

### Endpoints

| Método | Path | Descrição |
|---|---|---|
| POST | `/api/v1/diarios-oficiais/doc/buscar` | Busca textual (body = query DSL ES) |
| GET | `/api/v1/diarios-oficiais/doc/get/<id>` | Detalhe de uma publicação |
| GET | `/api/v1/diarios-oficiais/fontes_recortes` | Lista de diários cobertos |
| GET | `/api/v1/monitoramento/proc/tipos_norm_andamentos_movs` | Tipos normalizados |
| GET | `/api/v1/base-judicial/tribproc/status_cobertura?area=` | Cobertura por área |
| POST/GET | `/api/monitoramento/monitored_term` | CRUD termos monitorados |
| POST/GET | `/api/monitoramento/monitored_person` | CRUD pessoas monitoradas |
| POST/GET | `/api/monitoramento/proc` | CRUD processos monitorados |
| GET | `/api/monitoramento/detections` | Detecções recentes |

### Busca (`POST /doc/buscar`)

Body = query DSL Elasticsearch 7.14 (passthrough pro cluster). Response = envelope ES nativo (`took/hits/_shards`).

```json
{"from": 0, "size": 10, "query": {"query_string": {"query": "precatório"}}}
```

### Limitações

- `prev_page`/`next_page`/`num_pag_original`: sempre `null` (DJEN não dá página do diário).
- `proc_alt`/`proc_apens`: sempre `null` (não existe no Voyager).
- Busca por comarca: não suportada (granularidade = tribunal).
- `assunto_norm`: heurístico (cobertura parcial; `[]` quando incerto).
- `cached_docurl`: disponível se PDF baixado (job async pós-ingestão).

### MCP Server

O Voyager também expõe um **MCP (Model Context Protocol) server** em `/mcp/`
pra LLMs/agentes (Claude Desktop, Cursor, etc.) conversarem sobre processos.

| Método | Path | Descrição |
|---|---|---|
| GET | `/mcp/.well-known/mcp.json` | Descriptor (tools, versão, auth) |
| POST | `/mcp/` | Initialize (handshake) |
| POST | `/mcp/messages` | JSON-RPC (`tools/list`, `tools/call`) |
| GET | `/mcp/sse` | SSE stream (compat) |
| POST | `/mcp/resources` | Resources (`resources/list`, `resources/read`) |

Auth: `Authorization: Bearer <mcp_token>` (gerar via `python manage.py gerar_mcp_token <nome_cliente>`).

12 tools disponíveis: `buscar_diarios`, `get_documento`, `get_processo`, `list_movimentacoes`,
`get_partes`, `listar_fontes`, `status_cobertura`, `monitorar_termo`, `monitorar_processo`,
`listar_detections`, `get_pdf`, `classificacao_lead` (gated por `MCP_ENABLE_CLASSIFICACAO`).

Configuração Claude Desktop:
```json
{"mcpServers": {"voyager": {"url": "https://voyager.was.dev.br/mcp/sse", "token": "<mcp_token>"}}}
```

### Página de configuração

O dashboard tem uma página interativa que ensina a configurar o MCP em cada cliente:
`/dashboard/mcp/` — mostra instruções pra Claude Desktop, Claude Code, ChatGPT, Gemini, Cursor e clientes customizados (curl).

Plano completo de implementação: [`JUSBRASIL_COMPAT_PLANO.md`](JUSBRASIL_COMPAT_PLANO.md).
