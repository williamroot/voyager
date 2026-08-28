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

**TODO token de cor tem que estar registrado no `tailwind.config.colors` do
`base.html`** — senão `text-mission` & cia. são classes **mortas** (o Tailwind
não gera nada, falha em silêncio) e o autor acaba caindo em hex inline, que
falha WCAG no tema claro. Registrados hoje: os semânticos + `mission`,
`mission-fg`, `pulsar`, `pulsar-fg`, `golden`, `pale-blue`.

**Par `X` / `X-fg` (contrato de contraste):**

| Use | Onde | Contraste medido (claro / escuro) |
|---|---|---|
| `mission` (orange-600) | ícone, `bg-mission/10`, `border-mission/30`, número ≥24px | 3,56:1 / 5,30:1 — cumpre 3:1 de UI e texto grande, **não** cumpre 4,5:1 |
| `mission-fg` (orange-700 no claro, orange-300 no escuro) | **texto corrido** `<p>/<code>/<span>`/link | 5,18:1 / 11,19:1 |
| `accent` / `accent-fg` | idem (emerald) | — / — |
| `pulsar` (emerald-600/300) | bullet, dot, fundo | 3,77:1 / 13,44:1 |
| `pulsar-fg` (emerald-700/300) | **texto corrido** | 5,48:1 / 13,44:1 |

Regra: **texto < 24px sempre no `-fg`**; a var sem sufixo é cor de marca.
`golden` no tema claro é 2,94:1 (yellow-600) — só use como fundo/borda/ícone,
nunca em texto.

Tema dark/light via `data-theme` no `<html>` + `tailwind.config.darkMode = 'class'`. Toggle persiste em `localStorage`. Tema claro inspirado no Falcon (slate-based, sombras sutis em vez de bordas fortes).

**Ícones (`voy_icon` tag → sprite SVG):**

Sprite em `dashboard/static/dashboard/voyager-icons.svg` — 21 símbolos `<symbol id="voy-{alias}">`, todos derivados do **Lucide v0.460.0** (ISC). Renderiza via `{% voy_icon "name" "tailwind-classes" %}` em qualquer template.

Os aliases preservam o vocabulário espacial; o desenho vem do Lucide. Re-gerar com `python3 scripts/build_sprite.py` (atualiza versão pinada no topo do script).

Mapping atual (42 símbolos):

**Identidade espacial** (preserva vocabulário Voyager):
| alias | lucide source | uso típico |
|---|---|---|
| `telescope` | telescope | hero / branding / favicon |
| `moon` / `sun` | moon / sun | toggle tema |
| `probe` | satellite | sonda / Voyager |
| `pulsar` | radio-tower | leads (pipeline ativo) |
| `constellation` | share-2 | partes / rede |
| `trajectory` | route | navegação / rota |
| `transmission` | radio | broadcasting / ondas |
| `calibrate` | sliders-horizontal | ajustes / saúde |
| `dossier` | file-text | processos |
| `mission-tag` | tag | label mission-control |
| `retrograde` | rotate-ccw | reverter / refazer |
| `anomaly` | triangle-alert | erro / warning |
| `signal-ok` / `signal-lost` | wifi / wifi-off | status conexão |
| `uplink` / `downlink` | arrow-up-to-line / arrow-down-to-line | tráfego |
| `eject` | log-out | sair |
| `arrow` | arrow-right | reservado |
| `clear` | x | fechar |
| `radar` | radar | reservado (varredura) |
| `info` | info | ⓘ trigger de tooltip (`.voy-tip`) |
| `plus` | plus | nova análise / adicionar |

**Badges dos 4 níveis de classificação** (substituíram emojis 💎/⏳/🌱/🚫):
| alias | lucide source | uso |
|---|---|---|
| `gem` | gem | PRECATÓRIO (N1, com `text-mission`) |
| `hourglass` | hourglass | PRÉ-PRECATÓRIO (N2) |
| `sprout` | sprout | DIREITO CREDITÓRIO (N3, com `text-accent-fg`) |
| `ban` | ban | NÃO LEAD (com `text-red-400`) |

**Famílias de features** (`/algoritmo/`):
| alias | lucide source | família |
|---|---|---|
| `scale` | scale | Classe e tipo de mov |
| `scroll-text` | scroll-text | Texto |
| `trending-up` | trending-up | Volume |
| `history` | history | Recência |
| `link-2` | link-2 | Combos |
| `flask` | flask-conical | v7 |

**Features individuais** (cards de explicação):
| alias | lucide source | feature |
|---|---|---|
| `send` | send | F7 envTrib |
| `search` | search | F11/F12 texto |
| `tornado` | tornado | F16 variedade |
| `target` | target | F17 N1count |
| `calendar` | calendar | F18 ano |
| `circle-x` | circle-x | F19 cancelado |
| `circle-check` | circle-check | F20 juriscope |
| `users` | users | F23 partes |
| `sparkles` | sparkles | F24-F28 v7 novas |

Estilo: stroke=currentColor, width=1.6px (Lucide default 2px reduzido pra harmonizar com tipografia), viewBox 24×24. Tailwind colore via `text-*` (`text-mission`/`text-accent-fg`/`text-red-400`), dimensiona via `w-*/h-*`.

`tribunals/explicacao.py` é a fonte única de metadados (label, descrição, família, alias do ícone) — `dashboard/views.py:processo_detail` e `algoritmo()` consomem dela. Os campos `emoji` no `FEATURE_META` guardam o **alias do sprite** (ex.: `'emoji': 'scale'`), não o caractere unicode.

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

## ⚠️ Armadilha: `text-base` aqui é COR, não tamanho

`base` é um token de **cor** registrado no tailwind.config (é o fundo da página),
então `.text-base` gera `color: var(--base)` e **não** `font-size: 1rem`. Num
elemento com duas classes de cor (`class="text-base font-bold text-fg"`) quem
ganha é a ordem no CSS gerado, não a ordem no atributo — medido em browser:
ganha a cor `base`, ou seja, **texto da cor do fundo = invisível** no tema
escuro.

Achado em 12/08/2026 pelo agente da página do estado; mordia 3 templates
(`algoritmo.html`, `showcase_analise.html`, `ingestao.html`). Correção: use
`text-[1rem]` quando quiser tamanho. `text-base` só faz sentido como cor de
texto sobre tinta (`bg-accent text-base`, em `wizard.html`) — e ali é
intencional.

## Tooltip: `.voy-tip` (CSS-only) — **nunca use `title=`**

`title=` nativo é fonte do SO, posição do cursor, ~1s de atraso, sem controle de
largura, **zero em touch** e invisível pro teclado. Use o componente
(`voyager-identity.css`, sem JS, CSP-safe): hover + `:focus-visible` (teclado) +
`:focus-within` (**TAP** no mobile).

```django
<button type="button" class="voy-tip" aria-label="Ajuda: score"
        aria-describedby="tip-score">
  {% voy_icon "info" "w-3.5 h-3.5" %}
  <span class="voy-tip-body" role="tooltip" id="tip-score">
    Média das notas do modelo. <strong>Não</strong> é probabilidade.
  </span>
</button>
```

- Trigger = `<button type="button">` (sem `type`, dentro de `<form>` **submete**).
  `<span>` só com `tabindex="0"`.
- `aria-describedby` (trigger) ⇄ `id` (body) é **obrigatório** — é o que o leitor
  de tela lê. `role="tooltip"` no body. Trigger só-ícone precisa de `aria-label`
  (o `<svg>` do `voy_icon` é `aria-hidden`).
- Ancoragem: `.tip-right` (alinha pela direita — coluna direita do card),
  `.tip-center`, `.tip-below` (trigger no topo da página).
- Largura ~23rem (≈56 chars/linha). Herança de `uppercase`/`letter-spacing`/
  `font-mono` do rótulo é resetada dentro do body.
- **Limitação:** ancestral com `overflow:hidden` **clipa** o tooltip.
- `.voy-tip-underline` = sublinhado pontilhado no rótulo (afordância).

## Menu lateral (nav)

Grupos **colapsáveis** (Alpine `navGroup(id, active)` em `base.html`): chevron
girando, transição CSS (`grid-template-rows` 0fr→1fr), estado persistido em
`localStorage` (`voy-nav:<id>`), grupo ativo abre sozinho pela URL (checks
server-side de `request.path`), mobile fecha por padrão. Subitens com indent +
linha guia (`.nav-group-list`). Estrutura:

```
Command Center · Visão geral            (topo, fora de grupo)
🚀 IA LABS (premium — glow dourado, .nav-group-premium)
   Centro de Inteligência (/dashboard/ia/) · Sala de Controle (badge live "N 🟢"
   via fetch de modelos/treinos/data/, falha silenciosa) · Modelo Extrator ·
   Vitrine · Validação κ · Chat IA · Dossiê por CNJ · Jurimetria ·
   Busca semântica · Extrair autos · Analisar do acervo · Análises feitas
Leads        → Pipeline · Algoritmo · Visibilidade (perm) · Validação (perm)
Tribunais & Processos → Tribunais · Status/linha do tempo · Processos · Consulta rápida
Dados        → Movimentações · Partes
Operação     → Ingestão · Saúde · Workers · Vetorização · Exportar · API · Convites (su)
```

## Páginas

| URL | Arquivo | Descrição |
|---|---|---|
| `/dashboard/` | `overview.html` | KPIs + 6 charts + filtros globais (período + tribunais) |
| `/dashboard/ia/` | `ia_hub.html` | **IA LABS — Centro de Inteligência**: landing hub das ferramentas de IA. Hero deep-space (star-field), stats live (treinos rodando/concluídos/GPUs via `modelos/treinos/data/` client-side, falha silenciosa), grid de cards navegáveis e timeline "A jornada" (v1 87,9% → v2 Ficha da Parte → v2.1+A/B → DPO/3B/Analista IA) |
| `/dashboard/ia/estagio/` | `ia_estagio.html` | **IA LABS — Estágio do Crédito** (vitrine investidor): CNJ → veredito DC/PRÉ/EMITIDO/MORTO(SATISFEITO·IMPROCEDENTE) ancorado nas movs (motor de regras `dashboard/services/estagio_rules.py`, plugável `ESTAGIO_ENGINE=rules\|gbm\|hybrid`); CNJ fora da base dispara busca on-demand (fila manual DJEN+Datajud, prefer_cortex) com polling |
| `/dashboard/processos/` | `processos.html` | Tabela filtrada |
| `/dashboard/processos/<pk>/` | `processo_detail.html` | Hero card + cards de polos + timeline + botão enriquecer |
| `/dashboard/movimentacoes/` | `movimentacoes.html` | Cards com filter chips (tribunal/tipo/meio/classe/ativo) |
| `/dashboard/partes/` | `partes.html` | Tabela com filter chips (tipo) + busca |
| `/dashboard/partes/<pk>/` | `parte_detail.html` | Perfil + 3 charts (tribunal/papel/polo) + lista filtrada |
| `/dashboard/tribunais/` | `tribunais.html` | Cards por tribunal: processos, movs, cobertura, status backfill, contagens de enriquecimento |
| `/dashboard/tribunais/status/` | `tribunal_status.html` | Status / linha do tempo: visão geral de todos + detalhe (cobertura temporal, volume mensal, processos por ano CNJ, lags) |
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
| `/dashboard/mcp/` | `mcp_setup.html` | Configuração do MCP server (Claude/ChatGPT/Gemini/Cursor) |
| `/dashboard/consulta-rapida/` | `consulta_rapida.html` | Debug em tempo real: consulta CNJ no DJEN+Datajud, mostra raw + parsed sem persistir |
| `/dashboard/modelos/treinos/` | `treinos_dashboard.html` | Sala de controle dos treinos ML — status live de cada run (barra de progresso, loss, ETA, host/GPU/custo), fila e concluídos. Estende `base.html` (menu lateral do Voyager); o miolo dark premium fica escopado no wrapper `.page-treinos` (CSS vars locais, sem vazar pro app). JS puro faz fetch de `modelos/treinos/data/` a cada 60s. Dados: `/app/.ia/treinos_status.json`, publicado por `scripts/coletor_treinos.sh` (loop 120s fora do prod, SSH nas máquinas de treino + `docker cp` no web) |
| `/dashboard/modelos/extrator/` | `modelo_extrator.html` | Model card do extrator de precatórios (arquitetura, gates, artefatos, como rodar). Estende `base.html`; estilo dark premium escopado em `.page-mx`; âncoras internas + botão pra vitrine no topo do conteúdo |
| `/dashboard/modelos/extrator/vitrine/` | `modelo_extrator_vitrine.html` | Vitrine comercial do extrator (antes/depois, ficha da parte, números). Estende `base.html`; estilo escopado em `.page-vitrine`; botão "Ficha técnica completa" no topo do conteúdo |
| `/dashboard/invites/` | `accounts/invites_list.html` | **Superuser**: gerar/revogar convites de cadastro |
| `/invite/<token>/` | `accounts/accept_invite.html` | **Público**: aceitar convite, criar conta |
| `/dashboard/login/` | `login.html` | Patch + wordmark + telemetry strip + SOL counter. **Autocontida**: usa só `dashboard/login.css` (sem Tailwind CDN, sem `voyager-identity.css`, sem Google Fonts) — página pública precisa ser leve em mobile |
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

### Atualizações recentes

- **Dia útil DJEN ausente → vermelho (não cinza):** dia útil sem run de ingestão
  (`< hoje`, tribunal ativo com `backfill_concluido_em` definido) é sintetizado
  como célula com `volume=0` e pintado vermelho, tornando lacunas explícitas em
  vez de invisíveis (cinza).
- **Tooltip do heatmap com métricas nativas:** ao passar o mouse sobre uma
  célula, o tooltip mostra cabeçalho `<FONTE> · <tribunal> · <dia>`, status
  legível (`OK / atenção / anomalia / esperado vazio / sem baseline`) e métricas
  por fonte — DJEN: `encontradas`, `novas`, `duplicadas`, `páginas`, `runs`;
  demais fontes: `processos`. Todos os campos são guardados contra ausência
  (não lança erro se uma chave faltar).

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

## ⚠️ Armadilha: `chart.resize()` sozinho apaga o mapeamento de cor do choropleth

Em mapa (`series.type = 'map'`) com `visualMap`, chamar só `resize()` no
listener de janela **descarta o mapeamento**: medido em Chromium (12/08/2026,
mapa comercial), RO `#ea580c` e PR `#9a3412` viravam `#5470c6` — a paleta
padrão do ECharts — e o choropleth inteiro renderizava no cinza que a nossa
legenda define como "ainda não analisado". Ou seja, **o mapa passava a mentir**
depois de arrastar a janela, abrir o DevTools ou girar o tablet.

Correção: no resize, **repintar** (`setOption(opts, true)`, com debounce
~120ms), não só redimensionar. Vale pra qualquer série que dependa de
`visualMap`.

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

### Widgets da `/dashboard/leads/`

`leads_chart_data` (endpoint lazy) delega a `compute_leads_chart(key, tribunal,
nivel, dias, cliente)` — função pura, sem request/cache — compartilhada com o
warm job. Keys: `kpis`, `timeseries`, `calibration`, `funnel`, `by-tribunal`,
`distribuicao-score`.

### Página: Status por tribunal (`/dashboard/tribunais/status/`)

View `tribunal_status`. Dois níveis numa página só: **visão geral** (tabela de
todos os tribunais ativos, cada linha com mini-timeline de cobertura, clicável
pra `?tribunal=X`) + **detalhe** do tribunal selecionado (KPI strip de saúde,
faixa de cobertura temporal, volume mensal de movs, processos por ano do CNJ).

Dados 100% do warm cache — chave `tribunal_status:v1`, job `warm_tribunal_status`
(`dashboard/tasks.py`, scheduler inline 15min). `compute_tribunal_status` computa
todos os tribunais numa passada (GROUP BY `tribunal_id`); hot path
(`tribunal_status_data`) só lê. Cache miss → placeholders `pending` +
"acquiring signal". Sem lazy-load: tudo cabe numa `cache.get()`.

- **Pré-aquecido** por `warm_leads_charts` (`dashboard/tasks.py`, scheduler 30min)
  no filtro default (`tribunal=None`, `cliente=juriscope`) × períodos
  7/30/90/365d, TTL 7d. Antes era só cache lazy de 5min sem warm — a página
  ficava presa em "ACQUIRING SIGNAL" a cada expiração. Filtros não-default
  continuam lazy (cache 5min).
- `funnel`/`calibration` normalizam `LeadConsumption.resultado` com `.lower()`
  ao bucketizar — o único valor válido é `'validado'` (lowercase); houve 982
  linhas legadas `'VALIDADO'` (path antigo pré-`lote_id`, já limpas em prod)
  que rachavam o funil em dois buckets.

### Página: Esteiras de processamento (`/dashboard/vetorizacao/`)

View `vetorizacao` (`dashboard/views.py`). Layout premium herói→detalhe, tudo no
partial `_partials/_vetorizacao_data.html` (grid consistente, `space-y-8`):

1. **3 esteiras** (HERÓI) — **Vetorização · Classificação · Extração**, cada uma
   um card via include parametrizado `_partials/_vetor_esteira.html` (consistência
   total): donut ECharts maior (`buildVetorProgress`, % no centro), done/total
   grande, **faltam X · Y% restante** (Y = `100 − pct`, `|unlocalize` p/ não
   quebrar o Alpine), **velocidade X/min (últimos 10 min)** e **ETA** (`format_eta`
   → `~2d 4h` / `concluído` / `—`). Badge `(parcial)` no cold-start.
2. **Frota — tabela unificada filtrável** (substitui os antigos cards soltos de
   "Máquinas da frota" + "Extração — frota"). Uma linha por host, colunas
   **Máquina · GPU · Util (barra) · VRAM · Workers · Papel (chips) · Status**.
   Chips de papel: Vetorização (accent/emerald) e/ou Extração (mission/orange) —
   máquina pode ter os dois. **Filtro client-side** (Alpine `x-data`, chips
   Todas/Vetorização/Extração, `x-show` por tag, sem reload). Fresh/stale com
   badge. Dados do bloco `maquinas` do endpoint. Linha compacta abaixo com
   backlog + ETA da extração.
3. **Throughput ao vivo** — KPI strip de 6 (**antes dos gráficos**): workers
   busy/total, fila, acervo (proc. vetorizados), velocidade (proc-vetorizados/min),
   throughput (docs embedados/h), extração/h.
4. **Gráficos** — velocidade de embedding (docs/min, ECharts, 60 min) + breakdown
   por tribunal.

Ordem final: **esteiras (herói) → frota (tabela) → throughput (KPIs) → gráficos.**

Banner **"Como o documento fica útil"** (no shell `vetorizacao.html`) agora é
**colapsável** (Alpine, fechado por default, resumo inline) pra não empurrar o
herói pra baixo.

Auto-refresh HTMX `every 600s` (partial `_partials/_vetorizacao_data.html`).
Builders ECharts (`buildVetorProgress`/`buildVetorSpeed`) persistem no shell
(sobrevivem aos swaps HTMX).

Fluxo **scheduler → cache → página** (a frota vive no Zordon, fora do Voyager):
- Job `warm_vetorizacao_fleet` (`dashboard/tasks.py`, scheduler inline **10min**
  em `djen/scheduler.py`) faz 1 GET em `ZORDON_URL/api/vetorizacao/fleet` e grava
  `cache.set('vetor:fleet:v1', ..., 1300s)`.
- A view lê do cache (`_vetor_fleet_data`, fast path; fallback: warm inline 1x se
  frio). `_vetor_maquinas` funde workers+GPUs num conjunto de máquinas.
- Endpoint no Zordon (`acervo/vetorizacao_fleet_view.py`): conta só workers que
  escutam a fila `vetorizar` (`w.queue_names()` — senão conta ingest/extract),
  velocidade via buckets de `acervo_processo.criado_em`, GPU via hash Redis DB3
  `vetor:gpu` publicado por reporters (`vet_gpu_report.py`) nas máquinas da frota.
- **Enumeração de workers robusta** (`_iter_workers`): `Worker.all()` lê o set
  global `rq:workers`, que nesta topologia fica **VAZIO** (idem os per-fila
  `rq:workers:*`), causando "0 workers" com a frota rodando. Fallback: varre as
  chaves de estado `rq:worker:*` via `Worker.find_by_key`. Mantém o filtro
  `"vetorizar" in queue_names()`.
- **Bloco `maquinas`** (tabela unificada): 1 dict por host `{host, gpu_name, util,
  mem_used, mem_total, workers, workers_busy, tags[], stale}`. Funde `vetor:gpu` +
  `extracao:gpu` + workers. Tag `vetorizacao` se está em `vetor:gpu` OU tem worker
  vetorizar; `extracao` se está em `extracao:gpu`. Rótulos **mantidos como
  reportados** (o embed 3090 se reporta como `zordon` em `vetor:gpu` por herança;
  a mesma máquina física aparece como `llmsv2` em `extracao:gpu`) — NÃO fundimos
  `zordon`→`llmsv2` pra não mascarar a origem de cada telemetria; ficam 2 linhas.
- **Bloco `pipelines`** (3 esteiras): counts atuais por esteira. **Sinais REAIS**
  (o `acervo_processo` é ESTÁTICO — a vetorização adiciona Documento/Chunk a
  processos que já existem, não cria processo novo; medir `Processo.count()`/
  `.criado_em` dava ~0 com a frota a todo vapor).
  - **REGRA DO DENOMINADOR-UNIVERSO** (crítico): os 3 gauges medem o quanto do
    trabalho **TOTAL** foi feito, contra o **MESMO universo de processos COM
    AUTOS** (`vet_total` = manifest **1.149.509** − s3_404 `acervo_missingautos`
    ~12.6k ≈ **1.136.881**). Classif/extração só operam sobre o que a vetorização
    já produziu (~2%) — medir contra o subconjunto já-processado dava a ilusão de
    "99,9% classificado / 36% extraído". Todos convergem p/ **~2%** (honesto).
  - **vetor**: done = `Processo(vetorizado=True)` ("autos embedados"); total =
    com-autos → ~2,3%. Expõe `manifest_total`, `missing`, `universo`.
  - **extração** (processo-level): done = `MetadadoExtraido.count()`; total =
    **com-autos** → ~1,9%. `ext_disponiveis` (processos vetorizados c/ doc
    LLM-relevante) é só sub-rótulo "disponíveis p/ extrair agora" **e** o backlog
    OPERACIONAL da frota (`extracao.backlog` = disponíveis − done), NÃO o
    denominador do gauge.
  - **classif** (doc-level): o total de docs só existe após vetorizar, então
    **extrapola**: `clas_total_est` = (`docs_atuais` / `processos_vetorizados`) ×
    com-autos ≈ **85M** → ~2,3%. É ESTIMATIVA (front rotula "de ~N docs
    estimados", expõe `docs_atuais`).
    - **Display "em dia" (batch):** a classificação roda em **BATCH**
      (`zordon-classif.timer` ~10-30min) e fica caught up (~1-2k docs sem classe).
      A janela de 10min cai entre rodadas → 0/min (parecia travada). Fix de
      display: `_rate_wide` mede a taxa numa janela **larga (1h)** do snapshot
      (suaviza os bursts); `rate_min` faz fallback pra ela quando a curta dá 0.
      Flag **`em_dia`** (backlog operacional = docs sem classe < 2%) + `cadencia`
      → o front mostra pill **"Em dia · batch a cada ~10-30 min"** com a média/1h,
      em vez de "0/min · ETA —". `eta_seg` = None quando em dia (sem backlog
      crescente); só há ETA operacional se houver docs sem classe acumulando.
  - Todas as esteiras carregam `total_estimado=True` + `universo` p/ o front.
    ETA de classif/extração é recalculada contra o novo denominador (dias/semanas,
    honesto) — `faltando` e `rate_min` ficam na mesma unidade (docs p/ classif,
    processos p/ extração).
  - **Top-level KPIs**: `rate_10min` = proc-vetorizados/min (= vet esteira
    rate_min, coerente c/ o numerador); `rate_1h` = **docs embedados/h**
    (`acervo_documento status='ready' criado_em ≥ 1h`, throughput real);
    `speed_per_min` (gráfico) = docs `status='ready'` por minuto; `acervo_total` =
    `Processo(vetorizado=True)`.
  - A **taxa dos últimos 10min** é uniforme via snapshots num sorted-set Redis DB3
    `vetor:pipe:snap` (score=epoch, value=JSON `{ts,vet,clas,ext}`): a cada chamada
    grava snapshot + compara com o mais próximo de `now-600s` (dt<120s é ignorado
    p/ não inflar; poda >2h). Resolve a Classificação (Documento **não** tem
    timestamp de update de `doc_classe`). ETA = faltando/taxa. **Ao trocar a
    definição de um sinal (ex.: vet_done), flushar `vetor:pipe:snap`** senão o
    delta contra snapshots do sinal antigo dá negativo → clampa a 0 por ~10min.
- Sem migration (só RedisCache). A 3090 do embed (host `llmsv2`) não é SSH-ável →
  card de GPU "indisponível" pra ela.
- **Bloco `extracao`** (frota de 2× 3090): `gpus` (hash Redis DB3 **`extracao:gpu`**
  — separado do `vetor:gpu`; labels `llmsv2` e `GipsyDanger`, stale >180s),
  `rate_10min`/`rate_h` (contagem de `MetadadoExtraido.criado_em` nos últimos
  10min → /min e /h; timestamp real, sem snapshot), `backlog` (=
  `pipelines.extracao.faltando`, reusa os counts) e `eta_seg` (backlog/taxa).
  Reporters: `vet_gpu_report.py` (agora aceita `VET_GPU_REDIS_HASH`) via cron
  `*/1min` nas 2 máquinas (`VET_GPU_HOST=GipsyDanger|llmsv2`,
  `VET_GPU_REDIS_HASH=extracao:gpu`, Redis tailscale `100.98.86.54`). O
  `warm_vetorizacao_fleet` do Voyager cacheia o payload inteiro — o bloco
  `extracao` flui sem mudança em `tasks.py`.

## Showcase do Extrator — upload em chunks + extração assíncrona

A tela `/dashboard/ia/showcase/` sobe um PDF/ZIP dos autos e mostra a ficha
extraída on-device pelo pod. **O upload é em CHUNKS + extração ASSÍNCRONA** —
substituiu o POST síncrono que estourava no Cloudflare (body 100 MB / timeout
~100s) em arquivo grande. Aguenta ~1 GB.

**Por quê:** `voyager.was.dev.br` passa por Cloudflare Tunnel. Fatiar em chunks de
8 MB (cada `POST` << 100 MB) e devolver `job_id` na hora (`finish` < 2s, nunca
segura o request longo) contorna os dois tetos. A extração roda num worker.

**Módulos:** `dashboard/showcase_chunks.py` (views de upload/job) +
`dashboard/showcase_jobs.py` (job RQ) + `dashboard/showcase_proxy.py`
(`extrair_no_pod` — chamada ao pod, compartilhada com o endpoint síncrono legado).

| Método | URL | name | Corpo | Resposta |
|---|---|---|---|---|
| POST | `/dashboard/api/showcase/upload/init/` | `dashboard:showcase-upload-init` | `{filename,size,total_chunks,content_type}` | `{upload_id}` |
| POST | `/dashboard/api/showcase/upload/chunk/<upload_id>/<idx>/` | `dashboard:showcase-upload-chunk` | bytes crus do chunk (+ header `X-Chunk-MD5`) | `{ok,idx,bytes,recebidos,total}` |
| POST | `/dashboard/api/showcase/upload/finish/<upload_id>/` | `dashboard:showcase-upload-finish` | `{versao}` **ou** `{versoes:[...]}` (+ `sha256` opcional) | `{jobs:{<versao>:<job_id>}}` |
| GET | `/dashboard/api/showcase/job/<job_id>/` | `dashboard:showcase-job` | — | `{status,etapa,progresso,resultado?,erro?}` |

- **Cliente (Alpine, `showcase.html`):** `File.slice` em chunks de 8 MB, envio com
  **concorrência 4** + **retry com backoff** + md5 por-chunk (`md5FromBuffer`, MD5
  inline — Web Crypto não tem MD5) + sha256 do arquivo inteiro (`crypto.subtle`).
  Barra rica (`X/N chunks · MB · MB/s`). Poll a cada 2s até `done|erro`; quando
  `done`, o `resultado` é a MESMA forma do endpoint síncrono → **`renderFicha`
  intocado**. Compare = 1 job por versão, poll em paralelo.
- **Servidor:** `@login_required @csrf_exempt` (padrão do `showcase_extrair`).
  Chunks gravados em **streaming** (nunca em RAM) em `SHOWCASE_UPLOAD_DIR`
  (default `media/showcase_uploads/<upload_id>/chunk_<idx>`; `media/` é
  gitignored). `finish` **valida integridade** (contagem de chunks + tamanho
  total + hashes), monta o arquivo único (streaming), **enfileira** o job e
  devolve `job_id`. Guarda de path-traversal: `<upload_id>`/`<job_id>` só UUID.
  Limites: `SHOWCASE_MAX_UPLOAD_BYTES` (2 GB), `SHOWCASE_MAX_CHUNKS` (4096).
- **Job/fila:** roda na fila **`manual`** (`worker_manual`, `.103` — MESMO host do
  `web`, ambos bind-montam o repo em `/app` → o worker enxerga os chunks
  montados). `job_timeout=3600s` (pod pode demorar em doc grande). **Decisão de
  fila:** reusar `manual` (já existe, já roda no `.103`, alta prioridade p/
  cliques on-demand) em vez de criar fila nova — zero mudança de compose no
  caminho feliz. Overridable via `SHOWCASE_QUEUE`.
- **Estado do job:** hash no cache Redis keyed por `job_id` (`set_job_state`),
  TTL 1h — sobrevive à limpeza do registry do RQ, polling barato sem tocar o DB.
- **Cleanup:** o diretório do upload é apagado após o ÚLTIMO job (no compare, só o
  job que fecha o lote apaga, pra não puxar o arquivo dos outros). TTL do meta 6h.
- **Compat:** o endpoint síncrono `showcase_extrair` **continua vivo** (fallback);
  ambos passam por `extrair_no_pod` (ponto único da chamada ao pod).
- **LOGS RICOS** (`voyager.showcase`, formato greppável): `evt=init|chunk|assembly|
  enqueued|job_start|pod_ok|job_done|cleanup|*_error` com `upload_id`, `job_id`,
  `idx/total`, bytes, `dt`, MB/s, tamanho da resposta, nº de fichas/docs. Abrir
  `docker logs web` (upload/assembly) e `worker_manual` (job/pod) e ver onde
  travou. Ex.: `[showcase upload_id=… evt=chunk idx=3/128 bytes=8388608 dt=0.42s 19.1MB/s recebidos=4/128]`.
- **Deploy:** toca `dashboard/**` + `urls.py` + o job que roda no `worker_manual` →
  **rebuild web + rebuild/restart do `worker_manual`** no `.103` (não é hot-deploy
  puro — o worker precisa do código novo do job). Sem migration, sem `.102`.
- Testes: `tests/test_showcase_chunks.py` (integridade, rejeições, job com pod
  mockado, guarda de traversal).

## Showcase do Extrator — exportar a análise (MD/PDF/JSON)

A tela `/dashboard/ia/showcase/` extrai a ficha on-device (proxy pro pod em
`showcase_proxy.py`). O **export** do resultado vive em `dashboard/showcase_export.py`
— o front POSTa o **próprio payload** que já tem em mãos e recebe o arquivo:

| URL | name | Corpo | Resposta |
|---|---|---|---|
| `POST /dashboard/api/showcase/export/json` | `dashboard:showcase-export-json` | JSON da análise | `application/json` pretty, attachment |
| `POST /dashboard/api/showcase/export/md` | `dashboard:showcase-export-md` | idem | `text/markdown`, attachment |
| `POST /dashboard/api/showcase/export/pdf` | `dashboard:showcase-export-pdf` | idem | `application/pdf`, attachment |

- `@csrf_exempt @login_required @require_POST` (mesmo padrão do `showcase_extrair`);
  o front manda `X-CSRFToken` mesmo assim (harmless).
- **Sem consulta externa** — o export é 100% derivado do payload; o rodapé reafirma
  "gerado on-device pelo modelo `<versao>`".
- **Forward-compatible**: inclui `estagio` (estágio + linha do tempo + próximos passos)
  e `fichas[].valor_a_receber.status` **se presentes**; nunca quebra se ausentes.
- Nome do arquivo: `analise-<arquivo-sanitizado>-<YYYYMMDD-HHMM>.<ext>`.
- **Lógica de render é PURA** (`render_md(payload)->str`, `render_html(payload)->str`,
  `render_pdf(payload)->bytes`), separada do request — testada em `tests/test_showcase_export.py`.
- **PDF via WeasyPrint** (HTML→PDF, dark premium: cards de resumo, tabela de partes
  com pill de status colorido por estado, timeline com pontos, chips de docs). Exige
  `weasyprint` (requirements.txt) + libs nativas no `Dockerfile`
  (`libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libgdk-pixbuf-2.0-0 libcairo2
  libffi8 fonts-dejavu-core`). Se a lib faltar, o endpoint responde **501** (não 500).
  → mudar isto é **rebuild web** (não hot-deploy: mexeu em requirements + Dockerfile).

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
| `format_eta` | filter | Segundos → ETA humano (`~2d 4h` / `~22min` / `concluído` / `—`). Usado nas esteiras da `/dashboard/vetorizacao/` |

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


## Acompanhamento — o diário de bordo do produto

`/dashboard/acompanhamento/` (login-gated). Model `dashboard.NotaAcompanhamento`.

**Por que existe, e não um changelog em Markdown:** o que este projeto produz de
mais valioso não é código, é **descoberta medida**. "Só tínhamos 13% do acervo
nacional", "uma linha decapitava 43,6% do TJSP por 17 meses", "94 milhões de
publicações indexadas e fora do alcance da tela" — cada uma custou horas de
sonda, e a correção que veio depois se lê no código, mas a descoberta se perde.

**As três regras que o modelo cobra:**

| campo | regra |
|---|---|
| `numeros` | é onde mora a PROVA. Nota de descoberta sem número medido é opinião com data — a tela marca isso explicitamente |
| `data_evento` | ≠ `criado_em`. A descoberta acontece quando a medição fecha, não quando alguém teve tempo de escrever. O filtro de período usa a primeira |
| `impacto` | é sobre o ACERVO e o usuário, não sobre esforço. Corrigir uma linha que devolve 217 mil publicações/dia é alto; refatorar três arquivos sem mudar dado é baixo |

`numeros` aceita duas formas, e a tela renderiza as duas:
`{"rotulo", "antes", "depois", "unidade", "nota"}` (o antes/depois é o que
convence) ou `{"rotulo", "valor", "unidade"}`.

**Detalhe de implementação que já mordeu:** a contagem dos chips por tipo é feita
DENTRO do período mas **antes** do filtro de tipo. Contar depois faria
"Incidente (3)" mostrar 3 só porque o filtro de incidente está ligado — contagem
circular que engana quem lê.

Semear/atualizar as notas de agosto/2026: `manage.py acompanhamento_semear`
(idempotente por título + data).

> ⚠️ Gate de browser: `uppercase` do Tailwind **muda o `innerText`**. Um teste
> que compara texto de label com caixa vai reprovar a tela por engano — compare
> em minúsculas. E `.first` num dia com várias notas pega qualquer uma; navegue
> pelo href.

## Card de cobertura nacional (`/dashboard/acompanhamento/`, 27/08/2026)

A régua do princípio nº 1, na tela. `dashboard/cobertura_nacional.py` mede,
`warm_cobertura_nacional` no scheduler aquece de 6 em 6 h, a view só **lê o
cache**.

```
numerador    tribunals_process       (um por (tribunal, CNJ) — unifica graus)
denominador  CNJs DISTINTOS do voyager-acervo   ≈289.277.192
```

**A armadilha, e ela é de 5 pontos percentuais.** O `voyager-acervo` tem
**342.046.902 documentos** para **≈289.277.192 CNJs**: o Datajud emite um doc
por *(tribunal, grau)* e 24,3% dos processos têm dois ou mais graus. Dividir
pelos documentos dá **29,2%**; pelos CNJs dá **34,5%**. A versão errada é a que
parece mais rigorosa — por isso o card mostra os DOIS números e explica a
diferença no rodapé, em vez de só publicar o certo.

**Nunca no caminho da requisição.** A cardinalidade sozinha leva ~36 s
(HyperLogLog, `precision_threshold=40000`, ±1% neste volume). Sem cache o card
**abstém** com todas as letras — foi medição de rodapé sem teto que derrubou o
site em julho (regra nº 7).

**A série é regressiva.** Ninguém guardou snapshot diário, então ela é
reconstruída do total de hoje menos o que entrou em cada dia (`inserido_em`,
45 dias). A propriedade que importa: **o ponto de hoje é exato por construção**,
então o erro, se houver, fica no passado distante e não no número que alguém vai
citar.

**Três ressalvas ficam VISÍVEIS no card**, não em `title=`: a aproximação HLL, a
estimativa do `reltuples` e a data do retrato do esqueleto (ele não é
atualizado — serve para dizer o tamanho do país, **não** para afirmar que um
processo não existe).

Gráficos: área de cobertura dia a dia com `markPoint` no dia de maior salto (a
recuperação nacional pôs 6,7 M num dia — sem o marcador a curva parece
crescimento orgânico) e barras por tribunal ordenadas por **tamanho do
tribunal**, não por cobertura: o que importa é onde está o buraco absoluto, e o
TJSP sozinho é 20% do país.

## Card "Avanços da semana" (`/dashboard/acompanhamento/`, 28/08/2026)

`dashboard/marcos_semana.py` — aquece de 3 em 3 h (`warm_marcos_semana`), a view
só **lê o cache**.

A timeline do Acompanhamento guarda o RELATO; este card guarda a **RÉGUA**. Um
relato diz "consertamos a duplicação"; a régua diz **2,70 → 1,02**, e é ela que
permite conferir daqui a um mês se regrediu.

**Os dois lados vêm de lugares diferentes, e isso é deliberado:**

| | origem |
|---|---|
| **agora** | MEDIDO a cada aquecimento — banco, índice, Redis. Nunca digitado |
| **antes** | constante **com a data** da medição original |

Não existe máquina do tempo: a duplicação de 2,70× medida em 27/08 não se
recalcula hoje, o dia já passou. Por isso cada marco carrega `medido_em` e a tela
mostra "antes medido em 27/08". Fingir que o passado é recalculável seria pior
que assumir.

**Marco que não deu para medir SAI da lista** e aparece pelo nome em vermelho.
Sumir da tela seria silêncio verde — o card pareceria completo com uma régua a
menos. Há teste para isso, e outro que varre `MARCOS` reprovando qualquer valor
de "agora" fixo no código (isso seria propaganda, não medição).

As oito réguas atuais: cobertura nacional · partes tiradas do texto · processos
com grau · atraso da busca · duplicação da coleta · páginas pedidas ao CNJ ·
workers que o alarme enxerga · tribunais fechados no portão.
