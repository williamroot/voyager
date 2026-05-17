# Fix _dia_coberto + dashboard gap — Implementation Plan

> Executar via subagent-driven-development. Steps com `- [ ]`.

**Goal:** (1) `_dia_coberto`/`_dias_cobertos`: dia dentro do horizonte recente (`hoje - overlap_dias`) só conta coberto se o `success` teve dados — dias antigos inalterados (backfill seguro). (2) `pipeline_saude_grid`: sintetizar célula DJEN `volume=0` para dia útil `< hoje` sem run (tribunal ativo c/ `backfill_concluido_em`) → vermelho, não cinza. (3) tooltip do heatmap com métricas nativas + docs.

**Tech:** Django, Postgres, pytest (`docker compose exec -T web python -m pytest`). Branch `feat/fix-dia-coberto-e-dashboard-gap`. Commits limpos, sem atribuição Claude.

---

### Task A: Fix 1 — predicado de cobertura com horizonte recente

**Files:** Modify `djen/jobs.py` (`_dia_coberto`, `_dias_cobertos`, novo helper); Test `tests/test_cobertura_horizonte.py`.

Design: helper `_run_tem_dados(v)` = `v['movimentacoes_novas'] or v['movimentacoes_duplicadas'] or v['paginas_lidas']`. Horizonte recente = `date.today() - timedelta(days=tribunal.overlap_dias)`. Para `dia >= horizonte`: cobre só se existe success cobrindo `dia` com dados. Para `dia < horizonte`: comportamento atual (qualquer success).

- [ ] Step 1 — failing test `tests/test_cobertura_horizonte.py`:

```python
import pytest
from datetime import date, timedelta, datetime, timezone
from tribunals.models import Tribunal, IngestionRun
from djen.jobs import _dia_coberto, _dias_cobertos

def _run(t, d, **kw):
    return IngestionRun.objects.create(
        tribunal=t, status=IngestionRun.STATUS_SUCCESS,
        janela_inicio=d, janela_fim=d,
        movimentacoes_novas=kw.get('novas', 0),
        movimentacoes_duplicadas=kw.get('dup', 0),
        paginas_lidas=kw.get('pag', 0),
        finished_at=datetime(d.year, d.month, d.day, 3, tzinfo=timezone.utc))

@pytest.mark.django_db
def test_horizonte_recente_empty_nao_cobre_mas_antigo_cobre():
    t = Tribunal.objects.create(sigla='TCH', sigla_djen='TCH', nome='T',
                                ativo=True, overlap_dias=3)
    hoje = date.today()
    recente_vazio = hoje - timedelta(days=1)
    recente_cheio = hoje - timedelta(days=2)
    antigo_vazio = hoje - timedelta(days=400)
    _run(t, recente_vazio)                 # success vazio, recente
    _run(t, recente_cheio, novas=10)       # success com dados, recente
    _run(t, antigo_vazio)                  # success vazio, antigo (backfill)
    assert _dia_coberto(t, recente_vazio) is False
    assert _dia_coberto(t, recente_cheio) is True
    assert _dia_coberto(t, antigo_vazio) is True       # backfill intacto
    cob = _dias_cobertos(t, antigo_vazio, hoje)
    assert recente_vazio not in cob
    assert recente_cheio in cob
    assert antigo_vazio in cob

@pytest.mark.django_db
def test_recente_empty_mais_cheio_cobre():
    t = Tribunal.objects.create(sigla='TC2', sigla_djen='TC2', nome='T',
                                ativo=True, overlap_dias=3)
    d = date.today() - timedelta(days=1)
    _run(t, d)              # vazio
    _run(t, d, dup=5)       # com dados (duplicadas conta)
    assert _dia_coberto(t, d) is True
```

- [ ] Step 2 — run, verify FAIL: `docker compose exec -T web python -m pytest tests/test_cobertura_horizonte.py -q`. Paste.

- [ ] Step 3 — implement in `djen/jobs.py`. Add helper near the predicates:

```python
def _run_tem_dados(v) -> bool:
    return bool(v['movimentacoes_novas'] or v['movimentacoes_duplicadas']
                or v['paginas_lidas'])


def _dia_coberto(tribunal: Tribunal, dia: date) -> bool:
    qs = IngestionRun.objects.filter(
        tribunal=tribunal, status=IngestionRun.STATUS_SUCCESS,
        janela_inicio__lte=dia, janela_fim__gte=dia,
    ).values('movimentacoes_novas', 'movimentacoes_duplicadas', 'paginas_lidas')
    horizonte = date.today() - timedelta(days=tribunal.overlap_dias)
    if dia >= horizonte:
        return any(_run_tem_dados(v) for v in qs)
    return qs.exists()


def _dias_cobertos(tribunal: Tribunal, ini: date, fim: date) -> set[date]:
    """Dias cobertos por IngestionRun success. Para dias no horizonte recente
    (hoje - overlap_dias), exige run com dados; dias antigos: qualquer success."""
    runs = list(IngestionRun.objects.filter(
        tribunal=tribunal, status=IngestionRun.STATUS_SUCCESS,
        janela_inicio__lte=fim, janela_fim__gte=ini,
    ).values('janela_inicio', 'janela_fim', 'movimentacoes_novas',
             'movimentacoes_duplicadas', 'paginas_lidas'))
    horizonte = date.today() - timedelta(days=tribunal.overlap_dias)
    covered: set[date] = set()
    for run in runs:
        d = max(run['janela_inicio'], ini)
        end = min(run['janela_fim'], fim)
        com_dados = _run_tem_dados(run)
        while d <= end:
            if d < horizonte or com_dados:
                covered.add(d)
            d += timedelta(days=1)
    return covered
```
Confirm `date`, `timedelta` already imported in `djen/jobs.py` (used elsewhere) — reuse.

- [ ] Step 4 — run all: `docker compose exec -T web python -m pytest tests/test_cobertura_horizonte.py tests/test_ingestion_chunks.py -q`. Paste. Green.

- [ ] Step 5 — commit:
```bash
git add djen/jobs.py tests/test_cobertura_horizonte.py
git commit -m "fix(djen): _dia_coberto exige dados em dia recente (overlap retenta success vazio)"
```

---

### Task B: Fix 2 — sintetizar dia útil DJEN ausente como vermelho

**Files:** Modify `dashboard/queries.py` (`pipeline_saude_grid`); Test append `tests/test_pipeline_saude.py`.

Após montar as células DJEN e ANTES do passo de baseline/cor, sintetizar células faltantes:
- Tribunais esperados: se `tribunais` dado, filtrar `Tribunal.objects.filter(sigla__in=tribunais, ativo=True, backfill_concluido_em__isnull=False)`; senão todos `ativo=True, backfill_concluido_em__isnull=False`. Valores = `sigla` (pk).
- Para cada tribunal esperado, para cada `dia` weekday (`weekday()<5`) em `[inicio, hoje)` (exclui hoje) sem célula DJEN existente: append `{'tribunal_id': sigla, 'fonte':'djen', 'dia': dia, 'novas':0,'duplicadas':0,'paginas':0,'runs':0,'encontradas':0,'volume':0}`.
- O passo de baseline/cor existente então pinta vermelho se houver baseline (dias úteis anteriores com volume), cinza se sem baseline.

- [ ] Step 1 — failing test (append):
```python
@pytest.mark.django_db
def test_dia_util_djen_ausente_vira_vermelho():
    from datetime import timedelta
    t = Tribunal.objects.create(sigla='TGA', sigla_djen='TGA', nome='T',
        ativo=True, backfill_concluido_em=datetime(2020,1,1,tzinfo=timezone.utc))
    hoje = date.today()
    # acha 6 dias úteis anteriores; baseline nos 5 mais antigos, gap no mais recente
    uteis = []
    d = hoje - timedelta(days=1)
    while len(uteis) < 6:
        if d.weekday() < 5:
            uteis.append(d)
        d -= timedelta(days=1)
    uteis = sorted(uteis)            # antigos -> recentes
    gap = uteis[-1]                  # dia útil mais recente fica SEM run
    for du in uteis[:-1]:
        IngestionRun.objects.create(tribunal=t, status=IngestionRun.STATUS_SUCCESS,
            janela_inicio=du, janela_fim=du, movimentacoes_novas=100,
            movimentacoes_duplicadas=0, paginas_lidas=5,
            finished_at=datetime(du.year,du.month,du.day,3,tzinfo=timezone.utc))
    grid = pipeline_saude_grid(dias=30, tribunais=[t.pk])
    cel = [c for c in grid if c['fonte']=='djen' and c['dia']==gap]
    assert cel and cel[0]['volume'] == 0 and cel[0]['cor'] == 'vermelho'
```

- [ ] Step 2 — run, verify FAIL (no synthetic cell yet → list empty → assertion error). Paste.
`docker compose exec -T web python -m pytest tests/test_pipeline_saude.py::test_dia_util_djen_ausente_vira_vermelho -q`

- [ ] Step 3 — implement in `pipeline_saude_grid` (insert synthesis block between the MV-read loop and the `series = defaultdict(list)` baseline block):
```python
    # Sintetiza dia útil DJEN esperado-mas-ausente (< hoje) -> baseline pinta vermelho
    from tribunals.models import Tribunal
    esp = Tribunal.objects.filter(ativo=True, backfill_concluido_em__isnull=False)
    if tribunais:
        esp = esp.filter(sigla__in=tribunais)
    esp_siglas = list(esp.values_list('sigla', flat=True))
    djen_dias = {(c['tribunal_id'], c['dia'])
                 for c in cells if c['fonte'] == 'djen'}
    for sig in esp_siglas:
        d = inicio
        while d < hoje:
            if d.weekday() < 5 and (sig, d) not in djen_dias:
                cells.append({'tribunal_id': sig, 'fonte': 'djen', 'dia': d,
                              'novas': 0, 'duplicadas': 0, 'paginas': 0,
                              'runs': 0, 'encontradas': 0, 'volume': 0})
            d += timedelta(days=1)
```
(`timedelta` already imported at top of `pipeline_saude_grid`'s local import block — reuse; do not duplicate.)

- [ ] Step 4 — run full file: `docker compose exec -T web python -m pytest tests/test_pipeline_saude.py -q`. Paste. All green (6 tests).

- [ ] Step 5 — commit:
```bash
git add dashboard/queries.py tests/test_pipeline_saude.py
git commit -m "fix(ingestao): dia útil DJEN ausente vira vermelho no heatmap (não cinza)"
```

---

### Task C: tooltip heatmap com métricas nativas + docs

**Files:** Modify `dashboard/templates/dashboard/base.html` (`buildPipelineHeatmap` tooltip); `.ia/INGESTION.md`, `.ia/DASHBOARD.md`, `.ia/DECISIONS.md`.

- [ ] Step 1 — Em `base.html`, `buildPipelineHeatmap`: no `tooltip.formatter`, em vez de mostrar só a cor, mostrar fonte + dia + métricas nativas quando presentes (DJEN: `novas`, `duplicadas`, `encontradas`, `paginas`, `runs`; demais: `processos`/`volume`) + status legível (verde/amarelo/vermelho/cinza → "OK"/"atenção"/"anomalia"/"esperado vazio"). Manter o idioma ECharts já usado nos outros `build*` (não inventar libs/estilo). Garantir que o payload de `pipeline-grid` já carrega essas chaves (carrega — `pipeline_saude_grid` retorna; confirme lendo o handler/JSON).

- [ ] Step 2 — Verificar render sem erro: `docker compose exec -T web python manage.py check` (sem erro) e que o teste de view segue verde: `docker compose exec -T web python -m pytest tests/test_pipeline_saude.py::test_ingestao_saude_view_200 -q`. Paste.

- [ ] Step 3 — Docs (pt-BR, estilo de cada arquivo):
  - `.ia/INGESTION.md`: nota no predicado de cobertura — dia no horizonte recente exige `success` com dados (overlap retenta success vazio); dias antigos inalterados (backfill).
  - `.ia/DASHBOARD.md`: heatmap agora pinta dia útil DJEN ausente como vermelho (não cinza); tooltip mostra métricas nativas.
  - `.ia/DECISIONS.md`: estender ADR-025 (ou nota) — Fix `_dia_coberto` com horizonte recente (decisão: não quebrar backfill de dias antigos vazios); dashboard sintetiza dia ausente.

- [ ] Step 4 — commit:
```bash
git add dashboard/templates/dashboard/base.html .ia/
git commit -m "feat(ingestao): tooltip heatmap com métricas nativas + docs (fix cobertura/gap)"
```

---

## Self-review
- Spec: Fix1 horizonte-recente (Task A, testes cobrindo recente-vazio/recente-cheio/antigo-vazio/recente-misto + _dias_cobertos) ✓; Fix2 síntese dia ausente (Task B) ✓; tooltip+docs (Task C) ✓.
- Placeholders: nenhum — código completo em cada step.
- Tipos: `tribunal_id` = sigla (string) consistente; chaves de célula DJEN idênticas às de Task 3 original (`novas,duplicadas,paginas,runs,encontradas,volume`); `_run_tem_dados` recebe dict de `.values()` em ambos os usos.
