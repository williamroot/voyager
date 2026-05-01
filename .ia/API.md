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
| POST | `/api/v1/leads/consumed/` | Marca processos como consumidos com resultado |
| GET | `/api/v1/leads/stats/` | Métricas agregadas pro cliente |
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

### `POST /api/v1/leads/consumed/`

Body:
```json
{"consumos": [
  {"cnj": "...", "resultado": "validado"},
  {"cnj": "...", "resultado": "sem_expedicao"}
]}
```

Resultados aceitos: `validado`, `sem_expedicao`, `erro`, `pendente`, `pago`, `arquivado`, `cedido`.

Sem unique constraint — re-consumo permitido (cria registro novo, mantém histórico).

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
