# Estágio do crédito — classificador DC / PRE / EMITIDO / MORTO

> Modelo NOVO, primo do classificador de leads v6/v7 (`.ia/CLASSIFICACAO.md`) e
> herdeiro do design do Emulador de Autos (`.ia/EMULADOR_AUTOS.md`). Responde
> **em que estágio o crédito está**, só com dado público — treinado com
> supervisão cruzada (a verdade vem dos autos/Falcon; a feature vem do público).

## As 4 classes (fronteiras)

| Classe | Definição | Evidência de rótulo (autos/Falcon) |
|---|---|---|
| **DC** | Direito creditório: sentença/acórdão/trânsito, sem cumprimento maduro | doc `SENTENCA`/`ACORDAO`/`TRANSITO_JULGADO`; evento `TRANSITO_JULGADO` |
| **PRE** | Pré-precatório: cumprimento/homologação de cálculos, sem requisitório | doc `CUMPRIMENTO_SENTENCA`; evento `HOMOLOGACAO_CALCULOS`; `PLANILHA_CALCULO` (fraca) |
| **EMITIDO** | Precatório/RPV requisitado | doc `OFICIO_REQUISITORIO`; Falcon `data_oficio`/`numero_processo_DEPRE`/`codigo_requisitorio`; evento `EXPEDICAO_OFICIO`; `ALVARA`/`PAGAMENTO_COMPROVANTE` (pagamento pressupõe requisição) |
| **MORTO** | Crédito encerrado — só com evidência forte, sempre subtipado | ver abaixo |

Hierarquia de conflito: **EMITIDO > PRE > DC** (estágio mais avançado vence).
MORTO é avaliado **por último**.

### MORTO subtipado (granularidade obrigatória)

| Subtipo | Significado | Evidência |
|---|---|---|
| `satisfeito` | extinção pelo pagamento / art. 924 II CPC — **desfecho feliz** (vale como sinal de jurimetria "ente pagou") | texto de SENTENCA/DECISAO/ACORDAO dos autos (scan com trecho); ou `falcon.is_extinto` + doc de pagamento |
| `improcedente_prescrito` | improcedência / prescrição / decadência COM mérito — morto de verdade | texto dos autos (scan com trecho), sem ofício no processo |

Regras que NÃO rebaixam:
- **extinção sem resolução de mérito** (art. 485 etc.) → mantém estágio + flag `extincao_sem_merito` (pode ser reproposto);
- **extinção de incidente** (embargos/agravo/impugnação, pelo nome do doc) → ignorada;
- **`falcon.is_extinto` sem evidência textual** → flag `extincao_natureza_incerta`, mantém estágio;
- improcedência convivendo com ofício expedido → conflito (provável incidente) → flag incerta.

### Flags
- `rpv` — Falcon `tipo=RPV` (sub-tipo do EMITIDO, não classe própria);
- `pagamento_parcial` — ALVARA/PAGAMENTO_COMPROVANTE/evento PAGAMENTO presente (EMITIDO em levantamento);
- `cessao` — Falcon `cessao_credito`.

## Pipeline

```
[FASE 1 — rótulos]           zordon (LAN acervo+falcon)
scripts/estagio/build_labels.py scan   → extincao_scan.jsonl   (decifra chunks
                                          SENTENCA/DECISAO/ACORDAO, padrões de
                                          extinção com trecho auditável)
scripts/estagio/build_labels.py build  → estagio_labels.jsonl.gz + report
      fontes: acervo_documento.doc_classe + acervo_metadadoextraido.eventos
              + falcon datamodel_process (agregado por numero_autos)
      cada linha carrega evidencias[] (qual doc/campo gerou o rótulo)

[FASE 2 — features]          host voyager (container web, LAN DB)
scripts/estagio/build_features.py      → estagio_features.csv.gz
      join rótulo×Process por (numero_cnj, tribunal do CNJ); 1 query agregada
      SARGável por processo_id (extensão do _MOVS_AGG_SQL do v6)

[FASE 3 — treino/gate]       máquina de treino (venv lightgbm)
scripts/estagio/converter_parquet.py   → parquet
scripts/estagio/train_estagio.py       → out/estagio_gbm_v1.joblib + gate_report.json

[Predição]                   Django
tribunals/estagio.py::predict_estagio(cnj)
      → {classe, proba, sinais[], valor_homologado?, partes?, versao}
      artefato em settings.ESTAGIO_MODEL_PATH (default models/estagio_gbm_v1.joblib)
      fail-closed: sem artefato → EstagioIndisponivel (nunca chuta)
```

## Anti-leakage (requisito nº 1)

- **Feature = SÓ dado público do Voyager**: cadastro (`classe_codigo`, `assunto`,
  `ano_cnj`, `data_autuacao`), movimentações (contagens/datas de marcos lidos do
  TEXTO público) e partes do enricher público.
- **PROIBIDO como feature**: qualquer campo de autos/Falcon (são só rótulo),
  `Process.classificacao*` (saída do outro modelo), `subtipo`/`flag_*`/`fonte`/
  `label_ev_dt` da Fase 1. A lista de features do treino é explícita
  (`train_estagio.py::FEATURES`).
- `tribunal` entra como categórica, mas com dupla checagem: LOTO (validação 3)
  + red flag automático se `tribunal` aparecer no top-3 de ganho.

## Features (todas públicas, origem documentada)

- Cadastro: `ano_cnj`, `is_cumprimento`, `is_fazenda`, `is_juizado_anti`,
  `dias_autuacao`, `classe_codigo`†, `assunto_codigo`†, `tribunal`† (†categórica)
- Volume: `total_movs`, `distinct_tipos`, `movs_por_ano`, `duracao_dias`,
  `dias_ult_mov`, `n_partes`, `tem_ente_publico_passivo`
- Marcos (contagem + tem_/dias_desde_): expedição (`exped_tc_n` tipo_comunicacao,
  `exped_forte_n` texto, `oficio_text_n`, `reqpag_text_n`), `precat_text_n`,
  `rpv_text_n`, trânsito (`transito_n`), homologação (`homolog_n`),
  cumprimento (`cumpr_text_n`), pagamento (`pago_n` alvará/sequestro),
  desfechos (`ext_satisf_n`, `ext_semmerito_n`, `improc_n`, `cancel_n`)
- Ordenação temporal: `pago_pos_exped`, `extneg_pos_exped`, `dias_transito_a_exped`
- Best-effort (nullable, abstém): `log_valor_homologado` (R$ perto de "homolog"
  no texto público) — e como SAÍDA informativa: `valor_homologado`, `partes`
  (polo ativo, não-advogado)

## Rótulos — volumes (Fase 1, 2026-07-30)

Universo avaliado: 1.329.507 CNJs (acervo 319k ∪ Falcon 1,33M; acervo ⊂ Falcon).
Rotulados: **820.777** (descartados: 454k sem evidência de estágio, 55k not_found).
Scan de extinção: 248.939 docs lidos (SENTENCA/ACORDAO/DECISAO de 70k candidatos),
20.802 hits com trecho.

| classe | N | principais tribunais |
|---|---|---|
| EMITIDO | 627.475 | TRF1 436k · TJSP 179k · TRF5 9,4k · TJMG 2,3k |
| PRE | 155.665 | TRF1 152k · TRF3 3,4k (falcon sem_expedicao + classe cumprimento dos autos) |
| MORTO | 32.392 | TJSP 32k — **100% subtipo `satisfeito`** |
| DC | 5.245 | TRF5 2,6k · TRF1 2,2k · TJMG/TJSP/TJMA/TJAL |

Notas honestas:
- `improcedente_prescrito` = **0** no corpus (a firma baixa lead, não morto; os
  hits de improcedência coexistem com ofício → conflito → flag incerta). A
  classe MORTO treinável hoje é só o `satisfeito`.
- MORTO é 100% TJSP (scan e-SAJ + `is_extinto` TJSP) — risco de proxy de
  tribunal monitorado no gate (LOTO + red flag).
- PRE nasce do negativo forte do Falcon: `sem_expedicao` em todas as linhas
  (firma abriu os autos e NÃO achou requisitório) + classe judicial do cadastro
  raspado (`data->>'Classe judicial'`) de cumprimento/execução contra a Fazenda.
- md5 `estagio_labels.jsonl.gz` = `658258043d640b556a6e2c98eb6f2759`.

## Dataset de treino (Fase 2, snapshot 2026-07-30)

Join rótulo×Voyager: 234.334 rótulos (após cap 60k/classe-tribunal em
EMITIDO/PRE) → **99.667 com feature pública** (43% — floor de ingestão limita
TJSP/TRF3 antigos; TRF1 domina). Higiene de rótulo DEFASADO (staleness — o
público é mais novo que os autos): −1.749 EMITIDO com extinção-pelo-pagamento
publicada pós-expedição, −1.465 PRE e −238 DC com expedição publicada →
**96.215 linhas**: EMITIDO 76.969 · PRE 14.008 · DC 3.881 · MORTO 1.357
(MORTO 100% TJSP — o floor derrubou de 32k pra 1,4k).

## Gate pré-registrado + resultados (estagio_v1, 2026-07-30)

Critério: precision(EMITIDO) ≥ 0,90 **e** precision(MORTO) ≥ 0,90 no split
hash-CNJ; red flag se `tribunal` dominar o ganho. Decisão com guarda de
precisão: MORTO só quando p(MORTO) ≥ **0,80** (threshold calibrado no val;
abaixo rebaixa pro melhor não-MORTO — demote-only, replicado na lib).

**Veredito: PASS** (LightGBM 4-classes, best_iter 156, sem `tribunal` como
feature — removido após red flag no 1º treino).

### V1 — split hash-CNJ 70/10/20 (saltado; test 19.313)

| classe | precision | recall | F1 | support | Brier ovr |
|---|---|---|---|---|---|
| EMITIDO | **0,954** | 0,972 | 0,963 | 15.451 | 0,043 |
| MORTO | **0,926** | 0,189 | 0,315 | 264 | 0,008 |
| PRE | 0,863 | 0,868 | 0,865 | 2.826 | 0,028 |
| DC | 0,830 | 0,720 | 0,771 | 772 | 0,013 |

acc 0,936 · macro-F1 0,728. Recall de MORTO é baixo por desenho
(precisão-first: só afirma MORTO com p≥0,80; o resto cai em EMITIDO — o
estágio anterior verdadeiro).

### V2 — temporal (treina última-mov >365d, testa recentes; 38.686)

acc 0,670. EMITIDO p 0,736/r 0,865 · PRE p 0,500 · DC recall colapsa (0,014) ·
MORTO p 0,582. Leitura honesta: processos com atividade recente são mais
difíceis (o corte remove do treino exatamente a faixa de `dias_ult_mov` do
teste); reclassificar periodicamente em produção; o gate de negócio vale no V1.

### V3 — leave-one-tribunal-out (treina sem TRF1, testa TRF1; 70.242)

EMITIDO p 0,828/r 0,841 (robusto cross-tribunal). PRE colapsa (p 0,181) — 91%
do rótulo PRE é TRF1, sem ele não há treino da classe. MORTO: support 0 no
TRF1 (N/A). Red flag tribunal: **não disparou** (top-3 do ganho:
`distinct_tipos`, `dias_ult_mov`, `dias_autuacao`).

### Top-10 feature importance (gain)

`distinct_tipos` 96k · `dias_ult_mov` 57k · `dias_autuacao` 31k ·
`classe_codigo` 28k · `rpv_text_n` 20k · `cumpr_text_n` 20k ·
`dias_desde_transito` 19k · `total_movs` 16k · `precat_text_n` 14k ·
`dias_desde_exped` 14k — marcos processuais/temporais dominam (sanidade OK).

### Amostra de aceite (TJSP)

`scripts/estagio/out/amostra_validacao_tjsp.md` — 10 conhecidos certos (com
evidência dos autos + sinais públicos), 5 EMITIDO novos e 5 DC novos
(predição cega p/ validação humana). Gerador: `gerar_amostra_validacao.py`.

### Artefatos (md5 — fora do git)

| arquivo | md5 |
|---|---|
| `estagio_gbm_v1.joblib` | `6bf0c7a476147611a520f1b4c7601e36` |
| `estagio_features.csv.gz` | `c80691c8e69de3c23371b3af1529a39d` |
| `estagio_labels.jsonl.gz` | `658258043d640b556a6e2c98eb6f2759` |

Cópias na máquina de treino (curiosity) em `scripts/estagio/out/`; a lib
procura o joblib em `models/estagio_gbm_v1.joblib` (ou `ESTAGIO_MODEL_PATH`).

## Limitações conhecidas

- **Rótulo tem viés de seleção** (autos baixados = quase-leads; ver
  `.ia/EMULADOR_AUTOS.md §d`) — mitigado com negativos Falcon
  (`sem_expedicao`/`is_extinto`) e MORTO textual, mas o universo rotulado NÃO é
  amostra aleatória do acervo de 75M.
- **Staleness**: o rótulo reflete os autos no momento do download; as features
  são o público de hoje. Processo que evoluiu depois (ex.: PRE→EMITIDO) vira
  ruído de rótulo a favor do público (o modelo tende a "adiantar" o estágio).
- **MORTO/improcedente_prescrito é raro** no acervo (a firma baixa lead, não
  morto) — recall dessa classe deve ser lido com N pequeno.
- **TJSP/TRF3**: floor de ingestão do Voyager limita o match rótulo×feature
  (mesma fronteira do Emulador de Autos).
- Falcon é **silver** (régua: 90% do erro de extração é do LLM — memória
  `observabilidade-eval-rag`); κ humano continua sendo o próximo gargalo.

## Operacional

- Datasets e modelo fora do git (`scripts/estagio/out/`, `models/` no
  .gitignore); vínculo por md5 no `.ia/MODELOS.md`.
- Rodar de novo: seção "Pipeline" acima — cada script tem docstring com o
  comando exato e onde rodar (zordon / host voyager / máquina de treino).
- A lib NÃO está plugada em views/jobs — outro agente faz a UI.
