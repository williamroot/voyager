# Dashboard

Server-rendered (Django templates). Sem framework SPA. Stack: HTMX 2 + Alpine.js 3 + Tailwind CSS (CDN) + Apache ECharts (CDN). CSS de identidade em `dashboard/static/dashboard/voyager-identity.css`.

## Identidade visual — "Voyager / Mission Control"

Inspirada no programa espacial Voyager (NASA, 1977).

**Tipografia:**
- `Major Mono Display` — wordmark VOYAGER, error codes
- `JetBrains Mono` — telemetria, timestamps, números
- `Manrope` — corpo, UI

**Paleta (tokens CSS):**
- Base: `--c-base`, `--c-surface`, `--c-card`, `--c-muted`, `--c-border`, `--c-fg`, `--c-fg-muted`, `--c-fg-subtle`
- Semânticos: `--c-accent` (emerald), `--c-danger` (rose), `--c-warning` (amber), `--c-info` (sky)
- Voyager: `--c-mission` (NASA orange), `--c-pulsar` (phosphor green), `--c-golden` (Golden Record), `--c-pale-blue` (Pale Blue Dot)

Tema dark/light via `data-theme` no `<html>` + `tailwind.config.darkMode = 'class'`. Toggle persiste em `localStorage`. Tema claro inspirado no Falcon (slate-based, sombras sutis em vez de bordas fortes).

**Elementos visuais:**
- `.star-field` (10 layers de estrelas com deriva 220s)
- `.grain` (ruído fractal SVG sutil em overlay)
- `.scanlines` (CRT)
- `.signal-noise` (faixa animada)
- `.frame-brackets` (colchetes mission-control)
- `.pulsar` keyframes (bullet verde piscando)
- `.brand-wordmark` (VOYAGER com glow orange no dark)
- `.mission-tag` (pill orange uppercase)
- `.btn-mission` (CTA com aura orange)
- `.error-code` (display gigante com gradient)

## Páginas

| URL | Arquivo | Descrição |
|---|---|---|
| `/dashboard/` | `overview.html` | KPIs + 6 charts + filtros globais (período + tribunais) |
| `/dashboard/processos/` | `processos.html` | Tabela filtrada |
| `/dashboard/processos/<pk>/` | `processo_detail.html` | Hero card + cards de polos + timeline + botão enriquecer |
| `/dashboard/movimentacoes/` | `movimentacoes.html` | Cards com filter chips (tribunal/tipo/meio/classe/ativo) |
| `/dashboard/partes/` | `partes.html` | Tabela com filter chips (tipo) + busca |
| `/dashboard/partes/<pk>/` | `parte_detail.html` | Perfil + 3 charts (tribunal/papel/polo) + lista filtrada |
| `/dashboard/tribunais/` | `tribunais.html` | Cards por tribunal: processos, movs, cobertura, status backfill, contagens de enriquecimento |
| `/dashboard/tribunais/<sigla>/` | `tribunal_detail.html` | Detalhe de um tribunal (KPIs + charts) |
| `/dashboard/workers/` | `workers.html` | Filas RQ + workers conectados, auto-refresh HTMX 5s |
| `/dashboard/ingestao/` | `ingestao.html` | Saúde operacional (proxies, drift, runs) |
| `/dashboard/ingestao/saude/` | `ingestao_saude.html` | Dashboard de saúde do pipeline — KPI strip + heatmap tribunal×fonte×dia + gráfico temporal |
| `/dashboard/leads/` | `leads.html` | Pipeline de leads (Precatório/Pré/Direito Creditório) — KPIs + charts lazy + tabela paginada + export CSV |
| `/dashboard/leads/visibilidade/` | `leads/visibilidade.html` | Observabilidade do classificador — 8 KPIs + 5 charts (histograma de score, calibração por tribunal, funil, top FN, shadow status) + heatmap tribunal × ano CNJ. Requer `can_view_validacao_dashboard` |
| `/dashboard/leads/validacao/` | `leads/validacao_overview.html` | Lista de lotes ativos do usuário; botão criar lote (precisa `can_publish_model`) |
| `/dashboard/leads/validacao/<id>/` | `leads/validacao_lote.html` | Fila de anotação 1-por-vez com hotkeys (HTMX swap entre itens) |
| `/dashboard/leads/validacao/<id>/concluido/` | `leads/_partials/_lote_concluido.html` | Sumário pós-finalização do lote |
| `/dashboard/api/` | `api_docs.html` | Docs da API de leads + cards de stats por nível + clientes ativos + métricas do modelo |
| `/dashboard/consulta-rapida/` | `consulta_rapida.html` | Debug em tempo real: consulta CNJ no DJEN+Datajud, mostra raw + parsed sem persistir |
| `/dashboard/invites/` | `accounts/invites_list.html` | **Superuser**: gerar/revogar convites de cadastro |
| `/invite/<token>/` | `accounts/accept_invite.html` | **Público**: aceitar convite, criar conta |
| `/dashboard/login/` | `login.html` | Patch + wordmark + telemetry strip + SOL counter |
| 404/500/403/400 | `<code>.html` | Error pages temáticas com `error-code` gigante |

## Página: Saúde do pipeline (`/dashboard/ingestao/saude/`)

View: `ingestao_saude`. URL name: `dashboard:ingestao-saude`.

### O que mostra

**KPI strip (5 cards):**

| KPI | Fonte | Cor alerta |
|---|---|---|
| `ultima_ingestao_djen` | MAX `janela_fim` de IngestionRun success | — |
| `anomalias_24h` | células vermelhas de ontem/hoje no grid | text-danger se > 0 |
| `datajud_lag_dias` | hoje − MAX `data_enriquecimento_datajud` | text-warning se > 3d |
| `classif_lag_dias` | hoje − MAX `classificacao_em` | text-warning se > 3d |
| `dias_ok` | células verdes dos últimos 30d (DJEN) | text-accent-fg |

**Heatmap tribunal × fonte × dia** (`pipeline_saude_grid`):
- Eixos: tribunal (linha) × dia (coluna), um painel por fonte (djen, datajud, pje, classif).
- Cor de cada célula determinada por `_classificar_celula`.

**Gráfico temporal por fonte** (`pipeline_volume_temporal`):
- Stacked bar diário por fonte. Útil pra ver interrupções.

### Regra de cor das células

```
baseline = mediana das últimas 4 ocorrências do mesmo tipo de dia (seg/ter/.../dom)

verde    → contagem ≥ 0.60 × baseline
amarelo  → 0.20 × baseline ≤ contagem < 0.60 × baseline
vermelho → contagem < 0.20 × baseline  (em dia útil com baseline > 0)
cinza    → fim de semana  OU  sem baseline (primeiras semanas de dados)
```

### Fontes dos dados

| Fonte | Como é lido |
|---|---|
| `djen` | Live de `IngestionRun` — `MAX(janela_fim)` por tribunal/dia; anti-double-count de overlap. Chaves: `novas`, `duplicadas`, `encontradas`, `paginas`. **Não está na MV.** |
| `datajud` / `pje` / `classif` | MV `mv_pipeline_diario` — formato long: `SELECT tribunal_id, dia, fonte, processos FROM mv_pipeline_diario WHERE fonte = '<fonte>'`. Coluna de valor: `processos` (int). |

### Limitação conhecida

Feriado forense (Corpus Christi, feriado estadual, recesso) não está em nenhum
calendário — qualquer dia útil com volume zero vira **vermelho** mesmo que seja
esperado. Falso-positivo aceito (fora de escopo desta entrega). Ao ver vermelho
num feriado conhecido, ignore ou filtre manualmente por tribunal.

## Componentes (`dashboard/templates/dashboard/_partials/`)

| Componente | Uso |
|---|---|
| `page_header.html` | Título + subtitle + actions |
| `section_header.html` | h2 com subtitle |
| `period_picker.html` | Tabs 7d/30d/90d/365d/Todo |
| `empty_state.html` | Estado vazio padronizado |
| `kpi.html` | Card de KPI |
| `badge.html` | Badge com variantes (accent/danger/warning/info/neutral) |
| `chip.html` | Chip de filtro com active/mini |
| `search_box.html` | Input com ícone |
| `stat_pill.html` | Pill compacto |
| `filter_bar.html` | Wrapper de chips |
| `modal.html` | Modal Alpine com dispatch global |
| `toast_container.html` | Container global de toasts |
| `dropdown.html` | Menu Alpine click.outside |
| `_parte_row.html` | Linha de parte em card de polo (com indent pra advogados) |
| `_chart_card.html` | Card padronizado de chart com header + skeleton + lazy-load |
| `_validacao_card.html` | Card de item de validação (CNJ, score, features, decision buttons) |
| `_score_breakdown.html` | Detalhamento das top features (positivas e negativas) com `bar_pct` |
| `leads/_partials/_validacao_card.html` | Wrapper específico do dashboard de validação |
| `leads/_partials/_lote_concluido.html` | Sumário do lote |

## Filtros globais

Todas as queries do dashboard aceitam `dias` + `tribunais` (CSV). Implementadas em `dashboard/queries.py::_aplicar_filtros`. Aplicado em:

- `kpis_globais` (24h sempre 24h reais; resto respeita período)
- `volume_temporal` (auto-bucket: TruncDate ≤365d, TruncMonth se "todo período")
- `distribuicao_por_tribunal`, `distribuicao_por_meio`
- `top_tipos_comunicacao`, `top_classes`, `top_orgaos`
- `sparkline_24h` (só tribunais, período não aplica)

`_periodo_dias(request, default=90) → int|None`:
- `?dias=all` ou `?dias=0` ou ausente sob backfill em curso → `None`
- Senão `min(max(int, 1), 3650)`
- Banner amarelo no `overview.html` quando `_backfill_em_curso() is True` informando cobertura atual

## Charts

`base.html` define helpers globais (`buildVolumeChart`, `buildDonut`, `buildHorizBar`, `buildSparkline`) que respeitam tema (via `chartGridColors()`).

Pattern em cada chart:
```html
{{ data|json_script:"data-x" }}    <!-- HTML-safe -->
<div class="h-72 chart-cell">
  <div class="chart-skeleton">
    <div class="pulsar-mark">acquiring signal</div>
  </div>
  <div data-echart='{}' x-init="setupChart($el, buildXxx(jsonData('data-x')))" class="absolute inset-0"></div>
</div>
```

`setupChart`:
1. `el.dataset.echart = JSON.stringify(opts)`
2. Inicializa ECharts (com tema baseado em `html.classList.contains('dark')`)
3. Remove o skeleton irmão dentro de `.chart-cell`

Em troca de tema: `initAllCharts()` re-renderiza tudo (palette adaptável).

## Atalhos de teclado

Globais (em `base.html`): `g h` → home, `g p` → processos, `g m` → movimentações, `g i` → ingestão, `/` → busca, `t` → toggle tema, `?` → modal de ajuda. Listener com flag `pendingG`.

Fila de validação (`/dashboard/leads/validacao/<id>/`, em `dashboard/static/dashboard/validacao_hotkeys.js`):

| Tecla | Ação |
|---|---|
| `1` | É Precatório (N1) |
| `2` | É Pré-precatório (N2) |
| `3` | É Direito Creditório (N3) |
| `4` | Não é lead |
| `I` | Incerto |
| `E` | Precisa enriquecer |
| `S` | Skip (pula) |
| `J` / `K` | Próximo / anterior item (sem salvar) |
| `Ctrl+Z` | Desfazer última anotação |
| `?` | Mostrar mapa de atalhos |

## Custom template tags e filters (`voyager_extras.py`)

Além dos já existentes (`type_classes`, `format_cnj`, etc), as Waves 0-5 adicionaram:

| Nome | Tipo | Função |
|---|---|---|
| `motivo_visivel` | simple_tag | Wrapper de `ProcessoValidacao.motivo_visivel_para(user)` — retorna o motivo só se user tem direito |
| `nivel_suspeita` | filter | Mapeia `suspeita_score` → label/cor (baixa/média/alta) pra badge no card de validação |
| `absval` | filter | `abs(value)` — usado em barras de contribuição negativa |
| `bar_pct` | simple_tag | `{% bar_pct contrib max_abs as pct %}` — produz % de largura relativa pra barra de feature breakdown |

## Mobile

Sidebar vira drawer fixed + backdrop. Top bar com hamburger + tema. Breakpoint `md:` (768px).

## Como criar uma página nova

1. View em `dashboard/views.py` (com `@login_required @require_GET`)
2. URL em `dashboard/urls.py`
3. Template:
   ```html
   {% extends 'dashboard/base.html' %}
   {% load voyager_extras %}
   {% block title %}Minha Página{% endblock %}
   {% block content %}
   {% include 'dashboard/_partials/page_header.html' with title='X' %}
   <div class="card">conteúdo</div>
   {% endblock %}
   ```
4. Adicionar entrada na sidebar (`base.html`, bloco `nav-item`)

Sempre usar tokens semânticos (`bg-card`, `text-fg`, `border-border`) em vez de cores literais (`bg-zinc-900`, etc.).

## Padrão obrigatório: listagens com lazy load + paginação HTMX

**Toda listagem grande** (`processos`, `partes`, `movimentacoes` e novas) segue este padrão. Nunca renderize a lista server-side junto com o resto da página.

### Pipeline

```
GET /dashboard/<lista>/                  ← shell, sem queryset (instantâneo)
  └→ HTML inclui <div id="xxx-list"
                   hx-get="?..."
                   hx-trigger="load"
                   hx-swap="outerHTML">
       [overlay 'acquiring data']
     </div>

  ↓ on load (HTMX dispara automaticamente)

GET /dashboard/<lista>/?... (HX-Request: true)
  └→ partial _xxx_list.html (queryset + paginação)
     swap outerHTML substitui o shell

GET /dashboard/<lista>/?page=2 (HX-Request: true)
  └→ mesmo partial, página 2
```

### View

A view detecta `HX-Request` e bifurca:
- **Sem HX-Request**: monta `base_ctx` com filtros (chips, valores selecionados) e retorna a página shell. **Sem rodar queryset.**
- **Com HX-Request**: aplica filtros, ordenação, paginação (`_paginar`), retorna o partial `_xxx_list.html`.

```python
def minha_lista(request):
    # parse de filtros (sem queryset ainda)
    base_ctx = {'tribunal_filtro': ..., 'q': ...}

    if not _is_htmx(request):
        return render(request, 'dashboard/minha_lista.html', base_ctx)

    qs = MeuModel.objects.filter(...)  # só agora
    page = _paginar(qs, request, default_size=50)
    return render(request, 'dashboard/_partials/_minha_lista_list.html', {
        **base_ctx,
        'page': page,
        'items': page.object_list,
    })
```

### Templates

**Shell** (`dashboard/minha_lista.html`):
```django
{% extends 'dashboard/base.html' %}
{% load voyager_extras %}
{% block content %}
  {# header + filtros + chips ficam aqui, server-side #}
  {% include 'dashboard/_partials/_list_shell.html' with id='minha-lista-list' %}
{% endblock %}
```

**Partial** (`dashboard/_partials/_minha_lista_list.html`):
```django
{% load voyager_extras %}
<div id="minha-lista-list" class="card overflow-hidden p-0">
  <table>...</table>
  {% include 'dashboard/_partials/pagination.html' with page=page target='#minha-lista-list' %}
</div>
```

### Convenções

- ID do container = `<nome>-list` (sufixo obrigatório — JS hooks de loading detectam por `[id$="-list"]`)
- Partial em `_partials/_<nome>_list.html`
- Helpers `_paginar(qs, request, default_size=50)` e `_is_htmx(request)` em `views.py`
- `_partials/_list_shell.html` aceita parâmetro `id` e dispara `hx-get` no `load`
- `_partials/pagination.html` recebe `page` (Django Paginator Page) e `target` (selector CSS)
- Loading overlay automático em qualquer `[id$="-list"]` durante swap (CSS + hooks em `base.html::htmx:beforeRequest`)

### Por quê

- TTFB <300ms na navegação (sidebar → lista) — sensação de SPA
- Filtros chips renderizam instantâneo, dados vêm depois
- Paginação não recarrega filtros nem charts
- Falha no queryset não derruba a página inteira — overlay de erro localizado
- URL bookmark-friendly: `?page=5&tribunal=TRF1` continua funcionando direto
