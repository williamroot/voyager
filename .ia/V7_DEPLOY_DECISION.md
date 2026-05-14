# V7 Deploy Decision — Procedimento + Sign-off

**Status:** ⏸ AGUARDANDO RUN REAL EM PROD-LIKE
**Aprovado por:** [DECISÃO PENDENTE — preencher após rodar em prod]
**Data:** [DATA REAL — preencher]
**Versão atual em produção:** v6 (TRF1, AUC=0.9610, prec@5000=0.991 — commit 6cdfff6)
**Versão candidata:** v7 (24 features, weighted LR, thresholds DB-driven, 6 gates)

---

## Visão geral

O v6 foi treinado apenas sobre TRF1 (887k procs, AUC 0.9610) e está em
produção desde o commit 6cdfff6. Ele já classifica TRF3, TJMG e TJSP
aplicando os mesmos pesos, mas sem ground truth desses tribunais a
precision real é desconhecida — o pipeline depende do `LeadConsumption`
do Juriscope para feedback assíncrono.

O v7 (T18) acrescenta cinco features novas (F24 RPV expedida, F25
pagamento administrativo, F26 inscrição em ordem cronológica, F27
trânsito julgado, F28 líquido e certo) e troca a regressão logística
não-ponderada por uma versão com `sample_weight` por origem do label
(humano=3.0, Juriscope=2.0, CSV reforçado=2.0, CSV base=1.0 — política
em [REGRAS_NEGOCIO_VALIDACAO §3](REGRAS_NEGOCIO_VALIDACAO.md)).
A política de threshold passa a ser DB-driven (`ThresholdTribunal` por
tribunal × versão_modelo), grid-search no holdout otimiza
`precision@500`, e split conformal produz delta global de incerteza.

A promoção do v7 só acontece após passar nos **6 gates formais**
(`REGRAS_NEGOCIO_VALIDACAO §3`) avaliados no holdout real de TRF1+TRF3
mais a regressão sobre `leads_trf1_falsos_consumidos_1327.csv`. A T19
implementou shadow logging (`ClassificacaoShadowLog`) e a comparação A/B
diária (`comparar_shadow`) — só com 7 dias de shadow + sign-off humano
nos disagreements o flip de `ativa=True` é autorizado. Hot reload (T17)
garante propagação em ≤ 60s após troca via DB, sem restart de worker.

O risco de **agreement-rate v6↔v7 artificialmente baixo** por drift de
política de threshold foi **resolvido**: `classificar()` agora delega
para `_categorizar()`, mesma função usada pelo path shadow. Ambos
leem `ThresholdTribunal` do DB filtrando por `versao_modelo`. A
comparação shadow agora isola variação de pesos, não de política.

O segundo risco operacional é a **base de validação humana ainda
nascente**. O pipeline T6/T11/T15 está em produção, mas até produzir
volume suficiente (alvo: ≥ 500 labels humanos por tribunal antes do
treino) o peso 3.0 contribui pouco, e o gate `recall@FN_candidatos`
depende de o mining (T16) ter rodado em TRF1 e TRF3 com volume real.

Esta decisão **não é deploy hoje**. É a definição do procedimento que,
quando executado, vai produzir o sign-off final.

---

## Pré-requisitos antes de rodar treino real

- [x] **Imagem `web` reconstruída em 2026-05-14** com numpy via `requirements.txt`.
  - Deploy do código (4c25b7c) concluído nos 2 hosts.
  - `.32`: web + scheduler + worker_manual + worker_classificacao + 4 drainers + nginx + cloudflared.
  - `.36`: worker_ingestion + worker_default + worker_djen_audit + worker_trf1 + worker_trf3 + worker_tjmg + worker_datajud + worker_classificacao.
  - Migrations 0024-0027 aplicadas. ClassificadorVersao(versao=v6, ativa=True) seedada por 0026.
  - Hot reload validado: `classifier reloaded: hardcoded -> v6` logado em worker_ingestion.
  - `setup_validacao_groups` rodado: 4 grupos criados (`validadores_leads`, `revisores_seniores`, `auditores_leads`, `model_admins`).
  - Liveness https://voyager.was.dev.br/api/v1/health/liveness/ → 200.
- [ ] Mining TRF1 + TRF3 rodado pelo menos 1x → CSV de FN candidatos
      disponível em `/app/data/fn_candidatos_*.csv`.
- [ ] `exportar_labels_retreino` rodado em prod → CSV em
      `/app/data/labels_retreino_*.csv` com coluna `processo_id`
      preenchida (em dev fica vazia porque o DB não tem os procs
      TRF1/TRF3 — não tente treinar localmente).
- [ ] Suíte de testes passa em prod-like (não só dev local):
      `docker compose exec web python manage.py test tests.test_treinar_v7
      tests.test_shadow_mode tests.test_classificador_reload
      tests.test_export_labels tests.test_minerar_fn`.
- [ ] Backup do `tribunals_classificadorversao`:
  ```bash
  ssh ubuntu@192.168.1.32 \
    "docker compose -f ~/voyager/docker-compose-prod.yml exec -T postgres \
       pg_dump -U voyager -t tribunals_classificadorversao \
       -t tribunals_thresholdtribunal voyager \
       | gzip > ~/backups/classificadorversao_pre_v7_$(date +%F).sql.gz"
  ```
- [x] ~~Fix das 2 issues médias do REVIEW_T20_ml ANTES do flip~~ **RESOLVIDO**:
  - Issue #1: `classificar()` agora delega a `_categorizar()` (DB-driven).
    Path ativo e shadow usam a mesma lógica de threshold.
  - Issue #2: `_categorizar()` filtra `ThresholdTribunal` por
    `versao_modelo` (default = versão ativa via `get_versao_ativa()`).
- [x] Settings `SHADOW_SAMPLE_RATE`, `CLASSIFICADOR_RELOAD_TTL`,
      `VALIDACAO_LOTES_SEMANAIS_ENABLED` documentadas em `.ia/OPS.md`
      e `.ia/CLASSIFICACAO.md`.
- [x] Em prod (2026-05-14), `VALIDACAO_LOTES_SEMANAIS_ENABLED=False`
      seteado no `.env` de `.32` e `.36` pré-deploy. Cron semanal
      desativado até autorização biz pra abrir validação. Reativar com
      `VALIDACAO_LOTES_SEMANAIS_ENABLED=True` + restart do `scheduler`.

---

## Procedimento de validação (passo a passo)

### Passo 1: Mineração de FN candidatos

Roda em prod no host principal. ~30-60min por tribunal com 5000
candidatos cada (depende do volume de NAO_LEAD presente).

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py minerar_fn --tribunal TRF1 --limit 5000 \
     --output /app/data/fn_candidatos_TRF1_v22.csv"

ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py minerar_fn --tribunal TRF3 --limit 5000 \
     --output /app/data/fn_candidatos_TRF3_v22.csv"
```

Saída esperada: dois CSVs com colunas `cnj,tribunal,suspeita_score,E1..E6,
motivos`. Cada um com até 5000 linhas ordenadas por `suspeita_score`
desc. Relatório `.ia/MINING_FN_v1_*.md` é regenerado.

Validar:
- [ ] Distribuição de `suspeita_score` cobre [0.10, 0.90] (não
      concentrada em [0.20, 0.30] — sinal de mining funcional).
- [ ] ≥ 60% dos top 500 têm `E2 > 0` (texto sinaliza expedição) ou
      `E4 > 0` (similaridade com recuperados_1327).

**Consolidar em um único CSV (input do v7):**
```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   bash -c 'cat /app/data/fn_candidatos_TRF1_v22.csv > /app/data/fn_candidatos_v22.csv && \
            tail -n +2 /app/data/fn_candidatos_TRF3_v22.csv >> /app/data/fn_candidatos_v22.csv'"
```

### Passo 2: Export labels consolidado

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py exportar_labels_retreino \
     --min-data 2024-01-01 \
     --output /app/data/labels_retreino_v22.csv"
```

Saída esperada: 1 CSV consolidado (~400-450k linhas em prod, dependendo
de quantos labels humanos existirem). Cabeçalho:
`cnj,tribunal,label,peso,fonte,conflito_flag,processo_id`.

Validar:
- [ ] `processo_id` preenchido em ≥ 95% das linhas (resto fica fora
      do treino).
- [ ] Distribuição de `peso`: maioria 1.0 (CSV base), parcela
      crescente em 2.0 (Juriscope+csv_reforcado) e 3.0 (humano).
- [ ] `conflito_flag=True` em < 1% (acima sinaliza divergência
      sistemática entre fontes).
- [ ] Pelo menos `N_humano ≥ 200` em cada tribunal alvo (TRF1, TRF3).

### Passo 3: Treino v7 SHADOW (não-deploy)

Treino completo: ~30-60min em prod com 400-800k procs.

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py treinar_classificador_v7 \
     --ground-truth-csv /app/data/labels_retreino_v22.csv \
     --fn-candidates-csv /app/data/fn_candidatos_v22.csv \
     --shadow \
     --output-dir /app/data/v7/ \
     --epochs 400 --seed 42"
```

Persiste como `ClassificadorVersao(versao='v7', ativa=False, shadow=True)`
e gera artefatos em `/app/data/v7/`:
- `V7_TRAINING_REPORT_<ts>.md` — relatório principal humano-legível.
- `v7_metrics_<ts>.json` — métricas estruturadas.
- `v7_pesos_<ts>.json` — pesos do modelo (24 features + intercept).
- `v7_threshold_grid_<ts>.csv` — grid de thresholds por tribunal/nível.

### Passo 4: Validar 6 gates manualmente

Inspecionar `V7_TRAINING_REPORT_<ts>.md` na seção "Gates de aceitação".
Cada gate tem status `PASS` / `WARN` / `BLOCK` (ou `NO_DATA` se a
métrica não pôde ser calculada — não bloqueia, mas exige justificativa
no sign-off).

**Checklist (preencher com valor real do relatório):**

- [ ] C1 — AUC global ≥ 0.960 PASS / 0.955-0.960 WARN / < 0.955 BLOCK
  - Spec: `REGRAS_NEGOCIO_VALIDACAO §3 C1`.
  - Implementação: `treinar_classificador_v7.py::_auc` (Mann-Whitney
    via trapezoidal manual, numpy puro).
- [ ] C2 — precision@5000 ≥ 0.985 PASS / 0.970-0.985 WARN / < 0.970 BLOCK
  - Spec: `REGRAS_NEGOCIO_VALIDACAO §3 C2`.
- [ ] C3 — recall@FN_candidatos ≥ 0.40 PASS / 0.20-0.40 WARN / < 0.20 BLOCK
  - Spec: `REGRAS_NEGOCIO_VALIDACAO §3 C3`.
  - Implementação: `_calcular_recall_fn` carrega top 5000 do
    `fn_candidates_csv`, scoreia com pesos v7, conta quantos score ≥ 0.20.
- [ ] C4 — AUC TRF3 ≥ 0.90 PASS / 0.85-0.90 WARN / < 0.85 BLOCK
  - Spec: `REGRAS_NEGOCIO_VALIDACAO §3 C4`.
- [ ] C5 — ECE ≤ 0.05 PASS / 0.05-0.08 WARN / > 0.08 BLOCK
  - Spec: `REGRAS_NEGOCIO_VALIDACAO §3 C5`.
  - Implementação: `_ece` (10-bin, `|conf - acc|` ponderado por
    tamanho do bin).
- [ ] C6 — Regressão FP: 0 procs com score ≥ 0.3 em
      `leads_trf1_falsos_consumidos_1327` PASS / ≤ 10% WARN / > 10% BLOCK
  - Spec: `REGRAS_NEGOCIO_VALIDACAO §3 C6`.
  - Implementação: `_calcular_regressao_falsos` (cutoff hardcoded 0.3).

**Política de aceitação:**

- 6/6 PASS → liberação automática (mas sempre exige aprovação dupla
  documentada no sign-off abaixo).
- ≥ 1 WARN e 0 BLOCK → `--force` permitido, MAS exige:
  - Justificativa textual em `ClassificadorVersao.notas_deploy`.
  - Aprovação dupla (2 usuários com `can_publish_model`).
  - Plano de rollback documentado neste arquivo.
- Qualquer BLOCK → deploy proibido. Override só com edição manual no
  shell + `AuditoriaDeploy`.

### Passo 5: Shadow mode 7 dias

v7 já está como `shadow=True` (Passo 3). Configurar prod:

```bash
# settings já tem default SHADOW_SAMPLE_RATE=0.10. Para acelerar
# coleta nos primeiros 7 dias, subir pra 0.30:
ssh ubuntu@192.168.1.32 "cd ~/voyager && \
  grep -q '^SHADOW_SAMPLE_RATE' .env || echo 'SHADOW_SAMPLE_RATE=0.30' >> .env"

# Reiniciar workers (apenas para pickar nova env).
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml \
   up -d --force-recreate worker_classificacao"
```

**Confirmar que shadow está rodando:**
```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.models import ClassificacaoShadowLog
from django.utils import timezone
from datetime import timedelta
since = timezone.now() - timedelta(hours=1)
n = ClassificacaoShadowLog.objects.filter(criada_em__gte=since).count()
print(f'shadow logs ultima 1h: {n}')
\""
```

Esperado: ≥ 50 logs/h em prod sob carga normal de classificação.

**Aguardar 7 dias.** O job `comparar_shadow_daily` (cron 04:00 UTC) já
escreve `.ia/SHADOW_COMPARISON_<data>.md` automaticamente. Para rodar
manualmente antes de 7 dias completos:

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.jobs import comparar_shadow
print(comparar_shadow('v6', 'v7', dias=7))
\""
```

Inspecionar `.ia/SHADOW_COMPARISON_*.md`:

- [ ] Agreement rate v6 vs v7 ≥ 0.80 (modelo similar; abaixo de 0.60
      sinaliza mudança grande — exige validação humana extra).
- [ ] KS statistic < 0.15 (distribuições próximas).
- [ ] |delta_med| ≤ 0.02 (mudança média de score baixa).
- [ ] Inspecionar **top 50 disagreements**: amostrar 10
      manualmente e verificar se o v7 está certo no caso.

Faixas operacionais (do REVIEW_T20):
- **agreement ≥ 0.95 + |delta_med| ≤ 0.02** → refinamento incremental,
  go-no-go fácil.
- **0.85 ≤ agreement < 0.95 + ks ≤ 0.10** → mudança significativa em
  fronteira. Inspecionar top_disagreements; consistente com regras
  → liberar.
- **agreement < 0.85 ou ks > 0.20** → mudança grande, exigir Passo 6
  obrigatoriamente antes do flip.

### Passo 6: Validação humana dos disagreements (50 CNJs)

Gerar lote dedicado com os CNJs em disagreement (top 50 do shadow
report), enviar para anotação por revisor sênior:

```bash
# 1. Extrair top 50 CNJs do SHADOW_COMPARISON em CSV.
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
import json
from pathlib import Path
from datetime import date
from tribunals.jobs import comparar_shadow
stats = comparar_shadow('v6', 'v7', dias=7)
# top_disagreements vem no stats — re-rodar para extrair lista detalhada
\""

# 2. Criar lote on_demand via UI ou comando.
#    Estratégia 'on_demand' aceita lista de CNJs vinda de
#    parametros.cnj_list (T11 já suporta).
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py gerar_amostra_validacao \
     --estrategia on_demand --tribunal TRF1 --tamanho 50 \
     --csv-path /app/data/v7_disagreements_top50.csv"
```

(Adaptar comando conforme assinatura real — se a estratégia
`shadow_disagree` ainda não existir como first-class, criar lote
on_demand passando os CNJs via `parametros_json={"cnj_list": [...]}`.)

Após anotação por revisor sênior (1-2 dias), calcular precision sobre
os disagreements: dos 50, em quantos o v7 acertou e o v6 errou?

- **≥ 70% de "v7 correto"** → v7 melhora real, libera flip.
- **50-70%** → v7 marginalmente melhor, exige WARN justification.
- **< 50%** → v7 piora; **abortar deploy**.

### Passo 7: Sign-off final

| Gate | Threshold | Resultado | Status |
|---|---|---|---|
| C1 AUC global | ≥ 0.960 | [valor] | PASS/WARN/BLOCK |
| C2 prec@5000 | ≥ 0.985 | [valor] | PASS/WARN/BLOCK |
| C3 recall@FN | ≥ 0.40 | [valor] | PASS/WARN/BLOCK |
| C4 AUC TRF3 | ≥ 0.90 | [valor] | PASS/WARN/BLOCK |
| C5 ECE | ≤ 0.05 | [valor] | PASS/WARN/BLOCK |
| C6 Regressão FP | 0 com score ≥0.3 | [valor] | PASS/WARN/BLOCK |
| Shadow agreement (7d) | ≥ 0.80 | [valor] | INFO |
| KS statistic | < 0.15 | [valor] | INFO |
| Precision em disagreements | ≥ 70% | [valor] | INFO |

**Decisão (marcar uma):**

- [ ] APROVAR DEPLOY: nenhum BLOCK, máximo 1 WARN justificado,
      agreement ≥ 0.85, disagreement precision ≥ 70%.
- [ ] APROVAR COM --force: ≥ 1 WARN com justificativa textual abaixo +
      disagreement precision ≥ 70%.
- [ ] REJEITAR: qualquer BLOCK presente OU agreement < 0.60 OU
      disagreement precision < 50%.

**Justificativa (obrigatória se --force):**
```
[texto]
```

**Aprovadores:**
- Assinatura biz (`model_admins` #1): _____________ Data: ___
- Assinatura ml  (`model_admins` #2): _____________ Data: ___

Salvar este documento atualizado em git + popular
`ClassificadorVersao.notas_deploy` com link para o commit.

---

## Comando de deploy

Após sign-off, **com a v7 já persistida como shadow**, o flip é:

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py treinar_classificador_v7 \
     --ground-truth-csv /app/data/labels_retreino_v22.csv \
     --fn-candidates-csv /app/data/fn_candidatos_v22.csv \
     --deploy \
     --seed 42 --epochs 400 \
     $( [ \"\$HAS_WARN\" = '1' ] && echo --force )"
```

Notas:
- O comando re-treina (idêntico ao Passo 3 graças à seed fixa) e só
  então persiste como `ativa=True, shadow=False`.
- `update_or_create(versao='v7')` desativa as outras versões ativas
  numa transação (constraint partial garante 1 ativa).
- `_persistir_thresholds` cria/atualiza `ThresholdTribunal` para TRF1,
  TRF3, TJMG, TJSP × versao_modelo='v7'.

**Efeitos após sucesso:**
- `ClassificadorVersao(versao='v6', ativa=False)`.
- `ClassificadorVersao(versao='v7', ativa=True)`.
- `ThresholdTribunal` por tribunal × versao='v7' criado.
- Workers `worker_classificacao` detectam via hot reload em até
  `CLASSIFICADOR_RELOAD_TTL=60s` (default settings).
- Próximas classificações usam v7. Não é necessário restart.

**Validar pós-deploy (primeiras 24h):**
```bash
# 1. Confirma versão ativa
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.models import ClassificadorVersao
for cv in ClassificadorVersao.objects.all().order_by('-criada_em'):
    print(f'{cv.versao} ativa={cv.ativa} shadow={cv.shadow} criada={cv.criada_em}')
\""

# 2. Sanity: amostra de processos classificados nos últimos 10min.
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.models import ClassificacaoLog
from django.utils import timezone
from datetime import timedelta
since = timezone.now() - timedelta(minutes=10)
qs = ClassificacaoLog.objects.filter(criada_em__gte=since)
print(f'classificacoes ultimos 10min: {qs.count()}')
print(f'versao distinta: {set(qs.values_list(\\\"versao_modelo\\\", flat=True))}')
\""

# 3. Conferir dashboard
# /dashboard/leads/ → KPIs e charts
# /dashboard/leads/validacao/ → versão ativa exibida no header
# /dashboard/api/ → versão e métricas do modelo ativo
```

---

## Rollback (se algo der errado em até 24h)

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.models import ClassificadorVersao
from django.db import transaction
with transaction.atomic():
    ClassificadorVersao.objects.filter(versao='v7').update(ativa=False)
    ClassificadorVersao.objects.filter(versao='v6').update(ativa=True)
print('rollback aplicado')
\""
```

Workers detectam em ≤ 60s (hot reload) e voltam a usar v6.

**Re-classificar processos atingidos pelo v7 nas últimas 24h:**

```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
from tribunals.jobs import reclassificar_recentes
job = reclassificar_recentes.delay(dias=1, paralelizar=True)
print(f'job: {job.id}')
\""
```

Acompanhar drenagem:
```bash
ssh ubuntu@192.168.1.32 \
  "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
   python manage.py shell -c \"
import django_rq
q = django_rq.get_queue('classificacao')
print(f'classificacao: pending={len(q):,} failed={q.failed_job_registry.count}')
\""
```

**Sinais de que o rollback foi bem-sucedido:**
- Distribuição de `Process.classificacao` volta ao perfil pré-v7
  (consultar `chart_dist_por_tribunal` na `/dashboard/leads/`).
- `ClassificacaoShadowLog` pode ser invertido: dispararíamos shadow
  agora com v6 como shadow, mas isso só faz sentido se o v7 ficar
  ativo após investigação. Por padrão, manter v6 ativo + v7 shadow.

---

## Riscos identificados nas reviews

(Compilados de REVIEW_T11_backend.md, REVIEW_T15_frontend.md,
REVIEW_T20_ml.md. Listados por impacto no flip do v7.)

### Risco 1 — `classificar()` (ativo) vs `_categorizar()` (shadow) usam thresholds diferentes
**Origem:** REVIEW_T20 issue média #1.
**Impacto:** alto. `comparar_shadow` pode reportar `agreement_rate`
artificialmente baixo porque a divergência seria de **política de
threshold**, não de pesos do modelo. Inviabiliza A/B legítimo.
**Mitigação:** fix em `tribunals/classificador.py:426-430` antes do
Passo 5 (shadow). Alinhar `classificar()` para também ler
`ThresholdTribunal` do DB, como `_categorizar()` já faz. REGRAS §4
exige DB-driven mesmo.
**Owner:** ML.
**Bloqueia flip?** Sim — sem isso, agreement não é confiável.

### Risco 2 — `_categorizar()` não filtra `ThresholdTribunal` por `versao_modelo`
**Origem:** REVIEW_T20 issue média #2 (`classificador.py:494-499`).
**Impacto:** alto no momento exato do switch v6→v7. Se v6 e v7 têm
thresholds no DB para o mesmo tribunal, `.filter(tribunal_id=...,
ativo=True).first()` retorna qualquer um sem ordem determinística.
**Mitigação:** aceitar `versao_modelo` como kwarg em `_categorizar`,
ou desativar (`ativo=False`) thresholds antigos antes do flip, ou
ordenar por `-criada_em` na query.
**Owner:** ML.
**Bloqueia flip?** Sim — risco de race no momento do flip.

### Risco 3 — Grid de threshold sem CV interno (overfit)
**Origem:** REVIEW_T20 issue média #3
(`treinar_classificador_v7.py:973-993`).
**Impacto:** médio. Otimiza `precision@500` no próprio holdout — risco
de overfit. Com dataset prod grande (centenas de milhares) o risco é
baixo; com validação humana ainda pequena (T6/T11/T15 ainda
populando), pode mascarar regressão.
**Mitigação:** monitorar `precision@500` real em prod nas primeiras
48h via `chart_top_fn_semana` e `chart_calibracao_modelo`. Follow-up
ML: CV interno 5-fold no train antes do v8.
**Owner:** ML.
**Bloqueia flip?** Não — aceita como follow-up.

### Risco 4 — Cache `delete_pattern` no-op silencioso no dashboard
**Origem:** REVIEW_T11 issue média #1 (`dashboard/views.py:1966-1971`).
**Impacto:** baixo no flip; afeta apenas UX da validação humana
(charts ficam stale por até 5min após cada `ProcessoValidacao`).
**Mitigação:** trocar `cache.delete_pattern(...)` por
`cache.delete_many([list])` ou usar versioning explícito.
**Owner:** Backend.
**Bloqueia flip?** Não — UX, não correção.

### Risco 5 — `kpis_validacao` N+1 (40 queries por hit)
**Origem:** REVIEW_T11 issue média #2 (`dashboard/queries.py:65-86`).
**Impacto:** baixo. ~40 queries / 20-60ms em toda visita a
`/dashboard/leads/visibilidade/`. Cresce com volume de lotes.
**Mitigação:** consolidar via `annotate` (1 query) ou cachear 5min com
`(user_id, minuto)`.
**Owner:** Backend.
**Bloqueia flip?** Não.

### Risco 6 — Permission `can_view_motivo` criada mas sem call-site
**Origem:** REVIEW_T11 issue média #3.
**Impacto:** baixo agora; vira blocker em T12/T13 quando templates
renderizarem `motivo` de outros validadores. Risco de leak de texto
livre se esquecerem o check.
**Mitigação:** documentar como TODO obrigatório antes de T16.
**Owner:** Backend.
**Bloqueia flip?** Não.

### Risco 7 — ~~Salt LGPD default em código~~ — RESOLVIDO POR DESESCOPAGEM
**Origem:** REVIEW_T11 issue média #4.
**Status (2026-05-12):** LGPD/anonimização foi declarada fora de escopo desta
versão. Hash + salt removidos de `dashboard/views.py:leads_validacao_salvar`;
campo `usuario_hash` permanece no schema mas não é populado. Sem feature de
anonimização ativa não há salt em uso → risco N/A.
**Reativação futura:** quando reabrir LGPD, ler salt obrigatório de
`os.environ['VALIDACAO_USUARIO_HASH_SALT']` (sem default).
**Bloqueia flip?** Não.

### Risco 8 — Focus trap ausente em modais + reduced-motion não respeitado
**Origem:** REVIEW_T15 issues médias #1 e #2.
**Impacto:** acessibilidade; não afeta o flip do v7.
**Owner:** Frontend.
**Bloqueia flip?** Não.

### Risco 9 — Settings novos não documentados
**Origem:** REVIEW_T20 (categoria K).
**Impacto:** operacional. `CLASSIFICADOR_RELOAD_TTL`, `SHADOW_SAMPLE_RATE`,
`VALIDACAO_LOTES_SEMANAIS_ENABLED` não estão em `.ia/OPS.md` nem
`.ia/CLASSIFICACAO.md`. Operador novo não sabe que existem.
**Mitigação:** documentar em T23 (docs).
**Owner:** Docs.
**Bloqueia flip?** Não — mas bloqueia hand-off.

### Risco 10 — `comparar_shadow` sobrescreve `.md` no mesmo dia
**Origem:** REVIEW_T20 (categoria G).
**Impacto:** baixo. Re-rodar `comparar_shadow` no mesmo dia
sobrescreve `SHADOW_COMPARISON_<data>.md`. Pode confundir auditoria
se rodado ad-hoc várias vezes no mesmo dia.
**Mitigação:** salvar cópia manual antes de re-rodar, ou aceitar
"último ganha" (default).
**Owner:** ML (follow-up).
**Bloqueia flip?** Não.

---

## Follow-ups pós-deploy (não bloqueiam o flip)

- T15 nit: focus trap em modais (`validacao_overview.html`,
  `validacao_lote.html`, `visibilidade.html`).
- T15 nit: bloco CSS global `prefers-reduced-motion`.
- T15 nit: `_lote_concluido.html` mostra chaves enum cruas em vez de
  labels humanas.
- T20 nit: CV interno no grid de thresholds (vide Risco 3).
- T20 nit: `recall@FN_candidatos` usa cutoff hardcoded `0.20`;
  importar `THRESHOLD_DIREITO_CREDITORIO` para single source of truth.
- T20 nit: conformal delta por nível (não só global).
- T6 nit: trigger PG UPDATE-block em `ProcessoValidacao` (REGRAS §6).
- T11 issue #1: `cache.delete_pattern` no-op silencioso (Risco 4).
- T11 issue #2: `kpis_validacao` N+1 (Risco 5).
- T11 issue #4: salt LGPD — RESOLVIDO via desescopagem (ver Risco 7).
- Dockerfile: adicionar `numpy` explicitamente (hoje vem só via
  `requirements.txt`; já basta, mas explicitação documental ajuda).
- Cleanup job de `ClassificacaoShadowLog` (retention 90d).
- Job semanal `gerar_amostra_validacao` (T21) deve estar habilitado
  via `VALIDACAO_LOTES_SEMANAIS_ENABLED=true`.
- Trigger semanal de `minerar_fn` (T16) — hoje é manual.

---

## Decisões pendentes do biz/jurídico (do REGRAS_NEGOCIO_VALIDACAO)

Estas duas perguntas estavam abertas em `REGRAS_NEGOCIO_VALIDACAO.md`
e **continuam pendentes** — não foram fechadas durante a implementação
de T6→T20. Não bloqueiam o flip do v7 (são governança), mas o
hand-off operacional precisa apontá-las.

### Pendência 1 — Interseção entre `model_admins` e `validadores_leads`
**Contexto:** Risco de conflito de interesse — quem treina o modelo
não deveria anotar seu próprio dataset de gate.
**Sugestão original:** proibir interseção via `clean()` em form de
admin.
**Status atual:** sem decisão. Hoje nada impede um usuário estar nos
dois grupos.
**Recomendação:** decidir antes de T23 (docs). Implementação trivial
no admin custom.

### Pendência 2 — DPO formal e política de privacidade publicada
**Contexto:** Texto do aviso de privacidade que aparece no
convite/onboarding do validador precisa ser revisado pelo jurídico
antes de go-live público.
**Status atual:** sem decisão. Convite usa template default sem
aviso LGPD específico.
**Recomendação:** levantar com jurídico antes de abrir validação
humana além da equipe interna. Não bloqueia validação interna em
prod hoje (todos os anotadores são funcionários).

---

## Histórico

| Data | Evento | Responsável |
|---|---|---|
| 2026-05-12 | Documento criado (T22). Smoke run em dev OK (sem dados TRF1 → CommandError esperado). | business-rules-analyst |
| [data real] | Pré-requisitos checados em prod | [nome] |
| [data real] | Treino shadow rodado | [nome] |
| [data real] | Shadow comparison 7d concluído | [nome] |
| [data real] | Sign-off + flip executado | [nome 1] + [nome 2] |
