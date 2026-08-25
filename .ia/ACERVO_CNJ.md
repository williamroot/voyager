# Completude do acervo — varredura do Datajud

> **Em uma linha:** a ingestão sempre foi DJEN-only, e por isso tínhamos ~13% do
> acervo nacional; a varredura do Datajud traz o **esqueleto** dos outros 87%.

## A lacuna (medida em 14/08/2026)

Duas medições independentes, que convergiram:

| tribunal | declarado ao CNJ | nosso | amostra de 300 CNJs: faltam |
|---|---:|---:|---:|
| TJSP | 74.492.169 | 4.271.354 | **96,2%** |
| TJMG | 36.595.940 | 4.686.311 | 85,7% |
| TJRJ | 23.117.604 | 3.466.533 | 89,0% |
| TRF3 | 17.385.970 | 4.104.831 | 66,3% |
| TRF1 | 11.990.136 | 2.263.456 | 80,7% |
| TRT3 | 4.196.220 | 469.625 | 83,0% |

**Total declarado nos 60 tribunais que rastreamos: 343.235.554** (inclui
duplicação por grau — o mesmo CNJ é um doc em G1 e outro em G2).

### Por que faltava

O DJEN é veículo de **comunicação**, não cadastro: só entra processo que teve
intimação publicada em diário. Perfil dos ausentes (amostra de 2.000 CNJs,
ausência por faceta):

| corte | ausentes |
|---|---|
| processo **físico** | 93% (TJMG) · 100% (TJSP) |
| TJSP no **SAJ** | 96,5% — contra 52,9% no Projudi |
| última atualização em **2021** | 97,9% — em 2026: 61% |
| criminal, carta precatória, execução fiscal, juizado | ~100% |
| grau JE / G1 | 93% / 83% — contra G2 57% |

Ou seja: não há publicação quando a intimação é **pessoal ou por portal** (MP,
Defensoria, Fazenda, entes cadastrados no PJe push — Lei 11.419/2006 art. 5º),
quando **nada acontece** no processo, quando o tribunal publica em **diário
próprio**, ou quando o ato é **anterior à adesão** dele ao DJEN.

### O que o Datajud resolve — e o que NÃO resolve

O `_source` do Datajud tem: `numeroProcesso`, `classe`, `assuntos`,
`orgaoJulgador`, `dataAjuizamento`, `grau`, `sistema`, `formato`, `nivelSigilo`,
`movimentos`. **Não tem parte, advogado nem valor.**

Então a varredura entrega **esqueleto**. Partes e valor continuam vindo do DJEN
(destinatários) e dos enrichers PJe/e-SAJ — e o enriquecimento é o gargalo real:
**112.820 processos/dia** na frota inteira, com enricher em só **16 dos 60**
tribunais. Varrer 343M com ele levaria 8 anos; por isso o esqueleto existe
também como **instrumento de escolha** (classe/assunto/órgão permitem priorizar
sem gastar requisição).

Cobertura do nicho — classe 12078, *Cumprimento de Sentença contra a Fazenda
Pública*, que é onde mora o precatório:

| tribunal | no CNJ | temos |
|---|---:|---:|
| TJSP | 739.833 | **5,2%** |
| TJMG | 365.455 | 28,8% |
| TRF1 | 1.370.418 | 47,2% |
| TRF3 | 54.221 | 61,7% |
| TJRJ | 40.324 | 59,5% |

## Como a varredura pagina (a parte não-óbvia)

O índice do Datajud **só aceita `sort` por `@timestamp`** — por `_id` ou
`numeroProcesso` devolve 400 (*Fielddata is disabled*). Consequências:

1. **`search_after` puro PULA documento.** A chave não é única; se a página
   termina no meio de um empate de milissegundo, os irmãos somem. Por isso
   paginamos com `range: {@timestamp: {gte: cursor}}` e **relemos a cauda de
   propósito** — o `_id` vem do próprio Datajud (`TJMG_156_JE_17296_<cnj>`), o
   que torna a reescrita idempotente.
2. **Um ms mais cheio que a página travaria o laço.** `_desempatar_ms` resolve
   com `from`+`size` (teto `max_result_window` = 10.000) e, se nem isso couber,
   fatiando por `grau` e depois por `classe.codigo`. O que ainda sobrar é
   **contado em `perdidos`** — a varredura declara a perda em vez de fingir
   completude.

```
cursor=0
  └─ range gte cursor, size 10.000, sort @timestamp
       ├─ página cheia e cursor avançou  → cursor = último_ts   (relê a cauda)
       ├─ página cheia e cursor PARADO   → _desempatar_ms(); cursor += 1
       └─ página incompleta              → fim
```

`@timestamp` == `dataHoraUltimaAtualizacao`, então o mesmo cursor vira
**watermark incremental**: a passada seguinte pede `gte watermark` e traz só o
que nasceu ou mudou (F5, cron 02:20).

## Números medidos da puxada

| medida | valor |
|---|---|
| página aceita | **10.000** docs (o máximo) — ~10s por requisição |
| vazão real, serial | **1.232–1.458 docs/s** (medido em TJMG e TRT20) |
| requisições pro país inteiro | 343M ÷ 10k = **34,3 mil** |
| tempo serial | ~77h · com 4-5 conexões: **~16-20h** |
| custo em disco | ~225 B/doc → **77 GB** pros 343M (livre: 718 GB) |

**Movimentos ficam de fora**: são ~73 por processo (~15 KB), o que faria cada
página pesar 150 MB em vez de 4 MB — e 343M × 73 seriam ~10 bilhões de linhas
(o índice de movimentações já tem 1,16B ocupando 684 GB). Movimento vem na
hidratação, por CNJ.

### Gate de completude (executado)

TRT20 varrido inteiro: **235.758** docs no nosso índice contra **235.754**
declarados no início da varredura. Os 4 a mais entraram durante os 162s de
execução (`@timestamp` novo ≥ cursor) — é completude, não erro.

## Peças

| arquivo | papel |
|---|---|
| `datajud/varredura.py` | o puxador (`Varredura`, `varrer_tribunal`, `marcar_no_acervo`) |
| `datajud/hidratacao.py` | esqueleto → processo real (`hidratar_cnj`, `hidratar_lote`) |
| `datajud/jobs.py` | `varrer_acervo`, `tick_varredura_incremental`, `hidratar_processo` |
| `datajud/ingestion.py` | `sync_processo` (movimentos por CNJ) + `_entregar_ao_indice` (write-through) |
| `datajud/indice.py` | gate de completude do ÍNDICE desta porta (janela de escrita, dois lados) |
| `search/mappings.py` | `ACERVO_MAPPING` — índice `voyager-acervo` |
| `search/busca_api.py` | `get_esqueleto` — fallback da busca por CNJ |
| `tribunals.Tribunal` | `datajud_varredura_cursor/_em/_docs/_status` (watermark) |

Fila RQ dedicada `varredura` (timeout 24h) — separada da `datajud` porque uma
varredura de tribunal grande é job de horas e empurraria pro fim da fila as
sincronizações por processo, que atendem usuário.

### Checkpoint (por que o watermark é salvo no meio)

Varrer o TJSP é job de **horas**. O watermark era gravado só no fim, e um
restart de worker — ou o `AbandonedJobError` que o RQ dá quando o container
morre — jogava fora todo o progresso. Aconteceu com o TJMG em 14/08/2026, no
deploy que subiu a frota de 4 pra 8 réplicas. Agora `varrer_tribunal` salva o
cursor **a cada 20 páginas** (200k docs ≈ 2 min de trabalho em risco). Vale só
na passada completa: a filtrada não pode tocar o watermark.

## O que a porta grava tem que virar dado BUSCÁVEL (24/08/2026)

A varredura enche o `voyager-acervo` direto no ES, então ela nunca teve este
problema. Quem tinha era o **outro** caminho desta porta — `sync_processo`, que
puxa os MOVIMENTOS por CNJ e escreve no Postgres. Ele gravava e não entregava:

| o que ele escreve | por que não chegava ao índice |
|---|---|
| `Movimentacao` (`bulk_create`) | `bulk_create` não dispara `post_save` ⇒ o write-through de `search/signals.py` nunca rodava. Só o poller de 10 min levava as linhas ao ES |
| meta do `Process` (`.update()` de classe/assunto/órgão/valor/totais) | `.update()` não dispara `post_save` **e não mexe em `atualizado_em`** (`auto_now` só roda em `Model.save()`) — e `atualizado_em` é a chave do keyset do poller de processos. Este não tinha poller NENHUM |

Medido em produção com amostra aleatória e `_mget` por id, com o poller
SAUDÁVEL (atraso de 122.604 ids ≈ 1 tick):

- movimentações escritas há **0-5 min**: 3.000 de 3.000 fora do índice (100%);
  há 5-15 min: 1.268 de 3.000 (42,27%); acima de 15 min: 0.
- processos tocados nas últimas 2 h: **0 de 500** com o doc em dia
  (critério: `doc.enriquecido_em >= PG.data_enriquecimento_datajud`).
  Nas janelas mais antigas: 8/500 (1d-2h), 98/500 (3d-1d), 0/500 (30d-7d).
- restrito aos tribunais SEM enricher, onde a classe só pode ter vindo daqui:
  **2 de 345 (0,6%)** das classes gravadas chegaram ao índice na janela
  2h-30min.

Escala: a porta escreve **327.566 movimentações/h em média** (pico medido de
798.824/h às 12h UTC, vale de 28.610/h às 14h — 28x de amplitude) e toca
**~5.600 processos/h**, 74 movimentos por processo; 22.475.738 processos já têm
`data_enriquecimento_datajud` (1.703.782 nos últimos 30 dias).

A cura é a mesma da terceira porta, adaptada ao que esta porta é:

1. **entrega na gravação** — `_entregar_ao_indice` no `on_commit` manda as
   movimentações NOVAS em lote e o doc do processo para a fila `es_index`.
   Entrega só as novas (e não o lote inteiro como no diário) porque aqui seriam
   ~73 movimentos por processo x 5.000 sincronizações/h = 365 mil docs/h contra
   27,5 mil que realmente mudaram — 13x — e esta porta não reescreve texto;
2. **`atualizado_em` carimbado à mão** no `.update()`, para o poller de
   processos passar a enxergar o que ele nunca enxergou;
3. **gate dos dois lados** (`datajud/indice.py`, cron de 15 min) sobre a
   **janela de escrita** (`inserido_em`), não sobre `(tribunal, dia)`: uma
   sincronização espalha linhas por décadas de `data_disponibilizacao`, então o
   recorte do diário nunca alcançaria esta porta. Custo medido por passo de
   15 min: 2,33 s (movimentações, `mov_inserido_tribunal_idx`) + 0,29 s
   (processos, `proc_datajud_em_idx`). O passo é ponto de partida: na hora de
   pico ele não cabe no teto de leitura e o gate **divide a janela ao meio**
   em vez de travar — mesmo remédio do 413 do `_bulk`;
4. **backfill** por faixa explícita: `manage.py datajud_conferir_indice
   --desde … --ate …`, que não toca o watermark do cron.

Validado em produção no mesmo dia: escrita de 0-60 min de idade passou de
**100% fora do índice** (movimentações) e **100% com doc anterior à escrita**
(processos) para **0 em 39.040 movimentações e 0 em 5.607 processos**. O cron
rodou sozinho duas passadas seguidas (416.186 e 376.220 movimentações
conferidas, 10.342 processos reparados) e o backfill de 3 dias fechou
**418.273 processos conferidos, 356.739 reparados, 0 restantes**.

Dívida que permanece, com o número: **22.475.738** processos já têm
`data_enriquecimento_datajud`, e só os 3 dias mais recentes foram backfilados.
O resto é reindex de fase 2/3 do plano de `SEARCH_SCHEMA.md`, não trabalho de
gate — e a partir de agora ele não CRESCE mais, que era o problema.

Detalhes, tabelas completas e os EXPLAIN em [`SEARCH_SCHEMA.md`](SEARCH_SCHEMA.md).

## Runbook

```bash
# medir antes de agir (não escreve watermark)
datajud_varredura TJMG --max-paginas 3 --dry-run

# varredura completa de um tribunal (retomável)
datajud_varredura TJMG

# só o nicho — não mexe no watermark, porque viu só um recorte do tempo
datajud_varredura TJSP --classe 12078

# marcar quem já está no acervo rico (ES→ES, não toca Datajud nem Postgres)
datajud_varredura TJMG --marcar-acervo
```

**Kill switch** (a APIKey do Datajud é compartilhada — se o CNJ estrangular,
tem que parar em segundos):

```python
from datajud.jobs import set_varredura_pausados
set_varredura_pausados({'TJSP', 'TJMG'})     # vira no-op sem deploy
```

**Cotas**: `DATAJUD_VARREDURA_RPM` (default 40) é um bucket SEPARADO, pego
ANTES do global `DATAJUD_RATE_LIMIT_RPM` (100). Nessa ordem porque o bucket
próprio é o mais restrito: se ele negar, nem encostamos no global — que fica
livre pro enriquecimento, que é quem atende usuário.

## Pendências conhecidas

- **TJDFT declara 359.608**, baixo demais pro tamanho do tribunal — provável
  índice fora do padrão `api_publica_<sigla>`. Investigar antes de varrer.
- **STF e TRT3** falharam na contagem (404 e rate-limit). Reconferir.
- O `search_by_date` de `datajud/client.py` ordena por `_id` e portanto
  **nunca funcionou** — está morto, não é caminho de código vivo.

---

## Medição nacional do teto por UF — 59 tribunais (18/08/2026)

Um general e 11 agentes mediram **todos** os tribunais: para cada um, sondaram a
API paginando na força bruta até esgotar e compararam com o que temos.

**212.504.437 publicações recuperáveis** em **12.934 dias-tribunal**, sobre uma
base de 1,344 bilhão de movimentações — **+16% do acervo**. 41 de 59 tribunais
sangram; 18 descartados COM PROVA.

### Como um dia afetado é identificado

Assinatura do caminho fatiado: run de janela de 1 dia, `fonte='djen'`,
`status='success'`, ≥9.000 itens e **razão itens/página < 700**. O caminho por
UF grava em lotes de `BATCH_SIZE=500` e cada uma das 27 fatias termina numa
página parcial, então a média cai pra ~490-500/pg; o caminho flat dá ~990-1000.

### Ranking (recuperável estimado)

| tribunal | perda média/dia | recuperável |
|---|---|---|
| TJSP | 84.7% | 64.895.691 |
| TJPR | 73.8% | 38.807.963 |
| TJMG | 70.1% | 20.054.027 |
| TRT2 | 61.3% | 14.599.568 |
| TJRS | 66.5% | 11.709.666 |
| TJGO | 64.0% | 11.055.268 |
| TJRJ | 36.4% | 9.779.900 |
| TJBA | 49.6% | 8.157.297 |
| TJSC | 54.9% | 7.627.068 |
| TRT15 | 40.4% | 5.491.200 |
| TRF3 | 27.9% | 4.013.425 |
| TRT1 | 29.6% | 3.462.830 |
| TRT3 | 29.8% | 2.955.367 |
| TRT4 | 25.6% | 2.667.080 |
| TJAM | 25.6% | 1.269.615 |
| TJMT | 8.5% | 863.224 |
| TJPE | 20.4% | 812.345 |
| TRT9 | 17.8% | 734.000 |
| TJRR | 32.9% | 620.000 |
| TRF4 | 9.4% | 606.811 |
| TJCE | 11.6% | 459.862 |
| TRF6 | 18.4% | 372.220 |
| TJAP | 28.6% | 310.000 |
| TJMA | 7.2% | 270.628 |
| TJSE | 10.3% | 216.095 |
| TJMS | 12.4% | 210.786 |
| TRF2 | 10.6% | 183.233 |
| TJTO | 6.4% | 103.000 |
| TJAL | 0.6% | 43.281 |
| TRT6 | 12.1% | 28.000 |
| TJRN | 9.1% | 23.453 |
| TJDFT | 0.4% | 19.000 |
| TRT7 | 41.9% | 18.000 |
| TRT5 | 5.1% | 18.000 |
| TRT12 | 0.9% | 14.000 |
| TJPI | 2.7% | 8.288 |
| STJ | 0.3% | 7.455 |
| TRT11 | 7.0% | 6.114 |
| TRT19 | 4.8% | 3.196 |
| TRT17 | 4.8% | 3.081 |
| TJRO | 0.0% | 2.300 |
| TRT10 | 7.0% | 1.700 |
| TRT18 | 0.0% | 400 |
**Sem perda pelo teto (18):** TJPA, TJPB, TJES, TJAC, TRF5, TST, TRT8, TRT13,
TRT14, TRT16, TRT20, TRT21, TRT22, TRT23, TRT24 — mais TRF1 (perdia, mas
incremento atribuível = 0), TJPE e TJMA (o teto não cortava; perdem pelo buraco
do sem-OAB).

O padrão dos descartes é estrutural: TST e STJ são nacionais (volume espalhado
pelas 27 seccionais); TRF5 reparte entre seis OABs; os TRTs pequenos nunca
cruzaram 10.000 num dia. ⚠️ O **TJPA é limpo por sorte de volume** — pico
histórico 9.948, a 5% do gatilho. No dia em que cruzar 10.000 começa a perder
~7% em silêncio. Vigiar.

### ⚠️ A régua canônica é o Postgres djen-only

O ES mente dos DOIS lados e as duas mentiras são grandes:

* **infla** — a tabela `Movimentacao` mistura DJEN com Datajud, então `_count`
  cru no TJMG em 2025-08-13 dá 347.526 contra os 73.386 que a API inteira
  entrega no dia (4,7×). Sem filtrar `tipo_comunicacao` aos tipos que o DJEN
  entrega, a cobertura sai inflada em até 15×;
* **desinfla** — o lag do reindexador: TJSE tem agosto/2025 com 127 docs, TJRN
  2026-08-12 com zero.

Medir cobertura do DJEN pelo ES sem filtro de tipo é medir errado.

### Buraco MAIOR, e separado deste: dias que nunca foram ingeridos

Cinco relatórios tropeçaram nele. **Não é o teto** — são dias sem run nenhum:

| tribunal | evidência | ordem de grandeza |
|---|---|---|
| TRF1 | 710 de 1.480 dias úteis com <100 publicações; em 2025-08-13 a API entrega 53.919 e temos **9** | 18-35 M |
| TRF5 | 1.037 de 1.393 dias com <100; jan/2025 com 22 de 23 dias vazios | dezenas de M |
| TJTO | 2025 **inteiro** zerado (163/163 dias úteis), ~6.000/dia | ~930 k |
| TJRO | 2021, 2022 e 2023 inteiros zerados (781 dias úteis) | ? |
| TJRS | grosso de 2023-2024 sem run de 1 dia | ? |
| TJSP | ~360 dias com run num período de ~750 dias úteis | ? |

Provavelmente vale mais que os 212M do teto. É missão própria.


### 1.232 runs verdes escondiam fatia perdida (18/08/2026)

O defeito nº 3 da Fase 0 não era hipótese: era estado de produção. Varrendo
`IngestionRun` com `fonte='djen'` e `status='success'`:

    runs success com erro de fatia dentro ....... 1.232  (todos `uf_fetch`)
    dias-tribunal marcados como cobertos ........ 1.165

    TJDFT 293 · TJMA 84 · TJMT 84 · TJPR 62 · TJRJ 59 · TJGO 57
    TRF4 45 · TRT2 40 · TRT3 34 · TJPE 30 · TJSP 30 · TJAM 26 · TJMG 26 …

`_dia_coberto` pula qualquer dia com run `success`, então esses 1.165 dias
ficariam fora de QUALQUER backfill futuro — inclusive o dos 212M.

Reclassificados para `failed` com a marca `reclass_fatia_perdida_2026-08-18`
acrescentada em `erros` (o motivo original fica). Não é reescrever história: o
run falhou mesmo — perdeu fatia e gravou o dia pela metade. O que era mentira
era o `status`.

**Como conferir se voltou a acontecer:**
```python
R.objects.filter(fonte='djen', status='success').exclude(erros=[])  # tem que dar 0
```

---

## O que o esqueleto do Datajud já tem e o Postgres jogava fora (25/08/2026)

Dois campos que a auditoria de completude do DADO ranqueou (`ENRICHMENT.md`,
achados 3 e 7). Nenhum dos dois custa uma requisição: o dado está no
`voyager-acervo` (o esqueleto nacional que a varredura gravou) ou no próprio
`tribunals_process`.

### `grau` — o campo que separa RPV de precatório

`doc_do_datajud` **já grava** `grau` em cada doc do `voyager-acervo` desde
sempre; quem não lia era o Postgres (`_meta_updates_from_source` e
`hidratacao.hidratar_cnj`).

Domínio real, medido no índice inteiro (`_count` por termo — nunca `exists`,
que conta string vazia como presente):

| grau | docs | o que é |
|---|---:|---|
| `G1` | 203.782.129 | 1º grau |
| `JE` | **73.791.952** | **Juizado Especial — paga por RPV, não por precatório** |
| `G2` | 41.972.803 | 2º grau |
| `TR` | 14.272.244 | Turma Recursal (também RPV) |
| `SUP` | 8.159.129 | tribunal superior |
| `TRU` | 68.645 | Turma Regional de Uniformização |
| **soma** | **342.046.902** | = total do índice ⇒ **`grau` presente em 100%** |

**21,6% do acervo nacional é `JE`.** Sem o campo, o funil de produto do
Juriscope mistura dois produtos com prazo e preço diferentes. Valor fora
desses seis: `logger.warning` e coluna vazia — abster, nunca normalizar no
chute (regra nº 6).

**Limitação honesta:** o `_source` bruto do Datajud **não é guardado em lugar
nenhum** — não há model, não há coluna JSONB. Então `grau` só chega por dois
caminhos: (a) processos sincronizados/hidratados daqui pra frente, e (b) um
backfill que leia o `voyager-acervo` por `proc` e case com `Process`. O (b) é
possível **sem tocar no Datajud** (o índice é nosso), mas é um job novo, não é
o que este commit entrega.

### `nivelSigilo` — presente em 100% e informativo em 0%

`nivelSigilo` vale **0 em 342.046.902 documentos e qualquer outro valor em 0**
(contado termo a termo, 0..5, mais o `must_not exists`). A API pública do CNJ
só expõe o que é público, então o campo não carrega informação nenhuma em
escala nacional. Mapeá-lo para `segredo_justica` escreveria `False` em 102 M de
processos — exatamente a afirmação que a migration 0052 desfez ao tornar a
coluna NULL. **Decisão: não é lido.** Quem sabe dizer que há segredo é o e-SAJ,
pela página "informe a senha" (achado 5 de `ENRICHMENT.md`).

### `assunto`: o `)` que o PJe não fecha (8,2 M, zero requisição)

O detalhe do PJe entrega o assunto hierárquico com o código da folha **sem o
parêntese de fecho**, e empilha os assuntos do processo separados por `\n`:

```
DIREITO CIVIL (899)  -  Responsabilidade Civil (10431)  -  Indenização por Dano Material (10439)  -  Acidente de Trânsito (10441
    DIREITO DO CONSUMIDOR (1156)  -  Responsabilidade do Fornecedor (6220)  -  …
```

`CLASSE_COM_CODIGO_RE` exigia o fecho ⇒ `assunto_codigo=''` e `assunto_nome`
recebia a hierarquia inteira, truncada em 255.

Medido em 25/08/2026 — 12 âncoras uniformes em `id`, `random.Random(20260825)`,
blocos de 40.000 pks consecutivos = **480.000 processos varridos**, 84 s:

| | amostra |
|---|---:|
| processos com `assunto_nome` | 209.580 |
| … **sem** `assunto_codigo` | 120.755 (57,6%) |
| … código recuperável do texto já gravado | **105.690 (87,5%)** |
| … abstenções (truncado em 255 sem `\n`, ou formato sem código) | 15.065 (12,5%) |
| processos **com** código cujo nome ainda era hierarquia | 9.602 |
| … desses, com o texto apontando **outro** código (conflito) | 418 (0,47%) |

O regex **antigo** recuperava **0 dos 120.755**.

Duas descobertas que a auditoria não tinha:

1. **O separador é `  -  ` com DOIS espaços.** Cortar por ` - ` mutila nome de
   folha legítimo (`Crédito Direto ao Consumidor - CDC`, `Agente Agressivo -
   Eletricidade`). Medido: `  -  ` em 100% (5.598/5.598) de uma janela de
   200 mil pks; ` - ` de um espaço em 144 delas, sempre dentro do nome.
2. **41,6% são multi-assunto (`\n`).** Tratar o texto como UMA string casa o
   fim da ÚLTIMA hierarquia — e quando o campo bateu no teto de 255 esse fim é
   o código de um **ancestral** da segunda hierarquia. Era a origem dos "1.515
   com código de ancestral" da auditoria. Isolando a primeira hierarquia
   ANTES, esses viram recuperação boa: é por isso que 87,5% > 72,7%.

**Conferido contra fonte independente** (regra nº 5, os dois lados): dos pares
(código → nome da folha) recuperados, 150 sorteados foram comparados com o
`voyager-acervo` — o assunto que o **CNJ** declara para aquele código.
**150 de 150 com o código certo**; 141 com o nome idêntico e 9 com variante de
nomenclatura da própria TPU (`&quot;` do Datajud, `Erro Médico` × `Serviços de
Saúde` para 10503, `Corrupção de Menores` × `Corrupção de Menores - art. 218`).
**Controle negativo do mesmo teste: o nome hierárquico bate 0 de 150.**
99,9% dos códigos recuperados (105.624 de 105.690) já existiam em
`tribunals_assunto`.

### Catálogo `Assunto` limpo (executado em 25/08/2026)

| | antes | depois |
|---|---:|---:|
| linhas com a hierarquia no lugar do nome | **1.690 de 2.681 (63,0%)** | **42** |
| `ClasseJudicial` | 0 de 658 | 0 de 658 |

1.648 nomes corrigidos por `bulk_update`; **nenhuma linha apagada**
(`Assunto.codigo` é PK e `Process.assunto` é FK `PROTECT`). Os 42 que ficaram
são abstenções declaradas:

- **21 truncados em 255** sem `\n` — não dá para provar qual é a folha.
- **21 conflitos**, que são **decisão pendente de merge, não bug de parser**:
  - `00001`…`00022` (19 linhas, 163 processos, todos **TRF1**): o código é
    zero-padded e o nome termina em `Atendimento Bancário (401`. A tabela local
    do TRF1 tem um código que o Datajud não conhece.
  - `14196`/`14197` (1 processo): o nome da folha termina em `(1989)`/`(1987)`
    — **ano**, não código. O regex antigo capturou o ano porque o parêntese do
    ano estava fechado e era o último. O parser novo detecta a divergência e
    abstém.

### Runbook

```bash
# medir sem escrever (a régua do antes/depois)
manage.py backfill_assunto --de <pk> --ate <pk> --sem-reparo --json
manage.py backfill_assunto --modo catalogo --sem-reparo

# a corrida (retomável pelo checkpoint no cache)
manage.py backfill_assunto --sleep 0.2 --freio-ms 800 --teto-linhas 2000000

# parar de qualquer lugar, sem deploy
manage.py shell -c "from django.core.cache import cache; \
    cache.set('enrichers:backfill_assunto:off', True)"
```

**Custo medido em produção** (25/08/2026, escrita real, recorte
`id ∈ (42809753, 42819753]`): 2.713 linhas em 23,8 s = **114 linhas/s**,
≈ 8,8 ms por linha, com o drainer aplicando ~126 mil events/h em paralelo.
Nesse ritmo, 11,3 M de linhas ≈ **27,5 h de escrita contínua**. Leitura seca
(`--sem-reparo`): 40.000 pks em 6,7 s.

Prova do recorte, por contagem independente (não pelo log do job):

| | antes | depois |
|---|---:|---:|
| `assunto_nome <> '' AND assunto_codigo = ''` | 2.665 | **1** |
| `assunto_id IS NULL` | 2.715 | **2** |
| nome ainda hierárquico | 2.673 | **2** |

O que sobrou é exatamente 1 abstenção + 1 conflito que o job declarou.

**O que este backfill NÃO faz:** não reindexa no Elasticsearch. `bulk_update`
não dispara `post_save`, então o doc de `voyager-processos` mantém o assunto
velho até o reindex passar por ele.

---

## `grau` a partir do índice, e o recorte do TJSP — medido em 25/08/2026

### `grau` sem tocar na chave do CNJ: dá, e é barato

A varredura já grava `grau` em cada doc do `voyager-acervo`. `backfill_grau`
casa `Process.numero_cnj` com `proc` do índice por `terms` em lotes de 1.000.

| | medido |
|---|---:|
| processos achados no índice | **85,7%** (17.129 de 19.995, faixa de 20.000 pks) |
| `terms` de 1.000 CNJs — quente | 109 ms (0,11 ms/CNJ) |
| `terms` de 1.000 CNJs — **frio** | 1,0 – 2,8 s (o número honesto para varrer tudo) |
| escrita, ES **quente** | **8.316 linhas em 8,5 s = 978 linhas/s = 1,02 ms/linha** |
| escrita, ES **frio** (a corrida de verdade) | 15.084 linhas em 44,5 s = **2,95 ms/linha** |
| projeção do acervo (~87 M linhas) | **≈ 50 h**, **zero requisição ao CNJ** |

⚠️ **A projeção honesta é ~50 h, não 25 h.** Os 25 h saíam da medição com o ES
quente; a corrida percorre o índice inteiro e paga o preço frio. A curva
medida nos 6 primeiros blocos, em ms/linha:

    2,95 → 3,76 → 2,59 → 2,15 → 2,07 → 1,90

O primeiro bloco é o pior porque o índice está gelado; depois estabiliza perto
de **2,0 ms/linha** — ainda o dobro dos 1,02 ms do teste quente. Decomposição
dos dois primeiros blocos (20.000 pks cada):

| bloco | duração | **dentro do ES** | requisições ES |
|---|---:|---:|---:|
| pk 0–20.000 | 44,5 s | **19,4 s (44%)** | 17 |
| pk 20.000–40.000 | 70,4 s | **40,0 s (57%)** | 20 |

**Metade do custo é o Elasticsearch frio, não o Postgres.** Isso foi separado
com uma sonda de controle — a mesma consulta indexada, na mesma faixa, a cada
20 s durante a corrida: mediana **4,6 ms** contra **3,2–3,8 ms** de baseline
antes de começar. O Postgres não sente a corrida, e a corrida também não sente
os 4 shards do backfill de partes que rodavam em paralelo.

E a **busca não degrada**: com o backfill correndo, `_cat/thread_pool/search`
dá `rejected=0`, `queue=0`, CPU do nó em 9%, e uma busca real do produto
responde em 9–11 ms quente (1,87 s no primeiro toque frio). A corrida é
sequencial — uma requisição por vez —, então não satura o nó.

1,02 ms/linha é **8,6× mais barato** que o backfill de assunto (8,8 ms/linha):
uma coluna curta, `UPDATE … FROM (VALUES)` em lote de 2.000, sem FK e sem
catálogo.

Prova por contagem independente (recorte `id ∈ (42809753, 42819753]`, 10.000
processos): o job relatou `JE 1.589 · G1 6.483 · G2 238 · TR 6 · fora do
acervo 1.684`, e o `SELECT … GROUP BY grau` no banco devolveu **exatamente os
mesmos números**. `JE` = 19,1% do recorte (nacional: 21,6%).

**Um CNJ tem MAIS DE UM grau.** G1 e G2 são documentos distintos no Datajud:
**24,3%** dos processos achados têm 2+ graus (amostra aleatória uniforme de pk,
20.000 pks, semente 20260825, 16.357 CNJs achados). A sonda anterior dizia
12,2% — vinha de **faixa contígua** de pk e subestimava pela metade. É a mesma
armadilha do `random_score` com outra roupa: bloco contíguo de pk amostra
poucos MOMENTOS de ingestão, não o acervo.

`Process.grau` é escalar, então a regra é o **grau de ORIGEM** —
`JE > TR > TRU > G1 > G2 > SUP` — porque é ele que decide o produto.

Conferido contra os pares REAIS antes de aplicar:

| combinação | n | veredito |
|---|---:|---|
| `G1+G2`, `G1+G2+SUP`, `G2+SUP` | 2.788 | `G1`/`G2` — SUP nunca é origem ✔ |
| `JE+TR`, `JE+SUP+TR`, `JE+SUP+TR+TRU` | 918 | `JE` — Juizado → Turma Recursal → Uniformização no STJ ✔ |
| `G1+TR`, `G1+G2+TR`, `G2+TR` | 67 | `TR` — o "G1" é `01 VARA JUIZADO ESP. DA FAZENDA PUBLICA`; a Turma Recursal PROVA o juizado ✔ |
| **`G1+JE`, `G1+G2+JE`, `G1+G2+JE+SUP`** | **104 (0,64%)** | **ABSTÉM** — a fonte se contradiz ✗ |

O par que abstém, com o documento real:

```
5017073-48.2024.4.04.7003 [TRF4]
  G1  Procedimento Comum Cível                2a Vara Federal de Maringa
  JE  Procedimento do Juizado Especial Cível  2ª Vara Federal de Maringá
```

São varas adjuntas ("VARA FEDERAL … COM JUIZADO ESPECIAL FEDERAL") que o
tribunal rotula ora `G1`, ora `JE`. Marcar `JE` num processo cuja classe
principal é `Procedimento Comum Cível` diria RPV onde é precatório — erraria o
PRODUTO, não um campo. Coluna vazia, e o job conta quantos, por motivo.

#### Ressalva: `JE ⇒ RPV` é 99,92%, não 100%

Medido no acervo nacional, classe `Precatório` (TPU 1265), 618.558 documentos:

    G2  395.126 (63,9%) · G1 210.638 (34,1%) · SUP 12.328 (2,0%)
    JE      462 ( 0,1%) · TR       4 (0,0%)

O Juizado Especial **da Fazenda Pública** expede precatório quando o valor
passa do teto de RPV. São 466 documentos — nada que mude a regra, mas a tela
não pode dizer "JE = RPV" como identidade.

### Segunda porta do TJSP: SUSPENSA em 25/08/2026, e por quê

A abertura chegou a ser autorizada com a conta de "3,55% do TJSP ≈ 104 mil
processos ≈ 17 h de quota". **Os dois números estavam errados**, e a medição
que os derrubou está aqui.

Amostra **aleatória uniforme por pk** — 20.000 pks sorteados com
`random.Random(20260825)`, 19.572 existentes, **3.107 do TJSP**:

| | amostra | % do TJSP | ≈ acervo TJSP (~16,3 M) |
|---|---:|---:|---:|
| sem `data_enriquecimento_datajud` | 2.792 | 89,9% | ~14,6 M |
| … com `tem_sinal_precatorio = TRUE` (**o recorte**) | **85** | 2,74% | **~446 k** |
| … com `tem_sinal_precatorio IS NULL` (**nunca varrido**) | **783** | 25,2% | **~4,1 M** |
| … com sinal FALSE (fora, medido) | 1.924 | 61,9% | ~10,1 M |
| órfãos (`nao_encontrado`/`erro`) sem datajud | 368 | 11,8% | ~1,9 M |
| … órfãos sem datajud **e** com sinal | **11** | 0,35% | **~58 k** |

**Universo real: ~14,6 M, não 2,94 M.** Os 2,94 M eram o número de ÓRFÃOS
(`nao_encontrado`/`erro`), não o de processos sem segunda porta.

**Custo real: ~74 h de quota integral, não 17 h.** Só os órfãos com sinal —
o recorte que de fato casa com o achado 7, porque órfão é quem não tem porta
nenhuma — custam **≈ 9,7 h**.

### 🔴 DEFEITO — o recorte filtraria por um sinal que o TJSP nunca teve

Isto **não é nota de rodapé**. `tem_sinal_precatorio` é sinal NOSSO, não da
fonte: **se ele tiver falso-negativo, o recorte herda o erro e nunca
descobrimos, porque só olhamos o que já marcamos.** Não escreva, em lugar
nenhum, que este recorte "cobre o que importa".

E no TJSP o sinal não tem falso-negativo — ele **não existe**, por política que
está no código:

```python
# tribunals/management/commands/backfill_sinal_precatorio.py:24
DATAJUD_ALVO = ['TJPR', 'TRF4', 'TRF6', 'TRF2']    # ← o TJSP NÃO está aqui
```

O comando que computa o sinal roda, por padrão, em quatro tribunais, e o TJSP
não é um deles. Resultado medido: **25,2% do TJSP (≈ 4,1 M processos) tem
`tem_sinal_precatorio IS NULL`** — ficam de fora do recorte **por OMISSÃO, não
por medição**. São 9,2× o tamanho do próprio recorte (~446 k).

**Abrir o refill assim seria o recorte medindo o próprio buraco** — um corte
plausível que exclui em silêncio e devolve run verde. É literalmente o
`for pagina in range(1, 11)` do CLAUDE.md, com outra roupa: o teto parece
razoável, ninguém vê o que ficou de fora, e o log fica limpo.

⇒ **Nenhum refill de Datajud para o TJSP até o sinal ser computado para ele.**

E dá para estimar o que a omissão esconde. Entre os processos TJSP que TÊM o
sinal medido, a taxa de positivos é 85/2.324 = **3,66%** (coerente com os 3,55%
contados no índice). Aplicada aos 4,1 M nunca varridos: **≈ 150 k processos com
sinal que o recorte não enxerga** — a mesma ordem de grandeza do recorte
inteiro. Rodar `backfill_sinal_precatorio --tribunais TJSP` **antes** de gastar
quota é o que separa "recorte declarado" de "recorte cego".

Conferir a cobertura (amostra uniforme de pk, ~40 s, não depende do índice):

```sql
-- sorteie ~20.000 pks com semente declarada e conte por estado do sinal
SELECT count(*) FILTER (WHERE tem_sinal_precatorio IS NULL)  AS nunca_varrido,
       count(*) FILTER (WHERE tem_sinal_precatorio IS TRUE)  AS com_sinal,
       count(*) FILTER (WHERE tem_sinal_precatorio IS FALSE) AS sem_sinal
FROM tribunals_process WHERE id = ANY(%s) AND tribunal_id = 'TJSP';
```

### Armadilha: amostra sorteada pelo ÍNDICE mentiu por 50×

Antes de chegar aos números acima eu sorteei processos do TJSP com
`function_score`/`random_score` sobre o `voyager-processos` e conferi cada id
no Postgres. Deu **1,7% sem `data_enriquecimento_datajud`**. A amostra uniforme
por pk, no mesmo dia e no mesmo banco, deu **89,9%**.

A amostra do índice estava errada, e a explicação óbvia foi **refutada por
medição**: não é "quem foi ao Datajud está no índice" — dos processos SEM
datajud, 99,6% (996 de 1.000) estão no índice. O que sobra é o próprio
`random_score` com `field: _seq_no`: o score é função da ordem de ESCRITA, e o
top-N cai numa janela estreita de `_seq_no`, isto é, numa janela de escrita —
não numa amostra do índice.

**Regra que fica:** para estimar proporção de um campo do Postgres, sorteie
**pk** e vá ao Postgres. O índice serve para contar o que está no índice.
Amostra de 20.000 pks custou 40 s e não depende de nada além do banco.
