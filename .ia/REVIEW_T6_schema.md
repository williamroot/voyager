# Code Review T6 — Schema de validação humana

**Reviewer:** code-reviewer-arch (squad Voyager)
**Data:** 2026-05-11
**Escopo:** T4+T5 do plano — 5 models novos em `tribunals/models.py` (l. 481-775),
migration `0024_validacao_humana.py`, admins, `setup_validacao_groups`, 12 testes.

**Veredito:** APPROVE WITH NITS

Schema está sólido e fiel à spec naquilo que se propôs a entregar. Existe **1
gap conhecido e documentado** (trigger Postgres UPDATE-block do ADR-018) que NÃO
bloqueia T7-T10 mas **deve** virar follow-up obrigatório antes de T18 (publicação
externa do dataset). Demais pontos são nits ou clarificações de escopo.

## Resumo executivo

- Cobertura da spec (resultados, choices, constraints, LGPD) está completa. As 8
  estratégias incluem 2 a mais que a tabela base (fn_candidatos, shadow_disagree)
  — alinhado com T5, defensável.
- Imutabilidade da label é **parcial**: `UniqueConstraint(processo, usuario)`
  impede INSERT duplicado, mas **não há trigger PG impedindo UPDATE**. ADR-018
  explicitamente promete os dois mecanismos (`REGRAS_NEGOCIO_VALIDACAO.md` l.
  229-231, l. 316). O admin desabilita edição de readonly_fields como camada
  defensiva, mas isso **não** protege ORM/SQL direto.
- Permissions custom (`can_validate_lead`, `can_publish_model`,
  `can_view_validacao_dashboard`, `can_resolve_disagreement`) coladas na
  `ProcessoValidacao.Meta` — funciona, mas é um anti-pattern leve (permissions
  conceituais de um domínio inteiro penduradas num único model). Aceitável dado
  que as 4 são todas relativas ao subdomínio "validação".
- Falta a permission `can_view_motivo` referenciada em
  `REGRAS_NEGOCIO_VALIDACAO.md` l. 293. Como não há views/serializers ainda, é
  nit que pode ser corrigido junto com T11 (serializers DRF) ou agora.
- Falta `versao_modelo` em `Process.classificacao_versao` é `max_length=10` —
  consistente com `AmostraValidacao.versao_modelo`, `ProcessoValidacao.versao_modelo`,
  `ThresholdTribunal.versao_modelo`, `ClassificacaoShadowLog.versao_shadow`. OK.

## Pontos por categoria

### A. Conformidade spec (REGRAS_NEGOCIO_VALIDACAO.md)

- [OK] Estratégias de amostragem: 6 da spec + 2 estendidas (`fn_candidatos`,
  `shadow_disagree`) — `tribunals/models.py:508-525`. As 6 da tabela base
  (`top_score`, `borderline`, `low_score`, `falsos_consumidos`, `recuperados`,
  `on_demand`) estão todas presentes. Extras alinham com T5 (shadow) e mining
  de falsos negativos — não conflita com spec.
- [OK] 7 estados de resultado da spec implícita: implementados **8**
  (`tribunals/models.py:604-621`): `eh_lead`, `eh_precatorio`, `eh_pre`, `eh_dc`,
  `nao_lead`, `incerto`, `precisa_enriquecer`, `skip`. Cobertura completa.
- [PARCIAL] `UniqueConstraint(processo, usuario)` implementada
  (`tribunals/models.py:680-683`). **Garante INSERT-only**; imutabilidade
  **completa** (UPDATE-block) **não** implementada. Ver bloco E e blockers.
- [OK] LGPD: `usuario` é `SET_NULL` (`tribunals/models.py:641`), `usuario_hash`
  CharField(max_length=64, blank=True) presente (l. 646). Não há lógica de
  preenchimento do hash neste passo — é responsabilidade do comando
  `anonimizar_usuario` futuro. Schema está pronto.
- [OK] `label_final`, `label_final_resolvido_por`, `label_final_resolvido_em`
  presentes (l. 665-672) com FK `SET_NULL` no resolvedor.
- [OK] `ThresholdTribunal`: 1 ativo por `(tribunal, versao_modelo)` via
  constraint partial `condition=Q(ativo=True)` (l. 767-771). Note que **uma row
  só** existe por par `(tribunal, versao_modelo)` (constraint sem condição em
  l. 762-765) — isso significa que **não é possível** ter duas rows da mesma
  versão diferindo só em `ativo`. Decisão razoável: histórico de mudança fica
  em `AuditoriaThreshold` (futuro), não em rows extras. Documentar.
- [OK] `ClassificadorVersao.shadow`: pode haver N True simultâneos (sem
  constraint partial); apenas 1 `ativa=True` é garantido por
  `uniq_classificador_versao_ativa` (`tribunals/models.py:415-416`). Coerente
  com biz (T5 prevê múltiplas shadow paralelas).

### B. Padrões projeto (PATTERNS.md, DATA_MODEL.md)

- [OK] Naming pt-BR em `verbose_name`/`verbose_name_plural`; snake_case nos
  fields. Consistente.
- [NIT] `gettext_lazy as _` aplicado apenas nos modelos novos (`tribunals/models.py:548,
  579, 675, 729, 759`). Resto do arquivo não usa. Não é violação (PATTERNS.md
  não exige), mas é assimetria que vai chamar atenção em code review futuro.
  **Sugestão:** manter como está; se for padronizar, fazer em PR dedicado.
- [OK] FKs idiomáticas:
  - `Process` → `CASCADE` em todas (`ProcessoValidacao`, `AmostraProcesso`,
    `ClassificacaoShadowLog`) — coerente com modelo de domínio (apagar
    processo apaga validações dele).
  - `Tribunal` → `PROTECT` em `AmostraValidacao` (l. 530) e `CASCADE` em
    `ThresholdTribunal` (l. 745). **Inconsistência intencional?**
    `ThresholdTribunal` CASCADE faz sentido (config morre com tribunal), mas
    `AmostraValidacao` PROTECT também faz (lote é dataset histórico). OK,
    documentar.
  - `User` → `SET_NULL` em todas as FKs de auditoria/criação. Exceto
    `AmostraValidacao.criada_por` que é `PROTECT` (l. 535). **Inconsistência:**
    se o usuário criador do lote pedir LGPD delete, lote não pode ser
    anonimizado sem mudar para SET_NULL. **Nit** — recomendo mudar para
    `SET_NULL` em consistência com pedido LGPD. (Ou aceitar que criação de
    lote é ato administrativo não-anonimizável.)
  - `AmostraValidacao` → `SET_NULL` em `ProcessoValidacao.amostra` (l. 636) —
    permite deletar lote preservando anotações. OK.
- [OK] Indexes nos campos de filtro previsto: `resultado`, `criada_em`,
  `amostra`, `label_final`, `(processo, criada_em)`, `(usuario, criada_em)`.
  Em `AmostraValidacao`: `-criada_em` (l. 552) e `estrategia` (l. 553).
  Cobertura adequada para queries documentadas em
  `REGRAS_NEGOCIO_VALIDACAO.md` (kappa por anotador, kpis por estrategia, fila
  de divergências por label_final NULL).
- [NIT] `AmostraValidacao.criada_em` tem `db_index=True` no field (l. 538) **e**
  Index na Meta (l. 552). Duplicação — Postgres cria 2 indexes idênticos
  (B-tree). **Recomendação:** remover o `db_index=True` do field OU remover o
  index da Meta. Mesmo problema em `ClassificacaoShadowLog.criada_em`
  (`db_index=True` l. 726) — só não duplica porque o Meta indexa
  `(versao_shadow, criada_em)` composto, não `criada_em` sozinho. Verificar
  `makemigrations` não gera 2 indexes equivalentes.
- [OK] JSONField com `default=dict` em `parametros` (l. 539) e
  `features_snapshot` (l. 659). `motivos_suspeita` (l. 576) está
  `null=True, blank=True` em vez de `default=list` — defensável porque "sem
  motivos" é semanticamente diferente de "lista vazia". Aceitável.
- [OK] Sem `null=True` em CharField/TextField nos modelos novos, **exceto**
  `label_final` (l. 666). Aceitável: NULL ≠ string vazia aqui (NULL =
  "ainda não resolvido", string vazia jamais ocorre). Coerente com lógica de
  resolução de divergência.

### C. Coerência cross-app (ARCHITECTURE.md)

- [OK] `tribunals/` mantém monopólio dos models. Nada vazou.
- [OK] Migration `0024` importa apenas `django.*` e `settings.AUTH_USER_MODEL`.
  Sem cross-app imports indevidos.
- [OK] Admin importa apenas de `tribunals.models` (l. 6-25). OK.
- [OK] Command `setup_validacao_groups` importa apenas `django.contrib.auth`
  e `django.core.management`. OK.

### D. Migration safety

- [OK] Migration é **schema-only** — sem `RunPython`/`RunSQL`. Aplica em <1s
  em prod (tabelas novas, vazias).
- [OK] Permissions custom carregadas via `ProcessoValidacao.Meta.permissions`
  (l. 692-701) — gera 4 entradas em `auth_permission` no `migrate` (padrão
  Django).
- [OK] Reversibilidade: Django gera operação reversa automaticamente
  (DROP TABLE + DROP CONSTRAINT). Não testei `migrate tribunals 0023`
  manualmente — recomendo o implementador rodar.
- [OK] Dependência `tribunals.0023_proc_ultmov_classif_idx` correta.
- [OK] `AddField shadow` em `ClassificadorVersao` (l. 16-20) é
  `default=False` — backfill instantâneo (sem lock longo) mesmo se a tabela
  crescer.

### E. Issues levantadas pelo implementador

- **Trigger Postgres UPDATE-block — NÃO implementado.** O comentário em
  `tribunals/models.py:678-679` declara explicitamente
  `# Imutabilidade — biz exige sem UPDATE. Garantido também por trigger
  Postgres em migration futura (T5+).`
  Status real: trigger não existe em nenhuma migration neste branch. ADR-018
  (`REGRAS_NEGOCIO_VALIDACAO.md` l. 316) menciona ambos mecanismos como **AND**
  (`UniqueConstraint + trigger UPDATE-block`). Decisão: **aceitar gap como
  follow-up T7+ obrigatório** porque (a) UniqueConstraint já impede o caminho
  principal (re-anotação via ORM `create`), (b) views/serializers em T7-T8
  não precisarão de UPDATE em `ProcessoValidacao`, (c) admin já bloqueia
  edição/delete via `has_delete_permission=False` e readonly_fields. Não
  bloqueia. **Mas registrar como follow-up obrigatório antes de T18.**
- **Grupo `revisores_seniores`** (`setup_validacao_groups.py:58-66`): recebe
  `can_validate_lead + can_view_validacao_dashboard + can_resolve_disagreement`.
  Spec (`REGRAS_NEGOCIO_VALIDACAO.md` l. 196) diz subset estrito de
  `can_validate_lead`. **A implementação está correta:** revisor sênior também
  é validador (pode anotar fila normal + resolver divergências). "Subset
  estrito" no contexto da spec quer dizer "subgrupo dos validadores", não
  "permissions menores". OK.
- **Grupo `auditores_leads` da spec NÃO foi criado** pelo command (só os 3
  grupos: validadores, revisores_seniores, model_admins). Spec l. 194 e 201
  prevê `auditores_leads` read-only. **Nit** — pode ser criado em comando
  futuro com a permission `can_view_validacao_dashboard` (e `can_view_motivo`
  quando essa existir). Não bloqueia, mas anotar.

### F. Testes (test_validacao_models.py)

- [OK] 12 testes cobrem: criação de amostra + through M2M, unique amostra-
  processo, criação de validação, unique processo-usuario, dupla anotação por
  usuários distintos, SET_NULL preserva label, choices via full_clean,
  shadow N True, ativa duas viola, threshold unique tribunal-versao,
  threshold versão diferente OK, command idempotente.
- [GAP] **Falta teste de imutabilidade real:** depois do INSERT, fazer
  `v.resultado = 'X'; v.save()` deveria falhar (e atualmente passa, porque o
  trigger PG não existe). Quando o trigger for adicionado, esse teste vai
  documentar a invariante. **Sugestão:** adicionar agora um teste **xfail
  com motivo "trigger PG pendente"** para deixar marca no código.
- [GAP] Falta teste do hash LGPD: criar validação, deletar user, verificar que
  `usuario_hash` foi preenchido. Atualmente o hash não é preenchido por
  nenhum signal/save — é responsabilidade do comando `anonimizar_usuario`
  (não implementado neste passo). OK adiar.
- [GAP] Falta teste de `label_final` happy path: revisor sênior preenche
  `label_final`, `label_final_resolvido_por`, `label_final_resolvido_em` e
  isso persiste. Trivial mas seria cobertura útil.
- [OK] Testes são determinísticos — sem timing/ordering.
- [OK] Sem teste de migration reversa, mas isso normalmente não é feito em
  pytest-django (ferramenta seria `django-migration-linter` ou rodada manual
  em staging).

### G. Pontos finos

- [OK] `ProcessoValidacao.classificacao_no_momento` (l. 657) é
  `CharField(max_length=20)` — bate com `Process.classificacao` (l. 131).
- [OK] `AmostraProcesso.classificacao_no_sorteio` (l. 572) também
  `max_length=20`. Consistente.
- [NIT] `ClassificacaoShadowLog.criada_em` tem `db_index=True` (l. 726). O
  Meta.indexes (l. 731-733) tem `(versao_shadow, criada_em)` composto. Postgres
  usa o composto pra queries `versao_shadow=X ORDER BY criada_em` mas o index
  solo de `criada_em` é útil pro **cleanup retention 90d** (`DELETE WHERE
  criada_em < now() - 90d`). Aceitável manter.
- [OK] `verbose_name_plural` declarado em pt-BR em todos os 5 modelos novos.
- [NIT] `help_text` ausente em fields ambíguos como `versao_modelo`,
  `seed`, `parametros`, `tempo_segundos`, `usuario_hash`. Não bloqueia mas
  ajudaria admin/DRF. Sugiro adicionar em PR de polimento.
- [NIT] `AmostraValidacao` não declara `verbose_name` em fields
  individualmente — só Meta. Admin mostra "criada por", "criada em" com
  defaults Django (sem acento, "criada por" em vez de "Criada por"). Cosmético.
- [GAP arquitetural] **Modelos auxiliares da spec não implementados** (fora
  de escopo declarado T4/T5, mas listar como follow-up T7-T18):
  - `DivergenciaValidacao` (RNV l. 90, 91) — fila de divergências detectadas.
  - `AuditoriaLGPD` (RNV l. 254).
  - `AuditoriaDeploy` (RNV l. 142).
  - `AuditoriaThreshold` (RNV l. 171).
  - `AuditoriaGrupo` (RNV l. 216).
  - `AuditoriaAcesso` (RNV l. 299).
  - `RelatorioValidacao` (RNV l. 77).
  - Permission `can_view_motivo` (RNV l. 293).

## Blockers (devem ser resolvidos antes de prosseguir)

**Nenhum.** T7-T10, T16-T18 podem prosseguir com este schema.

## Nice-to-have (não bloqueia, criar follow-up)

1. **Remover duplicação de index em `AmostraValidacao.criada_em`** — escolher
   entre `db_index=True` no field OU `Index` na Meta. Pequena correção em
   models.py + makemigrations + 0025.
2. **Mudar `AmostraValidacao.criada_por` para `SET_NULL`** — para
   consistência com pedido LGPD. Pequena migration 0025.
3. **Adicionar teste xfail de UPDATE-block** — documenta a invariante até o
   trigger PG ser criado.
4. **Adicionar teste de happy-path `label_final`** — 8 linhas, alta utilidade.

## Follow-up tasks sugeridas (não criar, só listar)

- **[OBRIGATÓRIO antes de T18]** Trigger Postgres UPDATE-block em
  `ProcessoValidacao` — permite UPDATE apenas em `label_final`,
  `label_final_resolvido_por`, `label_final_resolvido_em`, `usuario`,
  `usuario_hash`, `motivo` (este último só quando redact LGPD). Migration
  `tribunals/migrations/00XX_processo_validacao_immutable_trigger.py` com
  `RunSQL` + `reverse_sql`. ADR-018 promete isso.
- **[OBRIGATÓRIO antes de T18]** Comando `anonimizar_usuario --user-id=N`
  que preenche `usuario_hash` com `sha256(username + settings.LGPD_SALT)` e
  seta `usuario=NULL`. Idempotente.
- **[Antes de T7]** Criar grupo `auditores_leads` no
  `setup_validacao_groups` (read-only sobre dashboards).
- **[Antes de T9]** Permission `can_view_motivo` — atualmente derivada de
  `can_resolve_disagreement` na spec; pode ser permission custom adicional ou
  property no serializer.
- **[Antes de T10]** Modelos auxiliares de auditoria (`AuditoriaLGPD`,
  `AuditoriaThreshold`, `AuditoriaDeploy`, `AuditoriaGrupo`, `AuditoriaAcesso`).
- **[Antes de T16]** Modelo `DivergenciaValidacao` + job
  `marcar_divergencias` que detecta dupla-anotação divergente.
- **[Decisão pendente p/ biz]** Política de interseção `model_admins ∩
  validadores_leads` (RNV l. 218-221) — `clean()` em admin pra proibir? Ou
  registrar em `AuditoriaGrupo` e deixar policy social? `[QUESTION p/ biz]`
- **[Decisão pendente p/ biz]** Texto do aviso de privacidade no convite/
  onboarding de validador (RNV l. 260-262). `[QUESTION p/ biz]`

## Decisão final

**APPROVE WITH NITS.** T7, T8, T9, T10, T16, T17, T18 desbloqueadas. Nits
ficam como follow-up. Antes de T18 (publicação externa do dataset/Juriscope)
**é obrigatório** fechar o gap do trigger PG e o comando `anonimizar_usuario`
— sem isso, a promessa de imutabilidade no ADR-018 não está cumprida.

## 3 issues mais importantes (priorizadas)

1. **Trigger PG UPDATE-block ausente.** Schema viola promessa do ADR-018 de
   imutabilidade completa. Não bloqueia desenvolvimento mas vira blocker em
   T18.
2. **`AmostraValidacao.criada_por` é `PROTECT`** — quebra fluxo LGPD se
   criador do lote pedir delete. Mudar para `SET_NULL`.
3. **Index duplicado em `AmostraValidacao.criada_em`** (`db_index=True` + Meta
   Index) — desperdício de disco/escrita. Resolver na próxima migration.
