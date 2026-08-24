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
