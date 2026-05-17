# Dashboard de saúde do pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Página `/dashboard/ingestao/saude/` mostrando volume por fonte (DJEN, Datajud, PJe, Classificação) × tribunal × dia, com detecção de anomalia e analítica de tendência.

**Architecture:** Híbrido — DJEN lido live de `IngestionRun` (agregação MAX por dia); Datajud/PJe/Classificação de uma MV nova `mv_pipeline_diario` (criada via migration, refresh no cron diário existente + warm 1h). Camada de queries em `dashboard/queries.py`, view+handlers no padrão `_CHART_HANDLERS`, template HTMX/ECharts no design system Voyager.

**Tech Stack:** Django, PostgreSQL (materialized view, REFRESH CONCURRENTLY), pytest, APScheduler, HTMX, Alpine, ECharts.

**Spec:** `docs/superpowers/specs/2026-05-17-dashboard-saude-pipeline-design.md`

---

## File Structure

- Create: `tribunals/migrations/0029_mv_pipeline_diario.py` — MV + índice único + índice em `data_enriquecimento_datajud`.
- Modify: `dashboard/queries.py` — `pipeline_saude_grid`, `pipeline_volume_temporal`, `pipeline_kpis`, helper `_classificar_celula`.
- Modify: `dashboard/tasks.py` — add `'mv_pipeline_diario'` ao `refresh_materialized_views`; novo `warm_pipeline_diario`.
- Modify: `djen/scheduler.py` — registrar `warm_pipeline_diario` (interval 1h).
- Modify: `dashboard/views.py` — `ingestao_saude` + 3 chart handlers no `_CHART_HANDLERS`.
- Modify: `dashboard/urls.py` — rota `ingestao/saude/`.
- Create: `dashboard/templates/dashboard/ingestao_saude.html`.
- Modify: `dashboard/templates/dashboard/base.html` — item de nav.
- Modify: `dashboard/templates/dashboard/ingestao.html` — link cruzado.
- Test: `tests/test_pipeline_saude.py`.
- Docs: `.ia/DASHBOARD.md`, `.ia/INGESTION.md`, `.ia/OPS.md`, `.ia/DECISIONS.md`.

---

### Task 1: Migration da MV `mv_pipeline_diario`

**Files:**
- Create: `tribunals/migrations/0029_mv_pipeline_diario.py`
- Test: `tests/test_pipeline_saude.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline_saude.py
import pytest
from datetime import date, datetime, timezone
from django.db import connection
from tribunals.models import Tribunal, Process

@pytest.mark.django_db
def test_mv_pipeline_diario_popula_tres_fontes():
    t = Tribunal.objects.create(sigla='TST', sigla_djen='TST', nome='Teste', ativo=True)
    dt = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    Process.objects.create(tribunal=t, numero_cnj='1', data_enriquecimento_datajud=dt)
    Process.objects.create(tribunal=t, numero_cnj='2', enriquecido_em=dt)
    Process.objects.create(tribunal=t, numero_cnj='3', classificacao_em=dt)
    with connection.cursor() as c:
        c.execute('REFRESH MATERIALIZED VIEW mv_pipeline_diario')
        c.execute("SELECT fonte, processos FROM mv_pipeline_diario "
                  "WHERE tribunal_id=%s AND dia=%s ORDER BY fonte", [t.id, date(2026,5,15)])
        rows = dict(c.fetchall())
    assert rows == {'classif': 1, 'datajud': 1, 'pje': 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline_saude.py::test_mv_pipeline_diario_popula_tres_fontes -v`
Expected: FAIL — `relation "mv_pipeline_diario" does not exist`.

- [ ] **Step 3: Write the migration**

```python
# tribunals/migrations/0029_mv_pipeline_diario.py
from django.db import migrations

CREATE = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_pipeline_diario AS
  SELECT tribunal_id, data_enriquecimento_datajud::date AS dia,
         'datajud'::text AS fonte, COUNT(*)::int AS processos
    FROM tribunals_process
   WHERE data_enriquecimento_datajud IS NOT NULL GROUP BY 1,2
  UNION ALL
  SELECT tribunal_id, enriquecido_em::date, 'pje', COUNT(*)::int
    FROM tribunals_process WHERE enriquecido_em IS NOT NULL GROUP BY 1,2
  UNION ALL
  SELECT tribunal_id, classificacao_em::date, 'classif', COUNT(*)::int
    FROM tribunals_process WHERE classificacao_em IS NOT NULL GROUP BY 1,2;
CREATE UNIQUE INDEX IF NOT EXISTS mv_pipeline_diario_uniq
  ON mv_pipeline_diario (tribunal_id, dia, fonte);
"""
DROP = "DROP MATERIALIZED VIEW IF EXISTS mv_pipeline_diario;"
IDX = ("CREATE INDEX CONCURRENTLY IF NOT EXISTS proc_datajud_em_idx "
       "ON tribunals_process (data_enriquecimento_datajud);")
IDX_DROP = "DROP INDEX CONCURRENTLY IF EXISTS proc_datajud_em_idx;"


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('tribunals', '0028_leadconsumption_lote_id')]
    operations = [
        migrations.RunSQL(CREATE, DROP),
        migrations.RunSQL(IDX, IDX_DROP),
    ]
```

- [ ] **Step 4: Run migration + test**

Run: `python manage.py migrate tribunals && pytest tests/test_pipeline_saude.py::test_mv_pipeline_diario_popula_tres_fontes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tribunals/migrations/0029_mv_pipeline_diario.py tests/test_pipeline_saude.py
git commit -m "feat(ingestao): MV mv_pipeline_diario (datajud/pje/classif por tribunal/dia)"
```

---

### Task 2: `_classificar_celula` (regra de anomalia)

**Files:**
- Modify: `dashboard/queries.py` (adicionar função no fim do módulo)
- Test: `tests/test_pipeline_saude.py`

- [ ] **Step 1: Write the failing test**

```python
from dashboard.queries import _classificar_celula

def test_classificar_celula():
    # baseline = mediana de [100,100,100,100] = 100
    base = [100, 100, 100, 100]
    assert _classificar_celula(90, base, dia_util=True) == 'verde'
    assert _classificar_celula(40, base, dia_util=True) == 'amarelo'
    assert _classificar_celula(5, base, dia_util=True) == 'vermelho'
    # fim de semana sem volume esperado -> cinza, nunca vermelho
    assert _classificar_celula(0, base, dia_util=False) == 'cinza'
    # sem baseline (fonte nova) -> cinza, não alarma
    assert _classificar_celula(0, [], dia_util=True) == 'cinza'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline_saude.py::test_classificar_celula -v`
Expected: FAIL — `cannot import name '_classificar_celula'`.

- [ ] **Step 3: Implement**

```python
# dashboard/queries.py  (fim do arquivo)
from statistics import median

def _classificar_celula(volume, baseline_amostras, dia_util):
    """Cor de saúde de uma célula (tribunal,fonte,dia).

    baseline = mediana das últimas amostras do mesmo tipo de dia.
    Sem baseline -> 'cinza' (não alarma fonte nova/sem histórico).
    Fim de semana -> 'cinza' (DJEN/Datajud não publicam).
    """
    if not dia_util:
        return 'cinza'
    if not baseline_amostras:
        return 'cinza'
    base = median(baseline_amostras)
    if base <= 0:
        return 'cinza'
    ratio = volume / base
    if ratio >= 0.60:
        return 'verde'
    if ratio >= 0.20:
        return 'amarelo'
    return 'vermelho'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline_saude.py::test_classificar_celula -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/queries.py tests/test_pipeline_saude.py
git commit -m "feat(ingestao): regra de anomalia _classificar_celula"
```

---

### Task 3: `pipeline_saude_grid` (DJEN live + MV + cor)

**Files:**
- Modify: `dashboard/queries.py`
- Test: `tests/test_pipeline_saude.py`

- [ ] **Step 1: Write the failing test**

```python
from dashboard.queries import pipeline_saude_grid
from tribunals.models import IngestionRun

@pytest.mark.django_db
def test_pipeline_saude_grid_djen_usa_max_nao_sum():
    t = Tribunal.objects.create(sigla='TSU', sigla_djen='TSU', nome='T', ativo=True)
    d = date(2026, 5, 15)
    # overlap: dois runs success do MESMO dia -> deve usar MAX, não somar
    for novas in (10, 12):
        IngestionRun.objects.create(tribunal=t, status='success',
            janela_inicio=d, janela_fim=d, movimentacoes_novas=novas,
            movimentacoes_duplicadas=3, paginas_lidas=2, finished_at=datetime(2026,5,15,3,tzinfo=timezone.utc))
    grid = pipeline_saude_grid(dias=30, tribunais=[t.id])
    djen = [c for c in grid if c['tribunal_id']==t.id and c['fonte']=='djen' and c['dia']==d][0]
    assert djen['novas'] == 12          # MAX(10,12), não 22
    assert djen['encontradas'] == 15    # MAX(novas+dup) = 12+3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline_saude.py::test_pipeline_saude_grid_djen_usa_max_nao_sum -v`
Expected: FAIL — `cannot import name 'pipeline_saude_grid'`.

- [ ] **Step 3: Implement**

```python
# dashboard/queries.py
from datetime import date, timedelta
from django.db import connection
from django.db.models import Max, Count, F
from tribunals.models import IngestionRun

_FONTES_MV = ('datajud', 'pje', 'classif')

def pipeline_saude_grid(dias=30, tribunais=None):
    """Lista de células {tribunal_id, fonte, dia, ...metricas, cor}.

    DJEN: live de IngestionRun (MAX por dia — overlap idempotente).
    datajud/pje/classif: de mv_pipeline_diario.
    """
    hoje = date.today()
    inicio = hoje - timedelta(days=dias)

    djen_qs = (IngestionRun.objects
        .filter(status=IngestionRun.STATUS_SUCCESS,
                janela_inicio=F('janela_fim'), janela_inicio__gte=inicio))
    if tribunais:
        djen_qs = djen_qs.filter(tribunal_id__in=tribunais)
    djen_rows = (djen_qs
        .values('tribunal_id', 'janela_inicio')
        .annotate(novas=Max('movimentacoes_novas'),
                  duplicadas=Max('movimentacoes_duplicadas'),
                  paginas=Max('paginas_lidas'),
                  runs=Count('id')))

    cells = []
    for r in djen_rows:
        cells.append({
            'tribunal_id': r['tribunal_id'], 'fonte': 'djen',
            'dia': r['janela_inicio'], 'novas': r['novas'],
            'duplicadas': r['duplicadas'], 'paginas': r['paginas'],
            'runs': r['runs'],
            'encontradas': r['novas'] + r['duplicadas'],
            'volume': r['novas'] + r['duplicadas'],
        })

    where = ['dia >= %s']
    params = [inicio]
    if tribunais:
        where.append('tribunal_id = ANY(%s)')
        params.append(list(tribunais))
    sql = ('SELECT tribunal_id, dia, fonte, processos FROM mv_pipeline_diario '
           f'WHERE {" AND ".join(where)}')
    with connection.cursor() as cur:
        cur.execute(sql, params)
        for tid, dia, fonte, proc in cur.fetchall():
            cells.append({'tribunal_id': tid, 'fonte': fonte, 'dia': dia,
                           'volume': proc, 'processos': proc})

    # baseline por (tribunal,fonte,tipo_de_dia): últimas 4 amostras anteriores
    from collections import defaultdict
    series = defaultdict(list)
    for c in sorted(cells, key=lambda x: x['dia']):
        series[(c['tribunal_id'], c['fonte'], c['dia'].weekday() < 5)].append(c)
    for c in cells:
        dia_util = c['dia'].weekday() < 5
        hist = [s['volume'] for s in series[(c['tribunal_id'], c['fonte'], dia_util)]
                if s['dia'] < c['dia']][-4:]
        c['cor'] = _classificar_celula(c['volume'], hist, dia_util)
    return cells
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline_saude.py::test_pipeline_saude_grid_djen_usa_max_nao_sum -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/queries.py tests/test_pipeline_saude.py
git commit -m "feat(ingestao): pipeline_saude_grid (DJEN live MAX + MV + cor)"
```

---

### Task 4: `pipeline_volume_temporal` e `pipeline_kpis`

**Files:**
- Modify: `dashboard/queries.py`
- Test: `tests/test_pipeline_saude.py`

- [ ] **Step 1: Write the failing test**

```python
from dashboard.queries import pipeline_volume_temporal, pipeline_kpis

@pytest.mark.django_db
def test_pipeline_volume_temporal_e_kpis():
    t = Tribunal.objects.create(sigla='TVK', sigla_djen='TVK', nome='T', ativo=True)
    d = date(2026, 5, 15)
    IngestionRun.objects.create(tribunal=t, status='success', janela_inicio=d,
        janela_fim=d, movimentacoes_novas=50, movimentacoes_duplicadas=0,
        paginas_lidas=5, finished_at=datetime(2026,5,15,3,tzinfo=timezone.utc))
    serie = pipeline_volume_temporal(dias=30, tribunais=[t.id])
    pontos = [p for p in serie if p['fonte']=='djen' and p['dia']==d]
    assert pontos and pontos[0]['volume'] == 50
    k = pipeline_kpis()
    assert 'ultima_ingestao_djen' in k and 'anomalias_24h' in k
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline_saude.py::test_pipeline_volume_temporal_e_kpis -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

```python
# dashboard/queries.py
def pipeline_volume_temporal(dias=90, tribunais=None):
    """Série diária [{dia, fonte, volume}] somando todos os tribunais do filtro."""
    grid = pipeline_saude_grid(dias=dias, tribunais=tribunais)
    from collections import defaultdict
    agg = defaultdict(int)
    for c in grid:
        agg[(c['dia'], c['fonte'])] += c['volume']
    return [{'dia': d, 'fonte': f, 'volume': v}
            for (d, f), v in sorted(agg.items())]

def pipeline_kpis(tribunais=None):
    grid = pipeline_saude_grid(dias=30, tribunais=tribunais)
    hoje = date.today()
    ontem = hoje - timedelta(days=1)
    djen = [c for c in grid if c['fonte'] == 'djen']
    ultima = max((c['dia'] for c in djen), default=None)
    anomalias = sum(1 for c in grid
                    if c['dia'] >= ontem and c['cor'] == 'vermelho')
    def lag(fonte):
        ds = [c['dia'] for c in grid if c['fonte'] == fonte]
        return (hoje - max(ds)).days if ds else None
    return {
        'ultima_ingestao_djen': ultima,
        'anomalias_24h': anomalias,
        'datajud_lag_dias': lag('datajud'),
        'classif_lag_dias': lag('classif'),
        'dias_ok': sum(1 for c in djen if c['cor'] == 'verde'),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline_saude.py::test_pipeline_volume_temporal_e_kpis -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/queries.py tests/test_pipeline_saude.py
git commit -m "feat(ingestao): pipeline_volume_temporal + pipeline_kpis"
```

---

### Task 5: Warm job + refresh cron

**Files:**
- Modify: `dashboard/tasks.py:202` (tupla do `refresh_materialized_views`)
- Modify: `dashboard/tasks.py` (novo `warm_pipeline_diario` após `warm_ingestao_por_hora`)
- Modify: `djen/scheduler.py:139-156` (registrar no loop de warm)

- [ ] **Step 1: Add MV ao refresh diário**

Em `dashboard/tasks.py`, função `refresh_materialized_views`, trocar:
```python
for mv in ('mv_volume_diario', 'mv_ingestion_rate_hora'):
```
por:
```python
for mv in ('mv_volume_diario', 'mv_ingestion_rate_hora', 'mv_pipeline_diario'):
```

- [ ] **Step 2: Novo warm job**

Adicionar em `dashboard/tasks.py` após `warm_ingestao_por_hora`:
```python
@job('warm', timeout=900)
def warm_pipeline_diario():
    """REFRESH CONCURRENTLY mv_pipeline_diario — intraday, hoje/ontem fresco."""
    def _run():
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("SET statement_timeout = '600s'")
                cur.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario')
            logger.info('refresh MV mv_pipeline_diario ok (warm)')
        except Exception as e:
            logger.warning('warm_pipeline_diario: %s', e)
            _reset_connection()
    _with_lock('lock:warm_pipeline_diario', 900, _run)
```

- [ ] **Step 3: Registrar no scheduler**

Em `djen/scheduler.py`, import (junto dos outros warm) `warm_pipeline_diario`, e adicionar à tupla de warm jobs:
```python
(warm_pipeline_diario,       'warm_pipeline_diario',       {'hours': 1}),
```

- [ ] **Step 4: Verificar import/lint**

Run: `python -c "import dashboard.tasks, djen.scheduler"`
Expected: sem erro.

- [ ] **Step 5: Commit**

```bash
git add dashboard/tasks.py djen/scheduler.py
git commit -m "feat(ingestao): warm_pipeline_diario 1h + MV no refresh diário"
```

---

### Task 6: View, URL, chart handlers, nav

**Files:**
- Modify: `dashboard/views.py` (handlers + `ingestao_saude`)
- Modify: `dashboard/urls.py:23`
- Test: `tests/test_pipeline_saude.py`

- [ ] **Step 1: Write the failing test**

```python
from django.contrib.auth import get_user_model

@pytest.mark.django_db
def test_ingestao_saude_view_200(client):
    u = get_user_model().objects.create_user('w', password='x')
    client.force_login(u)
    resp = client.get('/dashboard/ingestao/saude/')
    assert resp.status_code == 200
    assert b'sa\xc3\xbade do pipeline' in resp.content.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline_saude.py::test_ingestao_saude_view_200 -v`
Expected: FAIL — 404 (rota não existe).

- [ ] **Step 3: Implement view + url + handlers**

`dashboard/urls.py`, após a linha `path('ingestao/', ...)`:
```python
    path('ingestao/saude/', views.ingestao_saude, name='ingestao-saude'),
```

`dashboard/views.py`, junto dos `_chart_*` handlers:
```python
def _chart_pipeline_grid(dias, tribunais, sigla):
    return queries.pipeline_saude_grid(dias=dias, tribunais=[sigla] if sigla else tribunais)

def _chart_pipeline_temporal(dias, tribunais, sigla):
    return queries.pipeline_volume_temporal(dias=dias, tribunais=[sigla] if sigla else tribunais)
```
No dict `_CHART_HANDLERS` adicionar:
```python
    'pipeline-grid': _chart_pipeline_grid,
    'pipeline-temporal': _chart_pipeline_temporal,
```
View (junto de `ingestao`):
```python
@login_required
@require_GET
def ingestao_saude(request):
    periodo_dias = _periodo_dias(request, default=30)
    tribunal_filtro = request.GET.get('tribunal', '')
    return render(request, 'dashboard/ingestao_saude.html', {
        'periodo_dias': periodo_dias,
        'tribunal_filtro': tribunal_filtro,
        'tribunais': Tribunal.objects.filter(ativo=True).order_by('sigla'),
        'kpis': queries.pipeline_kpis(
            tribunais=[tribunal_filtro] if tribunal_filtro else None),
    })
```

- [ ] **Step 4: Criar template mínimo**

Criar `dashboard/templates/dashboard/ingestao_saude.html` estendendo `base.html`, bloco `title` "Saúde do pipeline", `<h1>Dashboard de saúde do pipeline</h1>`, KPI strip com `{{ kpis.* }}`, chips de tribunal/período (copiar padrão de `ingestao.html`), e dois containers `data-echart` com `lazyChart` apontando pra `{% url 'dashboard:api-chart' 'pipeline-grid' %}` e `'pipeline-temporal'` (heatmap e stacked bar — reusar helpers de `base.html`; se não houver builder de heatmap, adicionar `buildPipelineHeatmap(d)` em `base.html` no padrão dos `build*` existentes).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_pipeline_saude.py::test_ingestao_saude_view_200 -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/views.py dashboard/urls.py dashboard/templates/dashboard/ingestao_saude.html dashboard/templates/dashboard/base.html tests/test_pipeline_saude.py
git commit -m "feat(ingestao): página /ingestao/saude/ + chart handlers + heatmap"
```

---

### Task 7: Nav, link cruzado e docs

**Files:**
- Modify: `dashboard/templates/dashboard/base.html` (item de nav)
- Modify: `dashboard/templates/dashboard/ingestao.html` (botão "Saúde do pipeline")
- Modify: `.ia/DASHBOARD.md`, `.ia/INGESTION.md`, `.ia/OPS.md`, `.ia/DECISIONS.md`

- [ ] **Step 1: Nav + link cruzado**

Em `base.html`, ao lado do link de "Ingestão" existente, adicionar link pra `{% url 'dashboard:ingestao-saude' %}` ("Saúde do pipeline"). Em `ingestao.html`, no header, botão linkando pra `{% url 'dashboard:ingestao-saude' %}`; e na nova página, link de volta pra `{% url 'dashboard:ingestao' %}` ("Detalhe de runs").

- [ ] **Step 2: Docs**

- `.ia/DASHBOARD.md`: nova seção da página `/dashboard/ingestao/saude/` (KPIs, heatmap, regra de cor).
- `.ia/INGESTION.md`: MV `mv_pipeline_diario` (definição, refresh diário 03:00 + warm 1h).
- `.ia/OPS.md`: runbook — refresh manual (`REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario`), significado das cores, limitação de feriado.
- `.ia/DECISIONS.md`: ADR — híbrido DJEN-live + MV; MV via migration (corrige prod-diverge-do-git); MAX-não-SUM no overlap; feriado fora de escopo.

- [ ] **Step 3: Rodar suíte completa do arquivo**

Run: `pytest tests/test_pipeline_saude.py -v`
Expected: todos PASS.

- [ ] **Step 4: Commit**

```bash
git add dashboard/templates/dashboard/base.html dashboard/templates/dashboard/ingestao.html .ia/
git commit -m "docs(ingestao): nav + link cruzado + .ia (DASHBOARD/INGESTION/OPS/DECISIONS)"
```

---

## Self-Review

**Spec coverage:**
- Rota separada + nav + link cruzado → Task 6, 7 ✓
- DJEN live MAX-não-SUM → Task 3 (teste explícito) ✓
- MV datajud/pje/classif via migration → Task 1 ✓
- Refresh diário + warm 1h → Task 5 ✓
- Regra de anomalia (verde/amarelo/vermelho/cinza, fim de semana, sem-baseline) → Task 2 ✓
- Janela seletável 30/90/180 → `_periodo_dias` na view + param `dias` nos handlers (Task 6) ✓
- KPIs + heatmap + analítica temporal → Task 4, 6 ✓
- Testes (MAX, anomalia, MV 3 fontes, view) → Tasks 1-6 ✓
- Docs `.ia/*` → Task 7 ✓
- Limitação feriado documentada → Task 7 ADR ✓

**Placeholder scan:** Task 6 Step 4 (template) é descritivo por seguir o design system existente (copiar padrões de `ingestao.html`/`base.html`) — aceitável: o contrato (URLs de chart, contexto da view, nome do builder) está explícito; markup é estilização sem lógica.

**Type consistency:** `pipeline_saude_grid`/`pipeline_volume_temporal`/`pipeline_kpis` e chaves de célula (`tribunal_id, fonte, dia, volume, cor`) usadas consistentemente entre Tasks 2-6. Handlers seguem assinatura `(dias, tribunais, sigla)` do `_CHART_HANDLERS` real. Migration dep `0028_leadconsumption_lote_id` confere.
