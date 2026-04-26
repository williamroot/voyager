# Padrões e anti-padrões

Padrões idiomáticos do projeto. Quando estiver em dúvida, escolha a opção que combina com estes.

## Imports

✅ Sempre no topo, em 3 grupos separados por linha em branco:

```python
import json                          # stdlib
import logging
from datetime import date

import requests                      # third-party
from django.db import transaction
from rest_framework import viewsets

from djen.proxies import ProxyScrapePool   # local
from tribunals.models import Process

from . import queries                # local relativo
```

✅ Ordem alfabética dentro de cada grupo.

❌ **Imports inline** dentro de função — só permitido pra **try/except ImportError** de feature opcional (Sentry, pythonjsonlogger).

❌ Wildcard imports (`from x import *`).

## Models

✅ **Constraints e indexes na Meta**, nunca via SQL ad-hoc (exceto extensions/triggers). Trigger SQL fica em RunSQL na migration:

```python
class Movimentacao(models.Model):
    ...
    class Meta:
        constraints = [UniqueConstraint(fields=['tribunal', 'external_id'], name='uniq_mov_tribunal_extid')]
        indexes = [Index(fields=['tribunal', '-data_disponibilizacao'])]
```

✅ **Constraints partial** (`condition=Q(...)`) quando dedupe contextual:

```python
UniqueConstraint(fields=['documento'], condition=~Q(documento=''), name='uniq_parte_documento')
```

✅ **Triggers SQL pra agregações em massa** (statement-level com REFERENCING NEW/OLD TABLE) — escala melhor que signals Django.

❌ Não usar `assert` em prod-path (`-O` strip). Usar `if ... raise ValueError`.

❌ Não usar signals pra agregação heavy — preferir trigger SQL.

## Bulk operations

✅ `bulk_create(ignore_conflicts=True)` pra idempotência. Combine com `UniqueConstraint` no DB.

✅ `bulk_update(fields=[...])` pra update em massa.

✅ `update_fields=[...]` em todo `instance.save()` que toca poucos campos.

✅ Métricas TOCTOU aceitas (`SELECT ... WHERE id IN (...)` antes do bulk_create) — documentar.

❌ **NUNCA** `for x in qs: x.save()` em loops.

## Proxy / HTTP

✅ Reaproveitar `ProxyScrapePool.singleton()` + `cortex_proxy_url()` em **qualquer** cliente HTTP de tribunal/DJEN. Pool é shared via Redis.

✅ Backoff exponencial com jitter. Diferenciar status codes (403/429 = mark_bad + retry; 5xx = manter proxy + backoff longo).

❌ `time.sleep` em loops sem jitter (thundering herd).

❌ Hardcode de proxy fora dos helpers.

## Logs

✅ `logger = logging.getLogger('voyager.<modulo>')` no topo do módulo.

✅ Logs estruturados via `extra={...}` carregando contexto:

```python
logger.info('djen request', extra={
    'sigla_djen': sigla_djen, 'pagina': pagina, 'attempt': attempt,
    'proxy': using if proxy_url else 'direct',
    'status_code': resp.status_code, 'latency_ms': latency_ms,
})
```

❌ f-strings com PII em mensagens — usar `extra` (pode ser scrubbed).

## DRF

✅ ViewSets com `mixins.ListModelMixin + mixins.RetrieveModelMixin` (read-only).

✅ Serializers separados em **List** vs **Detail** — list traz campos enxutos.

✅ Filtros em `FilterSet` declarativos (`django-filter`), não em `get_queryset` ad-hoc.

✅ Cursor pagination pra entidades de alto volume (`Movimentacao`).

❌ `HttpResponse` cru em viewset — usar `Response`.

## HTMX / Alpine

✅ Cada chart envolto em `.chart-cell` com `.chart-skeleton` irmão. `setupChart($el, opts)` remove skeleton ao inicializar.

✅ Charts carregam via `lazyChart($el, url, builder)` em vez de SSR — view só passa KPIs, charts buscam JSON em endpoints `/dashboard/api/chart/<key>/`.

✅ **Listagens grandes** (qualquer tabela/lista que possa ter >50 rows) seguem o **pattern shell + lazy + paginação HTMX** — view bifurca por `HX-Request`, retorna shell (sem queryset) ou partial. Detalhes em [`DASHBOARD.md`](DASHBOARD.md#padrão-obrigatório-listagens-com-lazy-load--paginação-htmx).

✅ Container de lista tem `id="<nome>-list"` (sufixo obrigatório — loading overlay detecta via `[id$="-list"]`).

✅ Filtros em URL (chips são `<a href="?...">`). Back/forward funciona, link compartilhável.

❌ `data-echart='{...}'` com valores inline — quebra com aspas no JSON.

❌ **Renderizar lista server-side junto com a página**. Sempre lazy.

❌ **Paginação que recarrega a página inteira**. Sempre HTMX swap do `#xxx-list`.

## CSS

✅ Sempre tokens semânticos: `bg-card`, `text-fg`, `border-border`, `text-accent-fg`, `text-danger`, `bg-warning/15`.

❌ Nunca cores literais: ~~`bg-zinc-900`~~, ~~`text-emerald-400`~~ (exceto status colors específicos via filtro `type_classes`).

✅ `dark:` prefix só pra status colors (intimação=sky, decisão=emerald) que precisam de variante explícita por tema.

## Migrations

✅ Geradas por `makemigrations` exceto data migrations (manual).

✅ Data migrations idempotentes (`update_or_create`, não `create`).

✅ Trigger SQL em `RunSQL` com `reverse_sql` correspondente. Idempotente (`CREATE OR REPLACE` + `DROP IF EXISTS`).

❌ **Nunca dropar coluna em uma deploy só** — etapa 1 nullable + parar de escrever; etapa 2 drop.

## Dashboard / templates

✅ Componentes em `_partials/` reusáveis com `{% include ... with var=value %}`.

✅ Custom template tags em `<app>/templatetags/<app>_extras.py`. Decorate com `@register.filter` ou `@register.simple_tag`.

✅ `{% spaceless %}` em badges/chips pra evitar whitespace que estraga inline-flex.

## Tests

✅ `pytest` + `pytest-django`. Sem `unittest.TestCase`.

✅ Camadas:
- **unit**: parser, dedupe, classificações — sem DB nem rede
- **integration**: `ingest_window` com Postgres real (testcontainers ou pg fixture), DJEN mockado via `responses`
- **api**: DRF com `APIClient`
- **smoke**: `djen_run_now TRF1 --dias 1` em staging

❌ Testes que dependem de ordem (use fixtures isoladas).

## Commits

✅ Conventional Commits, em **pt-BR**, imperativo, presente:
```
feat(djen): adiciona retry pra ChunkedEncodingError
fix(dashboard): light mode com paleta inspirada no falcon
docs(.ia): atualiza padrões de bulk_create
refactor(enrichers): extrai helpers de parsing pra parsers.py
```

✅ Linha 1 ≤ 72 chars. Corpo (opcional) explica **por quê**, não **o quê**.

❌ `--no-verify`, `--amend` em commits publicados.

❌ Mensagens em inglês ou misturadas.
