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

> ⚠️ **Esta tabela foi medida com a régua torta.** Ela compara o `classe_codigo`
> do nosso lado com a `classe` do Datajud como se fossem a mesma coisa — e não
> são: um é a FASE que o tribunal publica, o outro é o CADASTRO do CNJ. Medido
> em 31/08/2026, o número nacional passa de **18,6% para 41,6%** quando a
> comparação é feita com a régua consertada, sem coletar nada. Ver
> [§ Classe × Fase](#classe--fase-o-mesmo-processo-tem-as-duas-e-não-são-a-mesma-coisa-31082026).

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

> ⚠️ **Esta tabela é de 14/08/2026 e foi remedida em 31/08.** Os três números
> que mais se citam dela estão errados hoje: o peso do documento é **787-820 B**
> e não 225 B (3,6×), a latência de uma requisição pelo pool de proxies é de
> **3 a 56 s** e não ~10 s, e a vazão real medida numa passada completa é de
> **474,7 docs/s** e não 1.232-1.458. E, sobretudo, **as 34,3 mil requisições já
> foram gastas**: o acervo nacional está varrido. Ver
> [A puxada nacional, medida em 31/08/2026](#a-puxada-nacional-medida-em-31082026--e-por-que-ela-já-aconteceu).

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

### 🟢 O sinal do TJSP: computado em 31/08/2026 — e o corte NÃO era o `DATAJUD_ALVO`

A seção acima estava certa no diagnóstico ("o recorte mede o próprio buraco") e
**errada na causa**. O `DATAJUD_ALVO` era o corte visível; embaixo dele havia um
segundo, e era esse que prendia o número inteiro.

#### Antes (medido em 31/08/2026, os dois lados batendo)

```sql
-- Postgres (7 s, índice proc_trib_sinalprec_idx)
SELECT (data_enriquecimento_datajud IS NOT NULL) AS tem_datajud,
       tem_sinal_precatorio, count(*)
  FROM tribunals_process WHERE tribunal_id = 'TJSP' GROUP BY 1,2;
```

| tem_datajud | tem_sinal | processos |
|---|---|---:|
| f | f | 14.504.845 |
| f | t | 763.477 |
| **t** | **(NULL)** | **1.513.486** ← 100% dos NULL |

O mesmo, do lado do Elasticsearch (`voyager-processos`, `_count` com filtro —
`exists` sobre `boolean` não sofre do problema da string vazia):

| | ES | Postgres |
|---|---:|---:|
| TJSP total | 16.781.808 | 16.781.808 |
| com sinal computado | 15.268.322 | 15.268.322 |
| sinal `true` | 763.477 | 763.477 |
| **NULL** | **1.513.486** | **1.513.486** |

Os dois lados batem **exatamente**. Não havia defasagem de índice a descontar.

#### O achado: o `'TJSP'` na lista não teria mudado nada

`data_enriquecimento_datajud IS NOT NULL` em **100%** dos NULL restantes — e o
pick do comando trazia, hardcoded:

```python
"WHERE tribunal_id = ANY(%s) AND tem_sinal_precatorio IS NULL "
"AND data_enriquecimento_datajud IS NULL "      # ← o corte de baixo
```

Ou seja: `backfill_sinal_precatorio --tribunais TJSP` (o conserto "óbvio")
selecionaria **zero** linhas e terminaria `SUCCESS: 0 processados`. Run verde,
log limpo, número redondo — e o 1,5 M continuaria invisível, agora com a
aparência de já ter sido tratado, que é pior que antes.

Foi o que de fato aconteceu em 27/08: o comando **rodou** no TJSP (é o incidente
das 12,7 h em `.ia/OPS.md`) e levou o NULL de ~4,1 M para 1,51 M. Ele não parou
por erro — parou porque **acabou o que o corte deixava ele ver**. Os 4,1 M da
medição de 25/08 e os 1,51 M de hoje são o mesmo buraco, medido antes e depois
de o comando esgotar a parte visível dele.

O corte tinha motivo na Fase 0: o sinal servia só para **priorizar a fila** do
refill datajud, e quem já foi ao Datajud não precisa de prioridade. Só que
`tem_sinal_precatorio` virou campo de **produto** — está no doc do ES, no filtro
da busca e no "potencial" do mapa (`search/agg_overview.py`) —, e aí "já
enriquecido" não é motivo nenhum para o campo ficar vazio na tela.

#### Computável sem Datajud: sim, com ZERO requisição

Amostra uniforme de **3.000 dos 1.513.486** pks (`random.Random(20260831)`,
dump completo dos ids em 188 s):

| | medido | controle |
|---|---:|---|
| processos existentes no Postgres | 3.000 / 3.000 | **100%** ✔ |
| com ao menos 1 movimentação | **3.000 / 3.000** | nenhuma abstenção necessária |
| com movimentação no índice ES | **3.000 / 3.000** | **100%** ✔ |
| movimentações por processo | mediana 26 · média 54,8 · máx 1.764 | |
| `tem_sinal_precatorio = TRUE` pelo padrão atual | **100 (3,33%)** | ≈ 50,4 k no total |

Os 1,5 M **não** são esqueletos do Datajud: são processos que já vinham do DJEN
e que o Datajud enriqueceu depois. Na mesma amostra, por origem da movimentação
(`meio`): **2.976 de 3.000 têm texto de DJEN** (`meio='D'`), 1.976 têm
movimentação do Datajud (`meio='datajud'`, que também traz texto —
`datajud/parser.py:build_texto` = nome do movimento + complementos tabelados),
159 têm de enricher.

E as duas portas contribuem para o sinal — dos 100 TRUE:

| | n |
|---|---:|
| detectável no texto **DJEN** | 93 |
| detectável no texto **Datajud** | 18 |
| **só** no Datajud (o DJEN não veria) | **7** |
| só no DJEN | 82 |

Ou seja: ler só o DJEN perderia 7% dos acertos, e ler só o Datajud perderia 82%.
O `EXISTS` sobre `movimentacao.texto` lê as duas — sem uma requisição ao CNJ.

**E a precisão de cada porta é diferente** — censo COMPLETO dos 100 positivos da
amostra, contexto de ±110 caracteres lido um a um (não sub-amostra):

| porta | acertos | falsos positivos | precisão |
|---|---:|---:|---:|
| **Datajud** (`meio='datajud'`) | 18 | **0** | **100%** |
| **DJEN** (`meio='D'`) | 93 | **5** | **94,6%** |
| união (o que o comando grava) | 100 | 5 | **95,0%** |

A porta do Datajud é pequena e **pura**: os 18 acertos são, os 18, o MESMO texto
— `Expedição de precatório/rpv`, que é o nome do movimento na TPU do CNJ. Não é
texto livre, é código de movimento do próprio tribunal; não há como confundir.

Os 5 falsos positivos são todos da porta do DJEN, e **4 deles são a mesma coisa**:
"ofício requisitório" no sentido CRIMINAL/administrativo — requisitar uma pessoa
ou a força policial, não um pagamento:

```
11888679  PRECATÓRIA DE INTIMAÇÃO (Réu) e de INQUIRIÇÃO … e OFÍCIO REQUISITÓRIO
          ao COMANDO DA POLÍCIA MILITAR ou DELEGACIA DE POLÍCIA
12032128  apresentação virtual do réu junto ao atual local de internação
          (servirá a presente decisão como ofício requisitório)
25652676  estabelecimento prisional onde recolhido o acusado … servindo, ainda,
          como OFÍCIO REQUISITÓRIO
12907621  Procedimento Comum Cível × BANCO PAN — "servirá a presente" para a
          OAB indicar curador especial (nenhuma Fazenda no processo)
11894278  rodapé com URL: ".../saiba-como-solicitar-comprovantes-de-resgate-de-
          depositos-judiciais-e-precatorios-pelo-portal-bb"
```

⇒ o termo `of[íi]cio requisit[óo]rio` carrega **4 dos 5 erros** sozinho. Quem for
apertar a precisão um dia mexe nele (por exemplo exigindo Fazenda no polo
passivo), não no `precat[óo]rio`.

Casos que **parecem** ruído e não são, conferidos no texto inteiro: falência com
"Precatório recebido oriundo de ação proposta contra a União" (o precatório é
ativo da massa), arresto sobre "o precatório expedido na ação nº …", e depósito
"de precatório de competência delegada … pelo TRF da 3ª Região". Todos TRUE.


3,33% é coerente com os 3,66% medidos em 25/08 na parte já varrida do TJSP. A
estimativa antiga de "≈ 150 k processos com sinal escondidos" era sobre os 4,1 M;
sobre os 1,51 M que sobraram ela vira **≈ 50 k**.

#### Precisão, medida dos dois lados contra uma fonte independente

O regex do Postgres foi conferido contra o **`voyager-movimentacoes-v2`**, que é
outro motor (analisador `portuguese_asciifolding`, com stemming — trata plural e
acento sozinho). Mesmos 3.000 processos:

| | n |
|---|---:|
| controle: com movimentação no índice | **3.000 / 3.000 = 100%** |
| PG=TRUE **e** ES=TRUE | 100 |
| PG=TRUE e ES=FALSE | **0** |
| PG=FALSE e ES=TRUE | **2** |

⇒ **concordância do PG com o ES = 100/100 = 100%**; **cobertura = 100/101 =
99,0%**. (Isto mede a LEITURA, não o mundo: os dois motores leem o mesmo texto.
A precisão contra o significado está medida logo abaixo, por porta.) Os 2 divergentes foram lidos um a um, e os dois dizem
"**ofícios requisitórios**" (plural), que `of[íi]cio requisit[óo]rio` (singular,
um espaço) não casa:

- `25055662` — "expeça-se competentes **ofícios requisitórios**", ação contra o
  INSS: sinal **real** que o padrão perde;
- `12548760` — "mandados/**ofícios requisitórios**" com link de audiência TEAMS
  num Termo Circunstanciado criminal: requisição de **pessoa**, não de pagamento
  — falso positivo se fosse capturado.

Um padrão tolerante a plural (`of[íi]cios? +requisit[óo]rios?|\mrpvs?\M|
requisi[çc](ão|ões) de pagamento`) acha exatamente esses **2 em 3.000** — a
mesma dupla que o ES apontou, por dois caminhos independentes. **Não foi
adotado**: o ganho líquido é 1 acerto real e 1 erro em 3.000 (+0,03%), e mudar o
padrão tornaria os 1,5 M novos incomparáveis com os 15,3 M já computados.

⚠️ **NÃO ampliar `precat[óo]rio` para `precat[óo]ri`** "para pegar o plural".
`precat[óo]rio` já casa "precatórios" por substring; o `o` final é o que separa
PRECATÓRIO de **CARTA PRECATÓRIA**. Medido nos mesmos 3.000:
`precat[óo]ri[ao]s?` sobe de 92 para 316 acertos, e **196 dos 224 a mais são
"carta precatória"** — ruído puro, e "carta precatória" é justamente uma das
facetas com ~100% de ausência no DJEN (tabela do topo deste arquivo).

**Precisão semântica** (não só lexical): censo dos **100** positivos, lidos um a
um — **95 legítimos, 5 falsos positivos (95,0%)**. A conta por porta e a lista
dos 5 estão na seção seguinte. É consistente com o que o campo declara ser:
`tem_sinal_precatorio` é **indício**, `classificacao=PRECATORIO` é o veredito.

#### O que mudou no comando

`tribunals/management/commands/backfill_sinal_precatorio.py`:

1. o corte `data_enriquecimento_datajud IS NULL` virou a flag `--so-sem-datajud`
   (desligada por padrão);
2. `--tribunais TODOS` roda em quem tiver NULL, e `--dry-run` virou **censo**;
3. **abstenção**: o `UPDATE` só toca quem tem ao menos uma movimentação —
   `EXISTS(texto ~* padrão)` devolvia `false` para quem não tem texto nenhum, e
   `false` na tela quer dizer "medimos e não tem". O run conta e declara os
   abstidos (no TJSP: **0**);
4. **teto é ERRO com o número real**: lote que estoura o `statement_timeout` é
   logado como ERROR com a contagem e a faixa de ids, entra numa lista de
   queimados que o pick exclui (sem isso, engolir o timeout viraria loop
   infinito no mesmo lote — o pick não tem `ORDER BY`), e o `FIM` sai em
   **stderr** se houve queimado. Acima de 20.000 queimados o comando aborta;
5. **censo do fora-do-recorte** no fim de todo run, em ERROR:

```
FORA DO RECORTE: 20.248.498 processos seguem com tem_sinal_precatorio NULL em
58 tribunais que este run NÃO tocou — TJMG 4.431.133, TRF3 4.018.460,
TRF1 1.861.036, TJDFT 1.157.752, TJMA 1.053.299, TRF5 992.756, …
```

Nacionalmente são **21.761.984** NULL (41 s de index-only scan). O TJSP é 7% do
problema; o resto está catalogado e visível a cada run, em vez de escondido
atrás de uma lista de quatro siglas.

#### Custo medido da corrida

`--batch 2000 --sleep 0.1`, no `.102` via `docker compose -f
docker-compose-workers.yml run --rm`:

| runners | `--batch` | vazão | duração do LOTE | sonda de latência |
|---|---:|---:|---|---|
| baseline (0) | — | — | — | 0,20 s |
| 1 | 2.000 | 65 – 78 /s | 25 – 35 s | 0,20 s |
| 2 | 2.000 | ≈ 160 /s | 25 – 35 s | 0,20 s |
| 4 | 2.000 | ≈ 300 /s | 25 – 35 s | 0,20 s |
| 8 | 2.000 | **383 /s** (medido em 120 s) | 25 – 35 s | 0,20 s |
| 2 | **500** | ≈ 150 – 330 /s | **1,6 – 2,5 s** | 0,20 s |

`FOR UPDATE SKIP LOCKED` dá lotes disjuntos, então N cópias no MESMO tribunal
não brigam por row-lock. O banco está `IO: DataFileRead` (I/O-bound, como todo o
resto), mas a sonda não mexeu em nenhuma configuração.

⚠️ **O `--batch 2000` custou caro e não comprou nada.** Lote de 35 s numa tabela
que o produto inteiro lê é pressão pura: com 8 cópias, o R105 tentou 40 vezes
aplicar a migration 0054 com `lock_timeout=3s` e não conseguiu — holders de 35 s
sobrepostos nunca abrem um buraco de 3 s. Baixar para `--batch 500` derrubou o
lote **~15×** (35 s → 2 s) com a **mesma** vazão por runner (78 → 75 /s), porque
o custo por linha é o `EXISTS` com regex, não a transação. Ver `.ia/OPS.md`.

#### Depois — a mesma consulta do antes

```sql
SELECT count(*) FILTER (WHERE tem_sinal_precatorio IS NULL)  AS nunca_varrido,
       count(*) FILTER (WHERE tem_sinal_precatorio IS TRUE)  AS com_sinal,
       count(*) FILTER (WHERE tem_sinal_precatorio IS FALSE) AS sem_sinal,
       count(*) AS total
  FROM tribunals_process WHERE tribunal_id = 'TJSP';
```

| | antes (30/08) | **depois (31/08)** |
|---|---:|---:|
| `IS NULL` | 1.513.486 | **0** |
| `IS TRUE` | 763.477 | **814.591** |
| `IS FALSE` | 14.504.845 | 15.967.217 |
| total (controle) | 16.781.808 | **16.781.808** |

**+51.114 processos com sinal** — a amostra de 3.000 previa 3,33% (≈ 50.400) e o
acervo entregou 3,38%: **erro de previsão de 1,4%**. Total inalterado: nenhuma
linha nasceu nem sumiu na corrida.

**0 abstenções** (nenhum processo sem movimentação) e **0 lotes queimados** pelo
teto, nos dois runs.

E os dois lados batem, de novo:

| | Postgres | Elasticsearch |
|---|---:|---:|
| TJSP total | 16.781.808 | 16.781.808 |
| sem o sinal | **0** | **0** |
| sinal `true` | 814.591 | **814.591** |

#### 🔴 O `atualizado_em = now()` tocou a campainha — e 524.945 docs não vieram

Isto quase passou, e o jeito como quase passou é a regra nº 4 do CLAUDE.md com
roupa nova.

O Postgres fechou em 0 NULL, mas o ES ainda tinha **524.945** docs do TJSP sem o
campo — 34,7% do lote. Não era defasagem em trânsito: a watermark do
`sync_processos_atualizados` estava **em dia** (1 min 23 s de atraso), a fila
`es_index` estava em **0** e o `FailedJobRegistry` em **0**. Ou seja, para todo
instrumento de saúde do sync, estava tudo certo — e estava faltando meio milhão.

O doc builder não tem culpa: `indexar_processos_bulk([3 ids])` chamado à mão
gravou os três valores em 5 ms. Os pks foram enfileirados, os jobs não falharam,
e mesmo assim o doc ficou com `null`. Reenfileirei os **1.513.486** pela lista de
ids em disco (3.027 jobs, 6 s para enfileirar, drenados em < 90 s) e o ES foi a
zero. **A causa raiz do sync ficou em aberto** — ver "o que eu não consegui
medir".

**A armadilha da medição, que é o que precisa ficar:** a minha primeira
conferência dos dois lados deu **60/60 sincronizados** e estava ERRADA. Eu
testei `'tem_sinal_precatorio' in doc['_source']` — e o doc velho tem a CHAVE,
com valor `null`:

```json
{"_id": "23329528", "_source": {"tem_sinal_precatorio": null}}
```

A chave presente com valor nulo passa no teste de presença e falha no de valor.
É a mesma família do `exists` que conta string vazia (regra nº 4), num campo
`boolean` — onde a gente jurava estar a salvo. A conferência certa:

```python
(doc['_source'] or {}).get('tem_sinal_precatorio') is not None
```

Com ela, os mesmos 60 deram **38/60** — batendo com o agregado (65,3%), que é
como os dois lados voltaram a conversar. Um controle que dá 100% quando o
número está errado não é controle: é anestesia.

#### O que eu NÃO consegui medir (31/08/2026)

Meia régua é pior que régua nenhuma, então o que ficou de fora:

1. **Por que o `sync_processos_atualizados` perdeu 524.945 docs.** Medi o
   sintoma e o conserto, não a causa. Watermark em dia, fila zerada, zero jobs
   falhados — e meio milhão de docs sem o valor. As duas hipóteses que sobraram,
   nenhuma provada: (a) os jobs rodaram na janela em que `models.py` tinha as
   colunas da 0054 e o banco não, e falharam de um jeito que não deixou rastro
   no registry; (b) a chave de watermark (`sync_es:wm:proc_ts`) foi perdida no
   Redis e o tique re-ancorou em `agora`, que é uma perda SILENCIOSA e definitiva
   por desenho (`if wm is None: cache.set(agora)`). A (b) é a mais grave e a mais
   barata de blindar. **Enquanto isso não for respondido, "a watermark está em
   dia" não prova que o índice está completo** — o gate tem que ser contagem dos
   dois lados, não idade de watermark.
2. **Precisão contra o mundo, não contra o texto.** Tudo que medi lê o que o
   tribunal publicou. Um precatório expedido que nunca saiu no DJEN nem virou
   movimento no Datajud é invisível para este sinal, e eu não sei quantos são —
   descobrir exige ir ao e-SAJ processo a processo, isto é, requisição.
   `FALSE` aqui quer dizer "não há sinal no que temos", não "não há precatório".
3. **A precisão dos 15,3 M computados ANTES.** Auditei 100 positivos dos
   1.513.486 novos. Os 15,3 M antigos foram gravados com o mesmo padrão, mas em
   outra população (sem passagem pelo Datajud) — a taxa de falso positivo lá
   pode ser outra, e não medi.
4. **A lacuna do plural nos 15,3 M antigos.** Medi que "ofícios requisitórios"
   custa ~0,07% de cobertura nos novos. Não recomputei os antigos, então essa
   mesma lacuna segue neles, sem número.
5. **Os 20.248.498 NULL dos outros 58 tribunais.** Estão catalogados e o comando
   os declara em ERROR a cada run, mas não foram computados — o recorte desta
   pendência era o TJSP. `--tribunais TODOS` roda neles; ninguém rodou ainda.
6. **A suíte completa não tem baseline atribuível.** A árvore de trabalho é
   compartilhada por quatro agentes e carregava, durante o meu diff, a migration
   0054 pela metade e mudanças em voo de outros três. `pytest tests/` deu
   213 falhas / 146 erros, e não dá para dizer o que é meu. O que dá para
   afirmar: os módulos que tocam o meu código (`test_sinal_recorte_datajud`,
   `test_sinal_lote_com_teto`, `test_campainha_sync`, `test_sync_incremental`,
   `test_leads_sinal_precatorio`, `test_classificador_sinal_precatorio`,
   `test_agg_entidade`, `test_set_local_timeout`) dão **96 passando** contra um
   baseline de 27 passando + 1 falha; as 2 falhas restantes são anteriores ao
   diff e de outros donos (`test_tjsp_fora_do_escopo_nao_promove` e o
   `SET LOCAL` em `tribunals/services/partes_djen.py:469`).
7. **`ruff` não rodou.** Não está instalado em nenhum ambiente que eu alcanço
   (nem no venv local, nem na imagem de prod). Conferi só `py_compile`.

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

---

## A puxada nacional, medida em 31/08/2026 — e por que ela já aconteceu

> **Em uma linha:** o #92 dizia "187,7 M que faltam, 34,3 mil requisições,
> 16-20 h, 77 GB". Medido: falta **283.987** (0,082%), **29 requisições**,
> minutos. O resto do delta é lixo do denominador do CNJ, e os 187,7 M são
> outro job — a **hidratação**, que custa 1 requisição por CNJ.

### O delta, dos dois lados (59 requisições `size:0` + `_count` no nosso índice)

```
declarado ao CNJ ....... 350.430.801      (59 tribunais rastreados + STM)
voyager-acervo ......... 344.603.487      98,34%
delta bruto ............   5.800.259
  ├─ linhas sem `numeroProcesso` ... 5.516.272   (95,1%)  ← não é processo
  └─ RESÍDUO REAL .................    283.987   (0,082%)
```

| tribunal | delta bruto | inválidos | resíduo real |
|---|---:|---:|---:|
| TJSP | 5.607.865 | 5.337.680 | **270.185** |
| TRF1 | 8.937 | 0 | **8.937** |
| TJRJ | 89.807 | 87.341 | **2.466** |
| TJMT | 2.300 | 0 | **2.300** |
| TJAC · TJSC | 50 · 46 | 0 · 0 | 50 · 46 |
| TJPE · TJMG · TRF4 · TJGO · TRF3 · TJPR · TRT2 · TRT13 · TJMS | 45.772 · 20.313 · 15.700 · 6.642 · 2.300 · 498 · 19 · 6 · 1 | idênticos | **0** |
| TJBA · TJRS · TRT20 | 1 · 1 · 1 | 0 | 1 · 1 · 1 |
| **total** | **5.800.259** | **5.516.272** | **283.987** |

### O denominador do CNJ tem linha vazia dentro

Amostra de 200 documentos do TJSP sem `@timestamp`, 200 de 200 iguais:

```json
{"numeroProcesso": null, "classe": {"codigo": "-1", "nome": "Inválido"},
 "dataAjuizamento": null, "grau": null}
```

`doc_do_datajud` já os descarta (sem 20 dígitos de CNJ não há doc). E há um
fato que fecha o argumento: **o conjunto "sem `numeroProcesso`" é EXATAMENTE o
conjunto "sem `@timestamp`"** — 5.337.680 = 5.337.680 no TJSP, e igual no TJMG
(20.313) e no TJRJ (87.341). Sem chave de ordenação, a paginação por
`range @timestamp` **nunca poderia** alcançá-los.

⚠️ `classe.codigo = -1` **não serve** como critério: é superconjunto (TJSP
5.408.140) e engloba processos REAIS com classe inválida. O critério é a
ausência de `numeroProcesso`.

### 🔴 O incremental é um no-op verde — e era assim desde sempre

Medição de hoje, 59 requisições, `@timestamp >= cursor` de cada tribunal:

    59 de 59 devolveram ZERO documentos.

E no entanto faltavam 283.987. Os dois fatos convivem por dois motivos:

1. **o cursor termina sempre em "máximo da fonte + 1"**, então `parou_por='fim'`
   é auto-confirmatório: o laço acaba exatamente onde a fonte acaba;
2. **o CNJ reescreve `dataHoraUltimaAtualizacao` em lote.** Histograma mensal do
   TJSP, o mesmo campo dos dois lados:

| mês do `@timestamp` | CNJ hoje | nosso `atualizado_em` |
|---|---:|---:|
| 2024-09 | 4.756.523 | 33.801 |
| 2025-02 | 6.192.765 | 1.511.330 |
| 2025-06 | **12.949.424** | **0** |
| 2025-10 | **15.003.492** | **0** |
| 2025-11 | 13.316.280 | 208.662 |
| 2026-02 … 2026-06 | batem em ±0,01% | batem |

Os meses recentes batem; os antigos não. São os MESMOS documentos com o
timestamp reescrito — e sempre para valores **abaixo** do nosso cursor.
Documento que anda para trás no tempo é invisível para um cursor que só anda
para a frente.

⇒ **`tick_varredura_incremental` (cron 02:20) enfileira 59 jobs por dia que não
podem trazer nada.** A única passada capaz de fechar o delta é `--do-zero`.

Desde 31/08/2026, passada incremental que traz **0 documentos** com **alvo
medido > 0** registra o erro `incremental_cego` (com o número e com a saída no
texto) em vez de devolver `fim` verde.

### O que a puxada NÃO traz — e por que isso importa mais que o delta

O `_source` do Datajud **não tem parte, advogado nem valor**. O que a varredura
enche é o `voyager-acervo`, que é **esqueleto**. Contar esqueleto como "temos"
seria dado coletado pela metade — vale menos que zero.

O número que interessa, medido hoje (cardinalidade HLL por tribunal, os 59):

```
voyager-acervo ... 344.603.487 docs  =  298.271.660 CNJs distintos  (1,155 doc/CNJ)
voyager-processos  104.003.151 docs
```

A diferença — da ordem de 190-210 M — **é o "187,7 M que faltam" do #92**. Ela
não se fecha com páginas de 10.000: cada CNJ vira processo com **uma requisição
sua** (`hidratar_cnj`). É outro job, com outro custo, e é ele que continua
sendo o gargalo real.

O card de Cobertura (`dashboard/cobertura_nacional.py`) já separa os dois: o
numerador é `tribunals_process` (acervo rico) e o denominador é a cardinalidade
do esqueleto. **Varrer mais faz a cobertura CAIR, não subir** — quem cresce é o
denominador.

### Custo medido do caminho de produção (não o do papel)

| medida | papel (14/08) | medido em produção (31/08) |
|---|---:|---:|
| bytes por doc | 225 B | **787 B** (JSON na rede, TRT20) |
| vazão | 1.232-1.458 docs/s | ver `datajud_varredura_status` durante a corrida |
| latência de 1 requisição | ~10 s | **3-56 s** (mediana ~10 s, cliente com pool de proxies) |

A latência varia 18× entre requisições porque o cliente rotaciona proxies. É
por isso que o ETA da telemetria é recalculado a cada página e não estimado uma
vez no começo.

## Runbook da puxada

```bash
# 1. MEDIR SEM ESCREVER (é isto que embasa a decisão de rodar)
manage.py datajud_conferir_acervo                  # tabela, com a coluna `inválidos`
manage.py datajud_conferir_acervo --json           # o mesmo, pra colar em issue
manage.py datajud_conferir_acervo --conexoes 5     # ETA agregado com N conexões
manage.py datajud_varredura TJMG --max-paginas 3 --dry-run   # vazão e B/doc reais

# 2. A TELA (deixe aberta durante a corrida — sem ela é caixa preta de horas)
manage.py datajud_varredura_status --watch 5

# 3. UM TRIBUNAL, ponta a ponta
manage.py datajud_varredura TRT20 --do-zero        # `--do-zero` é o ÚNICO que
                                                   # fecha delta (ver acima)
manage.py datajud_conferir_acervo --tribunais TRT20   # o gate, dos dois lados

# 4. NACIONAL, só depois que um tribunal fechar o gate
manage.py datajud_conferir_acervo --enfileirar     # devolve pra fila os INCOMPLETOs
```

### Kill switch — parar e RETOMAR são a mesma feature

```bash
manage.py datajud_varredura_status --parar      # PARA TUDO. vale em ~10s
manage.py datajud_varredura_status --retomar    # religa; cada tribunal continua do cursor
manage.py datajud_varredura_status --pausar TJSP
manage.py datajud_varredura_status --despausar TJSP
```

A varredura em curso confere o switch **a cada página** (~10 s) e sai **salvando
o cursor**; a retomada continua dali. Antes de 31/08/2026 o switch só era lido
quando um job NOVO começava — uma varredura de 20 h em curso o ignorava até o
fim, que é o oposto de "parar em segundos". O watchdog também respeita a pausa:
devolver pra fila quem o operador acabou de parar transformaria o switch num
laço.

### O que observar, e quando abortar

| sinal | onde | o que fazer |
|---|---|---|
| coluna `erros` com `resposta_grande` | `datajud_varredura_status` | nada: a página encolheu sozinha e releu o mesmo ponto |
| `perdidos_no_ms` > 0 | idem | a varredura **não está completa** — o número está no run |
| `teto_max_paginas` | idem | você impôs `--max-paginas`; o run diz quantos ficaram de fora |
| `incremental_cego` | idem | o watermark não alcança o buraco: só `--do-zero` |
| cluster ES `red` | `_cat/health` | **--parar** |
| disco livre < 200 GB | `_cat/allocation` | **--parar** |
| `_cat/thread_pool/write` com `rejected > 0` | idem | **--parar** |
| busca do produto degradando | a tela | **--parar**; site em pé vale mais que terminar 3 h antes |

### Orçamento de BYTES, nunca de páginas

`DATAJUD_VARREDURA_BYTES_ALVO` (16 MB) divide-se pelo peso **medido** do doc
para dar o `size` de cada requisição; `DATAJUD_VARREDURA_BYTES_MAX` (48 MB) é o
teto DURO por resposta — acima dele a varredura aborta o download, encolhe a
página e **relê o mesmo cursor** (idempotente, porque a paginação é `range gte`).
A primeira requisição de cada passada é uma **sonda** de 500 docs, que pesa o
tribunal antes de comprometer memória.

Nunca reintroduzir teto de PÁGINAS. Teto de páginas é o `for pagina in
range(1, 11)` que escondeu 43,6% do TJSP por 17 meses. O `--max-paginas` que
existe é ferramenta de dry-run e, quando bate, é **ERRO registrado** com o
número real do que sobrou na fonte — medido com uma requisição a mais, não
estimado.

### Deploy

O bind mount entrega o arquivo; o Python não recarrega. `manage.py` sobe um
processo NOVO e portanto lê o disco — mas os **workers da fila `varredura`**
não. Depois de um `git pull`, para que `varrer_acervo` rode o código novo:

```bash
ssh ubuntu@192.168.30.102 'cd ~/voyager && \
  docker compose -f docker-compose-workers.yml up -d --force-recreate worker_varredura'
```

Sem o `-f docker-compose-workers.yml` o restart sai verde e reinicia outro
container.

### Política para o incremental cego — as opções, com o custo medido

Registrar o erro é meia solução: o alarme grita e o dado continua sem chegar.
As opções, com o custo medido em 31/08/2026 (vazão real de **474,7 docs/s** e
**820 B/doc**, medidos numa passada completa do TRT20; requisições a 10.000
docs por página):

| política | docs por ciclo | requisições | serial | 5 conexões |
|---|---:|---:|---:|---:|
| `--do-zero` nacional | 344,6 M | 34.460 | **202 h** | ~40 h |
| janela de 365 dias | ~223 M (64,8%) | 22.335 | 131 h | ~26 h |
| janela de 180 dias | ~142 M (41,3%) | 14.235 | 83 h | ~17 h |
| janela de 90 dias | ~76,8 M (22,3%) | 7.685 | 45 h | ~9 h |
| **medir o desvio** (`datajud_conferir_acervo`) | 0 | **59** | **~25 min** | — |
| **janela localizada** (`--histograma` → `--desde/--ate`) | 2,47 M lidos → 187.585 novos | **248** (TJSP) | **~1 h 27** | — |

⚠️ A janela **lê o mês inteiro** para achar o que falta dentro dele: 2.471.983
documentos para recuperar 187.585. O custo é o do mês (1 h 27), não o dos
documentos novos (7 min). Confundir os dois foi o primeiro número que escrevi
aqui, e ele errava por 12×.

As frações das janelas foram medidas com um `date_histogram` mensal nos 12
maiores tribunais (241,4 M docs, 70% do acervo nacional).

**Recomendação: as duas últimas linhas, nesta ordem.**

1. **Medir, não re-varrer.** `datajud_conferir_acervo` custa 59 requisições e
   ~25 min e dá o resíduo por tribunal. Semanalmente, ele mostra se o buraco
   CRESCE — que é o número que ninguém tem hoje. Nenhuma política de re-varredura
   deve ser ligada antes disso: hoje o resíduo é **0,082%**, e gastar 202 h de
   quota compartilhada para recuperar 0,082% é trocar o certo pelo duvidoso.
2. **Quando o resíduo justificar, localizar antes de varrer.**
   `--histograma` compara o `@timestamp` mês a mês e diz QUAL janela varrer.
   No TJSP isso é a diferença entre 6.900 requisições (~40 h serial, 39
   documentos recuperados por requisição) e **248 requisições (~1 h 27, 756 por
   requisição)** — **28× menos tempo** para 69% do resíduo nacional.

A janela cega de 90/180/365 dias **não resolve o caso medido**: as reescritas do
TJSP caíram em buckets de 10 e 14 meses atrás, fora de qualquer janela que caiba
no orçamento. Janela por tempo é remédio para fonte que se move para a frente;
esta se move para trás.

⚠️ **Não ligue re-varredura periódica sem antes ter a série do desvio.** Sem ela
não há como saber se o custo se paga — e este é exatamente o tipo de trabalho que
produz run verde e número redondo.

### Como ler o `--histograma` (a regra do vizinho)

```bash
manage.py datajud_conferir_acervo --histograma --tribunais TJSP
```

| o que o mês mostra | o que significa |
|---|---|
| os dois lados batem (±1%) | trecho não reescrito, e completo |
| fonte tem MAIS **e os dois vizinhos batem** | **buraco de verdade** — varra a janela |
| fonte tem mais e o vizinho NÃO bate | carimbo reescrito: ignore |
| nós temos mais | carimbo reescrito: ignore |

Sem a regra do vizinho, o TJSP recomendaria varrer 2025-11 primeiro
(+13.107.618 docs, 1.332 requisições) — e ali não falta nada: é o carimbo dos
14,2 M que temos em 2025-08 e que o CNJ moveu para novembro. A varredura
gastaria 1.332 requisições reescrevendo o que já é nosso e voltaria verde.

Com a regra, sobra no TJSP **um** mês: 2026-07, +187.585, vizinhos batendo em
0,002% e 0,05%. O comando devolve a linha pronta:

```
manage.py datajud_varredura TJSP --desde 2026-07-01 --ate 2026-08-01
    # +187.585 docs · ~248 requisições · lê 2.471.983 · ~1 h 27 a 474,7 docs/s
```

### Estado em 31/08/2026 — o que ficou fechado e o que ficou aberto

| tribunal | declarado | inválidos | nosso | resíduo | gate |
|---|---:|---:|---:|---:|---|
| **TRT20** | 235.759 | 0 | **235.759** | **0** | ✔ **fechado** (era 235.758) |
| TJMG | 36.698.417 | 20.313 | 36.678.104 | 0 | ✔ fechado |
| TJSP | 74.686.714 | 5.337.680 | 69.078.849 | 270.185 | 99,61% — janela 2026-07 pronta |
| TRF1 | 12.521.855 | 0 | 12.512.918 | 8.937 | 99,93% — sem janela localizável |
| TJRJ · TJMT · TJAC · TJSC | — | — | — | 2.466 · 2.300 · 50 · 46 | — |
| **nacional** | 350.430.801 | 5.516.272 | 344.603.487 | **283.987** | **99,92%** |

TRT20 foi varrido inteiro em 31/08/2026 (`--do-zero`): 25 páginas, 25
requisições, 496,7 s, 474,7 docs/s, 819,9 B/doc, 189,4 MB lidos, `perdidos: 0`,
`erros: {}`. Fechou **235.759 contra 235.759 declarados** — o mesmo padrão de
conferência do gate de 14/08 (235.758 × 235.754), agora exato.

Controle da medição: `proc` presente em **344.603.488 de 344.603.488**
(`must_not exists` = 0) e **239 de 239** no formato do CNJ numa amostra por
faixa de chave.

**O que NÃO foi executado, e por quê:** as janelas do TJSP (248 requisições) e
qualquer passada `--do-zero` de tribunal grande. O comando está pronto e medido;
disparar quota compartilhada contra a API pública em volume é decisão de quem
responde pela licença do DataJud, não de quem prepara a puxada.

### 🔴 Dois tribunais que a régua nunca vai acusar

O gate percorre a tabela `Tribunal`. O que não está lá não aparece como buraco —
aparece como nada, que é o pior jeito de faltar.

* **STM não existe na tabela.** `api_publica_stm` responde e declara **27.055**
  documentos; nosso acervo tem **zero**. Três requisições fechariam. Não foi
  adicionado porque criar `Tribunal` mexe em tudo que itera sobre tribunais
  ativos (schedulers, refill, enrichers) — é decisão de escopo, não de coleta.
* **STF está na tabela, inativo, e o índice do CNJ não existe:**
  `api_publica_stf` devolve `index_not_found_exception`. A pendência antiga
  ("STF falhou na contagem, reconferir") fica resolvida com o motivo: não é
  rate-limit, é índice inexistente.

E uma anomalia do lado oposto, que continua aberta: **o TJDFT declara 529.535 ao
CNJ e nós temos 1.506.169 processos dele no `voyager-processos`** — 2,8× mais
acervo rico do que a fonte declara existir. Ou o índice `api_publica_tjdft` está
incompleto, ou estamos atribuindo ao TJDFT processo que é de outro tribunal.
Não é buraco de coleta; é uma pergunta sobre a régua, e vale investigar antes de
usar o TJDFT em qualquer denominador.

---

## Classe × Fase: o mesmo processo tem as duas, e não são a mesma coisa (31/08/2026)

> **Em uma linha:** 98,6% da "divergência de classe" contra o CNJ não era rótulo
> errado — era um campo só carregando dois fatos, e consertar a régua levou a
> cobertura do nicho de **18,6% para 41,6%** sem coletar um documento novo.

### A pergunta

Rotulávamos **2.337.739** processos com a classe `12078` (*Cumprimento de
Sentença contra a Fazenda Pública*). Cruzando por CNJ contra o `voyager-acervo`,
o CNJ dizia OUTRA classe numa fatia grande deles. Rótulo errado, ou dois campos
colididos?

### A amostra (uniforme por pk, com campo de controle)

Semente `20260831`, 40.000 pks sorteados em `[3.520, 106.326.832]`, **39.101
existiam** (97,8%), **861 com o rótulo** = **2,20%**.

**Controle:** 2.337.739 de 104,1 M linhas no banco = 2,245%. A amostra reproduz
a proporção conhecida da população — é isto que autoriza extrapolar. (Amostra
sorteada pelo ÍNDICE não tem como mostrar isso; ver a armadilha do `random_score`
com `_seq_no` mais acima neste arquivo.)

| | n | |
|---|---:|---|
| concorda (o CNJ também diz 12078) | 608 | |
| **discorda** (o CNJ diz outra classe) | **222** | **26,7% dos conferíveis** |
| o CNJ não tem o CNJ | 31 | 3,6% |

### 26,7% ou 37,1%? Não é o sorteio — é o critério, e ele tem causa estrutural

Uma medição anterior (30/08) deu 37,1%. Rodando as duas regras sobre a **mesma**
amostra:

| regra de "o que o CNJ diz" | discordância |
|---|---:|
| UM documento arbitrário do CNJ (o 1º que a página devolveu) | 306 — **36,9%** |
| um documento sorteado | 293 — 35,3% |
| **QUALQUER documento, em qualquer grau** | **222 — 26,7%** |

Os 37,1% reaparecem em 36,9%. A diferença é o critério, e a razão é a estrutura
da fonte: **no Datajud cada GRAU é um documento próprio, com a classe daquela
instância.**

```
5005545-67.2020.4.03.6103
  G1   7    Procedimento Comum Cível
  JE   436  Procedimento do Juizado Especial Cível
  TR   460  Recurso Inominado Cível
```

Medido na mesma amostra: **30,7% dos CNJs têm 2+ documentos e 29,3% têm 2+
classes DISTINTAS**. Perguntar "qual classe o CNJ declara" sem dizer *de qual
grau* é pergunta mal formada, e comparar contra um documento arbitrário fabrica
~10 pp de discordância que é da FONTE, não do nosso rótulo.

**O número canônico é 26,7%**, com a regra escrita ao lado: *o CNJ discorda
quando NENHUM documento dele, em nenhum grau, traz a classe*. É também a regra
conservadora para a tese abaixo — ela minimiza justamente a discordância que a
tese explica.

### O veredito: 98,6% é colisão de campo

O rótulo nasce do campo estruturado `codigo_classe` da movimentação. Usar esse
mesmo campo como prova seria circular, então a evidência veio de **canais
independentes**:

| canal | n | % das discordâncias |
|---|---:|---:|
| **TEXTO** da publicação, verbatim, com o CNJ do próprio processo: `CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA (12078) Nº …` | 207 | **93,2%** |
| **PARTES do PJe**: papel `EXEQUENTE`/`EXECUTADO` com ente público no polo passivo | 10 | 4,5% |
| **MOVIMENTO do Datajud** de fase de execução (extinção da execução, precatório, RPV) | 2 | 0,9% |
| **sem evidência** — abstenção declarada, não "erro provado" | 3 | 1,4% |

⇒ **98,6% das discordâncias são colisão de campo.**

Calibração da régua, dos dois lados:

* **sensibilidade** — no grupo em que o CNJ CONCORDA (608 processos, onde a fase
  é certa por construção), a rubrica dispara em **94,2%**;
* **especificidade** — controle negativo com 400 processos que **não** rotulamos
  12078: o canal verbatim dispara em **6 (1,5%)**. 93,2% contra 1,5% é **62× de
  separação**. (E os 6 não são falso-positivo do detector: são processos com a
  fase e sem o rótulo — o mesmo fenômeno pelo outro lado.)

O documento real, que resume tudo:

```
5000472-69.2024.4.03.6202
  DJEN 2024-10-15  "PODER JUDICIÁRIO … JUIZADO ESPECIAL FEDERAL DE DOURADOS/MS
                    CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA (12078)
                    Nº 5000472-69.2024.4.03.6202
                    EXEQUENTE: … EXECUTADO: INSS"
  Datajud (JE)     436 Procedimento do Juizado Especial Cível
```

Os dois estão certos. Um é o **cadastro**, o outro é a **fase** — e a fase é o
que o produto vende. O TRF3 concentra 208 das 222 discordâncias porque o JEF
faz o cumprimento contra a fazenda NO MESMO número de processo, reclassificando
no PJe (o Datajud publica `Retificação de Classe Processual` 143 vezes e
`Mudança de Classe Processual` 29 nesses mesmos processos) sem que o cadastro
acompanhe.

### O conserto: três fatos, três campos (migration 0054)

| campo | o que é | quem escreve |
|---|---|---|
| `classe_codigo` / `classe_nome` | **compatibilidade** — segue como está, ninguém quebra | os três, como sempre |
| `classe_cnj_codigo` / `classe_cnj_nome` | **cadastro do CNJ** — casa com `voyager-acervo.classe_codigo`, é o denominador nacional | só a porta do Datajud (`sync_processo`, `hidratar_cnj`, `backfill_classe_cnj`) |
| `fase_codigo` / `fase_nome` / `fase_em` | **a fase**: a classe com que o tribunal PUBLICOU o processo mais recentemente, com a data | ingestão DJEN (ao vivo), drainer do enricher, `backfill_fase` |

Três detalhes que não são estética:

1. **`classe_cnj_*` NÃO herda o "só preenche lacuna"** que `classe_codigo` tem.
   Com ele, o cadastro nunca seria gravado justamente onde os dois divergem —
   nos processos que motivaram a coluna. E o cadastro MUDA: é o próprio Datajud
   que publica as retificações de classe.
2. **`fase_em` existe para a fase só SUBIR.** Backfill de histórico roda o tempo
   todo aqui; sem a guarda de recência, uma recoleta de 2023 rebaixaria o
   cumprimento de 2026.
3. **Movimento do Datajud é excluído da fase de propósito.** `datajud/parser.py`
   copia a classe CADASTRAL do processo em cada movimento — lê-lo faria a fase
   virar o cadastro com outro nome, a mesma colisão um campo adiante.

`escolher_classe` (em `backfill_classe_cnj`) resolve o multi-grau pela regra do
**grau de ORIGEM** (`JE > TR > TRU > G1 > G2 > SUP`, o mesmo `escolher_grau` do
`backfill_grau`) e **abstém** quando a fonte não decide (par `G1+JE` sem
`TR`/`TRU`; ou o mesmo grau com duas classes).

### O tamanho real do nicho, recontado

**Lado do CNJ** (1.774 CNJs sorteados do `voyager-acervo` com `classe_codigo =
12078`, 3 sementes, controle `proc` em 100%):

| | | |
|---|---:|---|
| não temos o processo | 52,1% | é o #92 |
| **temos, o rótulo se perdeu, mas HÁ publicação de classe 12078** | **23,0%** | é o que `fase_codigo` passa a enxergar |
| temos e rotulamos 12078 | 18,6% | a cobertura de hoje |
| temos, sem classe e sem publicação | 5,3% | |
| temos com outra classe e sem publicação | 1,0% | |

> **Cobertura do nicho nacional: 18,6% → 41,6%.** Sobre os ~8,8 M de CNJs que o
> CNJ declara na 12078, são **≈ 2,0 M de processos que já estão no nosso banco**,
> com a publicação de cumprimento contra a fazenda gravada, e que a régua antiga
> não enxergava.

**Lado nosso**, por medição independente no índice de movimentações:
**4.449.365 processos distintos** têm movimentação de classe 12078, contra
**2.337.739** que rotulamos — **+90%**. A amostra por pk previa 4,12 M (2,34 M
rotulados + 1,75% dos 101,8 M não rotulados, medido no controle negativo): duas
contas independentes a **8%** uma da outra.

**União das duas réguas** (o nicho de verdade): os ~8,8 M do cadastro mais os
~18% do lado da fase que o cadastro não cobre (medido: dos processos com
publicação 12078, 82% o CNJ também declara 12078, 12% ele diz outra classe e 6%
ele não tem o CNJ) ⇒ **≈ 9,6 M de CNJs**.

### Custo medido em produção (31/08/2026)

| | medido |
|---|---|
| `backfill_classe_cnj`, faixa de 100.000 pks | 99.983 lidos · 97.435 no acervo (97,5%) · **97.097 escritos** · 22.201 multi-classe (22,2%) · 338 abstenções (0,34%) · **267 s** = 2,7 ms/linha ⇒ ~78 h para o acervo |
| … quantos o cadastro do CNJ CONTRADIZ | **4.865 (5,0%)** da faixa |
| `backfill_fase`, faixa de 100.000 pks | 99.998 escritos · **158 s** = 1,6 ms/linha ⇒ ~46 h |
| … em quantos a FASE difere do `classe_codigo` gravado | **16.896 (16,9%)** da faixa |
| reindex dirigido de 199.981 docs | **~600 docs/s**, 5,5 min |

Nenhuma requisição ao CNJ: `classe_cnj_*` sai do `voyager-acervo` (índice nosso)
e `fase_*` sai das publicações que já estão no nosso Postgres.

### Verificação em produção, ponta a ponta

Faixa de controle `id ∈ (9.200.000, 9.210.000]`, 10.000 processos:

```
ANTES  docs com `fase_codigo` no índice ..........      0
       (contado com must_not term:'' — `exists` conta string vazia, regra nº 4)
backfill_fase → 10.000 escritos, campainha tocada
+200 s   1
+240 s   10.000 / 10.000
```

O caminho percorrido é o de produção, não um script: `backfill_fase` (Postgres)
→ `atualizado_em = now()` → `sync_processos_atualizados` no **scheduler que já
estava rodando** → fila `es_index` → **`worker_es_index` reiniciado com o código
novo** → documento no índice com o campo. É prova de comportamento, não de
disco.

### Runbook

```bash
# medir sem escrever (a régua do antes/depois)
manage.py backfill_classe_cnj --de 9000000 --ate 9100000 --sem-reparo --json
manage.py backfill_fase       --de 9000000 --ate 9100000 --sem-reparo --json

# as corridas (retomáveis pelo checkpoint, com freio por ms/linha)
manage.py backfill_classe_cnj --sleep 0.1 --parar-ms-linha 12
manage.py backfill_fase       --sleep 0.1 --parar-ms-linha 12

# parar de qualquer lugar, sem deploy
manage.py shell -c "from django.core.cache import cache; \
    cache.set('datajud:backfill_classe_cnj:off', True); \
    cache.set('tribunals:backfill_fase:off', True)"

# levar ao índice a faixa que acabou de ser escrita (sem esperar o poller)
manage.py reindexar_processos --desde-id <lo> --ate-id <hi> --sleep 0.05
```

### O que NÃO foi medido, e por quê

* **A corrida completa não rodou.** Foram 210.000 processos de 104,1 M
  (`classe_cnj` na faixa 9,0-9,1 M; `fase` em 9,0-9,21 M). As projeções de 78 h
  e 46 h saem de faixas contíguas de pk — e faixa contígua amostra poucos
  MOMENTOS de ingestão, então o custo real da corrida inteira pode diferir.
* **A régua de fase mede o que está no nosso acervo, não o país.** "1,75% dos
  não-rotulados têm a fase escondida" vem de 400 processos: intervalo de
  confiança de ±1,3 pp, que sobre 101,8 M são ±1,3 M. O número do índice
  (4.449.365) é exato e é o que deve ser citado.
* **Não sei quanto do `fase_codigo` está DESATUALIZADO** por processo que
  publicou pela última vez há anos. `fase_em` existe justamente para essa
  pergunta ser respondível depois; ela ainda não foi feita.
* **Os 3 casos "sem evidência"** (1,4%) não foram provados errados — não há
  como reconstruir a posteriori qual escritor gravou aquele `classe_codigo`
  (não existe tabela de auditoria do enricher). São abstenção, não erro.
* **A publicação foi lida só nos primeiros 900 caracteres.** O cabeçalho com a
  classe fica no começo, mas quem tiver a prova depois do corte foi contado como
  "sem evidência" — o viés é contra a minha própria tese.
