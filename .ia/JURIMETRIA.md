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

## Fontes de referência
- CNJ Justiça em Números / Glossário de Indicadores; DataJud API (Elasticsearch);
  TPU (Res. CNJ 46/2007) / SGT.
- Precatório: Painel Federal SOF/MPO, SisPreq/Res. CNJ 303/2019, Tesouro Transparente
  (RCL/regime especial), EC 62→113/114→136/2025. Listas: TRF1 ordemcronologica,
  TJSP CADIP, TJMG, TJDFT, TJES, TRTs.
- Estado da arte: Lex Machina (entidade×outcome×motion), ABJ (método 4 etapas +
  validação manual), datasets PT-BR (RulingBR, LeNER-Br, VICTOR), BERTikal/LegalNLP.
