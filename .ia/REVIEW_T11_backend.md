# Code Review T11 — Backend (T7+T8+T9+T10)

**Reviewer:** code-reviewer-sec (squad Voyager)
**Data:** 2026-05-12
**Escopo:** T7 sampling.py, T8 dashboard/views+queries+urls+migration 0027, T9
gerar_lote_validacao, T10 export_labels + comando.

**Veredito:** APPROVE WITH NITS

Backend está sólido em segurança e correção funcional, com **um bug
operacional confirmado** (cache invalidation silenciosamente no-op em prod
porque o backend Redis configurado é o nativo do Django, não django-redis).
Sem isso, charts `top_fn_semana`, `funil_ampliado` e `calibracao` continuam
servindo stale por até 5 min após cada validação — UX ruim, não vaza dado.
Os outros achados são nits/médias. Nenhum issue de segurança crítico.

## Resumo executivo

- **Permissions:** todas as 13 views novas têm `@login_required` +
  `@permission_required(..., raise_exception=True)` corretas. CSRF e
  `@require_POST` aplicados nos 2 endpoints de mutação. Anti-IDOR em
  `leads_validacao_salvar` valida `(processo_id, lote_id)` via
  `AmostraProcesso` antes do INSERT, com log estruturado `idor_attempt`.
- **Permission `can_view_motivo` criada (T8) mas nunca usada.** A view
  `leads_validacao_item` e o admin não diferenciam autor vs outros ao renderizar
  `motivo`. Como T8 não inclui template/serializer que exibe motivo de outros
  (cada validador só vê o próprio fluxo), aceitável; vira blocker em T12/T13
  (templates de revisão sênior + admin) quando isso for renderizado.
- **SQL injection:** `sampling.py` parametriza seed via `select_params` — não
  vulnerável. `extra(order_by=['_h'])` usa alias, OK. Filtros vindos de
  `request.GET` (tribunal, classificacao, dias, min_score) são todos
  `.upper()`+sanitizados ou casted via `float/int` com fallback.
- **Bug operacional confirmado:** `cache.delete_pattern(...)` em
  `leads_validacao_salvar` (views.py:1966-1971) só funciona com django-redis;
  o projeto usa `django.core.cache.backends.redis.RedisCache` (settings.py:132),
  que **não** expõe `delete_pattern`. O `try/except Exception: pass`
  esconde o `AttributeError` em prod — invalidation silenciosa nunca acontece.
- **Performance:** `kpis_validacao` faz **2N queries** num loop sobre até 20
  lotes (queries.py:77-84) — flagado pelo time. Custo total ~40 queries por
  hit. Cacheável (5 min TTL) ou consolidável via single GROUP BY.
- **Cache key:** `_chart_validacao_cache_key` não inclui `request.user.pk` —
  válido porque os dados servidos são agregados não-PII e a permission é
  `can_view_validacao_dashboard` (todos os autorizados veem o mesmo). OK.
- **LGPD:** hash de username com salt usa fallback em código se
  `VALIDACAO_USUARIO_HASH_SALT` não estiver no settings. Salt default é
  literal hardcoded (`'voyager-validacao-default-salt'`) — **vira blocker em
  prod** se a feature de anonimização entrar antes de definir o salt em
  settings com `os.environ`.

---

## A. Permissions / Authentication

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| Toda view tem `@login_required` | OK | views.py:1618,1633,…,2022 | 13 views novas, todas têm. |
| Permission read = `can_view_validacao_dashboard` | OK | views.py:1620,1635,1648,1661,1671,1682,1694 | 7 views read. |
| Permission anotar = `can_validate_lead` | OK | views.py:1724,1744,1818,1840,2021 | 5 views. |
| Permission resolver = `can_resolve_disagreement` | N/A | — | Endpoint não existe ainda. Será T16 (resolver divergência). |
| `raise_exception=True` em todas | OK | views.py:1620,…,2021 | 13 ocorrências corretas. Sem isso seriam redirect 302 para login = bad UX security (revela existência da view). |
| Superuser bypass | NIT | tests/test_views_validacao.py | Falta teste. Django Auth permite superuser bypassar `permission_required`. Documentar ou testar. |
| Endpoints chart leak data sensível | OK | views.py:1636-1687 | Charts agregam (KPIs, histograma, decis, heatmap) — sem PII. Permission `can_view_validacao_dashboard` correta. |
| Permission `can_view_motivo` | NIT crítico | migration 0027, models.py:702, setup_validacao_groups.py:30 | **Permission existe e está em grupos `revisores_seniores`/`auditores_leads`, mas zero views/serializers/templates a checam.** T12/T13 vão precisar. |

## B. CSRF / Mutações

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| `@require_POST` em mutações | OK | views.py:1838,2019 | salvar + criar_lote. |
| `@csrf_protect` ativo | OK | views.py:1839,2020 | Decorador explícito (cinto+suspensório com middleware). |
| Test CSRF 403 sem token | OK | test_views_validacao.py:385-400 | `Client(enforce_csrf_checks=True)`. |
| JSON parsing robusto | OK | views.py:1857-1861 | `json.JSONDecodeError → 400 JSON`. |
| 405 em métodos errados | OK | — | `@require_POST` gera 405 automaticamente. |

## C. IDOR / Authorization

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| `leads_validacao_salvar` valida `(processo_id, lote_id)` | OK | views.py:1900-1915 | `AmostraProcesso.filter(amostra_id, processo_id).first()` antes do INSERT. Log `idor_attempt`. 403 retornado. |
| Test IDOR cross-lote | OK | test_views_validacao.py:286-299 | Lote A + Lote B, POST com processo de B em lote_id A = 403. |
| `leads_validacao_lote/item` permite ver qualquer lote_id | NIT | views.py:1722-1813 | Qualquer usuário com `can_validate_lead` pode acessar `?lote_id=NN` arbitrário. Não é exatamente IDOR (todos validadores podem ver fila), mas falta `AuditoriaAcesso` (RNV §7 l. 299). Acompanhar com T16. |
| `posicao` arbitrária na URL | OK | views.py:1763-1775 | Itens carregados por ordem; `posicao>total` → redirect concluido. Não revela dados de outros lotes. |
| `criar_lote` valida estratégia | OK | views.py:2030-2035 | Whitelist `_ESTRATEGIAS_PERMITIDAS` (set inline). |
| `criar_lote` tamanho clampado | OK | views.py:2041-2042 | `1 ≤ tamanho ≤ 5000`. Bom guard contra DoS. |

## D. SQL Injection / Mass Assignment

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| `extra(select=..., select_params=[...])` parametrizado | OK | sampling.py:278-279, 294-295, 325-326, 398-399 | `select_params` é parametrizado via psycopg. Seed convertida para `str` antes — `str(seed) + sigla/classe` ainda parametrizado. Safe. |
| `extra(order_by=['_h'])` alias seguro | OK | sampling.py:280,296,327,400 | `_h` é alias do SELECT, não user input. |
| Filtros `request.GET` sanitizados | OK | views.py:1571-1577 | `.upper()`, `_split_csv` (split + strip), `int()` com fallback, `max(1, min(int(...), 365))` para dias. |
| `min_score` float user-input | OK | views.py:2077,2082 | `float(parametros.get('min_score', 0.85))`. Falta validação de range [0,1] — `min_score=-1` ou `1e308` passa. **NIT médio** — não causa SQL injection mas pode gerar query sem matches ou DoS via NaN. |
| `export_labels` paths controlados | OK | export_labels.py:403,409 | `base_dir = settings.BASE_DIR`. `--output` aceita path arbitrário do CLI, mas é management command (operador trusted). Não há path traversal exposto via HTTP. |
| `_ler_cnjs_csv` aceita absolute/relative | NIT | sampling.py:127-130 | `csv_path` pode ser absoluto. Em `criar_lote` via UI, `parametros.get('csv_path')` chega como string. **POC**: validador autenticado posta `parametros_json={"csv_path": "/etc/passwd"}` — função tenta abrir e falha pela regex CNJ. Conteúdo NÃO vaza (`_ler_cnjs_csv` só lê linhas que parecem CNJ). Erro vira 500 com mensagem genérica. Aceitável, mas seria mais defensivo restringir paths a `BASE_DIR`. |

## E. Performance

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| `select_related` aplicado | OK | views.py:1701,1759,1786, queries.py:157,338 | Tribunal/processo/parte com select_related onde renderiza. |
| `kpis_validacao` 2N queries em loop | MEDIUM | queries.py:77-84 | Para 20 lotes = 40 COUNT queries (~20-60ms total). UI carrega em toda visit ao /visibilidade e /validacao/. **Sugestão:** consolidar via `lotes_qs.annotate(total=Count('itens'), anotados=Count('validacoes', filter=Q(validacoes__usuario=usuario)))` — 1 query. Ou cachear 5min com user_id na key. |
| `AmostraProcesso.filter(amostra_id).order_by('ordem')` usa índice | OK | tribunals/models.py (M2M through) | UniqueConstraint `(amostra, processo)` + Index `(amostra, ordem)` documentado em T6. |
| Cache `delete_pattern` em prod | **BUG** | views.py:1966-1971 | **Confirmed bug:** `django.core.cache.backends.redis.RedisCache` (settings.py:132) **não tem** `delete_pattern`. Só `django-redis`. `except Exception: pass` engole o `AttributeError` silenciosamente. Resultado: cache nunca invalida → charts ficam stale por até 300s pós-save. Não há vazamento — só UX degradada. Conserto possível: trocar para `cache.delete_many([list_de_keys])` ou usar versioning de cache via `cache_version`. |
| `sample_borderline tribunal=None` faz N sub-queries | OK | sampling.py:283-298 | 1 query por tribunal ativo (max ~6), cada uma com index scan na banda + sort top-K. ~150ms total. |
| `sample_random_tribunal` TABLESAMPLE | INFO | sampling.py:392-402 | Docstring promete TABLESAMPLE BERNOULLI mas implementação real usa `extra(order_by='md5')` por classe. **Doc vs código divergente**. Performance ainda OK por estar filtrado por `(tribunal, classificacao)` que entra no índice composto. Recomendo atualizar a docstring. |
| `compute_features` síncrono em `salvar` | OK | views.py:1924-1933 | Wrap em try/except, snapshot best-effort. ~50-200ms por save. Para 10 validadores anotando 1 CNJ/min = 0.6 req/s; 50-200ms latência é aceitável. Sem fila assíncrona necessária agora. |
| `kpis_validacao(usuario)` na hot path de `leads_visibilidade` E `leads_validacao_overview` | MEDIUM | views.py:1623,1699 | Toda navegação dispara as 40 queries. **Sugestão:** memoize por (user_id, minuto). |

## F. Cache

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| Cache keys consistentes | OK | views.py:1580-1588 | `voyager:chart:<nome>:<md5(filtros)>`. Falta versioning explícito da query (se schema mudar, cache não expira até TTL). Aceito porque TTL é só 5min. |
| Invalidation correta | **BUG** | views.py:1966-1971 | Ver seção E — `delete_pattern` no-op silencioso. |
| TTL apropriado | OK | views.py:1562 | 5min razoável para chart de gate de modelo. |
| `_safe_cache_get` engole exceções Redis | OK | views.py:18-23 | Bom — Redis down não derruba dashboard. |

## G. Logging / Auditoria

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| Criação de ProcessoValidacao logada | OK | views.py:1991-2000 | `validacao_salva` com lote_id, usuario_id, processo_id, resultado. Estruturado via `extra`. |
| Tentativas IDOR logadas | OK | views.py:1904-1912 | `idor_attempt` com action+ids. Boa instrumentação para alerting. |
| Sensitive data em logs | OK | views.py:1991-2000 | Logs não incluem `motivo`. Resultado, lote_id, usuario_id é OK. |
| Exception em chart → Sentry mas response não vaza | OK | views.py:1600-1607 | `exception()` logado completo, `JsonResponse` só vaza `str(exc)[:120]`. **NIT:** mensagem `str(exc)[:120]` pode incluir SQL ou path interno em caso de DB error — restringir a "erro interno" e logar o resto. |
| `criar_lote_falhou` exception | OK | views.py:2120-2125 | `str(exc)[:200]` no JSON — idem nit acima. |

## H. Validação de input

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| `tempo_segundos` validado | OK | views.py:1886-1895 | Casted para int, `>0 and <_TEMPO_MAX_SEGUNDOS (3600)`. Faltam micro-ranges (0 segundos não permitido = OK; máx exclusivo é 3599 = consistente com spec "< 1h"). |
| `motivo` truncado | OK | views.py:1897 | `[:_MOTIVO_MAX_CHARS]` (5000). Coerente com spec. |
| `resultado` em choices | OK | views.py:1873-1879 | Whitelist `{c[0] for c in RESULTADO_CHOICES}`. |
| `confianca` em choices | OK | views.py:1881-1884 | Whitelist com default `media`. |
| `motivo` aceita HTML | NIT | views.py:1897 | Persistido cru. Quando renderizado em template, **deve usar `|escape`** (default Django no `{{ var }}` já escapa; mas se for `|safe`, vaza XSS). T12/T13 precisa garantir isso. |
| `min_score` range | NIT | views.py:2077,2082 | Sem clamp `[0,1]` — passar `-99` ou `999` não causa SQL injection mas retorna lote vazio. |

## I. Concorrência / Race

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| 2 workers salvando mesma validação | OK | views.py:1942-1961 | UniqueConstraint + `IntegrityError → 409 "re-anotação proibida"`. Mensagem amigável. |
| `criar_lote` atomic rollback | OK | sampling.py:504-524 | `transaction.atomic()` em todo o bloco. Testado em test_sampling.py:332-350 (monkeypatch força bulk_create a falhar; nada persiste). |
| `_excluir_recentes` race | INFO | sampling.py:79-98 | TOCTOU: lote A em criação inclui proc X → lote B também sorteia X antes do commit de A. Resultado: ambos lotes têm X. UniqueConstraint(amostra, processo) é por lote, não global — não bloqueia. Em prática, cron semanal sequencial. Aceito; documentar no docstring (já parcialmente). |

## J. Export labels (T10)

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| CSVs root — path traversal | OK | export_labels.py:403 | `base_dir = settings.BASE_DIR` (fixo). CLI `--output` aceita arbitrário, mas é management — operador trusted. Sem exposição HTTP. |
| Pandas/csv read robusto | OK | export_labels.py:123-145 | `csv.reader`, tolera com/sem header. Linhas inválidas puladas. Boa defesa contra CSV malformado. |
| `min_data` filtra correto | OK | export_labels.py:212-213,245-246 | Aplica `consumido_em__gte` e `criada_em__gte`. Testado em test_export_labels.py:237-285 (proc velho 365d + cutoff 30d → só recente persiste). |
| Pesos por origem | OK | export_labels.py:43-46 | 3.0 humano, 2.0 juriscope+csv_reforçado, 1.0 csv_base. Bate com spec RNV §LGPD/Treino. |
| Dedupe por CNJ | OK | export_labels.py:288-296 | Maior peso vence (humano > juriscope > csv). Empate de peso resolve por `_ORIGEM_PRIORIDADE`. Conflito de label flagado. |
| `label_final` precedência | OK | export_labels.py:250 | `resultado_efetivo = pv.label_final or pv.resultado`. Testado em test_export_labels.py:161-186. |
| Exclui incerto/skip/precisa_enriquecer | OK | export_labels.py:59,251 | Skip explícito. Testado em test_export_labels.py:191-232. |
| Idempotência (mesma ordem) | OK | export_labels.py:319-320 | `rows.sort(key=cnj)`. Testado em test_export_labels.py:289-314 (bit-for-bit equal). |

## K. Tests sufficiency

| Item | Status | Path:linha | Comentário |
|---|---|---|---|
| 17+14+17+8 = 56 testes críticos | OK | tests/* | Cobertura razoável dos paths principais. |
| Edge cases ausentes | NIT | — | Falta: lote vazio (`tamanho_alvo=N` mas QS retorna 0), `motivo` vazio (já implicitamente OK), `score=0/1.0` exato (borderline `<` exclusivo testado mas não validado nas extremidades). |
| Permissions tests cada perm | OK | test_views_validacao.py:137-204,338 | `noperm_user` testa todas as 4 perms (403). |
| Superuser bypass | NIT | — | Não testado. Documentar comportamento (provavelmente bypass por Django default). |
| Tests de concorrência | NIT | — | UniqueConstraint testado via 2 saves sequenciais (test_validacao_salvar_duplicate). Race real (2 threads simultâneas) não testado — `pytest-django` não oferece isolamento de transação adequado. Aceitável. |

---

## Blockers (devem ser resolvidos antes de prosseguir)

**Nenhum.** Nenhum issue de segurança crítico ou bug que impeça T11+
(frontend) avançar. Os blockers reais virão em T12/T13 quando templates
renderizarem `motivo` (precisará checar `can_view_motivo`).

## Issues médias (criar follow-up)

1. **Cache `delete_pattern` é no-op silencioso em prod.** Backend nativo Django
   Redis não suporta. Trocar por `cache.delete_many([list de keys])` ou
   versionar key com `cache_version`. Sem isso, charts ficam stale 5min
   após cada validação — pequena, mas é dívida que cresce com tráfego.
   Path: `dashboard/views.py:1964-1971`.
2. **`kpis_validacao` 2N queries por hit.** ~40 queries em ~20-60ms total,
   mas roda em toda visit a `/dashboard/leads/visibilidade/` e
   `/dashboard/leads/validacao/`. Consolidar via `annotate` (1 query) ou
   cachear 5min com chave `(user_id, minuto)`.
   Path: `dashboard/queries.py:77-84`.
3. **Permission `can_view_motivo` criada mas zero call-sites.** Esquecida em
   templates/serializers. Vira blocker em T12/T13 quando rendering de
   `motivo` de outros existir. Path: `tribunals/migrations/0027`,
   `tribunals/models.py:702`, `setup_validacao_groups.py:30`.
4. **Salt LGPD default em código.** `getattr(settings,
   'VALIDACAO_USUARIO_HASH_SALT', 'voyager-validacao-default-salt')` em
   `views.py:1936-1937`. Se prod entrar sem definir o salt no env,
   anonimização vai usar o salt literal — não compromete imediatamente
   (ainda é hash + salt fixo conhecido), mas é regressão silenciosa do ADR-018.
   **Recomendado:** levantar erro de boot se settings não estiver
   definido E feature flag de anonimização ativa.

## Nice-to-have / micro-otimização

1. `min_score` em `criar_lote` deveria fazer `min_score = min(max(float(...),
   0.0), 1.0)` (clamp [0,1]). Sem isso, valores fora de faixa só retornam
   lotes vazios — não é vulnerabilidade, é UX.
2. Restringir `csv_path` em `criar_lote` via UI a `BASE_DIR` apenas
   (atualmente aceita absolute path; embora não vaze conteúdo, é POLA
   violation). Validar em `views.py:2072-2098`.
3. Atualizar docstring de `sample_random_tribunal` que promete TABLESAMPLE
   BERNOULLI mas usa `extra(order_by='md5')`. Path: `sampling.py:373-378`.
4. Sanitizar mensagens de erro em JSON responses (views.py:1602-1605,
   2125): `str(exc)[:120]` pode incluir SQL fragments. Trocar por
   "erro interno (request_id=X)" + log completo via Sentry.
5. Tests:
   - Adicionar teste de superuser bypass (documentar comportamento).
   - Adicionar teste de cache invalidation (verifica que após save,
     próximo GET retorna dado fresco).
   - Adicionar teste de min_score fora de faixa (clamp behavior).
6. `leads_validacao_overview` faz query inline `from tribunals.models
   import AmostraValidacao` (views.py:1697) — quebra padrão de imports no
   topo em PATTERNS.md §Imports. Mesmo em `leads_validacao_lote/item/
   concluido/salvar/criar_lote`. Aceitável como lazy-import pra evitar
   ciclo, mas inconsistente.

## Decisão final

**APPROVE WITH NITS.** T22 (frontend) desbloqueada — segurança backend está
boa o suficiente pra prosseguir. Bug do `delete_pattern` deve virar
follow-up explícito antes de produção. Permission `can_view_motivo` precisa
ser usada em T12/T13 ao renderizar texto livre de outros validadores.

## 3 issues mais importantes (priorizadas)

1. **`cache.delete_pattern` no-op silencioso em prod.** Backend é
   `django.core.cache.backends.redis.RedisCache` (Django nativo), que **não
   expõe** `delete_pattern`. O `except Exception: pass` engole o
   `AttributeError`, então charts cacheados nunca expiram via invalidation —
   só por TTL 5min. Não vaza dado, mas frustra a UX de "anotei e o KPI
   continua igual". Path: `dashboard/views.py:1964-1971`.

2. **`kpis_validacao` N+1 (40 queries por hit).** Loop sobre 20 lotes
   chamando 2 `.count()`. Total ~20-60ms mas em toda navegação `/leads/
   visibilidade/` e `/leads/validacao/`. Refatorar para single GROUP BY com
   annotate ou cachear 5min com `(user_id, minuto)` na chave. Path:
   `dashboard/queries.py:65-86`.

3. **Permission `can_view_motivo` criada mas sem call-site.** Migration 0027
   adiciona, `setup_validacao_groups` distribui aos grupos
   `revisores_seniores`/`auditores_leads`, mas **nada** no código verifica.
   Quando T12/T13 renderizar `motivo` de outros validadores em template
   (admin / fila de divergência), risco de leak de texto livre se
   esquecerem o check. Documentar como TODO obrigatório antes de T16.
