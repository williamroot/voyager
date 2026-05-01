# Classificação automática de leads (Precatório / Pré / Direito Creditório)

## Motivação

O **Juriscope** (plataforma operacional do grupo) trabalha com leads de **precatórios e RPV** — a fila de "baixar autos" tem capacidade ~5.000/dia. Antes do Voyager classificar automaticamente, a definição de lead só vinha **depois de baixar os autos** (custo operacional alto).

A meta deste sistema: **identificar antecipadamente** quais processos são leads, com 3 níveis de confiança, e expor via API consumida pelo Juriscope.

## Os 3 níveis

| Nível | Nome | Critério (regra + score) | Ação |
|-------|------|--------------------------|------|
| **N1** | 💎 PRECATÓRIO | score ≥ 0.7 + tem expedição explícita (F2 OU F11) | Fila imediata pra baixar autos |
| **N2** | ⏳ PRÉ-PRECATÓRIO | score ≥ 0.4 + classe Cumprimento contra Fazenda | Re-checar mensalmente |
| **N3** | 🌱 DIREITO CREDITÓRIO | score ≥ 0.2 + classe Cumprimento | Watch-list trimestral |
| — | NAO_LEAD | resto | ignorar |

## Modelo (v5)

**Algoritmo:** Logistic Regression — pesos lineares aplicados a 19 features, sigmoid no fim retorna probabilidade `[0, 1]`.

**Treino:**
- Universo: 887.534 processos TRF1 com ≥1 mov (114.541 leads + 772.993 não-leads)
- Ground truth: lista `leads_trf1.csv` (396k CNJs confirmados pela plataforma Juriscope)
- Split: 80/20 estratificado, seed=42
- Otimização: Gradient Descent batch + L2 (lr=0.5, epochs=400, l2=0.0005)
- Sem libs ML (numpy puro) — pesos hardcoded em `tribunals/classificador.py`

**Métricas (test 178k procs):**
- AUC = **0.9523**
- precision@500 = 97.8%
- precision@1.000 = 96.9%
- precision@2.500 = 96.2%
- precision@5.000 = **93.9%**
- precision@10.000 = 91.9%

### Calibração

```
decil  score_med  taxa_real_lead
D1     0.720      0.843   ← top 10% ≈ 84% confirmados
D2     0.315      0.281
D3     0.090      0.108
D4-10  ≤0.06      ≤0.02
```

D1 + D2 (top 20%) capturam ~87% dos leads. Modelo bem calibrado: probabilidade prevista bate com frequência real.

## As 19 features

### Estruturais (8)
| # | Feature | Definição | Peso |
|---|---------|-----------|------|
| F1 | `cumprim` | classe IN {12078, 156, 15160, 15215, 12079} | **+1.92** |
| F10 | `juizado_ANTI` | classe matches Juizado/Recurso Inominado/Procedimento Comum | **−1.13** |
| F2 | `precat_tc` | tem mov `tipo_comunicacao` IN {Expedição precatório/rpv, Precatório} | +0.08 |
| F4 | `mudClasse_tc` | tem mov Mudança/Evolução de Classe | (não usado v5) |
| F5 | `transito_tc` | tem mov Trânsito julgado / Definitivo | (não usado v5) |
| F6 | `homolog_tc` | tem mov Homologação de Transação | (não usado v5) |
| F7 | `envTrib_tc` | tem mov Enviada/Preparada Tribunal | +0.09 |
| F8 | `laudo` | tem mov Laudo Pericial | (peso quase 0) |

### Texto (4)
| # | Feature | Definição | Peso |
|---|---------|-----------|------|
| F11 | `precat_text` | regex `precat[óo]rio` em mov.texto | **+0.89** |
| F12 | `rpv_text` | regex `\\mrpv\\M` em mov.texto | +0.53 |
| F13 | `reqPag_text` | regex `requisi[çc][ãa]o de pagamento` | −0.56 |
| F14 | `oficio_text` | regex `of[íi]cio requisit[óo]rio` | −0.19 |

### Contagem/Cohort (4)
| # | Feature | Definição | Peso |
|---|---------|-----------|------|
| F15 | `logMovs` | `log1p(total_movs) / log(500)` | **+2.31** ← mais forte |
| F16 | `logTipos` | `log1p(distinct tipos) / log(50)` | **−1.74** ← anti |
| F17 | `logN1count` | `log1p(F11+F12+F13+F14 totals)` | +0.18 |
| F18 | `anoZ` | `(ano_cnj - 2018.9) / 6.6` z-normalizado | +0.44 |

### Anti / Juriscope-specific (2)
| # | Feature | Definição | Peso |
|---|---------|-----------|------|
| F19 | `cancelado_ANTI` | regex cancelamento/revogação precatório/rpv | ≈0 (raro nas movs públicas) |
| F20 | `exp_juriscope` | termos exatos do `has_expedicao_oficio_movement` (precatório expedido, rpv expedida, etc) | ≈0 (vivem nos autos completos) |

### Recência/Partes (2)
| # | Feature | Definição | Peso |
|---|---------|-----------|------|
| F21 | `diasUltMovZ` | dias desde última mov, z-normalizado | +0.57 |
| F23 | `logPartes` | `log1p(total_partes) / log(50)` | −0.40 (ações coletivas raramente são leads) |

### Interações (3)
| # | Feature | Peso |
|---|---------|------|
| F1×F11 | Cumprimento × precatório no texto | −0.13 (evita dupla contagem) |
| **F1×F15** | Cumprimento × volume movs | **+1.61** (sinergia forte) |
| F1×F2 | Cumprimento × precatório tipo_com | −0.04 |

**Insight chave**: features dominantes são **F15 (volume de movs)** e **F1 (classe Cumprimento)**, com sinergia F1×F15 = +1.61. Texto (F11/F12) adiciona moderado. Interações de "expedição explícita" (F19/F20) deram peso quase zero porque esses termos raramente aparecem nas movs públicas DJEN/Datajud — vivem nos autos completos do PJe que o Juriscope baixa.

## Arquitetura do pipeline

```
┌─────────────────┐    ┌─────────────────┐    ┌──────────────────┐
│  DJEN ingest    │    │ Datajud sync    │    │  PJe enricher    │
│  (janela diária)│    │ (per-CNJ)       │    │  (TRF1/TRF3)     │
└────────┬────────┘    └────────┬────────┘    └─────────┬────────┘
         │                      │                       │
         └──────────────────────┼───────────────────────┘
                                ▼
                      ┌──────────────────┐
                      │ classificar()    │  ← módulo tribunals/classificador.py
                      │ aplica v5        │
                      └────────┬─────────┘
                               ▼
              ┌──────────────────────────────┐
              │ Process.classificacao         │
              │ Process.classificacao_score   │
              │ ClassificacaoLog (transições) │
              └────────┬──────────────────────┘
                       ▼
              ┌──────────────────────────────┐
              │ /api/v1/leads/                │  ← Juriscope consome
              │ /api/v1/leads/consumed/       │
              │ /api/v1/leads/stats/          │
              └──────────────────────────────┘
```

### Triggers de classificação

1. **In-process após sync** (caminho quente — sub-segundo):
   - `datajud.ingestion.sync_processo()` → chama `classificar_e_persistir()` no fim quando há mov nova
   - `djen.ingestion.ingest_processo()` → idem (per-CNJ)

2. **Batch periódico** (caminho frio — drena backlog):
   - `tribunals.jobs.reclassificar_recentes` (cron 1h, fila `classificacao`)
   - Pega processos com mov nos últimos 7 dias **OU** nunca classificados (cap 5M)
   - Splitta em batches de 1000 → `reclassificar_batch.delay()` (workers paralelos)

3. **Re-treino** (manual):
   - Treinar nova versão (v6) → criar `ClassificadorVersao(versao='v6', pesos=..., ativa=True)`
   - Marca `ativa=True` (constraint partial garante 1 ativa)
   - Workers carregam nova versão no próximo restart (TODO: hot reload)

## Fila dedicada

`classificacao` queue (TIMEOUT 14400s) — separada de `default` pra batch pesado não bloquear watchdogs/ticks.

Workers configurados em `docker-compose-prod.yml` (`worker_classificacao`, 4 réplicas em .30). Cada worker processa ~12 queries Postgres por classificação (todas com index covering em `processo_id`). Sem contenção entre workers (cada processo = row distinta).

Pra escalar: aumentar `replicas` em `worker_classificacao`. Limite real é `max_connections` do Postgres.

## API REST (consumida pelo Juriscope)

Auth via header `X-API-Key: <chave>`. Cada cliente (Juriscope) tem `ApiClient` com chave única.

### `GET /api/v1/leads/`
Próximos N leads não consumidos pelo cliente.

Query params: `nivel`, `tribunal`, `limit` (1..10000, default 5000), `min_score` (0..1), `incluir_consumidos`.

Por default exclui processos com registro em `LeadConsumption` para esse cliente. Anti-join via `Exists(OuterRef)` pra escalar com 100k+ consumos.

### `POST /api/v1/leads/consumed/`
Marca processos como consumidos. Re-consumo permitido (sem unique constraint — cada chamada cria registro novo).

Resultados aceitos: `validado` · `sem_expedicao` · `erro` · `pendente` · `pago` · `arquivado` · `cedido`.

### `GET /api/v1/leads/stats/`
Métricas agregadas para o cliente: pendentes por nível, consumidos hoje/total, taxa de validação, versão do modelo.

## UI no Voyager

### `/dashboard/leads/`
- 6 KPI cards lazy-loaded: backlog Precatório + runway, taxa validação 30d, descobertos/dia, consumidos/dia, totais N2/N3
- 5 charts ECharts: time-series oferta vs consumo, distribuição por tribunal (grouped bars), calibração (decis), funil (descobertos→consumidos→resultado), histograma de score
- Filtros: tribunal (chips), nível, período (7d/30d/90d/1ano)
- Tabela paginada de leads pendentes
- Export CSV (`leads_export_csv` view)

### `/dashboard/api/`
- Cards stats por nível
- Documentação completa dos endpoints com curl examples
- Lista de clientes ativos (sem expor key)
- Métricas do modelo ativo (AUC, precision@K)

### `/dashboard/processos/{pk}/`
- Badge no header: `💎 PRECATÓRIO · 0.99` / `⏳ PRÉ-PRECATÓRIO` / `🌱 DIREITO CREDITÓRIO`
- Card colapsado "Por que essa classificação" (clique pra expandir):
  - Banner "Decisão" com explicação textual da regra
  - Thresholds dos 3 níveis
  - Top 12 features expandíveis com emoji + label legível + descrição completa + peso/valor/contribuição

## Limitações conhecidas

### 1. Modelo treinado só com TRF1
- TRF3 está sendo classificado automaticamente (features universais), mas sem ground truth não dá pra validar precision/calibração.
- Lista `lead_trf3` existe (347 confirmados — amostra pequena) — usar pra validar primeiro, re-treinar v6 se confirmar viés.

### 2. Tribunais estaduais (TJMG/TJSP) precisam Datajud
- Validado em **POC local** que rodando classificador puro retorna 100% NAO_LEAD pra TJMG/TJSP.
- **Causa**: nem DJEN nem Datajud original populavam `Process.classe_codigo` — só o enricher PJe (TRF1/TRF3) fazia.
- **Fix aplicado**: `datajud.ingestion.sync_processo` agora popula `Process.classe_codigo/classe_nome` a partir do `source.classe` retornado pelo Datajud.
- **Próximo passo**: re-rodar TJMG/TJSP com Datajud (que agora popula classe) — sinal F1 vai aparecer onde houver Cumprimento contra Fazenda Estadual (cod 12078 também é usado em justiça estadual).

### 3. Termos do Juriscope (F19/F20) não disparam
- O `has_expedicao_oficio_movement` do Juriscope detecta termos como "precatório expedido", "rpv expedida", "ofício requisitório expedido" — mas esses vivem nos **autos completos** do PJe.
- DJEN/Datajud entregam mov **resumida** — esses termos são raros.
- Solução teórica: integrar texto dos autos via Juriscope. Fora do escopo atual.

### 4. Calibração ainda zero
- Calibration plot na `/dashboard/leads/` fica vazio até o Juriscope começar a marcar `POST /leads/consumed/` com `resultado=validado/pago`.
- Após primeiras 1000 marcações, vai aparecer.

## Re-treino (instruções)

```python
# 1. Carregar ground truth + features
from tribunals.models import Process, Movimentacao
# ... query massiva (~30s pra 887k procs)

# 2. Construir matriz de features X + labels y
import numpy as np
X = ...  # 887k × 19 floats
y = ...  # 887k bools

# 3. Train/test split 80/20 estratificado
# 4. Logistic Regression batch GD + L2
# 5. Avaliar AUC, precision@K, calibração
# 6. Persistir nova versão:
from tribunals.models import ClassificadorVersao
ClassificadorVersao.objects.update_or_create(
    versao='v6',
    defaults={
        'pesos': WEIGHTS_DICT,
        'metricas': {'auc': ..., 'precision_at_5000': ...},
        'ativa': True,  # automaticamente desativa as outras (constraint partial)
    },
)
```

Workers precisam restart pra carregar nova versão (módulo importa pesos no boot).

## Snapshot do estado de produção (2026-04-30)

- 396.275 leads TRF1 ground truth → 100% no DB, 100% Datajud feito (pós-correção bug early-return)
- ~50k processos classificados (de 2.4M total — batch ainda drenando)
- Distribuição preliminar TRF1 (47k classificados):
  - Precatório: 51 (0.1%)
  - Pré-precatório: 393 (0.8%)
  - Direito creditório: 378 (0.8%)
  - Não-lead: 46.520 (98.3%)
- Distribuição preliminar TRF3 (12.8k classificados):
  - Precatório: 300 (2.3%) ← muito mais
  - Pré-precatório: 1.073 (8.4%)
  - Direito creditório: 634 (5.0%)
- API key Juriscope gerada, endpoints validados, dashboard em produção
- Filas: `classificacao` (4 workers), `datajud` (~210 workers em 3 hosts), `djen_backfill` (drenado)

## Bugs corrigidos durante a fase POC

1. **`datajud.sync_processo` não setava `data_enriquecimento_datajud` em early-returns** (não-encontrado / encontrado-sem-movs) → leads voltavam pra fila eternamente. Fix: 2 paths atualizam timestamp.

2. **Hosts diferentes com código velho** (.177/.184 antes do rsync) → workers throwing `FieldDoesNotExist` em campos novos. Fix: rsync rigoroso pra todos hosts, restart com confirmação grep do código atualizado.

3. **`Process.classe_codigo` vazio em TJMG/TJSP** (DJEN não popula, Datajud original tb não) → modelo retornava NAO_LEAD em 100%. Fix: patch `sync_processo` pra extrair `classe` do source Datajud quando `Process.classe_codigo` está vazio.

4. **`reclassificar_recentes` rodava sequencial em 1 job** → workers paralelos não ajudavam. Fix: modo `paralelizar=True` enfileira N batches via `reclassificar_batch.delay()` na fila `classificacao`.

5. **TRF1 enricher não detectava nova página de indisponibilidade** ("PJe está indisponível no momento" / `RelatorioIndisponibilidade`) — só detectava `errorUnexpected.seam`. Fix: 4 markers novos no `_PJE_ERROR_MARKERS`.

6. **API endpoints retornavam JSON direto** (sem wrapper `{data: ...}`) → `lazyChart` da base.html ficava com `signal lost`. Fix: `JsonResponse({'data': data})`.

## TODOs / Próximos passos

- [ ] Lista de leads TRF3 (já temos 347 — pequena, mas começa) → backfill `lead_trf3` table
- [ ] Re-treinar v6 com TRF1 + TRF3 combinado (ground truth multi-tribunal)
- [ ] Adaptar para TJMG/TJSP — testar precision real após patch de `classe_codigo`
- [ ] Calibration plot por tribunal (drift detection)
- [ ] Hot reload de pesos sem restart (ler `ClassificadorVersao.ativa` a cada job)
- [ ] PSI / drift score em produção (alerta quando distribuição muda)
- [ ] Heatmap "tribunal × ano CNJ" pra detectar gap de captura
- [ ] Adicionar texto dos autos via Juriscope (cobrir F19/F20)
