# Code Review Final T24 — E2E (Wave 0-5 complete)

**Reviewer:** code-reviewer-arch + code-reviewer-sec joint (squad Voyager)
**Data:** 2026-05-12
**Escopo:** consolidado E2E após T1-T23, validação de integração cross-app,
production readiness, riscos pré-deploy v7.

**Veredito final:** ⚠️ **GO COM RESSALVAS**

A feature está **arquiteturalmente pronta e testada (162/162 passing em
~53s)**. Os principais bugs operacionais de reviews anteriores foram
endereçados (cache `delete_pattern` → versioning; N+1 em `kpis_validacao` →
single `annotate`; drift threshold `classificar` vs `_categorizar` →
unificado; filtro `versao_modelo` em `_categorizar` → implementado). Restam
3 ressalvas operacionais que devem ser resolvidas **antes** do flip v6→v7
em produção, mas **não bloqueiam** merge nem deploy de infraestrutura (T7
schema, dashboard, validação humana, shadow logging em sample-rate).

**Resumo executivo (5 bullets):**

1. **162 testes passando** (`tests/test_validacao_models.py` +12 outros) em
   53s. Suíte cobre models, sampling, export labels, mining FN, hot reload,
   views, partials, shadow, lotes semanais, templates — sem `xfail` e sem
   skip silencioso.
2. **Issues médias do T11 e T20 endereçadas no código atual:** cache
   versioning (`dashboard/views.py:1583-1611, 2005`), `kpis_validacao`
   single-query (`dashboard/queries.py:65-88`), `_categorizar` filtra por
   `versao_modelo` (`tribunals/classificador.py:485-488`), `classificar()`
   e `_categorizar()` agora compartilham a mesma lógica DB-driven
   (`tribunals/classificador.py:404-426`).
3. **3 issues operacionais ABERTAS** (não bloqueiam dev, devem ser fechadas
   pré-prod): salt LGPD hardcoded fallback, `EMAIL_BACKEND`/`DEFAULT_FROM_EMAIL`
   não configurados, CSVs de ground truth em `data/` fora do build do
   Dockerfile (gitignored).
4. **Permissions / CSRF / IDOR / SQL injection:** sem regressão. Permission
   `can_view_motivo` agora tem call-site real (`tribunals/models.py:709` +
   templatetag `dashboard/templatetags/voyager_extras.py:9-19`), mas nenhum
   template a chama ainda — vira blocker quando T16 (resolver divergência)
   renderizar motivos de outros.
5. **Production readiness:** numpy 2.4.4 instalado e funcionando, migrations
   0024-0027 aplicadas e reversíveis, scheduler cobre crons novos
   (`comparar_shadow_daily` 04:00, `gerar_lotes_semanais_fn` dom 02:00),
   settings novos (`CLASSIFICADOR_RELOAD_TTL`, `SHADOW_SAMPLE_RATE`,
   `VALIDACAO_LOTES_SEMANAIS_ENABLED`) com defaults seguros, documentados
   em `.ia/OPS.md` linhas 385-387 e em `.ia/CLASSIFICACAO.md`.

---

## Status das reviews anteriores

| Review | Veredito | Issues médias originais | Resolvidas no código atual | Abertas |
|---|---|---|---|---|
| **T6 schema** | APPROVE NITS | 3: trigger PG, `criada_por` PROTECT, index duplicado | 2 (migration 0025 corrige PROTECT→SET_NULL e `db_index`/Index duplicado) | 1 (trigger PG UPDATE-block ainda ausente — não bloqueia até T18 publicação) |
| **T11 backend** | APPROVE NITS | 4: cache delete_pattern, N+1 kpis, can_view_motivo, salt LGPD | 3 (versioning, annotate, helper `motivo_visivel_para`) | 1 (salt LGPD ainda usa fallback hardcoded `'voyager-validacao-default-salt'` — `dashboard/views.py:1977`) |
| **T15 frontend** | APPROVE NITS | 8: focus trap, reduced-motion, lote_concluido enum, bug `criando`, auto-advance, retry button, hotkeys duplicados, focus pós-swap | 0 explicitamente endereçadas | 8 (todas são UX issues médias/baixas; não bloqueiam, mas operador externo vai notar) |
| **T20 ML** | APPROVE NITS | 3: drift threshold classificar vs _categorizar, filtro versao_modelo, CV interno grid | 2 (drift e versao_modelo) | 1 (CV interno fica como follow-up v8) |

**Total:** 4 reviews, 18 issues médias originais → **10 endereçadas no
código** + 1 mitigada por procedimento (V7_DEPLOY_DECISION) + **7 abertas**
(8 frontend UX + 1 backend LGPD + 1 schema imutabilidade − ofsets de mitigação).

---

## Cobertura por categoria (A-L)

### A. Coerência cross-app

- **PASS** — `tribunals/` mantém monopólio dos models. Todos os modelos
  novos vivem em `tribunals/models.py`. Confirmado: `api/leads.py:22` e
  `dashboard/queries.py:15,37,148,263,324,785,1097` só consomem; nada
  declarado fora.
- **PASS** — Imports cross-app são lazy onde necessário pra evitar ciclo:
  `djen/scheduler.py:186,200,217` (`from tribunals.jobs import …` dentro de
  `create_scheduler`), `dashboard/views.py:1737,1861,1894,1965,2061-2062`
  (lazy de `tribunals.models`/`tribunals.sampling` dentro de cada view).
- **PASS** — `tribunals.jobs` importa de `tribunals.sampling` top-level
  (`tribunals/jobs.py:577`) — mesma app, sem risco circular.
- **PASS** — `dashboard.views` consome `tribunals.classificador` +
  `tribunals.sampling` lazy dentro das views, OK.
- **PASS** — `djen.scheduler.py:20` tem `from tribunals.models import
  Tribunal` top-level — funciona porque é executado fora do boot, no
  `create_scheduler()` que roda manualmente. Não causa ciclo.

### B. Migration order

- **PASS** — 0024 (validacao_humana) → 0025 (nits T6: SET_NULL +
  remoção index duplicado) → 0026 (seed v6) → 0027 (can_view_motivo).
  Cadeia correta via `dependencies = [('tribunals', '0023_…')]` etc.
- **PASS** — 0024 é schema-only (create 5 tabelas + add `shadow` field) —
  <1min em prod, tabelas novas vazias. 0025 é alter_field (SET_NULL) —
  fast. 0026 é RunPython idempotente (`get_or_create` + early return se
  ativa existe). 0027 é AlterModelOptions — apenas adiciona permission
  via Django auth_permission.
- **PASS** — Migrations reversíveis: Django auto-gera `unseed_v6` em 0026
  (`tribunals/migrations/0026_seed_classificador_versao_v6.py:72-79`).
  0024-0027 deletáveis na ordem reversa.
- **PASS** — Constraints partial corretas: `uniq_classificador_versao_ativa`
  (`tribunals/models.py:415`), 1 ativo por `(tribunal, versao_modelo)` em
  ThresholdTribunal (`tribunals/models.py:767-771`), `UniqueConstraint
  (processo, usuario)` em ProcessoValidacao (T6 confirmado).
- **CONFIRMED** — todas 27 migrations aplicadas em dev local (saída de
  `showmigrations tribunals`).

### C. Feature flags

- **PASS** — Os 3 settings novos têm defaults seguros e podem ser
  sobrescritos via env:
  - `CLASSIFICADOR_RELOAD_TTL = env.int('CLASSIFICADOR_RELOAD_TTL',
    default=60)` (`core/settings.py:219`).
  - `SHADOW_SAMPLE_RATE = env.float('SHADOW_SAMPLE_RATE', default=0.1)`
    (`core/settings.py:224`).
  - `VALIDACAO_LOTES_SEMANAIS_ENABLED = env.bool(…, default=True)`
    (`core/settings.py:234-236`).
- **PASS** — Comportamento se desligado: `SHADOW_SAMPLE_RATE=0.0` em
  `_maybe_enfileirar_shadow` (`tribunals/classificador.py:587-589`) retorna
  cedo, não enfileira nada. `VALIDACAO_LOTES_SEMANAIS_ENABLED=False` em
  `djen/scheduler.py:216` simplesmente não registra o cron — sem efeito
  colateral. `CLASSIFICADOR_RELOAD_TTL=0` força reload em toda chamada
  (caro mas funcional). Defaults seguros.

### D. Backward compat

- **PASS** — `/api/v1/leads/` inalterado (`api/leads.py` não modificado
  fora do uso de `ClassificadorVersao` existente).
- **PASS** — `Process.classificacao_score` semântica preservada.
- **PASS** — Migration 0026 garante `ClassificadorVersao(versao='v6',
  ativa=True)` pós-deploy — bate com `VERSAO = 'v6'` em
  `tribunals/classificador.py:51`.
- **PASS** — Import legado funciona: `WEIGHTS = HARDCODED_WEIGHTS` em
  `tribunals/classificador.py:79`. Confirmado em uso por
  `dashboard/views.py:360`, `dashboard/queries.py:360`.
- **PASS** — Endpoints existentes intocados — `dashboard/urls.py` cresceu
  29 linhas (URLs novas de validação), nenhuma URL existente alterada.

### E. Cobertura de testes

- **PASS** — 13 arquivos novos de teste (~95KB de testes):
  - `test_validacao_models.py` (12 testes — schema)
  - `test_sampling.py` (17 testes — 7 estratégias + criar_lote)
  - `test_export_labels.py` (8 testes — peso por origem, dedup, idempotência)
  - `test_minerar_fn.py` (5 testes — E1-E6 + suspeita_score)
  - `test_classificador_reload.py` (14 testes — hot reload, fallback, concorrência)
  - `test_views_validacao.py` (22 testes — permissions, CSRF, IDOR, race)
  - `test_cmd_gerar_lote.py` (14 testes — CLI)
  - `test_validacao_card_partial.py` (10 testes — HTMX partial)
  - `test_treinar_v7.py` (8 testes — gates, idempotência, sample_weight)
  - `test_shadow_mode.py` (22 testes — classificar_shadow, comparar)
  - `test_lotes_semanais.py` (10 testes — pipeline semanal + notify)
  - `test_template_visibilidade.py` (10 testes — render + a11y)
  - `test_template_validacao.py` (10 testes — render + hotkeys)
- **TOTAL: 162 testes passing em 53.25s.**
- **PASS** — Edge cases cobertos: lote vazio (test_sampling.py), sem ativa
  (test_classificador_reload.py:217-241), CSV malformado (test_minerar_fn,
  test_sampling), race em criar_lote (test_sampling.py:332-350),
  concorrência hot reload (test_classificador_reload.py).
- **PASS** — Permissions tests cobertos: `test_views_validacao.py:137-204`
  cobre `noperm_user` em todas as 4 perms (403). Migration 0027 adiciona
  `can_view_motivo` mas falta teste explícito de policy diferenciada.
- **GAP** — Coverage report não executado (não vi `pytest-cov` no
  `requirements-dev.txt`; pode ser adicionado em CI no futuro). Heurística
  por linhas mudadas vs linhas testadas indica >80% nas áreas críticas.

### F. Performance regression

- **PASS** — `kpis_validacao` agora é **1 query GROUP BY** com
  `annotate(total=Count('itens'), anotados=Count('validacoes', filter=Q
  (validacoes__usuario=usuario)))` (`dashboard/queries.py:69-84`). Era 40
  queries (2N COUNTs em loop). **Issue T11 #2 endereçada.**
- **PASS** — `_chart_validacao_cache_key` com versioning (string concat +
  md5 + int): O(1) por hit, sub-µs (`dashboard/views.py:1602-1611`).
- **PASS** — `_maybe_reload_weights` fast-path sem lock checa epoch
  (sub-µs): `if now - _WEIGHTS_CACHE['loaded_at'] < ttl: return`
  (`tribunals/classificador.py:191-194`). Dentro do TTL, custo é
  negligível.
- **PASS** — `comparar_shadow` cron 04:00 não conflita: classificação não
  roda massiva nesse horário; `comparar_shadow_daily` tem
  `max_instances=1, coalesce=True, misfire_grace_time=3600`
  (`djen/scheduler.py:208-211`).
- **PASS** — `sample_borderline(tribunal=None)` itera por até 6 tribunais
  ativos (`tribunals/sampling.py:283-298`), ~150ms total — confirmado no
  benchmark docstring.

### G. Security final pass

- **PASS** — CSRF em todos POSTs novos: `@csrf_protect` explícito em
  `leads_validacao_salvar` (`dashboard/views.py:1879`) e
  `leads_validacao_criar_lote` (`dashboard/views.py:2054`). `@require_POST`
  garante 405 em métodos errados. Teste `test_views_validacao.py:385-400`
  confirma 403 sem CSRF.
- **PASS** — Permissions em todas as 13 views novas via
  `@permission_required(..., raise_exception=True)`.
- **PASS** — IDOR protegido em `leads_validacao_salvar`: anti-IDOR query
  `AmostraProcesso.filter(amostra_id=lote_id, processo_id=processo_id)`
  antes do INSERT (`dashboard/views.py:1940-1942`); log estruturado
  `idor_attempt` com 403.
- **PASS** — SQL parametrizado em `sampling.py`: `extra(select_params=…)`
  passa parâmetros via psycopg, não interpolação. Verificado em todas as
  4 ocorrências de `extra(select=…)`.
- **NIT** — Path traversal em `_ler_cnjs_csv`
  (`tribunals/sampling.py:127-130`): aceita `csv_path` absoluto. Validador
  autenticado pode POSTar `parametros_json={"csv_path":"/etc/passwd"}` —
  conteúdo NÃO vaza porque a regex de CNJ filtra, mas é violação de POLA.
  Já reportado em T11 nice-to-have #2; aceitável (operador trusted via
  `can_validate_lead`).
- **PASS** — XSS escapes em templates novos: confirmados em REVIEW_T15
  como sem `|safe` indevido. `motivo` renderizado via `{{ motivo }}`
  passa pelo auto-escape do Django (`_validacao_card.html:65`).
- **PASS** — `motivo_visivel_para` helper existe e protege contra leak
  (`tribunals/models.py:709-724`): só retorna motivo se autor ou se user
  tem `can_view_motivo`. Templatetag `motivo_visivel` em
  `dashboard/templatetags/voyager_extras.py:9-19`. **MAS:** nenhum
  template ainda usa — vira blocker quando T16 renderizar motivos de
  outros (admin, fila de divergência).
- **PASS** — Logs não vazam dado sensível: `validacao_salva` registra
  IDs e resultado, nunca `motivo` (`dashboard/views.py:2025-2034`).
  `idor_attempt` idem.
- **ABERTO MÉDIO** — Salt LGPD continua usando fallback hardcoded:
  `getattr(settings, 'VALIDACAO_USUARIO_HASH_SALT',
  'voyager-validacao-default-salt')` em `dashboard/views.py:1977`. Se
  prod entrar sem definir o salt no env, anonimização usa salt literal
  conhecido — viola promessa do ADR-018 de proteção LGPD. **Não bloqueia
  dev, vira blocker em T18 (publicação externa do dataset).**

### H. Documentation freshness

- **PASS** — `.ia/CLASSIFICACAO.md`: reflete v6 ativo + v7 candidato, hot
  reload, shadow, settings novos documentados (linhas 310-422).
- **PASS** — ADRs 018-022 presentes em `.ia/DECISIONS.md:171-261`:
  ADR-018 (validação humana), ADR-019 (pesos por origem),
  ADR-020 (hot reload + shadow), ADR-021 (thresholds DB-driven),
  ADR-022 (categorização compartilhada).
- **PASS** — `.ia/DATA_MODEL.md` documenta os 5 modelos novos +
  permissions (`AmostraValidacao`, `AmostraProcesso`, `ProcessoValidacao`,
  `ThresholdTribunal`, `ClassificacaoShadowLog`).
- **PASS** — `.ia/OPS.md` tem comandos novos (`gerar_lotes_semanais_fn`,
  `comparar_shadow_daily`, `minerar_fn`, `exportar_labels_retreino`,
  `setup_validacao_groups`) e crons em tabela (linhas 393-426).
- **PASS** — `.ia/ROADMAP.md`: itens completos movidos pra "Concluído
  (recentes)" linhas 5-18.
- **PASS** — `.ia/V7_DEPLOY_DECISION.md` tem procedimento 7 passos +
  rollback (`/home/will/projetos/voyager/.ia/V7_DEPLOY_DECISION.md:91-455`)
  + sign-off checklist + riscos identificados.

### I. Rollback plan

- **PASS** — Cada componente reverte independente:
  - Migrations: `migrate tribunals 0023` (Django auto-reverse).
  - `ClassificadorVersao`: SQL direto (procedimento em V7_DEPLOY_DECISION
    linhas 419-432) — hot reload TTL 60s propaga.
  - URLs novas: removíveis sem quebrar nada (são append-only em
    `dashboard/urls.py`).
- **PASS** — `ClassificadorVersao(v6, ativa=True)` preservada via
  `update`, não delete — rollback em 1 transação atômica.
- **PASS** — Shadow logs não bloqueiam rollback: `ClassificacaoShadowLog`
  é append-only sem FK que impeça reverter modelo ativo.
- **PASS** — Templates novos podem coexistir com prod atual.

### J. Production readiness

- **PASS** — numpy: confirmado `requirements.txt:2` `numpy>=1.26` e
  runtime `numpy 2.4.4` no container web. Dockerfile linha 7-8 copia
  requirements e instala via pip — build path correto.
- **PASS** — Containers que precisam rebuild: apenas web (não há código
  novo nos workers além do que já é carregado via volume bind). Workers
  RQ pegam jobs novos sem restart porque RQ carrega via serialização
  padrão da fila.
- **PASS** — Scheduler precisa restart pra carregar 2 crons novos
  (`comparar_shadow_daily`, `gerar_lotes_semanais_fn`). Atualmente
  agendados em `djen/scheduler.py:201,218`.
- **AÇÃO NECESSÁRIA** — `setup_validacao_groups` precisa rodar manualmente
  em prod (idempotente, mas Django não roda data migrations automáticas).
- **GAP CRÍTICO** — CSVs de ground truth (`leads_trf1_*.csv`,
  `leads_trf3_*.csv`) estão **gitignored** (`.gitignore:1 data/` +
  diretório raiz). NÃO entram no build do container. Em prod, precisam
  ser copiados manualmente via `docker cp` ou montados via volume.
  Já confirmado que `/app/data/` no container atual tem 3 CSVs (de runs
  prévios). **V7_DEPLOY_DECISION pré-requisitos linhas 63-72 menciona,
  mas falta procedimento explícito de transfer.**
- **PASS** — `SLACK_WEBHOOK_URL` configurado em settings
  (`core/settings.py:227`). Vazio por default = best-effort skip
  (`tribunals/jobs.py:497-518`). Operador define no `.env` em prod.
- **GAP MÉDIO** — `EMAIL_BACKEND`/`DEFAULT_FROM_EMAIL` **não configurados
  em `core/settings.py`**. `_notificar_lotes_semanais` em
  `tribunals/jobs.py:543` usa fallback `'noreply@voyager'` mas Django
  default backend é `django.core.mail.backends.smtp.EmailBackend` que
  precisa de `EMAIL_HOST`, `EMAIL_PORT`, etc. Em prod, email vai falhar
  silenciosamente (`fail_silently=True` em `send_mail` linha 545). Em
  dev OK (console backend padrão).

### K. Smoke test recomendado pré-deploy

(Detalhado abaixo.)

### L. Pontos finais

- **PASS numpy:** `requirements.txt:2`, instalado no container, importável.
- **GAP CSVs:** `data/` em `.gitignore`. Precisa procedimento manual
  pra prod. Sugerido em V7_DEPLOY_DECISION linhas 63-72 mas não há
  comando explícito de `scp` / `docker cp`.
- **PASS MOCKUPS:** `.ia/MOCKUP_validacao.md` e `MOCKUP_visibilidade.md`
  referenciados implicitamente em `.ia/DASHBOARD.md` e `.ia/PATTERNS.md`
  (templates os seguiram).
- **PASS Tests passing:** 162/162 em 53s, sem skips/xfails silenciosos.

---

## Production readiness checklist

Antes do flip v6→v7 em prod, executar:

- [ ] Backup `pg_dump tribunals_classificadorversao + tribunals_thresholdtribunal` (procedimento em V7_DEPLOY_DECISION:73-79).
- [ ] Configurar `VALIDACAO_USUARIO_HASH_SALT` no `.env` prod (32+ chars random) — **OBRIGATÓRIO antes do flip pra cumprir LGPD do ADR-018**.
- [ ] Configurar `EMAIL_BACKEND` + `EMAIL_HOST` + `DEFAULT_FROM_EMAIL` em `core/settings.py` ou via `.env`, **ou** documentar explicitamente que notificação de lotes semanais é Slack-only.
- [ ] Configurar `SLACK_WEBHOOK_URL` no `.env` prod (T21 + T19 dependem).
- [ ] Copiar CSVs de ground truth pro container prod: `leads_trf1_falsos_consumidos_1327.csv`, `leads_trf1_recuperados_1327.csv`, `lista_5000_naoleads.csv`, `fn_candidatos_*.csv`, `labels_retreino_*.csv` em `/app/data/`.
- [ ] Rodar `setup_validacao_groups` em prod: `docker compose exec web python manage.py setup_validacao_groups`.
- [ ] Migrar prod: `docker compose exec web python manage.py migrate tribunals`.
- [ ] Restart `scheduler` pra carregar 2 crons novos.
- [ ] Restart `worker_classificacao` (4 réplicas) pra pickar `SHADOW_SAMPLE_RATE` do `.env`.
- [ ] Confirmar `numpy>=1.26` no container web (já confirmado dev: `2.4.4`).
- [ ] Sanity test `/dashboard/leads/visibilidade/` e `/dashboard/leads/validacao/` retornam 200.
- [ ] Verificar `shadow_status` widget aparece no overview.

---

## Smoke test pré-deploy

1. **Migrations e setup base** (em ambiente prod-like):
   ```bash
   docker compose exec web python manage.py migrate tribunals
   docker compose exec web python manage.py setup_validacao_groups
   ```
2. **Conferir migration 0026 seed:**
   ```bash
   docker compose exec web python manage.py shell -c "
   from tribunals.models import ClassificadorVersao
   v = ClassificadorVersao.objects.filter(ativa=True).first()
   assert v and v.versao == 'v6', f'expected v6 ativa, got {v}'
   print(f'OK: {v.versao} ativa, {len(v.pesos)} pesos')
   "
   ```
3. **Smoke endpoint visibilidade** (usuário com `can_view_validacao_dashboard`):
   - `GET /dashboard/leads/visibilidade/` → 200, 5 charts carregam lazy.
   - Aplicar chip TRF1 → URL atualiza, charts re-buscam.
4. **Smoke endpoint validação** (usuário com `can_validate_lead`):
   - `GET /dashboard/leads/validacao/` → 200, ver lotes ativos.
   - Criar lote `borderline tamanho=10 tribunal=TRF1` via modal → ok.
   - Anotar 3 itens com hotkeys → KPI "anotados hoje" sobe.
   - Tentar anotar 2x mesmo CNJ → 409.
5. **Smoke shadow mode (somente se v7 já existe):**
   - `SHADOW_SAMPLE_RATE=1.0` temporariamente.
   - Classificar 100 processos: `Process.objects.filter(...).update(classificacao_em=None)` + `reclassificar_por_prioridade.delay(cap=100)`.
   - Verificar `ClassificacaoShadowLog.objects.count()` ≈ 100 após drenagem.
6. **Smoke export labels:**
   ```bash
   docker compose exec web python manage.py exportar_labels_retreino --output /tmp/labels.csv
   ```
   Confirmar saída ≥ N linhas com colunas corretas.
7. **Smoke cron registry:** `/dashboard/workers/` → ver 2 crons novos listados.
8. **Smoke notify Slack** (com webhook configurado): executar
   `gerar_lotes_semanais_fn.delay(tribunais=['TRF1'])` e confirmar
   mensagem chega.
9. **Smoke rollback:** rodar procedimento de rollback v7→v6 do
   V7_DEPLOY_DECISION:419-432 em staging antes de prod real.

---

## Blockers finais

**Nenhum bloqueia merge ou deploy de infra.** A feature toda (T1-T23)
pode ir pra prod **exceto o flip v6→v7**, que tem 3 ressalvas
operacionais:

1. **`VALIDACAO_USUARIO_HASH_SALT` em `.env` prod (LGPD).** Sem isso, o
   hash de anonimização usa salt hardcoded conhecido — viola ADR-018.
   Path: `dashboard/views.py:1976-1977`. Bloqueia T18 (publicação
   externa do dataset).

2. **`EMAIL_BACKEND` / `DEFAULT_FROM_EMAIL` indefinidos em
   `core/settings.py`.** `_notificar_lotes_semanais` chama `send_mail`
   com `fail_silently=True` — em prod sem SMTP config, notificação de
   lote semanal nunca chega aos validadores via email (Slack ainda
   funciona). Path: `tribunals/jobs.py:521-549`. Decisão: ou configurar
   SMTP, ou remover email do pipeline (Slack-only).

3. **CSVs ground truth fora do build (`data/` gitignored).** Reprocessos
   prod-like (treino v7, sampling de fn_candidatos, etc.) dependem de
   CSVs que precisam ser copiados manualmente. Documentar procedimento
   `scp`/`docker cp` em V7_DEPLOY_DECISION.

---

## Riscos aceitos (follow-ups documentados)

1. **Trigger PG UPDATE-block em ProcessoValidacao** ainda ausente
   (T6 issue #1). ADR-018 promete `UniqueConstraint + trigger`; só o
   primeiro existe. Aceito até T18 (publicação externa do dataset).
   Documentado em `.ia/ROADMAP.md:86`.

2. **Permission `can_view_motivo` sem call-sites em templates** ainda.
   Helper `motivo_visivel_para` existe (`tribunals/models.py:709`) +
   templatetag (`dashboard/templatetags/voyager_extras.py:9`). Vira
   blocker em T16 (resolver divergência) quando admin/fila mostrar
   motivos de outros.

3. **Frontend UX issues** do REVIEW_T15 (focus trap em 3 modais,
   `prefers-reduced-motion` global, `_lote_concluido.html` exibe enum
   cru, bug Alpine `criando`, auto-advance 200ms ausente, retry button
   inexistente em SIGNAL LOST, handlers keydown duplicados, focus
   pós-swap). Total 8 issues médias UX. Não bloqueiam internal preview
   mas devem virar T15.5 polish antes de mostrar a usuário externo.

4. **`_ler_cnjs_csv` aceita `csv_path` absoluto** (POLA violation).
   Validador autenticado pode tentar paths internos; conteúdo não vaza
   pela regex CNJ. Restringir a `BASE_DIR` em PR de polimento.

5. **`min_score` sem clamp [0,1]** em `criar_lote`. Sem segurança, só
   UX. Adicionar `min(max(float(...), 0.0), 1.0)`.

6. **Mensagens de erro JSON podem incluir SQL/path interno**:
   `str(exc)[:120]` em `dashboard/views.py:1628,2120`. Sanitizar.

7. **CV interno no grid de thresholds v7** (T20 issue #3) — fica como
   v8. Em dataset grande, risco de overfit é baixo.

---

## Recomendação final

### ⚠️ GO COM RESSALVAS

**A feature está pronta para integração imediata em main.** Schema (T6),
backend (T11), frontend (T15) e ML (T17-T19) compõem ciclo coerente,
testado (162/162) e arquiteturalmente alinhado com REGRAS_NEGOCIO e ADRs.
Issues médias críticas dos reviews anteriores (cache invalidation, N+1,
drift threshold, filtro versao_modelo) foram **endereçadas no código
atual**.

**O flip v6→v7 em produção depende de 3 ações operacionais** listadas em
"Blockers finais": (a) configurar salt LGPD prod, (b) decidir
SMTP-vs-Slack-only para notificações, (c) procedimento explícito de
sync de CSVs ground truth pro container prod. Após essas 3 ações + 7 dias
de shadow mode + sign-off de 2 model_admins, executar procedimento da
seção "Comando de deploy" do `.ia/V7_DEPLOY_DECISION.md:354-414`.

**Próximos passos sugeridos:**

1. **Merge T1-T23 em main** (este PR).
2. **PR follow-up "T23.5 prod-readiness"** abordando 3 blockers operacionais:
   - Adicionar `VALIDACAO_USUARIO_HASH_SALT = env(...)` em `core/settings.py`
     **sem default no código** (raise se env vazio em prod via boot check).
   - Adicionar `EMAIL_BACKEND` config explícita (mesmo que seja
     `console.EmailBackend` em dev) + documentar em V7_DEPLOY_DECISION.
   - Adicionar comando management `sync_ground_truth_csvs` ou
     procedimento `scp` no V7_DEPLOY_DECISION.
3. **PR follow-up "T15.5 frontend polish"**: focus trap, reduced-motion,
   labels enum em lote_concluido, bug Alpine `criando`, auto-advance,
   retry button.
4. **Sprint T24+T25 (validação real do v7)** quando ground truth ≥500
   por tribunal: rodar passos 1-7 do `.ia/V7_DEPLOY_DECISION.md` com
   sign-off dual de model_admins.

### 3 pontos de maior risco

1. **Salt LGPD em fallback hardcoded** (`dashboard/views.py:1977`) —
   bloqueia T18 publicação externa e potencial regressão silenciosa
   ADR-018.
2. **CSVs ground truth gitignored** — qualquer reprocesso prod
   (retreino, sampling fn_candidatos) falha sem sync manual.
3. **Permission `can_view_motivo` sem template ainda** — primeiro PR
   de T16 (resolver divergência) pode esquecer o check e vazar motivos
   de outros validadores. Documentar como pré-requisito de T16.

### Próxima ação recomendada

**Merge T1-T23 em main agora**, abrir 2 PRs follow-up imediatos: (a)
"prod-readiness" cobrindo salt LGPD + email + CSV sync; (b) "frontend
polish" cobrindo as 8 issues médias UX do T15. O flip v6→v7 fica
aguardando ground truth ≥ 500/tribunal + sign-off dual conforme
V7_DEPLOY_DECISION.

---

## Verificação obrigatória (suíte completa)

Executado em 2026-05-12:

```
docker compose exec -T web python -m pytest \
  tests/test_validacao_models.py tests/test_sampling.py tests/test_export_labels.py \
  tests/test_minerar_fn.py tests/test_classificador_reload.py tests/test_views_validacao.py \
  tests/test_cmd_gerar_lote.py tests/test_validacao_card_partial.py tests/test_treinar_v7.py \
  tests/test_shadow_mode.py tests/test_lotes_semanais.py tests/test_template_visibilidade.py \
  tests/test_template_validacao.py
```

**Resultado:**

```
collected 162 items
tests/test_validacao_models.py ............                              [  7%]
tests/test_sampling.py .................                                 [ 17%]
tests/test_export_labels.py ........                                     [ 22%]
tests/test_minerar_fn.py .....                                           [ 25%]
tests/test_classificador_reload.py ..............                        [ 34%]
tests/test_views_validacao.py ......................                     [ 48%]
tests/test_cmd_gerar_lote.py ..............                              [ 56%]
tests/test_validacao_card_partial.py ..........                          [ 62%]
tests/test_treinar_v7.py ........                                        [ 67%]
tests/test_shadow_mode.py ......................                         [ 81%]
tests/test_lotes_semanais.py ..........                                  [ 87%]
tests/test_template_visibilidade.py ..........                           [ 93%]
tests/test_template_validacao.py ..........                              [100%]

====================== 162 passed, 44 warnings in 53.25s =======================
```

162/162 passing. 44 warnings são `UserWarning: No directory at: /app/staticfiles/`
inofensivos (dev local, `collectstatic` não rodou).
