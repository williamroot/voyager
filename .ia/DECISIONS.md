# Architecture Decision Records

Decisões arquiteturais relevantes com motivação. Estilo ADR enxuto.

## ADR-001 — Modelo separado Process + Movimentacao (não JSON em Process)

**Contexto:** Falcon armazena movimentações como JSON em `Process.detail_data`. Isso torna queries por movimentação extremamente caras e impede índices nativos.

**Decisão:** Voyager separa em entidades. `Movimentacao` é first-class com índices próprios e search vector.

**Consequência:** ~10x mais linhas (1 mov ≠ 1 row em JSON), mas queries por tipo/órgão/texto são instantâneas. Aumenta espaço (~30% mais — JSON é mais compacto), mas o ganho em consulta compensa.

## ADR-002 — Tribunal denormalizado em Movimentacao

**Contexto:** Toda query de dashboard inclui `tribunal_id`. Forçar JOIN em Process+Movimentacao em milhões de rows seria caro.

**Decisão:** `Movimentacao.tribunal` FK além do `Movimentacao.processo`. Mantemos consistência via constraint que assume FK do processo.

**Consequência:** ~20 bytes a mais por row × 10M rows = 200MB extras. Vale a pena pelas queries instantâneas.

## ADR-003 — Trigger SQL para Process aggregates

**Contexto:** `Process.total_movimentacoes` e `primeira/ultima_movimentacao_em` precisam ficar sincronizados com `Movimentacao`.

**Decisão:** Trigger Postgres statement-level (`AFTER INSERT REFERENCING NEW TABLE`) recalcula em batch — 1 UPDATE por bulk_create batch.

**Alternativas rejeitadas:**
- Django signals: row-by-row (500 sinais por bulk_create de 500), inviável.
- Recompute periódico: aceita atrasos, mostra valores velhos no dashboard.

**Consequência:** ~5-10ms por bulk_create. Aceitável. Dashboard sempre fresh.

## ADR-004 — bulk_create idempotente com ignore_conflicts

**Contexto:** Workers podem processar a mesma página 2x se o job RQ for retentado. Inserções precisam ser idempotentes.

**Decisão:** `UniqueConstraint(tribunal, external_id)` + `bulk_create(ignore_conflicts=True)`. Métrica de "novos vs duplicados" via SELECT prévio, aceitando TOCTOU race entre workers.

**Consequência:** Métrica de novos pode ter pequena imprecisão sob concorrência. Documentado. Dados são sempre corretos.

## ADR-005 — Sem raw payload na Movimentacao

**Contexto:** Spec inicial guardava o payload DJEN cru pra auditoria. Custo: dobrar storage.

**Decisão:** Mapeamos os 23 campos conhecidos como colunas. Drift alert detecta quando DJEN adiciona/remove campo.

**Consequência:** Quando DJEN evolui, alerta é levantado e podemos absorver o campo novo via migration. Auditoria via re-ingestão (idempotente).

## ADR-006 — Estratégia híbrida de proxies (Cortex 80% + ProxyScrape 20%)

**Contexto:** ProxyScrape datacenter compartilhado: ~80% dos IPs bloqueados pelo WAF da DJEN. Cortex residencial: 100% de sucesso, mas IP único = ponto de falha.

**Decisão:** Em cada request, sortear: 80% Cortex (alta taxa de sucesso), 20% pool (diversifica e divide carga). Em retry, prefere alternar fonte.

**Consequência:** Backfill estável mesmo quando WAF aperta. Cortex não é queimado por sobreuso.

## ADR-007 — Resilient run_backfill (1 chunk falha ≠ job morre)

**Contexto:** ChunkedEncodingError fazia o job RQ inteiro morrer. Chunks seguintes ficavam orfãos.

**Decisão:** Cada chunk em `try/except` dentro do loop. Falha vira `IngestionRun(status=failed)` com erro persistido. Loop continua. `Tribunal.backfill_concluido_em` só é setado quando todos os chunks têm `success`.

**Consequência:** Backfill atravessa ondas de instabilidade da DJEN sem intervenção humana. Re-rodar `djen_backfill <sigla>` retenta apenas os failed (apaga primeiro pra começar limpo).

## ADR-008 — DJEN `data_disponibilizacao` como filtro padrão (não `inserido_em`)

**Contexto:** Dashboards padrão filtram últimos 90 dias.

**Decisão:** Filtro de período usa `data_disponibilizacao` (data DJEN, real). Quando backfill em curso, default vira "Todo período" pra não mostrar widgets vazios.

**Consequência:** Banner amarelo no overview/processos avisa o usuário que o backfill está parcial e indica até quando temos cobertura.

## ADR-009 — Parte como entidade compartilhada (não embedded)

**Contexto:** 1 advogado representa N processos. Embedded duplicaria nome/CPF/OAB em N rows.

**Decisão:** `Parte` como entidade única, dedupe por `documento` ou `oab` (constraints partial). Relação N-N via `ProcessoParte` com `polo` + `papel` + `representa` (FK self).

**Consequência:** Página de "Partes" mostra advogados ranqueados por número de processos. `Parte.total_processos` mantido por trigger SQL. Quando enriquecermos TRF3 com mesmo advogado: dedupe automático via OAB.

## ADR-010 — Constraint partial em ProcessoParte

**Contexto:** Advogado pode representar 2 réus distintos no mesmo processo → 2 rows com mesmo (processo, parte, polo, papel) mas `representa` diferentes.

**Decisão:** `UniqueConstraint(processo, parte, polo, papel) WHERE representa IS NULL` — só dedupe entre principais.

**Consequência:** Múltiplas representações OK. Principal dedupada via `get_or_create` no enricher.

## ADR-011 — Tema dark/light com tokens CSS

**Contexto:** Tailwind `dark:` prefix em centenas de classes é frágil. Mudança de paleta exige search-and-replace global.

**Decisão:** CSS custom properties (`--c-base`, `--c-fg`, etc.) injetadas no Tailwind via `tailwind.config.theme.colors`. Templates usam `bg-base`, `text-fg`, etc. (sempre semântico). Toggle no `<html>` via `.dark` class flipa as vars.

**Consequência:** Mudança de paleta = editar 3 lugares (`:root`, `html:not(.dark)`, e na tailwind config se for nome novo). Charts respeitam tema via `chartGridColors()`. Componentes 100% reutilizáveis.

## ADR-012 — Server-rendered + HTMX (não SPA)

**Contexto:** Dashboard interno com poucos usuários simultâneos. SPA agrega complexidade (build pipeline, state management, hydration) sem ganho de UX.

**Decisão:** Django templates + HTMX 2 + Alpine.js 3 + ECharts. Sem build do JS. Tudo via CDN com `tailwind.config` inline.

**Consequência:** Páginas são SEO-friendly por acidente. Tempo até interativo <500ms. Custo: dependência de CDNs (mitigada por whitenoise + nginx cache, e por baixar local em prod se necessário).

## ADR-013 — `/api/v1/health/` rico vs liveness simples

**Contexto:** Healthcheck rico (lag por tribunal, drift, filas) é útil pra monitoring externo, mas se trippa 503 em Docker HEALTHCHECK, derruba o container quando há drift — exatamente quando precisamos do dashboard.

**Decisão:** Endpoints separados:
- `/api/v1/health/liveness/` — sempre 200 se processo vivo. Usado por Docker.
- `/api/v1/health/` — readiness rico, 503 se DB/Redis fora ou lag>36h. Usado por monitoring externo. Drift NÃO trippa 503 (só aparece no payload).

**Consequência:** Sistema fica disponível mesmo com schema drift. Monitoring vê o problema sem derrubar a app.

## ADR-014 — Logistic Regression (puro Python) vs sklearn/XGBoost pra classificação de leads

**Contexto:** Precisava de modelo pra classificar 887k+ processos como Precatório/Pré/Direito Creditório. Container web já tinha Django + dependências mínimas — adicionar sklearn (50MB+) ou xgboost (200MB+) inflaria imagem. Plus, treino é 1x manual; inferência é per-processo (1 dot-product de 19 floats).

**Decisão:** Logistic Regression manual com gradient descent batch + L2:
- Treino: numpy puro (instalado on-demand quando re-treinar)
- Inferência: pesos hardcoded em `tribunals/classificador.py` como dict Python
- Features: 19 dimensões — binárias (presença) + log-normalizadas (volume) + z-score (recência/ano)
- Hierarquia categorial em código (regras + thresholds), não no modelo (modelo só dá score 0..1)

**Consequência:**
- Imagem leve, sem dependências adicionais runtime
- Inference <1ms por processo (negligível no path de sync)
- Re-treino requer numpy + persistir pesos em `ClassificadorVersao` no DB
- Trade-off: sem features non-linear automáticas. Mitigado adicionando interações manuais (F1×F11, F1×F15, F1×F2)
- Resultado v5: AUC 0.95, precision@5k 93.9% — competitivo com modelos complexos pra esse dataset

## ADR-015 — Classificar TODOS os processos vs só os de tribunais com ground truth

**Contexto:** Modelo treinado só com TRF1 (396k leads). Universo tem TRF3, TJMG, TJSP. Aplicar onde não treinou pode dar precision ruim.

**Decisão:** Classifier APLICA em qualquer tribunal — features são universais (classe Cumprimento, palavras-chave, contagens). NÃO filtra por tribunal.

**Consequência:**
- TRF3 classificado em produção (15% taxa de lead — plausível pq SP concentra cumprimentos)
- TJMG/TJSP em POC: precisava `Process.classe_codigo` populado (corrigido via patch Datajud)
- Trade-off: precision real desconhecida em tribunais sem ground truth — mitigado via calibration plot (Juriscope marca consumed → vemos taxa real por bucket)
- Quando tiver ground truth de outros tribunais, re-treinar v6 multi-tribunal

## ADR-017 — Warm jobs do dashboard inline no scheduler (sem fila RQ)

**Contexto:** Jobs de warm de cache (KPIs, charts, partes, estatísticas, filtros, MV refresh) eram enfileirados na fila `warm` via `_enqueue_singleton`. A lógica de singleton era complexa e sofria race condition quando 6+ jobs disparavam simultaneamente, gerando acúmulo de duplicatas na fila. Reiniciar o `worker_warm` via SIGKILL deixava locks Redis presos e queries PG zumbis.

**Decisão:** Jobs de warm passaram a ser agendados diretamente no `BlockingScheduler` com `ThreadPoolExecutor(20)`. Cada função warm roda inline no thread pool — sem fila RQ, sem worker externo. `max_instances=1` + `coalesce=True` no APScheduler + `_with_lock` Redis são as camadas de proteção contra sobreposição.

**Removido:** `worker_warm` (2 réplicas), fila `warm` em `RQ_QUEUES`, `_enqueue_singleton`.

**Intervalos:**
- KPIs, charts leves/pesados, partes, estatísticas, filtros: a cada 30 min
- Velocidade de ingestão (`warm_ingestao_por_hora`): a cada 4h (lê da MV, muda pouco)
- MV refresh (`refresh_materialized_views`): cron diário 03:00

**Consequência:** Zero acúmulo de jobs na fila. Sem dependência de worker externo pra dashboard funcionar. Falha de 1 warm job não afeta os outros (thread pool isolado). Trade-off: warm jobs pesados (charts_pesados, estatisticas) ocupam threads do scheduler por até 30-60min — mitigado pelo pool de 20 threads.

## ADR-016 — Re-consumo permitido em LeadConsumption (sem unique constraint)

**Contexto:** API expõe `POST /leads/consumed/` pro Juriscope marcar processos. Mesmo (cliente, processo) pode aparecer 2+ vezes?

**Decisão:** SEM unique constraint. Cada chamada cria registro novo. Histórico completo preservado.

**Consequência:**
- Cliente pode atualizar resultado: `pendente` → `validado` → `pago` (todos visíveis na linha do tempo)
- Listar leads disponíveis: anti-join via `Exists(OuterRef)` (presença em qualquer registro)
- Funil/calibration usa MAIS RECENTE por processo (`order_by('-consumido_em').first()`)
- Trade-off: tabela cresce mais rápido (~1.8M/ano em ritmo de 5k/dia, fácil pro Postgres)
