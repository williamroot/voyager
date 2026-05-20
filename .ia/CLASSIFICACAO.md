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

## Modelo atual (v6 — em produção desde 2026-05-08)

**Algoritmo:** Logistic Regression — pesos lineares aplicados às mesmas 19 features do v5, sigmoid retorna probabilidade `[0, 1]`. Snapshot dos pesos em `tribunals/classificador.py::HARDCODED_WEIGHTS` (fallback); valores vivos em `ClassificadorVersao(ativa=True)` no DB (hot reload, ver abaixo).

**Treino v6 (commit 6cdfff6):**
- Universo: 1.050.791 processos TRF1
- Split: 80/20 estratificado, seed=42
- Otimização: GD batch + L2, numpy puro
- Pesos persistidos em `ClassificadorVersao` (versao='v6', ativa=True)

**Métricas v6 (test 210k procs):**
- AUC = **0.9610**
- precision@500 = 98.6%
- precision@1.000 = 99.3%
- precision@5.000 = **99.1%**
- precision@10.000 = 98.2%

## Modelo v7 (em preparação — ver V7_DEPLOY_DECISION.md)

24 features = 19 do v6 + F24 (RPV expedida), F25 (pagamento administrativo),
F26 (inscrição em ordem cronológica), F27 (trânsito julgado), F28 (líquido e certo).

Trocas em relação ao v6:
- `sample_weight` por origem do label (humano=3.0, juriscope=2.0, csv reforçado=2.0, csv base=1.0) — ver ADR-019.
- Thresholds N1/N2/N3 por tribunal via `ThresholdTribunal` (DB-driven, ADR-021).
- Split conformal produz delta global de incerteza para fila Juriscope.
- 6 gates formais PASS/WARN/BLOCK (ver `REGRAS_NEGOCIO_VALIDACAO.md §3`).

Promoção em 3 passos:
1. Treino + relatório de gates (`treinar_classificador_v7`).
2. Marcar `ClassificadorVersao(versao='v7', shadow=True)` por 7 dias — `comparar_shadow` (cron 04:00) avalia agreement com v6.
3. Sign-off humano + flip de `ativa=True` (rollback documentado em `V7_DEPLOY_DECISION.md`).

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
   - Treinar nova versão → criar `ClassificadorVersao(versao='vN', pesos=..., shadow=True)` por 7d
   - Comparar via `comparar_shadow` (cron 04:00 diário) — agreement_rate, KS, disagreements
   - Promover: flip `ativa=True` (constraint partial garante 1 ativa)
   - Workers detectam em ≤ 60s via hot reload (sem restart) — ver seção dedicada

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

### `POST /api/v1/leads/consumed/` — assíncrono (202)
Requer `lote_id` (UUID) no body: `{lote_id, consumos: [{cnj, resultado}, ...]}`. Enfileira o job RQ `registrar_consumo_leads` (fila `leads_consumo`) e responde `202 {enfileirado, lote_id, recebidos}`. Idempotente por `(cliente, processo, lote_id)` — retry/reenvio não duplica nem perde. Detalhes em [`API.md`](API.md) e ADR-024.

Resultados aceitos: `validado` · `sem_expedicao` · `erro` · `pendente` · `pago` · `arquivado` · `cedido`.

### `GET /api/v1/leads/stats/`
Métricas agregadas para o cliente: pendentes por nível, consumidos hoje/total, taxa de validação, versão do modelo.

## UI no Voyager

### `/dashboard/leads/algoritmo/` (didática, advogado-first)
Página explicativa: hero + 4 níveis + 19 sinais agrupados em 5 famílias com `<details>` colapsáveis (didático + critério técnico) + bloco de combinação (sigmoide) + 4 exemplos curados + sandbox CNJ + tabela v6 vs v7 + métricas. Reusa `tribunals/explicacao.py` (FEATURE_META + builder). Sandbox: `POST /dashboard/leads/algoritmo/explicar/` retorna partial HTML com `compute_features + _categorizar` ao vivo.

Exemplos curados via `settings.ALGORITMO_EXEMPLOS_CNJS` (dict rótulo→CNJ); fallback é top-1 por categoria no DB.

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

### 1. Modelo (v6) ainda treinado só com TRF1
- TRF3 classificado automaticamente (features universais), sem ground truth amplo até listas TRF3 expandidas serem ingeridas (em curso — CSVs em `data_ground_truth/` (versionados em git desde 2026-05-12)).
- v7 (em preparação) inclui labels humanos via `ProcessoValidacao` (peso 3.0) — primeiro modelo multi-fonte.

### 2. Tribunais estaduais (TJMG/TJSP) precisam Datajud
- Validado em **POC local** que rodando classificador puro retorna 100% NAO_LEAD pra TJMG/TJSP.
- **Causa**: nem DJEN nem Datajud original populavam `Process.classe_codigo` — só o enricher PJe (TRF1/TRF3) fazia.
- **Fix aplicado**: `datajud.ingestion.sync_processo` agora popula `Process.classe_codigo/classe_nome` a partir do `source.classe` retornado pelo Datajud.
- **Próximo passo**: re-rodar TJMG/TJSP com Datajud (que agora popula classe) — sinal F1 vai aparecer onde houver Cumprimento contra Fazenda Estadual (cod 12078 também é usado em justiça estadual).

### 3. Termos do Juriscope (F19/F20) não disparam
- O `has_expedicao_oficio_movement` do Juriscope detecta termos como "precatório expedido", "rpv expedida", "ofício requisitório expedido" — mas esses vivem nos **autos completos** do PJe.
- DJEN/Datajud entregam mov **resumida** — esses termos são raros.
- Solução teórica: integrar texto dos autos via Juriscope. Fora do escopo atual.

### 4. Calibração
- Calibration plot agora aparece em duas formas: (a) `/dashboard/leads/` via `LeadConsumption.resultado` do Juriscope; (b) `/dashboard/leads/visibilidade/` via `ProcessoValidacao` (humano) — segunda fonte é independente do Juriscope e disponível imediatamente após validação interna.

## Pipeline de validação humana

Sistema de revisão manual sobre a saída do classificador (T4-T22). Detalhes em
[`REGRAS_NEGOCIO_VALIDACAO.md`](REGRAS_NEGOCIO_VALIDACAO.md), modelos em
[`DATA_MODEL.md`](DATA_MODEL.md#validação-humana--shadow--thresholds),
ADRs 018/019.

### Fluxo de anotação

```
sampling/mining FN  ─→  AmostraValidacao  ─→  fila /dashboard/leads/validacao/<id>/
   (estratégias E1..E6,                          ↓
    estratos, score_composito)                   anotador escolhe resultado/confianca
                                                  + motivo opcional + hotkeys
                                                  ↓
                                          ProcessoValidacao (append-only)
                                                  ↓ (10% dupla-anotação)
                                          divergência?
                                            ↓ sim
                                  revisor sênior preenche label_final
```

### Estratégias de amostragem

`tribunals/sampling.py` implementa as funções de sorteio; `criar_lote(estrategia=...)`
encadeia tudo (seed → query → exclude já validados na janela → AmostraValidacao + N
AmostraProcesso). Estratégias: `top_score`, `borderline`, `low_score`,
`falsos_consumidos`, `recuperados`, `on_demand`, `fn_candidatos`, `shadow_disagree`.

### Cohen's kappa e label_final

- 10% de cada lote é dupla-anotado (subsample no `criar_lote`).
- Job `marcar_divergencias` (diário) detecta `(processo, 2 usuários distintos, labels diferentes)`.
- Revisor com `can_resolve_disagreement` preenche `ProcessoValidacao.label_final` + `label_final_resolvido_por` + `label_final_resolvido_em`.
- Kappa alvo: ≥ 0.7 (substancial). Abaixo de 0.6 → revisar guideline.

### Anonimização (LGPD)

**Fora de escopo desta versão.** Campo `usuario_hash` permanece no schema mas
não é populado. `usuario` é `SET_NULL` no delete de User (cobre delete
administrativo). Reativar futuramente requer setting com salt + popular no save
+ comando `anonimizar_usuario`. Ver REGRAS_NEGOCIO_VALIDACAO §6.

### Permissions custom + grupos

Migration `0024_validacao_humana` cria as 4 permissions custom em `ProcessoValidacao.Meta.permissions`
(`can_validate_lead`, `can_publish_model`, `can_view_validacao_dashboard`,
`can_resolve_disagreement`, `can_view_motivo`).

Comando `setup_validacao_groups` cria os grupos:
- `validadores_leads` → `can_validate_lead` + `can_view_validacao_dashboard`
- `revisores_seniores` → tudo de validador + `can_resolve_disagreement` + `can_view_motivo`
- `auditores_leads` → read-only sobre dashboards de validação + `can_view_motivo`
- `model_admins` → `can_publish_model` + `can_view_validacao_dashboard`

Convite via `accounts.Invite` carrega `grupos_alvo`, aplicado no `accept_invite`.

## Mining de falsos negativos

`tribunals/management/commands/minerar_fn.py` calcula `suspeita_score` composto a partir de 6
estratégias **ortogonais** rodadas no universo completo (TRF1+TRF3 por tribunal/mês):

| Estratégia | Filtro | Delta no score_composito |
|---|---|---|
| **E1** | score do modelo em [0.10, 0.20] (borderline baixo) | +0.20 |
| **E2** | matches em 7 novos regex pré-v7 (expedição/RPV/líquido e certo/etc) | +0.30 × min(n/3, 1) |
| **E3** | F1 órfão — classe de Cumprimento mas ≤5 movs | +0.15 |
| **E4** | similaridade > 0.6 com cluster de 1327 recuperados (mini-LR numpy) | +0.20 |
| **E5** | KMeans cluster density > 0.7 (centroid próximo, vizinhança densa de positivos) | +0.10 |
| **E6** | cross-tribunal cosine sim > 0.7 ao centroid global de leads | +0.05 |

Output: CSV `fn_candidatos_<tribunal>_<data>.csv` com `cnj`, `score_modelo`,
`suspeita_score`, `motivos` (lista E1..E6), top features negativas.
Integra com `sampling.sample_fn_candidatos` → vira lote `estrategia=fn_candidatos`.

Cron semanal `gerar_lotes_semanais_fn` (dom 02:00, fila `default`, gated por
`VALIDACAO_LOTES_SEMANAIS_ENABLED`) chama `minerar_fn` por tribunal e cria
automaticamente lotes pra anotação.

## Hot reload de pesos

`tribunals/classificador.py` mantém `_WEIGHTS_CACHE` thread-safe com TTL configurável
via `settings.CLASSIFICADOR_RELOAD_TTL` (default 60s):

```
classificar()  →  _current_weights()  →  _maybe_reload_weights()
                                            ├─ fast path (TTL não venceu): return
                                            └─ slow path (lock + double-check):
                                                ├─ lê ClassificadorVersao(ativa=True)
                                                ├─ _validate_pesos() checa schema
                                                ├─ se OK: atualiza cache + log "reloaded v6→v7"
                                                └─ erro/inválido: fallback HARDCODED_WEIGHTS
                                                                  (preserva último valor bom)
```

- **`_validate_pesos`** aceita superset (v7 com F24-F28 OK, features extras ignoradas no predict) mas rejeita subset.
- **Storm-retry guard**: em DB-down, ainda atualiza `loaded_at` pra não tentar a cada chamada.
- **`force_reload_weights()`** pula TTL — usado em testes e management commands.
- Workers detectam troca de versão em ≤ 60s sem restart.

**Limitação:** features novas (F24+) exigem deploy de código pra `compute_features`
saber extrair. Hot reload cobre só ajuste de pesos das features já conhecidas
(F1-F23).

## Shadow mode (A/B)

`ClassificadorVersao.shadow=True` (N podem coexistir). `classificar_e_persistir`
chama hook `_maybe_enfileirar_shadow(processo_id)` no fim que:

1. Lê `SHADOW_SAMPLE_RATE` (default 0.10).
2. Sorteio aleatório — se passar, enfileira `classificar_shadow_async.delay(pid)`.
3. Job aplica TODAS as versões shadow no processo, grava `ClassificacaoShadowLog` por versão.

Comparação A/B via cron diário `comparar_shadow_daily` (04:00, fila `default`):
- Roda `comparar_shadow('v6', 'v7', dias=7)` para cada par.
- Calcula `agreement_rate`, KS test entre distribuições de score, top disagreements (categoria muda).
- Output em `MOCKUP`/relatório que vira widget no dashboard de visibilidade.

Constraint: `shadow=True` NÃO é restrito (suporta múltiplas candidatas em paralelo). `ativa=True` é partial unique (sempre 1 ativa).

## Categorização DB-driven (compartilhada)

`_categorizar(score, features, tribunal_id, versao_modelo)` em `tribunals/classificador.py:464+`
é a **função única** chamada tanto por `classificar()` (path ativo) quanto por
`classificar_shadow()`. Lê `ThresholdTribunal(ativo=True, tribunal=tribunal_id, versao_modelo=...)`
do DB; fallback aos defaults hardcoded (0.7/0.4/0.2) se row não existir ou erro.

Filtro por `versao_modelo` protege a transição v6→v7 (cada versão pode ter row
própria de threshold). Ver ADR-022 (drift de lógica resolvido).

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

## Snapshot do estado de produção (2026-05-12)

- v6 ativo em produção (commit 6cdfff6, AUC 0.9610, prec@5000 0.991)
- 396.275 leads TRF1 ground truth → 100% no DB, 100% Datajud feito
- Listas TRF3 expandidas (CSVs em `data_ground_truth/`: `leads_trf3.csv`, `leads_trf3_top1000_recentes.csv`, `leads_trf3_precatorio_500.csv`, `lista_5000_cnjs.txt`, `lista_5000_naoleads.csv`)
- Pipeline de validação humana em produção (5 modelos novos + 6 management commands + 3 telas)
- Shadow mode armado (SHADOW_SAMPLE_RATE=0.10) — cron `comparar_shadow_daily` (04:00) e ETL pra widget
- API key Juriscope ativa, endpoints validados, dashboard em produção
- Filas: `classificacao` (4 workers), `datajud` (~210 workers em 3 hosts), `djen_backfill` (drenado)

## Bugs corrigidos durante a fase POC

1. **`datajud.sync_processo` não setava `data_enriquecimento_datajud` em early-returns** (não-encontrado / encontrado-sem-movs) → leads voltavam pra fila eternamente. Fix: 2 paths atualizam timestamp.

2. **Hosts diferentes com código velho** (.177/.184 antes do rsync) → workers throwing `FieldDoesNotExist` em campos novos. Fix: rsync rigoroso pra todos hosts, restart com confirmação grep do código atualizado.

3. **`Process.classe_codigo` vazio em TJMG/TJSP** (DJEN não popula, Datajud original tb não) → modelo retornava NAO_LEAD em 100%. Fix: patch `sync_processo` pra extrair `classe` do source Datajud quando `Process.classe_codigo` está vazio.

4. **`reclassificar_recentes` rodava sequencial em 1 job** → workers paralelos não ajudavam. Fix: modo `paralelizar=True` enfileira N batches via `reclassificar_batch.delay()` na fila `classificacao`.

5. **TRF1 enricher não detectava nova página de indisponibilidade** ("PJe está indisponível no momento" / `RelatorioIndisponibilidade`) — só detectava `errorUnexpected.seam`. Fix: 4 markers novos no `_PJE_ERROR_MARKERS`.

6. **API endpoints retornavam JSON direto** (sem wrapper `{data: ...}`) → `lazyChart` da base.html ficava com `signal lost`. Fix: `JsonResponse({'data': data})`.

## TODOs / Próximos passos

Concluído desde o último snapshot:
- [x] v6 treinado e em produção (TRF1 1.05M procs, AUC 0.9610)
- [x] Hot reload de pesos (TTL 60s, thread-safe, fallback)
- [x] Shadow mode A/B (SHADOW_SAMPLE_RATE + ClassificacaoShadowLog + cron comparar)
- [x] Categorização DB-driven via ThresholdTribunal (compartilhada ativo+shadow)
- [x] Pipeline de validação humana (5 modelos + 8 estratégias + 6 commands + 3 telas)
- [x] Mining FN (6 estratégias E1-E6 + composite suspeita_score)
- [x] Calibration plot por tribunal (widget na /dashboard/leads/visibilidade/)
- [x] Heatmap tribunal × ano CNJ (idem)

Pendente:
- [ ] v7 — em preparação, ver `V7_DEPLOY_DECISION.md`
- [ ] Trigger Postgres UPDATE-block em `ProcessoValidacao` (antes de publish externo do dataset)
- [ ] CV interno no grid de thresholds v7 (hoje é só holdout único)
- [ ] Cleanup job de `ClassificacaoShadowLog` (retention 90 dias)
- [ ] Numpy no Dockerfile (estabilidade — hoje só em `requirements.txt`)
- [ ] Webhook outbound pro Juriscope (lead high-conf) em vez de polling
- [ ] PSI / drift score formal (shadow mode cobre parte)
- [ ] Adicionar texto dos autos via Juriscope (cobrir F19/F20)
- [ ] Adaptar para TJMG/TJSP — precision real após patch de `classe_codigo`
- [ ] [BIZ pendente] `model_admins ∩ validadores_leads` permitido? (conflito de interesse)
- [ ] [BIZ pendente] Texto LGPD do convite revisado pelo DPO
