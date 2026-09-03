# Diários oficiais além do DJEN — a terceira porta

> **Estado em 24/08/2026: LIGADA — uma fonte de cinco.**
> `DIARIOS_SCHEDULER_ENABLED=1` com `DIARIOS_FONTES_AGENDADAS=tjsp-dje` e
> orçamento diário. As outras quatro continuam fora do agendamento, e cada uma
> tem um NÚMERO que a manteve de fora — o `dejt` está literalmente fora do ar
> (HTTP 503 em 4/4 sondas). Tudo em
> [§13 LIGADA — o que foi ligado, com que números, e quando desligar](#13-ligada--o-que-foi-ligado-em-24082026-com-que-números-e-quando-desligar):
> inventário medido, projeção de disco, tetos automáticos e critério de parada.
>
> O achado que dimensiona tudo: o backfill completo do DJE/TJSP projeta
> **~772 GB de índice** contra **1,0 TB livre** no nó de ES ⇒ levaria o disco a
> **88,6%**, e o `flood_stage` (95%) marca o índice `read_only_allow_delete`,
> derrubando a escrita das **três** portas. **Ligar a fonte, sim; abrir o
> backfill inteiro, não — antes de expandir o disco do ES.**

> **Estado em 21/08/2026 (histórico):** o agendamento estava DESLIGADO, mas
> **a primeira coleta real em produção aconteceu**, à mão: os 8 cadernos do
> DJE/TJSP de 12/03/2025, **220.544 linhas**. A INFRA da `.102` está de pé
> (`pymupdf` na imagem, `worker_diarios` com 2 réplicas).
>
> Essa primeira coleta revelou o buraco que **§12** documenta: a edição fechava
> `ok` e as linhas ficavam FORA do Elasticsearch, esperando um poller de 10
> minutos. **Coletado não era buscável, e nada no sistema dizia isso.**

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

> ⚠️ **A FONTE ESTÁ FORA DO AR desde 18/08/2026**, declarado pelo próprio CSJT.
> `dejt.jt.jus.br` responde 302 para uma página de aviso e o coletor recusa a
> tela (`sem javax.faces.ViewState`). Existe um espelho de contingência com o
> acervo inteiro — e ele traz um `robots.txt` com `Disallow: /`. Tudo em
> **[§19](#19-as-duas-portas-que-estavam-prontas-e-desligadas--por-quê-medido-03092026)**,
> junto com os dois eixos do gate que passaram a existir para esta fonte.

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
| `Movimentacao` | destino de todas as fontes com tribunal. Sem coluna nova (medido em 20/08/2026: **1,39 BILHÃO** de linhas, 815 GB de heap, num Postgres disk-I/O-bound — a doc dizia "~65M", 21× defasado) |
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
4. **`texto` do STF é XHTML**, não texto puro como as ~1,39B linhas do DJEN: 24%
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

O `--pausar` NÃO existe: a fonte vai como argumento POSICIONAL, e `--pausar`
faz o argparse recusar o comando inteiro (`unrecognized arguments`).

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
| teto de fila por fonte | `WATERMARK_POR_FONTE=200` em `diarios/jobs.py` | fairness (lição do incidente 2026-07-29). É PROFUNDIDADE de fila, não vazão |
| **orçamento de vazão** | `DIARIOS_TETO_UNIDADES_DIA[_<SLUG>]` | unidades fechadas por 24 h. **`0` = SEM TETO**; para frear, `1` (§13.3) |
| **guarda de disco do ES** | `DIARIOS_ES_DISCO_MAX_PCT` (85) | o tick para acima do `watermark.low`. Falha FECHADA |
| **guarda da fila do índice** | `DIARIOS_FILA_ES_MAX` (5.000) | o tick para com a `es_index` funda: coletar ali produz linha gravada e não buscável. Falha FECHADA |
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
   DJEN é o namespace **legado, sem prefixo** (re-prefixar 1,39B linhas quebraria a
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

> **Esta seção é HISTÓRICA.** O que está ligado hoje, com que orçamento e sob
> quais critérios de parada, está em
> [§13](#13-ligada--o-que-foi-ligado-em-24082026-com-que-números-e-quando-desligar).
> O que segue é o canário que abriu caminho até lá, e vale pelo método.

> **Veredito do canário de 20/08/2026: NÃO PASSOU — e o motivo não é o PDF.**
> A troca do extrator está de pé e conferida em produção; o que impede ligar é
> um índice AUSENTE no banco (pré-requisito 4). O gate dos 32.366 CNJs nunca
> chegou a ser medido ao vivo: a coleta travou ANTES, no `SELECT` de
> sobreposição. Nada foi persistido, nada duplicou com o DJEN, e o `tjsp-dje`
> está PAUSADO no kill switch — estado mais conservador que o inicial, de
> propósito. Reverter é `manage.py diarios_pausar --religar tjsp-dje`.

**Estado dos pré-requisitos — conferido em produção em 20/08/2026:**

1. **`pymupdf` na imagem: FEITO na `.102`, PENDENTE na `.103`.**
   O caso desta casa é **híbrido, e é isso que confunde**: o compose monta
   `.:/app` (o CÓDIGO é hot-deploy por bind mount, `git pull` basta), mas a
   dependência Python mora em `site-packages` DENTRO da imagem — `pip install`
   dentro do container morreria no primeiro `up -d --force-recreate`. Logo:
   `pymupdf==1.24.14` entra por `requirements.txt` + **rebuild da imagem**, e
   cada host constrói a sua (`build:` existe nos dois composes).

   Feito na `.102` (`voyager-workers`), onde a fila `diarios` roda:
   ```bash
   ssh ubuntu@192.168.30.102 'cd ~/voyager && git pull --ff-only && \
     docker compose -f docker-compose-workers.yml build worker_trf1'   # único serviço com build:
   ```
   Conferido no container vivo: `import pymupdf` → **1.24.14** (antes:
   `ModuleNotFoundError`), e o round-trip do módulo do coletor devolve
   `1127986-08.2023.8.26.0100` inteiro, sem o espaço espúrio.

   Detalhe que assusta quem for conferir: os **outros ~238 containers da `.102`
   continuam sem `pymupdf`**, porque foram criados ANTES do rebuild e um
   container vive na imagem que o criou. Isso é inócuo (só o `worker_diarios`
   lê caderno) e some sozinho no próximo `up -d --force-recreate`. Recriar 240
   workers no meio do voo só para uniformizar seria trocar um não-problema por
   uma janela de fila parada. A verificação que vale é na imagem:
   `docker run --rm --entrypoint python voyager-web:prod -c 'import pymupdf'`.

   **Pendente na `.103`** (`voyager-web-1` ainda dá `ModuleNotFoundError`).
   Não foi feito de propósito: o `web` de lá roda `migrate --noinput` no boot
   (o que esta casa proíbe fazer por deploy) e o checkout tem arquivos soltos.
   Conferido em 20/08/2026: os soltos da tela de completude (`completude_*.py`,
   `completude.html`, `dashboard/urls.py`) são **byte a byte iguais** ao que já
   está na `main` (md5 confere) — foram hot-deploy por cópia, não trabalho
   perdido. O `git pull --ff-only` lá é seguro; o que ele arrasta junto é o
   deploy da tela de completude, que é outra mudança. Enquanto não for feito, **rodar a coleta manual pelo container da
   `.102`**, não pelo `web`:
   ```bash
   ssh ubuntu@192.168.30.102 'docker exec voyager-worker_diarios-1 python manage.py diarios_coletar tjsp-dje ...'
   ```
   Quando for a hora (deploy normal, com o `.103` alinhado à `main`):
   ```bash
   ssh ubuntu@192.168.30.103 'cd ~/voyager && git pull --ff-only && \
     docker compose -f docker-compose-prod.yml build web && \
     docker compose -f docker-compose-prod.yml up -d'
   ```
   O `dejt` segue no pypdf e não depende disto.
2. **Migrations: aplicadas, nada pendente.** Conferido em 20/08/2026 pelo
   worker da `.102` (`showmigrations`, jamais pelo boot do `web`):
   `diarios/0001_initial` **[X]**, `diarios_entes/0001_initial` **[X]**,
   e as dependências `tribunals/0048_ingestionrun_fonte` **[X]** /
   `0049_stj_horizonte_djen` **[X]** / `0050_indices_io` **[X]**.
   `makemigrations --check` nos três apps: *No changes detected*. Nota para
   quem for aplicar em outro ambiente: as duas `0001_initial` são atômicas
   (CreateModel puro, sem `CONCURRENTLY`), mas a `tribunals/0048` de que elas
   dependem é `atomic = False` + `AddIndexConcurrently` — não pode ser
   embrulhada em transação.
3. **Worker da fila `diarios`: NO AR na `.102`, ocioso.** 2 réplicas,
   `mem_limit 1g`, `nofile 65536` (`worker_diarios` em
   `docker-compose-workers.yml`, com a conta numérica no comentário). Ocioso
   porque quem enfileira é o agendamento, e ele continua desligado. Medido logo
   após subir: 113 MiB de RSS em repouso, fila em 0 job, `Listening on
   diarios...` nos dois containers.
4. **RESOLVIDO (20/08/2026) — o índice de `hash` não existe, e não vai ser
   criado.** O canário estava certo no diagnóstico e o desbloqueio veio por
   outro caminho, porque o problema era maior do que a falta de índice.

   O que o canário mediu: `tribunals/models.py` DECLARAVA
   `models.Index(fields=['hash'])`, `pg_indexes` lista 9 índices em
   `tribunals_movimentacao` e nenhum tem `hash` na chave; `makemigrations
   --check` nunca pega isso (compara o model com o ESTADO DAS MIGRATIONS, jamais
   com o banco). Plano medido: `Parallel Index Scan using
   mov_inserido_tribunal_idx` varrendo TODO o TJSP, custo 73.427.276 — e um lote
   de 11 hashes ficou **313 s** ativo em `pg_stat_activity` sem terminar.

   O que faltava descobrir: **a métrica não podia acertar nem com índice.**
   `fingerprint_ato` devolve sha1 — 40 caracteres. O `hash` das linhas do DJEN é
   o opaco da API — 30 (`djen/parser.py:243`; por amostra em prod, onde não é
   vazio, `len=30`). Uma string de 40 nunca é igual a uma de 30, então
   `hash__in=[…]` casava com NADA: `espelhadas` era **0 por construção**. Criar
   o índice teria tornado rápida uma resposta errada — e `espelhadas=0` lê-se "o
   diário próprio não repete o DJEN", que é o oposto da verdade.

   O teste que cobria isso construía o item do DJEN COM o fingerprint, que é o
   que a produção não faz: validava uma ficção montada para a métrica passar.

   **Decidido:** parear por `(processo, data)` — o único par que os dois
   veículos de fato compartilham, já que o texto verbatim difere de propósito —
   usando `mov_processo_data_disp_idx`, que existe. Mais `statement_timeout` de
   3 s (métrica nunca segura escrita) e abstenção explícita: estourou, devolve
   `None`, o runner marca `>=` no log e `espelhadas_parcial` no retorno. Nunca 0.

   Medido em produção, lote de 200 de linhas reais do TJSP: **1,16 s**, contra
   os 313 s que não terminavam. Ver ADR-033.

   Os índices declarados-e-ausentes `mov_search_vector_gin` e `mov_texto_trgm`
   foram removidos do model pela migration `0051_indices_fantasma` (só de
   estado, `database_operations=[]`) — ver ADR-032, que também tirou a busca por
   texto do Postgres. O de `classe` continua declarado e ausente; é o único que
   sobrou e não morde ninguém hoje.

5. **Decidir §5.1 (CNJ de origem)** antes de ligar `stf` ou o STJ.
6. **Medir sobreposição com o acervo de produção** (§2) antes de dimensionar —
   com um primeiro número já na mão, do canário: no dia 12/03/2025 o DJEN já
   tem **62.849** movimentações do TJSP (39.405 CNJs distintos) DENTRO da
   janela que o coletor declara exclusiva (2007-10-01 → 2025-03-13). O DJEN
   cobre o TJSP desde **2023-08-14**; a borda superior da janela do §7 está
   errada e precisa recuar antes de qualquer backfill que atravesse 2023.

**CUIDADO ao ler o gate 6: são duas réguas diferentes, e confundi-las custa uma
conclusão errada.** Na primeira leitura o número parecia 80,6% e "reprovado":

    itens gravados ....................... 29.033
    processos com ato próprio ............ 26.084   ← 80,6% de 32.366
    CNJs no TEXTO gravado (regex estrita)  32.272   ← 99,7% de 32.366

O gabarito offline (32.366) conta **CNJ que aparece no PDF**. Um bloco do
caderno é UM ato, de UM processo, mas cita outros processos no corpo ("nos autos
nº X, referente ao processo Y"). Então `processo_id` distinto SEMPRE será menor
que "CNJ no texto", e a diferença não é perda — é a diferença entre o ato e o que
o ato menciona. A régua comparável ao gabarito é a que roda a MESMA regex no
`texto` gravado.

**O que a primeira coleta real revelou sobre o dia 12/03/2025:**

| porta | itens | CNJs distintos |
|---|---|---|
| diário próprio, caderno 12 | 29.033 | 26.084 |
| DJEN, o dia inteiro | 56.566 | 35.089 |

**Os 8 cadernos coletados (21/08/2026) — e o número que decide a terceira porta:**

| | itens | processos distintos |
|---|---|---|
| diário próprio, os 8 cadernos | **220.544** | **207.869** |
| DJEN, o mesmo dia | 62.849 | 39.405 |

    só no diário (INÉDITOS) ..... 200.729 processos
    só no DJEN ..................  32.265
    nos dois ....................   7.140
    união ................................. 240.134

**A terceira porta traz 5,3× mais processos que o DJEN, e a sobreposição é de
apenas 7.140 (3,4% da união).** As duas portas são quase disjuntas — o que é
coerente com o que o DJEN É: veículo de COMUNICAÇÃO (só o que é dirigido a
advogado cadastrado), não o caderno inteiro do tribunal.

Num único dia de um único tribunal, 200.729 processos que o DJEN nunca contou.
Este é o argumento da terceira porta, medido em vez de suposto.

**Canário em produção — o gate NUMÉRICO para quem repetir (20/08/2026).**
Não é "rodou sem estourar": é bater número por número. O que já foi conferido
ao vivo e o que cada linha tem que devolver:

| # | Passo | Gate — o número exato | Estado em 20/08/2026 |
|---|---|---|---|
| 1 | `diarios_pausar --listar` / `--pausar` / `--religar` | a fonte aparece e some da lista de pausadas; 5 fontes registradas | **passou** |
| 2 | `showmigrations` + `migrate --plan` **pelo worker**, nunca pelo boot do `web` | `No planned migration operations`; `diarios/0001` e `diarios_entes/0001` `[X]`; `tribunals/0048-0049-0050` `[X]` | **passou** (0 pendentes, nada aplicado) |
| 3 | worker da fila `diarios` | 2 réplicas `Up`, `Listening on diarios...`, `import pymupdf` → **1.24.14**, fila em 0 | **passou** |
| 4 | `diarios_coletar tjsp-dje --de 2025-03-12 --ate 2025-03-12 --dry-run` | **4.162** edições em **1** request | **passou** |
| 5 | `--catalogar`, duas vezes | 1ª: 8 unidades novas · 2ª: **0** novas (idempotente) | **passou** |
| 6 | coleta do caderno **12** de 12/03/2025 (Capital Parte I, 4.229 págs) | **≥32.000** CNJs distintos pela regex ESTRITA (offline deu 32.366) e cobertura **≥95%** (offline: 99,751%) | **passou** (21/08): **32.272** CNJs distintos no texto gravado = **99,7%** do gabarito. 29.033 itens, 0 duplicados, 33,5 MB de texto, `status=ok` |
| 7 | `espelhadas_no_lote()` antes de cada lote | tem que ser **instantâneo** | **passou** (20/08, depois do fix): lote de 200 do TJSP em **1,16 s**, pareando por `(processo, data)`. Antes: 313 s para 11 hashes, e a resposta era 0 por construção — ver pré-requisito 4 |
| 8 | conferência dos dois lados no mesmo dia | movs `tjsp-dje` antes = depois; DJEN **62.849** antes = depois; atos espelhados = 0; `external_id` repetido = 0 | **passou** (nada foi gravado) |
| 9 | site durante a coleta | **200** em toda amostra | **1 falha em 161** amostras (HTTP 520 às 20:32:21, 11,79 s). Não atribuído à coleta: o log do nginx da origem tem buraco de 23 s no horário, ou seja o pedido nunca chegou lá. Causa **não provada** — fica registrado como não provado |

Como contar `tjsp-dje` no banco sem derrubar nada: **não** use
`external_id LIKE 'tjsp-dje:%'` — a coluna está em collation `en_US.UTF-8`, o
`LIKE` não usa o índice e a consulta estoura o `statement_timeout` (testado).
Use a faixa, que casa com `uniq_mov_tribunal_extid`:
`WHERE tribunal_id='TJSP' AND external_id >= 'tjsp-dje:' AND external_id < 'tjsp-dje;'`
(voltou em milissegundos). Triangule sempre com `EdicaoDiario.itens_gravados` e
com `IngestionRun.exclude(fonte='djen')`, porque contagem de um lado só não
prova nada.

**Quanto custa o backfill, com o número medido — e por que a conta de CPU é a
parte fácil.** O catálogo devolve **4.162 edições** e o único dia catalogado ao
vivo (12/03/2025) tem **8 cadernos** ⇒ ~**33 mil cadernos**. Custo por caderno,
medido no MAIOR conhecido (Capital Parte I, 36 MB, 4.229 págs): **16,3 s** só de
extração PyMuPDF, **44,8 s** de extração + segmentação, **67,2 s** rodando o
coletor real do repo. Extrapolando o maior caderno para todos (superestima, a
média não foi medida) e com as 2 réplicas de hoje:

| a 16,3 s (só extração) | a 44,8 s (pipeline) | a 67,2 s (coletor real) |
|---|---|---|
| 151 h CPU ⇒ **~3,1 dias** | 414 h CPU ⇒ **~8,6 dias** | 621 h CPU ⇒ **~13 dias** |

O gargalo REAL não é esse. É o banco: (a) `espelhadas_no_lote()` sem índice de
`hash` levou 313 s para **11** hashes — nessa taxa o backfill não termina nunca;
(b) o caderno medido produz **29.037** itens, e mesmo supondo média de 1/4 disso
são ~**240 milhões** de linhas novas num Postgres que já tem 1,39B linhas e
1,6 TB, disk-I/O-bound. **Decidir o recorte de volume (§5.2) e o destino do
`espelhadas_no_lote()` vem ANTES de qualquer conta de CPU.**

**Zumbicida:** `watchdog_ingestao` (`djen/jobs.py:255`) **não filtra por fonte**.
Run de diário abortado que ficar `running` vira "worker crashou" em 1 h — causa
FALSA no histórico. Ao abortar uma coleta à mão, feche o `IngestionRun` você
mesmo, com a causa real em `erros`.

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
| `tjsp-dje` | catálogo = 4.162 edições em 1 request; ≥95% dos CNJs tolerantes segmentados; **canário do extrator**: ≥32.000 CNJs distintos pela regex ESTRITA no caderno de 4.229 páginas | **99,1%** (2025), **99,7%** (2015), **96,1%** (caderno 11), **99,751%** (Capital Parte I de 12/03/2025, 4.229 páginas). Canário reconferido em 20/08/2026 **no container de dev** (em PRODUÇÃO ele nunca chegou a rodar — a coleta travou antes, §8), mesmo arquivo, dois extratores: PyMuPDF **32.366** estritos / 32.575 tolerantes / 44,8 s / RSS 185 MB contra pypdf **27.483** / 32.575 / 184,1 s / RSS 425 MB; por linha, **23** com CNJ partido contra **5.866**. 39 + 11 testes |
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
| todas | **`espelhadas_no_lote()` está no caminho da gravação, é métrica, e não tem teto de espera.** Roda antes de cada lote de 500 e faz `count()` por `hash` em 1,39B linhas SEM índice de `hash` (§8, pré-requisito 4): 313 s medidos para 11 hashes. Formato exato da regra nº 7 do `CLAUDE.md` |
| todas | o índice de `hash` declarado em `tribunals/models.py` **não existe no banco** — e `makemigrations --check` nunca pega, porque compara model com migrations, não com o banco. Mesma coisa em `mov_search_vector_gin`, `mov_texto_trgm` e no índice de `classe` |
| `tjsp-dje` | `janela_fim = 2025-03-13` sobrepõe ~19 meses com o DJEN, que cobre o TJSP desde **2023-08-14** — a janela do §7 NÃO é de exclusividade nessa borda. **Mas encolhê-la seria a perda, não a cura**, e agora há número: no dia 12/06/2024, dentro da faixa sobreposta, o diário trouxe **198.695** processos contra 30.470 das outras portas, com apenas **6.711 (3,0%)** nos dois. A borda fica como está, e o que muda é a leitura: `janela_*` é *o período que a fonte está autorizada a coletar*, não *o período em que ela é a única* |
| todas | `watchdog_ingestao` (`djen/jobs.py:255`) **não filtra por fonte**: run de diário deixado `running` vira "worker crashou" em 1 h — causa falsa no histórico |
| todas | **o gate de índice (§12) mede o (tribunal, DIA), não a edição.** Um dia do DJE/TJSP são 8 cadernos e os 8 compartilham o mesmo número — por isso os campos se chamam `indice_*_no_dia`. Recortar por edição custaria 29,2 s e 65.846 blocos lidos do disco por caderno (`EXPLAIN` em produção do `LIKE 'tjsp-dje:4161-12-%'`), e a alternativa barata seria um campo novo no doc do ES, com mapping e plano de reindex de 1,4 bilhão de documentos |
| todas | **`job_id` determinístico dedupa o JOB no RQ, não a ENTRADA na lista da fila.** O `tick` reenfileira as pendentes a cada passada e a lista da `diarios` acumula: medido em 24/08/2026, **7 entradas para 6 `job_id` distintos**. Consequência real, com log: o caderno `3985-20` foi baixado e segmentado **duas vezes** (221 s + 149 s), porque as 2 réplicas pegaram a mesma unidade antes de a primeira marcar `ok`. Não corrompe dado (a 2ª rodada deu `novas=0 dup=26.351`), mas queima download e CPU, e `fila.count` superconta |
| todas | **`espelhadas` só é confiável na PRIMEIRA coleta da unidade.** `espelhadas_no_lote` exclui os `external_id` do LOTE atual, não os da unidade inteira — então numa re-coleta ele conta as linhas que a própria fonte gravou nos lotes anteriores. Medido no mesmo `3985-20`: 1.883 na 1ª passada, **2.527** na 2ª, mesmo caderno |
| `tjsp-dje` | o caderno **1 (Administrativo)** fecha `vazia` mesmo tendo CNJ impresso: em 12/06/2024 o segmentador achou **0 blocos** com **44** CNJs no texto, e o gate de cobertura não dispara porque `MINIMO_PARA_AFERIR_COBERTURA=200`. É lacuna pequena e SILENCIOSA — abaixo de 200 CNJs a edição não é aferida contra nada |
| todas | o gate REPARA (re-enfileira) o que falta, mas **não prova que o reparo drenou**: ele carimba a edição na passada em que enfileirou. Quem quiser a prova roda `manage.py diarios_conferir_indice --tribunal X --dia D --so-medir` depois — que é exatamente o que fecha o dia 12/03/2025 em 0 |

---

## 11. Onde tudo isso está

| Arquivo | O quê |
|---|---|
| `diarios/base.py` | contrato, runner, dedupe, kill switch, `dv_cnj_valido`, **entrega ao índice** (`_entregar_ao_indice`) |
| `diarios/orcamento.py` | **orçamento e guardas de recurso** (§13.3): teto de unidades/24 h por fonte, guarda de disco do ES e guarda de profundidade da fila `es_index`. As duas guardas falham FECHADAS |
| `diarios/indice.py` | **gate de completude do índice** (§12): `conferir_dia` (os dois lados), `reparar_dia` (re-enfileira só o que falta) |
| `diarios/management/commands/diarios_conferir_indice.py` | entrada manual do gate — `--tribunal X --dia D`, `--so-medir` |
| `diarios/models.py`, `diarios/jobs.py` | watermark, fila e o job `conferir_indice` (cron 15 min) |
| `diarios/fontes/{tjsp_dje,dejt,stf}/` | uma fonte por diretório |
| `diarios_entes/` | app próprio dos DOEs de entes |
| `djen/scheduler.py` (fim do arquivo) | agendamento, atrás de `DIARIOS_SCHEDULER_ENABLED` |
| `core/settings.py` | bloco `DIÁRIOS PRÓPRIOS` |
| `tests/test_diarios_base.py` + `test_diario_{tjsp_dje,dejt,stf}.py` + `test_diarios_entes.py` | 176 testes (26 do contrato + 39 tjsp-dje + 57 dejt + 29 stf + 25 entes) |
| `tests/test_diarios_pymupdf.py` | 11 testes que travam a ADR-031: fluxo (não acúmulo), CNJ inteiro e erro explícito sem a lib. O canário do caderno de 36 MB **pula** sem a fixture pesada `tjsp_esaj/caderno3_capital_parteI_20250312.pdf` (fora do git, ver `tests/fixtures/diarios/README.md`) |
| `tests/test_diarios_orcamento.py` | 16 testes do orçamento: a linha `UNASSIGNED` do `_cat/allocation` não vira 0%, ES/fila mudos FECHAM a guarda, bloqueio não perde pendente nem conta tentativa, e o disco é perguntado ANTES da fila |
| `tests/test_diarios_indice.py` | 14 testes do gate de índice: entrega no `on_commit`, fila morta derruba a coleta, abstenção nunca vira 0, teto é ERRO, `_bulk` por bytes e o 413, watermark que não anda no erro |
| `.ia/DECISIONS.md` ADR-030 | por que a terceira porta, e o que foi recusado |

---

## 12. O gate de índice — "coletado" não é "buscável"

**O incidente que criou esta seção, medido em produção em 21/08/2026.** Logo
depois da primeira coleta real (os 8 cadernos do DJE/TJSP de 12/03/2025,
220.544 linhas), a conferência dos dois lados do MESMO dia deu:

    TJSP, 12/03/2025
      Postgres ....... 283.393
      Elasticsearch .. 255.709
      FORA do índice .  27.684   (9,8% do dia)

E o número não se movia entre duas medições. As 8 edições estavam
`status=ok`, `itens_gravados=220.544`. Run verde, log limpo, número redondo —
os três da tabela do `CLAUDE.md` de uma vez.

### A causa, reconstruída à unidade

`persistir_movimentacoes` grava por `bulk_create`, que **não dispara
`post_save`**, logo o write-through de `search/signals.py` nunca é acionado. A
docstring dizia que "a indexação é feita depois, em lote, por `reindexar_*`" —
comando que ninguém rodava para edição coletada. Na prática, a ÚNICA coisa que
levava essas linhas ao índice era o poller `search/sync_incremental.py`: de 10
em 10 minutos, keyset por `id > watermark`.

O log do scheduler tem a prova (horários em -03):

| tick | movs enfileiradas | watermark deixado |
|---|---|---|
| 21:33:37 | 96.117 | 1.662.856.501 |
| 21:41:38 | 75.096 | **1.663.688.937** |
| 21:53:01 | 35.379 | 1.665.564.574 |

A coleta terminou às **21:44:43** com `id` máximo **1.664.109.049**. Linhas do
diário acima do watermark de 21:41:38: **27.619**. Somadas a 65 de resíduo
antigo do DJEN no mesmo dia, dão **27.684** — o número relatado, exato.

O tick das 21:53:01 as enfileirou e o buraco fechou sozinho. Quem mediu duas
vezes com poucos minutos entre as medições viu o mesmo número porque **entre
ticks de um poller nada se move** — não porque estivesse parado.

**Foi ESPERA, não perda.** Conferido depois, id a id: 220.544 de 220.544 no
índice. E a corrida do watermark (linha que commita depois da leitura do tick
e fica abaixo dele para sempre) **não mordeu** aqui: em 5 de 5 faixas de id
medidas, PG = ES = o que o tick enfileirou, diferença 0. Os saltos grandes de
`id` entre ticks são valor de sequência queimado por `ignore_conflicts`, não
linha invisível.

### Por que isso é um buraco mesmo tendo fechado sozinho

1. **Nada afirmava que a edição era buscável.** Se o poller estivesse desligado
   (`sync_es:off`), freado (`FILA_ES_ALTA`), ou se a chave do watermark sumisse
   do cache — ele **re-ancora no TOPO** e o que ficou abaixo nunca mais é lido
   —, a edição continuaria dizendo `ok`.
2. **A espera é ilimitada por construção.** Durante a recuperação nacional do
   DJEN o watermark corre atrás de milhões de ids; um caderno coletado entra no
   FIM dessa fila. Aqui foram ~9 minutos; no meio de um backfill, não é.
3. **O poller engolia falha de enfileiramento e avançava o watermark assim
   mesmo.** Um soluço do Redis apagava do índice, para sempre, a leva inteira
   daquele tick — com um `WARNING` no log. Corrigido: erro no enqueue **não
   move** o watermark.

### O que passou a existir

| Camada | Onde | O que faz |
|---|---|---|
| **Entrega na gravação** | `diarios/base.py::_entregar_ao_indice` | cada lote de 500 gravado é enfileirado na hora em `es_index` (`transaction.on_commit`) — 441 jobs para um dia, não 220.544. NÃO é religar o `post_save` |
| **Gate mecânico do lote** | `persistir_movimentacoes` | toda linha que o lote diz ter gravado tem que ter pk; falta vira ERRO em `IngestionRun.erros`. Fila fora do ar **derruba a coleta** em vez de calar |
| **Gate dos dois lados** | `diarios/indice.py` + job `diarios.jobs.conferir_indice` (cron 15 min) | conta PG e ES do (tribunal, dia) com a MESMA janela, re-enfileira só o que falta, carimba `EdicaoDiario.indice_*_no_dia`. Abstenção explícita: ES mudo ⇒ `None`, nunca 0, e sem carimbo |
| **`_bulk` por bytes** | `search/jobs.py::_enviar_bulk` | o lote fecha por TAMANHO, e um `413` divide ao meio e reenvia em vez de morrer no `FailedJobRegistry` |

Chaves novas em `core/settings.py`: `DIARIOS_INDEXAR_AO_GRAVAR` (default `True`)
e `DIARIOS_GATE_INDICE_ENABLED` (default `True`, e **fora** do
`DIARIOS_SCHEDULER_ENABLED` de propósito: a coleta de hoje é manual, e um gate
que só rodasse com o agendamento ligado não teria pego o caso que o criou).

### O segundo buraco, achado na mesma medição

O `FailedJobRegistry` da fila `es_index` tinha **91 jobs
`indexar_movimentacoes_bulk`** parados: **83 com `ApiError(413)`** (corpo acima
do `http.max_content_length` do ES, porque o lote era contado em DOCUMENTOS e
não em BYTES) e 8 com `JobTimeoutException` de 120 s. Eles referenciam **45.500
publicações**, das quais **45.313 estavam fora do índice** — e ninguém
reprocessa o `FailedJobRegistry`. Um teto que existia do lado do ES era um
**corte mudo do nosso lado**.

Corrigido em três frentes: lote por bytes (20 MB), divisão automática no 413, e
`DEFAULT_TIMEOUT` da fila `es_index` de 120 s → 600 s (os 120 s vinham de
quando o job era UMA publicação).

### Custo, medido, para quem for dimensionar o backfill

| medida | valor |
|---|---|
| `SELECT` dos pks por `external_id` (lote de 500, índice único) | **0,072 s** |
| `_bulk` de 500 documentos no ES | **4,13 s** |
| `terms` count de 220.544 ids no ES (23 perguntas) | **~1 s** |
| `LIKE 'tjsp-dje:4161-12-%'` num dia do TJSP (`EXPLAIN` real) | **29,2 s**, 65.846 blocos lidos do disco ⇒ **descartado** como caminho do gate |

A entrega na gravação faz o poller reindexar as mesmas linhas depois — trabalho
dobrado, medido em 4,13 s por lote de 500. Para o backfill inteiro do DJE isso
é da ordem de ~22 h de frota, contra os ~13 dias do backfill. Se um dia esse
overhead incomodar, `DIARIOS_INDEXAR_AO_GRAVAR=0` desliga e o gate continua
sendo a rede de segurança — fica lento, não fica errado.

### Como conferir um dia à mão

```bash
manage.py diarios_conferir_indice --tribunal TJSP --dia 2025-03-12 --so-medir
manage.py diarios_conferir_indice --tribunal TJSP --dia 2025-03-12   # mede E repara
manage.py diarios_conferir_indice --fonte tjsp-dje --carencia-min 0  # tudo pendente
```

### Validado em produção — 21/08/2026

Não é "deployou": é número, dos dois lados, com a régua nova.

| passo | resultado |
|---|---|
| `migrate diarios 0002` **pelo worker** (nunca pelo boot do `web`) | 4 `ADD COLUMN NULL`, aplicada, `[X]` |
| gate no dia 12/03/2025, ANTES | PG **277.110** · ES **277.050** · faltando **60** |
| reparo | 277.110 pks lidos, **60** ausentes, 60 re-enfileiradas |
| gate no mesmo dia, DEPOIS | PG **277.110** · ES **277.110** · faltando **0** |
| cron do scheduler (15 min), sozinho | carimbou as **8** edições às 03:17:01 UTC com `indice_no_es_no_dia=277110`, `indice_faltando_no_dia=0` |
| re-coleta ao vivo do caderno 10 (11 itens) | `novas=0 dup=11`, e no MESMO segundo um `indexar_movimentacoes_bulk` com os 11 pks (`1662366704…`) no log do `worker_es_index` — o write-through, provado |
| `FailedJobRegistry` da `es_index` | os 91 bulks mortos re-enfileirados: **45.313** publicações que estavam fora voltaram. Amostra de **511** pks colhida do log dos workers: **511 no índice, 0 fora**. Registry agora com **0** falhas de movimentação (as 576 restantes são de `indexar_processos_bulk`, outro bug, anterior) |
| `_bulk` por bytes | **0** ocorrências de 413 nos 25 min seguintes (o lote por tamanho impediu antes de precisar dividir) |

Ressalva honesta: a lista exata dos 45.313 pks não foi salva antes de limpar o
registry, então a conferência id a id vale para a amostra de 511 colhida do
log, não para os 45.313. O que se sabe dos outros é que os 91 jobs rodaram sem
falhar — nenhum voltou para o registry.

### Os 5 que sobravam, e a lição de fuso outra vez

Depois de tudo fechado pela régua do dia CIVIL, a régua da janela UTC ainda
acusava 5. **Não era resíduo do diário, e não era do dia 12/03.** Medido:

    janela UTC   [12T00:00Z, 13T00:00Z) ..... PG 283.393 · ES 283.388 · fora 5
    dia civil -03 12/03 (o que o gate mede) . PG 277.110 · ES 277.110 · fora 0
    borda de baixo [12T00:00Z, 12T03:00Z) ... PG   7.056 · ES   7.051 · fora 5

As 5 têm `data_disponibilizacao` entre 00:41 e 02:53 UTC — que em horário local
é **21:41 a 23:53 do dia 11/03**, o dia civil ANTERIOR. O gate do dia 12 estava
certo em dizer 0; quem estava somando dois dias diferentes era a janela UTC.
É a mesma armadilha de fuso que já produziu um alarme falso nesta casa, agora
do outro lado da meia-noite.

E o `external_id` das 5 responde de onde vieram: **`datajud:…`**. Não são do
diário nem do DJEN — são da varredura do Datajud, que grava `Movimentacao` por
`bulk_create` e portanto tem exatamente a mesma doença que este documento
descreve, sem a cura. O dia civil 11/03 tinha **39** nessa situação.

Fechado rodando a mesma ferramenta no dia vizinho
(`diarios_conferir_indice --tribunal TJSP --dia 2025-03-11`: 39 ausentes, 39
re-enfileiradas). Resultado final, nas três réguas:

    janela UTC   [12T00:00Z, 13T00:00Z) ..... PG 283.393 · ES 283.393 · fora 0
    dia civil -03 12/03 ..................... PG 277.110 · ES 277.110 · fora 0
    dia civil -03 11/03 ..................... PG  66.121 · ES  66.121 · fora 0

**Dívida que fica registrada e NÃO foi resolvida:** o `datajud/ingestion.py`
continua dependendo só do poller. O gate desta porta não o alcança, porque ele
só olha dia que tem `EdicaoDiario` catalogada. Ver `.ia/SEARCH_SCHEMA.md`.

### O que sobrou no `FailedJobRegistry` — e por que NÃO foi re-enfileirado

Depois do reparo, restam **576** falhas, **todas** de
`indexar_processos_bulk`, com `ValueError: Invalid attribute name:
indexar_processos_bulk` — bug anterior e de outra natureza (o nome da função
não resolvia no worker). Zero de movimentação.

Elas referenciam **607** processos distintos, e os 607 foram conferidos —
**não por amostra, o conjunto inteiro**: **607 no índice, 0 fora**. O
`sync_processos_atualizados` já os pegou por outro caminho. Re-enfileirar seria
trabalho inútil num ES de um nó só e I/O-bound. Ficam ali como evidência do bug
que ninguém investigou ainda, e não como perda de acervo.

⚠️ Ao ler esse registry: `get_job_ids()` devolve por ordem de **expiração**, não
de tempo. Amostrar os primeiros é amostra enviesada — foi assim que dois
alarmes falsos nasceram em 21/08/2026. Embaralhe antes, e diga o tamanho.

⚠️ **Não conte `tjsp-dje` por faixa de `external_id` no ORM.** O
`external_id__gte='tjsp-dje:'` + `__lt='tjsp-dje;'` que esta doc sugeria para
SQL cru devolve **0** pelo ORM: a coluna está em collation `en_US.UTF-8`, que
ignora pontuação no nível primário, e `'tjsp-dje;'` compara como `tjspdje` —
menor que qualquer `tjsp-dje:4161…`. Medido em 21/08/2026: a faixa devolveu 0
onde o `LIKE` devolveu 220.544. Ancore SEMPRE por `tribunal` +
`data_disponibilizacao` e use `LIKE` como FILTRO, nunca como caminho de acesso.


---

## 13. LIGADA — o que foi ligado em 24/08/2026, com que números, e quando desligar

> **Estado: `DIARIOS_SCHEDULER_ENABLED=1`, `DIARIOS_FONTES_AGENDADAS=tjsp-dje`,
> `DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE=8`.** Uma fonte de cinco, com orçamento
> de um dia de cadernos por 24 h. As outras quatro estão fora do agendamento e
> §13.6 diz por qual NÚMERO cada uma ficou de fora.

O §8 é a história do canário; esta seção é o que ficou ligado, o teto que
segura, e o número que manda desligar.

### 13.1 O inventário, medido antes de abrir a torneira

Catálogo enumerado com `--dry-run` — nada gravado, nenhum caderno baixado:

| fonte | unidades no catálogo | na janela | custo do catálogo | viva em 24/08? |
|---|---:|---:|---|---|
| `tjsp-dje` | **33.296** (4.162 edições × 8 cadernos − 398 `datasSemDiario`) | **32.616** | 1,4 s, **1** requisição | HTTP **200** em 0,29 s (728 KB) |
| `dejt` | — | — | 6 tentativas, ~15 min, e desistiu | **HTTP 503 em 4/4 sondas** |
| `stf` | 1/dia ⇒ ~**2.185** (2020-09-01 → hoje) | idem | 0,1 s | `ultimo-dje` = **2026-08-24** em 0,14 s |
| `doe-sp` | 1/dia ⇒ ~**1.142** (2023-07-10 → hoje) | idem | 4,0 s | 200; declara 3.489 publicações no dia |
| `qd-municipal` | 1/dia | idem | 0,0 s | 200 |

`tjsp-dje` por ano: 2007 **464** · 2008 1.936 · 2009 1.912 · 2010 1.912 · 2011
1.880 · 2012 1.840 · 2013 1.888 · 2014 1.880 · 2015 1.848 · 2016 1.856 · 2017
1.832 · 2018 1.840 · 2019 1.888 · 2020 1.864 · 2021 1.848 · 2022 1.848 · 2023
1.832 · 2024 1.880 · 2025 1.048.

**As 6.224 unidades de 2007-2010 são download e CPU por ZERO linha.** Provado ao
vivo, não deduzido: o caderno 3 de 15/06/2009 (`492-12`) foi coletado à mão e
fechou **`sem_aproveit` em 54,6 s** — baixou, extraiu o texto, segmentou e
gravou nada, porque é a era pré-CNJ do §1. Extrapolando, **~95 h de CPU de
frota** para zero acervo. Qualquer recorte de backfill começa em **2011**, e
isso corta 19% do catálogo antes de gastar um byte.

**Atenção ao ler `catalogar_fronteira`:** ele só toca fonte cuja `janela_fim`
alcança hoje. `tjsp-dje` (fim 2025-03-13) e `dejt` (fim 2024-07-31) são
HISTÓRICAS — o cron das 05:40 devolve `{'skip': 'fonte histórica'}` para elas.
Consequência prática: **ligar o agendamento para o `tjsp-dje` não cataloga
nada sozinho**. Quem decide qual pedaço da jazida entra é um
`diarios_coletar --catalogar` explícito. Isso é uma proteção, não um bug.

### 13.2 Quanto custa em disco — e por que quem barra é o ES, não o Postgres

Base medida em 24/08/2026 19:35 UTC, com a terceira porta ainda parada:

| medida | valor |
|---|---|
| `pg_database_size` | **2.230.291.797.683 B** (2.077 GiB) |
| `tribunals_movimentacao` (heap+toast+índices) | **1.968.480.280.576 B** (1.833 GiB), 1.503.114.112 linhas |
| disco do host do banco (VM 201, `qm guest exec`) | 4,9 TB, 2,1 TB usados — **46%** |
| `voyager-movimentacoes-v2` | **1.517.849.080** docs, **1,5 TB** ⇒ **1,06 KB/doc** |
| disco do nó de ES | 2,9 TB, 1,8 TB usados, **1,0 TB livre** — **63%** |
| crescimento das OUTRAS duas portas (janela de 904 s) | **+0,45 GB/h** no banco, **+346.445 docs/h** no ES |

Peso de uma linha do diário, amostra de **3.000** linhas reais do dia
12/03/2025: `pg_column_size(texto)` **868,9 B**, `length(texto)` **975,7**
caracteres (mediana 610), `pg_column_size` da linha inteira **1.606,3 B**.

Projeção do backfill do `tjsp-dje` de 2011 em diante (26.392 unidades), com
**27.568 itens por caderno** (220.548 em 8, medido):

    linhas novas ................ ~7,3e8
    índice no Elasticsearch ..... ~772 GB   (a 1,06 KB/doc)
    ES depois .................. 2,57 TB de 2,9 TB = 88,6%

**O `flood_stage` do ES é 95%, e nele o índice vira `read_only_allow_delete` —
o que derruba a escrita das TRÊS portas, não só desta.** O backfill completo do
DJE/TJSP **não cabe** no nó de hoje. O Postgres cabe (iria a ~3,0 TB de 4,9 TB);
quem barra é o índice. **Expandir o disco do ES é pré-requisito de qualquer
janela larga** — procedimento online e conhecido, [`OPS.md`](OPS.md), "VM do
Elasticsearch".

### 13.3 Os tetos que ficaram no código — e por que teto de FILA não bastava

`WATERMARK_POR_FONTE=200` limita a PROFUNDIDADE da fila `diarios`, não a VAZÃO:
os workers drenam e o tick reabastece. Com o agendamento ligado, quem definia o
ritmo era o número de réplicas do `worker_diarios` — dimensionado por CPU, nunca
por disco. `diarios/orcamento.py` fechou isso:

| chave | default | o que faz |
|---|---|---|
| `DIARIOS_TETO_UNIDADES_DIA[_<SLUG>]` | 0 (**sem teto**) | unidades FECHADAS por 24 h. É o botão de "ligar em etapas" |
| `DIARIOS_ES_DISCO_MAX_PCT` | 85,0 | o tick para acima disso. 85% é o `disk.watermark.low` do ES; 90% é o `high`, 95% o `flood_stage` |
| `DIARIOS_FILA_ES_MAX` | 5.000 | o tick para se a fila `es_index` estiver mais funda que isso |
| `DIARIOS_GUARDA_DISCO_ENABLED` | 1 | desliga as duas guardas de infra (não o orçamento) |

Três decisões escritas para não serem redescobertas:

1. **As guardas falham FECHADAS.** ES mudo ⇒ o tick não enfileira. Assimetria
   decidida com número: parada falsa custa 10 minutos (o tick volta), "vai"
   falso custa um índice em read-only. Custo da medição, ao vivo:
   `_cat/allocation` em **0,376 s** frio e **0,009 s** quente.
2. **A guarda de fila existe porque "coletado" ≠ "buscável"** (§12). 5.000 jobs
   = 2,5 milhões de documentos ≈ 15 min de dreno com os 24 `worker_es_index`
   livres, e horas sob contenção. Duas ordens de grandeza abaixo do
   `FILA_ES_ALTA=150.000` do poller **de propósito**: lá o freio protege o
   poller de si mesmo; aqui a terceira porta sai da frente das outras duas.
3. **Teto é ALERTA, nunca corte mudo.** Bloqueio e orçamento esgotado saem no
   log com o número E no retorno do job; a unidade continua `pendente`, com
   `tentativas` intacto. Nada é descartado. 16 testes em
   `tests/test_diarios_orcamento.py`.

### 13.4 Etapa 1 — `tjsp-dje`, o dia 12/06/2024, pelo caminho de produção

Não foi coleta à mão: foi `--catalogar` + o **mesmo** `tick_todas` que o cron
dispara → fila `diarios` → as 2 réplicas do `worker_diarios`. Catálogo rodado
duas vezes: 8 novas, depois **0** (idempotente).

| caderno | blocos | itens | `sem_cnj` | cobertura de CNJ | novas | espelhadas | s |
|---|---:|---:|---:|---|---:|---:|---:|
| 3985-10 Administrativo | 0 | 0 | 0 | 0/44 ⇒ `vazia` | 0 | 0 | 2 |
| 3985-11 | 11.131 | 11.131 | 0 | 12.762/12.762 = **100%** | 11.128 | 1.544 | 124 |
| 3985-12 | 27.890 | 27.828 | 59 | 30.090/30.130 = **99,9%** | 27.815 | 2.176 | 336 |
| 3985-13 | 44.519 | 44.490 | 29 | 52.353/52.370 = **100%** | 44.464 | ≥1.643 | 498 |
| 3985-15 | 42.799 | 42.773 | 26 | 50.567/50.601 = **99,9%** | 42.740 | ≥1.794 | 448 |
| 3985-18 | 51.362 | 51.316 | 45 | 57.274/57.309 = **99,9%** | 51.297 | 4.441 | 401 |
| 3985-19 | 9.954 | 9.897 | 57 | 10.243/10.351 = **99,0%** | 9.835 | 652 | 99 |
| 3985-20 | 26.402 | 26.376 | 26 | 30.271/30.283 = **100%** | 26.351 | 1.883 | 221 |
| **total** | | | | **99,0-100%** | **213.630** | ≥14.133 | **2.129 s de CPU** |

Cobertura de CNJ entre **99,0% e 100%** em todos os cadernos com dado — o gate
do §9 passou com folga. `>=` em dois cadernos é o `espelhadas_no_lote` se
abstendo por `statement_timeout` e dizendo isso, como manda a regra nº 6.

Relógio: **~23 min** com 2 réplicas (2.129 s de CPU / 2). Vazão medida:
**90 itens/s por réplica**, ou **648 mil itens/h** com as 2 de hoje.

**Os dois lados do dia 12/06/2024 (TJSP), depois da coleta:**

| porta | movimentações | processos distintos |
|---|---:|---:|
| diário próprio (`tjsp-dje:`) | **213.630** | **198.695** |
| DJEN + Datajud, o mesmo dia | 54.364 | 30.470 |
| **união** | **267.994** | **222.454** |

    só no diário ............ 191.984 processos
    só nas outras portas ....  23.759
    nos dois ................   6.711   (3,0% da união)

**A sobreposição de 3,4% medida em 12/03/2025 se repetiu em 12/06/2024 com
3,0%, numa era diferente da jazida.** A hipótese do §8 deixou de ser um ponto
solto: em dois dias distintos as portas são quase disjuntas, e o diário traz
**6,5×** mais processos que as outras duas juntas.

**E o número que fecha a discussão de completude — `Process` NOVOS no acervo:**

    TJSP, 17:35→19:35 UTC (sem a terceira porta) ......      0 processos novos
    TJSP, 19:35→19:57 UTC (a coleta de 1 dia) ......... 96.587 processos novos

Zero contra 96.587, em 22 minutos, de um único dia de um único tribunal. Note a
diferença entre os dois números: 191.984 é "processo cuja única movimentação
NAQUELE DIA veio do diário"; **96.587 é "processo que o acervo não tinha em
lugar nenhum"** — cerca de metade dos 198.695 já existia por outras datas. Os
dois são verdadeiros e medem coisas diferentes; o segundo é o que vale como
acervo recuperado, e é o que esta seção usa.

**Custo em disco desta rodada** (deltas com as outras portas juntas dentro):

    pg_database_size ......... +1,00 GiB
    tribunals_movimentacao ... +0,93 GiB
    tribunals_process ........ +0,03 GiB
    disco do ES .............. 63% → 63% (sem casa decimal no `_cat`)

**Gate de índice — "coletado" virou "buscável", provado pelos dois lados:**

| momento | PG | ES | faltando |
|---|---:|---:|---:|
| logo depois da coleta | 267.994 | 243.553 | **24.441** |
| ~4 min depois | 267.994 | 267.964 | **30** |
| depois do `reparo` (30 re-enfileiradas) | 267.994 | **267.994** | **0** |

Era ESPERA, como em 21/08 — não perda. Mas quem afirma isso é a régua, não a
esperança: sem o gate do §12, "faltando 24.441" seria invisível.

### 13.5 O ES aguenta em regime? — 8 passadas de 60 s COM a coleta rodando

Este é o número que a etapa 2 exige antes de existir. Não é "não quebrou".

| UTC | docs/s | write.active | write.queue | **write.rejected** | fila `es_index` | falhas `es_index` | busca p50 | busca máx |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 19:44:41 | 102,9 | 16 | 6 | **0** | 16 | 652 | 0,117 | 0,295 |
| 19:45:41 | 265,1 | 0 | 0 | **0** | 0 | 652 | 0,014 | 0,035 |
| 19:46:42 | 253,4 | 2 | 0 | **0** | 0 | 652 | 0,058 | 0,189 |
| 19:47:44 | 113,4 | 9 | 0 | **0** | 952 | 652 | 0,073 | 0,222 |
| 19:48:44 | 275,1 | 0 | 0 | **0** | 2.040 | 652 | 0,016 | 0,021 |
| 19:49:44 | 755,3 | 2 | 0 | **0** | 2.586 | 652 | 0,011 | 0,038 |
| 19:50:44 | 1.267,9 | 0 | 0 | **0** | 3.056 | 652 | 0,014 | 0,022 |
| 19:51:45 | 1.258,6 | 16 | 85 | **0** | 3.364 | 652 | 0,049 | 0,101 |

- **`rejected` = 0 em todas as passadas.** Rejeição do thread pool `write` é
  documento PERDIDO do lado do ES; fila de 85 é lentidão, não perda.
- **`falhas` da `es_index` constante em 652 — zero novas.** As 26
  `ConnectionTimeout` de `indexar_movimentacoes_bulk` achadas mais cedo
  (amostra ALEATÓRIA declarada, semente 7, n=250 de 652) pararam de aparecer.
- **Busca:** mediana das medianas **0,0325 s**, pior máximo **0,295 s**, contra
  baseline sem coleta de 0,0155 s / 0,243 s. Dobrou a mediana e continua em
  centésimos de segundo.
- **Site:** 8/8 e 10/10 HTTP **200**, média 0,05-0,06 s — igual ao baseline.
- **Pico da fila `es_index` = 3.364, ou 67% do teto de 5.000.** Isto é o achado
  operacional da etapa: **um dia de 8 cadernos já usa dois terços da guarda.**
  Subir o orçamento sem subir o teto faz a guarda barrar — que é exatamente o
  comportamento desejado, e é por isso que os dois números andam juntos.

**Veredito: o ES aguenta UMA fonte no orçamento de 8 unidades/24 h.** Não
aguenta o backfill aberto — §13.2 diz por quê, em GB.

### 13.6 O que ficou LIGADO, o que ficou DESLIGADO, e o número de cada um

| fonte | veredito | o número que sustenta |
|---|---|---|
| `tjsp-dje` | **LIGADA**, orçamento 8 unidades/24 h | 6,5× mais processos que as outras portas no mesmo dia, sobreposição de **3,0%**, e **96.587 processos novos** onde as outras duas trouxeram **0** nas 2 h anteriores |
| `dejt` | **NÃO LIGAR** | a fonte está **fora do ar**: HTTP **503** em 4 de 4 sondas (1,07 s cada, com e sem UA próprio), e o coletor esgotou **6 tentativas** ao longo de ~15 min antes de desistir. Não é decisão de risco, é ausência de fonte — nem o catálogo saiu |
| `stf` | **NÃO LIGAR** | a fonte responde (578 publicações em 19/08/2026, página de 500 em 2,69 s), mas as duas decisões que o §5 marca como "olho humano ANTES" continuam abertas, e uma foi RE-medida hoje: **56 de 500 publicações (11,2%)** vêm marcadas `Segredo de Justiça` (51) ou `Sigiloso` (5) **pela própria fonte**, com corpo completo, e cairiam no mesmo índice buscável servido a cliente. A outra é §5.1 (28% dos CNJs são de OUTRO tribunal ⇒ `Process` duplicado). Nenhuma das duas é decisão de engenharia |
| `doe-sp` | **liberada tecnicamente**, fora do agendamento | canário manual de 1 dia (21/08/2026): **0** publicações gravadas, das 3.489 que a fonte declara no dia, em 8,8 s. E não escreve em `Movimentacao` nem no ES — `PublicacaoOficial` não tem signal em `search/signals.py`, ou seja **custo ZERO no recurso mais apertado**. Fica fora só para respeitar "uma fonte por vez" |
| `qd-municipal` | idem | canário manual de 1 dia: **1** publicação gravada. É o volume que o §1 já dizia — 2-3 gazetas/dia no país com precatório *e* CNJ |

### 13.7 Critério de parada — escrito ANTES, com o porquê de cada número

Ordem de aperto MEDIDA nesta casa: **o ES antes de tudo.** Não é o disco do
banco nem a RAM dos workers.

| # | sinal | onde ler | **DESLIGA** em | por quê |
|---|---|---|---|---|
| 1 | disco do nó de ES | `_cat/allocation` | **≥ 85%** | `watermark.low`; a 5 pontos do `high` e 10 do `flood_stage`, que vira read-only e derruba as três portas. Hoje 63%. **Guarda automática** |
| 2 | `rejected` do pool `write` do ES | `_cat/thread_pool/write` | **> 0 e crescendo** | rejeição é documento perdido, não lentidão. Medido: 0 em 8/8 passadas, com `queue` chegando a 85 |
| 3 | `indexar_movimentacoes_bulk` novos no `FailedJobRegistry` da `es_index` | amostra ALEATÓRIA, semente e n declarados | **> 100/h** | job morto ali é publicação gravada e invisível (§12). Medido no início (semente 7, n=250 de 652): 26 ⇒ ~68 no total; no fim (n=250 de 661): 28 ⇒ ~74. **~8-12/h**, uma ordem de grandeza abaixo do limiar. Ressalva honesta: o registry NÃO foi reprocessado — os documentos entraram assim mesmo (amostra de 3.000 pks dos jobs que estouraram: 0,27% fora), e o gate do §12 fechou o dia em 0 |
| 4 | profundidade da fila `es_index` | `Queue('es_index').count` | **≥ 5.000** | **guarda automática**. Pico medido com 8 cadernos: 3.364 (67%) |
| 5 | latência da busca | 8 termos, semente 42, janela 31 d | mediana **> 2 s** ou máx **> 12 s** | 12 s é o teto que a busca do site já declara. Medido em regime: p50 0,0325 s, máx 0,295 s |
| 6 | site | `GET /dashboard/login/` | qualquer **não-200** repetido | 18/18 em 200, média 0,05-0,06 s, antes e durante |
| 7 | disco do host do banco | `qm guest exec 201 -- df -h /` | **≥ 75%** | a 4,9 TB, 75% deixa 1,2 TB — mais que um backfill inteiro de folga. Hoje 46% |
| 8 | falha da fonte | `diarios_status <fonte>` | `runs_falha` **> 10%** das unidades da rodada | acima disso não é caderno ruim, é fonte ou parser mudado. Medido: 0 falhas em 8 |
| 9 | gate de índice | `indice_faltando_no_dia` | **> 0 em duas passadas seguidas** | o reparo não está drenando; coletar mais só aumenta o invisível |
| 10 | `.102` | `/proc/loadavg`, `free -m` | load1 **> 40** ou `MemAvailable` **< 6.000 MiB** | os mesmos pisos do `~/onda.sh` (OPS.md). Medido durante: load1 12-30, avail 12,3-12,8 GiB |

Nenhum foi acionado na etapa 1.

**Como desligar, do mais barato para o mais caro:**

```bash
# 1. a fonte para em SEGUNDOS, sem deploy, e retoma de onde parou
manage.py diarios_pausar tjsp-dje     # POSICIONAL — não existe --pausar (argparse recusa)
manage.py diarios_pausar --tudo
manage.py diarios_pausar --listar
manage.py diarios_pausar --religar tjsp-dje
# provado ao vivo em 24/08: com a fonte pausada, `fontes_agendadas()` → [] e
# `tick_todas()` → {}. Religar devolve os dois.

# 2. fechar a torneira sem parar: orçamento no .env dos DOIS hosts
#    (o tick roda no worker_default da .102; o cron que o dispara, no scheduler da .103)
DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE=1   # ATENÇÃO: 0 = SEM TETO, não "parado"

# 3. tirar do agendamento (exige force-recreate: `restart` NÃO relê o .env)
DIARIOS_FONTES_AGENDADAS=
DIARIOS_SCHEDULER_ENABLED=0            # no .env da .103
```

⚠ Ao abortar uma coleta à mão, **feche o `IngestionRun` você mesmo**, com a
causa real em `erros`: o `watchdog_ingestao` não filtra por fonte e transforma
run `running` órfão em "worker crashou" em 1 h — causa falsa no histórico.

### 13.8 O caminho, em ordem, para quem for continuar

```bash
# 0. o kill switch responde? (a fonte tem que sumir e voltar da lista)
manage.py diarios_pausar --listar
manage.py diarios_pausar tjsp-dje && manage.py diarios_pausar --listar
manage.py diarios_pausar --religar tjsp-dje

# 1. mede ANTES — os dois lados (§13.2 tem os números de referência)
manage.py diarios_status tjsp-dje

# 2. cataloga só a janela da rodada — barato, não baixa caderno nenhum
manage.py diarios_coletar tjsp-dje --de AAAA-MM-DD --ate AAAA-MM-DD --catalogar
manage.py diarios_coletar tjsp-dje --de AAAA-MM-DD --ate AAAA-MM-DD --catalogar   # 2ª vez: novas=0

# 3. o tick do scheduler (10 min) drena sozinho, respeitando orçamento e guardas.
#    Para não esperar, dispare o MESMO job que o cron dispara:
#      django_rq.get_queue('default').enqueue('diarios.jobs.tick_todas')

# 4. mede DEPOIS, e confere o índice pelos dois lados
manage.py diarios_status tjsp-dje
manage.py diarios_conferir_indice --tribunal TJSP --dia AAAA-MM-DD --so-medir
manage.py diarios_conferir_indice --tribunal TJSP --dia AAAA-MM-DD        # mede E repara
```

### 13.9 O estado em que a porta ficou

Não ficou "ligada e parada": ficou **andando devagar, na parte mais valiosa da
jazida, com teto**.

| item | valor |
|---|---|
| fonte agendada | `tjsp-dje` (única) |
| orçamento | **8 unidades/24 h** (≈ 1 dia de cadernos) |
| catalogado e pendente | **360 unidades** — `2025-01-01 → 2025-03-13`, o pedaço mais recente da jazida |
| dreno projetado | ~45 dias · ~9,9 M linhas · ~10,5 GB de índice (1,0 TB livre) |
| ordem do `tick` | mais recente → mais antigo, então o valor comercial entra primeiro |
| coletado nesta sessão | **213.631** linhas (12/06/2024 + 1 caderno de 2009 vazio) |

Duas escolhas deliberadas que quem for continuar precisa conhecer:

1. **A janela catalogada é 2025-01→2025-03, não o catálogo inteiro.** Catalogar
   as 32.616 unidades de uma vez seria enfileirar um backfill que **não cabe no
   ES** (§13.2) e depender só do orçamento para não estourar. Catalogar em
   fatias é a barreira mais grossa e a mais barata.
2. **As 7 unidades pendentes de 15/06/2009 foram APAGADAS do catálogo** depois
   do canário. Motivo com número: `492-12` provou `sem_aproveit` em 54,6 s por
   zero linha, e deixar as outras 7 pendentes gastaria o orçamento de um dia
   inteiro numa era que já se sabe vazia. Apagar `EdicaoDiario` é reversível —
   um `--catalogar` do mesmo dia as recria.

Provado ao vivo, e **pelo cron, não por chamada manual** — que é a diferença
entre "a função devolve o que eu esperava" e "o sistema se segura sozinho".
Log do `worker_default` da `.102` (horários em -03):

    16:50:59  tick tjsp-dje: +2 enfileiradas, 2 pendentes, folga24h=2,
              disco do ES em 63% (teto 85%); fila `es_index` com 3199 jobs (teto 5000)
    17:01:07  WARNING  tick tjsp-dje: orçamento de 24h ESGOTADO (teto=8, coletadas=8).
                       Nada enfileirado; as pendentes seguem pendentes.
    17:11:41  WARNING  (idem)
    17:21:40  WARNING  (idem)

Três ticks consecutivos do cron de 10 min recusando enfileirar, cada um com o
número no log em nível WARNING, e as 360 unidades intactas em `pendente`. O
tick das 16:50 mostra o outro modo do teto: `folga24h=2` cortou o lote em **2**
de 100 — o orçamento fatia, não é só tudo-ou-nada. E na mesma linha aparece a
guarda de fila medindo ao vivo (`3199 de 5000`) no pico da coleta.

Medido de fora, o estado ficou parado em **360 pendentes / 434.179 itens de
20:04:47 a 20:28:25 UTC** — 24 minutos, ~2,5 ticks. **Cuidado ao ler isso:**
número parado é o sintoma que o teto funcionando e o cron MORTO compartilham.
Quem afirmar um dos dois sem abrir o log do `worker_default` está adivinhando —
e a §13.10 conta como essa checagem quase deu a resposta errada.

### 13.10 A armadilha de sonda que quase inverteu a conclusão

Registrada porque quem for conferir esta porta vai cair nela, e o erro é do
tipo caro: ele produz um alarme falso convincente.

Ao conferir se o cron estava mesmo segurando as 360 unidades, a sonda foi:

```bash
docker logs --since 45m voyager-worker_default-1 voyager-worker_default-2   # ERRADO
```

**`docker logs` aceita UM container.** Com dois nomes ele falha, escreve o
`usage` no stderr e **não devolve uma linha de log** — e um `grep` em cima disso
volta vazio. Vazio lido como "o `tick_todas` nunca rodou", o que levaria à
conclusão exatamente oposta à verdade: que a porta estava morta em vez de
freada. Os dois estados têm o MESMO sintoma de fora (número parado), e foi
preciso o log para separá-los.

O certo é um laço, um container por vez:

```bash
for c in voyager-worker_default-1 voyager-worker_default-2; do
  ssh ubuntu@192.168.30.102 "docker logs --since 55m $c 2>&1"
done | grep -E 'tick_todas|orçamento|BLOQUEADO'
```

Vale para toda a frota, não só aqui: a mesma linha com dois nomes já tinha
devolvido vazio no `worker_diarios` mais cedo na mesma sessão, e ali passou
despercebida porque o resultado esperado também era vazio. **Sonda que devolve
vazio precisa provar que sabe devolver não-vazio** — rode-a uma vez contra um
período em que você SABE que há linha.

Corolário para os critérios do §13.7: os sinais 2, 3 e 8 são todos "conte
ocorrências". Nenhum deles distingue *zero ocorrências* de *sonda quebrada* sem
um controle positivo. Ao usá-los para liberar uma etapa, meça primeiro algo que
tem que dar não-zero.

**Ampliar é subir o orçamento, nunca tirar o teto** — e subir o orçamento sem
subir `DIARIOS_FILA_ES_MAX` faz a guarda barrar (§13.5). A ordem certa de
ampliar o `tjsp-dje` é:

1. **expandir o disco do nó de ES** (é o que barra o backfill inteiro, §13.2);
2. subir `DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE` em passos, medindo o §13.5 entre
   cada um;
3. catalogar a fatia seguinte, **de 2011 em diante** — nunca 2007-2010, que é
   `sem_aproveit` provado (§13.1) — e sempre em fatias, nunca o catálogo inteiro;
4. só então pensar em réplica do `worker_diarios`.

---

## 14. A porta fechada por um INSERT (31/08 → 02/09/2026)

> **Não era falta de trabalho: era `NotNullViolation`.** Entre 31/08 15:19 UTC
> (quando a `tribunals/0054` foi aplicada) e 02/09 01:01 UTC (quando o
> `worker_diarios` foi reiniciado), **toda** coleta do `tjsp-dje` que criasse
> processo inédito falhou no primeiro `bulk_create`. Run vermelho desta vez —
> mas ninguém estava olhando, e a porta ficou 46 h coletando zero.

### 14.1 O estado, medido em 02/09/2026

`EdicaoDiario` inteira, em produção (os três `fonte` que existem):

| fonte | total | ok | pendente | falha | outros | faixa |
|---|---:|---:|---:|---:|---:|---|
| `tjsp-dje` | 377 | 62 | 50 | **257** | 8 | 2009-06-15 → 2025-03-13 |
| `doe-sp` | 1 | 0 | 0 | 0 | 1 `vazia` | 2026-08-21 |
| `qd-municipal` | 1 | 1 | 0 | 0 | 0 | 2026-08-21 |

As 257 falhas do DJE/TJSP, agrupadas por `ultimo_erro`:

    215 · null value in column "classe_cnj_codigo" of relation "tribunals_process"
     40 · null value in column "grau"               of relation "tribunals_process"
      2 · segmentação abaixo do piso   ← OUTRA natureza, ver §14.5

Do outro lado, a mesma coisa contada por `IngestionRun`: **1.043 runs do
`tjsp-dje` em 01/09 e 32 em 02/09** com `classe_cnj_codigo` na mensagem. O
`tjsp-dje` acumulava **1.312 `failed` contra 73 `success`**.

### 14.2 A causa — e a linha de log que prova a tese inteira

A `0054` usou o idioma padrão do Django, `ADD COLUMN ... DEFAULT '' NOT NULL`
seguido de `DROP DEFAULT`. Conferido por COLUNA em `pg_attrdef` (nunca por
nome de migration):

    classe_cnj_codigo  NOT NULL  default=None   ← sem rede
    classe_cnj_nome    NOT NULL  default=None   ← sem rede
    fase_codigo        NOT NULL  default=None   ← sem rede
    fase_nome          NOT NULL  default=None   ← sem rede
    fase_em            NULL      default=None
    grau               NOT NULL  default=''     ← COM rede

Coluna NOT NULL sem `DEFAULT` só aceita INSERT que **nomeie** a coluna, e o
Django nomeia as colunas do model **carregado em memória**. O `worker_diarios`
da `.102` tinha `StartedAt = 2026-08-24T20:06:49Z` — sete dias antes da `0054`.
O bind mount `.:/app` entrega o arquivo novo; o processo Python não reimporta o
módulo (é a mesma armadilha de `OPS.md`, "worker rodando código velho").

**A prova está no `DETAIL` da própria exceção**, no log do
`voyager-worker_diarios-1` de 02/09 00:49:01 UTC. A linha recusada termina em:

    …, '', null, null, null, null, null)
       ↑    ↑
       │    └── classe_cnj_codigo, classe_cnj_nome, fase_codigo, fase_nome, fase_em
       └─────── grau, PREENCHIDO PELO DEFAULT DO BANCO

Mesmo INSERT, mesmo escritor atrasado, dois destinos. `grau` nasceu com o mesmo
idioma na `0052` e derrubou o dia 25/08 (10.410 runs `failed`, **32**
publicações contra 1,5 M do dia anterior); a cura foi um `SET DEFAULT ''` na
mão, em 30 s, que **nenhuma migration registrava**. As 40 falhas de `grau` do
catálogo são anteriores a essa cura.

`bulk_create` é UM statement: um CNJ inédito sem a coluna derruba a **edição
inteira**, não a linha.

### 14.3 A cura: `DEFAULT` no banco + uma catraca

**Reiniciar a frota é necessário e não é a cura.** Entre o `migrate` e o
restart existe uma janela, e nesta casa ela é de DIAS: web, scheduler, 5
drainers e ~240 workers recarregam em momentos diferentes, alguns só no
próximo `--force-recreate`. O `DEFAULT` fecha a janela porque **só é usado por
quem não conhece a coluna** — exatamente o escritor atrasado que se quer que
sobreviva. Quem já nomeia a coluna não muda em nada.

`tribunals/0057_default_no_banco_colunas_novas` devolve `DEFAULT ''` às quatro
colunas da `0054` **e a `grau`** — este último de propósito: em produção o
default de `grau` veio de um ALTER de incidente, então um banco novo nasceria
sem ele, com prod e migrations divergindo em silêncio (a doença que a `0056`
inventariou).

Três detalhes que a migration carrega no docstring e valem para a próxima:

1. **Ela pergunta antes de agir.** Se as cinco colunas já têm `DEFAULT`, não
   emite ALTER e **não pede lock nenhum**. Isso não é elegância: o `web` de
   produção roda `migrate --noinput && gunicorn` no comando do container, e
   uma migration que estoura por lock **impede o `web` de subir**.
2. **Laço com teto CURTO, nunca teto grande.** 40 tentativas de
   `lock_timeout='3s'`, uma transação por tentativa (`atomic = False`). Não se
   sobe o teto: ACCESS EXCLUSIVE **enfileira**, e um ALTER preso trava LEITURA
   no banco inteiro (63 sessões em 25/08).
3. **Falha alto no fim**, com o comando manual na mensagem. Não existe caminho
   que a marque como aplicada sem ter aplicado.

**O custo real de pegar esse lock, medido em 02/09** — e é o número que
surpreende: o bloqueador de `tribunals_process` **não** é UPDATE curto. São
SELECTs de agregação de **616 s a 1.081 s** segurando `AccessShare`, somados
ao backfill de `fase` rodando em 4 processos. Contra isso, **110 tentativas
seguidas** de `lock_timeout='3s'` perderam. Quem for aplicar ALTER em
`tribunals_process` planeje o laço em HORAS, não em minutos — e não troque
isso por um teto maior.

**Aplicada em produção em `2026-09-02 01:13:58 UTC`**, pelo worker (nunca pelo
boot do `web`). Conferido por coluna logo depois:

    classe_cnj_codigo  DEFAULT ''::character varying
    classe_cnj_nome    DEFAULT ''::character varying
    fase_codigo        DEFAULT ''::character varying
    fase_nome          DEFAULT ''::character varying
    grau               DEFAULT ''::character varying
    fase_em            NULL   (nullable, e continua sem default — está certo)

E a prova de que a rede pega o escritor atrasado — um `INSERT` cru que **não
nomeia nenhuma das cinco**, exatamente o que o Django de um container velho
emite, dentro de uma transação revertida:

    INSERT INTO tribunals_process (numero_cnj, tribunal_id, …) VALUES (…)
      RETURNING id, grau, classe_cnj_codigo, classe_cnj_nome, fase_codigo, fase_nome, fase_em
    → (106503467, '', '', '', '', '', None)      ← ANTES da 0057: NotNullViolation

**A catraca — que é o que faltava.** A regra ("coluna NOT NULL nova em tabela
que já tem escritor em produção NUNCA fica sem `DEFAULT` no banco") já estava
escrita em `.ia/OPS.md` desde **25/08**. A `0054` a repetiu **seis dias
depois**. Não faltou conhecimento: faltou cobrança. `tests/test_default_no_banco.py`
congela as **50** colunas NOT NULL-sem-`DEFAULT` das quatro tabelas quentes
(`tribunals_process`, `_movimentacao`, `_processoparte`, `_parte`) e reprova
qualquer migration que **acrescente** uma. Identidade e chave (`numero_cnj`,
`tribunal_id`, `processo_id`, `parte_id`) ficam de fora de propósito: ali o
INSERT sem a coluna TEM que explodir. Tem controle positivo — se a sonda
deixar de enxergar as 4 tabelas, ela falha em vez de passar medindo o vazio.

**Controle negativo, porque catraca que nunca reprovou não se sabe se trava.**
Rodando a `0057` para trás num banco limpo (estado da `0056`) e aplicando a
mesma sonda, ela acusa **exatamente** as cinco colunas do incidente —
`classe_cnj_codigo`, `classe_cnj_nome`, `fase_codigo`, `fase_nome`, `grau` —
e nenhuma outra. É a prova de que o teste teria reprovado a `0054` no dia em
que ela foi escrita, e de que o `reverse` da `0057` funciona.

**Varredura das outras tabelas quentes, pedida e feita:** o padrão NOT
NULL-sem-`DEFAULT` é o idioma do Django e está em toda parte (22 colunas em
`tribunals_movimentacao`, 19 em `_process`, 8 em `_parte`, 5 em
`_processoparte`). Isso **não** é perigo por si: coluna que sempre esteve lá
não tem escritor que a desconheça. O perigo é a coluna NOVA, e a varredura por
`attnum` + histórico de migrations diz que **as únicas colunas acrescentadas a
tabela quente desde que os containers de hoje subiram são as cinco da `0052` e
da `0054`** — `0053` é índice, `0055` é seed, `0056` é estado e FK. Nenhuma
outra bomba armada hoje; a catraca é para a de amanhã.

### 14.4 A prova de que reabriu

`worker_diarios` reiniciado em `2026-09-02T01:01:05Z`
(`docker compose -f docker-compose-workers.yml restart worker_diarios` — o
`-f` não é opcional). Prova comportamental, não `StartedAt`:

| `IngestionRun` | quando | movs novas | processos novos |
|---|---|---:|---:|
| 238925 / 238926 / 238927 | 02/09 00:48-00:49 (antes) | **0** | **0** |
| 238929 (mesma unidade, `4123-15`) | 02/09 01:01 (depois) | **45.464** | **9.142+** |

A edição `4123-15` (15/01/2025, caderno 4 — Interior Parte III) fechou **`ok`
com 45.464 `itens_gravados` e 0 duplicados**. As três falhas anteriores e o
sucesso posterior são da MESMA chave, no MESMO caderno: antes ela morria em
3-16 s no primeiro `bulk_create`, com 0.

Ressalva de leitura: sob a contenção medida no §14.3 (backfill de `fase` em 4
processos + agregações de ~1.000 s) o caderno levou **~9 min** — 01:01:46 →
~01:11 UTC —, contra os 99-498 s medidos em 24/08. A terceira porta divide I/O
com o resto e é a primeira a sentir.

⚠️ **E aqui eu quase publiquei um número errado, pela armadilha de fuso que a
casa já registra.** O `asctime` do log sai em **-03** e todo o resto (`now()`,
`started_at`, `docker inspect`) sai em **UTC**, no MESMO processo. Ler
`22:01:46` do log como se fosse UTC transforma 9 minutos em 50 e uma vazão de
~4 min/unidade em "dias". Toda hora deste §14 é UTC, de propósito.

### 14.5 Veredito da segmentação — as 2 falhas que NÃO eram o INSERT

Estas duas são de outra natureza e são mais importantes que as 255. Medido
baixando o caderno e rodando o mesmo segmentador do coletor, offline:

| unidade | data | caderno | cobertura | CNJs fora |
|---|---|---|---:|---:|
| `4155-11` | 2025-02-28 | 2ª Inst. — **Entrada e Distribuição** Parte I | **68,4%** | 6.170 |
| `4153-19` | 2025-02-26 | 2ª Inst. — Processamento Parte II | **92,6%** | 1.212 |

**`4155-11` — 100% dos 6.170 órfãos são a relação da DEPRE.** Não é ruído, é
um SEXTO formato de bloco que o segmentador não conhece:

    3.833 (62,1%)  linha 'Processo: <CNJ>'            ← o precatório
    2.337 (37,9%)  linha 'Processo de origem: <CNJ>'  ← o processo de conhecimento

O registro impresso é este, campo a campo, e é **exatamente o produto desta
casa**:

    Nº de ordem cronológica: 2/2026
    Processo: 0313356-07.2024.8.26.0500          ← foro 0500 = DEPRE
    Processo de origem: 0000003-92.2024.8.26.0040/0003
    Vara: 2ª VARA - Foro: FORO DE AMÉRICO BRASILIENSE
    Reqte: GODOY E BRASILEIRO ADVOGADOS
    Advogado: GILVANY MARIA MENDONCA B MARTINS (OAB 54762/SP)
    Entidade devedora: MUNICÍPIO DE AMÉRICO BRASILIENSE

Credor, advogado com OAB, **ente devedor**, ordem cronológica, o CNJ do
precatório E o CNJ de origem — o par que a memória da casa registra como
"incidentes CNJ vinculados", impresso pela própria fonte. O segmentador
descarta o bloco inteiro.

**`4153-19` — 96,5% dos 1.212 órfãos são pauta numerada**, um SÉTIMO formato:
a linha abre com um ordinal antes do número (`3 - 0000239-66.2022.8.26.0120 -
Processo Digital. …`), e a âncora `numero_primeiro` do segmentador exige que a
linha COMECE no CNJ. O resto é resíduo legítimo e **não é perda**: 10 (0,8%)
são citação de jurisprudência dentro do corpo de uma decisão
(`(TJSP; Apelação Cível 0000051-09.2007.8.26.0279; Relator …)`) e 7 (0,6%) são
CNJ partido entre linhas.

**O achado que dói mais: as duas edições GRAVARAM.** O gate roda depois da
persistência, então as linhas estão no acervo e a unidade diz `falha` com
`itens_gravados=0`:

| unidade | movimentações no banco | processos distintos | `itens_gravados` |
|---|---:|---:|---:|
| `4155-11` | **9.985** | 8.868 | 0 |
| `4153-19` | **13.490** | 12.907 | 0 |

É o "dado coletado pela metade" da regra nº 1 do `CLAUDE.md`, com a agravante
de o watermark NEGAR que ele existe. Reprocessar não conserta: o formato que
falta continua faltando, e a gravação é idempotente. As duas ficam como dívida
escrita, **fora** do lote reprocessado, de propósito.

**ATUALIZAÇÃO (02/09, até 01:48Z) — não são 2 edições, são DUAS FAMÍLIAS
SISTEMÁTICAS.** Com a `0057` aplicada e o lote drenando, mais duas reprovaram,
uma de cada família:

| unidade | data | caderno | cobertura | família |
|---|---|---|---:|---|
| `4155-11` | 28/02/2025 | 11 — Entrada e Distribuição | **68,4%** | DEPRE |
| `4159-11` | 10/03/2025 | 11 — Entrada e Distribuição | **83,1%** | DEPRE |
| `4153-19` | 26/02/2025 | 19 — Processamento II | **92,6%** | pauta numerada |
| `4162-19` | 13/03/2025 | 19 — Processamento II | **92,7%** | pauta numerada |

**A `4159-11` foi conferida por sonda barata** (baixa e extrai o texto, não
segmenta) e a assinatura da DEPRE é aritmeticamente perfeita — as quatro
âncoras do registro aparecem **exatamente o mesmo número de vezes**:

    2.426 páginas · 22.847 CNJs impressos · 3.852 fora de bloco (16,9%)
    'Nº de ordem cronológica' .. 2.568
    'Entidade devedora' ........ 2.568
    'Processo:' ................ 2.568
    'Processo de origem:' ...... 2.568
    pauta numerada ............. 0      ← família pura, sem mistura

Esse quádruplo pareado é o gabarito mecânico de graça para quem for escrever o
parser: se o número dos quatro não bater, o parser errou.

**E aqui está a resposta parcial à pergunta que o §14.5 deixou aberta.** A
relação da DEPRE varia de tamanho — **3.833** registros na `4155-11` (68,4%) e
**2.568** na `4159-11` (83,1%). A cobertura anda JUNTO com o tamanho da
relação, e o piso do gate é 95%. Pela mesma aritmética, uma relação de ~760
registros num caderno de ~23 mil CNJs deixaria a edição **acima de 95%** e ela
fecharia `ok` com a relação inteira perdida, em silêncio. **Isso é
extrapolação, não medição** — nenhum dia assim foi observado, e o §14.7 segue
dizendo que não foi medido. Mas deixa de ser hipótese solta: é o mesmo
mecanismo, num tamanho menor.

São **47 unidades de caderno 11 e 47 de caderno 19** catalogadas ⇒ até **94**
unidades expostas a uma das duas famílias. Isto **eleva a prioridade** do item
1 do §14.7 de "dívida conhecida" para "a próxima coisa a fazer nesta porta".

Note o que os mesmos eventos provam do outro lado: das **22** unidades
processadas depois do restart, **as 2 únicas `failed` foram por cobertura** —
nenhuma por `NotNullViolation`. O gate reprovando é o gate FUNCIONANDO; foi ele
que separou "o INSERT quebrou" de "o parser não conhece o formato".

**Controle, para não confundir o gate com o formato:** `4161-11` (12/03/2025)
e `4154-11` (27/02/2025), ambos `ok`, deram **100,0%** de cobertura, **0**
órfãos e **0** ocorrências de "ordem cronológica" no PDF. A relação da DEPRE é
PERIÓDICA, não diária — quando ela aparece, o gate a pega; a pergunta que fica
aberta é se existe dia em que ela aparece pequena o bastante para a edição
fechar acima de 95% e a perda passar calada. **Não foi medido.**

### 14.6 Cobertura real do DJE/TJSP — medida contra o denominador da FONTE

O denominador não é estimado: veio do próprio catálogo do e-SAJ, numa
requisição, em 02/09/2026 (`col.catalogar(...)`, nada baixado):

    faixa que temos catalogada .... 2009-06-15 → 2025-03-13
    dias que a fonte declara ......  3.671  (já descontadas as 406 datasSemDiario)
    unidades declaradas ...........  29.368  (3.671 dias × 8 cadernos)
    unidades NOSSAS na faixa ......     377
      ok ..........................      62
      pendente/falha ..............     307
      inexistente/vazia/sem_aprov .       8

    COBERTURA REAL = 62 / 29.368 = 0,211%
    (contando também vazia+inexistente+sem_aproveit: 70/29.368 = 0,238%)

> ⚠️ **O denominador desta subseção está SUPERESTIMADO em 29,8% — ver §19.1.**
> A ressalva 1 logo abaixo estava certa: `dias × 8` é teto, não igualdade.
> Medido em 03/09/2026 por bissecção sobre o catálogo (124 requisições `HEAD`,
> zero PDF), os cadernos **19 e 20 só existem a partir de 2023-11-27** — logo a
> faixa tem **22.620** unidades, não 29.368, e as coberturas deste §14.6 e do
> §15.4 devem ser lidas com esse denominador.

E a jazida inteira que a fonte declara, para dimensionar: **33.296 unidades**
em **4.162 dias**, 2007-10-01 → 2025-07-22.

**Duas ressalvas honestas sobre esse denominador**, porque ele é um teto e não
uma igualdade:

1. o catálogo multiplica dia × 8 cadernos porque **quais** cadernos existem em
   cada data só o download responde (§ do coletor). Alguns dias têm menos de 8
   — é o que gera `inexistente`. O denominador real está entre ~26 mil e
   29.368, e a cobertura real está entre 0,21% e 0,24%.
2. o caderno 5 (Editais e Leilões, `cdCaderno=14`) está fora dos dois lados —
   não entra no catálogo nem na conta.

De qualquer forma a ordem de grandeza não muda e é ela que importa: **a
terceira porta tem dois DÉCIMOS de um por cento** do DJE/TJSP na faixa que já
catalogou, e menos de **0,2%** da jazida inteira. `doe-sp` e `qd-municipal`,
com **1 edição cada, do mesmo dia (21/08/2026)**, são prova de conceito, não
coleta — e a `doe-sp` fechou `vazia`.

### 14.6b O reprocessamento das 307 — o que foi feito e a que custo

As **305** unidades que a `NotNullViolation` fechou (256 `falha` + 49
`pendente`, medidas às 01:11 UTC) foram reabertas com
`status='pendente', tentativas=0` — sem isso o `tick` nunca mais tocaria nelas,
porque `MAX_TENTATIVAS=5` e 250 já estavam em 5 — e enfileiradas na `diarios`.
As **2** de segmentação ficaram FORA, de propósito (§14.5).

**Por que enfileirar direto, e não esperar o orçamento:** o teto de 8
unidades/24 h existe pela projeção de disco do §13.2, e ela é sobre **26.392**
unidades (~772 GB). Estas 305 valem, à média medida de ~25 mil linhas por
caderno, ~7,6 M linhas ⇒ **~8 GB de índice** contra **995 GB livres** no nó de
ES: +0,3 ponto percentual de disco. O orçamento não é o instrumento certo para
frear um lote deste tamanho, e o kill switch (`diarios_pausar tjsp-dje`)
continua a um comando de distância. Verificados antes de soltar, os 10
critérios do §13.7 que se pode medir: ES em 66% (teto 85), `write.rejected` 0,
fila `es_index` em 0 (teto 5.000), load1 da `.102` em 5,5 (teto 40),
`MemAvailable` 17,7 GiB (piso 6).

**A vazão medida:** **6 unidades fechadas em 24 min** com as 2 réplicas ⇒
~4 min por unidade, ~15 unidades/h. Nesse ritmo as 305 levam da ordem de
**20 h**, não dias — mas o ritmo depende do Postgres, que está dividido com o
backfill de `fase` (4 processos) e com agregações de até 1.081 s, e pode piorar
sem aviso. Quem retomar mede pelo par `EdicaoDiario.status` +
`sum(IngestionRun.movimentacoes_novas WHERE started_at >= '2026-09-02 01:01Z')`,
contra a linha de base congelada às 01:11:20 UTC de 02/09/2026:

    baseline (01:11:20Z) .. ok 62 · itens_gravados 1.815.386
                            runs tjsp-dje: 1.312 failed / 73 success

**Medido em `2026-09-02 01:24:50 UTC`, 24 minutos depois do restart** (o lote
continua drenando; estes números são um PISO, não o total):

| | baseline (01:11:20Z) | 01:24:50Z | delta |
|---|---:|---:|---:|
| `EdicaoDiario` `ok` | 62 | **68** | +6 |
| `sum(itens_gravados)` | 1.815.386 | **2.006.577** | **+191.191** |
| `IngestionRun` do `tjsp-dje` desde 01:01Z | — | **6 `success`, 2 `running`, 0 `failed`** | — |
| `sum(movimentacoes_novas)` desses runs | — | **238.590** | — |
| `sum(processos_novos)` desses runs | — | **44.870** | — |
| fila `diarios` | 305 | 298 | — |

**Zero `failed` em 8 runs depois do restart, contra 1.312 antes.** É esse par
que fecha o veredito — não o `StartedAt` do container. Os 238.590 passam dos
191.191 de `itens_gravados` porque duas unidades ainda estavam `running` na
hora da medição e ainda não tinham carimbado a edição.

Critérios do §13.7 relidos durante o lote: ES em **66%** (teto 85),
`write.rejected` **0**, fila `es_index` no máximo **374** (teto 5.000), load1
da `.102` **10,27** (teto 40), `MemAvailable` **16,2 GiB** (piso 6). Nenhum
acionado.

### 14.7 O que NÃO foi feito, e por quê

| pendência | estado |
|---|---|
| os dois formatos do §14.5 no segmentador (DEPRE e pauta numerada) | **NÃO feito, e é a próxima coisa a fazer.** É trabalho de parser com fixture real e gate próprio, e mexer no segmentador no meio de um reprocessamento de 305 unidades trocaria uma perda medida por uma não medida. Mas as duas famílias já reprovaram **4** edições (2 de caderno 11, 2 de caderno 19) e há **47 + 47** catalogadas: o lote vai produzir mais `falha` destas famílias enquanto drena |
| medir se a relação da DEPRE já passou calada em edição `ok` | **NÃO medido.** Exige baixar e re-segmentar as 62 unidades `ok` (≈ 3 h de CPU) |
| as 9.985 + 13.490 linhas que as 2 edições reprovadas gravaram | **estão no acervo e o watermark as nega.** Ninguém as reconcilia hoje |
| `IngestionRun` `failed` do `tjsp-dje` (1.312) | não foram limpos — ficam como evidência datada |
| ampliar o orçamento de 8 unidades/24 h | **não mexido.** §13.10 é claro: primeiro o disco do ES |

---

## 15. Os dois formatos que o segmentador não conhecia (#118, 02/09/2026)

> **O que mudou:** o segmentador do DJE/TJSP passou de cinco para **sete**
> formatos de bloco. Os dois novos são a **relação da DEPRE** (ordem
> cronológica de precatórios) e a **pauta numerada** de julgamento. Não foram
> descobertos lendo o caderno: apareceram porque **doze edições reprovaram o
> gate de cobertura** entre 00:00 e 06:00 UTC de 02/09, e 100% dos CNJs órfãos
> estavam num dos dois.

### 15.1 Formato 6 — a relação da DEPRE, e por que ela vale mais que as outras

    NATUREZA ALIMENTÍCIA                          ← cabeçalho da relação
    Nº de ordem cronológica: 278/2026 (CANCELADO)
    Processo: 0156916-80.2024.8.26.0500           ← o PRECATÓRIO (foro 0500)
    Processo de origem: 0001538-65.2023.8.26.0210/0001
    Vara: JUIZADO ESPECIAL CÍVEL E CRIMINAL - Foro: FORO DE GUAÍRA
    Reqte: VERA LÚCIA DA SILVA E SILVA            ← o CREDOR
    Advogado: MARINA MACHIAVELI BRUNHARA (OAB 396304/SP)
    Entidade devedora: SPPREV - SÃO PAULO PREVIDÊNCIA        ← QUEM DEVE
    Entidade agrupadora: FAZENDA DO ESTADO DE SÃO PAULO
    Advogados: FERNANDA RIBEIRO DE MATTOS LUCCAS (OAB 136973/SP)
    WLADIMIR RIBEIRO JUNIOR (OAB 125142/SP)       ← continuação SEM rótulo

É o **único lugar do acervo** onde o credor, o ente devedor e o par
precatório↔processo de origem vêm impressos no MESMO registro. O tamanho do
buraco que isso fechava, medido por outra porta em 02/09/2026: `ENT. DEVEDORA`
existia em **duas** `ProcessoParte` no acervo inteiro, contra **4.550.742** de
`AUTOR`. A tela "Quem deve" do Overview não tinha de onde se alimentar.

**A régua vem da fonte, e isso é raro.** A relação imprime quatro campos
obrigatórios por registro e eles aparecem o MESMO número de vezes — medido na
relação inteira de 10/03/2025:

    Nº de ordem cronológica ... 2.568
    Processo .................. 2.568
    Processo de origem ........ 2.568
    Entidade devedora ......... 2.568

O teste conta os quatro marcadores NO PDF e exige que batam com o número de
blocos. Se o parser errar, quem reprova é o caderno — não um número que eu
escrevi à mão. Não é toda hora que um formato desconhecido vem com a própria
conferência embutida.

**Resultado no caderno 4159-11 (10/03/2025), com o segmentador novo:**

| | antes | depois |
|---|---:|---:|
| cobertura de CNJ | **83,1%** | **100,0%** (0 de 22.847 fora) |
| blocos `precatorio` | 0 | **2.568** |
| com ente devedor no polo **P** | 0 | **2.568 / 2.568** |
| com credor (polo A) | 0 | 2.553 |
| com advogado + OAB | 0 | 2.551 |

O total de blocos do caderno bateu com a coleta real em produção: a soma
offline (2.568 precatório + 17.751 pauta + 17 de 2ª instância = **20.336**) é
exatamente o `itens_gravados` que a unidade fechou.

**Abstenções escritas, para não virarem descoberta amanhã:**

| decisão | por quê |
|---|---|
| `nome_classe` fica **vazio** | a relação não imprime classe da TPU; escrever "Precatório" contaminaria `Process.classe_nome` via `preencher_classe_via_djen` com rótulo que a fonte não disse |
| `sucessor`/`favorecido`/`invtante` ficam com **polo vazio** | 15 registros em 2.568, e o lado deles não é óbvio o bastante para valer um chute. O nome é gravado assim mesmo, com o papel verbatim |
| `Processo de origem` **não ganha coluna** | fica verbatim no `texto`, de onde `search/entidades_texto.py` já o colhe para `cnjs_citados` — o campo de incidente vinculado que já existe no índice. Migration em tabela de 1,5 bi de linhas para um dado que já tem lugar seria custo puro |
| `(CANCELADO)` não vira `status` | o DJEN grava `'P'` ali; um terceiro vocabulário no mesmo campo é a dívida que o STF já criou (§10). Fica verbatim no texto |
| `NATUREZA ALIMENTÍCIA` vai para `tipo_comunicacao` | é o que a fonte imprime, e é o que decide em qual fila de pagamento o crédito entra |

### 15.2 Formato 4b — a pauta numerada, e o bug que ela revelou

A pauta de julgamento imprime a POSIÇÃO na sessão antes do número:

    3 - 0000239-66.2022.8.26.0120 - Processo Digital. Petições para juntada …

A âncora do formato 4 exigia que a linha COMEÇASSE no CNJ. Passou a aceitar um
ordinal opcional na frente — e o `Processo (Digital|Físico)` continua
obrigatório, que é o que impede a âncora frouxa de partir um ato ao meio dentro
de citação de jurisprudência quebrada de linha.

No caderno 4162-19 (13/03/2025): **92,7% → 99,7%**. Os 35 CNJs que sobram são
citação de jurisprudência no corpo de decisões — menção, não perda.

**E um bug que só apareceu por tabela.** A ladainha do art. 7º da Res. 551/2011
vem COLADA em "Processo Digital" e tem **137 caracteres**.
`_classe_do_cabecalho` comparava o marcador por IGUALDADE, então esse segmento
virava a CLASSE, estourava o teto de 120 e a função abstinha — em **toda** a
pauta, inclusive na que o segmentador já reconhecia antes desta mudança. Com
prefixo em vez de igualdade:

    caderno 4159-11 ... 17.751 / 17.751 blocos passam a sair com classe
    caderno 4162-19 ...  1.139 /  1.139

### 15.3 O achado que vale mais que os dois parsers: o gate é cego para bloco ERRADO

A pergunta que o §14.5 deixou aberta era "existe dia em que a perda passa por
baixo do gate?". A resposta foi medida, e ela é **diferente para cada um dos
dois formatos** — o que é mais interessante que um "sim" ou um "não".

Método: baixar as **45 edições que fecharam `ok`** dos cadernos 11 e 19,
extrair o texto (sem segmentar — a pergunta é "quantos registros existem no
PDF") e contar os marcadores dos dois formatos. Sonda barata, com controle
positivo: ela tem que devolver não-zero onde se sabe que há registro.

| | edições `ok` medidas | registros do formato encontrados |
|---|---:|---:|
| relação da DEPRE (caderno 11) | 23 | **0** |
| pauta numerada (caderno 19) | 22 | **7.917**, em **22 de 22** edições |

**A DEPRE nunca passou calada: o gate a pegou todas as vezes.** Nas 23 edições
verdes do caderno 11 não há um único `Nº de ordem cronológica`. A relação é
tudo-ou-nada — quando aparece, tem milhares de registros e derruba a cobertura
para 42-83%, sempre abaixo do piso.

**A pauta numerada passou calada em TODAS as 22.** E o número que explica o
porquê é o mesmo em todas: o **tamanho da pauta como fração dos CNJs impressos
no caderno**.

| edição | entradas de pauta | CNJs no caderno | % | veredito do gate |
|---|---:|---:|---:|---|
| 4148-19 | 73 | 12.091 | **0,60%** | `ok` |
| 4143-19 | 99 | 11.021 | **0,90%** | `ok` |
| 3985-19 | 157 | 10.351 | **1,52%** | `ok` |
| 4136-19 | 387 | 14.198 | **2,73%** | `ok` |
| 4152-19 | 498 | 14.520 | **3,43%** | `ok` |
| 4155-19 | 726 | 17.082 | **4,25%** | `ok` |
| **4139-19** | **714** | **15.728** | **4,54%** | `ok` ← o maior que passou |
| … | | | | |
| **4162-19** | ~896 | 12.197 | **~7,3%** | **falha** (92,7%) |
| **4153-19** | 1.170 | 16.303 | **~7,4%** | **falha** (92,6%) |

**22 de 22 edições verdes ficam ABAIXO de 5%; 6 de 6 reprovadas ficam ACIMA.**
A folga do piso de 95% é exatamente a linha divisória, e a separação é
monotônica — não há um único caso fora de lugar. A extrapolação que o §14.5
registrou como "aritmética, não medição" virou medição: **uma relação pequena
passa por baixo do gate**, e passou 22 vezes.

**A lição, que é maior que estes dois parsers:** um gate de proporção não é um
gate de completude. Ele reprova a perda GRANDE e absolve a pequena pelo mesmo
mecanismo que o faz funcionar — e um formato inteiro desconhecido, mas de
volume modesto, atravessa todas as edições sem nunca acender uma luz. É a
assinatura do `CLAUDE.md` ("run verde, log limpo, número redondo") com a
agravante de a régua ser cúmplice. **Piso percentual precisa de um segundo
eixo**: um inventário do que a fonte imprime, contado por marcador, não só a
fração do que caiu dentro de algum bloco.

**E a perda é de publicação inteira, não de atribuição.** Conferido no banco
para três edições verdes, com `LIKE` como filtro sobre a âncora
`tribunal + data_disponibilizacao`:

| edição | pauta impressa | movs da edição | movs que ABREM numa entrada de pauta |
|---|---:|---:|---:|
| 3985-19 (12/06/2024) | 157 | 9.835 | **0** |
| 4136-19 (03/02/2025) | 387 | 12.937 | **0** |
| 4137-19 (04/02/2025) | 430 | 13.477 | **0** |

Nenhuma das 974 entradas virou linha própria. (Um controle que quase virou
falso positivo: contar as movs que CONTÊM a ladainha do art. 7º não prova
nada — ela está no cabeçalho de praticamente toda publicação de 2ª instância,
9.650 de 9.835 numa edição. Marcador onipresente não é evidência.)

**Dívida NOVA, e não foi resolvida:** reprocessar essas 22 edições recupera a
pauta, mas o `external_id` é hash do TEXTO — a linha nova não substitui a
velha, convive com ela. Reprocessar as 22 é decisão de volume que não foi
tomada aqui.

### 15.4 O que entrou em produção, e o que ficou faltando

Deploy do parser: `worker_diarios` da `.102` reiniciado em
`2026-09-02T15:29:03Z` (`docker compose -f docker-compose-workers.yml restart
worker_diarios` — o `-f` não é opcional).

**As 12 edições que o gate reprovou foram reabertas** (`status='pendente'`,
`tentativas=0` — sem isso o `tick` nunca mais as tocaria) e reprocessadas.
Estado final, às **15:43:28 UTC** — o lote inteiro drenou em ~14 minutos:

| | valor |
|---|---:|
| das 12, fecharam `ok` | **12 de 12** |
| `itens_gravados` somados | **194.874** |
| `IngestionRun` desde o deploy | **13 `success`** |
| reprovas por COBERTURA | **0** (eram 12) |
| `movimentacoes_novas` | **35.576** |
| `processos_novos` | **12.446** |

Os 3 `failed` do período não são do gate e estão documentados: dois são runs
órfãos do restart do worker, fechados à mão com a causa real (o
`watchdog_ingestao` não filtra por fonte e os chamaria de "worker crashou"), e
o terceiro é o `deadlock detected` que eu mesmo provoquei — ver o aviso abaixo.

Cobertura antes → depois, por edição reprocessada:

| edição | cobertura antes | itens gravados depois |
|---|---:|---:|
| 4142-11 | **42,0%** | 17.089 |
| 4155-11 | 68,4% | 14.207 |
| 4138-11 | 71,8% | 14.727 |
| 4149-11 | 75,3% | 26.006 |
| 4139-11 | 77,2% | 19.617 |
| 4159-11 | 83,1% | 20.336 |
| 4153-19 | 92,6% | ✓ |
| 4162-19 | 92,7% | 11.167 |
| 4145-19 | 93,3% | 13.643 |
| 4135-19 | 94,3% | ✓ |
| 4140-19 | 94,5% | 14.654 |
| 4141-19 | 94,5% | 11.742 |

`movimentacoes_novas` (35.576) é muito menor que `itens_gravados` (194.874)
porque as edições reprovadas **já haviam gravado** a maior parte das linhas: o
gate roda DEPOIS da persistência (§14.5). Os **35.576** são o que os dois
formatos novos acrescentaram de fato, e os **12.446 processos novos** são
acervo que não existia em lugar nenhum.

⚠️ **Erro meu, registrado porque o §10 já avisava e eu caí assim mesmo.** Ao
re-enfileirar as unidades eu usei DOIS `job_id` diferentes para a mesma unidade
(`r118:` e `r118b:`) achando que estava só garantindo que nenhuma ficasse de
fora. As duas réplicas do `worker_diarios` pegaram cada uma o seu, rodaram o
MESMO caderno em paralelo, e os dois `bulk_create` se travaram:
`deadlock detected`. O `job_id` determinístico dedupa o JOB, não a INTENÇÃO de
enfileirar — mudar o prefixo desliga a única proteção que existe. **Uma unidade,
um `job_id`, e confira a fila antes de enfileirar de novo.**

**O que ficou faltando — escrito para não virar surpresa:**

| pendência | estado |
|---|---|
| as 22 edições `ok` do caderno 19 com **7.917** entradas de pauta perdidas | **NÃO reprocessadas.** Recuperá-las é decisão de volume, e o `external_id` é hash do TEXTO: a linha nova convive com a velha em vez de substituí-la |
| segundo eixo do gate (inventário por marcador, não só fração) | **NÃO feito.** É a lição do §15.3 e vale para as três fontes, não só para esta |
| as 9.985 + 13.490 linhas que as 2 edições do §14.5 gravaram com `itens_gravados=0` | reconciliadas pelo reprocessamento destas 12 — as unidades agora fecham `ok` com o total correto |
| caderno 5 (Editais e Leilões) | continua fora do catálogo, com 4,7% de cobertura medida (§1) |
| a cobertura do DJE/TJSP | **0,24%** (70 de 29.368 unidades declaradas na faixa, §14.6). Consertar o parser não move esse número — mas coletar 29 mil edições com o parser cego seria coletar 29 mil vezes um dado pela metade |

---

## 16. Extraído não é PARTE — o ente devedor não aterrissava (02/09/2026)

> **O parser extraía e o dado não chegava.** O formato 6 (§15.1) entrega
> `Entidade devedora` no polo passivo, e o JSONB provou que chegou ao banco:
> **2.568 de 2.568** movimentações da relação de 10/03/2025 com
> `papel='ENTIDADE DEVEDORA'` e `polo='P'` em `Movimentacao.destinatarios`. E
> `ProcessoParte` desses processos: **ZERO**. Para o produto isso é quase o
> mesmo que não ter extraído — a tela "Quem deve" lê `ProcessoParte`, não o
> JSONB. É o §12 outra vez, em outro campo: "coletado" não era "buscável";
> agora "extraído" não era "parte".

### 16.1 Onde o vínculo se perdia — QUATRO pontos, todos medidos antes do código

| # | ponto | como foi medido |
|---|---|---|
| A | **ninguém promovia** | a promoção existe (`services/partes_djen.py`) mas é backfill por FAIXA DE PK disparado à mão. No Redis: **zero** checkpoints de shard. E as **218.068** `ProcessoParte` criadas nas 24 h anteriores eram todas do enricher (`fonte IS NULL`), nenhuma `fonte='djen'`. Processo que nasce hoje de uma coleta tem pk acima de qualquer faixa já varrida |
| B | **o papel morria** | `specs_do_processo` lia o `polo` do JSONB (por isso o passivo sobrevivia) e tirava o `papel` só de uma tabela HTML no texto, pensada para o eproc. Rodando a função sobre o JSONB REAL de produção, os três saíam com `papel=''` |
| C | **a janela de 3 movs** | `promover_lote` lê as `JANELA_MOVS=3` mais recentes. Dos 2.568 processos, **823 (32%) têm mais de 3** — e **nenhum** deles ganhou o ente devedor; os 1.445 que ganharam estão TODOS na faixa de até 3 |
| D | **`sem_processoparte`** | pula processo que já tenha QUALQUER parte. Depois de C, os 823 tinham linhas com `papel=''` vindas de outras publicações — e por "já terem parte" nunca mais receberiam a linha MELHOR |

C e D só apareceram DEPOIS de consertar A e B e medir de novo. Nenhum dos
quatro era visível no código sem olhar o dado.

### 16.2 As quatro curas

| cura | onde |
|---|---|
| `papel_da_fonte()` + precedência **FONTE > TEXTO** | quem rotulou o campo foi o diário; `papeis_do_texto` é inferência e só entra quando a fonte cala. Com controle negativo: o caminho eproc (79,2% das publicações) continua tirando AUTOR/RÉU da tabela |
| `diarios.jobs.promover_partes` + entrega no `on_commit` | mesmo remédio do §12: entregar na GRAVAÇÃO, não depender de varredura que alguém precisa lembrar de rodar |
| `ler_movimentacoes_por_pk` + `promover_lote(movs_por_processo=)` | o coletor SABE quais linhas gravou; o `pks` do lote já existia no mesmo `on_commit`. Trocar heurística por fato |
| `sem_parte_de_terceiro` (`fonte IS NULL`) | protege o ENRICHER, que é o que aquela regra queria proteger — não a nossa própria linha `papel=''` |

**Assimetria deliberada:** fila fora do ar **não** derruba a coleta aqui.
Índice ausente torna a edição inútil (não é buscável) e por isso derruba
(§12); parte ausente é enriquecimento que o `backfill_partes_djen` recupera
depois, e a movimentação — que é o acervo — já está gravada.

**E a procedência passou a dizer a verdade.** `fonte='djen'` significava
"promovida de `Movimentacao.destinatarios`" quando só o DJEN escrevia ali.
Agora os diários escrevem no MESMO JSONB, com dado que o DJEN não tem, e o
carimbo é `'diario'` (`FONTE_DIARIO`). A contagem independente acompanha o
carimbo — contar por `'djen'` uma passada que gravou `'diario'` devolveria 0 e
o job reportaria "não gravei" tendo gravado.

### 16.3 ANTES/DEPOIS de uma edição, contado nos dois lados

Edição `4159-11` (10/03/2025), a relação inteira da DEPRE — 2.568 registros:

| | ANTES (16:10:46Z) | DEPOIS (16:22Z) |
|---|---:|---:|
| `ProcessoParte` dos 2.568 processos | **0** | **16.871** |
| `ENTIDADE DEVEDORA` em polo **passivo** | **0** | **2.568** (100%) |
| `ENTIDADE AGRUPADORA` em passivo | 0 | 317 |
| `REQTE` em polo ativo | 0 | 2.504 |
| no acervo INTEIRO, `ENTIDADE DEVEDORA` passivo | **0** | **2.568** |

Os 2.568 saem em duas procedências — **1.445 `fonte='djen'`** (promovidas
antes do carimbo novo) e **1.123 `fonte='diario'`** —, e isso fica registrado
porque a constraint é `(processo, parte, polo, papel)` e **não inclui `fonte`**:
re-promover não reescreve o carimbo antigo.

E o que NÃO foi tocado: as **101** `ENT. DEVEDORA` em polo `outros` que já
existiam continuam lá, intactas. São do enricher e-SAJ, e `sem_parte_de_terceiro`
existe exatamente para não encostar nelas.

⚠️ **Nota de unidade, porque ela já causou um engano:** `tribunals_partepapel`
conta ENTIDADES DISTINTAS, não participações. "2 partes com ENT. DEVEDORA no
acervo inteiro" eram 2 entidades em 101 participações. Para medir cobertura de
papel, a tabela é `tribunals_processoparte`.

### 16.4 As 22 edições verdes do caderno 19 — número e recomendação

O §15.3 deixou 7.917 entradas de pauta em 22 edições `ok` como "decisão de
volume". Com o vínculo funcionando elas passam a valer parte de verdade, e o
custo de recuperá-las foi medido em vez de estimado — numa edição do caderno 19
JÁ reprocessada hoje (`4162-19`):

    movs da edição depois do reprocessamento .......... 11.198
    linhas que ABREM numa entrada de pauta (novas) .....   925
    linhas ANTIGAS com pauta no meio do texto (órfãs) ..     2

**Duas.** A duplicação que eu tinha registrado como risco não se materializa
neste caderno: as entradas de pauta estavam ÓRFÃS (descartadas), não engolidas
por um bloco vizinho — por isso reprocessar cria linha nova sem deixar linha
velha para trás.

**Recomendação (quem decide o volume é o dono):** reprocessar as 22.

| | valor |
|---|---:|
| publicações recuperadas | ~**7.917** |
| linhas antigas órfãs, extrapolando os 2 medidos | ordem de **dezenas**, não milhares |
| custo de relógio | ~10 min/caderno com 2 réplicas ⇒ ~**2 h** |
| partes que vêm junto | Agravante/Agravado rotulados, com OAB (é 2ª instância) |
| o que **não** vem | ente devedor — a DEPRE é caderno 11, não 19 |

Não é o "quem deve"; é acervo de 2ª instância com parte rotulada. Vale, e vale
menos que a DEPRE — por isso é recomendação, não ação tomada.

### 16.5 O que ficou de fora

| pendência | estado |
|---|---|
| as 22 edições do §16.4 | **não reprocessadas** — recomendação acima, com número |
| as 23 edições `ok` do caderno 11 | não precisam: têm **zero** registros da DEPRE (§15.3) |
| `fonte='djen'` nas 1.445 linhas promovidas antes do carimbo novo | ficam como estão; a constraint não inclui `fonte` |
| o resto do acervo (86,7 M processos com destinatário e sem parte) | continua dependendo do `backfill_partes_djen`, que **não tem checkpoint e não está rodando** — é achado desta medição e não foi endereçado aqui |
| segundo eixo do gate (§15.3) | continua não feito |

---

## 17. O caderno 19 inteiro — o que o reprocessamento das 47 mediu

> **A ordem foi do dono, e a lógica é a do §15.3 levada até o fim:** se a régua
> não distingue "verde" de "verde com buraco", então "já está `ok`" não é
> motivo para não reprocessar — é justamente a categoria suspeita.

### 17.1 O que são as 47

| grupo | n | o que é | ação |
|---|---:|---|---|
| **1** | **22** | `ok` coletadas com o parser VELHO (12/06/2024 e 03/02→12/03/2025) | reprocessadas |
| **2** | **18** | **nunca coletadas** (07/01→30/01/2025) | coletadas pela 1ª vez |
| 3 | 6 | já reprocessadas hoje com o parser novo (§15.4) | não repetidas — seria 1 h de banco carregado para `novas=0` |
| 4 | 1 | `4146-19`, `inexistente` | terminal por decisão da fonte (§4) |

### 17.2 Recuperação — e a régua fecha SOZINHA, edição por edição

Duas contagens independentes: **`pauta` contada no PDF** (sonda que só extrai
texto, não segmenta, não toca no banco) e **`movimentacoes_novas` do
`IngestionRun`** (o que o banco recebeu). Elas não se conversam.

| edição | pauta no PDF | novas no banco | Δ `itens_gravados` | extra |
|---|---:|---:|---:|---:|
| 4143-19 | 99 | 99 | 99 | **0** |
| 4144-19 | 407 | 407 | 407 | **0** |
| 4148-19 | 73 | 73 | 73 | **0** |
| 4149-19 | 134 | 134 | 134 | **0** |
| 4150-19 | 96 | 96 | 96 | **0** |
| 4151-19 | 369 | 369 | 369 | **0** |
| 4155-19 | 726 | 726 | 726 | **0** |
| 4156-19 | 374 | 374 | 374 | **0** |
| 4157-19 | 106 | 106 | 106 | **0** |
| 4158-19 | 571 | 571 | 571 | **0** |
| 4159-19 | 574 | 574 | 574 | **0** |
| 4160-19 | 333 | 333 | 333 | **0** |
| 4161-19 | 157 | 157 | 157 | **0** |

(Tabela = as 13 primeiras a fechar; o invariante se manteve nas 22.)

**FECHAMENTO das 22, em 02/09/2026 22:57 UTC:**

    pauta contada nos PDFs .................. 7.917
    Δ `itens_gravados` no banco ............. 7.917   ← igual, no dígito
    linhas extras (duplicação) ..............     0
    `movimentacoes_novas` ................... 7.415   (ver ressalva abaixo)

**Vinte e duas de vinte e duas, casado no dígito.** O que a fonte imprime, o
que o parser segmenta e o que o banco grava são o mesmo número — e as três
medidas vêm de lugares diferentes.

⚠️ **A ressalva dos 502, e ela é minha:** `movimentacoes_novas` fica 502 abaixo
porque a edição `4154-19` teve suas 502 linhas gravadas por um run que **eu
abortei** (o restart para ativar o guarda do §17.5), e a re-execução as contou
como `dup`. Mesmo efeito em `4132-19` e `4134-19` do grupo 2, 500 cada — um
lote de `BATCH_SIZE` exato em cada caso. **É contagem, não dado**, e a prova é
por `external_id`:

    4132-19 ... 12.128 linhas · 12.128 external_id distintos · 0 duplicadas
    4134-19 ... 14.175 linhas · 14.175 external_id distintos · 0 duplicadas

Por isso a régua que vale é o **Δ `itens_gravados`** (o que a unidade TEM),
não `movimentacoes_novas` (o que ESTA execução criou): a segunda depende de
quantas vezes a unidade foi interrompida.

### 17.3 Duplicação em escala: **zero**, e melhor que a amostra de 1

O §16.4 media numa edição só (925 novas contra 2 antigas órfãs) e eu havia
registrado a duplicação como risco a confirmar. Confirmado, e para baixo:
`Δ itens_gravados` **=** pauta contada no PDF em **22 de 22**, ou seja
**nenhuma linha extra além das entradas de pauta**. Não há neighbour reescrito,
não há linha velha convivendo com a nova. E o controle por `external_id`
distinto fecha em 0 duplicadas nas duas edições onde a contagem de `novas`
divergiu.

O motivo é o do §15.3: no caderno 19 as entradas de pauta ficavam **órfãs**
(descartadas por não terem âncora), não engolidas por um bloco vizinho. Bloco
vizinho intacto ⇒ `external_id` (hash do texto) intacto ⇒ nada duplica.

### 17.4 As 22 medidas × as 18 nunca medidas — e a estação do ano

Este era o teste que valia mais que as publicações. As 18 foram medidas com a
MESMA sonda das 22, antes de qualquer coleta:

    22 edições `ok` (fev-mar/2025) ....... 7.917 entradas · média 360/edição
    18 nunca coletadas (jan/2025) ........ 4.918 entradas · média 273/edição

A média mais baixa **não** enfraquece a tese: ela esconde dois regimes, e o
corte é o **recesso forense**.

| regime | edições | pauta | média | cadernos |
|---|---:|---:|---:|---|
| recesso/retomada (07→17/01) | 9 | **118** | 13 | 48 a 3.419 páginas · 367 a 2.561 CNJs |
| regime normal (20→30/01) | 9 | **4.800** | **533** | 2.683 a 7.371 páginas · 12,8 a **33,1 mil** CNJs |

Não há pauta de julgamento onde não há sessão. Em 20/01 o caderno salta de
1.442 para 12.894 CNJs e a pauta reaparece — **e no regime normal as 18
rendem MAIS por edição (533) que as 22 (360)**.

**E a linha dos 5% se sustentou na população inteira, com dois casos novos que
teriam REPROVADO o gate:**

| edição | pauta / CNJs | % | o gate diria |
|---|---:|---:|---|
| 4133-19 | 82 / 15.609 | 0,53% | passa |
| 4127-19 | 444 / 33.141 | 1,34% | passa |
| 4126-19 | 406 / 12.894 | 3,15% | passa |
| 4131-19 | 596 / 12.822 | 4,65% | passa (raspando) |
| 4130-19 | 887 / 18.761 | 4,73% | passa (raspando) |
| **4132-19** | 812 / 13.061 | **6,22%** | **REPROVA** |
| **4125-19** | 93 / 1.442 | **6,45%** | **REPROVA** |

Somando aos 6 que reprovaram de fato (7,3-7,4%) e às 22 que passaram
(0,60-4,54%): **35 edições, e a separação continua monotônica em torno da folga
de 5% do piso**. Nenhum caso fora de lugar em nenhum dos dois lados. A tese do
§15.3 está confirmada em campo — e o corolário também: **o gate teria pego
4125 e 4132 se elas tivessem sido coletadas antes**, o que reforça que o
problema nunca foi o gate estar quebrado, e sim ele medir a coisa errada.

### 17.5 Um achado de OPERAÇÃO que o lote produziu: a fila `default` não é nossa

Com 40 unidades na fila, a promoção a parte (§16) enfileirou **77 jobs na
frente de 4 crons** (`reabastecer_filas_enriquecimento` ×2,
`reabastecer_fila_datajud` ×2), com projeção de **~850** para o lote inteiro —
cerca de **3,8 h de cron faminto**. RQ é FIFO sem prioridade, e a `default`
também é do tick dos diários e dos reabastecimentos.

É a lição que criou o `WATERMARK_POR_FONTE` ("quem enche primeiro monopoliza a
FIFO"), agora do outro lado. `DIARIOS_FILA_PARTES_MAX` (200) freia a promoção
quando a fila está funda — e **freia alto**: cada bloqueio sai no log com o
número de processos adiados. Nada se perde: a movimentação está gravada e o
`manage.py backfill_partes_djen` alcança o processo depois.

    promoção de partes ADIADA para TJSP: fila `default` com 201 jobs
    (teto 200). 457 processos NÃO foram enfileirados; a movimentação está
    gravada e o `backfill_partes_djen` os alcança.

**Consequência a cobrar:** as partes das edições do caderno 19 coletadas depois
do teto **não foram promovidas**. São Agravante/Agravado com OAB, não o ente
devedor (esse é o caderno 11, e já está feito — §16.3).

### 17.6 O caderno 11 (DEPRE) — decisão: NÃO reconferir com o parser

A pergunta em aberto era se a relação da DEPRE também passa por baixo do gate.
**Não vou reconferir com o parser, e o motivo é que a medida já existe em
DOIS instrumentos independentes.** A sonda das 23 edições verdes do caderno 11
contou, separadamente, quatro marcadores do registro — e nas 23:

    'Nº de ordem cronológica'  → 0
    'Entidade devedora'        → 0

Dois marcadores diferentes, ambos zerados, nas mesmas 23 edições. Rodar o
parser mediria a MESMA âncora textual de novo — não é um terceiro instrumento,
é o mesmo com outra roupa. E o comportamento da DEPRE é tudo-ou-nada
(2.568 registros quando aparece, 17-58% dos CNJs, sempre reprovando), o oposto
da pauta. Se algum dia ela aparecer pequena, quem vai pegar é o **segundo eixo
do gate**, não uma varredura manual.

### 17.6b Resultado final das 47

| grupo | n | resultado |
|---|---:|---|
| 1 — `ok` do parser velho | 22 | **7.917** entradas de pauta recuperadas, **0** duplicadas |
| 2 — nunca coletadas | 18 | **181.029** linhas de acervo NOVO |
| 3 — já feitas hoje | 6 | (§15.4) |
| 4 — `inexistente` | 1 | terminal por decisão da fonte |
| **caderno 19 inteiro** | **47** | `itens_gravados` **356.466 → 545.412** = **+188.946 linhas** · **46 `ok`, 0 falha** |

⚠️ **Uma armadilha de sonda que eu mesmo armei e quase paguei:** montei um
faxineiro para remover os dois workers avulsos "quando a fila zerar". **Fila
zerada não é trabalho terminado** — job em voo vive no `StartedJobRegistry`, e
a fila marcou 0 com TRÊS edições ainda rodando. O faxineiro ia matar dois
workers no meio da coleta. Desarmei a tempo e a espera correta é
`queue.count + len(StartedJobRegistry)`. É a mesma família do §13.10: número
parado não distingue "acabou" de "está acontecendo fora do lugar onde eu olho".

### 17.7 O que ficou de fora

| pendência | estado |
|---|---|
| **o segundo eixo do gate** (inventário por marcador, não só fração) | **continua não feito, e continua sendo o conserto durável.** Este §17 limpou o passado; o segundo eixo é o que impede o futuro |
| promoção a parte das edições do 19 coletadas após o teto | adiada pelo guarda de fairness; recuperável com `backfill_partes_djen` |
| `r19_w1` / `r19_w2` | dois `rqworker diarios` avulsos criados na `.102` (`docker run`, fora do compose) para dobrar a vazão. **Removidos** ao fim do lote |

---

## 18. O SEGUNDO EIXO do gate — inventário do que a fonte imprime

> Este é o conserto durável que os §15-§17 ficaram devendo. Eles limparam o
> passado; **este eixo é o que impede o futuro.**

### 18.1 Por que o eixo antigo não podia pegar o caso que importa

O eixo que existia mede **proporção**: dos CNJs impressos no caderno, quantos
caíram dentro de algum bloco; reprova abaixo de 95%. Ele funciona — foi ele que
pegou as doze edições de 02/09. E é **estruturalmente cego** para a perda
pequena. Não é opinião:

- a relação da DEPRE varia de tamanho: **3.833** registros derrubaram a
  cobertura a 68,4%, **2.568** a 83,1%, e uma de ~760 fecharia **acima de 95%**;
- a pauta numerada passou calada em **22 de 22** edições verdes — **7.917**
  registros, todos entre **0,60% e 4,54%** dos CNJs; as seis que reprovaram
  estavam em 7,3-7,4%;
- **duas edições nunca coletadas teriam reprovado** (6,22% e 6,45%): o acaso de
  QUANDO coletamos decidia se veríamos.

**Gate de proporção não é gate de completude.** Ele reprova a perda grande e
absolve a pequena pelo MESMO mecanismo.

### 18.2 O que o eixo novo mede — duas pernas, e a segunda é obrigatória

**Perna A — inventário por marcador.** Cada fonte declara, em
`MARCADORES_DE_REGISTRO`, as linhas que **abrem um registro** e o `Bloco.formato`
que cada uma tem que virar. Conta-se a linha no **texto extraído** e exige-se
`blocos[formato] >= registros[marcador]`. Falta é ERRO **com os dois números**,
**independente de quanto aquilo representa em CNJ**. É `>=` e não `==` porque um
balde de formato pode receber mais de um marcador.

**Perna B — assinatura dos órfãos.** A perna A só conhece o que já foi
declarado; um formato novo não tem marcador, e ali ela herdaria a mesma
cegueira. Então os CNJs que ficaram **fora de bloco** são agrupados pela FORMA
da linha (runs de dígito viram `#`) e a forma que se repete acima de um piso é
ERRO que **nomeia o suspeito**. Não é heurística inventada: é exatamente o que
foi feito à mão em 02/09 para achar os dois formatos — 100% dos 6.170 órfãos de
`4155-11` caíram em duas formas; 96,5% dos 1.212 de `4153-19` numa só. **Um
formato é, por definição, repetitivo — e é a repetição que o denuncia sem
marcador.**

### 18.3 Independência do segmentador — a condição que faz isto valer

A perna A conta `Pagina.linhas` (texto extraído do PDF) e compara com
`Bloco.formato` (saída do segmentador): **dois caminhos de código sobre a mesma
entrada**. `Inventario.ver_bloco` recebe **só o nome do formato, nunca o
texto** — e há teste travando a assinatura da função, porque no dia em que ela
receber texto alguém vai contar marcador ali dentro e o eixo vira circular. É o
erro que a régua do nicho 12078 quase cometeu ao usar `codigo_classe` como
prova de si mesmo.

### 18.4 A prova, na edição REAL que passou calada

`4148-19` (19/02/2025) — a de MENOR fração de todas: **73 registros em 12.091
CNJs = 0,60%**. Rodando o segmentador no estado em que ela de fato fechou `ok`
em produção:

| | eixo 1 (proporção) | eixo 2 (inventário) |
|---|---|---|
| segmentador **cego** (como era) | **99,0% → PASSA** | **ACUSA por dois caminhos:** perna A `73 impressos × 0 blocos do formato pauta`; perna B `68 CNJs órfãos com a MESMA forma de linha` |
| segmentador **atual** | 99,6% → passa | **nada a acusar** (sem falso positivo) |

E **em produção**, com o eixo já deployado (`worker_diarios` reiniciado em
`2026-09-02T23:15:58Z`), a mesma edição recoletada:

    tjsp-dje/4148-19: 11.293 blocos → 11.220 itens; cobertura de CNJ 12.044/12.091 = 99,6%
    tjsp-dje/4148-19: inventário da fonte {'pauta numerada': 73}
                      → blocos {'segunda_instancia': 10787, 'ato': 18, 'pauta': 73, 'numero_primeiro': 415}

**73 impressos, 73 blocos** — o número de fora bateu com o de dentro, ao vivo e
igual ao offline. E numa edição de recesso (`4117-19`, sem pauta e sem DEPRE) o
log diz `inventário da fonte {}` — nada impresso, nada a cobrar.

### 18.5 Três decisões que precisam estar escritas

1. **`FORMATO_PAUTA` ganhou constante própria por causa do GATE, não do
   parsing.** Com a pauta caindo no balde `numero_primeiro` junto com 415
   blocos da forma sem ordinal, a perna A **não disparava** (415 >= 73) e só a
   perna B pegava. **A perna A só morde quando o balde é EXCLUSIVO do
   marcador** — quem for declarar marcador novo precisa saber disso.
2. **A ordem é deliberada: o eixo de proporção fala primeiro.** Perda grande
   tem que continuar produzindo a MESMA mensagem que a `OPS.md` já ensina a
   ler — foi ela que separou "o INSERT quebrou" de "o parser não conhece o
   formato". O eixo novo entra onde o antigo se cala, com prefixo próprio
   (`inventário divergente`) e dizendo na mensagem que **a cobertura estava
   ACIMA do piso**.
3. **`EdicaoDiario.itens_esperados` NÃO foi usado, e o motivo importa.** Ele
   parece a casa natural (o DEJT declara "1 até 20 de 16.717"), mas o runner o
   compara contra o TOTAL de itens com piso percentual
   (`achados < alvo * 0,95`). Como o total de blocos é ordens de grandeza maior
   que o de registros marcados, a comparação passaria sempre — seria **um
   segundo número percentual ao lado do primeiro**, com a mesma cegueira. O
   inventário mora no gate da fonte, por marcador, sem percentual.

### 18.6 O que este eixo NÃO cobre — escrito antes de alguém descobrir

**Formato desconhecido, pequeno, E cujos CNJs já caem dentro de outro bloco.**
Se as linhas de um formato novo forem **engolidas** por um bloco vizinho em vez
de ficarem órfãs, os CNJs contam como cobertos, não há órfão para agrupar, e a
perna B não vê nada; a perna A também não, porque não há marcador declarado.
Esse resíduo continua descoberto pelos **dois** eixos.

Duas observações honestas sobre ele: (a) nas 35 edições medidas em 02/09 o
padrão real foi o oposto — as entradas ficavam ÓRFÃS, não engolidas (foi por
isso que o reprocessamento não duplicou nada, §17.3); (b) a única defesa
conhecida contra esse caso é alguém abrir um caderno de vez em quando. Não há
régua automática para ele, e dizer que há seria o tipo de conforto falso que
este documento inteiro existe para evitar.

**Em 02/09/2026 o eixo só estava armado para o `tjsp-dje`.** Em 03/09 o `dejt`
também passou a declarar marcador (§19.5). `stf`, `doe-sp` e `qd-municipal`
continuam sem: nelas o eixo **se abstém** e o log diz `inventário por marcador
NÃO MEDIDO`. Abstenção não é aprovação — e essa linha existe justamente para
que "não medido" e "medido e ok" não tenham a mesma cara no log.

| onde | estado |
|---|---|
| `diarios/inventario.py` | mecanismo (as duas pernas, assinatura, teto declarado) |
| `ColetorDiario.MARCADORES_DE_REGISTRO` | contrato; vazio = abstém |
| `diarios/fontes/tjsp_dje/coletor.py::MARCADORES_TJSP` | 3 marcadores (2 da DEPRE, 1 da pauta) |
| `diarios/fontes/dejt/coletor.py::MARCADORES_DEJT` | 2 marcadores (matéria e Distribuição) — §19.5 |
| `tests/test_gate_inventario.py` | 11 testes, incluindo o que trava a independência |
| `tests/test_gate_inventario_dejt.py` | 12 testes do eixo armado no DEJT |

---

## 19. O denominador REAL da fonte — e a ausência que virava perda (03/09/2026)

> **A ordem era volume. O primeiro achado foi que a régua do volume estava
> errada por 30%**: o denominador do DJE/TJSP não é `dias × 8`. Seis dos oito
> cadernos existem desde sempre; os cadernos **19 e 20 estreiam em
> 2023-11-27**. Contar as unidades que a fonte NÃO tem inflava o denominador em
> **6.748** e fazia a cobertura parecer menor do que é.
>
> E o segundo achado é pior que o primeiro: as **5** unidades marcadas
> `inexistente` — status TERMINAL, que nunca mais é retentado — **existem**.
> Conferido contra a fonte viva com GET real: as 5 devolveram `%PDF`.

### 19.1 O denominador, medido: **22.620**, não 29.368

O §14.6 registrou `3.671 dias × 8 cadernos = 29.368` e teve a honestidade de
marcar as duas ressalvas: "quais cadernos existem em cada data só o download
responde" e "o denominador real está entre ~26 mil e 29.368". As duas estavam
certas. O que faltava era o instrumento.

**O instrumento é `HEAD`, e ele custa ~400 bytes.** O `downloadCaderno.do`
responde ao HEAD e a resposta já separa os dois casos, sem baixar um byte de
PDF:

| resposta ao HEAD | significa |
|---|---|
| `Content-Type: application/octet-stream` + `Content-disposition: attachment; filename="caderno3-…pdf"` | o caderno EXISTE |
| `Content-Type: text/html` + `Content-Length: 398` | não existe naquela data |

Com isso, a estreia de cada caderno sai por **bissecção** sobre as 4.162
edições do catálogo — **124 requisições, zero PDF**, cada uma confirmada nas 3
edições seguintes:

| caderno | `cdCaderno` | última edição SEM | primeira edição COM | edições com |
|---|---:|---|---|---:|
| 1 Administrativo | 10 | 2007-11-05 | **2007-11-06** | 4.138 |
| 2 · 2ª Inst. Entrada e Distribuição I | 11 | 2007-11-05 | **2007-11-06** | 4.138 |
| 3 · 1ª Inst. Capital I | 12 | 2007-11-05 | **2007-11-06** | 4.138 |
| 4 · 1ª Inst. Interior II | 13 | 2007-11-05 | **2007-11-06** | 4.138 |
| 4 · 1ª Inst. Interior I | 18 | 2007-12-19 | **2007-12-20** | 4.109 |
| 4 · 1ª Inst. Interior III | 15 | 2008-12-01 | **2008-12-02** | 3.879 |
| 2 · 2ª Inst. Processamento II | 19 | 2023-11-24 | **2023-11-27** | 382 |
| 3 · 1ª Inst. Capital II | 20 | 2023-11-24 | **2023-11-27** | 382 |

**Os cadernos 19 e 20 têm 20 meses de vida, não 16 anos.** É o caderno 19 —
o da pauta numerada que o §15 e o §17 inteiros perseguiram.

Daí os denominadores, todos por soma direta (edição a edição, caderno a
caderno), nunca por multiplicação:

| janela | edições | **unidades REAIS** | `dias × 8` diria | erro do atalho |
|---|---:|---:|---:|---:|
| jazida inteira 2007-10-01 → 2025-07-22 | 4.162 | **25.304** | 33.296 | +31,6% |
| janela do coletor 2007-10-01 → 2025-03-13 | 4.077 | **24.624** | 32.616 | +32,5% |
| **faixa catalogada 2009-06-15 → 2025-03-13** | 3.671 | **22.620** | 29.368 | **+29,8%** |
| recorte 2011+ (o do §13.10) | 3.299 | **20.388** | 26.392 | +29,4% |

**Dois instrumentos independentes, e é isso que autoriza publicar o número.**
Além da bissecção, uma sonda percorre a população inteira das 29.368
combinações em **ordem EMBARALHADA com semente fixa** (`SONDA_RPS=2`, HEAD
apenas) — de propósito: uma execução parcial é uma **amostra aleatória** da
população, não um prefixo enviesado por ano. Com 2.962 unidades sondadas
(10,1%):

    contraexemplos ao modelo das estreias .... 0
    denominador PELA AMOSTRA ................. 23.024   IC95 [22.584 ; 23.464]
    denominador PELA BISSECÇÃO ............... 22.620   ← dentro do intervalo

Um contraexemplo seria um caderno 19 existindo antes de 2023-11-27, ou um dos
seis "de sempre" faltando.

**E a sonda achou dois, o que é melhor notícia do que "zero".** Com 4.086
unidades sondadas (13,9%), **nenhuma** unidade existe antes da estreia do seu
caderno (0 de 891) — a bissecção está certa. Mas **2 de 3.195** que o modelo
diz existirem NÃO existem: caderno 11 em 19/01/2024 e caderno 12 em 24/03/2020.
As duas foram reconferidas com 6 HEADs cada e com **GET real**: `6/6 "não"` e o
corpo é o HTML de erro. **São ausências ESPORÁDICAS verdadeiras** — caderno que
falta num dia isolado, sem padrão de era.

    ausência esporádica ..... 2 em 3.195 = 0,063%   (IC95: ± 0,09 pp)
    projetada sobre a faixa .. ~14 unidades de 22.620
    denominador corrigido .... ~22.606

Ou seja: o modelo das estreias erra por **0,06%**, e o número que vale continua
sendo **22.620** (com a ressalva de que ele é teto por uma dezena de unidades).

**Isto é, de quebra, o controle do §19.2:** a mesma sonda que declarou "existe"
para as 5 unidades falsamente `inexistente` declarou "não existe" para estas 2,
com o GET concordando nas 7. O instrumento sabe dizer as duas coisas — e as
ausências esporádicas provam que `inexistente` é um status legítimo, que
precisa existir. O que ele não podia é ser dado sem prova.

**Cobertura real da terceira porta, contra o denominador da FONTE:**

| | antes (15:46 UTC) | depois (ver §19.4) |
|---|---:|---:|
| unidades `ok` | 250 | — |
| denominador da faixa | 22.620 | 22.620 |
| **cobertura** | **1,105%** | — |

Contra o denominador inflado de 29.368 esse mesmo 250 dava 0,85%. A ordem de
grandeza não muda — continua sendo **cerca de um por cento** — mas a régua
agora é da fonte, não de uma multiplicação.

**O que este denominador NÃO cobre**, escrito antes que alguém descubra: o
caderno 5 (Editais e Leilões, `cdCaderno=14`) continua fora dos dois lados, e
`inexistente` esporádico (caderno que falta num dia isolado) é resíduo que só a
sonda completa mede — ela estava em 10,1% quando esta seção foi escrita.

### 19.2 `inexistente` é TERMINAL — e as 5 que estavam lá EXISTEM

O §4 é explícito: `inexistente` significa "a fonte diz que não existe unidade
nesse dia" e **nunca** é retentado. Um status assim é uma afirmação forte, e
por isso ele foi conferido contra a fonte viva. As cinco unidades que estavam
com esse carimbo:

| unidade | data | caderno | HEAD | **GET real** |
|---|---|---:|---|---|
| `4130-15` | 24/01/2025 | 15 | existe | **`%PDF-1.5`** |
| `4130-18` | 24/01/2025 | 18 | existe | **`%PDF-1.6`** |
| `4138-12` | 05/02/2025 | 12 | existe | **`%PDF-1.6`** |
| `4141-11` | 10/02/2025 | 11 | existe | **`%PDF-1.5`** |
| `4146-19` | 17/02/2025 | 19 | existe | **`%PDF-1.6`** |

Controle negativo na mesma passada: `15/06/2009` caderno 20 (que a bissecção
diz não existir) devolveu `\n\n<html>\n\t<head>`. A sonda sabe dizer "não".

**Cinco de cinco.** O e-SAJ tinha servido, uma vez, a página de 851 bytes de
`Erro ao acessar o caderno selecionado` — HTTP 200, `text/html` — para caderno
que ele tem. Não é reproduzível: 12 sondas seguidas na mesma unidade deram 12
"existe", distribuídas pelos três nós do cluster (`cdje1`/`cdje2`/`cdje3`).
Foi transitório. E uma observação transitória fechava o watermark **para
sempre**, com `IngestionRun.status='success'` e o log limpo — os três da tabela
do `CLAUDE.md` de uma vez, num status cuja definição é "nunca mais tentar".

**Tamanho, para não virar anedota:** 5 em ~260 unidades fechadas = **1,9%**. No
lote de 3.813 pendentes que esta sessão abriu, seriam da ordem de **70
cadernos** — ~1,8 milhão de publicações — perdidos sem retentativa e sem
alarme. É a mesma família da lição do §12 ("coletado" ≠ "buscável") e do §16
("extraído" ≠ "parte"): aqui, **"a fonte disse que não tem" ≠ "a fonte não
tem"**.

**A cura (commits `9247de2` + `28eeeee`):** ausência continua não sendo falha,
mas passa a exigir **confirmação**. Uma observação deixa a unidade `pendente`
com o motivo escrito; `CONFIRMACOES_DE_AUSENCIA = 3` observações a fecham. O
custo são 2 requisições de 851 bytes a mais por unidade que de fato não existe
— contra um download de caderno que chega a 62 MB. **A assimetria é o
argumento**, a mesma da guarda de disco do §13.3.

⚠️ **E o conserto estava errado na primeira versão — o dado é que corrigiu.** A
versão 1 contava `EdicaoDiario.tentativas`. Ao reabrir as cinco unidades
apareceu o número que derruba isso: elas estavam com **`tentativas` 4 e 5**,
queimadas em FALHAS de outra natureza (a `NotNullViolation` do §14), não em
ausências. Com o contador errado, uma ÚNICA observação fecharia o watermark de
qualquer unidade que já tivesse tropeçado antes — ou seja, **o conserto não
consertaria justamente os cinco casos que o motivaram**. A versão 2 conta
ausência **SEGUIDA** (marca em `ultimo_erro`, que todo outro desfecho
reescreve) e **não consome tentativa**: queimar `MAX_TENTATIVAS` com ausência
deixaria a unidade parada em `pendente` com `tentativas=5`, invisível para o
tick — dívida sem status, que é pior que o status errado.

Três testes em `tests/test_diarios_base.py`, com **controle negativo rodado**:
trocando o contador de volta por `tentativas`, os três reprovam.

**As 5 foram reabertas** (`status='pendente'`, `tentativas=0`, com o motivo no
`ultimo_erro`) e voltaram para a fila.

### 19.3 O volume aberto — e por que a fatia é esta

    catálogo 2023-01-01 → 2024-12-31 ...... 3.712 unidades vistas, 3.704 NOVAS
    2ª passada (idempotência) .............     0 novas
    pendentes: 119 → 3.813

Por que 2023-2024 e não a jazida: o §13.2 barra o backfill inteiro pelo disco
do ES (~772 GB projetados). Esta fatia, drenada por completo, são
3.286 unidades × ~26 MB ≈ **85 GB de índice** — o nó de ES sairia de **70,5%**
para ~**73,4%**, contra a guarda automática de 85%. Cabe com folga, e é o
pedaço mais recente da jazida, que é a ordem em que o `tick` já trabalha.

**Nota de leitura obrigatória para quem for conferir:** das 3.712 unidades
catalogadas nesta fatia, **426 não existem na fonte** — são os cadernos 19 e 20
de 2023-01-01 a 2023-11-24 (§19.1). Elas vão fechar `inexistente` **depois de 3
observações**, e isso é o comportamento CORRETO. `por_status.inexistente`
subindo para ~426 nesta fatia **não** é a fonte tendo mudado de layout (o
sintoma que o `OPS.md` ensina a temer) — é a estreia dos cadernos, medida por
bissecção e confirmada pela sonda. Fora desta fatia, `inexistente` crescendo
continua sendo alarme.

**Os tetos foram SUBIDOS, nunca tirados** (a regra do §13.10):

| botão | de | para | onde |
|---|---:|---:|---|
| `DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE` | 8 | 120 → **1.000** | `.env` da `.102` (efetivo) e da `.103` |
| réplicas do `worker_diarios` | 2 | **4** | `--scale`, **não persistido** no compose |
| `DIARIOS_FILA_ES_MAX` | 5.000 | **5.000** | não mexido — é ele que barra se eu exagerar |
| `DIARIOS_ES_DISCO_MAX_PCT` | 85 | **85** | não mexido |

1.000 unidades/24 h é teto de verdade: a ~46 unidades/h medidas com 4 réplicas,
24 h dão ~1.100 — o orçamento fica **junto** da capacidade, não uma ordem de
grandeza acima. Em disco, 1.000 unidades/dia são ~26 GB de índice = **+0,9
ponto percentual por dia**, com 14,5 pontos até a guarda.

⚠️ **`docker compose --scale worker_diarios=4` RECRIA as réplicas que já
existiam** — e mata a coleta em voo delas. Foi por isso que o §17.5 subiu
workers avulsos com `docker run` em vez de `--scale`. Custo desta sessão: 3
cadernos re-baixados e **10 `IngestionRun` órfãos**, fechados à mão com a causa
real (`watchdog_ingestao` não filtra por fonte e os chamaria de "worker
crashou"). Quem for repetir: ou usa avulsos, ou aceita o custo e fecha os runs.

### 19.4 Carga — medida antes, durante e depois

Baseline às 15:35-15:46 UTC, com a Fase 3, o `backfill_partes_djen`, o
`update_by_query` do #106 e o seguimento de incidentes do e-SAJ todos rodando.

| régua | teto | ANTES (15:46) | COM a coleta (16:04-16:12) |
|---|---|---:|---:|
| disco do nó de ES | **85%** | 70,35% | 70,50% |
| `write.rejected` do ES | **>0** | 0 | **0** |
| fila `es_index` | **5.000** | 0 | 1.718 (34%) |
| `load1` da `.102` | **40** | 4,04 | 8-13 (pico 25,9 no `--scale`) |
| `MemAvailable` da `.102` | **6.000 MiB** | 17.932 | 15.942 |
| **vazão 24 h da Fase 3** | não pode cair | **2.177** dias | **2.189** dias |
| pendentes da Fase 3 | — | 3.648 | 3.607 |
| `agg_estado('AC')` a frio | 30 s (cliente) | **26,0 s** (e 1 timeout antes) | **26,3 s** |
| `agg_estado('SP')` a frio | 30 s (cliente) | **TIMEOUT 2×** (>30 s) | **14,4 s** |

**A tela de estado já estourava o teto ANTES de eu abrir o volume** — `SP` deu
`ConnectionTimeout` em 30,03 s e 30,24 s às 15:38, com a terceira porta no
orçamento de 8/24 h e sem nada meu rodando. Quem manda nessa régua hoje é o
`update_by_query` do #106, que estava em **9 tarefas com 11.516 s de vida** no
nó de ES (load 18, RAM 97%). Durante a coleta o mesmo `SP` respondeu em
**14,4 s**. Ou seja: **não piorou** — mas seria desonesto ler isso como
"a coleta é de graça"; o certo é que o sinal está dominado por outra coisa e
esta régua não separa as duas causas hoje. Fica registrado como não separado.

Nenhum dos 10 critérios do §13.7 foi acionado.

### 19.5 O caderno 1 não é lacuna esporádica: é 5 de 5

O §10 já registrava que "o caderno 1 (Administrativo) fecha `vazia` mesmo tendo
CNJ impresso" e chamava isso de "lacuna pequena e SILENCIOSA". Com o volume
aberto dá para medir a frequência, e ela não é ocasional. Todas as unidades que
fecharam numa hora de coleta, agrupadas por caderno:

| caderno | unidades | cobertura mín / mediana / máx | CNJs no caderno (mediana) |
|---:|---:|---:|---:|
| **10** (1 · Administrativo) | 5 | **15,8% / 57,7% / 76,9%** | **50** |
| 11 (2 · 2ª Inst. Entrada e Distribuição I) | 4 | 100,0 / 100,0 / 100,0 | 17.417 |
| 12 (3 · 1ª Inst. Capital I) | 3 | 99,8 / 99,8 / 99,9 | 32.813 |
| 13 (4 · 1ª Inst. Interior II) | 4 | 99,7 / 99,9 / 100,0 | 52.794 |
| 15 (4 · 1ª Inst. Interior III) | 2 | 99,9 / 99,9 / 99,9 | 54.432 |
| 18 (4 · 1ª Inst. Interior I) | 2 | 99,9 / 99,9 / 99,9 | 61.101 |
| 20 (3 · 1ª Inst. Capital II) | 4 | 99,9 / 100,0 / 100,0 | 28.589 |

**Cinco unidades abaixo do piso de 95%, e as cinco são o caderno 10.** Nenhuma
reprovou, e o motivo é mecânico: `MINIMO_PARA_AFERIR_COBERTURA=200` e o caderno
Administrativo tem **13 a 78** CNJs impressos. Abaixo de 200 a edição não é
aferida contra nada — o gate se abstém, corretamente, mas a abstenção sai com a
mesma cara de aprovação no `diarios_status`.

Ordem de grandeza da perda, para dimensionar a dívida sem exagerá-la: ~50 CNJs
por unidade a ~42% fora de bloco, em 3.671 unidades da faixa ⇒ da ordem de
**77 mil publicações**. Contra os ~90 milhões que a faixa inteira promete, é
0,1% — pequeno, e **100% sistemático e 100% silencioso**, que é a combinação
que este documento inteiro existe para não deixar passar. Não foi consertado
aqui: é parser de outro layout, com gate próprio, como foram a DEPRE e a pauta.

### 19.6 A fila `default` oscila NO teto — e é assim que ela foi desenhada

Achado operacional que vale para quem for subir mais réplicas. Com o
`worker_diarios` em 4, a fila `default` (que é do `tick` dos diários, dos
reabastecimentos, do watchdog **e do tique da Fase 3**) sai de **0** e passa a
oscilar em torno de **200** — que é exatamente `DIARIOS_FILA_PARTES_MAX`. Não é
coincidência: o guarda de fairness do §17.5 adia a promoção a parte sempre que a
`default` passa de 200, então ele **regula** a fila nesse número. Medido: 511
adiamentos em 10 minutos, cada um com o número no log.

O efeito colateral é latência de cron, e ela é **em rajada, não constante**:
`tick_recuperacao_fase3` ficou **13 minutos sem rodar** e depois rodou 4 vezes
em 4 minutos. A causa é a cauda de duração dos jobs da `default` — mediana
**12 s**, máximo medido **188 s**, com 2 réplicas de `worker_default`.

**Não subi `worker_default`, de propósito.** Os dois critérios que o dono nomeou
seguem intactos (vazão da Fase 3 e a tela de estado, §19.4) e a fila se
autocorrige; acrescentar concorrência de escrita a um Postgres já
disk-I/O-bound para consertar uma oscilação que se resolve sozinha seria trocar
um não-problema medido por um risco não medido. Fica escrito como o **primeiro
lugar onde olhar** se alguém subir o `worker_diarios` acima de 4:

    o sintoma é `tick_todas` parado na fila `default` por 20-30 min
    a consequência é a fila `diarios` esvaziando sem refill — e a coleta parando
    a sonda é: docker logs do worker_default | grep 'tick tjsp-dje'
              (um container por vez — §13.10)

### 19.7 O que ficou de fora

| pendência | estado |
|---|---|
| a sonda do denominador completa (29.368 HEADs, ~10 h a 2 rps) | **parcial: 13,9%** quando esta seção fechou. O número publicado é da bissecção; a sonda é o controle, e já mediu a ausência esporádica em 0,063% |
| era 2007-10-01 → 2009-06-14 | fora da faixa catalogada. As estreias já estão medidas (§19.1), mas nenhuma unidade foi catalogada nem coletada |
| o **caderno 1 (Administrativo)** | ver §19.5: não é esporádico, é **5 de 5** — e não foi consertado |
| catalogar 2011 → 2022 | **não feito de propósito.** São ~16.500 unidades reais; o §13.2 manda expandir o disco do ES antes de janela larga |
| `DIARIOS_FILA_PARTES_MAX` | não mexido (200). Com 4 réplicas a promoção a parte adia mais — o `backfill_partes_djen` alcança depois (§17.5) |
| as 22 edições do §16.4 / caderno 5 | inalterados |

---

## 20. As duas portas que estavam prontas e desligadas — por quê, medido (03/09/2026)

> **Veredito: nenhuma das duas pode ser ligada hoje, e as razões são
> diferentes — nem uma nem outra é "faltou apertar o botão".**
>
> | fonte | por que estava desligada | por que continua |
> |---|---|---|
> | `dejt` | recorte `DIARIOS_FONTES_AGENDADAS=tjsp-dje` (uma fonte por vez) **+** a sonda de 24/08 achou HTTP 503 | **a fonte saiu do ar em 18/08/2026 e quem declara isso é o CSJT.** Não é o nosso lado |
> | `stf` | mesmo recorte **+** as duas decisões do §5 que o documento marca como "olho humano ANTES" | as duas continuam abertas, e apareceu uma **terceira**, que é defeito e não decisão (§20.7) |
>
> Nenhum kill switch estava ligado: `diarios_pausar --listar` devolve
> `{"pausadas": [], "tudo_pausado": false}` com as 5 fontes registradas. E
> nenhuma das duas tem UMA linha em `EdicaoDiario` — `unidades: 0`,
> `ultima_data_catalogada: null`. Nunca rodaram em produção.
>
> **O que saiu daqui:** o DEJT era a fonte mais desprotegida das cinco — **três
> réguas, três silêncios** (§20.4) — e agora tem os dois eixos do gate armados,
> medidos contra **7 cadernos reais** (§20.5). Ligar deixou de ser um projeto e
> virou uma decisão.

### 20.1 O DEJT está fora do ar desde 18/08/2026 — e é a fonte que diz isso

A sonda de 24/08 (§13.6) registrou HTTP **503**. Hoje o sintoma é outro, e o
novo sintoma é melhor, porque tem texto:

```
GET https://dejt.jt.jus.br/dejt/f/n/diariocon?pesquisacaderno=J&evento=y
→ HTTP 302  location: https://www.csjt.jus.br/web/csjt/diario-eletronico-da-jt-aviso
GET https://dejt.jt.jus.br/                → HTTP 502
```

4 de 4 sondas do IP local e 1 de 1 do egresso da `.102` — não é geo nem
bloqueio de IP. A página de destino é um aviso institucional do CSJT:

> *"O Diário Eletrônico da Justiça do Trabalho (DEJT) encontra-se indisponível
> **desde 18 de agosto de 2026**. Enquanto o sistema não for restabelecido, as
> matérias disponibilizadas até 17 de agosto de 2026 poderão ser consultadas
> clicando [aqui](https://dejt-caderno.jt.jus.br/?data=2026-08-17)."*

O coletor se comportou como devia, e isso vale registrar: rodado ao vivo pelo
`worker_diarios`, ele **recusou** a página de aviso em vez de catalogá-la.

```
diarios_coletar dejt --de 2024-07-10 --ate 2024-07-10 --dry-run
→ RespostaInvalida: GET diariocon: sem javax.faces.ViewState (166739 bytes)
                    — a tela não é o formulário esperado
```

166.739 bytes de HTML com HTTP 200 no fim da cadeia, e zero unidade catalogada.
É a defesa de "200 que não é dado" funcionando num caso que ninguém escreveu o
teste para: **a fonte inteira desapareceu e o coletor não inventou acervo.**

### 20.2 Existe um espelho de contingência — e ele é melhor de ler e pior de catalogar

`https://dejt-caderno.jt.jus.br/?data=AAAA-MM-DD`. Medido em 03/09/2026:

| | |
|---|---|
| Cobertura | `min="2008-06-09" max="2026-08-17"` no próprio `<input type="date">` — **a mesma janela** que o `janela_inicio` do coletor |
| Transporte | GET simples. **Sem JSF, sem ViewState, sem conversa Seam, sem `j_id`** — some a classe inteira de fragilidade que o `sessao_jsf.py` existe para domar |
| Download | `/download/<id>` numérico → `Content-Type: application/pdf`, `%PDF-1.4` no byte 0 |
| Ausência declarada | dia sem caderno responde **"Não há matérias disponibilizadas no DEJT"** por tribunal, em vez de tabela vazia. É LACUNA × AUSÊNCIA (§4) dita **pela fonte** |
| É o mesmo artefato? | **sim, no byte.** TRT3 de 10/07/2024 = **62.764.417 B**, exatamente os "62,7 MB" que o §1 mediu pelo host original em 16/08/2026 |

**O que ele NÃO tem, e pesa:**

1. **Não tem a pesquisa avançada** — ou seja, não tem o gabarito
   `"1 até 20 de 16.717"`. `ColetorDEJT.esperado()` devolve `None` por desenho
   em qualquer erro, então hoje ele devolve `None` **sempre**. Ver §20.4.
2. **Não tem catálogo em bloco.** O JSF entregava as **95.679** edições de 18
   anos em UMA resposta (50,8 MB, 37 s). O espelho é **um GET por dia**:
   ~6.640 requisições para o mesmo inventário.
3. **Tem `robots.txt` com `User-agent: * / Disallow: /`**, mais
   `X-Robots-Tag: noindex, nofollow, noarchive` em cada resposta. O §6 deste
   documento afirma que "nenhuma dessas fontes tem rate limit, WAF ou
   `robots.txt`" — **para este host, deixou de ser verdade.**

O item 3 **não é decisão de engenharia** e por isso nenhum transporte para o
espelho foi escrito. O que a casa faz quando o servidor não se defende é impor
o próprio teto; o que ela faz quando o servidor **pede para não ser varrido**,
num site de contingência publicado para consulta humana durante uma queda, é
outra conversa — e é do dono do produto. O que foi feito aqui foram **9
requisições de diagnóstico** (7 cadernos + 2 listagens), o suficiente para
medir e nem perto de uma varredura.

### 20.3 O `stf` responde — o que trava é semântica, e agora são três

Medido em 03/09/2026: `GET /decisoes-publicacoes/api/public/ultimo-dje` →
**HTTP 200 em 0,13 s**, devolvendo `"2026-09-03"` (o DJe de hoje). Sem WAF na
rota da API, TLS fechando com o `ca_stf.pem` embarcado. E o catálogo roda:

```
diarios_coletar stf --de 2026-09-01 --ate 2026-09-02 --dry-run
→ 2 unidades, `na_janela: true`
```

A porta está viva. O que não está resolvido:

1. **§5.1 — o CNJ de origem.** 28% dos CNJs do STF são de outro tribunal ⇒
   `Process` duplicado. Aberta.
2. **§5.3 — sigilo.** 11,2% das publicações vêm marcadas `Segredo de Justiça`
   ou `Sigiloso` **pela própria fonte**, com corpo completo. Aberta.
3. **§5.4 — o XHTML vaza para o CLIENTE, e isto é DEFEITO, não decisão.**
   Reconferido hoje: `dashboard/views.py:3015` exporta
   `(p.ultima_mov_texto or '')[:500]` para o CSV/XLSX, e `grep -r html_strip
   search/` não devolve **nada**. Como 24% do documento do STF é `<head>`+CSS,
   os primeiros 500 caracteres de uma publicação do STF são **folha de estilo**
   na planilha do cliente. Isso precisa ser consertado ANTES de a primeira
   linha do STF entrar em `Movimentacao` — e mora em arquivos que este trabalho
   não toca.

### 20.4 O DEJT era a fonte mais desprotegida das cinco — três réguas, três silêncios

Este é o achado que mais surpreendeu, e ele não depende de a fonte estar fora do
ar. Antes de 03/09/2026, uma coleta do DEJT passava por:

| régua | estado antes |
|---|---|
| eixo 1 — proporção (CNJ impresso × CNJ dentro de bloco) | **não existia.** `ColetorDEJT.coletar` segmentava, logava `N páginas → M matérias` e retornava. Nada comparava os dois lados — ao contrário do `tjsp-dje`, que tem `_aferir_cobertura` desde o começo |
| eixo 2 — inventário por marcador (§18) | **abstinha.** Sem `MARCADORES_DE_REGISTRO`, `Inventario.mede` é falso e o log diz `NÃO MEDIDO` |
| gabarito da fonte (`esperado()`) | devolve `None` em qualquer erro **por desenho** (reprovar coleta boa por falha do gabarito seria pior) — e com o host caído, `None` sempre |

Três réguas, três silêncios, um sintoma só de fora: **run verde**. É a mesma
assinatura da tabela do `CLAUDE.md`, e desta vez ela estava armada numa fonte
que cobre a Justiça do Trabalho inteira.

### 20.5 O que ficou armado — e os 7 cadernos que sustentam cada número

Territórios: só `diarios/fontes/dejt/`. `base.py`, `jobs.py`, `models.py` e
`inventario.py` **não foram tocados** — o mecanismo do §18 serviu como está,
que era a hipótese do contrato.

**Perna A — dois marcadores, dois baldes exclusivos.**
`segmentador.FORMATO_PROCESSO` e `FORMATO_DISTRIBUICAO` nasceram por causa do
GATE, não do parsing: com as duas âncoras no mesmo balde, as ~900 matérias
`Processo Nº` cobririam sozinhas a conta da Distribuição inteira e a perna A
ficaria muda (§18.5, decisão 1).

Medido em **7 cadernos**, pelos **três** caminhos que têm que concordar — regex
no texto colado, contagem linha a linha, e blocos produzidos:

| caderno | páginas | `Processo Nº` impressos × blocos | `Distribuição` impressas × blocos | descartes |
|---|---|---|---|---|
| TRT3 10/07/2024 | 13.853 | 16.954 × 16.940 | 1.828 × 1.828 | 14 pré-CNJ |
| TRT22 10/07/2024 | 612 | 890 × 890 | 109 × 109 | 0 |
| TRT16 10/07/2024 | 641 | 1.102 × 1.102 | 154 × 154 | 0 |
| TRT16 10/03/2022 | 805 | 1.395 × 1.392 | 0 × 0 | 3 pré-CNJ |
| TRT16 11/03/2020 | 1.207 | 2.190 × 2.187 | 0 × 0 | 3 pré-CNJ |
| TRT22 15/03/2018 | 2.210 | 750 × 744 | 0 × 0 | 6 pré-CNJ |
| TRT16 15/03/2018 | 1.400 | 1.237 × 1.181 | 0 × 0 | **56 pré-CNJ** |

> **O 7º caderno pagou a própria viagem — ele reprovou o marcador, não a
> edição.** Com os 6 primeiros o marcador de Distribuição parecia perfeito:
> 109×109 e 154×154, batendo no dígito. No TRT3 ele acusou **2.870 impressas ×
> 1.828 blocos** e teria reprovado a **edição de referência do DEJT** por uma
> perda de 1.042 registros. Fui olhar: das 2.870 linhas com a forma
> `SIGLA CNJ`, só 1.828 estão na seção que o outline declara `Distribuição`; as
> outras 1.042 estão dentro de `Notificação`, e uma delas
> (`AIRR 0004300-04.2002.5.03.0009`) aparece **4 vezes seguidas** — é citação
> no corpo de um ato, não registro.
>
> Era **falso positivo do meu marcador**, exatamente o que o comentário do
> `MARCADORES_TJSP` avisa: *"marcador que dispara fora do formato inflaria o
> `impresso` e produziria alarme falso — e alarme falso em gate é pior que gate
> ausente, porque ensina a ignorar"*. A cura é `_ver_linha`: a linha
> `SIGLA CNJ` só conta como registro **dentro da seção que o OUTLINE do PDF
> declara Distribuição** — informação da FONTE, não do segmentador, então a
> independência do §18.3 fica de pé.
>
> A lição operacional, que vale para o próximo marcador de qualquer fonte:
> **um marcador validado só em caderno pequeno não está validado.** Os 6
> primeiros somam 6.875 páginas e não continham UM caso; o 7º, sozinho, tem
> 13.853 e continha 1.042.
>
> **E a cura teve o próprio erro, na medida seguinte.** A primeira versão do
> recorte por seção cortava em `range(inicio, fim)`, com `fim` = página em que
> a seção SEGUINTE começa. Só que **seção troca no meio da página**: as linhas
> de Distribuição impressas depois do meio dessa página sumiam da contagem, e o
> `impresso` do TRT22 caiu de 109 para **107** e o do TRT16 de 154 para
> **144**. Nenhum alarme — a comparação da perna A é `>=`, então subcontar
> passa **calado**. Foi o log de rotina (`inventário da fonte {...} → blocos
> {...}`, impresso SEMPRE, mesmo sem divergência) que mostrou a diferença. É
> exatamente para isso que ele existe: **o número do dia bom é a referência do
> dia ruim.**

**A diferença precisa ter NOME, e a conta tem que fechar.** As 82 diferenças da
coluna `Processo Nº` são **82 de 82 (100%)** de numeração trabalhista PRÉ-CNJ
(`Processo Nº ROS-02029/2006-002-16-00.5`) — acervo real que existe e para o
qual não há de-para com `Process.numero_cnj`. Reprovar a edição por isso pararia
a fonte; calar sobre isso é a perda silenciosa. O meio-termo é a **conta**:
`blocos.descartes` classifica cada descarte (`pre_cnj` / `desconhecido` /
`vazio`), e o gate só absolve quando `impresso − segmentado <= pre_cnj` **e**
`desconhecido == 0`. Qualquer outra coisa reprova.

> **Esse `<=` foi pago no meio do caminho.** A primeira versão olhava só
> `desconhecido`, e num caso em que a seção de Distribuição inteira não virou
> bloco (**20 impressos × 0 blocos, ZERO descarte**, porque as âncoras nem
> foram reconhecidas) ela rebaixava a perda total a um WARNING que dizia, sem
> ironia, *"diferença EXPLICADA por 0 matérias"*. Perda inteira anunciada como
> explicada por nada — a doença que este eixo trata, dentro do próprio eixo.
> Quem escrever gate com balde de exceção: a exceção tem que **cobrir a
> diferença numericamente**, não apenas existir.

**Eixo 1 também foi escrito** (não existia): CNJ impresso × CNJ dentro de bloco,
piso `DIARIOS_COBERTURA_MINIMA` (0,95), com o `sem_aproveit` do §4 na frente —
edição com registro impresso e ZERO aproveitável fecha terminal **com o motivo
escrito**, nunca como `vazia`.

Resultado sobre os 7 cadernos reais: **6 passam, 1 reprova** — e a que reprova
é o §20.6.

### 20.6 O TERCEIRO formato do DEJT, que o eixo achou no primeiro caderno de teste

TRT22 de 15/03/2018 reprovou: **628 de 678 CNJs impressos (92,6%)** dentro de
bloco. Os 50 órfãos, agrupados pela forma da linha (perna B):

```
  45  'Processo : #-#.#.#.#.#'        ← 'Processo   : 0000817-80.2012.5.22.0107'
   1  'PROCESSO TRT /#ª T/RO #-#.#.#.#.#--'
   1  'PROCESSO TRT AP Nº #-#.#.#.#.#--'
   ...
```

**`Processo` + espaços + dois-pontos, sem o `Nº`.** `RE_ANCORA_PROCESSO` exige
`Processo\s+N[º°o]`, então esse formato é invisível para o segmentador —
matéria inteira que nunca vira bloco. 45 ocorrências passam o
`PISO_ASSINATURA` (30), então **a perna B o nomeia sozinha**, sem marcador
declarado, exatamente como foi projetada.

E o contraste com a régua antiga é o argumento do §18 reproduzido noutra fonte:
na MESMA era, o gabarito do próprio DEJT dava **140%** (1.166 achadas contra 834
declaradas, tabela do `segmentavel_desde`) e **absolvia**. Percentual acima de
100% convivendo com um formato inteiro perdido.

Consequência prática, e ela é uma decisão: **`segmentavel_desde = 2018-01-01`
está otimista para pelo menos um TRT.** Não foi mexido — mexer sem medir mais
cadernos de 2018 seria trocar um número por outro. O gate agora reprova a
edição em vez de gravá-la pela metade, que é o comportamento correto enquanto a
decisão não vem.

### 20.7 Cobertura contra o denominador da FONTE — o que deu para medir e o que não deu

**Não deu para medir ao vivo, e isto é abstenção, não estimativa:** o
denominador do DEJT (`"1 até 20 de N"`) mora na pesquisa avançada, que está no
host fora do ar. Nenhum número novo da fonte foi obtido hoje.

O que deu: os denominadores que a fonte declarou em 16/08/2026 estão **gravados
nas fixtures do repositório** (`tests/fixtures/diarios/dejt/materia_dia_*.html`,
travados em `test_gabarito_do_trt22_e_885` e `test_gabarito_do_trt3_e_16717`).
Rodando o segmentador sobre o PDF que o **espelho** serve hoje:

| edição | declarado pela fonte (16/08) | nosso, pelo espelho (03/09) | |
|---|---|---|---|
| TRT22 10/07/2024 | **885** | **890** matérias `Processo Nº` | **100,6%** |

É o mesmo 100,6% que o §1 registrou pelo host original — **o espelho reproduz o
número da fonte morta até o dígito**, e junto vêm 109 blocos de Distribuição que
o gabarito não conta (999 itens no total, 984 com parte = 98,5%, 973 com
advogado+OAB = 97,4%).

**A cobertura do DEJT contra o acervo declarado ao CNJ continua NÃO MEDIDA**, e
não por esquecimento: é a mesma lacuna que o §2 já declara para as três portas.

### 20.8 O que ficou de fora, dito na cara

| item | estado |
|---|---|
| transporte para `dejt-caderno.jt.jus.br` | **não escrito.** Trava em `Disallow: /` (§20.2), que é decisão do dono do produto |
| `esperado()` do DEJT | continua apontando para o host morto; devolve `None` e o runner segue sem gabarito. Os dois eixos novos é que seguram |
| parser do 3º formato (`Processo   :`) | **não escrito.** Achado, nomeado, medido em 1 caderno (45 ocorrências). Quantos cadernos o têm: não medido |
| parser do 4º formato (`<CNJ> - ROT`) | **não escrito.** A perna B o achou no TRT3 de 10/07/2024: **160 CNJs órfãos** na forma `#-#.#.#.#.# - ROT` (o CNJ vem ANTES da sigla, invertido em relação à Distribuição). Nunca tinha aparecido em nenhuma medição desta fonte |
| recorte de seção por PÁGINA | é aproximação: o outline dá a página em que a seção começa, e seção troca no meio da página. `_paginas_de_distribuicao` admite a página de fronteira. O exato exigiria offset de caractere, que significa colar o caderno inteiro uma segunda vez na memória (regra nº 1) |
| parser da era pré-PJe (2008-2017) | continua faltando — 49% do inventário (46.845 edições) |
| de-para da numeração pré-CNJ trabalhista | continua faltando. Agora ao menos é CONTADO por edição (`descartes['pre_cnj']`) |
| `segmentavel_desde=2018-01-01` | **não mexido**, e agora se sabe que está otimista para o TRT22 |
| `stf` | **não ligado.** Duas decisões do §5 abertas + o defeito do §20.3.3 |
| eixo armado para `stf`, `doe-sp`, `qd-municipal` | continuam abstendo (`NÃO MEDIDO` no log) |

| onde | o que mudou |
|---|---|
| `diarios/fontes/dejt/segmentador.py` | `FORMATO_PROCESSO`/`FORMATO_DISTRIBUICAO`, `Bloco.formato`, `RE_NUMERO_PRE_CNJ`, `blocos(..., descartes)` |
| `diarios/fontes/dejt/coletor.py` | `MARCADORES_DEJT` (2), `MARCADORES_DE_REGISTRO`, `_aferir_cobertura` (os dois eixos + `sem_aproveit`) |
| `tests/test_gate_inventario_dejt.py` | 10 testes, incluindo o do balde exclusivo e o do 3º formato |
