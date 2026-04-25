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
| `/dashboard/partes/<pk>/` | `parte_detail.html` | Perfil + lista de processos da parte |
| `/dashboard/ingestao/` | `ingestao.html` | Saúde operacional (proxies, drift, runs) |
| `/dashboard/login/` | `login.html` | Patch + wordmark + telemetry strip + SOL counter |
| 404/500/403/400 | `<code>.html` | Error pages temáticas com `error-code` gigante |

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

`g h` → home, `g p` → processos, `g m` → movimentações, `g i` → ingestão, `/` → busca, `t` → toggle tema, `?` → modal de ajuda. Implementação em `base.html` (event listener com flag `pendingG`).

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
