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
| `search/mappings.py` | `ACERVO_MAPPING` — índice `voyager-acervo` |
| `search/busca_api.py` | `get_esqueleto` — fallback da busca por CNJ |
| `tribunals.Tribunal` | `datajud_varredura_cursor/_em/_docs/_status` (watermark) |

Fila RQ dedicada `varredura` (timeout 24h) — separada da `datajud` porque uma
varredura de tribunal grande é job de horas e empurraria pro fim da fila as
sincronizações por processo, que atendem usuário.

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
