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

**Ícones (`voy_icon` tag → sprite SVG):**

Sprite em `dashboard/static/dashboard/voyager-icons.svg` — 21 símbolos `<symbol id="voy-{alias}">`, todos derivados do **Lucide v0.460.0** (ISC). Renderiza via `{% voy_icon "name" "tailwind-classes" %}` em qualquer template.

Os aliases preservam o vocabulário espacial; o desenho vem do Lucide. Re-gerar com `python3 scripts/build_sprite.py` (atualiza versão pinada no topo do script).

Mapping atual (40 símbolos):

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
| `/dashboard/consulta-rapida/` | `consulta_rapida.html` | Debug em tempo real: consulta CNJ no DJEN+Datajud, mostra raw + parsed sem persistir |
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
