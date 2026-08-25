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

### Ordem de aquisição de lock: SEMPRE por pk

✅ Todo `bulk_create`/`bulk_update` de tabela escrita por mais de um worker sai
em **ordem total** — `sorted()` na chave única no INSERT, `sort(key=pk)` no
UPDATE. Sem ordem comum existe ciclo de espera, e o Postgres mata um dos lados.

⚠️ `bulk_update` NÃO é uma transação por batch: ele envolve **todos** os
batches num único `transaction.atomic(savepoint=False)`. Ordenar a lista é
necessário mas não é suficiente quando o lote é grande — quem trava a linha
dentro de um `UPDATE ... WHERE id IN (...)` é o PLANO. Quando a tabela é quente
(`tribunals_process`, 86 M linhas, 14 réplicas de ingestão + drainers),
o padrão da casa é:

```python
lote.sort(key=lambda o: o.pk)
for i in range(0, len(lote), 500):
    faixa = lote[i:i + 500]
    with transaction.atomic():                     # uma transação POR lote
        list(Model.objects.filter(pk__in=[o.pk for o in faixa])
             .order_by('pk').select_for_update(no_key=True)
             .values_list('pk', flat=True))        # trava em ordem, sem depender do plano
        Model.objects.bulk_update(faixa, fields=CAMPOS, batch_size=len(faixa))
```

✅ Deadlock (SQLSTATE `40P01`) **pode** ser retentado — é erro transitório — com
teto, backoff+jitter e **o número de tentativas registrado** (regra nº 2). Só
retente quando a transação é sua (`connection.in_atomic_block` falso): o
deadlock aborta a transação inteira, retentar dentro de uma externa só produz
"current transaction is aborted".

Medido: duas transações concorrentes sobre os mesmos 300 processos em ordens
opostas dão **29/31/28 deadlocks** em 50 escritas sem esse padrão e **0** com
ele. Em produção eram 203 dias de coleta queimados (28,9% do cemitério da
`djen_backfill`). Ver `INGESTION.md` e `djen/ingestion.py::_gravar_lote_resumo`.

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

### ⚠️ O relógio do log é -03. TODO o resto é UTC.

Medido em 24/08/2026 dentro do MESMO container `voyager-web-1`, no mesmo
instante:

| relógio | valor |
|---|---|
| `date` (host e container) | `19:43:55 UTC` |
| `datetime.now()` **antes** do `django.setup()` | `19:43:56` (UTC) |
| `timezone.now()` | `19:43:59+00:00` |
| **`asctime` do log do Django** | **`16:40:31`** (-03) |

Causa, em uma frase: `settings.TIME_ZONE = 'America/Sao_Paulo'` e, ao instanciar
`Settings`, o Django escreve `os.environ['TZ']` e chama `time.tzset()` — e o
`logging.Formatter` monta `asctime` com `time.localtime()`. Ou seja, **o mesmo
processo grava log em -03 e mede tempo em UTC**, e os dois relógios divergem 3 h.

Consequências práticas:

- `ended_at`/`started_at` de job RQ são **naive/UTC**. Cruzar "o log diz 16:26"
  com "o job morreu 19:20" e concluir que são momentos diferentes é errado — é o
  mesmo momento. Já produziu uma linha do tempo errada num relatório de
  incidente nesta casa.
- `mtime` de arquivo (`ls -la`, `stat`) é UTC. Comparar com `asctime` sem somar
  3 h "prova" que um processo travou por horas quando ele acabou de escrever.
- ✅ **Em relatório, runbook e `.ia/`, hora de relógio é sempre UTC.** Ao citar
  linha de log, converta (`asctime + 3 h`) e diga que converteu.
- ✅ Preferir `extra={'em': timezone.now().isoformat()}` a depender do `asctime`
  quando o horário for parte do dado, não só do enquadramento.

(É primo do outro erro de fuso da casa: o ORM converte para `America/Sao_Paulo`
em `__date`, então filtro por dia e contagem por dia têm de usar o MESMO
critério dos dois lados — ver `.ia/SEARCH_SCHEMA.md`.)

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

### ⚠️ Verificação com INPUT VAZIO reporta SUCESSO

> **"Não havia nada para checar" e "não havia nada errado" saem com a mesma
> cara.**

Medido em 24/08/2026. O `run_tests.sh` roda `docker run --rm` **sem `-i`**, e
sem `-i` o stdin do container fica fechado: qualquer redirecionamento local
entrega **0 byte** lá dentro. Ruff, com stdin vazio, diz "All checks passed".

```bash
# quantos bytes o container REALMENTE recebe
$ wc -c /tmp/sched_head.py
23526 /tmp/sched_head.py
$ $SCRIPT sh -c 'wc -c' < /tmp/sched_head.py
0                                          # <- sem -i
$ docker run --rm -i ... voyager-test:local sh -c 'wc -c' < /tmp/sched_head.py
23526                                      # <- com -i

# e a consequência, o mesmo comando três vezes
A) $SCRIPT python -m ruff check --config ruff.toml --stdin-filename djen/scheduler.py - < /tmp/sched_head.py
   -> All checks passed!        # runner SEM -i: stdin vazio, nada foi checado
B) docker run --rm -i ... python -m ruff check --config ruff.toml --stdin-filename djen/scheduler.py - < /tmp/sched_head.py
   -> Found 4 errors.           # mesma entrada, com -i
C) docker run --rm -i ... python -m ruff check --config ruff.toml --stdin-filename djen/scheduler.py - < /dev/null
   -> All checks passed!        # stdin vazio de propósito
```

É a **regra nº 4 do CLAUDE.md aplicada à ferramenta em vez de ao dado**: assim
como `exists` do ES conta string vazia como valor presente, um verificador com
entrada vazia conta "sem violação" como "aprovado". E é a **regra nº 6**: uma
ferramenta de verificação deveria SE ABSTER quando não recebeu entrada, nunca
aprovar.

✅ **Confira paridade de lint por CAMINHO de arquivo.** Para checar uma versão
antiga, materialize-a num caminho (`git show <sha>:arq.py > /tmp/x/arq.py`) e
rode o linter nele.

✅ Se precisar de stdin, `docker run -i` **e valide que a entrada chegou**
(`wc -c` dentro do container).

### ⚠️ `'marcador' in html` decide sobre a PÁGINA INTEIRA — incluindo CSS, JS e comentário

> **Um substring casa em lugares que não são o dado.** O que você quer saber é
> se o *elemento* existe, não se a *palavra* aparece em algum canto do arquivo.

Três incidentes, o mesmo defeito, medidos em 25/08/2026 no e-SAJ:

| predicado | onde casou por engano | o que produziu |
|---|---|---|
| `'classeProcesso' in resp.text` | `<div class="classeProcesso">` da página de **lista** | a lista virava "detalhe"; `select_one('#classeProcesso')` não achava nada e o processo era gravado **`ok` com o cadastro inteiro vazio** |
| `'classeProcesso' in resp.text` | o **comentário HTML** da própria fixture `tests/fixtures/tjal/search_form.html`, que explicava "…sem `#classeProcesso`" | `test_enriquecer_nao_encontrado_emite_payload` passou a ler "não encontrado" como `ok` — a fixture derrubava o teste que ela ilustrava |
| `'…senha para acessar processo em segredo de justiça' in html` | `<form id="popupSenha" style="display: none;">`, presente em **TODA** página de detalhe (36 de 62 páginas baixadas; **33 delas com partes**) | marcaria segredo em processo bom e o **esconderia do funil** — pior que não detectar |

❌ **Errado** — o texto do arquivo inteiro responde por uma pergunta de estrutura:
```python
if 'classeProcesso' in resp.text:      # casa classe CSS e comentário
    return resp.text
if 'segredo de justiça' in resp.text:  # casa o popup escondido de toda página
    return 'segredo'
```

✅ **Certo** — pergunte pelo elemento, e defina cada desfecho pela presença E
pela **ausência** do que o distingue:
```python
_IDS_DETALHE = ('id="classeProcesso"', 'id="tablePartesPrincipais"', ...)

if any(m in html for m in _IDS_DETALHE):
    return DESFECHO_DETALHE
# segredo = chegamos na página do processo E o cadastro veio VAZIO
if 'id="containerDadosPrincipaisProcesso"' in html and 'id="popupSenha"' in html:
    return DESFECHO_SEGREDO
```

Regras que saem daí:

- **Marcador de existência é `id="x"`, não `x`.** Classe CSS, comentário e
  string de JS moram no mesmo arquivo que o dado.
- **Detector de ausência exige ausência.** "É segredo" só se NENHUM campo do
  cadastro estiver lá. Falso positivo aqui apaga processo bom — e some calado.
- **Prove com controle positivo E negativo, em HTML REAL.** Ver
  `tests/test_esaj_segredo.py`: a fixture da página normal existe justamente
  para falhar se alguém trocar a regra estrutural por uma frase.
- **Fixture sintética mente.** A do TJAL que "provava" a OAB é sintética, e por
  isso o Achado 6 (e-SAJ sem OAB, 0 de 347 advogados) passou meses despercebido
  — e o comentário dela quebrou outro teste. Colete HTML de verdade.

### ⚠️ `ssh $X sh -c "script"` não passa o script — o shell REMOTO o re-divide

Mesma doença, no scripting de operação. Com `X="ssh host docker exec cont"`, o
`ssh` **junta os argumentos com espaço** e o shell do outro lado re-interpreta a
linha inteira. Medido em 24/08/2026, três funções de um orquestrador quebradas
em silêncio:

| escrito | o que o remoto executou |
|---|---|
| `$X python manage.py shell -c "…cache.set(…)"` | `-c from`, depois `django.core.cache` como comando — o kill switch **nunca era acionado** |
| `$XD sh -c "python manage.py cmd … > /tmp/out"` | `sh -c python` com o resto como posicionais: **`python` sem argumento**, e o `>` redirecionando no HOST, não no container |
| contador de processos com `case "$c" in *es_backfill*)` | o próprio contador tem `es_backfill` no `cmdline` e **se conta** — nunca devolve 0 |

**Nenhuma falhou com erro.** Todas devolveram vazio ou zero e o script seguiu —
teto que vira corte mudo (regra nº 2), agora na ferramenta de medição.

✅ Rode o orquestrador **no host onde `docker exec` é local**; some uma camada
inteira de quoting.
✅ Se o script tem de ficar do lado de cá, coloque-o **dentro do container/host**
(`scp` + `docker cp`) e chame por caminho, sem argumentos com espaço.
✅ Padrão de busca de processo com classe de caractere (`es_backfil[l]`) para o
contador não se contar.
❌ Nunca conclua "está parado" de um contador que devolveu vazio.

## Freio por latência: meça também a FALHA, não só o relógio

Todo trabalho pesado que divide recurso com o caminho da requisição (backfill de
índice, reindex, varredura, migração de dados) precisa de um freio que mede a
saúde do que ele pode atrapalhar e cede vazão sozinho. O erro de projeto que
essa família de freios convida:

> **Busca que aborta responde RÁPIDO.**

Um freio que olha só latência lê 200 ms e conclui "está ótimo" **exatamente
enquanto o usuário não recebe resultado nenhum** — porque o caminho real
levantou o timeout dele e devolveu erro em 200 ms em vez de resultado em 14 s.
Um freio de segurança cego para a falha que deveria detectar é pior do que não
ter freio: ele dá licença para acelerar.

Medido em 24/08/2026, o caso concreto: a busca de conteúdo tem DOIS caminhos com
tolerâncias diferentes — `busca_api.buscar_movimentacoes` cai no `ES_TIMEOUT` do
cliente (30 s) e devolve resultado lento; `busca_api.ids_por_texto` passa
`request_timeout=IDS_TEXTO_TIMEOUT` (**12 s**) e levanta
`BuscaIndisponivelError(demorou=True)`, e a tela mostra "a busca demorou mais
que o limite e foi interrompida". Uma sonda que media só o primeiro descrevia
como "p90 de 14,3 s" o que no segundo é **100% de falha**.

✅ A sonda chama **as mesmas funções que a tela chama**, uma por caminho, com os
mesmos `request_timeout`. Sonda com timeout mais folgado que a produção mede uma
experiência que ninguém tem.

✅ Onde o caminho ABORTA, o limiar é **taxa de aborto**, não percentil de
latência. Onde ele só fica lento, é percentil.

✅ Limiar **relativo à baseline medida no início da corrida**, com piso E teto.
Só relativo não serve num cluster I/O-bound (a MESMA busca mediu 83,9 ms quente
e 10.116,5 ms fria): 4x de uma baseline de 7,1 s daria 28 s, praticamente o
timeout do cliente — freio calibrado ali só age depois que a busca morreu. Só
absoluto também não serve: ou freia sempre ou não freia nunca.

✅ Termos/parâmetros **rotacionados**. A mesma consulta repetida fica quente e a
sonda passa a medir o page cache, não a latência.

✅ Decisão pela **mediana de N sondas**, não por uma. Freio que dispara por acaso
é desligado pelo primeiro operador que o vê — o que é pior do que não tê-lo.

❌ Comparar uma janela ANTES com uma janela DURANTE quando a carga de terceiros
varia no tempo: a diferença pode ser a deriva deles. **Alterne A/B/A/B** e
compare os agregados (deriva lenta se cancela), ou caracterize a carga de cada
janela (taxa de indexação, profundidade de fila) e reporte junto.

Implementação de referência: `search/backfill_processos.py::sondar` / `Freio`,
com os números medidos em [`SEARCH_SCHEMA.md`](SEARCH_SCHEMA.md).

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

## Comentário em template: `{% comment %}` quando passa de 1 linha

O lexer do Django (`django/template/base.py`) usa `({%.*?%}|{{.*?}}|{#.*?#})`
**sem `re.DOTALL`**. Consequência: `{# ... #}` que atravessa linhas **não é
reconhecido como comentário e VAZA LITERAL na tela**.

```django
{# ok: uma linha só #}

{# ERRADO: isto sai
   como texto pro usuário #}

{% comment %}
Certo: várias linhas, quantas quiser.
{% endcomment %}
```

Aconteceu em prod (12/08/2026): a página do Mapa Comercial exibia 3 desses,
incluindo uma nota técnica sobre a altura do gráfico logo acima do mapa. A
armadilha é sutil — o editor colore como comentário e só vaza em runtime.
Guarda automática: `tests/test_templates_comentarios.py` (varre todo `*.html`,
com controle positivo pra não passar calado se o detector quebrar).
