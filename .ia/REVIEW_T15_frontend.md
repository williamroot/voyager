# Code Review T15 — Frontend (T12+T13+T14)

**Veredito:** ⚠️ APPROVE WITH NITS

**Resumo executivo:**

- Identidade Voyager (NASA orange / Pulsar green / Pale Blue Dot / Golden Record / Major Mono / JetBrains Mono / Manrope) está aplicada com consistência nos três blocos — paleta vem de tokens (`--c-mission`, `--c-pulsar`, etc.), sem hardcodes nos templates.
- Acessibilidade está acima da média do projeto: `role`/`aria-label`/`aria-live`/`aria-modal`/`aria-pressed`/`role="progressbar"` aparecem nos lugares certos; focus-visible aplicado às regiões críticas. Mas faltam **focus trap** nos dois modais, **`prefers-reduced-motion`** em todo o CSS animado, e o **atalho `g v` do mockup não foi implementado** em `base.html`.
- O Alpine state `criando` em `validacao_overview.html` nunca muda — o spinner do botão "Criar lote" não vai aparecer (bug funcional médio).
- O hot-key handler está **duplicado** entre `validacao_hotkeys.js` (global) e o `<script>` inline do `_validacao_card.html` (Alpine). Ambos disparam `.click()`. Idempotente, mas confuso — escolher um e remover o outro.
- Bug XSS: nenhum encontrado. `motivo`, `versao_modelo`, chaves de `por_resultado` etc. todos passam pelo auto-escape do Django. Nenhum `|safe` indevido.

## Por categoria (A-I)

### A. Identidade visual
- [✅] Cores corretas — `voyager-identity.css` usa apenas tokens `var(--c-mission|pulsar|golden|pale-blue|info|warning|danger|fg-*|border)`; nenhuma cor literal em CSS dos blocos T12/T13/T14 (`/home/will/projetos/voyager/dashboard/static/dashboard/voyager-identity.css:216-1252`). Pequena exceção justificada: `visibilidade.html:541-546` (heatmap COR map) define rgba literais para legenda do ECharts — documentado e necessário porque ECharts não lê CSS vars.
- [✅] Tipografia aplicada — `Major Mono Display` no wordmark (`visibilidade.html:18`, `_lote_concluido.html:13`), `JetBrains Mono` em telemetria/timestamps/CNJ via `.jb-mono`, `Manrope` no corpo.
- [✅] Terminologia "telemetry station" presente — "acquiring signal" (`visibilidade.html:294`, `validacao_lote.html:46`), "SIGNAL LOST" (`visibilidade.html:297, 334`), "MISSION COMPLETE" (`_lote_concluido.html:13`), "Modelo ativo v6" (`validacao_overview.html:33`).
- [✅] Reuso de partials — `page_header.html`, `kpi.html`, `badge.html`, `empty_state.html` todos reutilizados em validação. `_score_breakdown.html` virou partial reaproveitável (objetivo do T14).
- [⚠️] Wordmark "VOYAGER · LEADS · VISIBILIDADE" pode quebrar em mobile estreito — `visibilidade.html:16-18` usa `text-xl md:text-2xl` e `flex-wrap`, mas em 320px o texto continua em uma linha de ~32 chars. Major Mono Display é largo; provável overflow horizontal em 320px. **Nit:** considerar `truncate` ou `text-xs` em `<sm`. Não bloqueante.

### B. Acessibilidade (a11y)
- [✅] Contraste — texto em fundos `--c-card`/`--c-base` usa `--c-fg`/`--c-fg-soft`/`--c-fg-muted`. Pulsar green sobre card escuro passa AA. Golden sobre golden/0.1 + border golden/0.3 é o caso mais arriscado (`.vc-banner-suspeita`, `_validacao_card.html:57` + CSS `:304-322`) — testar manualmente.
- [✅] ARIA labels — todos os botões de decisão têm `aria-label` explícito incluindo o atalho (`_validacao_card.html:164,173,182,191,200,209,218`). Chips de filtro têm `aria-pressed` (`visibilidade.html:52,56`).
- [✅] Focus visible — `.validacao-card :focus-visible`, `.chart-card :focus-visible`, `.kpi-card :focus-visible`, `.lote-row :focus-visible` definem outline 2px NASA orange (CSS `:609-614, 850-857`).
- [⚠️] Focus management ao trocar de card via HTMX — após `outerHTML` swap em `#card-container`, o foco vai para o body. Operador anotando 100/h perde contexto a cada item. **Recomendação:** `hx-on::after-swap` que move foco para o primeiro `.vc-btn-decisao` (ou `data-autofocus`) do novo card. (`validacao_lote.html:39-43`).
- [✅] Atalhos não conflitam — `validacao_hotkeys.js:44-50` testa `inEditable(target)` E `inEditable(document.activeElement)`, ignorando textarea/input/select/contentEditable. Boa.
- [✅] Hierarquia de headings — h1 em `page_header`, h2 em sections (`.section-h2`), h3 em chart-card / vc-section-header. Lógica.
- [✅] `aria-live` em loading/error — `chart-skeleton` tem `role="status" aria-live="polite"` (`visibilidade.html:189, _chart_card.html:26`, `validacao_lote.html:44`). Erro de criar lote: `<p class="erro-criar" role="alert">` (`validacao_overview.html:180`).
- [✅] `role="dialog" + aria-modal` em modais — presente em `validacao_overview.html:117` (novo lote), `validacao_lote.html:64` (hotkeys), `visibilidade.html:204-206` (re-ingestão).
- [❌] **Focus trap em modais** — nenhum dos três modais (re-ingestão, novo lote, hotkeys) tem focus trap. Tab sai do modal e vai para a página atrás. Acessibilidade WCAG 2.1 — não é APPROVE clean. **Issue média.** Adicionar `@keydown.tab` handler ou usar Alpine `x-trap` (não há plugin instalado, então implementar manual).
- [❌] **`prefers-reduced-motion` não respeitado em lugar nenhum** — `@keyframes pulsar` (1.6s loop infinito), `@keyframes drift` (220s), `@keyframes signal-drift` (3s), `.acquiring-signal .pulsar`, `.lote-progress-bar-fill transition: 0.4s`, `.vc-toggle transition: 180ms`, `.vc-btn-decisao transition: 80ms`. Operador com vestibular sensitivity ou epilepsia vai sofrer com 100 anotações/h. **Issue média.** Adicionar bloco global:
  ```css
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
    }
  }
  ```
- [✅] `sr-only` — usado corretamente em `fieldset>legend.sr-only` no `_validacao_card.html:159` e definido no CSS `:617-621`.
- [✅] Tab order — lógico. Filtros → KPIs → charts → lotes → tabela em visibilidade; header → progress → card body → form decisão em lote.
- [⚠️] **Tab navega os 7 botões de decisão** — Sim, mas como todos são `type="submit"` e estão dentro do mesmo `<form>`, Enter no primeiro foco submete com `eh_precatorio`. O default no form é o primeiro submit button — pode causar submit acidental ao pressionar Enter durante navegação inicial. **Nit:** mudar o radio "Confiança" para ser a primeira tabindex ou prevenir Enter de auto-submit.
- [⚠️] **`kbd` esconde em mobile** — `.vc-kbd { display: none }` em `<720px` (`CSS:628`). `display:none` esconde também do leitor de tela. OK porque `aria-label="Marcar como Precatório (atalho 1)"` já cobre — verificado. **Nit redundante:** o `.vc-kbd` no modal hotkeys (`validacao_lote.html:70-79`) continua visível em mobile e não há `aria-hidden`. Como o conteúdo da `kbd` é texto curto ("1", "Ctrl"), leitor lê e fica OK.

### C. XSS / Segurança template
- [✅] Todos `{{ var }}` passam por auto-escape. Sem `|safe` nos três templates de validação ou em visibilidade.
- [✅] `motivo` é renderizado em chip via `{{ motivo }}` (`_validacao_card.html:65`) — escapado.
- [✅] URLs via `{% url %}` em todos os lugares; concatenação só em `visibilidade.html:38-39` (`{% url 'dashboard:leads-export' %}?nivel=PRECATORIO{% if tribunal_filtro %}&tribunal={{ tribunal_filtro }}{% endif %}&limit=5000`). `tribunal_filtro` vem do backend (sigla validada). Aceitável.
- [✅] `data-*` attributes — `data-processo-id`, `data-lote-id`, `data-posicao`, `data-hotkey`, `data-page`, `data-kpi`, `data-chart`, `data-nivel` etc. — todos com valores numéricos/strings curtas escapadas. OK.
- [⚠️] `visibilidade.html:71-72` o banner Juriscope usa Alpine `x-init="fetch(...).then(r=>r.json()).then(j=>{...})"` — confia no JSON do endpoint; sem risco de XSS porque o dado só popula `shown` boolean.
- [⚠️] **Inline `<script>` em `_validacao_card.html:251-281`** — é uma global function `validacaoCard()`. Reanexada a cada HTMX swap (o partial é o response do swap). Mas como Alpine processa o novo DOM, o `x-data="validacaoCard()"` re-avalia OK. **Nota:** o `<script>` deve ser executado pelo HTMX após swap — HTMX 2 executa por padrão. Verificado.

### D. Performance frontend
- [✅] HTMX triggers corretos — `load` para hidratar card, `submit` para form decisão. Sem `keyup` em inputs grandes.
- [⚠️] **Lazy charts não usam IntersectionObserver** — `lazyChart()` (`base.html:495`) é chamado direto via `x-init`, sem viewport check. Em `visibilidade.html` (5 charts) todos disparam fetch simultâneo no load. **Issue média:** para uma página com 5 charts cada um podendo ser >100KB de JSON, considerar IntersectionObserver. Não bloqueante porque é página interna com poucos usuários.
- [✅] ECharts singleton — `setupChart` no `base.html` reutiliza `el.dataset.echart` (stringified) e `echarts.getInstanceByDom` previne dupla instanciação. OK.
- [⚠️] **Wrapper monkey-patch de `setupChart` em `visibilidade.html:670-688`** — sobrescreve `window.setupChart` para todos os charts da página inteira (não só os do heatmap). Funcional, mas adiciona overhead e dificulta debugging. **Nit:** preferir registrar click handler via `htmx:afterSwap` ou Alpine `x-effect` específico do heatmap.
- [✅] Event listeners — `validacao_hotkeys.js` registra um único `document.addEventListener('keydown', ...)` e um `htmx:afterRequest`. Sem leak porque o módulo só registra uma vez (IIFE).
- [⚠️] **Duplicação de listeners** — `validacao_hotkeys.js:77` E `_validacao_card.html:152` (`@keydown.window="handleHotkey($event)"`) ambos escutam keydown e disparam `.click()` no botão. Documentado como "idempotente" (`validacao_hotkeys.js:6-8`), mas: 
  - clica duas vezes? Não — o segundo `.click()` num botão `submit` durante request HTMX cria race. 
  - HTMX desabilita `[disabled]`? Sim, por padrão `hx-disabled-elt`. 
  - **Nit:** escolher uma das duas implementações. Remover o `@keydown.window` do Alpine (deixar só o JS module).
- [⚠️] Após swap HTMX, listeners de Alpine são re-criados; mas o handler global do `validacao_hotkeys.js` persiste e continua mirando o seletor `button[name="resultado"][value="..."]` (que existe no novo card). OK.

### E. UX / Funcionalidade
- [⚠️] **Auto-advance 200ms não está implementado** — o mockup pediu (MOCKUP_validacao.md §10.1). Hoje o HTMX swap acontece imediato sem delay nem toast de feedback. **Issue média.**
- [⚠️] **Undo Ctrl+Z é só stub** — `validacao_hotkeys.js:67-75` dispara `CustomEvent('validacao:undo-requested')` mas nada escuta esse evento. Documentado como "T15/T16". Aceitável se T15 cobre.
- [✅] Hotkeys overlay com `?` funcional — `validacao_lote.html:60` escuta `@validacao:show-hotkeys.window` e o JS dispara.
- [⚠️] **Modal "criar lote" sem validação client** — `tamanho` aceita 1..5000 via HTML5 `min/max` mas `parametros_json` regex no input borderline é apenas `pattern=` (não bloqueia submit se vazio). `required` está só em estrategia/tamanho. OK como progressive enhancement.
- [✅] Estados loading/empty/error em charts — todos têm `chart-skeleton` + `pulsar-mark` ("acquiring signal") via `_chart_card.html`. Empty handled no builder (`buildHistScoreTribunal`, `buildCalibracaoTribunal`). Erro vai pra `showChartError` (base.html) — mas SEM retry button visível (mockup pediu).
- [⚠️] **Mobile responsive** — testado breakpoints `@media (max-width: 720px)` e `767px`. Wordmark VOYAGER em 320px provavelmente extrapola (já citado em A). KPI grid vira snap-scroll OK. Modal vira fullscreen OK.
- [✅] Snap-scroll horizontal dos KPIs — implementado em CSS `:654-669`.
- [✅] Heatmap clicável — modal stub funcional, gating por `_cor === 'vazio'` antes de abrir.
- [⚠️] **Form de decisão NÃO funciona sem JS** — botões `type="submit"` enviam normal, mas `action="{{ salvar_url|default:'' }}"` pode ser vazio se o wrapper T13 (`leads/_partials/_validacao_card.html:11`) falhar em passar o param. E `tempo_segundos` precisa de JS pra setar valor. Server-side deveria aceitar `tempo_segundos` ausente como 0 (verificar T11 backend).
- [❌] **Atalho `g v` não implementado** — `base.html:584-589` define `URLS = {h, p, m, i}` sem `v`. Mockup pediu `g v` → `/dashboard/leads/visibilidade/`. **Nit/issue baixa.**

### F. CSS qualidade
- [✅] Variáveis CSS — só tokens `--c-*`. Sem `bg-zinc-900` ou `text-red-500` nos blocos novos.
- [⚠️] **Duplicação**: `.lote-progress-bar` e `.lote-progress` compartilham regras via seletor agrupado (`CSS:938-956`) — boa. Mas há dois conceitos de "lote": `lote-card` (overview) e `lote-row` (visibilidade), e dois pulsars: `.lote-pulsar` (`:781-786`) e `.acquiring-signal .pulsar` (`:1039-1045`) — keyframe é o mesmo, OK.
- [⚠️] **CSS regra vazia** — `_validacao_card.html` CSS bloco linha `:481-483`: `.vc-botoes-decisao .vc-btn-decisao:nth-child(n+5) { /* segunda fila: 3 botões em 4 cols → fill */ }` — comentário sem regra. Lixo. Remover.
- [✅] Sem `!important` desnecessário.
- [✅] Mobile-first/desktop-first — convenção desktop-first via `@media (max-width: N)`, consistente.
- [✅] Light theme — usa `html:not(.dark)` overrides existentes. Tokens dão suporte automático. Testar manualmente o banner suspeita golden em light.

### G. UX edge cases
- [✅] Lote vazio — `_lote_concluido.html` cobre concluído, `empty_state.html` cobre overview sem lotes. View backend trata lote vazio?
- [✅] Lote concluído — `_lote_concluido.html` mostra distribuição.
- [❌] **`_lote_concluido.html:30` usa `{{ resultado }}` cru** — `por_resultado.items` retorna chaves como `eh_precatorio`, `nao_lead`, `incerto` (strings do enum) e mostra DIRETAMENTE como label. Mockup pediu "💎 PRECATÓRIO", "⏳ PRÉ" etc. **Issue média.** Mapear via dict ou via `get_resultado_display`.
- [⚠️] Sem permissão (403) — não há tratamento explícito no template; depende do middleware Django. Mockup pediu "RESTRICTED SECTOR" estilizado. **Nit (baixa).**
- [⚠️] Sem internet → "SIGNAL LOST" — implementado no `showChartError` mas SEM botão retry clicável. Mockup pediu `<button>retry</button>`. **Issue média.**
- [⚠️] Validação salvar falha → retry button — não implementado. HTMX swap falha silenciosa.
- [✅] Sem motivo escrito — opcional, OK.
- [⚠️] `tempo_segundos > 3600` — `x-data="validacaoCard()"` em `_validacao_card.html:266` faz `Math.floor((Date.now() - this.iniciado) / 1000)`. Se operador deixar a aba aberta 1 dia, vira `tempo_segundos = 86400`. Backend T11 deveria clamp. **Nit (baixa).**

### H. JS implementation
- [✅] `validacao_hotkeys.js` escopado — checa `document.body.dataset.page` começa com `leads-validacao` (`:22-25`). Bom isolamento.
- [⚠️] **`<script>` inline em `_validacao_card.html` conflita parcialmente** — handler `@keydown.window="handleHotkey($event)"` duplica o que o JS module faz. Já citado em D.
- [✅] Alpine `x-data="validacaoCard()"` re-inicializa após swap — OK porque o partial inteiro re-renderiza com `x-data` no `<article>`.
- [❌] **`Ctrl+Z` não chama nada útil** — `attemptUndo()` faz `undoStack.pop()` e dispara CustomEvent. Nenhum listener consome. Operador aperta Ctrl+Z, nada acontece visualmente. **Issue baixa** — documentado como T15/T16.
- [✅] Hotkeys filtradas em textarea — `inEditable()` cobre. Bom.

### I. Mockup conformity
- [⚠️] **Hotkeys J/K (navegação anterior/próximo) NÃO implementados** — mockup §8 pediu. Não estão nem em `validacao_hotkeys.js` nem no card. **Issue baixa** se backend T11 não suporta voltar item.
- [⚠️] **Confiança "Tab/setas" não implementado** — Tab funciona (radios em fieldset), setas não navegam entre opções. WCAG não exige; pular.
- [⚠️] **Auto-expand textarea motivo em INCERTO/ENRIQUECER** — implementado parcialmente: o Alpine inline `:273-275` seta `motivoExpandido = true` antes de `.click()`. Mas o `details[open]` é dependent de `:open="motivoExpandido"` — atributo é booleano e Alpine binding pode não funcionar como esperado (em Alpine, `:open="bool"` resolve para `open=""` quando true e remove quando false, OK). Verificado, deve funcionar.
- [✅] Estado pós-decisão (toast 200ms) — não está aqui (vai pro T15/T16).
- [⚠️] **Mission complete distribuição** — mockup mostrava "💎 PRECATÓRIO 22 (18%) ▮▮▮▮▮▮░░" com label humano + emoji. Hoje mostra "eh_precatorio 22 18%" — string crua sem emoji nem label. Já reportado em G.
- [✅] Banner suspeita FN — cores corretas, chips de motivos OK.

## Blockers
Nenhum blocker estrito. Aprovado com nits.

## Issues médias
1. **Focus trap ausente nos 3 modais** (`visibilidade.html:199-227`, `validacao_overview.html:111-191`, `validacao_lote.html:56-85`). Tab sai do modal.
2. **`prefers-reduced-motion` não respeitado em nenhum lugar** (`voyager-identity.css` inteiro). Adicionar bloco global.
3. **`_lote_concluido.html:30` mostra chaves enum cruas** (`eh_precatorio`, `nao_lead`) em vez de labels humanas com emoji.
4. **Bug Alpine `criando` em `validacao_overview.html:13,184-187`** — variável nunca muda; spinner "criando…" nunca aparece. Fix: trocar `hx-on::before-request` para `@htmx:before-request="criando=true"` e `@htmx:after-request="criando=false"`.
5. **Auto-advance 200ms + toast de feedback ausente** — mockup pediu para sentir que salvou. Hoje swap imediato sem confirmação.
6. **`showChartError` não renderiza botão retry clicável** — mockup pediu "SIGNAL LOST · retry". Hoje só texto.
7. **Handlers de keydown duplicados** — `validacao_hotkeys.js` (global) e Alpine inline em `_validacao_card.html:152`. Escolher um.
8. **Focus management pós-swap** — após HTMX troca card, foco vai para body. Operador perde contexto.

## Nice-to-have
1. **Atalho `g v` em `base.html:584-589`** — adicionar `v: '{% url "dashboard:leads_visibilidade" %}'`.
2. **Hotkeys J/K (anterior/próximo)** — mockup §8, depende de backend suportar.
3. **Wordmark `VOYAGER · LEADS · VISIBILIDADE` quebra em 320px** — Major Mono Display é largo, considerar `truncate` ou variante menor em mobile.
4. **Remover CSS regra vazia** `vc-botoes-decisao .vc-btn-decisao:nth-child(n+5)` em `voyager-identity.css:481-483`.
5. **Monkey-patch de `setupChart`** em `visibilidade.html:670-688` — substituir por hook mais cirúrgico (Alpine `x-effect` ou `htmx:afterSwap` filtrado pelo heatmap).
6. **IntersectionObserver para lazy charts** — quando a página tiver muitos charts pesados.
7. **Implementar listener real para `validacao:undo-requested`** — hoje undoStack só engorda.
8. **Validar `tempo_segundos > 3600`** no client antes de submeter (clamp ou warning).
9. **Estado 403 "RESTRICTED SECTOR"** — quando view rejeita por permissão (mockup §4.4). Hoje cai no 403.html genérico.

## Smoke test recomendado (manual)
- **Desktop**: navegar `/dashboard/leads/visibilidade/` → filtrar TRF1 → conferir 5 charts carregam + clicar gap no heatmap → confirmar modal. Trocar tribunal via chip e verificar URL atualizada.
- **Desktop validação**: abrir overview → criar lote (modal) → preencher e enviar → verificar redirect → anotar 5 itens com atalhos 1/2/3/4/I/E/S → conferir swap suave → testar `?` → Esc fecha. Reduzir conexão e verificar SIGNAL LOST.
- **Mobile 320-375px**: scroll horizontal dos KPIs em visibilidade → verificar wordmark NÃO causa overflow → abrir modal hotkeys em lote → ver se fullscreen funciona → tap fora fecha.
- **Atalhos**: Tab através de filtros/KPIs/charts/cards — verificar outline NASA orange em cada → testar Tab em modal aberto (esperado: deveria não escapar — falha hoje) → `Ctrl+Z` no card (esperado: undo; hoje noop) → `?` toggle hotkeys.
- **Acessibilidade**: rodar axe-core no Chrome DevTools nas 3 páginas. Atenção a:
  - contraste do banner golden em light theme
  - focus trap modal
  - reduced-motion (toggle no SO)
- **Light theme**: alternar tema (`t`) e revalidar cores Pulsar/Mission em light — `validacao-card border-left rgb(--c-pulsar/0.55)` pode sumir em fundo claro.

## Decisão final
- ✅ **APPROVE WITH NITS** — T24 desbloqueada. Issues médias (focus trap, reduced-motion, label do lote concluído, auto-advance, bug `criando`) são endereçáveis em PRs incrementais, não bloqueiam a entrega T15.
- Recomendado priorizar issues médias 1, 2, 3, 4 (focus trap, reduced-motion, labels enum, bug Alpine) num "T15.5 polish" antes de mostrar para usuário externo.
