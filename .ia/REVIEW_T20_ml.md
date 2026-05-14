# Code Review T20 — ML (T17+T18+T19)

**Veredito:** APPROVE WITH NITS

**Resumo executivo:**

- Arquitetura sólida: T17 (hot reload), T18 (treino v7) e T19 (shadow mode) compõem
  ciclo coerente — pesos carregados do DB, candidato treinado offline com gates
  formais, validação A/B em produção via shadow. Todas as decisões pesadas
  estão alinhadas com `REGRAS_NEGOCIO_VALIDACAO.md`.
- Backward compat preservado: `VERSAO`/`WEIGHTS`/`HARDCODED_WEIGHTS` continuam
  exportados; `_validate_pesos` aceita superset (v7 com F24-F28) sem quebrar
  predict v6; fallback hardcoded em qualquer falha de DB.
- Idempotência muito boa: migration 0026 idempotente; treino com mesma seed
  reproduzível; `update_or_create` em deploy; constraint partial garante 1
  ativa. Re-rodar `comparar_shadow` no mesmo dia **sobrescreve** o `.md` (pode
  ser desejado, vale documentar).
- Issues médias concentradas em (a) drift de lógica entre `classificar()`
  e `_categorizar()` — só shadow lê `ThresholdTribunal`, ativo usa
  hardcoded — e (b) `_categorizar` não filtra por `versao_modelo` ao
  consultar `ThresholdTribunal`, podendo pegar threshold de outra versão.
- Cobertura de testes ampla (44 testes citados — reload/treino/shadow). Falta
  edge case: `--deploy --force` com WARN puro, idempotência de
  `comparar_shadow` re-executado no mesmo dia.

---

## Por categoria (A-L)

### A. Backward compat

- PASS — Fallback hardcoded em DB down — `tribunals/classificador.py:247-254`.
  `_maybe_reload_weights` envolve toda a leitura num try/except amplo, com 2
  branches: `_WEIGHTS_CACHE['pesos'] is None` (boot) cai pra hardcoded;
  caso contrário preserva o cache anterior e só atualiza `loaded_at` pra
  evitar retry storm. Excelente.
- PASS — Features extras (v7 F24-F28) não quebram `compute_features` v6 —
  `tribunals/classificador.py:226-233` apenas loga warning e deixa o
  predict ignorar silenciosamente (pesos × 0). `predict_score` em
  `:388-401` usa `pesos.get(fname, 0.0)` — robusto.
- PASS — `predict_score` com pesos extras é silencioso — `:398-400`, loop
  itera sobre `features.items()` e ignora pesos sem feature correspondente.
- PASS — Constante `VERSAO = 'v6'` mantida em `:51` (compat de import).
  `tribunals/jobs.py:12` importa diretamente.
- PASS — `WEIGHTS = HARDCODED_WEIGHTS` em `:79` mantém o alias legado.

### B. Determinism (treino v7)

- PASS — `seed` propagado em split (`treinar_classificador_v7.py:643`) e
  conformal (`:1010`). Numpy `default_rng(seed)` é reprodutível.
- PASS — Split estratificado consistente — agrupa por `(tribunal, label)`
  e usa `rng.shuffle` único — `:649-656`.
- WARN — Grid threshold determinístico — não usa RNG (`:976-994`), só ordena
  por score; mas precisa-se notar que **empate** entre thresholds usa
  primeiro encontrado em ordem crescente. Ok.
- PASS — Conformal com mesma seed = mesmo delta — `:1010-1022` usa o
  mesmo `default_rng(seed)`.
- PASS — Teste `test_idempotencia_mesma_seed_mesmas_metricas` em
  `tests/test_treinar_v7.py:335-362` valida AUC/precision/ECE iguais.

### C. Sample weight (T18)

- PASS — Gradient com sample_weight correto matematicamente —
  `treinar_classificador_v7.py:692-694`. `err = (p - ytr) * w_norm` aplica
  o peso por exemplo; `grad_w = Xtr.T @ err / n + l2 * W`. Padrão clássico
  de "weighted logistic regression".
- WARN — Normalização `w_norm = wtr * (n / w_sum)` — `:685-686`. Quando
  todos pesos=1, recupera unweighted exato (w_sum=n → w_norm=wtr=1).
  Quando pesos variam, **preserva a escala média do gradiente** —
  decisão correta documentada nos comentários. Mas: efetivamente
  re-escala o gradiente proporcional à fração de pesos altos, mudando
  loss surface vs LR não-ponderado. Vale documentar em
  `.ia/CLASSIFICACAO.md`.
- NIT — L2 não considera sample_weight no termo regularizador
  (`l2 * W` direto em `:693`). Padrão clássico — L2 controla complexidade
  do modelo, não a importância de cada exemplo. Ok.
- NIT — L2 regulariza `W` mas não `b` — `:693-696`, `grad_b = err.mean()`
  sem L2. Padrão clássico (bias não é regularizado).
- NIT — `grad_b = err.mean()` usa média não-ponderada de `err`, que já
  contém o peso. Isso significa que o gradiente do bias acaba sendo
  `(p - y) * w_norm` médio, e não `(p - y)` médio ponderado por `w_norm`.
  Algebricamente equivalente quando o batch é uniforme; em batch GD não
  faz diferença prática mas vale documentar.

### D. Conformal prediction (T18)

- PASS — Split conformal canônico — `:1007-1031`. 20% calibration
  (`n // 5`), residuals `|y - score|`, quantil 0.9. Correto.
- WARN — Delta **global** (não por nível). T18 escolheu global —
  aceitável pra v7 (simplificação), mas calibração por nível
  (PRECATORIO/PRE/DC) seria mais útil pra reportar incerteza no
  pipeline Juriscope. Documentar como TODO em
  `.ia/CLASSIFICACAO.md`.
- PASS — `interpretacao` textual clara em `:1027-1030`.
- NIT — Não é "verdadeiro" split conformal cobertura-garantida (não há
  prova de cobertura assintótica do tipo 90% nas predições futuras —
  só estimativa empírica do residual em calibration). Aceitável como
  proxy mas não vender como "conformal coverage guarantee".

### E. Threshold grid (T18)

- WARN — Otimiza **precision@500 no holdout** —
  `treinar_classificador_v7.py:973-993`. Risco de **overfit no holdout**
  (sem CV interno). Dataset real do v7 deve ser grande o bastante
  (centenas de milhares) pra mitigar, mas pra dataset pequeno
  (validação humana ainda nascente) o overfit é real.
- PASS — Coerente com REGRAS_NEGOCIO_VALIDACAO §4 — privilegia
  precision (Juriscope tem capacidade fixa).
- PASS — Fallback default em `:961` (`THRESHOLDS_DEFAULT[trib]`)
  quando amostra < 4. Mas: **tribunal não em `TRIBUNAIS_THRESHOLDS`**
  (ex: TJRJ) não cai aqui — o loop só itera nos 4 conhecidos. Em
  prod, `_categorizar` cai nos defaults globais (`THRESHOLD_PRECATORIO`
  etc) — `classificador.py:488-490`. Funciona, mas a tabela em
  `_persistir_thresholds` (`:1110-1128`) só cria 4 rows. **Documentar**.

### F. Shadow mode (T19)

- PASS — Sample rate não bloqueia worker —
  `classificador.py:587-604`. Random + `.delay()` async; falha de
  enqueue logada em debug. Hot path intocado.
- PASS — `classificar_shadow` não toca em `Process.classificacao` —
  `classificador.py:566-571` cria apenas `ClassificacaoShadowLog`.
- PASS — Hook em `classificar_e_persistir` não crasha em falha de
  enqueue — `:600-604`, try/except amplo, debug log. Testado em
  `tests/test_shadow_mode.py:238-248`.
- WARN — `bulk_create` não atômico explicitamente —
  `classificador.py:578-583`. Se 1 row falhar, comportamento depende
  do Postgres (default = abortar transação inteira; com
  `ignore_conflicts` parcialmente). Sem `ignore_conflicts` aqui —
  ok porque não há unique constraint, então duplicidade é aceita
  (`classificar_shadow` rodar 2x cria 2 conjuntos — documentado em
  `:529-530`). Acceptable.
- BLOCK-soft — **`_categorizar` replica lógica de `classificar` MAS
  com diferença sutil**: `_categorizar` lê `ThresholdTribunal` do DB
  (`classificador.py:492-506`); `classificar()` usa
  `THRESHOLD_PRECATORIO=0.70` hardcoded (`:426-430`). Isso significa
  que **a comparação shadow x ativa pode ter agreement_rate menor
  do que real porque shadow usa thresholds do DB e ativa usa
  defaults**. Pra ser legítimo A/B, ambos precisam usar a mesma
  política de threshold. Recomendação: ou `classificar()` também
  lê `ThresholdTribunal`, ou `_categorizar` ignora DB. Vide
  REGRAS §4 que exige DB-driven — então deveria ser **`classificar()`
  que precisa migrar**.
- NIT — `_categorizar` filtra `ThresholdTribunal` apenas por
  `tribunal_id, ativo=True` (`:494-499`) — **não filtra por
  `versao_modelo`**. Se há row pra v6 e v7 (mesmo tribunal, ambos
  ativos), retorna o primeiro `.first()` sem ordenação determinística.
  Risco real quando v7 entrar em prod com seus thresholds próprios.
  Fix: aceitar `versao_modelo` como kwarg ou ordenar por `-criada_em`.
- PASS — `comparar_shadow` lida com 0 logs sem crash —
  `tribunals/jobs.py:296-414`. Test cobrindo em
  `tests/test_shadow_mode.py:311-321`.
- PASS — KS test manual `_ks_2samp` em `jobs.py:167-188` correto
  (max |CDF_a - CDF_b|). Tests cobrem distribuições iguais (0.0),
  distintas (1.0) e vazias (0.0).
- NIT — `comparar_shadow` busca `criada_em__gte=since` com
  `select_related('processo')` — mas o `.values()` na linha seguinte
  invalida o `select_related` (Django ignora). Não causa bug, só
  ruído.

### G. Idempotência

- PASS — Treinar 2x com mesma seed reproduz métricas — testado em
  `tests/test_treinar_v7.py:335-362`.
- PASS — `update_or_create` em `_criar_versao`
  (`treinar_classificador_v7.py:1102-1104`) — idempotente.
- PASS — Constraint partial `ativa=True` garante 1 ativa
  (`models.py:415-416`); shadow=True pode ter N (correto pra
  comparação multi-candidato).
- WARN — Re-rodar `comparar_shadow` mesmo dia **sobrescreve** o
  `.md` — `jobs.py:396` usa `SHADOW_COMPARISON_{date.today():%Y%m%d}.md`.
  Comportamento provavelmente desejado (1 relatório/dia) mas
  documentar e/ou adicionar timestamp HHMM pra runs ad-hoc.
- PASS — Migration 0026 idempotente —
  `migrations/0026_seed_classificador_versao_v6.py:53-69`. Se já
  existe ativa, retorna; se existe v6 inativa, ativa; ela só
  cria quando nada existe. Testado em
  `tests/test_classificador_reload.py:451-470`.

### H. Métricas (T18)

- PASS — AUC manual (Mann-Whitney) em `:134-145` — implementação
  trapezoidal manual após NumPy 2.0 remover `np.trapz`. Consistente
  com decisão do commit 70da950.
- PASS — `precision@K` clássico em `:148-153`.
- WARN — ECE 10-bin em `:156-176` — implementação `accuracy = y[mask].mean()`
  para mask boolean (true positives apenas). Convenção mais comum
  é usar `mean(y)` dentro do bin como acurácia. Está alinhado com
  o que faz scikit/literatura. Vale comparar saída com `decis` que
  T18 também emite (`:734-753`) — ambos devem ser consistentes.
- PASS — `recall@FN_candidatos` com cutoff `>=0.20` em `:811` —
  alinhado com `THRESHOLD_DIREITO_CREDITORIO=0.20` (mas hardcoded;
  poderia usar a constante importada).
- PASS — Regressão falsos_consumidos — `:821-836`, cutoff 0.3.
  Gates BLOCK em >10%.

### I. Logging / observabilidade

- PASS — Hot reload loga troca — `classificador.py:220-224` INFO
  `"classifier reloaded: %s -> %s"`. Testado em
  `tests/test_classificador_reload.py:318-331`.
- PASS — Shadow falha loga warning — `classificador.py:541, 550, 562,
  582` — sem crash. Testado em
  `tests/test_shadow_mode.py:164-185`.
- PASS — `comparar_shadow` loga métricas finais — `jobs.py:404-407`
  com agreement_rate, ks, report path.
- PASS — Treino v7 loga progresso por época — `treinar_classificador_v7.py:697-704`
  a cada 100 épocas.
- NIT — Sentry: o code path em `classificador.py:247` usa `logger.exception`
  que vai pro Sentry se configurado (via integration Django logging).
  Ok, mas vale verificar se `voyager.tribunals.classificador` está
  whitelisted em `core/settings.py` SENTRY config (não verificado neste
  review).

### J. Edge cases

- PASS — `ano_std == 0` (dataset pequeno) — guarda em
  `treinar_classificador_v7.py:560-561` com `max(..., 1e-6)`; e em
  `:883-884` (`_score_cnjs`) com `if std > 0 else 1.0`.
- PASS — DB sem `ClassificadorVersao` ativa — fallback hardcoded em
  `classificador.py:211-216`, com warning na primeira carga. Testado.
- PASS — CSV vazio — `_carregar_dataset` em `:425-451` ignora rows
  sem `processo_id` válido. Se dataset final vazio, `CommandError`
  em `:300`.
- PASS — 0 candidatos FN — `_calcular_recall_fn`
  (`:777-819`) retorna `recall=None` que vira NO_DATA no gate
  (`:1048-1052`). Boa.
- WARN — Tribunal novo (não em `TRIBUNAIS_THRESHOLDS`) — não está
  no grid, threshold default global é usado em prod. Funciona, mas
  silencioso. Documentar.
- PASS — `shadow_status` sem versão shadow → None
  (`dashboard/queries.py:1100-1104`). Testado em
  `test_shadow_mode.py:389-391`.

### K. Cross-cutting

- PASS — `tribunals/` mantém monopólio de models — todos
  `ClassificadorVersao`, `ClassificacaoShadowLog`, `ThresholdTribunal`
  vivem lá.
- PASS — `djen/scheduler.py:200` importa de `tribunals.jobs`
  localmente — evita ciclo no boot.
- WARN — Settings novos (`CLASSIFICADOR_RELOAD_TTL`,
  `SHADOW_SAMPLE_RATE`, `VALIDACAO_LOTES_SEMANAIS_ENABLED`) estão
  em `core/settings.py:215-236` mas **não documentados** em
  `.ia/OPS.md` ou `.ia/CLASSIFICACAO.md`. Pra T22, documentar
  estes 3 (e `SLACK_WEBHOOK_URL` que ganhou função nova).
- NIT — Numpy em runtime — T18 confirma numpy hard requirement
  (`treinar_classificador_v7.py:279-285`). Em prod do container
  web, se numpy não está em `requirements.txt`, T22 vai falhar.
  **Verificar `requirements.txt`** antes do gate real.

### L. Testes

- PASS — Cobertura ampla: 14 (reload) + 8 (treino) + 22 (shadow) = 44
  testes citados. Casos críticos cobertos: fallback DB down,
  concorrência, pesos corrompidos, idempotência, gates PASS/WARN/BLOCK,
  shadow 0/1/N versões.
- PASS — Mocks apropriados — usa `patch` em
  `tribunals.models.ClassificadorVersao.objects` pra simular falhas
  sem rodar treino real.
- PASS — Concorrência testada — `test_concorrencia_multiplas_threads_sem_crash`
  com 10 threads predizendo + 1 thread trocando pesos.
- PASS — Idempotência testada — `test_idempotencia_mesma_seed_mesmas_metricas`
  + `test_migration_seed_v6_e_idempotente`.
- WARN — **Faltam casos**:
  - `--deploy --force` com apenas WARN (não BLOCK) — confirma que
    versão é criada. Existe `test_force_block_nao_libera` mas não o
    contraponto.
  - `_categorizar` com `ThresholdTribunal` de **versão diferente** (v6+v7
    ambos ativos pra mesmo tribunal). Cobriria o NIT identificado em F.
  - `comparar_shadow` idempotência: rodar 2x no mesmo dia, segunda
    sobrescreve.

---

## Blockers

(Nenhum.)

---

## Issues médias

1. **Drift de threshold entre `classificar()` e `_categorizar()`**
   (`tribunals/classificador.py:426-430` vs `:488-506`). `classificar`
   (caminho ativo prod) usa thresholds hardcoded globais;
   `_categorizar` (caminho shadow) lê `ThresholdTribunal` do DB.
   Consequência: `comparar_shadow` pode reportar disagreement causado
   pela política de threshold, não pelo modelo. Para A/B legítimo:
   migrar `classificar()` pra também ler `ThresholdTribunal` (REGRAS §4
   exige DB-driven mesmo).

2. **`_categorizar` não filtra `ThresholdTribunal` por `versao_modelo`**
   (`tribunals/classificador.py:494-499`). Quando v7 entrar em prod
   junto com v6 (cenário transitório), `.filter(tribunal_id=..., ativo=True)
   .first()` retorna qualquer um sem ordem determinística. Risco
   prático no momento exato do switch.

3. **Grid threshold sem CV interno**
   (`treinar_classificador_v7.py:973-993`). Otimiza precision@500 no
   próprio holdout — risco de overfit. Em prod com dataset grande
   (centenas de milhares) o risco é baixo; com validação humana
   ainda pequena, pode mascarar regressão. Sugestão pra v8 ou
   pós-T22: CV interno 5-fold dentro do train.

---

## Nice-to-have

1. Documentar `CLASSIFICADOR_RELOAD_TTL`, `SHADOW_SAMPLE_RATE`,
   `VALIDACAO_LOTES_SEMANAIS_ENABLED` em `.ia/OPS.md`.
2. `comparar_shadow` deveria suportar override de filename
   (timestamp HHMM) pra runs ad-hoc no mesmo dia (hoje sobrescreve).
3. Conformal delta por nível (não só global) pra reportar
   incerteza diferenciada N1/N2/N3.
4. Threshold para tribunais não em `TRIBUNAIS_THRESHOLDS`: explicitar
   fallback em log/relatório (hoje silencioso).
5. Test adicional: `--deploy --force` com WARN puro (sem BLOCK)
   confirma criação de versão; teste de drift `_categorizar` quando
   v6 e v7 têm thresholds distintos no DB.
6. `comparar_shadow` em `jobs.py:298-308`: `.select_related('processo')`
   é ignorado pelo `.values(...)` subsequente — remover pra reduzir
   ruído.
7. `recall@FN_candidatos` usa cutoff hardcoded `0.20` em
   `:811` — importar `THRESHOLD_DIREITO_CREDITORIO` do classificador
   pra single source of truth.

---

## Recomendações pra T22 (validação real do gate)

### Como rodar v7 em prod-like

1. **Pré-flight check no container web**:
   ```bash
   docker compose exec web python -c "import numpy; print(numpy.__version__)"
   ```
   Confirmar numpy ≥ 1.26 (idealmente 2.x, dado fix np.trapz). Se
   ausente, adicionar a `requirements.txt` e rebuild **antes** de
   T22.

2. **Gerar export de labels e candidatos FN atualizados**:
   ```bash
   docker compose exec web python manage.py exportar_labels_retreino
   docker compose exec web python manage.py minerar_fn --tribunal TRF1 --limit 15000
   ```
   Confirmar que `data/labels_retreino_*.csv` e
   `data/fn_candidatos_*.csv` existem.

3. **Treino shadow primeiro (sem deploy)**:
   ```bash
   docker compose exec web python manage.py treinar_classificador_v7 --shadow
   ```
   Inspecionar `data/V7_TRAINING_REPORT_*.md` e
   `data/v7_metrics_*.json`. Gates devem estar verdes ou WARN.

4. **Configurar `SHADOW_SAMPLE_RATE` em prod**:
   ```bash
   # já default=0.1 em settings — pode subir pra 0.3 nos primeiros 7 dias
   ```

5. **Aguardar 7 dias de shadow logs** (`ClassificacaoShadowLog`
   cresce em segundo plano; `comparar_shadow_daily` roda 04:00 UTC
   e produz `.ia/SHADOW_COMPARISON_*.md`).

### Como interpretar shadow comparison

- **agreement_rate ≥ 0.95** + **|delta_med| ≤ 0.02** = v7 é
  refinamento incremental — go-no-go fácil.
- **0.85 ≤ agreement_rate < 0.95** + **ks_statistic ≤ 0.10** = mudança
  significativa em fronteira, esperado se v7 introduz F24-F28 fortes.
  Inspecionar `top_disagreements` no relatório — verificar se as
  novas categorias (N1↔N2) são consistentes com regras de negócio
  (REGRAS §3 C1+C2).
- **agreement_rate < 0.85** ou **ks > 0.20** = mudança grande,
  exigir validação humana adicional via lote `shadow_disagree`
  antes do flip.

### Critérios go/no-go pré-deploy

| Critério | Go | Discutir | No-go |
|---|---|---|---|
| 6 gates v7 | 6 PASS | 5 PASS + 1 WARN | qualquer BLOCK |
| Agreement v6↔v7 (7d) | ≥ 0.92 | 0.85–0.92 | < 0.85 |
| Disagreements N1→NAO_LEAD | < 1% | 1–5% | > 5% (regressão grave) |
| `regressao_falsos_consumidos_pct` | 0% | ≤ 10% | > 10% (gate BLOCK já cobre) |
| numpy disponível no container | Sim | — | Não |
| Settings documentados em `.ia/` | Sim | parcial | Não |

Pós-deploy:
- Monitorar `chart_shadow_status` no `/dashboard/leads/validacao/`.
- Plano de rollback: `ClassificadorVersao.objects.filter(versao='v6').update(ativa=True)`
  + `ClassificadorVersao.objects.filter(versao='v7').update(ativa=False)`.
  Hot reload (TTL 60s) propaga sem restart.

---

## Decisão final

**APPROVE WITH NITS — T22 desbloqueada** desde que pelo menos as 2
issues médias #1 e #2 (drift de threshold e filtro por versao_modelo)
sejam endereçadas **antes** de promover v7 a ativa. Os nits e a issue
#3 (CV interno) podem ficar como follow-up técnico em ROADMAP.

Resumo: o trio T17/T18/T19 está arquiteturalmente coerente, bem
testado, e os caminhos críticos (fallback, idempotência, concorrência)
foram pensados. Os pontos abertos são de polimento e consistência
entre o caminho ativo (`classificar`) e o caminho shadow
(`_categorizar`), que vão importar muito no momento exato do switch
v6→v7.
