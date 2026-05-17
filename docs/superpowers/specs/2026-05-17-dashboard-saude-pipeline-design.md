# Dashboard de saúde do pipeline de ingestão — Design

Data: 2026-05-17
Status: aprovado p/ implementação (pendente review do spec)

## Motivação

O dashboard atual de overview mostrou "Sem ingestão nas últimas 24h" e não havia
forma rápida de saber se era incidente ou comportamento normal (era fim de semana —
DJEN não publica sáb/dom). Investigação também revelou um **bug latente**:
`_dia_coberto` (`djen/jobs.py:370`) marca um dia como coberto checando só
`status=success`, ignorando `movimentacoes_novas`/`paginas_lidas`; combinado com o
cron movido pra 02:00/02:30 (commit `43a661e`) e 403 de proxy transitórios, um run
vazio em dia útil pode sombrear o dia pra sempre (overlap de 3 dias não retenta),
invisível ao watchdog. Uma dashboard de saúde por fonte × tribunal × dia tornaria
esse tipo de falha imediatamente visível.

> O fix do bug latente é **fora de escopo** deste spec (tracking separado), mas a
> dashboard é desenhada pra detectá-lo.

## Objetivo

Página dedicada que mostra, por tribunal e por dia, o volume de cada fonte do
pipeline com as **métricas nativas de cada fonte** (sem normalização forçada),
servindo simultaneamente como (a) painel de saúde com detecção de anomalia e
(b) ferramenta analítica de volume/tendência.

## Escopo

### Dentro
- Nova rota separada; `/dashboard/ingestao/` atual fica intacta.
- Tribunais: todos `Tribunal.ativo=True` (derivado, não hardcoded).
- 4 fontes/estágios: DJEN, Datajud, Enriquecimento PJe, Classificação ML.
- Janela seletável 30/90/180 dias (grade); seletor 30/90/180/365 na analítica.
- Detecção de anomalia (verde/amarelo/vermelho/cinza) por célula.
- MV nova criada via **migration Django** (não na mão em prod).

### Fora (não-objetivos)
- Calendário de feriados forenses (feriado → falso-vermelho assumido, documentado).
- Fix do bug latente `_dia_coberto` (tracking separado).
- Alertas externos (Slack/Sentry) — só visual nesta entrega.
- Absorver/substituir a `/dashboard/ingestao/` existente.

## Arquitetura

### Rota / nav
- `path('ingestao/saude/', views.ingestao_saude, name='ingestao-saude')` em
  `dashboard/urls.py`.
- Link cruzado: botão "Saúde do pipeline" no header de `ingestao`; "Detalhe de
  runs" na nova página.
- Item de nav em `dashboard/templates/dashboard/base.html` ao lado de "Ingestão".
- Decorator de acesso: mesmo padrão/decorator que `views.ingestao` já usa.

### Camada de dados (híbrido)

**DJEN — live, de `IngestionRun`** (tabela pequena; "hoje" reflete na hora):
- Filtro: `status=success`, `janela_inicio == janela_fim` (runs de 1 dia do
  fan-out diário).
- Agregação por `(tribunal, janela_inicio)`:
  - `encontradas = MAX(movimentacoes_novas + movimentacoes_duplicadas)`
  - `novas = MAX(movimentacoes_novas)`
  - `duplicadas = MAX(movimentacoes_duplicadas)`
  - `paginas = MAX(paginas_lidas)`
  - `runs = COUNT(*)`
- **MAX, não SUM**: overlap re-roda o mesmo dia de forma idempotente; somar
  contaria em dobro.

**Datajud / PJe / Classificação — MV `mv_pipeline_diario`**:
- Schema: `(tribunal_id int, dia date, fonte text, processos int)`; uma linha por
  `(tribunal, dia, fonte)`, `fonte ∈ {datajud, pje, classif}`.
- Definição (UNION de 3):
  ```sql
  SELECT tribunal_id, data_enriquecimento_datajud::date AS dia, 'datajud' AS fonte,
         COUNT(*) AS processos
    FROM tribunals_process
   WHERE data_enriquecimento_datajud IS NOT NULL
   GROUP BY 1,2
  UNION ALL
  SELECT tribunal_id, enriquecido_em::date, 'pje', COUNT(*)
    FROM tribunals_process WHERE enriquecido_em IS NOT NULL GROUP BY 1,2
  UNION ALL
  SELECT tribunal_id, classificacao_em::date, 'classif', COUNT(*)
    FROM tribunals_process WHERE classificacao_em IS NOT NULL GROUP BY 1,2
  ```
- **Migration** em `tribunals/migrations/` (`atomic=False`), `RunSQL`/`reverse_sql`
  idempotente:
  - `CREATE MATERIALIZED VIEW IF NOT EXISTS mv_pipeline_diario AS ...`
  - `CREATE UNIQUE INDEX ... ON mv_pipeline_diario (tribunal_id, dia, fonte)`
    (necessário p/ `REFRESH ... CONCURRENTLY`)
  - `CREATE INDEX CONCURRENTLY IF NOT EXISTS ... ON tribunals_process
    (data_enriquecimento_datajud)` (os índices de `enriquecido_em` e
    `classificacao_em` já existem)
  - `CONCURRENTLY` + `atomic=False`: tabela de 8,5M linhas, senão lock no deploy.
- Refresh:
  - Adicionar `'mv_pipeline_diario'` à tupla de `refresh_materialized_views`
    (cron diário 03:00, já existente em `dashboard/tasks.py`).
  - Novo warm `warm_pipeline_diario` registrado no scheduler (`djen/scheduler.py`)
    a cada 1h: `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario` com
    `SET lock_timeout='5s'`, `_with_lock`, padrão idêntico aos warm existentes.

### Regra de anomalia
Calculada em `queries.py` (Python, sobre o agregado já pronto — não no SQL da MV):
- Baseline por `(tribunal, fonte)` = mediana móvel das últimas 4 ocorrências do
  mesmo tipo de dia (útil vs fim de semana).
- **Verde**: volume ≥ 60% da baseline.
- **Amarelo**: 20–60% da baseline.
- **Vermelho**: < 20% da baseline E dia em que se esperava volume.
- **Cinza (esperado vazio)**: fim de semana para DJEN/Datajud.
- Limitação documentada: feriado forense → falso-vermelho (sem calendário).

### Layout (design system Voyager, HTMX + ECharts)
```
Dashboard de saúde do pipeline       [30d|90d|180d]  [tribunal chips]
┌ KPI strip (5) ─────────────────────────────────────────────────┐
│ Dias OK | Anomalias 24h | Última ing. DJEN | Datajud lag | Classif lag │
└─────────────────────────────────────────────────────────────────┘
┌ Heatmap saúde — linhas = tribunal×fonte, colunas = dias ────────┐
│ TRF1·DJEN    ▓▓▓░▓▓ …  (cor = saúde; tooltip = métricas nativas) │
│ TRF1·Datajud ▓▓▓▓▓▓ …                                           │
└─────────────────────────────────────────────────────────────────┘
┌ Analítica: volume/dia por fonte (stacked) — seletor 30/90/180/365 │
└─────────────────────────────────────────────────────────────────┘
┌ Tabela por tribunal: totais período + sparkline + último dia OK ─┐
└─────────────────────────────────────────────────────────────────┘
```
- Endpoints de chart reusam o dispatcher `path('api/chart/<key>/')`.
- Skeleton "acquiring signal" + `lazyChart` + cache warm, igual às demais páginas.

### Módulo de queries / views
- `dashboard/queries.py`:
  - `pipeline_saude_grid(dias, tribunais)` — DJEN live + leitura `mv_pipeline_diario`,
    aplica baseline/cor.
  - `pipeline_volume_temporal(dias, tribunais)` — série stacked por fonte.
  - `pipeline_kpis()` — strip de 5 KPIs.
  - Cache warm keys `chart:pipeline-*`.
- `dashboard/views.py`: `ingestao_saude` + handlers registrados no
  `_CHART_HANDLERS` existente.

## Testes (pytest, padrão do projeto)
- DJEN: agregação usa MAX (não SUM) sob overlap (vários runs mesmo dia).
- Anomalia: vermelho em dia útil com 0; cinza em fim de semana; baseline com
  mediana móvel de 4.
- MV: popula as 3 fontes corretamente a partir dos timestamps de `Process`.
- View: smoke 200 autenticado; 403/redirect sem permissão.

## Docs (mesma PR)
- `.ia/DASHBOARD.md` — página nova + componentes.
- `.ia/INGESTION.md` — MV `mv_pipeline_diario` + warm.
- `.ia/OPS.md` — runbook: refresh manual do MV; o que cada cor significa.
- `.ia/DECISIONS.md` — ADR: híbrido live+MV; MV via migration (corrige
  prod-diverge-do-git).

## Riscos / decisões
- MV refresh sobre 8,5M linhas: usar `CONCURRENTLY` + `lock_timeout`, mesmo padrão
  já operando em `mv_volume_diario`/`mv_ingestion_rate_hora`.
- Migration `CONCURRENTLY` precisa `atomic=False` — gotcha conhecido de deploy.
- Feriados não tratados — falso-vermelho aceito e documentado (YAGNI).
