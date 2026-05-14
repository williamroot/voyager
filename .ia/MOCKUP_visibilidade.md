# MOCKUP — `/dashboard/leads/visibilidade/`

Página de **observabilidade do pipeline de leads** — substitui/expande `/dashboard/leads/`.
Stack: Django templates + HTMX + Alpine + ECharts (lazy). Identidade "telemetry station".

---

## 1. ASCII mockup — desktop (>= lg, 90 chars)

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ VOYAGER · LEADS · VISIBILIDADE                       [v6 · AUC 0.961]  04:32:11 UTC    │
│ Observabilidade do pipeline · classificação + validação                                │
│                                                  [API docs] [exportar CSV] [novo lote] │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Tribunal: [Todos][TRF1][TRF3][TJSP][TJMG][...]      Periodo: [7d][30d·][90d][1a][Tudo] │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│  ┌─KPI 1─────────┐ ┌─KPI 2─────────┐ ┌─KPI 3─────────┐ ┌─KPI 4─────────┐               │
│  │ 💎 PRECATORIO │ │ ⏳ PRE-PRECAT │ │ 🌱 DIR.CRED.  │ │ ✅ VALID. 30d │               │
│  │   12.847      │ │    38.221     │ │   126.504     │ │     91,3 %    │               │
│  │ ≈ 6d ate fim  │ │ em homolog.   │ │ watch-list    │ │ treino 93,9%  │               │
│  └───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘               │
│  ┌─KPI 5─────────┐ ┌─KPI 6─────────┐ ┌─KPI 7 NOVO────┐ ┌─KPI 8 NOVO────┐               │
│  │ 📥 DESCOB./d  │ │ 📤 CONSUM./d  │ │ 🛰 LOTES ATIV.│ │ 🔭 FN SEMANA  │               │
│  │    2.134      │ │    1.890      │ │      4        │ │      87       │               │
│  │ MM7d  ▲ 12%   │ │ MM7d  ▼ 4%    │ │ ████░░ 62% md │ │ ▲ +34 vs ant. │               │
│  └───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘               │
│                                                                                        │
│ ┌──────────────────────────────────────────┐ ┌──────────────────────────────────────┐ │
│ │ HISTOGRAMA DE SCORE · POR TRIBUNAL       │ │ CALIBRACAO · POR TRIBUNAL            │ │
│ │ curvas overlay (densidade)               │ │ decil score x taxa real (uma p/trib.)│ │
│ │       n                                  │ │  1.0│                          ╱     │ │
│ │       │     ╱TRF1                        │ │     │       TRF1 ●──●──●──●───●      │ │
│ │       │   ╱╲╱╲ TRF3                      │ │  .8 │     ╱ TRF3 ○──○──○──○──○       │ │
│ │       │ ╱     ╲   TJSP                   │ │     │   ╱   diag (ideal)             │ │
│ │       │╱       ╲────                     │ │  .4 │ ╱     TJSP ▲──▲──▲──▲          │ │
│ │       └──────────── score                │ │     │╱                               │ │
│ │       0  .2  .5  .7  1.0                 │ │     └──D1─D2─D3──D4──D5──D6...D10──> │ │
│ │ [acquiring signal · pulsar]              │ │ Vermelho = curva afasta da diagonal  │ │
│ └──────────────────────────────────────────┘ └──────────────────────────────────────┘ │
│                                                                                        │
│ ┌──────────────────────────────────────────┐ ┌──────────────────────────────────────┐ │
│ │ HEATMAP · TRIBUNAL × ANO CNJ (gap)       │ │ FUNIL AMPLIADO                       │ │
│ │       2018 2019 2020 2021 2022 2023 2024 │ │ ┌───────────────────────────────┐    │ │
│ │ TRF1  ███  ███  ███  ███  ███  ███  ██   │ │ │ 1. descobertos      842.117   │    │ │
│ │ TRF3  ███  ███  ███  ▓▓▓  ███  ███  ██   │ │ │ 2. enviados (API)   210.443   │    │ │
│ │ TJSP  ░░░  ███  ███  ███  ░░░  ▒▒▒  ██   │ │ │ 3. consumidos       198.022   │    │ │
│ │ TJMG  ░░░  ░░░  ███  ███  ░░░  ███  ██   │ │ │ 4. validados        180.731   │    │ │
│ │ TJRJ  ░░░  ░░░  ░░░  ▒▒▒  ███  ███  ██   │ │ │ 5. FN recuperados       217   │    │ │
│ │   ░ vazio  ▒ baixo  ▓ medio  █ ok        │ │ └───────────────────────────────┘    │ │
│ │ Buracos vermelhos = re-ingerir / backfill│ │ taxa fim-a-fim · 21,5%               │ │
│ └──────────────────────────────────────────┘ └──────────────────────────────────────┘ │
│                                                                                        │
│ ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│ │ DISTRIBUICAO DE SCORE (Precatorio · agregada)                          secundario│  │
│ │ ▁▁▂▂▃▄▆█▇▆▅▄▃▃▂▂▂▂▁▁                                                              │  │
│ │ 0.0                            0.5                            1.0                 │  │
│ └──────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                        │
│ ┌──── LOTES DE VALIDACAO ATIVOS ─────────────────────────────────┐ ┌── Top 10 FN ──┐  │
│ │ ● top-score-trf1     TRF1   ███████████░  78%   ~32s/item  →  │ │ CNJ      score│  │
│ │ ● score-medio-trf3   TRF3   █████░░░░░░░  41%   ~58s/item  →  │ │ 1234... 0.91 ⚠│  │
│ │ ● gap-tjsp-2021      TJSP   ██░░░░░░░░░░  15%   ~71s/item  →  │ │ 5678... 0.88 ⚠│  │
│ │ ● fn-suspeitos-w19   ALL    █████████░░░  72%   ~22s/item  →  │ │ 9012... 0.85 ⚠│  │
│ │                                                                │ │ ... (+7)      │  │
│ │ [+ ver todos · 12]            [+ criar novo lote]              │ │ [ver todos →] │  │
│ └────────────────────────────────────────────────────────────────┘ └───────────────┘  │
│                                                                                        │
│ ┌── TOP 10 FN SUSPEITOS · 7d ─────────────────────────────────────────────────────┐   │
│ │ # │ CNJ                       │ Trib │ score │ susp │ motivos                     │ │
│ │ 1 │ 0001234-56.2023.4.01.3400 │ TRF1 │ 0.91  │ 0.84 │ [precat-text][volMov-alto]  │ │
│ │ 2 │ 0005678-90.2023.4.03.6100 │ TRF3 │ 0.88  │ 0.81 │ [cumprim][envTrib]          │ │
│ │ 3 │ 1009012-34.2022.8.26.0100 │ TJSP │ 0.85  │ 0.78 │ [cumprim][rpv-text]         │ │
│ │ ...                                                                                │ │
│ └────────────────────────────────────────────────────────────────────────────────────┘│
│                                                                                        │
│ ░ ░ ░ scanlines + grain + star-field (background)                                      │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

## 1.b ASCII mockup — mobile (< md, 1 coluna)

```
┌──────────────────────────────┐
│ VOYAGER · LEADS · VISIB.     │
│ [☰][🌓]            04:32 UTC │
├──────────────────────────────┤
│ tribunal ▼  período ▼        │
├──────────────────────────────┤
│ ┌── KPI swipe (snap) ──────┐ │
│ │ 💎 PREC  12.847  ≈6d ←/→ │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ HIST SCORE × TRIBUNAL    │ │
│ │ (collapse · tap p/abrir) │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ CALIBRACAO POR TRIB      │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ HEATMAP TRIB × ANO       │ │
│ │ (scroll horizontal)      │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ FUNIL                    │ │
│ └──────────────────────────┘ │
│ ┌── lotes ativos ──────────┐ │
│ │ ● top-score-trf1  78%  → │ │
│ │ ● score-medio-trf3 41% → │ │
│ └──────────────────────────┘ │
│ ┌── FN suspeitos ──────────┐ │
│ │ 0001234... TRF1 0.91 ⚠ → │ │
│ │ 0005678... TRF3 0.88 ⚠ → │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘
```

---

## 2. Componentes

| Componente                          | Partial existente?              | Partial novo?                          | Endpoint backend                                                      | Loading                                | Erro                                   |
|-------------------------------------|---------------------------------|----------------------------------------|-----------------------------------------------------------------------|----------------------------------------|----------------------------------------|
| Page header + mission tag           | `page_header.html` (adaptar)    | —                                      | render server-side                                                    | n/a                                    | n/a                                    |
| Filter bar (tribunal chips)         | `filter_bar.html` + `chip.html` | —                                      | server-side (`tribunais` no contexto)                                 | n/a                                    | n/a                                    |
| Period picker                       | `period_picker.html`            | —                                      | server-side                                                           | n/a                                    | n/a                                    |
| KPI 1-6 (atuais)                    | `kpi.html` (variante inline)    | —                                      | `GET /dashboard/leads/chart/kpis/` (Alpine `leadsKpis()`)             | `—` placeholder + `kpi-num` skeleton   | `SIGNAL LOST · KPIs`                   |
| KPI 7 "Lotes ativos"                | —                               | `_partials/_kpi_lotes_ativos.html`     | `GET /dashboard/leads/visibilidade/kpi/lotes/`                        | `—` + barra cinza                      | `SIGNAL LOST`                          |
| KPI 8 "FN semana"                   | —                               | `_partials/_kpi_fn_semana.html`        | `GET /dashboard/leads/visibilidade/kpi/fn-semana/`                    | `—` + delta cinza                      | `SIGNAL LOST`                          |
| Chart: histograma score × tribunal  | —                               | `_partials/_chart_card.html` (genérico)| `GET /dashboard/leads/visibilidade/chart/score-hist-tribunal/`        | `chart-skeleton` + `acquiring signal`  | `SIGNAL LOST · retry` (pulsar vermelho)|
| Chart: calibração × tribunal        | —                               | reusa `_chart_card.html`               | `GET /dashboard/leads/visibilidade/chart/calibracao-tribunal/`        | idem                                   | idem                                   |
| Chart: heatmap tribunal × ano CNJ   | —                               | reusa `_chart_card.html`               | `GET /dashboard/leads/visibilidade/chart/heatmap-gap/`                | idem                                   | idem                                   |
| Chart: funil ampliado (5 passos)    | —                               | reusa `_chart_card.html`               | `GET /dashboard/leads/visibilidade/chart/funil-ampliado/`             | idem                                   | idem                                   |
| Chart: hist. score agregado (mini)  | —                               | reusa `_chart_card.html`               | `GET /dashboard/leads/chart/distribuicao-score/` (já existe)          | idem                                   | idem                                   |
| Card "Lotes ativos" (lista 5)       | —                               | `_partials/_lotes_ativos_lista.html`   | `GET /dashboard/leads/visibilidade/lotes-ativos/` (HTMX `hx-trigger=load`) | `acquiring data` overlay         | `empty_state.html` com title=SIGNAL LOST|
| Tabela "Top 10 FN suspeitos"        | reusa padrão `_list_shell.html` | `_partials/_fn_suspeitos_list.html`    | `GET /dashboard/leads/visibilidade/fn-suspeitos/`                     | skeleton 10 linhas                     | empty_state                            |
| Chip de motivo (FN)                 | `chip.html` (variant `mini`)    | —                                      | inline                                                                | n/a                                    | n/a                                    |
| Banner Juriscope inativo            | existente em `leads.html`       | —                                      | reusa fetch `kpis_globais`                                            | hidden                                 | hidden                                 |

Endpoints novos a expor em `dashboard/urls.py` (nomeados):

- `dashboard:leads-vis-kpi-lotes`
- `dashboard:leads-vis-kpi-fn-semana`
- `dashboard:leads-vis-chart` (key: `score-hist-tribunal` | `calibracao-tribunal` | `heatmap-gap` | `funil-ampliado`)
- `dashboard:leads-vis-lotes-ativos`
- `dashboard:leads-vis-fn-suspeitos`

Todos respondem JSON `{ data: ... }` (wrapper já obrigatório pra `lazyChart`).

---

## 3. Estados

### Loading global
- Página shell renderiza instantâneo (TTFB <300ms). KPIs e charts ficam com `—` / `chart-skeleton` exibindo `pulsar-mark` "acquiring signal" (já no CSS).
- Lotes e FN suspeitos: overlay `[id$="-list"]` automático (hook em `base.html`).

### Empty state
- **Nenhum lote ativo**: card mostra `empty_state.html` com `title="Nenhum lote ativo"`, `subtitle="Crie um lote pra começar a validar"` + CTA `[+ criar novo lote]` (btn-mission).
- **Nenhum FN suspeito esta semana**: card mostra `title="Sem suspeitos novos"`, `subtitle="Modelo e ground-truth estão alinhados — boa!"`.
- **Calibração sem dados (Juriscope nunca consumiu)**: chart mostra texto centralizado "sem dados ainda — Juriscope ainda não consumiu nenhum lead" (já existe em `buildCalibration`, replicar por tribunal).
- **Heatmap sem dados**: célula cinza `--c-muted` com `░` ascii inline.

### Error state (HTMX falhou)
- Convenção Voyager: `SIGNAL LOST · <componente>` em `error-code` pequeno (`text-fg-muted`), pulsar vermelho (`--c-danger`), botão `[retry]` que dispara `htmx.trigger('this', 'load')`.
- Charts: skeleton vira mensagem `SIGNAL LOST · retry` clicável (substitui `acquiring signal`).
- KPI: número fica `—` e label ganha badge `danger` mini com tooltip do erro.

### Sem permissão (`can_view_validacao_dashboard` ausente)
- View redireciona pra `/dashboard/leads/` (versão pública) **OU** renderiza página completa com seção "Lotes" / "FN suspeitos" / "Heatmap-gap" substituídas por card único:

```
┌────────────────────────────────────────────────────────────────┐
│  403 · ACCESS DENIED                                           │
│                                                                │
│  Esta visão requer permissao `can_view_validacao_dashboard`.   │
│  Solicite ao administrador.                                    │
│                                                                │
│  Você ainda pode ver: KPIs · histogramas · calibração          │
└────────────────────────────────────────────────────────────────┘
```

Recomendado: **renderizar partial mas omitir blocos restritos** (graceful degradation, mantém valor analítico). Charts continuam pra todos; lotes e FN ficam atrás da permissão.

---

## 4. Especificação visual

### Cores (tokens CSS Voyager)

| Elemento                                         | Token              | Cor (referência)          |
|--------------------------------------------------|--------------------|---------------------------|
| Wordmark "VOYAGER · LEADS · VISIBILIDADE"        | `--c-mission`      | NASA orange (glow no dark)|
| Mission tag `v6 · AUC 0.961`                     | `--c-mission`      | NASA orange pill          |
| Bullet de lote ativo (`●`)                       | `--c-pulsar`       | phosphor green pulsar     |
| Barra de progresso de lote (preenchida)          | `--c-pulsar`       | phosphor green            |
| Barra de progresso de lote (trilho)              | `--c-border`       | cinza neutro              |
| Linha "ideal" (diagonal) na calibração           | `--c-fg-subtle`    | cinza tracejado           |
| Curva de calibração — TRF1                       | `--c-pulsar`       | green                     |
| Curva de calibração — TRF3                       | `--c-pale-blue`    | Pale Blue Dot             |
| Curva de calibração — TJSP                       | `--c-golden`       | Golden Record yellow      |
| Curva de calibração — TJMG                       | `--c-mission`      | NASA orange               |
| Curva de calibração — outros                     | `--c-fg-muted`     | cinza                     |
| Heatmap: ok (≥80% cobertura)                     | `--c-pulsar`/40    | green translúcido         |
| Heatmap: médio (40-80%)                          | `--c-golden`/40    | amber                     |
| Heatmap: baixo (10-40%)                          | `--c-warning`/40   | amber escuro              |
| Heatmap: vazio (<10%) — **gap**                  | `--c-danger`/30    | vermelho                  |
| Funil — passo "validados"                        | `--c-pulsar`       | green                     |
| Funil — passo "FN recuperados"                   | `--c-mission`      | NASA orange (destaque)    |
| KPI delta positivo (▲)                           | `--c-accent`       | emerald                   |
| KPI delta negativo (▼)                           | `--c-danger`       | rose                      |
| Chip de motivo (FN)                              | `chip` neutro      | bg-muted                  |
| FN suspeito ⚠                                     | `--c-warning`      | amber                     |
| Background canvas                                | `.star-field` + `.grain` | dark base           |
| Faixa decorativa (header)                        | `.signal-noise`    | NASA orange animada       |

### Tipografia

| Onde                                 | Família               | Tamanho/peso                |
|--------------------------------------|-----------------------|-----------------------------|
| Wordmark do header                   | `Major Mono Display`  | text-sm uppercase tracking-widest |
| Subtitle do header                   | `Manrope`             | text-xs text-fg-subtle      |
| Timestamp UTC + counters             | `JetBrains Mono`      | text-xs tabular-nums        |
| KPI label (uppercase)                | `Manrope`             | text-[0.7rem] tracking-wider|
| KPI valor                            | `JetBrains Mono`      | text-2xl/3xl font-semibold (kpi-num) |
| Score / probabilidade na tabela FN   | `JetBrains Mono`      | text-sm tabular-nums        |
| CNJ na tabela                        | `JetBrains Mono`      | text-xs                     |
| Motivos (chips)                      | `Manrope`             | text-[10px] uppercase       |
| Eixo X/Y dos charts                  | `JetBrains Mono`      | 11px                        |
| Mensagens "acquiring signal" / "SIGNAL LOST" | `JetBrains Mono` | text-xs uppercase       |
| Corpo / descrições / tooltips        | `Manrope`             | text-xs/sm                  |

### Espaçamento + hierarquia

- Container: `max-w-7xl mx-auto px-4 md:px-6`
- Gap entre cards: `gap-3` (KPIs) e `gap-4` (charts)
- KPIs: `grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8` (8 cards) — em lg cabem 8 lado a lado; em md vira 2 fileiras de 4
- Charts top: 2×2 (`grid-cols-1 lg:grid-cols-2`)
- "Lotes ativos" + "Top 10 FN preview" lado a lado: `grid-cols-1 lg:grid-cols-3` com lotes em `col-span-2`
- Tabela FN full-bleed: `col-span-full`
- Padding interno cards: `card` (p-5) / `card-tight` (p-3)
- Hierarquia: header → filtros → KPIs (8) → charts analíticos (4 + 1 secundário) → operacional (lotes + FN preview) → tabela FN

---

## 5. Partials a criar / reusar

### Criar
- `_partials/_kpi_lotes_ativos.html` — KPI com barra de progresso média (Alpine fetch endpoint dedicado)
- `_partials/_kpi_fn_semana.html` — KPI com delta vs semana anterior (▲/▼ + %)
- `_partials/_chart_card.html` — wrapper genérico de chart (title, subtitle, h-72, skeleton, slot `data-echart`, error fallback)
  - parâmetros: `title`, `subtitle`, `url`, `builder` (nome JS), `height` (default `h-72`)
- `_partials/_lotes_ativos_lista.html` — lista de até 5 lotes com `●` pulsar, nome, tribunal, barra, tempo médio, `→`
- `_partials/_lote_row.html` — uma linha de lote (reusada em "ver todos")
- `_partials/_fn_suspeitos_list.html` — tabela compacta (10 linhas, sem paginação aqui; "ver todos" leva pra página dedicada)
- `_partials/_fn_row.html` — linha com CNJ (mono), tribunal badge, score/susp, chips de motivos

### Reusar (sem modificar)
- `page_header.html` (com slot `actions`)
- `period_picker.html`
- `filter_bar.html` + `chip.html` (chips de tribunal)
- `kpi.html` (KPIs 1–6)
- `badge.html` (níveis N1/N2/N3, tribunal)
- `empty_state.html`
- `_list_shell.html` (padrão de listagem HTMX se "ver todos FN" tiver paginação)
- `pagination.html`

---

## 6. Interações

### Clicks principais

| Elemento clicado                       | Ação                                                                 |
|----------------------------------------|----------------------------------------------------------------------|
| Card de lote ativo (linha inteira)     | `→ /dashboard/leads/validacao/<lote_id>/` (full page)                |
| Botão `[+ criar novo lote]`            | abre `modal.html` com formulário (estratégia + tribunal + tamanho)   |
| Linha de FN suspeito (CNJ)             | `→ /dashboard/processos/<cnj>/` (página existente, abre badge ML)    |
| Chip de motivo na linha FN             | filtra tabela por motivo (query string `?motivo=precat-text`)        |
| Célula vermelha no heatmap (gap)       | abre modal: "Reingerir TJSP/2021? (X mil processos)" + CTA           |
| Curva no histograma de score (legend)  | toggle visibility (ECharts nativo)                                   |
| Card de chart                          | hover mostra `[ⓘ]` tooltip explicando o que está sendo medido         |
| Mission tag `v6 · AUC 0.961`           | `→ /dashboard/api/` (métricas do modelo)                             |
| Botão `[exportar CSV]`                 | reusa `dashboard:leads-export` com filtros atuais                     |
| Botão `[API docs]`                     | `→ /dashboard/api/`                                                  |

### Atalhos de teclado (opcional, opt-in)

| Atalho        | Ação                                                  |
|---------------|-------------------------------------------------------|
| `g v`         | abre `/dashboard/leads/visibilidade/` (consistência com `g h/p/m/i`) |
| `r`           | refresh KPIs (Alpine `load()`) sem recarregar página  |
| `n`           | foca botão "criar novo lote"                          |
| `/`           | foca busca CNJ na tabela FN                           |
| `?`           | abre modal de ajuda (existente, adicionar atalhos novos lá) |

Implementar `g v` em `base.html` (mesmo handler `pendingG`).

### Filtros

- Tribunal e período aplicam a **todos** os componentes (server-side: server pega `?tribunal=X&dias=Y`, propaga via `{% query_string %}` em cada `lazyChart` URL).
- Mudar filtro = full reload (não SPA). Mantém bookmark-friendly.
- Lotes ativos **ignoram filtro de tribunal** (mostra todos) — afinal, lote pode cruzar tribunais.

---

## Notas finais

- Página renderiza shell server-side em <300ms; tudo pesado é lazy via Alpine `fetch` (KPIs) ou `lazyChart` (charts) ou `hx-trigger="load"` (listas).
- Banner "Juriscope inativo" continua aparecendo no topo se `consumidos_total === 0` (reusa lógica existente).
- Identidade Voyager: **sem emojis decorativos** salvo os que já fazem parte da semântica do produto (💎 ⏳ 🌱 ✅ 📥 📤 — já consagrados em `leads.html` e `processo_detail.html`); adicionar **🛰** pra lotes ativos e **🔭** pra FN semana, mantendo a metáfora telemetria/observação.
- Charts seguem padrão `setupChart` + `chartGridColors()` pra respeitar dark/light.
- Mock dimensional: a página tem ~5 dobras em desktop. Aceitar scroll vertical — é dashboard analítico, não tela operacional.
