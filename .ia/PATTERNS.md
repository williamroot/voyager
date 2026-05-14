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

## Cache versioning via INCR (não delete_pattern)

✅ Invalidar grupos de chaves de cache via versão monotônica em vez de `cache.delete_pattern`:

```python
# Invalidar:
cache.incr('voyager:chart_version')  # ou cache.set se 1ª vez

# Compor a key:
ver = cache.get('voyager:chart_version') or 1
key = f'voyager:chart:{nome}:v{ver}:tribunais={...}'
```

❌ `cache.delete_pattern(...)` — no-op em backends sem scan/SCAN (LocMem, RedisCache padrão sem suporte). Pode parecer funcional em dev e falhar em prod.

Aplicação atual: `dashboard/views.py` invalida cache de charts de validação via `INCR voyager:chart_version`.

## Gating de campos sensíveis via helper de modelo

✅ Quando um campo é confidencial intra-equipe (texto livre com PII potencial, opinião pessoal etc), expor via método de instância:

```python
class ProcessoValidacao(models.Model):
    motivo = models.TextField(blank=True)

    def motivo_visivel_para(self, user) -> str:
        if user is None or not user.is_authenticated:
            return ''
        if self.usuario_id == user.pk:
            return self.motivo
        if user.has_perm('tribunals.can_view_motivo'):
            return self.motivo
        return ''
```

✅ Templatetag delega ao método (`{% motivo_visivel pv user %}`). DRF serializer chama o helper em `get_motivo`.

❌ Espalhar `if user.has_perm(...)` em N templates/views — corrige inconsistência depois.

## Compartilhar lógica entre paths A/B

✅ Quando um caminho ativo e um shadow precisam dar o mesmo resultado em algum sub-passo (ex.: categorização), extrair função pura compartilhada:

```python
def _categorizar(score, features, tribunal_id, versao_modelo=None):
    # lê ThresholdTribunal do DB, fallback aos defaults...

def classificar(processo, features=None):
    score = predict_score(features, pesos=_current_weights())
    return _categorizar(score, features, processo.tribunal_id), score, features

def classificar_shadow(processo):
    for sv in shadow_versoes:
        score = predict_score(features, pesos=sv.pesos)
        cat = _categorizar(score, features, processo.tribunal_id)
        # ...
```

✅ Garante que A/B compara só o que varia (pesos do modelo), não a política de threshold.

❌ Duplicar a lógica de threshold em 2 funções — drift é inevitável (REVIEW_T20 issue #1).

## Sample weight em treino ML

✅ Logistic regression com `sample_weight` por origem do label permite misturar fontes de confiabilidade diferente sem descartar dados:

```python
# loss ponderada
loss = np.average(per_sample_loss, weights=sample_weight)
# gradiente também ponderado
grad = X.T @ (sample_weight * (sigmoid(X @ W) - y)) / sum(sample_weight)
```

Pesos atuais em uso: humano=3.0, juriscope=2.0, csv reforçado=2.0, csv base=1.0 (ver ADR-019).

## Hot reload de pesos (TTL + double-check lock)

✅ Quando configuração viva no DB precisa propagar pra workers sem restart:

```python
_CACHE = {'value': None, 'loaded_at': 0.0}
_LOCK = threading.Lock()

def _maybe_reload():
    if time.time() - _CACHE['loaded_at'] < TTL:
        return                                    # fast path sem lock
    with _LOCK:
        if time.time() - _CACHE['loaded_at'] < TTL:
            return                                # double-check
        try:
            _CACHE['value'] = read_from_db()
            _CACHE['loaded_at'] = time.time()
        except Exception:
            # preserva último valor bom; atualiza só timestamp pra
            # evitar storm de retry quando DB está fora
            _CACHE['loaded_at'] = time.time()
```

✅ Fallback hardcoded no módulo pra garantir que o worker **nunca** fica sem valor.

✅ `force_reload()` em testes/commands pula o TTL.

❌ Reload em cada chamada — caro e adiciona dependência de DB no hot path.

❌ Reload sem `loaded_at` no `except` — storm de retry quando DB está fora.

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
