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

## ADR-018 — Sistema de validação humana por amostragem estratificada

**Contexto:** v6 (TRF1, AUC 0.961) precisa de ground truth para gate v7 e pra medir precision real em tribunais sem lista Juriscope (TRF3, TJMG, TJSP). Tensão entre 2 eixos: (a) imutabilidade necessária pra confiar nos labels como dataset de treino, (b) prevenção de viés de re-anotação ("efeito âncora").

**Decisão:**
- `AmostraValidacao` (lote) com 8 estratégias (`top_score`, `borderline`, `low_score`, `falsos_consumidos`, `recuperados`, `on_demand`, `fn_candidatos`, `shadow_disagree`). Seed persistida para reprodutibilidade do sorteio.
- `AmostraProcesso` (through M2M) preserva ordem, score no sorteio, e `suspeita_score`/`motivos_suspeita` quando vindo de mining FN ou shadow.
- `ProcessoValidacao` append-only via `UniqueConstraint(processo, usuario)`. Re-anotação proibida; divergência resolvida em `label_final` por revisor sênior (`can_resolve_disagreement`).
- 10% dupla-anotação automática para Cohen's kappa por anotador.
- `motivo` (texto livre) confidencial intra-equipe: helper `motivo_visivel_para(user)` + templatetag `{% motivo_visivel %}` checam ownership ou `can_view_motivo`.
- **LGPD/anonimização fora de escopo nesta versão.** Campo `usuario_hash` permanece no schema mas não é populado; `usuario` é `SET_NULL` apenas pra cobrir delete administrativo de User. Reativar quando abrir validação a anotadores externos.

**Alternativas rejeitadas:**
- *Permitir UPDATE da label* — destrói série temporal necessária pra detectar drift de anotador e calcular kappa.
- *Re-anotação livre* — efeito âncora (literatura de annotation research): anotador relê e "concorda consigo mesmo" mesmo errado.

**Consequência:**
- Dataset cresce monotonicamente; recálculos do gate v7 reprodutíveis dado snapshot temporal.
- Anotações ruins (kappa baixo) não removíveis — só ponderadas via `sample_weight` (ADR-019). Trade-off aceito por transparência.
- Follow-up: trigger Postgres UPDATE-block em `ProcessoValidacao` (hoje só constraint `UniqueConstraint` no insert). LGPD pode virar ADR-023 quando reabrir.

## ADR-019 — Pesos amostrais por origem do label no retreino v7

**Contexto:** v7 mistura 3 fontes de label: anotação humana, `LeadConsumption.resultado` do Juriscope, e CSVs históricos (`leads_trf1.csv`, `leads_trf1_recuperados_1327.csv`). Fontes têm confiabilidade muito diferentes — humano lê os autos completos, Juriscope marca após baixar autos (alto sinal mas com lag), CSV agregado tem ruído de extração.

**Decisão:** logistic regression treinada com `sample_weight` por origem:

| Origem | Peso | Racional |
|---|---|---|
| Anotação humana (`ProcessoValidacao.label_final` ou única label) | **3.0** | Olho humano sobre autos completos, mais confiável |
| Juriscope `LeadConsumption.resultado IN {validado,pago}` | **2.0** | Confirmação operacional, alto sinal |
| `leads_trf1_recuperados_1327.csv` (FN consumidos confirmados) | **2.0** | Equivalente a Juriscope (curado) |
| `leads_trf1.csv` (lista base) | **1.0** | Ground truth original, mais ruidoso |

**Alternativas rejeitadas:**
- *Treinar só com humano:* volume insuficiente (≤ 500/tribunal no go-live) — overfit garantido.
- *Igualar todas as fontes:* desperdiça sinal de qualidade humana, dilui correção dos FNs recuperados.
- *Filtrar conflitos (humano≠CSV) antes do treino:* descarta informação; melhor manter conflito e deixar otimização ponderar.

**Consequência:**
- v7 converge com gradient descent ponderado (`np.average(loss, weights=sample_weight)`).
- Dataset de gate v7 (REGRAS_NEGOCIO_VALIDACAO §3) inclui as 3 fontes, com humano dominando o sinal local.
- Trade-off: anotador com kappa baixo ainda contribui 3.0 — mitigação futura é multiplicar por kappa_individual.

## ADR-020 — Hot reload de pesos + shadow mode para A/B sem deploy

**Contexto:** Re-treinar v7/v8/... e propagar pra workers exige rebuild de imagem + force-recreate (15-30 min de janela). Inviável pra iterar threshold ou ajustar peso no curto prazo. E pra rodar A/B legítimo entre v6 (ativo) e candidata, precisa de comparação no MESMO universo de processos sem afetar `Process.classificacao` oficial.

**Decisão:**
- **Hot reload**: `tribunals.classificador._WEIGHTS_CACHE` mantém pesos da `ClassificadorVersao(ativa=True)` com TTL configurável (`CLASSIFICADOR_RELOAD_TTL`, default 60s). Cada `classificar()` cheka se TTL venceu e recarrega do DB via double-check locking. Fallback silencioso pra `HARDCODED_WEIGHTS` em DB-down/pesos corrompidos/sem versão ativa. `force_reload_weights()` pula TTL em testes/commands.
- **Shadow mode**: `ClassificadorVersao.shadow=True` (N podem coexistir) roda em job assíncrono separado (`classificar_shadow_async`) com sample rate configurável (`SHADOW_SAMPLE_RATE`, default 0.10). Resultados em `ClassificacaoShadowLog`, NÃO atualizam `Process.classificacao`. Comparação A/B via job `comparar_shadow` (cron 04:00 UTC) — calcula agreement rate, KS test entre score distributions, lista de disagreements pra revisão.
- Constraint `ativa=True` continua partial unique (1 ativa por vez). `shadow=True` não restrito (suporta N candidatas em paralelo).

**Alternativas rejeitadas:**
- *Restart de worker pra propagar:* 15-30 min de janela, prejudica iteração rápida e ainda tem race condition entre hosts.
- *Comparar via batch reclassify retroativo:* enorme custo computacional pra cada experimento. Shadow inline aproveita pipeline normal.
- *Substituir `ativa=True` por v7 e medir produção direto:* inaceitável — qualquer regressão atinge fila Juriscope.

**Consequência:**
- Re-treino v7 → ajustar pesos do DB → propaga em ≤ 60s.
- A/B legítimo: shadow só compara score; categorização é a mesma (ADR-022).
- Retention `ClassificacaoShadowLog`: 90 dias (job de cleanup é follow-up).
- Trade-off: features novas (F24-F28 do v7) não funcionam por hot reload — precisam deploy de código pra atualizar `compute_features`. Hot reload cobre só ajuste de pesos das features já conhecidas. Documentado na docstring do `classificador.py`.

## ADR-021 — Thresholds N1/N2/N3 por tribunal em ThresholdTribunal (DB-driven)

**Contexto:** TRF3 tem 15% taxa de lead (concentração de cumprimentos contra Fazenda em SP) vs ~2% no TRF1 — mesmo threshold N1=0.7 corta volumes muito diferentes. TJMG/TJSP têm menos cobertura via DJEN (intimações por correio físico, sigilo) — threshold maior protege precision até consolidar ground truth. Hardcoded em código exigiria deploy pra ajustar.

**Decisão:** tabela `ThresholdTribunal(tribunal, versao_modelo, threshold_precatorio, threshold_pre, threshold_dc, ativo)`:

| Constraint | Garante |
|---|---|
| `UniqueConstraint(tribunal, versao_modelo)` | 1 row por par |
| `UniqueConstraint(tribunal, versao_modelo) WHERE ativo=True` (partial) | só 1 ativo por (tribunal, versao_modelo) |

`_categorizar(score, features, tribunal_id, versao_modelo)` lê row ativa filtrando por versão. Fallback silencioso pros defaults hardcoded (`THRESHOLD_PRECATORIO=0.7`, etc.) se row não existir ou erro. Filtro por versão protege transição v6→v7 onde podem coexistir thresholds.

**Alternativas rejeitadas:**
- *Threshold único global:* TRF3 perde leads de borderline; TJMG/TJSP ficam ruidosos.
- *Tabela sem `versao_modelo`:* impossível ajustar thresholds independentes durante shadow A/B (v6 thresholds afetariam v7 e vice-versa).
- *Thresholds em settings.py:* exige deploy; sem auditoria de quem alterou e quando.

**Consequência:**
- Mudança requer `can_publish_model` (RBAC). UI bloqueia sem permission.
- Cadência de revisão trimestral + auto após cada `ClassificadorVersao.ativa=True` flip.
- Auditoria por `atualizado_por` + `atualizado_em` na própria row.
- Follow-up: CV interno no grid de thresholds v7 (hoje é só holdout único).

## ADR-022 — Categorização compartilhada entre classificar() e classificar_shadow()

**Contexto:** REVIEW_T20 issue média #1 detectou drift de lógica: `classificar()` (path ativo) usava thresholds hardcoded; `classificar_shadow()` lia `ThresholdTribunal` do DB. Resultado: `comparar_shadow` reportaria agreement rate artificialmente baixo — divergência viria de **política de threshold**, não de pesos do modelo. Inviabiliza A/B legítimo.

**Decisão:** extrair `_categorizar(score, features, tribunal_id, versao_modelo)` em `tribunals/classificador.py` como função única. Ambos os paths (ativo e shadow) chamam o mesmo `_categorizar` passando o `tribunal_id` do processo. Versão do modelo é parâmetro explícito, default `get_versao_ativa()`.

**Alternativas rejeitadas:**
- *Manter dois paths divergentes:* impede comparação shadow legítima — bloqueia gate v7.
- *Calcular thresholds em job offline e cachear em settings:* trade-off de complexidade não justificado; query é trivial.
- *Pular categorização no shadow (só comparar score):* perde-se sinal de movimentação entre níveis (N1↔N2↔N3), que é o que a fila Juriscope consome.

**Consequência:**
- Shadow A/B compara apenas o efeito dos PESOS (score) — categorização idêntica.
- Mudança de threshold em `ThresholdTribunal` afeta os dois paths simultaneamente.
- Removido issue média #1 da REVIEW_T20 — não bloqueia mais o flip v7.

## ADR-023 — Static files com hash via `STORAGES` (Django 5)

**Contexto:** após o deploy 2026-05-14 (commit 52b81cf), mudanças em
`voyager-identity.css` não chegavam ao browser dos usuários mesmo após
restart do `web`. O nginx serve `/static/` com
`Cache-Control: public, immutable, max-age=30d`, e o template referenciava
`{% static 'dashboard/voyager-identity.css' %}` que retornava o nome do
arquivo SEM hash (`voyager-identity.css`). Browser tratava como mesma URL
e usava a cópia em cache.

Investigação revelou que `settings.STATICFILES_STORAGE = 'whitenoise...'`
estava configurado, mas **Django 5.0+ ignora silenciosamente** essa setting
em favor da nova API `STORAGES = {...}`. Sem o manifest, `collectstatic`
não gerava `staticfiles.json` e o `{% static %}` caía no comportamento
padrão (URL sem hash).

**Decisão:** migrar pra `STORAGES` (API nova) com
`CompressedManifestStaticFilesStorage` em prod:

```python
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
}
```

Resultado:
- `collectstatic` gera `/app/staticfiles/staticfiles.json` com mapa
  `arquivo.ext → arquivo.HASH.ext`.
- `{% static 'dashboard/voyager-identity.css' %}` renderiza
  `/static/dashboard/voyager-identity.a1b2c3.css`.
- Toda mudança no source produz hash diferente → URL diferente → cache
  do browser e do nginx miss automaticamente → fetch da versão nova.
- `Cache-Control: immutable max-age=30d` continua válido (arquivo
  específico é imutável; só o que muda é o nome).

**Alternativas rejeitadas:**
- *Query string `?v=AAAAMMDD`* — gambiarra: exige bump manual a cada
  deploy de CSS/JS, esquecível, não cobre includes indiretos.
- *Desligar cache no nginx* — perde 99% das requests servidas do cache
  edge; degrada UX e custa CPU sem necessidade.
- *Versionar tudo via Git LFS / S3* — overkill pro projeto.

**Consequência:**
- Hot deploys de CSS/JS via `docker cp` + restart `web` (que roda
  `collectstatic`) entregam a nova versão imediatamente, sem hard refresh.
- Operacional não precisa lembrar de invalidar cache.
- Templates ficaram limpos (sem `?v=...`).
- Limitação: imagens/arquivos referenciados via `url()` no próprio CSS
  precisam usar caminho relativo pro Whitenoise re-escrever os hashes —
  é o comportamento padrão, mas vale lembrar em PRs futuros.

## ADR-024 — `POST /leads/consumed/` assíncrono + idempotente por lote (2026-05-16)

**Contexto:** O catch-up do Falcon (~268k processos) e o reporte diário do
Juriscope precisam ser à prova de falha — gravar consumo síncrono num request
HTTP perde dados se o request cair no meio, e reenvio cego (retry do cliente)
duplicava `LeadConsumption` (ADR-016 não tinha constraint nenhuma).

**Decisão:**
- `POST /leads/consumed/` passa a exigir `lote_id` (UUID) no body e responde
  `202 {enfileirado, lote_id, recebidos}` — só enfileira o job RQ
  `registrar_consumo_leads` na fila `leads_consumo` (worker
  `worker_leads_consumo`, 4 réplicas, `Retry(max=3, interval=[30,120,600])`).
- Idempotência via `UniqueConstraint(cliente, processo, lote_id)
  WHERE lote_id IS NOT NULL` + `bulk_create(ignore_conflicts=True)` no job:
  retry RQ ou reenvio do mesmo lote nunca duplica nem perde linha.
- `LeadConsumption.lote_id` nullable — NULL = registros legados pré-cutover
  (re-consumo solto continua suportado, ADR-016).
- `503` no enqueue (Redis fora) sinaliza ao cliente pra retentar; nada se perde.

**Scheduler:** `run_daily_ingestion` de TRF1 às **02:00** e TRF3 às **02:30**
(`djen/scheduler.py::EARLY`) — pré-processa cedo pra que o pull diário do
Falcon às 08:00 já encontre tudo classificado.

**Consequência:** Zero perda / zero duplicata no reporte de consumo mesmo sob
falha de rede ou restart de worker. Trade-off: cliente não recebe mais o
resultado de gravação na hora (criados/duplicados ficam no resultado do job RQ).
