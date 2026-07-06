# Jurimetria — plano e arquitetura

Objetivo: **jurimetria de alta qualidade** sobre o acervo (Voyager) + o corpus de
jurisprudência (Zordon), com foco na vantagem competitiva do negócio (crédito
judicial / precatórios). Base de recon: 4 auditorias (2026-07-05) — dados atuais,
corpus Zordon, jurimetria de ponta BR, jurimetria de precatório.

## Estado dos dados (auditado 2026-07-05)
- **~59M processos**, **~1,135 bilhão de movimentações** (~19,3 movs/proc — volume
  LEGÍTIMO, não duplicação). Dedup por `UniqueConstraint(tribunal, external_id)` —
  índice `uniq_mov_tribunal_extid` **válido/único**; re-ingestão é idempotente.
- **60 tribunais ativos**: 27 TJs + 6 TRFs + TST+24 TRTs + STJ/STF.
- **Zordon**: 665k acórdãos STJ (`Acordao`: relator, órgão, classe, tema, ementa,
  tese, decisão, data — todos no banco + embeddings) + extração de autos.

## Princípios de qualidade (inegociáveis)
1. Toda métrica agrega sobre **código TPU** (Classe/Assunto/Movimento), nunca texto
   livre; **dedup por numeração única CNJ** (`numero_cnj`).
2. **Camada 1 = KPIs oficiais CNJ** (fórmulas auditáveis) antes da Camada 2 (valor).
3. **Descritivo ≠ preditivo** na UI; sinalizar n pequeno e quebras temporais.
4. **Matriz Lex Machina**: entidade × resultado × pedido (não só volumetria).
5. Alta confiança = **classificador automático + validação humana em amostra** (ABJ).

## Três tracks
```
Jurimetria PROCESSUAL (Voyager) ─ fluxo/volume/tempo (grau CNJ), 59M proc / 1,1B movs
Jurimetria de RESULTADO (Zordon) ─ outcome/tese/relator, 665k+ acórdãos
Jurimetria de PRECATÓRIO (edge)  ─ tempo-até-pagamento T + risco de ente + lead score
```

### Track 1 — Processual (Voyager)
- **Camada 1 (CNJ-grade)**: MVs por {tribunal×grau×classe×assunto×órgão×ano} —
  congestionamento (bruta/líquida), IAD, tempo de tramitação (mediana por fase),
  recorribilidade, taxa de reforma, maiores litigantes. Reusa padrão das 4 MVs +
  `warm_charts`.
- **Camada 2**: outcome/tempo/valor por {classe×assunto×órgão×relator×CNPJ}.

### Track 2 — Resultado (Zordon / acórdãos)
- API `/api/jurimetria/*` (relator×resultado, tema×série temporal, órgão×classe).
- **Classificador de resultado** (provido/negado/parcial) do dispositivo `decisao`
  (TF-IDF/BERTikal + validação humana em amostra).
- **Clustering de teses** (embeddings de `tese_juridica`).
- **Grafo de citações** (`jurisprudencia_citada`/`referencias_legislativas`) → PageRank/HITS.
- Expandir corpus: súmulas TST + RG/Súmulas Vinculantes STF (recon feito).

### ⭐ Fonte de dados de precatório JÁ EXISTE: Juriscope/Falcon (2026-07-06)
O banco do **Juriscope/Falcon** (`10.10.0.51/falcon`, DSN read-only em
`JURISCOPE_DB_DSN`) já tem o Track 3 estruturado — **não reconstruir, integrar**:
- **2,31M processos** de precatório (`datamodel_process`), join por `numero_autos` (=CNJ)
  ↔ Voyager `numero_cnj`. Ponte existente: `datamodel_voyagerleadreport` (867k).
- **natureza 63%** (1,3M ALIMENTAR + 159k COMUM), **valor 70%**, **ente devedor 67%**
  (`entity_id`→`datamodel_entity`), **ordem cronológica 67%** (`ordem_orcamentaria` =
  posição na fila), **data_oficio 45%** → inputs diretos do modelo de T.
- **45k requisições de pagamento** (`datamodel_requisicaopagamento`: natureza, valor,
  situação). **1,12M autos baixados** (TRF1 572k + TJSP 504k) — PDFs em `processfile.file`.
- **Integração feita**: `dashboard/juriscope_client.dados_precatorio(cnj)` (read-only) +
  bloco precatório do dossiê (`dashboard/jurimetria_dossie.py`). API do Juriscope existe
  (`/datamodel/api/process/?numero_autos=`) mas exige `IsAuthenticated` sem token de
  serviço → DB read-only é o caminho pragmático.
- Próximo: modelo de **T** (ordem+data_oficio→quando paga), **rating de ente** (62k
  entidades + histórico), autos→Zordon (RAG/outcome/sinais finos).

### Track 3 — Precatório ⭐ (vantagem competitiva)
Alpha = **precificar T (tempo-até-pagamento) e risco por ente devedor melhor que o mercado**.
- **Scraper de ordens cronológicas** dos TJs/TRFs/TRTs (TRF1 ordemcronologica, TJSP
  CADIP, TJMG, TJDFT, TJES, TRTs…) → série histórica (inscrição→quitação) →
  prazo realizado, throughput, backlog por ente.
- **Rating de ente devedor (0–100)**: judicial (listas) + fiscal (Tesouro Transparente/
  RCL, regime especial, painel federal SOF/MPO, SisPreq/CNJ).
- **Modelo de T** por {ente×regime×natureza×posição na fila}, **versionado por marco
  legal** (EC 62/2009 → 113/114/2021 → 136/2025).
- **Lead score = deságio_justo − deságio_pedido** → integra ao classificador (v7).
- **Detecção de expedição em tempo real** (core DJEN/PJe) → prospecta antes do concorrente.

## Fundação (Fase 0 — pré-requisito)
| Gap | Ação |
|---|---|
| Sem `relator` | capturar do enricher + do Acordao |
| Natureza não estruturada | materializar ALIMENTAR/COMUM (extract_fields + classe TPU) |
| Classe/assunto incompleto (TJMG/TJSP hist.) | `preencher_classe_via_djen` em massa |
| Partes duplicadas (máscara TRF3/TJSP) | `dedup_partes` + consolidar mascaradas |
| Precatório sem estrutura | novo modelo (ente, natureza, situação, ordem, T) |
| TPU desatualizado | sync catálogo ClasseJudicial/Assunto com SGT/CNJ |

## Sequenciamento
| Fase | Entrega | Esforço |
|---|---|---|
| 0 Qualidade | relator, natureza, dedup, backfill classe, modelo precatório | ~1 sem |
| 1 Processual C1 | MVs CNJ-grade + endpoints + painel | ~1 sem |
| 2 Resultado | API agregação Zordon + classificador dispositivo + validação | ~2 sem |
| 3 Precatório ⭐ | scraper ordens + rating ente + modelo T + lead score | ~2-3 sem |
| 4 Teses/grafo | clustering tese + grafo citações + súmulas TST/STF | ~1-2 sem |
| 5 Serving | painéis + API + docs | contínuo |

Começar por **Fase 0 + Track 3 (precatório)** — Fase 0 destrava tudo; precatório é o maior ROI.

## Modelo de ciclo de vida do ativo (sobrevivência multi-estado)

Objetivo: prever, pra cada ativo, a **chance** e o **tempo** de avançar em cada
etapa até o pagamento. É a jurimetria de precatório de ponta-a-ponta.

```
DIREITO_CREDITÓRIO ─▶ [homologação de cálculos] ─▶ [expedição ofício = PRECATÓRIO] ─▶ PAGAMENTO
  1,19M (Voyager)        calculos_homologados NOT NULL     data_oficio (P50 ~594d          36k PAGO +
  população em risco     (arquivo de cálculo existe)       protocolo→ofício)              data_conta_liquidacao
```

Cada seta = transição de **sobrevivência** (time-to-event com **censura à direita**
pros que ainda não avançaram). "Chance" e "tempo" são leituras da mesma curva.

### Fontes (join Juriscope.numero_autos ↔ Voyager.numero_cnj)
- **População/censura**: Voyager `Process` (classificacao DIREITO_CREDITORIO / PRE_PRECATORIO / PRECATORIO + timestamps).
- **Eventos + datas + features**: Juriscope/Falcon `datamodel_process` (natureza, valor_acao,
  entity_id→ente, ordem_orcamentaria, data_oficio, calculos_homologados) + `datamodel_requisicaopagamento`
  (situacao "PAGO TOTAL" 36k, data_conta_liquidacao) — via `JURISCOPE_DB_DSN` read-only.

### Modelos (3 transições + serving)
1. **DC → precatório** (chance + tempo): sobrevivência sobre 1,19M DC; evento = expedição (`data_oficio`);
   features = tribunal, órgão, classe, ente (federal/estadual/municipal), valor, natureza, tempo decorrido.
2. **Marco homologação**: evento intermediário (`calculos_homologados` NOT NULL / mov "homologo os cálculos").
3. **Precatório → pagamento (modelo T)**: sobrevivência sobre precatórios; evento = pagamento (36k PAGO);
   T = data_oficio → data_conta_liquidacao. Features + posição na fila (`ordem_orcamentaria`) + regime do ente.

### Método
Baseline Kaplan-Meier estratificado (interpretável) + modelo principal (Cox/Gradient-Boosted
Survival). Avaliação C-index + calibração + **split temporal** (treino em coortes antigas, teste
recentes) contra vazamento. Desenho amostral cuida do viés (negativos vêm do Voyager, não só do
Juriscope que só tem os que já viraram). Serving leve (joblib) no dossiê por CNJ.

### Saída no dossiê (por CNJ)
```
DIREITO_CREDITÓRIO → chance de virar precatório 68% · tempo mediano ~18 meses
  próximo marco: homologação de cálculos — 45% em 6 meses
PRECATÓRIO → T (tempo-até-pagamento) ~2,1 anos · ente rating 78/100
```

## Modos de uso, papel do LLM e interface

**Princípio:** o LLM **não calcula** — os números vêm de **agregação SQL + modelos
determinísticos** (auditáveis). O LLM (gpt-oss:20b local do Zordon, fail-closed) faz:
(1) **lê** texto (extrai natureza/valor/ente; classifica resultado do acórdão),
(2) **traduz** pergunta NL → consulta estruturada, (3) **narra/explica com citações**,
(4) **nunca inventa número** (preso aos dados computados).

```
Pergunta(PT) ─► LLM planner (NL→consulta+retrieval)
                   │
      SQL agregação · RAG acórdãos(bge-m3+rerank) · Modelo(T,deságio,risco,score)
                   │  (tudo determinístico/auditável)
                   ▼
             LLM synthesizer (narra + CITA; nº vêm de cima)
```

**3 modos:**
1. **Painéis** (descritivo, sem LLM) — input: filtros TPU (tribunal/classe/assunto/
   relator/órgão/período/ente); output: taxa de êxito, tempo mediano, congestionamento,
   top teses, T por ente, backlog de precatório.
2. **Card por entidade** (determinístico + LLM narra) — input: CNJ/lead/ente; output:
   card com natureza, valor, rating do ente (0-100), T estimado, deságio justo vs pedido,
   score de lead + 1 parágrafo do LLM. Pluga no funil Juriscope.
3. **Pergunta em linguagem natural** (LLM planner+synthesizer) — input: pergunta livre;
   output: números computados + precedentes citados + ressalvas (n, quebra legal).

### Interface — princípios de AUDITABILIDADE (first-class)
Toda métrica exibe: **n amostral · período · fonte (MV/query) · data de atualização** +
affordance **"ver dados/query"** (drill-down até os processos/acórdãos). Badges
**Descritivo vs Preditivo**. A narrativa do LLM sempre **linka as fontes**. Nº nunca
sem procedência. Reusa HTMX/ECharts (padrão dashboard) + API de agregação do Zordon.

## Fontes de referência
- CNJ Justiça em Números / Glossário de Indicadores; DataJud API (Elasticsearch);
  TPU (Res. CNJ 46/2007) / SGT.
- Precatório: Painel Federal SOF/MPO, SisPreq/Res. CNJ 303/2019, Tesouro Transparente
  (RCL/regime especial), EC 62→113/114→136/2025. Listas: TRF1 ordemcronologica,
  TJSP CADIP, TJMG, TJDFT, TJES, TRTs.
- Estado da arte: Lex Machina (entidade×outcome×motion), ABJ (método 4 etapas +
  validação manual), datasets PT-BR (RulingBR, LeNER-Br, VICTOR), BERTikal/LegalNLP.
