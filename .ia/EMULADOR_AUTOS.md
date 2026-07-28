# Emulador de Autos — doc de decisão (escopo, NÃO implementado)

> **Status:** ESCOPO / read-only. Este documento é uma investigação de viabilidade
> (28/07/2026). Nenhum modelo foi treinado, nenhum código/DB alterado. Todos os
> números vêm de queries read-only em Voyager (`.103`, `core.settings`) e Zordon
> (`zordon.settings` + Falcon `datamodel_process`).

## 0. O que é (e o que NÃO é)

Modelo **novo e separado** do classificador de leads v6/v7. Objetivo: prever a
**verdade-dos-autos** (natureza, estágio, desfecho, valor, óbito/cessão — o que os
documentos dizem) usando **SÓ o dado público do tribunal** como feature, treinando
nos processos que TÊM autos, pra classificar o universo de 75,6M **sem baixar autos**.

| | Classificador v6/v7 (existe) | Emulador de autos (este doc) |
|---|---|---|
| Label | Juriscope + validação humana → é-lead | **Autos/Falcon** → natureza, estágio, desfecho, valor, eventos |
| Saída | 1 (lead/precatório, 3 níveis) | **multi-saída** (natureza, estágio, desfecho, valor-faixa, é-lead) |
| Feature | movs públicas (F1-F30) | mesmas features públicas + sequência de movs |
| Relação | em produção | **complementar**, não substitui |

O v6/v7 já é um "emulador de autos" implícito pro target **é-lead** (AUC 0,96). O
valor incremental do modelo novo está nos targets **estágio** e **desfecho** — e em
usar os **negativos ricos** do Falcon (`sem_expedicao`, `not_found`, extinto) que o
v6 não explora de forma estruturada.

---

## (a) Labels-autos disponíveis, por target + N

### Fonte 1 — `MetadadoExtraido` (Zordon, extração LLM dos autos vetorizados)
- **176.054 CNJs** extraídos (versão `v1`), não os ~154k do brief — cresceu.
- Distribuição por tribunal (via segmento CNJ): **TRF1 84,7k · TJSP 83,3k · TRF5 5,3k
  · TJAL 1,5k · TJMG 994 · TJMA 127 · TRF3 80**.
- Confiança global: baixa 102,6k / média 54,5k / alta 18,9k.

Cobertura por campo (não-abstido, com valor) sobre os 176k:

| Campo (target) | com valor | % |
|---|---|---|
| `natureza` | 145.649 | 82,7% |
| `estagio_sinal` | 101.554 | 57,7% |
| `ente_devedor` | 78.488 | 44,6% |
| `exercicio` | 78.235 | 44,4% |
| `beneficiario` | 78.005 | 44,3% |
| `data_oficio` | 77.819 | 44,2% |
| `numero_precatorio` | 54.515 | 31,0% |
| `valor_total` | 37.008 | 21,0% |

Eventos (distinct CNJ): EXPEDICAO_OFICIO 79,1k (44,9%) · TRANSITO_JULGADO 49,6k
(28,2%) · CESSAO_CREDITO 14,8k (8,4%) · PAGAMENTO 13,8k (7,8%) · FALECIMENTO 7,7k
(4,4%) · HABILITACAO_HERDEIROS 4,2k (2,4%). **`DESFECHO_NEGATIVO` = 0 eventos.**

Valores dos labels categóricos:
- `estagio_sinal`: **PRECATORIO 79.104 · PRE_PRECATORIO 22.443 · DIREITO_CREDITORIO 7 · None 74.500**.
- `natureza`: **ALIMENTAR 135.453 · COMUM 10.196** (praticamente binário, sem diversidade).

> ⚠️ Cuidado tautológico ([[dataset-falcon-extracao-eval]]): no corpus,
> `natureza` (fonte=join_falcon) e `valor_total` (fonte=agregação-Falcon) **JÁ vêm do
> Falcon**, não são leitura pura do LLM. Só `ente_devedor`, `data_oficio`, `cessao`
> são LLM-reais. Isso significa que **o label-autos "natureza/valor" É o label-Falcon**
> — a fonte real e ampla é o Falcon (Fonte 2), não a extração de 176k.

### Fonte 2 — Juriscope / Falcon (`datamodel_process`) — **a fonte de label real**
- **2.385.254 linhas / 1.326.373 processos distintos** (`numero_autos`) com dado
  estruturado de precatório. **7,5×** o tamanho da extração LLM.
- Formato `numero_autos` = CNJ formatado, casa direto com `Process.numero_cnj`.
- Por tribunal (distinct autos): **TRF1 668k · TJSP 292k · TRF3 126k · TRF4 73k ·
  TJMG 58k · TRF6 50k · TRF5 47k · TJAL 7,1k · TRF2 3,7k · TJMA 1,4k**.
- Labels estruturados por processo:
  - **valor** (`valor_acao`/`valor_acao_corrigido`): **1.094.330 com valor** (82%).
  - **natureza**: ALIMENTAR 688k · COMUM 93k · None 695k (≈52% ausente).
  - **desfecho/negativos** (por processo, agregado): `sem_expedicao` (todas as
    linhas) **276.552** · `not_found` **55.116** · `is_extinto` (≥1 linha)
    **129.193** · `cessao_credito` **15.075**.

### Fonte 3 — `LeadConsumption.resultado` (Voyager) — desfecho real reportado
- **718.522 processos distintos** consumidos por 2 clientes.
- Resultados presentes: **validado 425.903 · pendente 256.157 · sem_expedicao 92.977**.
- ⚠️ **Não há** `erro/arquivado/pago/cedido` em prod (só 3 dos 7 valores canônicos
  aparecem). O negativo limpo é `sem_expedicao` (92,9k).

### Fonte 4 — `ProcessoValidacao` (humano)
- **0 rows.** O pipeline de validação humana existe em código mas **nunca foi usado
  em prod**. Não há label humano disponível hoje.

**Resumo (label-autos confiável por target):**

| Target | Fonte principal | N com label |
|---|---|---|
| **é-lead / desfecho binário** | Falcon `sem_expedicao`/`not_found` + LeadConsumption | ~1,1M pos (Falcon com expedição) + **~331k neg** (Falcon sem_exp+not_found) + 92,9k neg (Juriscope) |
| **estágio** (PRE/PRECATÓRIO/DC) | MetadadoExtraido `estagio_sinal` | ~101k |
| **natureza** | Falcon `natureza` | ~781k (ALIMENTAR/COMUM) — **binário, silver** |
| **valor-faixa** | Falcon `valor_acao` | ~1,09M |
| **eventos** (óbito/cessão/trânsito) | MetadadoExtraido eventos | óbito 7,7k · cessão 14,8k · trânsito 49,6k |

---

## (b) Features públicas + cobertura (existe pra 75,6M)

Universo: **`Process` ≈ 75,6M** (reltuples), **`Movimentacao` ≈ 1,18 bilhão**.

Fonte de feature — tudo em `Process` + `Movimentacao` (Voyager), já usado pelo
`compute_features` do v6 (`tribunals/classificador.py:355`):

| Feature | Origem | Cobertura (universo) |
|---|---|---|
| `classe_codigo` / `classe_nome` | Process (DJEN/Datajud/enricher) | **TRF1 86% · TJSP 98% · TRF3 93% · TRF5 96%** (amostra TABLESAMPLE) |
| `assunto` CNJ | Process | catálogo 2.618 assuntos; cobertura menor (enricher-dependente) |
| `total_movimentacoes`, primeira/última mov | Process (trigger) | 100% (contagem sempre existe) |
| movs: `tipo_comunicacao`, `codigo_classe`, `texto`, datas | Movimentacao | 100% dos que têm mov |
| partes (tipo/contagem, papel, polo) | ProcessoParte | enricher-dependente |
| `valor_causa` | Process (enricher) | **baixíssima** (só 1.893 dos 100k autos-matched) |
| tribunal, órgão julgador, vara | Process | classe/órgão via enricher |

Catálogos: `ClasseJudicial` 642 · `Assunto` 2.618 (nacionais TPU/CNJ).

**Sinal da SEQUÊNCIA de movimentações** (o que o v6 ainda não explora bem):
- ordem temporal de `tipo_comunicacao`/`codigo_classe` (n-gramas de transição:
  Autuação→Trânsito→Expedição→Pagamento);
- deltas de tempo entre marcos (tempo até trânsito, até expedição);
- presença/data de marcos-chave (expedição, alvará, sequestro, extinção) — já
  parcialmente capturado pelos SQL do F2/F20/F24/F30;
- `search_vector`/`texto` completo pra features de bag-of-words / regex.

O `valor_causa` público é ruído (cobertura ~2%). **Valor só existe estruturado no
Falcon** — logo valor-exato NÃO é previsível do público (ver seção e).

---

## (c) O JOIN — N de treino real, por tribunal

Cruzando as fontes de label com `Process` que tenha **feature pública suficiente**
(`classe_codigo` preenchido **E** `total_movimentacoes ≥ 3`):

### Via extração LLM (176k autos) × Voyager
- **100.315 de 176.054 (57%)** dos CNJs extraídos casam com um `Process` em Voyager.
  O grande buraco é **TJSP** (83,3k extraídos → só 11,5k em Voyager): os `Process`
  do TJSP em Voyager só existem a partir de **2026-05-24** (`inserido_em` real vai de
  2026-05-24 a 2026-07-27 — enricher e-SAJ recente), enquanto o acervo/Falcon tem
  TJSP muito mais antigo. TRF3 idem (Falcon 126k, Voyager quase nada — enricher novo).
  **O limite é a idade/floor da ingestão do Voyager, não o label.**
- Dos matched, **feature-suficiente (classe + movs≥3)** = **N treinável por tribunal:**

| Tribunal | N treinável (extração) |
|---|---|
| TRF1 | 25.116 |
| TRF5 | 4.867 |
| TJSP | 4.086 |
| TJAL | 1.504 |
| TJMG | 994 |
| TRF6 | 186 |
| STJ | 16 |
| TRF3 | 76 |
| **Total** | **~36,9k** |

### Via Falcon (1,33M autos) × Voyager
O join Falcon×Voyager é o que define o N real do modelo (Falcon = fonte de label).
Limite estrutural: o Falcon tem label pra 1,33M processos, mas o **Voyager só tem
feature pública pros processos dentro do seu floor de ingestão por tribunal** — por
isso o match cai muito em TJSP/TRF3 (label Falcon existe, feature pública não).

Estimativa do teto por tribunal (label Falcon × cobertura de feature no Voyager):
- **TRF1**: Falcon 668k, Voyager tem TRF1 quase completo → maior N treinável (dezenas
  de milhares a >100k conforme o overlap de floor).
- **TJSP**: Falcon 292k mas Voyager só ingeriu TJSP a partir de 2026-05-24 → só a
  fração recente casa (mesmo padrão do join da extração: 83,3k → 11,5k matched).
- **TRF3**: Falcon 126k, enricher recente → pouco match hoje, cresce com ingestão.
- **TRF4/TRF6/TJMG**: label Falcon existe, feature pública parcial.

> Nota operacional: a contagem exata `Falcon∩Voyager∩feature-suficiente` por tribunal
> é um join de 1,3M × 75,6M que satura o DB de prod (I/O-bound, contende com o batch —
> ver memória `db-contencao-busca-vs-batch`). Deve ser materializada **offline / na
> réplica read-only**, não em query ad-hoc na prod. O número não altera o veredito:
> o gargalo é o floor de ingestão do Voyager, não a disponibilidade de label.

**Interpretação:** o N de treino "com autos-truth rica de extração LLM" é modesto
(~37k, dominado por TRF1). Mas o **label real do modelo é o Falcon**, e o join
Falcon×Voyager é a fronteira que importa — muito maior, mas limitado pelo floor de
ingestão do Voyager por tribunal (TJSP/TRF3 têm label Falcon mas não têm feature
pública no Voyager pra a maioria dos processos antigos).

---

## (d) Diagnóstico do VIÉS DE SELEÇÃO (crítico)

**O viés é real e severo. Um modelo treinado só nos autos-extraídos vai dizer
"tudo é lead".**

Evidências:
1. **Os autos foram escolhidos** — a firma baixou o que parecia lead. No corpus de
   extração: `estagio_sinal` = PRECATORIO 79k + PRE 22k, e `DESFECHO_NEGATIVO = 0
   eventos`. **Não há negativo nos autos extraídos.** natureza é 93% ALIMENTAR.
2. **Overlap autos-extraídos × LeadConsumption** (dos 100k matched):
   **validado 65.084 (≈65%) · sem_expedicao 10.579 (≈11%) · pendente 12.416 ·
   sem-consumo 12.236**. Ou seja: a base de autos é **~65% positiva confirmada, só
   ~11% negativa** — prevalência ~6× a de um universo aleatório.
3. Se o modelo só vê essa base, aprende P(lead)≈0,65 como prior → otimista.

**Temos negativos suficientes? SIM — mas fora dos autos-extraídos, no Falcon e no
LeadConsumption:**
- **Falcon `sem_expedicao` = 276.552** processos (todas as linhas sem expedição) +
  **`not_found` = 55.116** + **`is_extinto` = 129.193** → **~330k+ negativos limpos**
  com label estruturado (união parcial; extinto pode sobrepor com sem_exp).
- **LeadConsumption `sem_expedicao` = 92.977** (independente, reportado pelo cliente).
- Esses negativos têm CNJ → dá pra puxar feature pública do Voyager onde o processo
  existe (mesma limitação de floor por tribunal).

**Conclusão do viés:** os POSITIVOS abundam (Falcon com expedição ~1,05M) e os
NEGATIVOS existem em volume (~331k Falcon + 92,9k Juriscope) — **mas só se o
treino usar Falcon+LeadConsumption como fonte de label, NÃO a extração LLM de 176k
(que é quase toda positiva).** Treinar na extração LLM = garantia de modelo otimista.

---

## (e) Targets previsíveis vs não (teto honesto)

| Target | Previsível do público? | Teto honesto |
|---|---|---|
| **é-lead / desfecho binário** | **SIM** | v6 já faz AUC 0,96. Ganho marginal com negativos Falcon + sequência de movs. **Recomendado.** |
| **estágio** (DC/PRE/PRECATÓRIO) | **Provável** | Os marcos (expedição, trânsito) estão nas movs públicas; F1/F2/F11/F20 já capturam. Multi-classe é natural. **Recomendado.** |
| **evento óbito/cessão/trânsito** | **Parcial** | Trânsito aparece em mov pública; óbito/cessão raramente (vivem nos autos). **Abster onde não há sinal público.** |
| **natureza** (ALIMENTAR/COMUM) | **DUVIDOSO** | [[dspy-natureza-gepa]]: otimizar natureza deu **Goodhart** (+30pp vs Falcon, −37pp vs texto). O próprio Falcon é silver (52% None). Do público é ainda mais fraco. **Abster / não prometer.** |
| **valor-exato** | **NÃO** | `valor_causa` público tem cobertura ~2% e é valor-da-causa, não do precatório. Valor real só no Falcon. **No máximo faixa grosseira via proxy (classe+ano+ente), com incerteza alta.** |
| **valor-faixa** | **Talvez (fraco)** | Proxy de faixa (log-bucket) treinável no Falcon como label, mas features públicas são fracas pra magnitude. **Abster ou reportar só faixa larga + calibração.** |

Régua honesta (memórias): **90% do erro de extração é do LLM, não do retrieval**
([[observabilidade-eval-rag]]); Falcon é silver, não gold. Qualquer target onde só o
Falcon dá label deve ser gateado por régua triangulada, senão vira Goodhart.

---

## (f) Modelo recomendado + feature set + plano de validação

**Modelo:** começar com **GBM (LightGBM) multi-target** (um modelo por target, ou
multiclass onde faz sentido), NÃO sequência de movs de largada.
- Razão: o v6 (LR linear) já dá AUC 0,96 no é-lead com features tabulares; GBM captura
  as interações não-lineares (F1×F15 etc.) sem engenharia manual, treina em minutos,
  calibra bem (isotônica/Platt) e roda barato no batch de classificação existente.
- Sequência de movs (LSTM/Transformer sobre a lista de `tipo_comunicacao`) é o **passo
  2**, só se o GBM tabular saturar — o gargalo aqui é label/viés, não capacidade de
  modelo (mesma lição da extração: [[extracao-metadados-autos]]).

**Feature set (tudo público, já extraível):**
- Tabulares do v6 (F1-F23) + one-hot de classe/assunto TPU.
- Sequência agregada: contagem por `tipo_comunicacao`, n-gramas de transição top-K,
  deltas temporais entre marcos (autuação→trânsito→expedição→pagamento/extinção).
- Marcos determinísticos já no SQL do classificador (expedição, alvará, sequestro,
  extinção — F2/F20/F24/F30).
- Partes: contagem, presença de ente público no polo passivo (proxy de Fazenda).

**Labels (a fonte que evita o viés):**
- Positivo = Falcon com expedição/requisitório; Negativo = Falcon `sem_expedicao` ∪
  `not_found` ∪ LeadConsumption `sem_expedicao`. **NÃO usar a extração LLM 176k como
  base de treino do binário** (usar no máximo pra targets ricos: estágio/eventos).

**Plano de validação/calibração (numa amostra representativa do UNIVERSO, não só dos
autos-mapeados):**
1. **Hold-out por hash-CNJ** (40/25/35, split determinístico como em
   [[dspy-natureza-gepa]]) — nunca aleatório sobre a base enviesada.
2. **Reweighting / prior-correction**: treinar com a prevalência real estimada do
   universo (não os 65% da base de autos). Calibrar por decil e checar contra
   `LeadConsumption` (fonte independente do Falcon).
3. **Amostra representativa do universo**: sortear N processos aleatórios do universo
   75,6M (não da base de autos), classificar, e validar contra Falcon onde houver
   match — mede o gap de distribuição (o que a base de autos esconde).
4. **Gate triangulado** (obrigatório em natureza/valor): comparar contra Falcon E
   contra sinal-de-texto; BLOCK se subir vs Falcon mas cair vs texto (Goodhart).
5. **Calibração isotônica** + reliability plot por tribunal, como o v6 já faz.

---

## (g) VEREDITO honesto

**Dá pra treinar um GBM forte JÁ — mas SÓ para os targets é-lead e estágio, e SÓ se
o label vier de Falcon + LeadConsumption (não da extração LLM de 176k).**

- **é-lead / estágio: SIM, já.** Positivos abundam (~1,05M Falcon), negativos existem
  (~331k Falcon + 92,9k Juriscope), features públicas cobrem o universo (classe 86-98%,
  movs 100%). O join Voyager limita o N por tribunal (floor de ingestão), mas TRF1
  sozinho dá base sólida e o resto entra conforme a ingestão avança. Ganho sobre o v6:
  multi-classe de estágio + uso estruturado dos negativos.

- **natureza / valor-exato: NÃO — abster.** natureza é Goodhart-prone e o próprio
  Falcon é silver binário; valor real não existe no público. Prometer esses targets =
  entregar lixo calibrado bonito.

- **⚠️ Armadilha do viés:** se alguém treinar "no dataset de autos" (os 176k), o modelo
  vai ser otimista (base 65% positiva, 0 desfecho-negativo). **A tarefa NÃO está
  bloqueada por falta de negativos** — os negativos existem no Falcon/LeadConsumption.
  Está bloqueada por **usar a fonte de label errada**. Pré-requisito antes de treinar:
  montar o dataset de label a partir de Falcon (pos + `sem_expedicao`/`not_found`) e
  LeadConsumption, com prior-correction pra prevalência do universo.

- **Não precisa colher mais label pra o binário.** Precisa (i) montar o dataset
  Falcon+LeadConsumption com negativos, (ii) definir o hold-out por hash-CNJ, (iii)
  calibrar contra a fonte independente. Para estágio, a extração LLM (101k
  `estagio_sinal`) é utilizável como label rico, ciente do viés de prevalência.
  Para natureza/valor: **não deployar sem κ humano** (o `ProcessoValidacao` está vazio
  — é o próximo gargalo pra qualquer target silver).

**Ordem recomendada:** (1) GBM binário é-lead com labels Falcon+LC → compara com v6
como sanity; (2) GBM multiclasse de estágio; (3) parar aí até κ humano existir pra
natureza. Sequência-de-movs e valor-faixa ficam como P2 experimental.
