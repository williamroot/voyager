# Mockup — Validação manual de leads

Página `/dashboard/leads/validacao/` (overview) e `/dashboard/leads/validacao/<lote_id>/`
(workbench). Alvo: 100 anotações/h. Identidade visual "Mission Control" (consultar
`DASHBOARD.md` antes de implementar).

Convenções deste documento:

- Larguras: desktop ~88 chars, mobile ~38 chars
- `[chip]` = `_partials/chip.html`; `[BADGE]` = `_partials/badge.html`
- `(*)` = pulsar verde (`.pulsar`) | `▮▮▮░░` = progress bar
- Cores aludidas: accent=emerald, mission=NASA-orange, warning=amber,
  danger=rose, info=sky, pulsar=phosphor-green

---

## 1. Página overview — `/dashboard/leads/validacao/`

### Desktop

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ VOYAGER · MISSION CONTROL                                              [☼] [⚙] [▾WS] │
├──────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│  VALIDAÇÃO MANUAL DE LEADS                                       [+ NOVO LOTE  ⌘N]   │
│  Anote processos um-por-vez. Atalhos: 1/2/3/4 · I · E · S · J/K · ?                  │
│                                                                                      │
│  ┌─ LOTES ATIVOS ────────────────────────────────── 3 abertos ─────────────────────┐ │
│  │                                                                                 │ │
│  │  ┌─────────────────────────────────────────────────────────────────────────┐   │ │
│  │  │ (*) LOTE-0042   [BADGE TRF1]  [BADGE estratégia: mining-fn]             │   │ │
│  │  │     "alta suspeita FN · score 0.78–0.95 · 3+ matches"                   │   │ │
│  │  │                                                                         │   │ │
│  │  │     ▮▮▮▮▮▮▮▮░░░░░░░░░  47 / 120     39%        ⏱  ~31s/item             │   │ │
│  │  │                                                  ⏳ ~38 min restantes   │   │ │
│  │  │     skip rate 4.3% · última anotação 2min atrás                         │   │ │
│  │  │                                                                         │   │ │
│  │  │                                            [▶ CONTINUAR  ⏎]   [pausar]  │   │ │
│  │  └─────────────────────────────────────────────────────────────────────────┘   │ │
│  │                                                                                 │ │
│  │  ┌─────────────────────────────────────────────────────────────────────────┐   │ │
│  │  │     LOTE-0041   [BADGE TRF3]  [BADGE estratégia: random-naolead]        │   │ │
│  │  │     "amostra controle · precision check"                                │   │ │
│  │  │     ▮▮▮▮▮▮▮▮▮▮▮▮▮▮░░░  92 / 100     92%        ⏱  ~26s/item             │   │ │
│  │  │     skip rate 1.0%                              [▶ CONTINUAR]           │   │ │
│  │  └─────────────────────────────────────────────────────────────────────────┘   │ │
│  │                                                                                 │ │
│  │  ┌─────────────────────────────────────────────────────────────────────────┐   │ │
│  │  │     LOTE-0040   [BADGE TRF1]  [BADGE top-score]                         │   │ │
│  │  │     ▮░░░░░░░░░░░░░░░░  3 / 50      6%         pausado há 1d             │   │ │
│  │  │                                                 [retomar]   [descartar] │   │ │
│  │  └─────────────────────────────────────────────────────────────────────────┘   │ │
│  └─────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                      │
│  ┌─ HISTÓRICO (últimos 7d) ─────────────────────────────────────────────────────────┐ │
│  │ ID         estratégia       tribunal   itens  prec  pré  dc  ~NL  incerto  tempo │ │
│  │ LOTE-0039  mining-fn        TRF1        120    18   24    7    62      9   1h12 │ │
│  │ LOTE-0038  random-top       TRF3        100    31   28   14    24      3   0h41 │ │
│  │ LOTE-0037  random-naolead   TRF1        100     0    1    2    96      1   0h33 │ │
│  │                                                                  [ver tudo →]   │ │
│  └─────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Mobile (~38c)

```
┌──────────────────────────────────────┐
│ ☰  VOYAGER          [☼]              │
├──────────────────────────────────────┤
│ VALIDAÇÃO DE LEADS                   │
│ [+ NOVO LOTE]                        │
│                                      │
│ ── ATIVOS (3) ──                     │
│                                      │
│ ┌──────────────────────────────────┐ │
│ │ (*) LOTE-0042  [TRF1] [mining-fn]│ │
│ │ ▮▮▮▮▮▮▮▮░░░░░░  47/120  39%     │ │
│ │ ⏱ ~31s · ⏳ ~38min                │ │
│ │ skip 4.3%                        │ │
│ │ [▶ CONTINUAR]                    │ │
│ └──────────────────────────────────┘ │
│                                      │
│ ┌──────────────────────────────────┐ │
│ │ LOTE-0041  [TRF3] [random]       │ │
│ │ ▮▮▮▮▮▮▮▮▮▮▮▮▮▮░░  92/100  92%   │ │
│ │ [▶ CONTINUAR]                    │ │
│ └──────────────────────────────────┘ │
│                                      │
│ ── HISTÓRICO ──                      │
│ LOTE-0039  TRF1  120  1h12  [ver]   │
│ LOTE-0038  TRF3  100  0h41  [ver]   │
└──────────────────────────────────────┘
```

---

## 2. Página de validação — `/dashboard/leads/validacao/<lote_id>/`

### Desktop

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ ← lotes   LOTE-0042  [TRF1] [mining-fn]   ⏱ 00:23:41   skip 4.3%   ?=hotkeys  [⊗ X] │
│ ─────────────────────────────────────────────────────────────────────────────────────│
│ progresso  ▮▮▮▮▮▮▮▮▮▮░░░░░░░░░░░░░░  item 48 / 120   ETA ~38min   média 31s/item     │
├──────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│ ┌──────────────────────────────────────────────────────────────────────── CARD ──┐  │
│ │  CNJ  5012345-67.2023.4.01.3400   [BADGE TRF1]  → atual: [⏳ PRÉ · 0.62]       │  │
│ │  classe  Cumprimento de Sentença contra Fazenda Pública (12078)                │  │
│ │  3ª Vara Federal · DF · autuado 2023-04-11 · última mov 3 dias atrás           │  │
│ │                                                                                │  │
│ │  ╭─ ⚠ SUSPEITA DE FALSO NEGATIVO ──────────────────────────────────────────╮  │  │
│ │  │ Mining detectou: 3 estratégias bateram (score 0.82)                     │  │  │
│ │  │ [precat-regex] [rpv-text] [trans-julg→expedição]                        │  │  │
│ │  │ Motivo: classe Cumprimento + 2 movs com "precatório expedido"           │  │  │
│ │  ╰─────────────────────────────────────────────────────────────────────────╯  │  │
│ │                                                                                │  │
│ │  ╔═ POR QUE A CLASSIFICAÇÃO ATUAL ═══════════════════════════════════════════╗ │  │
│ │  ║  score 0.617  ·  threshold ⏳ ≥ 0.4  ·  threshold 💎 ≥ 0.7 + F2|F11       ║ │  │
│ │  ║                                                                          ║ │  │
│ │  ║  TOP 5 FEATURES                                  peso × valor = contrib  ║ │  │
│ │  ║  📁 F1   Cumprimento contra Fazenda             +1.92 × 1   = +1.92  ▮▮  ║ │  │
│ │  ║  📜 F15  Volume de movs (log)                   +2.31 × 0.61 = +1.41  ▮▮  ║ │  │
│ │  ║  🔁 F1×F15  sinergia classe × volume            +1.61 × 0.61 = +0.98  ▮   ║ │  │
│ │  ║  📅 F18  Ano CNJ (2023, recente)                +0.44 × 0.62 = +0.27      ║ │  │
│ │  ║  ⏰ F21  Dias desde última mov (recente)         +0.57 × 0.34 = +0.19      ║ │  │
│ │  ║                                       intercept −3.196 → logit 0.48 → 0.62║ │  │
│ │  ║  [ver todas as 19 →]                                                     ║ │  │
│ │  ╚══════════════════════════════════════════════════════════════════════════╝ │  │
│ │                                                                                │  │
│ │  ── ÚLTIMAS 5 MOVIMENTAÇÕES ─────────────────────────────────────────────────  │  │
│ │  2026-05-07  [Expedição]   "Expedido(a) Ofício Requisitório nº 2026/00231…"   │  │
│ │  2026-05-02  [Decisão]     "Defiro a expedição de precatório referente…"      │  │
│ │  2026-04-28  [Cumprimento] "Intime-se a Fazenda para impugnar nos termos…"    │  │
│ │  2026-03-14  [Sentença]    "Trânsito em julgado certificado em 14/03/2026."   │  │
│ │  2026-02-02  [Mov. Geral]  "Conclusão para sentença."                         │  │
│ │  [ver todas (47) →]                                                           │  │
│ │                                                                                │  │
│ │  ── PARTES ──────────────────────────────────────────────────────────────────  │  │
│ │  ativo:   [pf MARIA DA SILVA]  [adv ANA P. (OAB/DF 12345)]                    │  │
│ │  passivo: [un UNIÃO FEDERAL]   [adv AGU]                                       │  │
│ │                                                                                │  │
│ │                                                       [abrir no Voyager ↗]    │  │
│ └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│ ┌─ DECISÃO ─────────────────────────────────────────────────────────────────────┐   │
│ │                                                                               │   │
│ │  [1 💎 PRECATÓRIO]  [2 ⏳ PRÉ]  [3 🌱 D.CRED]  [4 ❌ NÃO-LEAD]                 │   │
│ │  [I ❓ INCERTO]     [E 🔄 ENRIQUECER]          [S ⏭ SKIP]                      │   │
│ │                                                                               │   │
│ │  confiança:  ( ) alta    (•) média    ( ) baixa            [tab/setas]        │   │
│ │                                                                               │   │
│ │  ▸ motivo (opcional, colapsado) — clique p/ expandir                          │   │
│ │                                                                               │   │
│ │  Enter = confirmar e avançar    ·    Ctrl+Z = desfazer última (2s)           │   │
│ └───────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                      │
│  J ← anterior   ·   próximo → K                                                      │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Estado pós-decisão (200ms — auto-advance feedback)

```
   ┌───────────────────────────────────────────────────┐
   │  ✓  Anotado como ⏳ PRÉ-PRECATÓRIO · média        │
   │     [Ctrl+Z desfaz]                              ⟳ │
   └───────────────────────────────────────────────────┘
            (toast verde, fade out em 2s)
```

### Mobile (~38c)

```
┌──────────────────────────────────────┐
│ ← LOTE-0042   48/120   ⏱31s          │
│ ▮▮▮▮▮▮▮▮▮░░░░░░░░░░░░  40%          │
├──────────────────────────────────────┤
│ CNJ 5012345-67.2023.4.01.3400        │
│ [TRF1] → [⏳ PRÉ · 0.62]              │
│ Cumprim. Sentença · Fazenda Pública  │
│                                      │
│ ⚠ SUSPEITA FN · score 0.82           │
│ [precat-regex][rpv-text][trans-julg] │
│                                      │
│ ▸ Por que a classificação (5 feats)  │
│ ▸ Últimas movs (5)                   │
│ ▸ Partes                             │
│ [abrir no Voyager ↗]                 │
├──────────────────────────────────────┤
│ ┌──────────────────────────────────┐ │
│ │      💎 PRECATÓRIO               │ │
│ ├──────────────────────────────────┤ │
│ │      ⏳ PRÉ-PRECATÓRIO            │ │
│ ├──────────────────────────────────┤ │
│ │      🌱 D. CREDITÓRIO             │ │
│ ├──────────────────────────────────┤ │
│ │      ❌ NÃO-LEAD                  │ │
│ ├──────────────────────────────────┤ │
│ │  ❓ INCERTO   🔄 ENRIQ   ⏭ SKIP  │ │
│ └──────────────────────────────────┘ │
│ confiança: [alta][média•][baixa]     │
│ ▸ motivo                             │
└──────────────────────────────────────┘
```

---

## 3. Partial isolado — `_validacao_card.html`

Estrutura puramente apresentacional. Entrada: `processo` + `mining_signal` (opcional) +
`classif_explicacao`. Reutilizável em outras páginas (preview de lote, drill-down).

```
┌─ _validacao_card.html ──────────────────────────────────────────────────────────┐
│                                                                                 │
│  ┌── HEADER ──────────────────────────────────────────────────────────────────┐ │
│  │  CNJ {{ processo.cnj }}   [BADGE tribunal]   → [BADGE classif·score]      │ │
│  │  {{ classe_nome }} ({{ classe_codigo }})                                  │ │
│  │  {{ orgao }} · {{ uf }} · autuado {{ data }} · última mov {{ rel }}       │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
│  {% if mining_signal %}                                                         │
│  ╭── BANNER AMBER (warning) ──────────────────────────────────────────────────╮ │
│  │ ⚠ SUSPEITA {{ tipo }}  ·  score {{ score }}  ·  {{ n }} estratégias        │ │
│  │ [chip estratégia1][chip estratégia2]…  + motivo curto                     │ │
│  ╰────────────────────────────────────────────────────────────────────────────╯ │
│                                                                                 │
│  ╔══ POR QUE A CLASSIFICAÇÃO ═══════════════════════════════════════════════╗   │
│  ║  score · threshold(s) aplicável(eis)                                    ║   │
│  ║  TOP 5 features (peso × valor = contribuição + barra visual)            ║   │
│  ║  [ver todas →] (link expande pra 19)                                    ║   │
│  ╚═════════════════════════════════════════════════════════════════════════╝   │
│                                                                                 │
│  ── ÚLTIMAS 5 MOVS ──────────────────────────────────────────────────────────   │
│  [data] [tipo]  trecho (1 linha, truncado a 70c)                                │
│  …                                                                              │
│  [ver todas ({{ total }}) →]                                                    │
│                                                                                 │
│  ── PARTES ──────────────────────────────────────────────────────────────────   │
│  ativo:   [chip parte][chip adv]                                                │
│  passivo: [chip parte][chip adv]                                                │
│                                                                                 │
│                                                       [abrir no Voyager ↗]     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Variantes:
- `compact=true` → score breakdown e movs ficam `<details>` colapsados (uso em listas)
- `readonly=true` → omite slot futuro de decisão (não há decisão neste partial; viver na página pai)

---

## 4. Estados especiais

### 4.1 Lote vazio

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← lotes   LOTE-0043                                                         │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│           ┌───────────────────────────────────────────────────┐              │
│           │              ╭─────────╮                          │              │
│           │              │   ▢ ▢   │   (ilustração: caixa)    │              │
│           │              ╰─────────╯                          │              │
│           │                                                   │              │
│           │       LOTE VAZIO — sem processos para anotar      │              │
│           │       a estratégia não retornou candidatos        │              │
│           │                                                   │              │
│           │       [GERAR NOVO LOTE]   [voltar à lista]        │              │
│           └───────────────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Lote concluído

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← lotes   LOTE-0042 · CONCLUÍDO ✓                                            │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ╭─ MISSION COMPLETE ─────────────────────────────────────────────────╮     │
│   │                                                                    │     │
│   │      120 itens anotados · 1h 02m 17s · 31s/item média              │     │
│   │                                                                    │     │
│   │   ┌──────────────────────────────────────────────────────────────┐ │     │
│   │   │  💎 PRECATÓRIO    ▮▮▮▮▮▮▮░░░░░░░░░░░░     22  (18%)          │ │     │
│   │   │  ⏳ PRÉ            ▮▮▮▮▮▮▮▮▮▮▮▮░░░░░░░     38  (32%)          │ │     │
│   │   │  🌱 D. CREDITÓRIO ▮▮▮▮░░░░░░░░░░░░░░░░░    11  ( 9%)          │ │     │
│   │   │  ❌ NÃO-LEAD       ▮▮▮▮▮▮▮▮▮▮▮▮▮▮░░░░░     41  (34%)          │ │     │
│   │   │  ❓ INCERTO        ▮▮░░░░░░░░░░░░░░░░░░     6  ( 5%)          │ │     │
│   │   │  🔄 ENRIQUECER    ▮░░░░░░░░░░░░░░░░░░░     2  ( 2%)          │ │     │
│   │   │  ⏭ SKIP           ▮░░░░░░░░░░░░░░░░░░░     0  ( 0%)          │ │     │
│   │   └──────────────────────────────────────────────────────────────┘ │     │
│   │                                                                    │     │
│   │   confiança média: 0.78 (alta)  ·  skip rate 0%                    │     │
│   │                                                                    │     │
│   │   [📥 EXPORTAR CSV]    [+ NOVO LOTE]    [voltar à lista]            │     │
│   ╰────────────────────────────────────────────────────────────────────╯     │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Erro de save (HTMX falha no POST de decisão)

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  ✗  FALHA AO SALVAR DECISÃO                                      │
   │     status 502 · BadGateway · timeout em /api/anotacoes/         │
   │     sua decisão NÃO foi perdida — está no buffer local           │
   │                                                                  │
   │     [↻ TENTAR NOVAMENTE]     [ver log]     [trocar item]         │
   └──────────────────────────────────────────────────────────────────┘
   (banner danger sticky no topo do card, não auto-advance até resolver)
```

### 4.4 Sem permissão (403)

```
                ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
                ┃                                            ┃
                ┃              4 0 3                         ┃   ← .error-code
                ┃                                            ┃     gigante
                ┃        RESTRICTED SECTOR                   ┃
                ┃                                            ┃
                ┃   você não tem permissão pra anotar leads  ┃
                ┃   solicite acesso ao administrador         ┃
                ┃                                            ┃
                ┃   [voltar ao dashboard]                    ┃
                ┃                                            ┃
                ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                (segue padrão Voyager 403 — `error-code` + star-field)
```

### 4.5 Sem rede / HTMX falha (overlay sobre o card)

```
   ┌──────────────────────────────────────────────────────────────────┐
   │                                                                  │
   │             ((•)) ACQUIRING SIGNAL                                │
   │                                                                  │
   │             sem resposta da estação · 8s                         │
   │             tentando reconectar…  ▮▮▮░░                          │
   │                                                                  │
   │             [↻ tentar agora]                                     │
   │                                                                  │
   └──────────────────────────────────────────────────────────────────┘
   (overlay com .scanlines + pulsar amarelo, bloqueia interação)
```

---

## 5. Overlay de hotkeys (`?`)

Modal centralizado (`_partials/modal.html`, id=`hotkeys`).

```
   ┌─ HOTKEYS ─────────────────────────────────────────────────  [esc / × ] ┐
   │                                                                        │
   │  DECISÃO                                                               │
   │  ┌─────┐  💎  Precatório                                               │
   │  │  1  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────┐  ⏳  Pré-precatório                                            │
   │  │  2  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────┐  🌱  Direito creditório                                       │
   │  │  3  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────┐  ❌  Não-lead                                                  │
   │  │  4  │                                                               │
   │  └─────┘                                                               │
   │                                                                        │
   │  AUXILIARES                                                            │
   │  ┌─────┐  ❓  Incerto                                                   │
   │  │  I  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────┐  🔄  Enriquecer (re-classifica depois)                        │
   │  │  E  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────┐  ⏭  Skip (pula sem decidir)                                  │
   │  │  S  │                                                               │
   │  └─────┘                                                               │
   │                                                                        │
   │  NAVEGAÇÃO                                                             │
   │  ┌─────┐  ←  item anterior                                             │
   │  │  J  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────┐  →  próximo item                                              │
   │  │  K  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────────┐  desfazer última anotação (janela 2s)                     │
   │  │ Ctrl+Z  │                                                           │
   │  └─────────┘                                                           │
   │                                                                        │
   │  OUTROS                                                                │
   │  ┌─────┐  abre/fecha esta ajuda                                        │
   │  │  ?  │                                                               │
   │  └─────┘                                                               │
   │  ┌─────────┐  confirma decisão (qdo já há botão selecionado)           │
   │  │  Enter  │                                                           │
   │  └─────────┘                                                           │
   │                                                                        │
   │  Atalho global: g h home · g p processos · / busca · t tema             │
   └────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Modal "criar novo lote"

```
   ┌─ NOVO LOTE DE ANOTAÇÃO ─────────────────────────────────────  [× esc]  ┐
   │                                                                        │
   │  ESTRATÉGIA                                                            │
   │  ( ) mining-fn          alta suspeita de falso negativo                │
   │                         (2+ estratégias batem, score 0.6–0.95)         │
   │  ( ) top-score          top-N classificados (validação precision)      │
   │  (•) random-naolead     amostra controle NL (≈recall check)            │
   │  ( ) random-top         amostra top decil (calibração D1)              │
   │  ( ) ad-hoc CSV         upload de lista de CNJs                        │
   │                                                                        │
   │  TRIBUNAL                                                              │
   │  [TRF1•] [TRF3 ] [TJSP ] [TJMG ] [todos ]                              │
   │                                                                        │
   │  TAMANHO                                                               │
   │  [ 50 ]  [ 100• ] [ 200 ] [ 500 ]   ou:  [ ___ ] custom                │
   │                                                                        │
   │  PERÍODO (autuação)                                                    │
   │  [ todo•]  [ 90d ]  [ 1ano ]  [ desde 2023 ]                           │
   │                                                                        │
   │  ┌──────────────────────────────────────────────────────────────────┐ │
   │  │ Estimativa: ~52 min  ·  31s/item média do operador                │ │
   │  └──────────────────────────────────────────────────────────────────┘ │
   │                                                                        │
   │                                       [cancelar]    [▶ CRIAR LOTE]    │
   └────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Tabela de componentes

| Componente                       | Partial novo?                 | Reutilizado?                          | Endpoint backend                                | Loading                                  | Erro                              |
|----------------------------------|-------------------------------|---------------------------------------|-------------------------------------------------|------------------------------------------|-----------------------------------|
| Lista de lotes ativos            | `_lotes_ativos.html`          | `kpi.html` (mini), `badge.html`       | `GET /dashboard/leads/validacao/` (HTMX shell)  | overlay `acquiring data` no `-list`      | banner danger + retry             |
| Histórico de lotes               | `_lotes_historico.html`       | `pagination.html`                     | `?historico=1&page=N` (HTMX)                    | skeleton table                           | empty_state                       |
| Card de validação                | **`_validacao_card.html`** ✦  | `badge.html`, `chip.html`, `_parte_row.html` | render server-side em `validacao_lote.html` | n/a (já renderizado)                     | placeholder "carregando processo" |
| Banner mining-signal             | `_mining_banner.html`         | `badge.html`, `chip.html`             | embutido no card via context                    | n/a                                      | omitido se sem sinal              |
| Score breakdown (top 5)          | reusa estilo do `processo_detail.html` (extrai pra `_score_breakdown.html`) | `badge.html`             | embutido                                        | n/a                                      | "score indisponível"              |
| Form de decisão                  | `_decisao_form.html`          | —                                     | `POST /dashboard/leads/validacao/<lote>/decidir/` (HTMX swap card) | botão spinner + disable | banner sticky "FALHA SALVAR" + retry |
| Hotkeys overlay                  | `_hotkeys_overlay.html`       | `modal.html`                          | static (sem fetch)                              | n/a                                      | n/a                               |
| Modal novo lote                  | `_novo_lote_modal.html`       | `modal.html`, `chip.html`             | `POST /dashboard/leads/validacao/criar/` (HTMX) | botão spinner                            | inline error nos campos           |
| Estado vazio                     | inline                        | `empty_state.html`                    | —                                               | n/a                                      | n/a                               |
| Estado concluído                 | `_lote_concluido.html`        | `badge.html`                          | embutido na response do POST decidir quando `lote.terminado` | n/a            | n/a                               |
| 403                              | `403.html` existente          | —                                     | Django middleware                               | n/a                                      | n/a                               |
| Auto-advance toast               | inline (Alpine `$dispatch`)   | `toast_container.html`                | —                                               | fade-in 80ms                             | persistente até retry             |

✦ = partial cuja existência foi exigida no briefing.

---

## 8. Especificação de atalhos

| Tecla       | Ação                                          | Mobile? | Notas                                              |
|-------------|-----------------------------------------------|---------|----------------------------------------------------|
| `1`         | Decide 💎 Precatório                          | não     | foco vai pra confiança após pressionar             |
| `2`         | Decide ⏳ Pré-precatório                      | não     | idem                                               |
| `3`         | Decide 🌱 Direito creditório                  | não     | idem                                               |
| `4`         | Decide ❌ Não-lead                            | não     | idem                                               |
| `I`         | Decide ❓ Incerto                             | não     | abre textarea motivo automaticamente               |
| `E`         | Decide 🔄 Enriquecer (manda pra fila enricher)| não     | não conta como anotação no skip rate              |
| `S`         | Decide ⏭ Skip                                 | não     | conta no skip rate (alerta se >10%)               |
| `J`         | Item anterior                                 | não     | só funciona se já decidiu o atual                  |
| `K`         | Próximo item                                  | não     | requer decisão (ou skip explícito)                 |
| `Ctrl+Z`    | Desfazer última decisão                       | não     | janela 2s após auto-advance                        |
| `Enter`     | Confirma decisão selecionada                  | não     | quando já há botão highlight                       |
| `?`         | Abre/fecha overlay de hotkeys                 | não     | toggle                                             |
| `Esc`       | Fecha modal/overlay aberto                    | sim     | tap fora também fecha (mobile)                     |
| `Tab/Shift+Tab` | Navega entre confiança / motivo            | parcial | mobile usa toque                                   |

**Mobile**: substitui atalhos por botões grandes (44×44 px min), sem keyboard listeners.
Swipe horizontal: **descartado** no MVP (risco de gesto acidental — usuário pode passar
item sem decidir). Reavaliar após telemetria de uso.

---

## 9. Fluxo de navegação

```
                       ┌─────────────────────────────┐
                       │ /dashboard/leads/validacao/ │
                       │  (overview)                 │
                       └──────────────┬──────────────┘
                                      │
                  ┌───────────────────┼────────────────────┐
                  │                   │                    │
            [+ NOVO LOTE]      [▶ CONTINUAR ⏎]      [histórico → ler]
                  │                   │                    │
                  ▼                   ▼                    ▼
        ┌──────────────────┐  ┌───────────────────┐ ┌────────────────┐
        │ modal estratégia │  │ /…validacao/<id>/ │ │ summary readonly│
        │  + tamanho       │  │  item 1 / N       │ └────────────────┘
        └────────┬─────────┘  └─────────┬─────────┘
                 │ POST criar           │ decide (1..4/I/E/S)
                 ▼                      ▼
        ┌──────────────────┐  ┌───────────────────┐
        │ lote criado      │  │ POST /decidir/    │
        │ redirect →       │  │ HTMX swap card    │
        └────────┬─────────┘  └─────────┬─────────┘
                 │                      │
                 └──────────────────────▶───────── auto-advance 200ms ────┐
                                        │                                  │
                                        ▼                                  │
                              ┌───────────────────┐                        │
                              │  item 2 / N       │  ←── Ctrl+Z volta item │
                              └─────────┬─────────┘                        │
                                        │  …                               │
                                        ▼                                  │
                              ┌───────────────────┐                        │
                              │  item N / N       │                        │
                              └─────────┬─────────┘                        │
                                        │  decide                          │
                                        ▼                                  │
                              ┌───────────────────┐                        │
                              │  LOTE CONCLUÍDO   │                        │
                              │  summary + CTA    │──── [novo lote] ──┐    │
                              └─────────┬─────────┘                   │    │
                                        │ [voltar à lista]            │    │
                                        ▼                             │    │
                              ┌───────────────────┐                   │    │
                              │  overview         │ ◀─────────────────┘    │
                              └───────────────────┘                        │
                                                                           │
   estados especiais (qualquer ponto):                                     │
     - sem rede   → overlay "ACQUIRING SIGNAL" + retry  ─────────────────▶─┤
     - 403        → página RESTRICTED SECTOR                               │
     - erro save  → banner sticky, não avança até resolver  ──────────────┘
```

---

## 10. Decisões de UX (com justificativa)

### 10.1 Auto-advance 200ms (e não 0, e não 500)

- **0ms**: feedback visual nulo. Operador vê o próximo card aparecer e fica em dúvida se
  a anotação foi salva. Gera ansiedade, leva a verificar histórico, derruba throughput.
- **200ms**: tempo suficiente pra renderizar um checkmark verde discreto (toast/inline),
  o cérebro registra "salvou" sem sentir lentidão. Pesquisa de HCI clássica
  (Nielsen, ~100ms = instantâneo, ~1s = fluido) — 200ms é o sweet spot pra confirmar.
- **500ms+**: o operador percebe espera. A 100 anotações/h o overhead vira 50s/h só de
  delay. Inaceitável.

### 10.2 Undo 2s (e não 5s, e não infinito)

- **2s** = janela em que a memória de curto prazo ainda guarda "o que acabei de clicar".
  Erros típicos (digitou 2 em vez de 3) são percebidos em <1s.
- **5s** seria seguro mas tornaria o auto-advance hesitante — pra implementar "undo
  seguro" 5s, a anotação precisa ficar em estado pendente e o próximo item já não pode
  ser submetido. Cria fila de pending writes. Complexidade alta pra ganho marginal.
- **Infinito** existe via tela de histórico (corrigir depois). Undo na hora é só pra
  digitação errada.
- Trade-off: se operador errar e demorar 3s pra perceber, vai precisar achar o item no
  histórico do lote e re-anotar. Aceitável dado o alvo de 100/h.

### 10.3 7 botões (e não 4 binário)

- **Razão produto**: queremos sinal pra três coisas distintas no pipeline:
  1. **classificação real** (precatório/pré/dc/não-lead) — alimenta re-treino v6+
  2. **incerto** — não polui ground truth com ruído humano + sinaliza necessidade de
     mais features no modelo (categoria com >5% incerto → revisar)
  3. **enriquecer** — operador detectou que falta dado (sem movs recentes, classe
     missing) → manda pra fila do enricher, retorna depois
  4. **skip** — operador não quer/pode decidir agora (cansado, processo bizarro) →
     **não vira ground truth** + telemetria de fadiga
- **4 botões** misturariam "incerto" com "não-lead", o que polui treino e mascara
  pontos cegos do modelo.
- Custo da granularidade: 7 atalhos em vez de 4. Mitigado com agrupamento visual
  (4 principais numerados, 3 auxiliares por letra).

### 10.4 Score breakdown sempre visível (desktop) / colapsado (mobile)

- **Desktop visível**: operador precisa do *contexto* do modelo pra decidir bem. Ver
  "F1=Cumprimento +1.92" enquanto lê a classe ajuda a calibrar o julgamento ("ah, o
  modelo já pegou o forte"). Esconder seria pedir scroll/clique a cada item — perda de
  velocidade.
- **Mobile colapsado**: tela pequena, prioridade é o CNJ + banner mining + botões.
  Quem quiser ver feats expande. 90% dos casos não precisam (decisão vem do banner FN
  + classe).
- **Mostra TOP 5, não TOP 12**: 5 cobre >80% da contribuição na maioria dos processos
  (insight do treino v5: pesos altos concentrados em F1, F15, F1×F15, F18, F21).
  Lista de 12 ocupa tela demais. Link "ver todas →" libera o detalhe sob demanda.

### 10.5 Textarea motivo: opcional sempre, mas auto-expandida pra `❓ INCERTO`

- **Obrigatória sempre** → atrito enorme, mata os 100/h.
- **Opcional sempre, sem dica** → operador nunca preenche, perdemos contexto valioso
  pros casos ambíguos.
- **Híbrido (decisão final)**: opcional pra 1/2/3/4 (decisão clara, motivo deduz-se),
  **auto-expande** quando aperta `I` (incerto) ou `🔄 Enriquecer`. Operador ainda pode
  enviar vazio (Enter direto), mas a caixa já está visível e o cursor lá → forte
  incentivo a escrever 1-2 palavras ("classe atípica", "movs cortadas", etc.). Dados
  ficam etiquetados pra re-treino futuro priorizar features.

---

## Anotações finais (pra implementação)

- O CSS do `card` já existe (`DASHBOARD.md` lista tokens). Reusar `bg-card`, `border-border`,
  `text-fg-soft`, `bg-accent/15`. Nunca cores literais (`bg-zinc-900`).
- Banner mining-signal usa `bg-warning/15 border-warning/30 text-warning` (variant `warning`
  já existe em `badge.html`).
- Score breakdown extrai `processo_detail.html` linhas 217–253 pra novo partial
  `_partials/_score_breakdown.html` parametrizado por `top_n` (default 5) — uma única
  fonte da verdade.
- Toast de confirmação reusa `toast_container.html` já no `base.html`.
- Mobile breakpoint `md:` (768px) — abaixo, esconder coluna de hotkeys hint no header,
  colapsar score breakdown via `<details>`.
- Atalhos: listener em `base.html` já existe pra `g h`/`g p`/`?` — adicionar listener
  **scoped à página** (`{% block extra_js %}`) e *desabilitá-lo* quando modal aberto
  ou foco em textarea (`document.activeElement.tagName === 'TEXTAREA'`).
