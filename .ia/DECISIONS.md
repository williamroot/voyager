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

> **Correção (2026-06-17, medido em prod):** o bloqueio do pool na **DJEN** é
> ~**100%**, não ~80% — amostra de 29 IPs do pool em `comunicaapi.pje.jus.br`
> deu **0 sucessos** (28× HTTP 403 + 1× 500). Na prática a ingestão DJEN é
> **Cortex-only**: o "20% pool" não substitui o Cortex, e o fallback pro pool
> (`prefer_other_than` + `mark_cortex_bad`) cai em 403. **Implicação: a ingestão
> DJEN é SPOF no gateway Cortex** — se ele cair, a ingestão para (o pool não
> cobre). O Cortex também **flapa**: em 2026-06-17 falhou 100% por ~15-20min
> (ProxyError em qualquer host) e se recuperou sozinho. Isso vale **só pra DJEN**;
> e-SAJ (TJSP/TJAL) responde pelo pool — ver ADR-021.

## ADR-021 — e-SAJ TJAL roteia pelo pool ProxyScrape (não pelo Cortex)

**Contexto:** `TjalEnricher` nasceu com `PREFER_CORTEX=True` (2026-05-30) sob a
premissa de que `www2.tjal.jus.br` dava ReadTimeout em 100% do pool datacenter.
Reavaliação em prod (2026-06-17): o pool responde ~**37%** dos IPs (página e-SAJ
válida em ~1s; os ReadTimeouts são IPs mortos do pool, não bloqueio do TJAL), e
com `MAX_PROXY_ROTATIONS=8` isso dá ~99,8% de sucesso por processo.

**Decisão:** `PREFER_CORTEX=False` no `TjalEnricher` (usa o pool) + `worker_tjal`
8→24 réplicas. Diferente da DJEN, o e-SAJ **funciona pelo pool**.

**Consequência:** o TJAL deixa de competir pelo gateway Cortex escasso — que a
**DJEN precisa com exclusividade** (ADR-006) — e ganha resiliência paralelizando
pelos 2500+ IPs do pool. Padrão: **DJEN → Cortex; e-SAJ → pool**.

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
- Velocidade de ingestão (`warm_ingestao_por_hora`): a cada 15 min (só **lê** a MV pro cache)
- Refresh da MV `mv_ingestion_rate_hora` (`refresh_ingestion_rate_hora`): a cada 30 min — **dedicado**, fora do refresh diário (ver abaixo)
- Demais MVs (`refresh_materialized_views`: `mv_volume_diario`, `mv_pipeline_diario`, `mv_tribunal_kpis`): cron diário 03:00

**Consequência:** Zero acúmulo de jobs na fila. Sem dependência de worker externo pra dashboard funcionar. Falha de 1 warm job não afeta os outros (thread pool isolado). Trade-off: warm jobs pesados (charts_pesados, estatisticas) ocupam threads do scheduler por até 30-60min — mitigado pelo pool de 20 threads.

**Adendo (2026-05-28):** `mv_ingestion_rate_hora` saiu do refresh diário. Ela alimenta o gráfico "Velocidade de ingestão" (janela rolante de 24-72h), então refresh 1x/dia a deixava stale perto do horário e — quando o scan de 7d estourava o `statement_timeout` de 3600s sob carga — stale por dias, mostrando falso "Sem ingestão" com a ingestão rodando normal (incidente: MV 41h velha vs 11M+ movs inseridas/24h). Correção: refresh dedicado a cada 30min + janela encolhida 7d→4d (migration 0034) + read resiliente que distingue "MV defasada" de "sem ingestão".

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

## ADR-025 — Dashboard de saúde do pipeline: camada de dados híbrida (DJEN live + MV)

**Contexto:** Investigação de um alerta "Sem ingestão nas últimas 24h" revelou que era
fim de semana — esperado, não falha. A investigação também expôs um bug latente em
`_dia_coberto`: qualquer run com lista `success` vazia sombreava o dia como coberto
mesmo sem movimentações. A dashboard existe para tornar esses cenários visíveis e
distinguíveis (verde/amarelo/vermelho/cinza). O fix do bug `_dia_coberto` é tracking
separado, fora do escopo desta entrega.

**Fontes:**
- `djen`: lido **live** de `IngestionRun` — `MAX(janela_fim)` por tribunal/dia.
  Motivo: overlap idempotente de janelas faz `SUM` duplicar o mesmo dia; `MAX` dá
  a data correta sem double-count. Não faz sentido pré-agregar na MV.
- `datajud`, `pje`, `classif`: lidos da **MV `mv_pipeline_diario`** — `COUNT(Process)`
  por `(tribunal, dia)` usando as colunas `data_enriquecimento_datajud`,
  `enriquecido_em`, `classificacao_em` respectivamente. MV criada na migration `0029`.

**Por que MV via migration (e não criada na mão):**
MVs criadas manualmente em prod divergem do git — qualquer rebuild ou restore de dump
perde a view. Criar via `RunSQL` na migration garante que o schema está sempre em sync
com o código.

**Regra de cor:** baseline = mediana das últimas 4 ocorrências do mesmo tipo de dia
(seg/ter/.../dom) por tribunal/fonte. Verde ≥ 60%, amarelo ≥ 20%, vermelho < 20%
(dia útil), cinza = fim de semana ou sem baseline.

**Limitação aceita:** feriado forense vira falso-vermelho. Calendário de feriados está
fora de escopo — impacto operacional baixo (poucos dias/ano, facilmente reconhecíveis
quando todos os tribunais ficam vermelhos no mesmo dia).

**Alternativas rejeitadas:**
- *MV única pra DJEN também:* duplicaria contagem em janelas sobrepostas.
- *Leitura live de Process pra tudo:* query pesada; MV é ~100× mais rápida pra séries históricas.
- *MV criada fora do Django (script de deploy):* diverge de prod em restore/rebuild — o problema que a migration resolve.

**Consequência:** Dashboard renderiza em <200ms. Schema sempre em sync com o código.
Feriados geram falso-vermelho (aceito). Bug `_dia_coberto` fica visível no heatmap
(tracking separado).

**Follow-up (implementado):**

(a) **Fix `_dia_coberto` com horizonte recente:** dias dentro de `hoje − overlap_dias`
só contam cobertos se o run `success` teve dados (`novas | duplicadas | paginas > 0`).
Decisão: não quebrar o backfill de dias antigos vazios — feriados/recesso histórico
(fora do horizonte) continuam aceitos com qualquer `success`, evitando re-processamento
de ruído. Apenas o horizonte recente exige dados reais pra fechar o bug latente.

(b) **Dashboard sintetiza dia útil ausente como vermelho:** dia útil sem run de
ingestão (`< hoje`, tribunal ativo com backfill concluído) agora é gerado como célula
`volume=0, cor=vermelho` em vez de ficar ausente (que aparecia como cinza / invisível).
Isso fecha o gap onde "ausente == cinza" escondia lacunas reais de ingestão no heatmap.

## ADR-026 — Charts/KPIs do dashboard: sargabilidade + MV-backing dos agregados pesados (2026-05-29)

**Contexto:** Storm de `statement_timeout` no scheduler — `warm_kpis`, `warm_charts_*`
e `warm_tribunal_status` cancelando. Causa não era falta de recurso, e sim queries de
agregação que não terminavam (1 fazia `SELECT DISTINCT process.*` + Merge Join 6M×204M
por 18-30min, segurando 4 parallel workers e estrangulando as outras). Agravado pela
tabela `movimentacao` ter crescido pra ~614M com o backfill.

**Decisão:**
- KPIs com período: `COUNT(DISTINCT processo_id)` sobre movs filtrados (não `DISTINCT`
  da linha inteira de Process); filtros de data **sargáveis** (`__gte=<timestamp>` em vez
  de `__date__gte=<date>`, que cancelava o índice btree); headline counts de `dias=None`
  via `pg_class.reltuples` (O(1)) em vez de `COUNT(*)` exato em ~614M.
- Agregados de série temporal saem de cálculo ao vivo pra MV: `volume_temporal` diário
  ← `mv_volume_diario` (já existia, mas **não era lida por ninguém**); `volume_temporal`
  mensal/None **e** `compute_tribunal_status` ← nova `mv_volume_mensal`. Ambas refreshadas
  no `refresh_materialized_views` diário; readers têm fallback live enquanto a MV não foi
  populada (`relispopulated`).

**Consequência:** `compute_kpis_globais(7)` 18-30min→~14s; `dias=None` 479s→~40s;
`volume_temporal(≤365d)` 407s→~0.03s; some o TruncMonth recorrente em 614M do ciclo de
warm. Trade-off: gráficos de volume ficam com staleness de até ~1 dia (refresh diário) e
counts de overview viram estimativa reltuples — aceitável pra headline/tendência; números
exatos por período continuam via query filtrada (barata e sargável).

## ADR-027 — Extrator v2 "Ficha da Parte": extração por-documento + merge determinístico (2026-07-28)

**Contexto:** O extrator v1 (Qwen2.5-7B QLoRA, macro 87,9) ficou com partes=54%.
Diagnóstico medido: erro de CONTEÚDO/cobertura, não formato — grammar não moveu
(0,533→0,529), modelo maior (120B) também não; o modelo gerava a lista de partes
one-shot vendo ~6 chunks e nunca via metade dos herdeiros. Meta do produto: ficha
COMPLETA por parte (papel, CPF, valor a receber, recebido via alvarás, saldo) +
camada jurimetria (juiz por decisão, vara, relator), operando SEM Falcon em
produção (Falcon só como professor no treino).

**Decisão:**
- **Extração POR-DOCUMENTO** condicionada a `doc_classe` (1 doc → registros JSON),
  não por-processo: completude vem do pipeline (lê TODOS os docs relevantes), não
  da memória do modelo; labels por documento são mais densos; doc cabe no ctx.
- **Merge determinístico** (`acervo/ficha_parte.py`): entidade por CPF (chave
  forte; mascarado NÃO conta), fuzzy conservador com guardas anti-mescla, espólio
  LIGADO (não mesclado), alvará nunca cria parte (órfão flagado), saldo sempre
  DERIVADO, conflito → abstém. LLM aponta; código decide.
- **Especialistas por tier, não por campo**: regex (cessão), classificador pequeno
  futuro (natureza), 7B único por-documento pro resto. Base swap rejeitado (120B
  não consertou; o lever é cobertura documental).
- Pré-requisito F0: classe ALVARA no doc_classificador (achou 171.667 alvarás,
  94% escondidos em ADMIN/OUTROS) — fonte do "já recebeu".

**Alternativas rejeitadas:** modelo maior (não resolve, mais caro); grammar mais
estrita (formato não era o problema); rotular partes por-processo com mais chunks
(estoura contexto e mantém one-shot).

**Consequência:** dataset novo de 196.360 exemplos por-documento (55.847 CNJs,
κ=13.470), treino v2 com caps por tarefa (anti "extrator de juiz": ofício+decisão
eram 92% do cru). Custo: pipeline de inferência passa a iterar por documento
(mais chamadas/processo, mitigado por docs curtos) e ganha um estágio de merge.
Registro completo: `.ia/MODELOS.md` + `.ia/FICHA_PARTE.md`.

## ADR-028: Elasticsearch + MinIO pra API Jusbrasil-compat

**Data**: 2026-08-01

### Contexto
A API Jusbrasil/Digesto usa Elasticsearch pra busca textual com query DSL 7.14.
O Voyager usava busca híbrida Postgres (tsquery + ILIKE GIN trigram). Pra ser
100% compatível, precisamos aceitar a query DSL nativa.

### Decisão
1. **Elasticsearch** dedicado (cluster novo) como índice de busca. Postgres
   continua como source of truth; ES é write-through via signals (fila `es_index`).
2. **MinIO** (S3-compat local) pra armazenar PDFs das movimentações
   (`Movimentacao.link` → `cached_docurl`). Download async via RQ.
3. **MCP server** exposto em `/mcp/` pra LLMs/agentes conversarem sobre processos
   sem implementar HTTP. 12 tools + resources. Auth por `mcp_token` (UUID).
4. **Sem busca por comarca**: granularidade = tribunal. Documentado.

### Consequências
- +2 containers (ES 4GB heap, MinIO 1GB).
- Reindex backfill de ~600M movs pode levar dias (throttle + batch).
- PDF storage cresce ~550MB/dia (política de retenção futura).
- API de busca tem SPOF em single node ES (mitigar com cluster em prod).

## ADR-029: segunda porta de entrada — varredura do Datajud (esqueleto nacional)

**Data**: 2026-08-14

### Contexto
A base sempre teve UMA porta: o DJEN. Só que o DJEN é veículo de **comunicação**
(Res. CNJ 455/2022), não cadastro de processo — entra quem teve intimação
publicada em diário. Medimos a consequência com amostra aleatória de 300 CNJs
por tribunal, conferida um a um: falta de **81% (TRF1) a 96% (TJSP)**; o CNJ
declara **343.235.554** processos contra os 71,4M que tínhamos. No recorte que é
o produto (classe 12078, Cumprimento contra a Fazenda) a cobertura era **5,2% no
TJSP**. O gatilho foi concreto: `5229078-89.2022.8.13.0024` existe no Datajud com
10 movimentos e tem ZERO publicação no DJEN — nunca teve como entrar.

O Datajud já era usado, mas só como ENRIQUECEDOR de CNJ que já conhecíamos:
pra perguntar por um processo, era preciso já tê-lo. Ovo e galinha.

### Decisão
1. **Varredura em massa do Datajud** (`datajud/varredura.py`) como segunda porta,
   gravando num índice ES **separado** (`voyager-acervo`), não no Postgres.
2. **Só esqueleto**: sem movimentos. O Datajud não tem parte nem valor, e os
   movimentos (~73/processo) inviabilizariam o volume (343M × 73 ≈ 10B linhas).
3. **Índice separado, não `voyager-processos`**: a procedência precisa ficar
   estrutural. "Existe no CNJ" ≠ "temos o processo" — misturar faria 343M docs
   ocos diluírem a busca por nome/documento, que é a mais usada.
4. **Enriquecer é escolhido, nunca automático**: a 112.820 processos/dia e com
   enricher em 16 dos 60 tribunais, varrer tudo levaria 8 anos. O esqueleto
   (classe/assunto/órgão) é o instrumento pra priorizar sem gastar requisição.
5. **Cota própria** (`DATAJUD_VARREDURA_RPM`, default 40) pega ANTES da global:
   34 mil requisições em série não podem faminar quem atende usuário.

### Alternativas descartadas
- **Ingerir tudo no Postgres**: a base já é I/O-bound com 71M processos e 1,16B
  movimentações (326 GB); somar 343M seria trocar um problema por outro maior.
- **Índice único com flag `so_esqueleto`**: uma flag que alguém esquece de
  filtrar vira dado errado na tela. Índice separado erra alto, não baixo.
- **`search_after` pra paginar**: o Datajud só ordena por `@timestamp`, que
  empata — `search_after` puro PULA documento silenciosamente. Ver ACERVO_CNJ.md.

### Consequências
- +77 GB no ES (medido: ~225 B/doc). Cabem nos 718 GB livres.
- A busca por CNJ passa a ter dois resultados possíveis, e a tela tem que dizer
  qual é qual — "sem parte e sem valor" é parte do contrato, não rodapé.
- Ganhamos watermark incremental de graça (`@timestamp` = última atualização):
  a completude se mantém viva em vez de virar foto de um dia.
- Passa a existir "hidratação" como conceito: processo pode estar na base em
  dois estados (esqueleto, completo), e isso vira dívida de UX em toda tela que
  listar processo.

## ADR-030: terceira porta — diários oficiais próprios (2026-08-16)

### Contexto
A ingestão sempre foi DJEN-only, e o DJEN é veículo de **comunicação**
(Res. CNJ 455/2022). A ADR-029 abriu a segunda porta (varredura do Datajud) para
o **esqueleto** nacional. Faltava o texto verbatim do que nunca passou pelo DJEN.

O achado que forçou o assunto: no TJSP, **96,5%** dos processos do sistema SAJ
estão ausentes contra **52,9%** do Projudi — mesmo tribunal. Não é falha de
ingestão: o TJSP publicava no **DJE próprio (e-SAJ)** e só entrou no DJEN em
**14/03/2025**. Medido do mesmo jeito: a Justiça do Trabalho migrou em
**01/08/2024** (o caderno do TRT3 caiu de 69 MB para 1,5 MB de um dia para o
outro) e o **STF nunca entrou** (`siglaTribunal=STF` devolve o mesmo HTTP 500 de
uma sigla inventada). O buraco é **temporal**, não geográfico.

### Decisão
1. **Um contrato (`diarios/base.py`), uma fonte por diretório.** Quatro
   implementações em paralelo só não viram quatro coletores diferentes se houver
   contrato. Registro por `@registrar` + auto-descoberta — sem lista central,
   sem conflito de merge. Alterar `base.py` para implementar uma fonte é sinal de
   que o contrato está errado.
2. **A unidade de coleta é EDIÇÃO-CADERNO, não dia.** Um dia do DJE/TJSP são 9
   cadernos independentes (um com 2.001 páginas); um dia do DEJT são 25, um por
   tribunal. "O dia" como unidade obrigaria a re-baixar 68 MB porque um caderno
   falhou, e impediria o paralelismo educado (por tribunal, não por página).
3. **Catálogo separado da coleta.** Nas duas fontes grandes o índice inteiro sai
   em UMA requisição (e-SAJ: 4.162 edições; DEJT: 95.679 linhas de 18 anos).
   Medir o acervo antes de baixar centenas de GB é o gate mais barato que existe.
4. **Dedupe por janela de exclusividade**, não por comparação de conteúdo. Cada
   fonte declara o período em que é a única porta, medido contra o DJEN. No caso
   normal a interseção é vazia. Namespace no `external_id` e `fingerprint_ato`
   são camadas 2 e 3, para o que coexiste.
5. **Sem coluna `fonte` em `Movimentacao`** (medido 20/08/2026: 1,39B linhas / 815 GB num Postgres já
   disk-I/O-bound) — o prefixo do `external_id` discrimina. **Com** coluna
   `fonte` em `IngestionRun`, porque sem ela um run do DJE/TJSP fazia o
   `_dia_coberto` do DJEN pular o dia: perda silenciosa.
6. **Abstenção é status, não silêncio.** `inexistente` (a fonte diz que não há
   unidade) e `sem_aproveit` (havia publicação e nada é aproveitável) são
   terminais e distintos de `vazia`. Sem essa distinção, 16.952 blocos reais de
   um caderno pré-CNJ fechavam como "não havia nada", com o run verde.
7. **O agendamento nasce DESLIGADO** (`DIARIOS_SCHEDULER_ENABLED=False`) e há
   kill switch por fonte em cache (`manage.py diarios_pausar`).

### Alternativas descartadas
- **Um coletor por fonte, sem contrato.** Barato no dia 1, caro na quinta fonte,
  e cada um reinventaria backoff/breaker/watermark — tudo já pago em incidente
  no DJEN.
- **Registrar as fontes em `FonteDiario`.** É a tabela de compatibilidade
  Jusbrasil/Digesto: PK é o `source_id` DELES e a relação com `Tribunal` é 1:1.
  Caberia inventar um id de terceiro, e o DEJT (25 tribunais) não caberia.
- **Escrever coletor para o STJ.** Ele aderiu ao DJEN em 29/11/2024 e o payload
  bate 100% com `djen/parser.py` (200 itens, zero drift). Vira migration +
  comando, não código de coleta.
- **Ligar o STJ com `ativo=True` numa migration.** O `watchdog_ingestao`
  dispararia `tick_backfill_retroativo` na hora e jogaria ~5,3M de movimentações
  na fila, disputando I/O com extração e vetorização.
- **Raspar o DJe em PDF do portal legado do STF** (2021-2022): morto desde 2023 e
  majoritariamente caderno administrativo — 26 CNJs em 127 páginas.
- **Estender os DOEs de entes a RS/MG/BA/PR/RJ/CE/PE/SC**: TLS quebrado, NXDOMAIN
  e sessão PHP por engenharia própria, para <0,2 documento/dia cada.

### Consequências
- Ganhamos texto verbatim de período que o DJEN estruturalmente não tem, mais
  **relator** e **envolvidos estruturados** (STF), que não existiam no acervo.
- **Volume é o risco, não a técnica**: um caderno-dia do TJSP rendeu 29.136
  movimentações; o backfill completo é da ordem de **400-600 milhões de linhas**.
  Colide com `PARTICIONAMENTO_ACERVO_CHUNK.md`. Ligar em série sem recorte não é
  decisão do coletor — por isso o scheduler nasce desligado.
- **Metade do DEJT fica de fora** (46.845 edições pré-2018, formato de prosa
  corrida) e a era pré-CNJ do TJSP também. Abstenção declarada e visível.
- O CNJ de **origem** do STF/STJ passa a criar `Process(tribunal=STF,
  numero_cnj=<CNJ do TJ>)` ao lado do que o TJ já tem — decisão humana pendente,
  travada em teste, mesmo nó dos "incidentes CNJ vinculados".
- `achar_cnjs` passou a conferir **dígito verificador** e adjacência de dígito: a
  tolerância a espaço (que recupera 8% dos processos escondidos pelo kerning)
  também decapitava número maior e devolvia o CNJ de OUTRO processo.

---

## ADR-031: extrator de PDF do DJE/TJSP — PyMuPDF no lugar do pypdf (2026-08-20)

### Contexto
O `tjsp-dje` é a maior das três portas: 4.162 edições × ~9 cadernos ≈ 37 mil
cadernos, de 567 a 4.229 páginas cada. O caderno baixa em 0,2-1,2 s e o `rps`
é 1,0 — ou seja, o gargalo desta fonte **não é rede, é CPU de extração**.

Medido em 20/08/2026 no caderno 3 (Capital Parte I) de 12/03/2025 — 36 MB,
4.229 páginas, contando CNJ **distinto** com a regex ESTRITA sobre o texto:

| extrator | tempo | CNJs distintos |
|---|---|---|
| PyMuPDF (MuPDF/C) | 16,3 s | 32.366 |
| pdftotext (poppler) | 17,3 s | 32.366 |
| pypdf 5.1.0 | 184,0 s | 27.483 (**-15,1%**) |

A causa dos 15,1% está medida: o pypdf insere espaço espúrio no meio do próprio
número (`1127986- 08.2023.8.26.0100`). Nas mesmas 600 páginas, 1.198 CNJs
quebrados contra 314 do MuPDF. É o **mesmo** defeito que produzia
'Estado de S ão Paulo' e 'Banco Rodoben s S/A' no `texto` verbatim — que o
`pdf.py` e este documento atribuíam ao PDF ("kerning por caractere no operador
`TJ`", "consertar seria chutar em cima de documento oficial"). **Estava errado:
era do extrator.**

### Decisão
Trocar o extrator **apenas do `tjsp-dje`** (`diarios/fontes/tjsp_dje/pdf.py`)
por PyMuPDF 1.24.14. `pypdf` **continua** no `requirements.txt`: o DEJT
(`segmentador.py`, outline do PDF) e o `dashboard/chat_arquivos.py` usam.

O `visitor_text` do pypdf (chamado por trecho desenhado) foi trocado por
`page.get_text('dict', sort=False)`, e não por `'blocks'`: 'blocks' devolve o
texto já colado, **sem o tamanho da fonte** — que é a tese inteira deste módulo
(título de seção × corpo × mobília só se distinguem por corpo de fonte). 'dict'
desce até o `span`, com `size` (já com a escala do text matrix aplicada, o que
resolve o `Tf=1` do e-SAJ) e `origin`. O agrupamento por linha de base continua
sendo nosso, porque a grade de distribuição vem em **duas colunas** e só a fusão
por Y mantém `PROCESSO :` e o valor na mesma linha; `sort=True` reordenaria para
ordem de leitura e desmontaria o formato `lista`.

### Alternativas descartadas
- **`pdftotext` (poppler)**: mesma velocidade, mas não devolve tamanho de fonte
  nenhum. Sem ele não há hierarquia de seção — o coletor viraria regex sobre
  linha solta, que é o erro que cola o ato de um processo no do vizinho.
- **`pypdfium2` (BSD, o que evitaria a AGPL)**: lê 567 páginas em 2,3 s, mas
  `FPDFText_GetFontSize` devolve **1.0 para todos os caracteres** deste PDF — a
  mesma armadilha do `Tf=1`. Exigiria reconstruir a escada de tamanhos a partir
  da altura da caixa do glifo, ou seja, inventar o dado que o segmentador usa
  para decidir. Fica registrado como saída se a licença virar problema.
- **Trocar o DEJT na mesma mudança**: o ganho lá é maior (229 s por caderno-dia,
  gargalo declarado da fonte), mas o uso é outro (`extract_text()` achatado +
  `outline`/`get_destination_page_number` → `doc.get_toc`) e o risco é outro
  (`nome_orgao`, `tipo_comunicacao`). Outra mudança, outro gate.
- **Manter o pypdf e "juntar" as palavras por heurística**: adivinhação em cima
  de documento oficial, e nome de parte é o que menos aceita chute.

### Consequências
- **O ganho NÃO é cobertura de CNJ, e o commit não pode dizer que é.** Rodando o
  segmentador real sobre o caderno de 4.229 páginas, os dois extratores acham os
  **mesmos 32.575 CNJs**, fecham os **mesmos 29.106 blocos** e dão a **mesma
  cobertura (99,751%)**, com os mesmos formatos e `sem_cnj=69` — porque a
  `CNJ_TOLERANTE` já absorvia o espaço espúrio. Diferença de CNJ aproveitado nos
  dois sentidos: **zero**.
- O ganho real é: **6,4× de CPU** (192,1 s → 30,1 s de segmentação), **3,2× de
  RAM** (pico de RSS 406 MB → 125 MB) e **qualidade do verbatim** ('Paul o'
  579 → 0, 'S ão' 27 → 0; CNJ partido 516/9,84% → 215/4,10%, e os 215 restantes
  são quebra de LINHA real, verdade impressa). Em backfill: ~2.000 h de CPU
  contra ~310 h.
- **Idempotência**: `external_id` e `hash` são sha1 **do texto**, e o MuPDF
  quebra linha de forma levemente diferente (320.099 contra 321.811 linhas no
  mesmo caderno) ⇒ **todo `external_id` muda**. Recoletar unidade já coletada
  gravaria de novo em vez de contar `dup`. A janela para trocar era agora:
  produção tem **0 `IngestionRun` não-DJEN e 0 `EdicaoDiario`**. Depois do
  backfill, esta troca deixa de ser barata.
- **Licença**: PyMuPDF é **AGPL-3.0** (ou comercial da Artifex). O Voyager é
  serviço em rede proprietário; a AGPL alcança quem oferece o serviço pela rede.
  A dependência é **interna** (worker de coleta, não servida ao usuário), mas
  isso é mitigação, não parecer. Se a avaliação jurídica reprovar, a saída é o
  `pypdfium2` com a escada reconstruída pela caixa do glifo — trabalho de dias,
  não de meses.
- **Deploy**: rebuild obrigatório de `web` **e** dos workers; nenhum host tem
  `pymupdf` hoje e o import é tardio, então sem rebuild a fonte levanta
  `RuntimeError` legível na coleta (não some do registro em silêncio).

## ADR-032: busca textual sai do Postgres e vai pro Elasticsearch (2026-08-20)

**Contexto.** `tribunals_movimentacao` tem 1.385.659.648 linhas e 815 GB só na
coluna `texto`. O model declarava três índices — `mov_texto_trgm` (GIN trigram),
`mov_search_vector_gin` e um btree em `hash` — e o `.ia/DATA_MODEL.md` descrevia
até o trigger que manteria a `search_vector`. **Nenhum dos três existe no banco,
e o trigger nunca existiu.** Conferido coluna a coluna em `pg_index`: a tabela
tem 8 índices e a PK, e nenhum cobre `texto`, `search_vector` ou `hash`.

O estrago não foi a lentidão; foi a crença. Três trechos independentes leram a
declaração e escreveram a mentira como certeza:

| onde | afirmava | medido |
|---|---|---|
| `api/filters.py` | "ILIKE %x% usa o índice GIN trigram" | Seq Scan, custo **111.195.298** |
| `diarios/base.py::fingerprint_ato` | `hash` "já existe e já é indexada" | custo **73.427.276** por lote, no caminho de ESCRITA |
| `.ia/DATA_MODEL.md` | trigger `mov_search_vector_trg` | não existe |

E o pior caso não era o lento, era o que **respondia**: a busca da API com 3+
palavras usava `search_vector` + `SearchRank`. A coluna existe, ninguém a
preenche, e ela está NULL em 99,8% da tabela — cheia só até `id≈4.876.372`
(13/03/2024), 2.753.688 linhas de 1.385.659.648. Uma busca de três palavras
varria **0,199% do país** e devolvia "encontrei isto" com run verde e log limpo.
É a terceira linha da tabela do CLAUDE.md acontecendo de novo, com a diferença
de que desta vez ninguém tinha pisado nela: `pg_stat_statements` (desde 14/05)
não registra uma única chamada. Era dívida a uma querystring de distância.

**Decisão.** A busca por texto é do Elasticsearch. `search/busca_api.ids_por_texto`
consulta o `voyager-movimentacoes-v2` — que desde 18/08 tem o acervo inteiro,
depois dos 179.490.613 documentos que faltavam — e devolve **PKs**; o Postgres só
hidrata por chave primária. Os dois caminhos alcançáveis por HTTP (`?q=` da tela,
`?q=` da API) passam por lá.

**Por que não criar os índices.** GIN trigram sobre 815 GB de `texto`; GIN sobre
uma `search_vector` que está vazia em 99,8% das linhas (indexar o vazio); btree
em `hash` sobre 1,39B de linhas num Postgres que a casa já classifica como
disk-I/O-bound, para parear campos que nem casam entre si — o fingerprint é sha1
de 40 chars e o `hash` do DJEN é o opaco da API, de 30. O índice que serve para
isso já existe e está no ES.

**O que a medição no cluster obrigou a mudar no desenho.** A primeira versão
disto ordenava por `publish_date` "porque a tela é cronológica", sem janela.
Medido em produção (mediana de 3, frase de 3 palavras):

| consulta | mediana |
|---|---|
| sort por data, sem filtro | **20,76 s** |
| sort por score, sem filtro | 8,07 s |
| + tribunal TJSP | 4,44 s |
| + janela de 31 dias | 2,52 s |
| **+ TJSP e janela de 31 dias** | **0,37 s** |
| + janela de 365 dias | 7,72 s |

Ordenar 1,4 bilhão de documentos por data obriga o ES a tocar todos os
casamentos; por score ele termina cedo. Daí a ordem ser de RELEVÂNCIA — e a tela
DIZER isso, porque lista ordenada por um critério e rotulada com outro é mentira
barata.

E a causa da lentidão restante não é CPU nem a forma da consulta: com o cluster
em `cpu=2%`, `load1=6,77`, fila de busca zerada e zero rejeições, a mesma
consulta repetida três vezes dá **9,28 → 3,95 → 0,79 s**. É I/O: 1,3 TB de
índice num nó só, e as postings do termo não cabem no page cache. A média
histórica do cluster é 0,35 s/query (1.043.283 s em 3.011.643 consultas) — o que
custa é o termo FRIO.

Consequência prática: busca por texto tem **janela** (padrão 31 dias, chips na
tela, `busca_janela` ecoado na API) e teto de espera de 12 s. Primeira busca de
um termo raro pode estourar; a segunda é sub-segundo. O aviso diz "demorou e foi
interrompida — estreite", nunca "fora do ar", que mandaria o plantão procurar no
lugar errado.

O lever de capacidade, se essa cauda incomodar, é RAM/disco no nó do ES — não
mais índice.

**Consequências.**
- `0051_indices_fantasma`: migration **só de estado** (`database_operations=[]`).
  O banco já estava certo; quem mentia era o model.
- Teto de 2.000 PKs por busca, com `truncado` no retorno — a tela mostra faixa
  amarela e a API devolve `busca_teto` no corpo. Teto é alerta, nunca corte mudo.
- ES fora do ar **não cai pro Postgres**: a tela avisa e a API devolve erro.
  Trocar "não sei" por resposta errada, varrendo 815 GB para errar, é o pior dos
  dois mundos.
- `tests/test_busca_texto_no_es.py` varre o código-fonte (por AST, ignorando
  docstrings) e falha se `texto__icontains`/`search_vector` voltarem ao caminho
  da requisição. Comentário não segura regressão.
- A coluna `search_vector` **fica**. Derrubá-la em 1,39B de linhas é decisão de
  operação, não de refactor, e ninguém mais a consulta. Está marcada como morta
  no `DATA_MODEL.md`.

## ADR-033: sobreposição entre portas se mede por (processo, data) (2026-08-20)

**Contexto.** `diarios/base.py::espelhadas_no_lote` responde "quantos atos deste
lote a outra porta já tinha trazido" — é a régua do princípio nº 5, medir a
completude dos dois lados. Ela rodava a cada lote da ingestão de diários, que
acabou de ser ligada nos 59 tribunais.

Estava errada de duas formas ao mesmo tempo, e as duas passavam verdes:

1. **Não podia acertar.** Comparava `fingerprint_ato` — sha1, **40 chars** — com
   `Movimentacao.hash`, que nas linhas do DJEN é o hash opaco da API, **30 chars**
   (`djen/parser.py:243`; medido por amostra em prod: onde não é vazio, len=30).
   Uma string de 40 nunca é igual a uma de 30 ⇒ o resultado era **0 por
   construção**. E `espelhadas=0` lê-se "o diário próprio não repete o DJEN", que
   é a conclusão oposta da verdade, dita com a autoridade de um número.
2. **Custava caro pra errar.** `hash` não tem índice (ADR-032). EXPLAIN do lote
   de 200: custo **73.427.276** no TJSP, **6.980.195** até no TJAC — por lote,
   dentro do caminho de escrita, sem teto de espera.

O teste que a cobria construía o item do DJEN **com o fingerprint**, que é
justamente o que a produção não faz. Ele não validava a métrica; validava uma
ficção montada para ela passar.

**Decisão.** Parear por `(processo, data_disponibilizacao)` — o único par que os
dois veículos de fato compartilham, já que o texto verbatim difere entre eles de
propósito. Usa `mov_processo_data_disp_idx`, que existe.

**Consequências.**
- A métrica é declaradamente **aproximada e por cima**: dois atos distintos do
  mesmo processo no mesmo dia contam como um espelhamento. Superestimar
  sobreposição erra pro lado seguro; subestimar venderia ineditismo que não temos.
- `statement_timeout` de 3s: métrica **nunca** segura escrita. Estourou, devolve
  `None` — e `None` não vira 0. O runner marca o total com `>=` no log e
  `espelhadas_parcial` no retorno.
- Fuso: a data sai de `timezone.localdate()`, não de `.date()`. O ORM converte
  para `America/Sao_Paulo` antes de truncar, e extrair em UTC punha o lote de
  meia-noite no dia anterior do outro lado — perdendo exatamente a sobreposição
  que a métrica veio buscar.
- `tests/test_diarios_espelhadas.py` trava a regressão: há um teste que falha se
  a coluna `hash` voltar ao SQL da métrica.

---

## ADR-034 (PROPOSTA — não implementada): worker que se recicla quando o código muda

> **Status: PROPOSTA.** Nada disto está no código. O pipeline de deploy segue
> como está em [`DEPLOY.md`](../DEPLOY.md) até haver decisão explícita. Escrito
> em 24/08/2026, depois de reciclar 256 workers à mão.

**Contexto — o defeito é do processo de deploy, não de um container.**

O `docker-compose-workers.yml` monta o repo com bind mount `.:/app`. O
`git pull` no host aparece dentro do container **imediatamente**, e é
exatamente isso que dá a impressão de deploy feito. Só que o processo Python já
tem os módulos em `sys.modules`: ele segue executando o código de quando subiu,
com run verde e log limpo. O deploy documentado hoje diz, textualmente, "nos
hosts de workers faça apenas `git pull --ff-only`" — ou seja, **o caminho feliz
do runbook produz a falha**.

O que isso já custou, medido:

| data | efeito | número |
|---|---|---|
| 21/08 | `ressuscitar_dias_de_recuperacao` (deploy 19/08) nunca executou | **3.007** dias-tribunal parados |
| 21/08 | `import datajud.jobs` quebrando no work-horse (símbolo novo só no disco) | **4.247** jobs mortos, refill parado 3 dias |
| 24/08 | frota inteira em código de 6 dias antes | **215 de 250** workers, atraso de **150,8 h** |

E os dois remendos que existem hoje são **detectores**, não curas:
`_alerta_codigo_velho` (o próprio processo) e `_alerta_workers_velhos` (a
frota). Ambos dependem de alguém ler o log e agir — em 24/08 o alerta estava
aceso e correto havia dias.

**Proposta — o worker se aposenta sozinho.**

Subclasse de `rq.Worker` que, entre jobs (no ponto onde o RQ já roda
`run_maintenance_tasks`), compara o `mtime` de todo `.py` do projeto em
`sys.modules` com o `starttime` do próprio processo — a mesma conta de
`_alerta_codigo_velho`, que custa ~200 `os.stat` (medido <5 ms). Se achar
módulo mais novo, pede **warm shutdown**: termina o job em voo e sai com 0. O
`restart: unless-stopped` do compose sobe o processo de novo, agora lendo o
disco atual.

Três guardas, todas por causa de números medidos hoje:

1. **Jitter obrigatório.** 240 workers detectando a mesma mudança no mesmo
   segundo é a reciclagem em massa que o incidente OOM de 06/2026 proíbe.
   Espalhar por `random.uniform(0, RECICLA_JANELA_S)` — 900 s dá ~16
   reinícios/min, abaixo da maior onda que medimos hoje (44 containers em 95 s,
   custo de 298 MiB de `MemAvailable`).
2. **Cota global no Redis.** Token bucket compartilhado (o padrão já existe em
   `datajud/ratelimit.py`): no máximo N reciclagens por minuto na frota inteira,
   independente de quantos workers acordem juntos. Jitter sozinho não é teto.
3. **Teto de espera, nunca corte mudo** (regra nº 2 do CLAUDE.md). Worker que
   não consegue sair porque o job não termina registra ERRO com a idade real —
   não fica em silêncio nem mata o job.

**O que a proposta NÃO resolve, e é honesto dizer:**

- **Imagem nova.** `restart` recarrega código, não imagem. Medido em 24/08: os
  ~170 containers de enriquecimento de 18/08 rodavam a imagem `5d536a9d`,
  anterior ao rebuild que trouxe o PyMuPDF — `import fitz` dava
  `ModuleNotFoundError` dentro deles. Mudança de `requirements.txt` continua
  exigindo `up --force-recreate` no runbook. A auto-reciclagem cobre o caso
  comum (código), não o raro (dependência).
- **`.env`.** `restart` não relê o `.env`; só `force-recreate`. Idem.
- **Containers fora do compose.** Em 24/08 a `.103` tinha 6 workers RQ vivos
  (`worker_leads_consumo` ×4, `worker_monitoring`, `worker_pdf_download`) de
  12/08 que **nem apareciam** em `Worker.all()` — um deles ainda tinha
  `search/agg_comercial.py` em `sys.modules`, arquivo apagado do disco em 13/08.
  Auto-reciclagem os pegaria (o processo é o mesmo), mas o censo da frota não —
  isso é problema separado.

**Alternativas consideradas.**

| opção | por que não é a primeira escolha |
|---|---|
| `rqworker --max-jobs N` (worker morre após N jobs) | recicla por VOLUME, não por mudança: fila parada nunca recarrega, e fila cheia recarrega toda hora à toa. Serve como rede de segurança grosseira, não como cura |
| Deploy sempre com `--force-recreate` da frota inteira | é o correto e é o que fizemos hoje à mão — 240 containers em **27 min** na `.102`. Aceitável num incidente; caro demais pra ser o caminho de todo commit, e é justamente o custo que faz alguém pular a etapa |
| Tirar o bind mount e assar o código na imagem | resolve de vez (container novo = código novo, por construção) e é o padrão certo pra prod. Mas é mudança de pipeline: exige build+push por deploy e um registry, hoje inexistente. **É a proposta de fundo**; a auto-reciclagem é o paliativo que cabe sem mexer no pipeline |

**Critério pra aceitar.** Só vale se, num deploy de teste, a frota inteira
(256 workers) chegar a `frota.velhos == 0` sem intervenção manual, com o pico
de reinícios por minuto abaixo de 20 e `MemAvailable` na `.102` nunca abaixo
dos 6.000 MiB que usamos como piso hoje.

---

## ADR-035: o `)` do assunto é OPCIONAL, o nome é a FOLHA, e o Datajud passa a dar `grau` (2026-08-25)

**Contexto.** A auditoria de completude do DADO (24-25/08/2026) ranqueou dois
buracos que não custam requisição nenhuma:

- **#2 — assunto.** O detalhe do PJe entrega o assunto hierárquico com o código
  da folha **sem o parêntese de fecho** (`… Acidente de Trânsito (10441`), e
  empilha múltiplos assuntos separados por `\n`. `CLASSE_COM_CODIGO_RE`
  (`^(.+?)\s*\((\d{2,5})\)\s*$`) exigia o fecho ⇒ `assunto_codigo=''` e
  `assunto_nome` = a hierarquia inteira truncada em 255. A classe passava
  porque o PJe fecha o parêntese dela — daí a assimetria da matriz (TRF3:
  classe cód 70,5% × assunto cód 3,6%).
- **#4 — `grau`.** `grau` vem em todo `_source` do Datajud e não havia coluna.
  `JE` = Juizado Especial = **RPV, não precatório**.

**Decisão.**

1. **`)` opcional, e só ele.** `^(.+?)\s*\((\d{2,5})\)?\s*$` — o fecho vira
   opcional, mas continua obrigatório o `(` + 2..5 dígitos **no fim da
   string**. **Não voltamos** ao regex permissivo
   `(.*?)(?:\s*\(?\s*(\d{2,5})\s*\)?)?`, que casava dígito do MEIO do texto
   como código (`Tributário 12345 algo`).
2. **Assunto passa por um parser de folha, classe não.** `split_assunto_folha`
   quebra por `\n`, fica com a **primeira** hierarquia, corta por `  -  `
   (DOIS espaços — ` - ` de um espaço existe DENTRO de nome de folha legítimo)
   e aplica o regex no último nível. `assunto_nome` passa a ser a **folha**.
   A classe não tem hierarquia (0 de 658 linhas do catálogo têm `(código)` no
   nome, contra 1.690 de 2.681 do assunto) e fica no caminho antigo.
3. **Abstenção explícita e contada.** Texto de exatos 255 sem `\n` pode ter
   sido cortado no meio da primeira hierarquia — o `(1234` do fim pode ser um
   **ancestral**. Nesses casos o parser devolve o texto como está e código
   vazio, e o backfill CONTA quantos ficaram. Idem quando o código guardado
   diverge do código no texto (dois writers discordando): abstém.
4. **`grau` gravado com domínio fechado.** `G1`/`G2`/`JE`/`SUP`/`TR`/`TRU`,
   medidos no `voyager-acervo` inteiro (342.046.902 docs, `_count` por termo).
   Qualquer outro valor: `logger.warning` e coluna vazia.
5. **`nivelSigilo` NÃO é lido.** Vale 0 em 342.046.902 de 342.046.902
   documentos — informação zero em escala nacional (a API pública do CNJ só
   expõe o que é público). Mapeá-lo para `segredo_justica` reescreveria em
   102 M de processos a afirmação que a migration 0052 acabou de desfazer.
6. **Backfill local, sem rede**, com checkpoint, teto (atingir = ERROR com o
   número real), freio por latência de bloco e kill switch em cache.

**Por que não o contrário.**

| alternativa | por que não |
|---|---|
| Regex permissivo (`)` e `(` opcionais em qualquer posição) | casa dígito do meio do texto. Foi o bug que o regex estrito consertou; reabri-lo troca 8,2 M de acertos por um número desconhecido de códigos inventados |
| Cortar a hierarquia por ` - ` (um espaço) | mutila nome de folha legítimo: `Crédito Direto ao Consumidor - CDC`, `Agente Agressivo - Eletricidade`, `Reajustes de Remuneração / Plano Verão (1989)`. Medido: `  -  ` em 100% dos casos; ` - ` só dentro do nome |
| Usar a ÚLTIMA hierarquia do texto | quando o campo bateu no teto de 255 a última está cortada, e o `(1234` final é o código de um **ancestral**. Era a origem dos "1.515 com código de ancestral" da auditoria |
| Chutar a folha no texto truncado | é o oposto da regra nº 6. 15.065 de 120.755 (12,5%) ficam vazios de propósito — e contados |
| Fazer merge das 21 linhas de catálogo em conflito | `Assunto.codigo` é PK e `Process.assunto` é FK `PROTECT`. Merge muda a identidade de 163 processos: é decisão de arquitetura, não de backfill |
| Mapear `nivelSigilo` → `segredo_justica` | escreveria `False` sem ter perguntado, em 102 M de linhas. "Perguntamos e a fonte disse que não" ≠ "a fonte não diz" |

**Prova.** Fonte independente: `voyager-acervo`, o esqueleto que o **CNJ**
declara. Dos pares (código → nome da folha) recuperados, 150 sorteados
(semente 20260825): **150/150 com o código certo**, 141 com nome idêntico, 9
com variante de nomenclatura da própria TPU. **Controle negativo: o nome
hierárquico bate 0 de 150.** No catálogo, 150/150 batem depois da correção e
0/150 antes. Amostra qualitativa de 10 processos reais (TJRJ): 10/10 com a
folha certa, incluindo 3 truncados em 255 onde só a primeira hierarquia
sobreviveu. 99,9% dos códigos recuperados já existiam no catálogo.

**Limitação registrada.** O `_source` bruto do Datajud não é guardado em lugar
nenhum — não há model nem coluna JSONB. `grau` só chega a processo já
existente por um backfill que leia o `voyager-acervo` por `proc`; isso é
possível **sem tocar no Datajud**, mas é job novo e não entra aqui. E nem o
backfill de assunto nem o `grau` reindexam no Elasticsearch: `bulk_update` e
`.update()` não disparam `post_save`.

---

## ADR-036: o TJPR entra na recuperação, e a "decisão comercial" não existia (2026-09-02)

**Contexto.** A Fase 3 da recuperação nacional deixava o TJPR de fora — 1.152
dias-alvo e ~38,8 M de publicações estimadas, **43% de todo o volume que resta**.
A justificativa, repetida em quatro lugares, era "está fora por decisão
comercial do dono do produto".

Ao ser perguntado *qual* foi a decisão, o rastreamento (02/09/2026) devolveu:

- a frase nasceu num **comentário** de `dashboard/completude_medicoes.py`,
  commit `34b1e3d` (20/08/2026). A mensagem daquele commit não a justifica;
- de lá foi copiada para `.ia/ACERVO_CNJ.md`, para `djen/recuperacao.py` e para
  a flag `djen_recup_f3 --tjpr-on`, ganhando aparência de premissa a cada cópia;
- **não havia ADR.** Um comentário sem fonte governou 43% do trabalho pendente
  por quinze dias.

**A régua que EXISTE.** `TRIBUNAIS_JURISCOPE` (`djen/ingestion.py:39`) — os 10
tribunais em que o Falcon de fato lê precatório e ele vira lead, medido em
06/08/2026 sobre 2,43 M precatórios: TJSP, TRF1, TRF3, TRF4, TJMG, TRF6, TRF5,
TJAL, TRF2, TJMA. **O TJPR não está nela.** Isso é real, medido e datado — mas
justifica *despriorizar*, não *excluir*.

E ela expõe outra coisa: a `FASE_2` **nunca foi** a lista comercial. Ela inclui
TJRS/TJGO/TJRJ, que o Juriscope não lê, e excluía o **TJMA**, que ele lê e que
tinha 272 dias por refazer — tratado como resto por quinze dias.

**Decisão.** Em 02/09/2026 o dono do produto, apresentado a esses fatos:

1. **inclui o TJPR** na Fase 3 (`--tjpr-on`, ligado em 02/09);
2. **promove o TJMA** ao primeiro lugar da `ORDEM_FASE_3`;
3. manda registrar este ADR.

**Por quê.** Acervo e lead são coisas diferentes, e o produto desta casa é o
acervo (princípio nº 1 do `CLAUDE.md`: *o produto é o acervo*). Um tribunal de
onde não sai lead hoje continua sendo 38,8 milhões de publicações do país que
temos pela metade — e "de onde não sai lead hoje" é uma medição de agosto de
2026, não uma lei.

**Consequências.**

- `FORA_DO_ALVO` fica **vazio** em `completude_medicoes.py`. A Fase 3 passa a
  publicar o número maior (8.419 dias-alvo em vez de 7.188): a tela mostra o
  problema inteiro, não a versão confortável.
- ETA da Fase 3 sobe de ~49 h para ~58 h no ritmo medido (~145 dias/h).
- A escotilha `--tjpr-off` **continua existindo**, mas com outro propósito:
  desligar em segundos se o TJPR pressionar a API do CNJ (o teto é
  réplicas × páginas, e o circuito aberto adia os dias dos outros tribunais).
  Não é mais o estado padrão.
- **Regra que fica:** quem puser um tribunal em `FORA_DO_ALVO` escreve o ADR
  junto. Foi a ausência dele que deixou isto acontecer.
