# Regras de Negócio — Validação humana de classificação de leads

Documento de **decisões fechadas** que governam o sistema de validação humana
sobre a saída do classificador v6 (e gate de promoção para v7).

Escopo: política de amostragem, re-anotação, gate de deploy, thresholds por
tribunal, RBAC, retenção e auditoria. LGPD/anonimização foi declarada fora de
escopo nesta versão (ver §6). Implementação fica em arquivos próprios
(`models`, `views`, `services`); aqui só especificação.

Referências cruzadas: [`CLASSIFICACAO.md`](CLASSIFICACAO.md),
[`ACCOUNTS.md`](ACCOUNTS.md), [`DECISIONS.md`](DECISIONS.md).

---

## 1. Política de amostragem por estrato

Amostragem **estratificada por (tribunal × bucket_score × origem)** para evitar
viés do top-of-the-list e capturar erros sistemáticos em buckets de baixo score.

### Estratos e tamanhos-alvo

| Estratégia | Estrato | Tamanho alvo / lote | Frequência | Trigger de refresh |
|---|---|---|---|---|
| `top_score` | score ≥ 0.7, **não validado** | 100/lote/tribunal | semanal | cron seg 02:00 |
| `borderline` | 0.30 ≤ score < 0.70 | 100/lote/tribunal | semanal | cron seg 02:00 |
| `low_score` | 0.05 ≤ score < 0.30 | 50/lote/tribunal | semanal | cron seg 02:00 |
| `falsos_consumidos` | `LeadConsumption.resultado IN {sem_expedicao, erro}` | 50/lote/tribunal | semanal | cron seg 02:00 |
| `recuperados` | classificados após backfill recente (últimos 7d) | 50/lote/tribunal | semanal | cron seg 02:00 |
| `on_demand` | seleção manual por CNJ | livre | manual | gatilho UI |

**Decisão:** lotes **semanais** via cron, com tamanho padrão **300 por tribunal**
(soma das 5 estratégias automáticas). On-demand é canal paralelo, não conta
contra a cota semanal.

**Por quê:** semanal balanceia carga humana (~5h/tribunal/semana a 1 min/CNJ) com
ciclo de feedback curto o bastante pra detectar drift entre re-treinos. Por
estrato garante que features dominantes (F15 volume, F1 cumprimento) não
mascarem falhas em buckets raros.

**Como aplicar/medir:**
- Cron `gerar_amostra_validacao` cria `LoteValidacao(estrategia, tribunal,
  semana_iso, processos=[...])` toda segunda 02:00.
- Trigger manual: comando `gerar_amostra_validacao --estrategia=X --tribunal=Y
  --tamanho=N` para casos pontuais (ex.: pós-deploy).
- Métrica de saúde: `coverage_estrato_semana_iso` ≥ 80% (alerta se <80%).
- Exclui CNJs já em `ProcessoValidacao` na mesma semana ISO (anti-duplicação).

### Seed e reprodutibilidade

- Sorteio com `random.Random(seed=hash(semana_iso + tribunal + estrategia))` —
  determinístico, auditável, regenerável.
- `LoteValidacao.seed` persistido pra auditoria.

---

## 2. Política de re-anotação

### Pode anotar 2x?

**Decisão:** Não. Cada `(processo, validador)` é **único** (constraint DB).
Re-anotação **só** acontece sob duas condições:

1. **Dupla-anotação amostral** — 10% de cada lote é deliberadamente
   atribuído a 2 validadores distintos para medir Cohen's kappa.
2. **Revisão de divergência** — quando dupla-anotação diverge, um terceiro
   (revisor sênior) decide e a label final fica em `ProcessoValidacao.label_final`.

**Por quê:** imutabilidade da label individual preserva sinal de viés de
anotador. Sobrescrever destrói histórico necessário para kappa e ponderação no
treino v7.

**Como aplicar/medir:**
- Constraint: `UniqueConstraint(processo, usuario)` em `ProcessoValidacao`.
- `LoteValidacao.dupla_anotacao_pct = 0.10` (10%).
- Sorteio de duplas: subsample aleatório no `gerar_amostra_validacao`, atribuído
  a 2 usuários diferentes do grupo `validadores_leads`.
- Kappa calculado em `RelatorioValidacao` por lote/tribunal — alvo **κ ≥ 0.7**
  (substancial). Abaixo de 0.6 = revisar guideline de anotação.

### Quem decide divergência?

**Decisão:** revisor **sênior** (usuário com permission `can_resolve_disagreement`,
subset estrito de `can_validate_lead`).

**Por quê:** evita loop infinito de divergência. Sênior fecha a decisão num
prazo curto.

**Como aplicar/medir:**
- Divergência detectada por job diário `marcar_divergencias` — cria
  `DivergenciaValidacao(processo, anotacoes=[ann1, ann2], status='aberta')`.
- SLA: revisor decide em ≤ 7 dias. Alerta no dashboard se backlog > 50.
- Label final grava `ProcessoValidacao.label_final` + `resolvido_por` + `resolvido_em`.

### Quem amostra os 10%?

**Decisão:** o próprio job `gerar_amostra_validacao` faz o subsample,
**não** depende de ação humana.

**Por quê:** automação remove viés de "quem sabe que será dupla-anotado".

---

## 3. Critérios de aceitação (gate v7)

Modelo candidato (v7) só vai pra produção se passar nos **6 critérios** abaixo,
todos avaliados contra o **conjunto de validação humana acumulado** + holdout
histórico v5/v6.

### Tabela de critérios

| # | Critério | Métrica | Pass | Warn (force deploy permitido) | Block |
|---|---|---|---|---|---|
| C1 | AUC global | ROC-AUC TRF1+TRF3 | ≥ 0.960 | 0.945–0.960 | < 0.945 |
| C2 | precision@5000 | TRF1 top-5k | ≥ 0.985 | 0.97–0.985 | < 0.97 |
| C3 | recall@FN_candidatos | em `falsos_consumidos_1327` recuperados | ≥ 0.40 | 0.25–0.40 | < 0.25 |
| C4 | AUC TRF3 | ROC-AUC só TRF3 | ≥ 0.90 | 0.85–0.90 | < 0.85 |
| C5 | calibração | ECE (Expected Calibration Error, 10 bins) | ≤ 0.05 | 0.05–0.08 | > 0.08 |
| C6 | sem regressão | `falsos_consumidos_1327` score médio | ≤ score_v6 + 0.02 | até +0.05 | > +0.05 |

### Política

- **Pass (6/6 verdes):** deploy automático (após aprovação de `can_publish_model`).
- **Warn em 1+ e Block em 0:** **force deploy** permitido com:
  - Justificativa textual obrigatória em `ClassificadorVersao.notas_deploy`.
  - Aprovação dupla: 2 usuários distintos com `can_publish_model`.
  - Plano de rollback documentado (versão alvo, condição de trigger).
- **Block em qualquer critério:** deploy **proibido** pela UI. Override requer
  edição manual no shell + entrada no `AuditoriaDeploy`.

**Por quê:** C1+C2 são proxies de produção (Juriscope consome top-5k). C3 mede se
o modelo aprende com os falsos positivos consumidos. C4 garante que TRF3 não
regrediu (era ponto fraco do v5). C5 protege a categorização N1/N2/N3 que
depende de thresholds calibrados. C6 evita over-fitting ao conjunto de
validação humana às custas de generalização.

**Como aplicar/medir:**
- View `/dashboard/validacao/gate/<versao>/` mostra os 6 critérios com
  semáforo verde/amarelo/vermelho.
- Comando `gerar_gate_relatorio --versao=v7` produz JSON em
  `ClassificadorVersao.relatorio_gate`.
- Trilha de auditoria: cada force deploy gera `AuditoriaDeploy(versao,
  aprovadores=[u1, u2], notas, criterios_warn)`.

---

## 4. Thresholds por tribunal

### Configuração

**Decisão:** thresholds **no DB** em tabela `ThresholdTribunal`, **não**
hardcoded. Fallback para defaults em código se row não existir.

| Tribunal | Threshold Precatório (N1) | Threshold Pré (N2) | Threshold DC (N3) |
|---|---|---|---|
| TRF1 | 0.70 | 0.40 | 0.20 |
| TRF3 | 0.65 | 0.35 | 0.20 |
| TJMG | 0.75 | 0.45 | 0.25 |
| TJSP | 0.75 | 0.45 | 0.25 |
| _default_ (fallback) | 0.70 | 0.40 | 0.20 |

**Por quê:** TRF3 tem distribuição com mais cumprimentos (15% taxa de lead vs 2%
TRF1) — threshold menor mantém precision. TJMG/TJSP têm menos cobertura via
DJEN — threshold maior protege precisão até consolidar ground truth.

**Como aplicar/medir:**
- Mudança requer aprovação de usuário com `can_publish_model` (mesma permission
  do deploy). UI bloqueia se faltar permission.
- Cadência de revisão: **trimestral**, ou imediatamente após novo deploy de
  modelo (cada `ClassificadorVersao` ativada dispara recálculo sugerido via job
  `sugerir_thresholds`).
- Cada alteração registra `AuditoriaThreshold(tribunal, valor_antigo,
  valor_novo, alterado_por, alterado_em, motivo)`.
- Job `sugerir_thresholds` roda sobre `ProcessoValidacao` por tribunal e sugere
  valor que maximiza F0.5 (privilegia precision).

### Quem aprova mudança?

**Decisão:** **dupla aprovação** — proponente + segundo usuário com
`can_publish_model`. Sem aprovação dupla, mudança fica em status `proposta`.

**Por quê:** thresholds afetam diretamente a fila Juriscope (capacidade ~5k/dia).
Erro humano custa caro (deslocamento da fila por dias).

---

## 5. RBAC

### Permissions custom

| Permission | Concedida a | Pode |
|---|---|---|
| `can_validate_lead` | grupo `validadores_leads` | Acessar fila de validação, anotar `ProcessoValidacao` |
| `can_publish_model` | grupo `model_admins` | Ativar `ClassificadorVersao`, editar `ThresholdTribunal`, force deploy |
| `can_view_validacao_dashboard` | grupos `validadores_leads` + `model_admins` + `auditores_leads` | Ver `/dashboard/validacao/` (KPIs, kappa, relatórios) |
| `can_resolve_disagreement` | subset de `validadores_leads` (sêniores) | Resolver `DivergenciaValidacao` |

### Grupos Django

- `validadores_leads` — equipe interna anotadora.
- `model_admins` — quem promove modelo / muda threshold.
- `auditores_leads` — read-only sobre dashboards de validação.

**Decisão:** entrada no grupo `validadores_leads` requer **aprovação de
superuser** (mesmo fluxo de `accounts.Invite`, ver [`ACCOUNTS.md`](ACCOUNTS.md)).
Convite carrega `invite.grupos_alvo = ['validadores_leads']` que é aplicado
no `accept_invite`.

**Por quê:** reaproveita o fluxo existente de convites com auditoria IP+UA, evita
criar segundo caminho de admissão. Superuser já é o gate atual de acesso ao
sistema.

**Como aplicar/medir:**
- Migration cria os 3 grupos no `accounts/migrations` via data migration.
- Decoradores `@permission_required('tribunals.can_validate_lead')` nas views.
- Auditoria de adição/remoção de membro: signal `m2m_changed` em
  `User.groups` grava `AuditoriaGrupo(user, grupo, acao, alterado_por, em)`.

**[DECISÃO PENDENTE: política interna sobre se membros de `model_admins`
podem também estar em `validadores_leads`. Risco de conflito de interesse —
quem treina o modelo não deveria anotar seu próprio dataset de gate. Sugestão:
proibir interseção via clean() em form de admin.]**

> Status (T22, 2026-05-12): segue pendente. Sem decisão durante a
> implementação T6→T20. Hoje nada impede um usuário estar nos dois grupos.
> Levantado também em [`V7_DEPLOY_DECISION.md`](V7_DEPLOY_DECISION.md)
> seção "Decisões pendentes do biz/jurídico".

---

## 6. Retenção

### Imutabilidade

`ProcessoValidacao` é **append-only**. UPDATE bloqueado por trigger Postgres
(futuro) exceto em campos de resolução de divergência (`label_final`,
`resolvido_por`, `resolvido_em`) que só `can_resolve_disagreement` pode tocar.

### LGPD / Anonimização

**Status (2026-05-12): fora de escopo desta versão.**

Decisão do produto: ignorar LGPD/anonimização neste ciclo. Todos os anotadores
hoje são funcionários internos sob NDA padrão da empresa; não há sujeito de
dados externo na fila de validação. Quando a feature for aberta a validadores
externos, retomar este bloco.

Implicações no schema (mantidas pra reabilitação futura):

- `ProcessoValidacao.usuario` é `SET_NULL` no delete do User (já protege contra
  perda de label se o User for deletado por motivo administrativo — turnover,
  conta apagada).
- `ProcessoValidacao.usuario_hash` permanece no schema, mas **não é populado**
  nesta versão. Reativar futuramente requer: setting com salt + popular no
  save + comando `anonimizar_usuario`.
- `ProcessoValidacao.motivo_visivel_para(user)` continua como controle RBAC
  (permission `can_view_motivo`) — desacoplado de LGPD, é gating de campo
  sensível entre anotadores.

---

## 7. Política de auditoria

### Quem vê o quê?

| Papel | Vê labels próprias | Vê labels de outros | Vê `motivo` (texto livre) | Vê kappa por anotador |
|---|---|---|---|---|
| validador (`can_validate_lead`) | sim | **não** (até resolução de divergência) | só própria | não |
| revisor sênior (`can_resolve_disagreement`) | sim | sim (após divergência detectada) | sim | não |
| `model_admins` | n/a | sim (agregado) | **não** (só agregado) | sim |
| `auditores_leads` | n/a | sim | sim | sim |
| superuser | tudo | tudo | tudo | tudo |

**Por quê:** anotador não-cego enviesa anotação (efeito âncora). Revisor precisa
ver as 2 labels pra arbitrar. Model admin não precisa de texto livre (pode
conter PII de processos ou opinião pessoal).

### `motivo` é confidencial?

**Decisão:** **confidencial dentro da equipe** — visível só pra autor,
revisores e auditores. **Nunca exposto via API externa** (Juriscope não vê).
Anonimizado em export CSV para análise externa.

**Por quê:** texto livre frequentemente contém raciocínio interno ("achei
estranho que o relator é o mesmo do caso XYZ") que não deve sair da equipe.

**Como aplicar/medir:**
- Decoradores em DRF serializers excluem `motivo` por default; só inclui se
  request.user tem `can_view_motivo` (permission derivada).
- Export CSV via comando `exportar_validacoes` exclui `motivo` exceto com flag
  `--include-motivo` (uso restrito a auditores).

### Log de acesso

Toda visualização de label de **outro anotador** grava `AuditoriaAcesso(user,
processo_validacao_id, em)`. Permite detectar abuso (ex.: model admin lendo
labels individuais sem justificativa).

---

## 8. ADR-018 — Validação humana imutável (append-only)

**Contexto:** Sistema de classificação ML (v6, AUC 0.961) precisa de pipeline de
validação humana para gerar ground truth de v7 e medir precision real em
tribunais sem lista Juriscope (TRF3, TJMG, TJSP). Tensão entre 2 eixos:
(a) imutabilidade necessária pra confiar nos labels como dataset de treino,
(b) prevenção de viés de re-anotação.

**Decisão:**
- `ProcessoValidacao` é append-only (`UniqueConstraint(processo, usuario)` +
  trigger UPDATE-block planejado).
- Re-anotação proibida; divergência resolvida por revisor sênior em campo
  separado (`label_final`).
- 10% dupla-anotação automática para Cohen's kappa.
- `motivo` (texto livre) confidencial intra-equipe via `can_view_motivo` —
  nunca em API externa.
- **Anonimização LGPD fora de escopo desta versão.** Campo `usuario_hash` no
  schema mas não populado; reativar quando abrir validação a anotadores externos.

**Alternativas rejeitadas:**
- *Permitir UPDATE da label* — destrói série temporal necessária pra detectar
  drift de anotador e calcular kappa.
- *Re-anotação livre* — efeito âncora documentado na literatura de annotation
  research (anotador relê e tende a "concordar consigo mesmo" mesmo errado).

**Consequência:**
- Dataset de validação cresce monotonicamente; recálculos do gate são
  reprodutíveis dado um snapshot temporal.
- Kappa por anotador permite filtrar/ponderar fontes ruins no treino v7
  (peso amostral 1.0 a 3.0 conforme já alinhado).
- Trade-off: anotações ruins (anotador com kappa baixo) não podem ser
  removidas, só ponderadas — aceito porque é mais transparente.
