# Diários oficiais além do DJEN — a terceira porta

> **Estado em 20/08/2026:** código escrito, validado contra as fontes reais no
> stack **dev**, com 187 testes verdes. **Não está ligado em produção** e o
> agendamento nasce DESLIGADO (`DIARIOS_SCHEDULER_ENABLED=False`). O que falta
> para ligar está em [§8 Runbook — ligar em produção](#8-runbook--ligar-em-produção).

> **Por que existe:** a ingestão é DJEN-only e o DJEN é veículo de **comunicação**
> (Res. CNJ 455/2022). Medido por amostra de 300 CNJs por tribunal (15-16/08/2026),
> falta de **81% (TRF1) a 96% (TJSP)** do acervo declarado ao CNJ. O achado que
> motivou esta porta: no TJSP, **96,5%** dos processos do sistema SAJ estão
> ausentes contra **52,9%** do Projudi — mesmo tribunal. Não é falha de ingestão:
> é que o TJSP publicava no **DJE próprio (e-SAJ)**, não no DJEN.

Portas do acervo, para não confundir:

| Porta | O que traz | Onde está documentada |
|---|---|---|
| 1. DJEN | movimentação com texto verbatim, do que foi publicado no diário **nacional** | [`INGESTION.md`](INGESTION.md) |
| 2. Varredura Datajud | **esqueleto** nacional (classe/assunto/órgão), sem parte e sem valor | [`ACERVO_CNJ.md`](ACERVO_CNJ.md) |
| 3. **Diários próprios** | movimentação com texto verbatim de **antes** de o tribunal aderir ao DJEN, + STF (que nunca aderiu) | **este arquivo** |

---

## 1. O que cada fonte dá — e o que cada uma NÃO dá

Tudo abaixo é MEDIDO contra a fonte viva em 16/08/2026, não estimado.

### `tjsp-dje` — DJE do TJSP (e-SAJ)

| | |
|---|---|
| **Dá** | 4.162 edições, 2007-10-01 → 2025-07-22, num catálogo de **1 requisição**. Publicação com classe, órgão, advogados+OAB, texto verbatim, permalink de página, e o **de-para CNJ ↔ numeração pré-2010** (que não existe em nenhum outro lugar do sistema) |
| **Volume real** | **29.136 movimentações** em UM caderno de UM dia (10/08/2016). Um dia inteiro de 2015 (7 cadernos) ≈ 100-150 mil linhas |
| **Cobertura de segmentação** | 99,7% (2015), 99,1% (2025), 96,1% (caderno 11) dos CNJs impressos caem dentro de um bloco |
| **NÃO dá** | **partes no formato `ato`** (a relação de intimações da 1ª instância — 22.339 dos 28.083 blocos): não há marcador entre a última parte e o despacho, e chutar produziria "Vistos" como nome de parte. O nome fica no texto, para o extrator de autos resolver |
| **NÃO dá** | o **caderno 5** (Editais e Leilões, `cdCaderno=14`) — fica fora do catálogo. Medido: o segmentador cobre 4,7% dos CNJs dele contra 99,7% no caderno 12. Coletar assim seria gravar lacuna e chamar de acervo |
| **NÃO dá** | a **era pré-CNJ**. 15/06/2009 → 0 CNJ e 8.862 números antigos; 15/06/2010 → idem. A unidade fecha como `sem_aproveit` (ver §4). O penhasco é **2010→2011**, não 2012→2013: 15/06/2011 rendeu 18.449 itens a 99,5% e 15/06/2012, 16.205 a 99,6% |
| **Texto** | limpo desde **20/08/2026**. O espaço espúrio ("Estado de S ão Paulo", "Banco Rodoben s S/A") **não era do PDF**, era do extrator: trocado o pypdf pelo PyMuPDF, `Paul o` some de 579 ocorrências para 0 e `S ão` de 27 para 0 no caderno de 21/07/2025. A `CNJ_TOLERANTE` continua sendo a defesa (4,10% dos CNJs ainda saem partidos, agora por quebra de LINHA real, que é verdade impressa). Ver ADR-031 |
| **Achado operacional** | **o arquivo do e-SAJ está FECHADO**: a última edição é a 4247, de 2025-07-22, e `datasSemDiario` lista os 390 dias seguintes. É backfill **finito**, sem fronteira diária |

### `dejt` — Diário Eletrônico da Justiça do Trabalho (TST + 24 TRTs)

| | |
|---|---|
| **Dá** | inventário de 18 anos (95.679 linhas) numa requisição. Publicação com partes+papel, advogados+OAB, órgão (do outline do PDF), tipo, texto verbatim. Gabarito **da própria fonte** por edição (a pesquisa avançada declara "1 até 20 de 16.717") |
| **Qualidade medida** | TRT3 10/07/2024 (62,7 MB, 13.853 páginas): 18.768 matérias contra 16.717 declaradas; **96,7% com partes**, 96,8% com advogado+OAB. Validação cruzada em TRT16/TRT8/TRT22: 99-106% do gabarito |
| **NÃO dá** | **a era pré-PJe (2008-2017)**. Medido contra o gabarito da própria fonte: 2013 = 0%, 2014 = 22%, 2016 = 72%, 2017-03 = 83%, 2018+ ≥95%. Não é bug de deploy, é outro formato (prosa corrida numerada). O coletor **abstém ANTES do download** e o catálogo nem matricula essas edições. **49% do inventário (46.845 edições) fica de fora** até alguém escrever o parser da era 2 |
| **NÃO dá** | `nome_classe`/`codigo_classe` — o caderno traz só a sigla ("ROT", "AIRR"), e escrever isso no campo contaminaria `Process.classe_nome` via `preencher_classe_via_djen`. A sigla fica verbatim no texto |
| **NÃO dá** | `link` (não existe URL estável), `data_envio`, `id_orgao`, e **polo em papel recursal** (num recurso a fonte não diz de que lado está — o `papel` vai verbatim) |
| **NÃO dá** | CSJT e ENAMAT (1.072 + 67 edições): publicam no DEJT mas não existem como `Tribunal` no Voyager. Ficam contados no log |
| **Achado** | cadernos de 2008-2012 vêm embrulhados em **PKCS#7** com `Content-Type: application/pdf` mentindo. `desembrulhar_assinatura()` recorta do `%PDF` ao `%%EOF`. NÃO validamos a assinatura |
| **Custo** | o gargalo é **CPU de extração de texto**, não rede: 229 s para extrair o texto de um caderno-dia do TRT3 e 6,6 s para segmentar. É trabalho de frota. O DEJT **continua no pypdf** — a troca por PyMuPDF do `tjsp-dje` (ADR-031) não foi estendida aqui: o uso é outro (`extract_text()` achatado + `outline`) e o risco é em `nome_orgao`/`tipo_comunicacao`. Ganho estimado maior que o do TJSP; é outra mudança, com outro gate |

### `stf` — DJe do STF (API JSON de digital.stf.jus.br)

| | |
|---|---|
| **Dá** | ~590 publicações/dia útil com **relator** identificado e **envolvidos** estruturados com polo, categoria e OAB — dois dados que hoje não existem em lugar nenhum do acervo. Texto nativo (zero OCR). É onde sai a tese de repercussão geral que decide precatório |
| **Horizonte** | primeira divulgação = **2020-09-01**. Completo de 2023 em diante; **2021-2022 é parcial** (justo onde o DJe em PDF do portal legado ainda vive). Série longa exige costurar as duas fontes |
| **NÃO dá** | **o CNJ**. Ele vem do portal legado (IIS/ASP), um GET por publicação. ~5-10% respondem "Sem número único" e **não são gravadas** (entram no balanço e no log, nunca no banco com CNJ chutado) |
| **NÃO dá** | `data_envio`, `link`, `codigo_classe` (o código TPU não é conhecido; escrever "ARE" ali poluiria o catálogo) |
| **Gargalo** | o portal legado, não a API: 1 dia útil sai da API em 2 requisições (~2 s) e exige ~590 GETs a 0,8 rps ≈ **12 min/dia** com cache frio. Backfill 2020-09→hoje (~450k publicações) é projeto de dias |
| **Semântica pendente** | o CNJ é o de **ORIGEM** em 28% das linhas medidas (8.16, 8.26, 4.03...). Ver §5 |

### `qd-municipal` + `doe-sp` — diários de ENTES DEVEDORES (app `diarios_entes`)

| | |
|---|---|
| **Dá** | **sinal de desfecho**: o ente devedor convocando o credor. O achado: a Câmara de Conciliação de Precatórios de Maceió publica tabela `PARTE × PRECATÓRIO Nº × HORÁRIO DA SESSÃO` — nome do credor + CNJ + data do acordo. Lead puro, e não existe no DJEN |
| **NÃO É** | porta de acervo. 0 de 30 publicações aleatórias do DOE-SP têm CNJ; 2-3 gazetas/dia no país com precatório **e** CNJ, contra ~3.000 movimentações/**minuto** do DJEN. Há teste travando essa expectativa |
| **NÃO grava** | em `Movimentacao` — grava em `diarios_entes.PublicacaoOficial`. Consequência: **não aparece em nenhuma métrica de ingestão existente** (heatmap, lag, `mv_pipeline_diario`, `IngestionRun`) |
| **NÃO dá** | advogado/OAB (o Executivo não lista), tribunal, classe, `numero_comunicacao`. `orgao` é órgão do EXECUTIVO, não juízo |
| **Guarda só a janela** | ±6.000 chars em torno das âncoras (17.711 de 482.437 em Maceió) + `link_texto` com o integral. Média de 772.500 chars por gazeta (a do Rio tem 6,6 MB) contra um Postgres já disk-I/O-bound |
| **Cobertura do QD é estagnada** | é raspador de ONG, sem SLA: 3 dos 28 maiores municípios não têm gazeta nenhuma (Fortaleza tem `level="1"` e ZERO gazetas) e 15 estão defasados — **São Paulo capital parou em 24/03/2025**. Por isso `manage.py entes_frescor` existe: o selo vem de medição, nunca do campo `level` |
| **Fora de escopo** | RS, MG, BA, PR, RJ, CE, PE, SC — cada um custa engenharia própria (TLS quebrado, NXDOMAIN, sessão PHP) para <0,2 documento/dia |

### STJ — **não é fonte nova**

O STJ aderiu ao DJEN em 29/11/2024 e o payload bate 100% com `djen/parser.py`
(200 itens conferidos chave a chave, zero drift; a única chave extra,
`dataenvio`, já está em `OPTIONAL_KEYS`). Reconferido ao vivo: `siglaTribunal=STJ`
→ HTTP 200 com as 24 chaves; `STF` → HTTP 500, o **mesmo erro de uma sigla
inventada**.

Entregue: `tribunals/migrations/0049_stj_horizonte_djen.py` (grava só
`data_inicio_disponivel=2024-11-18`, medido por bissecção — há publicação ANTES
da data oficial da adesão) + `manage.py djen_ligar_stj`, que **não grava sem
`--confirmar`**. Ligar `ativo=True` numa migration foi recusado de propósito: o
`watchdog_ingestao` dispararia `tick_backfill_retroativo` na hora e jogaria
~5,3M de movimentações na fila, disputando I/O com extração e vetorização.

---

## 2. Quanto de acervo isto promete recuperar — e o que NÃO foi medido

| Fonte | Jazida declarada | O que o código entrega hoje | Medido? |
|---|---|---|---|
| `tjsp-dje` | 4.162 edições (2007-10-01 → 2025-07-22) | ~2.900 edições de 2011 em diante, ~29 mil linhas por caderno-dia ⇒ **ordem de 400-600 milhões de linhas** | volume sim, recuperação de acervo **NÃO** |
| `dejt` | 95.679 edições (2008 → hoje) | **48.834** (2018-01-01 → 2024-07-31). As 46.845 pré-2018 exigem outro parser | volume sim, recuperação **NÃO** |
| `stf` | ~450k publicações (2020-09 → hoje) | tudo, menos os ~5-10% sem número único | sim (é fonte pequena) |
| entes | 2-3 gazetas/dia no país | idem | sim |

**A honestidade que falta, e é importante:** ninguém mediu **quanto do acervo
declarado ao CNJ isto recupera**. Foram medidos volume de publicação e cobertura
de segmentação — não sobreposição com o que já temos. O banco de dev tinha ZERO
movimentação de DJEN para o TJSP, então os `processos_novos` das coletas de
validação provam só que o dev estava vazio.

Antes de dimensionar o backfill, é preciso uma consulta **barata** em produção:
sortear N CNJs de um caderno já coletado e medir quantos já existem. Sem isso,
"o TJSP vai de 3,5% para X%" é chute.

E o limite estrutural continua de pé: o DJE também é veículo de **comunicação**.
Processo em segredo de justiça ou sem ato publicado permanece invisível pelas
três portas.

---

## 3. Arquitetura

```
diarios/                              ← COMPARTILHADO
├── base.py            CONTRATO: ColetorDiario, UnidadeColeta, SessaoDiario,
│                      external_id/fingerprint, dv_cnj_valido/achar_cnjs,
│                      validadores de "200 que não é dado", kill switch,
│                      runner (catalogar_fonte / coletar_unidade), registro
├── models.py          EdicaoDiario — catálogo + watermark por unidade
├── jobs.py            catalogar / coletar / tick / tick_todas / catalogar_fronteira
├── apps.py            auto-descobre diarios/fontes/*/coletor.py
└── management/commands/
    ├── diarios_coletar.py   entrada única (--dry-run, --catalogar, --chave, --limite)
    ├── diarios_status.py    snapshot JSON por fonte
    └── diarios_pausar.py    KILL SWITCH (ver §6)
diarios/fontes/{tjsp_dje,dejt,stf}/   ← uma fonte por diretório
diarios_entes/                        ← app próprio (model próprio: PublicacaoOficial)
```

Não há arquivo central de registro: `@registrar` + auto-descoberta no `apps.py`.
**Alterar `diarios/base.py` para implementar uma fonte é sinal de que o contrato
está errado** — abrir discussão, não acrescentar `if slug ==`.

### Como cai no que já existe

| Peça existente | Como é reusada |
|---|---|
| `Movimentacao` | destino de todas as fontes com tribunal. Sem coluna nova (são ~65M linhas num Postgres disk-I/O-bound) |
| `IngestionRun` | 1 run por unidade, agora com **`fonte`** (migration `tribunals/0048`) |
| `Process` | criado por CNJ inédito, igual ao DJEN |
| `ProxyScrapePool` | `SessaoDiario` usa o mesmo pool — mas **nenhuma** fonte precisou de proxy (todas responderam de IP datacenter sem 403) |
| circuit-breaker | mesma mecânica do `djen/client.py`, com chave **por fonte** |
| `SchemaDriftAlert` | para as APIs não-contratadas (STF e DOE-SP foram descobertas por reversa de bundle JS) |
| `_dia_coberto` | a lição (ausência ≠ falha) virou os status `inexistente` e `sem_aproveit` do `EdicaoDiario` |

### `IngestionRun.fonte` — por que foi preciso mexer no model compartilhado

Sem o campo, um run do DJE/TJSP na janela do dia X faria o `_dia_coberto` do
DJEN considerar o dia coberto e **pular a ingestão** — perda silenciosa. O
default `'djen'` mantém 100% da história correta sem backfill, e as leituras de
cobertura/lag do DJEN (`djen/jobs.py`, `dashboard/queries.py::cobertura_temporal`
e `pipeline_diario`) passaram a filtrar `fonte='djen'`. Regressão travada em
`tests/test_diarios_base.py::test_run_de_outra_fonte_nao_conta_como_cobertura_do_djen`.

### `FonteDiario` NÃO é o cadastro destes coletores

`tribunals.FonteDiario` é a tabela de compatibilidade **Jusbrasil/Digesto**: PK é
o `source_id` DELES, é `OneToOne` com `Tribunal`, e serve para preencher o campo
`source` dos documentos do ES (`search/documents.py`) e do payload de
monitoramento. Registrar `tjsp-dje` ali exigiria **inventar um source_id de
terceiro** e colidiria com a linha que o TJSP já tem (`source_id=18`); o `dejt`
nem caberia, porque cobre 25 tribunais e a relação é 1-para-1.

Efeito colateral bom: como as linhas `STF (25)` e `STJ (81)` já existem, as
publicações do STF que entrarem em `Movimentacao` já saem com `source: 25` no ES,
sem nada a fazer. O registro dos coletores é `@registrar` + `diarios_status`.

---

## 4. Estados do `EdicaoDiario` — a diferença entre lacuna e ausência

| Status | Significado | Retenta? |
|---|---|---|
| `pendente` | catalogada, ainda não coletada | sim |
| `ok` | coletada; `itens_gravados` = linhas que a unidade tem no banco | não |
| `vazia` | baixou, validou, **não havia** publicação | não |
| `inexistente` | a fonte diz que não existe unidade nesse dia (feriado forense) | **nunca** |
| `sem_aproveit` | **havia** publicação e **nenhuma** é aproveitável | **nunca** |
| `falha` | erro recuperável | sim, até `MAX_TENTATIVAS=5` |
| `fora_janela` | período coberto por outra porta (DJEN) | não |

`sem_aproveit` nasceu de um achado da verificação adversarial: o caderno 12 do
TJSP de 15/06/2009 tem **16.952 blocos de publicação real**, todos pré-CNJ. O
coletor descartava os 16.952 (correto) e fechava a unidade como `vazia`, com
`itens_gravados=0`, `ultimo_erro=''` e o log dizendo `cobertura de CNJ 0/0 =
100.0%`. Ou seja: 16.952 publicações viravam lacuna **invisível** com o run
verde. Os dois fatos agora são distintos, e o motivo fica escrito no
`ultimo_erro`.

---

## 5. Decisões que precisam de olho humano ANTES de ligar

1. **O CNJ de origem do STF (e do STJ).** Dos 225 CNJs do STF medidos, **63 (28%)**
   são de outros tribunais. Como `Process` é único por `(tribunal, numero_cnj)`,
   uma publicação do STF sobre um processo do TJPR cria
   `Process(tribunal=STF, numero_cnj=<CNJ do TJPR>)` **ao lado** do que o TJPR já
   tem: duas linhas para o mesmo processo, e toda métrica que assume
   `Process.tribunal == Movimentacao.tribunal` passa a mentir. É o mesmo nó dos
   "incidentes CNJ vinculados" do TJSP. O comportamento PADRÃO do contrato foi
   mantido e travado em teste, para a decisão ser consciente.
2. **Volume × particionamento.** 400-600 milhões de linhas do TJSP colidem de
   frente com `PARTICIONAMENTO_ACERVO_CHUNK.md` e com o Postgres já
   disk-I/O-bound. Ligar em série sem recorte (ex.: só 1ª instância 2013-2015, ou
   só CNJ de interesse) **não é decisão do coletor**.
3. **Sigilo no STF.** 24 de 226 publicações (10,6%) vêm marcadas
   `Segredo de Justiça`/`Sigiloso`, com corpo completo — a API pública serve, mas
   ela mesma marca a confidencialidade. Entrar no mesmo pool buscável sem
   tratamento merece decisão explícita.
4. **`texto` do STF é XHTML**, não texto puro como as ~65M linhas do DJEN: 24%
   do documento é `<head>`+CSS. Isso vaza HOJE para o CSV/XLSX do cliente
   (`dashboard/views.py`, `ultima_mov_texto = texto[:500]`), para o painel de
   movimentações e para o índice do ES (que não tem `html_strip`). Ou o coletor
   passa a gravar o `<body>` limpo, ou as três pontas precisam de `strip`.
   **Não resolvido** — está aqui porque é dívida conhecida, não surpresa.

---

## 6. Kill switch e conduta de rede

Nenhuma dessas fontes tem rate limit, WAF ou `robots.txt`. **O servidor não vai
nos defender de nós mesmos**: 765 GB puxados no talo de um JBoss 4.3.0.GA de 2010
do CSJT é negação de serviço acidental. O teto é nosso.

```bash
# PARAR AGORA (efeito em segundos, sem deploy — chave no Redis)
manage.py diarios_pausar --tudo
manage.py diarios_pausar dejt tjsp-dje      # ou só uma fonte
manage.py diarios_pausar --listar
manage.py diarios_pausar --religar --tudo
```

Mesma mecânica do `set_varredura_pausados` do Datajud, que existe pelo mesmo
motivo (a APIKey compartilhada do CNJ estrangula sem avisar). `checar_pausa` roda
**antes de cada requisição**; job que pega pausa devolve `{'skip': 'adiado'}`,
não empilha no `FailedRegistry` e **não conta tentativa** — religar retoma de
onde parou.

Camadas de contenção, da mais fina para a mais grossa:

| Camada | Onde | Efeito |
|---|---|---|
| `rps` por fonte | `DIARIOS_RPS_<SLUG>` (tjsp-dje 1,0 · dejt 0,5 · stf 1,0 · stf-portal 0,8 · qd 1,0 · doe-sp 2,0) | ritmo |
| circuit-breaker por fonte | `DIARIOS_CIRCUITO_*` | 15 respostas 5xx em 120 s ⇒ pausa 300 s, fast-fail sem tocar no servidor |
| teto de fila por fonte | `WATERMARK_POR_FONTE=200` em `diarios/jobs.py` | fairness (lição do incidente 2026-07-29) |
| recorte de fontes agendadas | `DIARIOS_FONTES_AGENDADAS` | liga UMA fonte por vez |
| chave geral | `DIARIOS_SCHEDULER_ENABLED` | agendamento inteiro |
| **kill switch** | `diarios_pausar` | para tudo, em segundos |

Sessão sticky: o DEJT prende o `JSESSIONID` a um backend do ALB e usa
`conversationId` do Seam — se um dia precisar de proxy, tem que ser
`PROXY_PRESO`; rotação por request (o padrão do DJEN) é **anti**-padrão aqui.

---

## 7. Deduplicação — o mesmo ato pelas duas portas

Três camadas, da mais barata para a mais cara. **Só a primeira é obrigatória.**

1. **Janela de exclusividade** (`janela_inicio`/`janela_fim`). Cada fonte declara
   o período em que é a única porta; fora dele o runner recusa (`--sobrepor` para
   forçar). É medição, não arbítrio: DJE/TJSP `2007-10-01 → 2025-03-13`, DEJT
   `2008-06-09 → 2024-07-31`. No caso normal a interseção é vazia e **não há o
   que deduplicar**.
2. **Namespace no `external_id`** — `<slug>:<coordenada>-<sha1_12(texto)>`. O
   DJEN é o namespace **legado, sem prefixo** (re-prefixar 65M linhas quebraria a
   idempotência da ingestão corrente). O hash do CONTEÚDO, em vez do ordinal do
   bloco, é o que impede a re-segmentação de duplicar a edição inteira.
3. **Fingerprint** — `Movimentacao.hash = fingerprint_ato(cnj, data, texto)`
   pareia o que coexiste; a leitura deduplica com
   `DISTINCT ON (processo_id, hash)`. Ressalva honesta: **não casa com o legado
   do DJEN**, que guarda ali o hash opaco da API. Por isso quem decide é a
   camada 1. Ressalva nova: no STF o `<body>` do XHTML começa depois do char
   4.345 e a janela do fingerprint é de 4.000 — o hash saía do CSS e 8,8% das
   publicações colidiam. **Corrigido em 16/08/2026**: no STF o fingerprint é
   calculado sobre o `<body>` (`_corpo_visivel`), e o `texto` gravado continua
   verbatim com o documento inteiro.

Quando o mesmo ato vem pelas duas portas, **as duas linhas ficam** — dois
veículos, dois verbatim, duas evidências — e o runner conta `espelhadas`.
Ingestão é append-only; conflito quem resolve é a leitura.

---

## 8. Runbook — ligar em produção

**Pré-requisitos que ainda não estão prontos:**

1. **`pymupdf` NÃO está na imagem — rebuild de `web` E dos workers.**
   Conferido em 20/08/2026 no `voyager-web-1` de prod (192.168.30.103):
   `import pypdf` → 5.1.0 (o pré-requisito antigo, "pypdf não está na imagem",
   estava **desatualizado**), `import pymupdf` → `ModuleNotFoundError`. Desde a
   ADR-031 o `tjsp-dje` lê o caderno com PyMuPDF, então sem rebuild ele levanta
   `RuntimeError` com mensagem explícita na coleta (o import é tardio e
   embrulhado de propósito, para não sumir do registro em silêncio no
   `apps.py`). **Bloqueia deploy do `tjsp-dje`.** O `dejt` segue no pypdf e não
   é afetado.
2. **Migrations: aplicadas.** Conferido em prod em 20/08/2026 —
   `diarios/0001_initial`, `diarios_entes/0001_initial`,
   `tribunals/0048_ingestionrun_fonte` e `tribunals/0049_stj_horizonte_djen`
   aparecem `[X]` no `showmigrations`. Nada a rodar aqui.
3. **Worker da fila `diarios`** (não existe hoje). Fila separada de propósito: a
   unidade aqui é um caderno de até 2.001 páginas / 62 MB, e um job desses na
   fila do DJEN empurraria a fronteira diária para o fim da linha.
4. **Decidir §5.1 (CNJ de origem)** antes de ligar `stf` ou o STJ.
5. **Medir sobreposição com o acervo de produção** (§2) antes de dimensionar.

**Sequência sugerida — uma fonte por vez, começando pela menor:**

```bash
# 0. antes de tudo: o kill switch responde?
manage.py diarios_pausar --listar

# 1. começar pelo STF (menor volume, sem PDF, sem pypdf)
DIARIOS_SCHEDULER_ENABLED=1 DIARIOS_FONTES_AGENDADAS=stf   # no .env do scheduler

# 2. observar por 1 dia:
manage.py diarios_status stf
#    · runs_falha tem que ficar em 0
#    · por_status não pode encher de `inexistente` (isso é sinal de fonte mudada)

# 3. só então acrescentar a próxima fonte em DIARIOS_FONTES_AGENDADAS
```

**Coleta manual (o caminho que NÃO depende do scheduler):**

```bash
manage.py diarios_coletar <slug> --de D --ate D --dry-run     # catálogo, sem gravar
manage.py diarios_coletar <slug> --de D --ate D --catalogar   # vira EdicaoDiario
manage.py diarios_coletar <slug> --limite 1                   # coleta 1 unidade
manage.py diarios_coletar <slug> --chave <chave>              # coleta uma específica
manage.py diarios_status <slug>
manage.py entes_frescor                                       # selo de frescor do QD
```

**Sintomas e o que significam:**

| Sintoma | Leitura |
|---|---|
| `por_status.inexistente` crescendo numa fonte que vinha bem | **layout da fonte mudou**. No DEJT isso agora vira `RespostaInvalida` + `SchemaDriftAlert` para unidade catalogada; se aparecer mesmo assim, é fonte com edição removida |
| `sem_aproveit` no `tjsp-dje` | era pré-CNJ. Esperado abaixo de 2011 |
| `falha` com "cobertura ... abaixo do piso" | o gate funcionou: a segmentação piorou (ou a fonte mudou de formato). **Não** baixar `DIARIOS_COBERTURA_MINIMA` sem medir |
| fila `diarios` parada com circuito aberto | a fonte está 5xx-ando; o breaker está fazendo o trabalho. Esperar o cooldown |
| `SSLError` no `stf` | o intermediário AlphaSSL do bundle expira em **21/05/2027**. Trocar por `DIARIOS_STF_CA_BUNDLE`, sem deploy |

---

## 9. Critério de aceite (o que foi cobrado de cada fonte)

Comum a todas: `--dry-run` → `--catalogar` → `--limite 1` **duas vezes** (a
segunda com `novas=0`) → `diarios_status` → `pytest tests/test_diario_<slug>.py`;
teste com **fixture real capturada**; teste que rejeita a **casca** da própria
fonte; `janela_*` com data medida; zero alteração em arquivo alheio.

| Fonte | Gate mecânico | Resultado |
|---|---|---|
| `tjsp-dje` | catálogo = 4.162 edições em 1 request; ≥95% dos CNJs tolerantes segmentados; **canário do extrator**: ≥32.000 CNJs distintos pela regex ESTRITA no caderno de 4.229 páginas | **99,1%** (2025), **99,7%** (2015), **96,1%** (caderno 11), **99,751%** (Capital Parte I de 12/03/2025, 4.229 páginas). Canário reconferido em 20/08/2026 no container de dev, mesmo arquivo, dois extratores: PyMuPDF **32.366** estritos / 32.575 tolerantes / 44,8 s / RSS 185 MB contra pypdf **27.483** / 32.575 / 184,1 s / RSS 425 MB; por linha, **23** com CNJ partido contra **5.866**. 39 + 11 testes |
| `dejt` | ≥95% das 16.717 matérias que a fonte declara para o TRT3 em 10/07/2024; prova de que não usa `j_id` literal | **18.768** (112%). 57 testes |
| `stf` | 1 dia útil em ≤2 requests; `total` == ids distintos; resolver devolve `0000876-17.2013.8.16.0021` para `ARE 1617690`; publicação sem CNJ não é gravada | passou. 29 testes |
| `diarios_entes` | model próprio; `Terms` contra `SearchTerms` (117 contra 487.579); rejeição da SPA do RS; os 3 CNJs de Maceió | passou. 25 testes |

O gate **não é decorativo**: reprovou de verdade o caderno 11 do TJSP a 78,6% na
primeira coleta ao vivo (e foi assim que dois formatos desconhecidos apareceram),
e a edição `J-TRT22-2010-03-10-436` está no banco de dev com `falha` e
`cobertura 0/418 abaixo do piso`.

---

## 10. Limitações conhecidas que NÃO foram consertadas

Registradas para não virarem surpresa. Nenhuma bloqueia, todas custam.

| Onde | O quê |
|---|---|
| `stf` | `texto` é XHTML inteiro e vaza para CSV/painel/ES (§5.4) |
| `stf` | `nome_orgao` guarda o **relator** (226/226 linhas) num campo que a dashboard faceta como órgão julgador; `status` guarda "Público"/"Segredo de Justiça" onde o DJEN grava "P" |
| `stf` | drift no HTML do portal legado vira abstenção em massa (`cnj=None`) sem alerta — o `SchemaDriftAlert` só está ligado na API |
| `stf` | cache incidente→CNJ vive no `django.core.cache`; qualquer `cache.clear()` (há testes que fazem isso) apaga |
| `dejt` | `esperado()` é **piso**, não igualdade: produzimos 105-164% do gabarito porque uma "matéria" do tipo Pauta lista N processos e emitimos 1 `Movimentacao` por processo. O gate só reprova para baixo |
| `dejt` | 1,2% (TRT3) a 11% (TRT22/2018) dos blocos são o mesmo ato impresso em páginas diferentes ⇒ 2 linhas com o mesmo `hash`. A leitura deduplica; `itens_gravados` infla |
| `dejt` | só o caderno **Judiciário** foi validado; o Administrativo está suportado no código e não foi medido |
| `tjsp-dje` | `nome_orgao` sem comarca em 26% de um caderno do interior ("VARA ÚNICA" solto) |
| `tjsp-dje` | nome de advogado sujo em 8 de 5.390 quando a inscrição tem letra colada ("99999D/SP - Defensoria...") |
| `tjsp-dje` | a troca de extrator (ADR-031) muda o `texto` (quebra de linha diferente) e portanto **todo `external_id`/`hash`**: recoletar unidade já coletada com pypdf GRAVA DE NOVO em vez de contar `dup`. Em prod é inócuo (0 `IngestionRun` não-djen, 0 `EdicaoDiario`); no **dev** há 5 unidades `tjsp-dje` `ok` e 140.313 movimentações do extrator antigo — purgar antes de recoletar |
| `tjsp-dje` | PyMuPDF é **AGPL-3.0** (ou licença comercial da Artifex) num serviço em rede proprietário. Uso é interno (worker de coleta), o que mitiga mas não é parecer jurídico. Saída BSD se reprovar: `pypdfium2` com a escada de tamanhos reconstruída da caixa do glifo (ADR-031) |
| `tjsp-dje` | `conferir_data` é **no-op no caderno grande**: a página 1 do Capital Parte I é capa e não tem 'Disponibilização:', e o coletor confere só a PRIMEIRA página. A única defesa contra 'baixei o dia errado' não dispara justamente onde mais importa. Pré-existente, não introduzido pela ADR-031 |
| `tjsp-dje` | `numero_comunicacao` guarda o número **como impresso** (com sufixo de incidente e número antigo entre parênteses) — uso diferente do campo em relação ao DJEN, que guarda inteiro |
| `qd-municipal` | um CNJ da tabela de Maceió está grafado com ponto no lugar do hífen **no original** e não é achado. É erro da fonte; corrigir seria chutar em cima de número de processo |
| `diarios_entes` | não emite `SchemaDriftAlert` (a tabela exige `tribunal` NOT NULL e esta fonte não tem tribunal). O drift vira exceção alta + log |
| todas | `janela_horaria` é **declarativa**: o runner não a lê. O teto real é `rps` + kill switch |

---

## 11. Onde tudo isso está

| Arquivo | O quê |
|---|---|
| `diarios/base.py` | contrato, runner, dedupe, kill switch, `dv_cnj_valido` |
| `diarios/models.py`, `diarios/jobs.py` | watermark e fila |
| `diarios/fontes/{tjsp_dje,dejt,stf}/` | uma fonte por diretório |
| `diarios_entes/` | app próprio dos DOEs de entes |
| `djen/scheduler.py` (fim do arquivo) | agendamento, atrás de `DIARIOS_SCHEDULER_ENABLED` |
| `core/settings.py` | bloco `DIÁRIOS PRÓPRIOS` |
| `tests/test_diarios_base.py` + `test_diario_{tjsp_dje,dejt,stf}.py` + `test_diarios_entes.py` | 176 testes (26 do contrato + 39 tjsp-dje + 57 dejt + 29 stf + 25 entes) |
| `tests/test_diarios_pymupdf.py` | 11 testes que travam a ADR-031: fluxo (não acúmulo), CNJ inteiro e erro explícito sem a lib. O canário do caderno de 36 MB **pula** sem a fixture pesada `tjsp_esaj/caderno3_capital_parteI_20250312.pdf` (fora do git, ver `tests/fixtures/diarios/README.md`) |
| `.ia/DECISIONS.md` ADR-030 | por que a terceira porta, e o que foi recusado |
