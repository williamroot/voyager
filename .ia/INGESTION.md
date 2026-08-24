# Ingestão DJEN

> ⚠️ **O DJEN é a porta ESTREITA.** Ele é veículo de *comunicação*: só entra
> processo que teve intimação publicada em diário. Medido em 14/08/2026, isso
> nos dava **~13% do acervo declarado ao CNJ** (343M docs).
>
> As outras duas portas:
> - **Varredura do Datajud** — o *esqueleto* nacional (sem parte, sem valor):
>   [`ACERVO_CNJ.md`](ACERVO_CNJ.md).
> - **Diários próprios** — texto verbatim de *antes* de o tribunal aderir ao
>   DJEN (o TJSP só entrou em **14/03/2025**, a Justiça do Trabalho em
>   **01/08/2024**, e o STF nunca entrou): [`DIARIOS.md`](DIARIOS.md).
>
> **Cuidado ao mexer em cobertura/lag:** desde 2026-08-16 `IngestionRun` tem o
> campo `fonte` (default `'djen'`) e TODA query de cobertura do DJEN filtra por
> ele. Sem esse filtro, um run do DJE/TJSP no dia X faz o `_dia_coberto` do DJEN
> pular a ingestão daquele dia — perda silenciosa. Ver `DIARIOS.md` §3.

## API DJEN

```
GET https://comunicaapi.pje.jus.br/api/v1/comunicacao
    ?siglaTribunal=TRF1
    &dataDisponibilizacaoInicio=YYYY-MM-DD
    &dataDisponibilizacaoFim=YYYY-MM-DD
    &pagina=1
    &itensPorPagina=100        ← max
```

Sem auth. Resposta JSON com `count`, `items[]`. Cada item = 1 movimentação.

## Cliente HTTP (`djen/client.py`)

`DJENClient.iter_pages(sigla_djen, data_inicio, data_fim)` — gerador de páginas.

**Por request:**
- timeout: connect=10s, read=60s
- header User-Agent configurável (`DJEN_USER_AGENT`)
- Sleep 1.0s entre páginas (`DJEN_PAGE_SLEEP_SECONDS`)

**Estratégia de proxy híbrida** (`_pick_proxy`):
- Modo normal: sorteia Cortex (residencial) vs Pool (datacenter ProxyScrape) por request, com `random() < DJEN_CORTEX_RATIO` (**default 0.0 desde 29/07/2026** — ProxyScrape é o PRIMÁRIO, tem mais banda; Cortex só explícito via `prefer_cortex=True`, ou fallback: pool vazio/degradado e retry pós-falha do pool). Subir o ratio via env se uma onda de WAF queimar o datacenter.
- Modo `prefer_cortex=True` (fila `manual`): tenta Cortex primeiro (latência baixa pro user esperando feedback). Pool é fallback.
- Em retry, `prefer_other_than=last_failed_source` força a fonte oposta.
- Pool armazenado em Redis (`voyager:proxies:scrape:list`), bad TTL 600s.

**Tratamento de erros (cumulativo nos retries):**
- `403/429`: marca proxy bad (se vier do pool) + retry com outro
- `5xx`: backoff longo (factor 3, máx 180s) — não marca proxy bad
- `4xx (não 403/429)`: erro real, sem retry, raise
- `ConnectionError/Timeout/ChunkedEncodingError/ContentDecodingError`: retry, marca proxy bad
- Backoff: `min(60s, 3 × 2^attempt + jitter)` para 403/429; `min(180s, 3×factor × 2^attempt + jitter)` para 5xx
- Máx 8 retries (`DJEN_MAX_RETRIES`)

### Circuit-breaker (5xx em massa) — incidente 2026-07-10

O DJEN (governo) devolve `500 {"message":"O sistema está muito ocupado"}` sob carga.
Em **2026-07-10** ele passou a 500ar de forma persistente; com **40k jobs `backfill_dia`
× 8 retries** martelando, viramos parte do problema (congestão auto-infligida) — a
ingestão parou ~59h e a MV `mv_ingestion_rate_hora` ficou defasada.

Fix (`djen/client.py`): **circuit-breaker fleet-wide via cache/Redis**.
- Cada `5xx` → `_record_5xx()` conta na janela (`DJEN_CIRCUIT_5XX_WINDOW=120s`).
  Ao atingir `DJEN_CIRCUIT_5XX_THRESHOLD=15` → **abre** o circuito (`djen:circuit_open`)
  por `DJEN_CIRCUIT_COOLDOWN=300s`.
- Com o circuito aberto, `_fetch` **fast-fail** com `DjenBusyError` (subclasse de
  `DjenServerError`) **sem tocar no DJEN**. Um `200` fecha o circuito (`_record_success`).
- `backfill_dia` trata `DjenBusyError` como **adiar** (return `{'skip':'circuito_aberto'}`,
  não empilha no FailedRegistry); `tick_backfill_retroativo` **não alimenta** a fila com
  o circuito aberto. Resultado: a fila drena como skips, o DJEN respira, e a ingestão
  **retoma sozinha** quando ele volta.
- Runbook: fila `djen_backfill` inflada + 5xx em massa = DJEN sobrecarregado. Não é
  nosso bug. Opcional acelerar: esvaziar `djen_backfill` + limpar FailedRegistry
  (`q.empty()` + `FailedJobRegistry.remove(..., delete_job=True)`) — os dias são
  re-enfileirados pelo tick quando o DJEN normaliza.

### Watermark por tribunal no tick de backfill — incidente 2026-07-29

Com ~25 tribunais em backfill simultâneo (TRTs/TST ativados 07-05) numa fila
`djen_backfill` única, o teto **global** (`BACKFILL_WATERMARK=4000`) não dava
fairness: quem enchia a fila primeiro monopolizava a FIFO e os demais viam
`fila>=4000` → "aguardando" pra sempre; a fronteira recente dos TRTs congelou
(lag 20d). Fix (`djen/jobs.py::tick_backfill_retroativo`):

- `BACKFILL_WATERMARK_POR_TRIBUNAL=200` — limite operacional é por tribunal;
  o global (agora 8000) é só sanidade (27×200=5.400 < 8.000).
- Jobs do tick usam **job_id determinístico** `bfd:<sigla>:<dia>` — contagem
  por tribunal via `get_job_ids()` (LRANGE barato, sem fetch de payload) e
  dedupe natural de re-enfileiramento entre ticks. O fan-out do daily continua
  com id aleatório + `at_front` (não conta no cap por tribunal).

**Frontend correlato** (`base.html::buildIngestaoRateChart` + `queries.ingestion_rate_por_hora`):
quando a MV está defasada, mostra o **último snapshot conhecido** (janela ancorada em
`max(hora)`, não em `NOW`) com selo **"⚠ defasado há Xh"** — em vez de esconder o gráfico.

## Parser (`djen/parser.py`)

`parse_item(item, tribunal, run) → ParsedItem | None`

**Validações:**
- Chaves esperadas: `EXPECTED_KEYS` (frozenset) — qualquer extra/missing dispara `SchemaDriftAlert`
- CNJ obrigatório: extrai de `numeroprocessocommascara` ou `numero_processo` ou regex em `texto`
- `data_disponibilizacao` obrigatória (parseia ISO ou `YYYY-MM-DD HH:MM:SS` ou `YYYY-MM-DDTHH:MM:SS.fffZ`)
- Itens inválidos: skip + append em `run.erros`

**Mapeamento de campos:** todos os 23 campos conhecidos da DJEN viram colunas explícitas em `Movimentacao`.

## ingest_window (`djen/ingestion.py`)

Coração da ingestão. Pra uma janela `(data_inicio, data_fim)`:

**O dia sai FLAT** (desde 18/08/2026). `iter_pages` pagina até a página voltar
incompleta, sem teto — medido no TJSP: 262 páginas, 261.076 itens, ids todos
distintos, zero página incompleta no meio.

⚠️ **Não existe "cap rígido de 10k".** O `count` da DJEN satura em 10.000 porque
por baixo é o `max_result_window` do Elasticsearch: é um **PISO** ("tem pelo
menos 10 mil"), não um teto de paginação. Construir lógica em cima dele foi a
origem das duas maiores perdas de acervo do projeto.

O caminho antigo — fatiar o dia em 27 requisições por `ufOab` — está atrás da
escotilha `DJEN_ESTRATEGIA_UF` (padrão OFF) porque tem **defeito próprio**:

- **é cego a publicação sem advogado com OAB.** Provado na unidade em cinco
  tribunais: TJPE 2025-08-13 tem 2.853 itens sem OAB e são exatamente as 2.853
  que faltavam; TJMA 911 = 911; TJRN, STJ e TJPB idem. 2% a 10% de todo dia
  acima de 10.000 — e o conserto do teto de páginas (17/08) NÃO recupera esses;
- **custa 27× mais requisição** à API do CNJ (rate-limit de 20/s);
- **uma fatia perdida derrubava o dia inteiro em silêncio** — ver abaixo.

**Janela paralela** (`DJEN_PAGINAS_PARALELAS`, 8). A página é offset puro, então
buscar 8 de cada vez não muda o que volta — só o relógio. Serial, o canário do
TJSP (261.076 publicações) levou 163 minutos: 1,61 pg/min.

**O teto de memória é BYTE, não página** (`DJEN_BYTES_EM_VOO`, 24 MB, desde
24/08/2026). "8 páginas × 1000 publicações ≈ 30 MB" assumia publicação de ~3 KB
e erra por 27× no TJDFT (56 KB por publicação, medido) — foi o que matou 342
work-horses com SIGKILL. `itensPorPagina` agora se ajusta ao peso medido do
tribunal; a paginação continua indo até esgotar. Ver "O OOM do TJDFT" abaixo.

A janela também enxerga o que a versão serial não podia: **página incompleta
seguida de página com dado = paginação mentiu**, e isso é ERRO, não `return`.

**Fatia perdida = run FAILED.** O limiar era 14 de 27 fatias, o que trata a
fatia do DF (77% do dia no TJDFT) igual à de um estado com 40 itens. Resultado
medido: 1.232 runs `success` com `uf_fetch` dentro, cobrindo 1.165
dias-tribunal — e `_dia_coberto` pula dia com run `success`, então esses dias
ficariam fora de qualquer backfill futuro. Hoje uma fatia basta pra falhar.

1. Cria `IngestionRun(status='running')`
2. `for items in client.iter_pages(...)`: chama `_process_page(items, tribunal, run, cnjs_tocados)`
   - `_process_page` envolto em **`transaction.atomic()`** — todo INSERT da página é atômico
   - Step 1: `Process.objects.bulk_create(novos, ignore_conflicts=True)` por CNJ ainda não conhecido
   - Step 2: re-query pra mapear CNJ → process_id
   - Step 3: `SELECT external_id WHERE tribunal=X AND external_id IN (...)` — conta novos vs duplicados (TOCTOU aceito)
   - Step 4: `Movimentacao.objects.bulk_create(movs, ignore_conflicts=True)`
   - Métricas atualizadas + `run.save(update_fields=...)` incremental (incluindo `erros` pra não perder em SIGKILL)
3. Trigger SQL atualiza `Process.total_movimentacoes`/`primeira/ultima_movimentacao_em` automaticamente
4. `run.status='success'` ou `'failed'` (com traceback no `erros`)

## Jobs RQ (`djen/jobs.py`)

| Job | Fila | Timeout |
|---|---|---|
| `run_daily_ingestion(sigla)` | `djen_ingestion` | 2h |
| `run_backfill(sigla, force_inicio=None)` | `djen_backfill` | 24h |
| `refresh_proxy_pool()` | `default` | 2min |
| `watchdog_ingestao()` | `default` | 2min |
| `sincronizar_movimentacoes(process_id)` | `default` | 5min |

`run_backfill` é resilient + retry-friendly:

```python
chunks = chunk_dates(inicio, fim, days=30)
for chunk in chunks:
    if IngestionRun(success).exists(janela=chunk): pulados += 1; continue
    IngestionRun.filter(status=failed, janela=chunk).delete()  # retenta
    try: ingest_window(...); completados += 1
    except: falhas += 1; log + continue   # NÃO mata o job inteiro
if all(IngestionRun(success).exists(c) for c in chunks):
    Tribunal.update(backfill_concluido_em=now)
```

## Scheduler (`djen/scheduler.py`)

Container `scheduler` roda `manage.py djen_register_schedules_and_run`. Na boot:

1. **Cancela schedules anteriores** com tag `voyager-cron` (idempotência — sem duplicação a cada restart)
2. Re-registra:
   - `run_daily_ingestion(sigla)` para cada `Tribunal.ativo=True`, escalonados em 30min (TRF1 04:00, TRF3 04:30, ...)
   - `refresh_proxy_pool` a cada 15min
   - `watchdog_ingestao` a cada 5min
3. `Scheduler(connection, interval=30).run()` — loop infinito

`run_daily_ingestion` faz no-op silencioso se `Tribunal.backfill_concluido_em IS NULL` — evita brigar com backfill em andamento.

## Watchdog de ingestão

`djen.jobs.watchdog_ingestao` roda a cada 5min na fila `default` e faz auto-heal:

0. **Código velho e cemitério de falhas** (21/08/2026): compara o mtime de todo
   `.py` do projeto que está em `sys.modules` com o `starttime` do processo pai
   (`_alerta_codigo_velho`), compara o `birth_date` de TODOS os workers do RQ
   com o `.py` mais novo do disco (`_alerta_workers_velhos` — pega os ~300
   containers, não só o que roda o watchdog) e conta o `FailedJobRegistry`.
   Ver OPS §"Deploy que não recarrega".
1. **Mata zumbis**: `IngestionRun.status=running` e `finished_at IS NULL` há >1h → marca FAILED + grava motivo. Worker que crashou e deixou rastro não trava o sistema.
2. **Re-enfileira backfill**: pra cada tribunal ativo com `backfill_concluido_em IS NULL`, se nenhum job dele em execução → `tick_backfill_retroativo.delay(sigla)`. Se redis perdeu state ou backfill morreu, recupera sozinho.
3. **Re-enfileira daily**: pra tribunal com backfill ok mas sem `IngestionRun success` há >26h → `run_daily_ingestion.delay(sigla)`.
4. **Ressuscita dia de recuperação órfão** (`ressuscitar_dias_de_recuperacao`).

Detecção de "já tem job pra essa sigla" usa `job.args[0]` como chave — evita duplicar quando um backfill está realmente em curso.

### Orçamento de tempo (21/08/2026)

O job tem `timeout=120` no decorador, mas **morrer por `JobTimeoutException`
apaga o relatório inteiro**: o que já foi feito não aparece no log, o teto não
vira ERRO, e a única marca é uma linha no `FailedJobRegistry` — que, medido, é
o lugar que ninguém abre (4.259 falhas acumuladas em 4 dias sem ninguém ver).

Então: `WATCHDOG_ORCAMENTO_S=90` conferido antes de cada etapa, e o que não
coube sai como ERRO em `etapas_puladas`. Toda leitura vai sob
`SET LOCAL statement_timeout` de 20s (`_leitura_com_teto`) — `SET` solto morre
no pgbouncer transaction-mode.

Números medidos em prod (banco sob carga de batch, `tribunals_ingestionrun`
com 207.772 linhas / 88 MB):

| etapa | tempo |
|---|---|
| zumbis (UPDATE) | 0,02s |
| loop de 59 tribunais (daily stale) | 0,53s |
| agregação de `failed` de 1 dia (7d) | 0,18s → 4.244 linhas |
| agregação de `success` de 1 dia (7d) | 0,04s → 2.013 linhas |
| 200 `enqueue` no Redis | 0,17s (0,8 ms/job) |
| **total** | **~1,6s** contra orçamento de 90s |

Os 11 estouros históricos (17/08/2026, todos em 15 minutos: 1 watchdog + 10
`tick_backfill_retroativo`, estes dentro de `_dias_cobertos`) **não eram custo
de algoritmo** — no mesmo código, hoje, o pior tribunal leva 0,14s em
`_dias_cobertos` e os 59 somam 1,40s. Era contenção de disco no Postgres. Como
são 59 ticks numa fila com 2 workers, cada um preso 120s, eles bloquearam a
FIFO da `default` e levaram o watchdog junto (head-of-line blocking). Com o teto
de 20s por leitura, o tique aborta com `{'skip': 'banco em contenção'}` + ERRO e
devolve o worker 6x mais rápido.

### Dias de recuperação órfãos — o que NÃO filtrar

`ressuscitar_dias_de_recuperacao` devolve o dia avulso (`f2:<SIGLA>:<dia>`) que
ficou `failed` e nunca foi refeito. Duas armadilhas medidas em 21/08/2026, com
os números que provam que "otimizar" ali destruiria a recuperação:

* **97,4% (2.929 de 3.007) dos órfãos já têm um `success` cobrindo o dia.**
  Isso não é redundância: a recuperação nacional existe pra refazer exatamente
  o dia que fechou `success` batendo o cap de 10k
  (`djen_reprocessar_janelas_capped`: `paginas_lidas>=100` e
  `novas+duplicadas>=10000`). Filtrar por `_dia_coberto` apagaria 97% do
  trabalho achando que estava economizando.
* **`bfd:<sigla>:<dia>` na fila NÃO ocupa o dia** (1,5%, 45 de 3.007):
  `backfill_dia` faz skip em dia coberto, então ele não refaz o dia capado. O
  `f2:` tem que entrar; o `bfd:` duplicado custa um skip sem request.

O critério certo de "já voltou" é temporal: `success` de 1 dia **mais recente
que a última falha**, ou o dia já estar na fila/agendado/em execução como `f2:`
ou `adiado:`.

Estado dos órfãos no dia da medição (janela de 7 dias):

| tribunal | dias órfãos |
|---|---:|
| TRF3 | 948 |
| TJMG | 372 |
| TJRJ | 334 |
| TJRS | 286 |
| TRF4 | 250 |
| TJSP | 245 |
| TRF2 | 234 |
| TRF6 | 200 |
| outros (7 tribunais) | 138 |
| **total** | **3.007** |

96% deles falharam em **19/08**, e em amostra ALEATÓRIA de n=120 o motivo em
117 (97,5%) era `DJEN circuito aberto (sobrecarregado)` — o incidente de
`réplicas × páginas = 112` descrito acima. Não é perda de dado: é trabalho
enfileirado que o watchdog devolve a 200/tique (≈75 min pra drenar 3.007).

### O OOM do TJDFT — RESOLVIDO em 24/08/2026

**O censo, completo (não amostra).** `FailedJobRegistry.get_job_ids()` devolve o
sorted set na ordem de EXPIRAÇÃO, então ler "os primeiros N" já apontou pro
tribunal errado antes. Censo dos **703** ids da `djen_backfill`:

| classe | n | % | onde |
|---|---:|---:|---|
| **OOM** (`waitpid returned 9`) | **342** | 48,6% | 333 TJDFT · 9 TJAM |
| deadlock no Postgres (`tribunals_process`) | 203 | 28,9% | TRF2 55 · TRF3 50 · TRF6 37 · TRF4 30 · TJSP 17 · TJRJ 8 |
| id órfão (hash expirado — não é falha nova) | 131 | 18,6% | — |
| transporte/HTTP | 14 | 2,0% | TRF1 13 |
| timeout RQ (21.600 s) | 4 | 0,6% | TJSP 3 |
| abandonado | 1 | 0,1% | TJDFT |

⚠️ **Duas armadilhas ao medir o cemitério**, as duas já custaram alarme falso:

1. `get_job_ids()` devolve por ordem de EXPIRAÇÃO. "Os N primeiros" não é
   amostra — ou se faz o censo completo, ou se sorteia declarando o n;
2. **traceback velho em job_id reenfileirado.** Os ids do `backfill_dia` são
   determinísticos (`bfd:<sigla>:<dia>`), então reenfileirar o mesmo dia
   reaproveita o hash: `enqueued_at` é renovado e `started_at`/`ended_at` somem,
   mas o `exc_info` da falha ANTERIOR fica. Um censo que date a falha pelo
   `enqueued_at` conta OOM de ontem como OOM de agora — foi o que fez 4 dias do
   TJAM parecerem OOM novo em 24/08, com os mesmos 4 ids aparecendo duas vezes,
   sem `started_at` nenhuma das duas, enquanto os dias estavam vivos e
   coletando. Falha sem `started_at` é traceback velho, não falha nova.

A `djen_ingestion` tinha **0** falhas. Dos 342 OOM, **197 são o MESMO dia**
(`backfill_dia TJDFT 2026-08-21`, ~12 tentativas/hora ao longo de 21/08): o dia
morria, nunca virava `success`, o watchdog reenfileirava, o dia morria de novo.
No banco o número é maior que no registry (que expira): **1.876** runs do DJEN
marcados `watchdog: status=running sem finished_at por >1h — worker crashou` em
7 dias.

**A causa, medida.** TJDFT 2026-08-21, paginando sem gravar nada no banco:

```
14.651 publicações no dia ............. 822,6 MB de texto
⇒ 56 KB por publicação (a conta do código assumia ~3 KB)
```

A `itensPorPagina=1000` isso são **55 MB de JSON por requisição**. Com
`DJEN_PAGINAS_PARALELAS=3` e a leva anterior ainda viva enquanto a próxima era
buscada, ficavam 6 páginas co-residentes. Pico de RSS medido: **957 MB**, contra
`mem_limit: 1g`. Controle com `DJEN_PAGINAS_PARALELAS=1` no mesmo dia: **640 MB**
— o pico acompanha a JANELA, como a conta prevê.

Não era o dia acumulado: era **a página**. O erro estava em contar memória em
PÁGINAS, quando a publicação de um tribunal pesa 27× a de outro.

**O conserto (`DJENClient.iter_pages`).** O que fica constante agora é o
**orçamento de BYTES em voo** (`DJEN_BYTES_EM_VOO`, 24 MB), dividido pelas
páginas paralelas; `itensPorPagina` virou a variável de ajuste.

1. **sonda**: a 1ª leva vai sozinha, com `PAGE_SIZE_SONDA=250`, só pra pesar a
   publicação daquele tribunal antes de comprometer memória;
2. **calibração com decaimento**: o peso varia MUITO dentro do mesmo dia (no
   TJDFT as levas mediram 24,6 KB, 220,4 KB, 336,3 KB, 578,8 KB e até 978,0 KB
   por item). Guardar o máximo de todos os tempos prende a página no pior
   trecho até o fim do dia (28 itens, 523 páginas — medido); guardar só a
   última leva re-expõe a cada oscilação. O peso lembrado perde ¼ do valor a
   cada leva — era ½ até a tarde de 24/08, e dobrar a página a cada leva fazia
   ela reencontrar o trecho pesado e estourar o teto de bytes (34 respostas
   recusadas em 3 min na frota, e cada recusa custa o download inteiro);
3. **crescer devagar, encolher na hora**: a página cresce no máximo
   `FATOR_CRESCIMENTO`=4× por recalibração — encolher é reação a peso MEDIDO,
   crescer é aposta;
4. **recorte de sobreposição**: quando o tamanho muda, a paginação re-ancora por
   offset de ITEM e a 1ª página da nova leva repete o que já saiu. O offset é
   conhecido, então o trecho repetido é cortado — a saída volta a ser
   exatamente o dia, em ordem e sem repetir (antes isso dependia do dedupe do
   banco, o que custava INSERT);
5. **a leva anterior morre antes da próxima nascer** (`del payloads`): a
   co-residência caiu de 2×janela para 1×janela páginas;
6. **teto DURO de bytes por resposta** (`DJEN_BYTES_MAX_RESPOSTA`, 48 MB).
   Previsão erra e teto não: com a sonda em 250 itens e uma leva de 766,9 KB
   por publicação, uma requisição só trouxe 192 MB de JSON e o
   `worker_ingestion-10` bateu **1023 MiB de 1 GiB** — a um suspiro do OOM
   killer, JÁ com a calibração ligada. Agora o coletor confere o
   `Content-Length` (e, sem ele, aborta durante o download), encolhe o
   `itensPorPagina` e **relê o mesmo offset**: nada é descartado, e o número
   real vira `resposta_acima_do_teto_de_bytes` no run. No piso de itens o teto
   cede — não há como pedir menos, e dia não coletado é perda de acervo.

   Visto em produção 2 minutos depois de subir, no TJDFT 2026-07-13, com o
   número real gravado no run:

   ```json
   {"erro": "resposta_acima_do_teto_de_bytes", "teto": 33554432,
    "bytes": 33868507, "tribunal": "TJDFT", "peso_item_bytes": 338685,
    "itens_por_pagina": 100, "novo_itens_por_pagina": 79}
   ```

   33,87 MB numa resposta de 100 publicações (338 KB cada) recusados, página
   encolhida pra 79, MESMO offset relido. Nenhum item a menos. E não é só o
   TJDFT: o TJAM publica 33 KB por item, o que a `itensPorPagina=1000` também
   dá 33 MB por requisição — são exatamente as 9 falhas de OOM dele no censo.

   O teto que sai de uma recusa é **herdado** pelo resto do dia (`teto_herdado`
   em `iter_pages`): sem isso a página passaria o dia crescendo pra ser recusada
   de novo, e cada recusa custa o download inteiro.

⚠️ **Isto não é teto de coleta.** A paginação continua indo até a página voltar
incompleta; muda o tamanho do balde, não quantos baldes. Teto de página é o
pecado original deste projeto (43,6% do TJSP).

**Medido em produção** (24/08, container com o mesmo `mem_limit: 1g`, gravando
no banco): TJDFT 2026-08-14 — o dia que morria com `pgs=0`, ou seja antes de
gravar UMA página — passou do ponto de morte e seguiu com pico de **311 MB**,
30% do teto. Os `worker_ingestion` no mesmo período: 200-290 MB.

**A prova, 3 h depois de subir.** Sete dias do TJDFT que morriam antes de
gravar UMA página fecharam `success`:

| dia | pgs | novas | lidas | duração |
|---|---:|---:|---:|---:|
| 2025-08-19 | 209 | 8.158 | 11.845 | 46 min |
| 2025-08-15 | 161 | 9.272 | 12.247 | 39 min |
| 2026-08-14 | 171 | 5.767 | 13.408 | 32 min |
| 2026-08-21 | 200 | 0 | 14.651 | 41 min |
| 2025-08-13 | 202 | 8.275 | 12.479 | 44 min |
| 2026-07-09 | 215 | 6.940 | 11.718 | 43 min |
| 2026-08-06 | 155 | 8.085 | 13.282 | 31 min |
| **total** | | **46.497** | **89.630** | |

Esses mesmos sete dias acumulavam **993 runs `failed` com `pgs=0`** — a
assinatura exata do OOM, o work-horse sumindo antes da primeira gravação.

E a medição dos dois lados bate ao ITEM (regra nº 5): o 2026-08-21 fechou com
**14.651 duplicadas e zero novas**, que é exatamente o total que a sonda tinha
medido paginando a API na força bruta (14.651 publicações, 822,6 MB de texto).
Contagem própria e fonte no mesmo número, e nenhuma publicação a mais pra
coletar naquele dia.

**`DJEN_BYTES_EM_VOO` é o botão de VAZÃO, não o de pico.** Quem manda no pico é
a sonda (lê 250 publicações de uma vez) e o teto de crescimento; o orçamento
manda no tamanho da página de regime. Medido no mesmo dia: a 24 MB o pico foi
os mesmos 311 MB **e** a página do TJDFT desabou pro piso de 25 itens — 586
requisições e ~3 h por dia-tribunal, o que é perda de acervo pelo relógio. O
padrão é **64 MB**: mesmo pico, página ~2,5× maior.

**Além da paginação, dois acumuladores O(dia) foram cortados:**

* `cnjs_tocados` juntava o dia inteiro e só era usado no fim — um `set` vivo do
  começo ao fim e um `numero_cnj__in=<260 mil>` no TJSP. Agora o lote fecha a
  cada `CNJS_POR_LOTE`=5.000 (resumo + auto-enqueue) e é esquecido;
* `IngestionRun.erros` crescia com o número de itens recusados, e é
  re-serializada INTEIRA a cada `run.save()` — que acontece **uma vez por
  página**. Agora guarda as primeiras `MAX_ERROS_NO_RUN`=200 e conta o resto
  (`itens_recusados_alem_do_teto`), com o total real registrado.

**Teto virou alerta (regra nº 2).** `DJEN_RSS_ALERTA_MB` (700 MB, ~70% do
`mem_limit`): passando disso, a ingestão grava na hora, no run,
`{"erro": "memoria_acima_do_alerta", "rss_mb": …, "paginas_lidas": …}` e loga
ERRO. Antes o único sinal era o watchdog escrevendo "worker crashou" uma hora
depois, sem um número. E se a publicação for tão pesada que nem
`PISO_ITENS`=25 caibam no orçamento, isso vira
`{"erro": "orcamento_memoria_no_piso", …}` no run — a coleta segue (dia não
coletado é perda de acervo), mas ninguém descobre pelo SIGKILL.

**A escotilha `DJEN_ESTRATEGIA_UF` NÃO recebeu a calibração.** Lá a página
continua fixa em 1000 itens com 27 fatias em voo; ela só ganhou o alerta de RSS
e o de orçamento. Ligá-la num tribunal pesado traz o OOM de volta.

**O que sobra (não resolvido aqui):** os **203 deadlocks** em
`tribunals_process` (28,9% das falhas). `_process_page` já insere em ordem total
de CNJ, mas `_flush_resumo` faz `bulk_update` sem ordem definida e é o suspeito
óbvio. É a próxima maior fonte de dia queimado.

```bash
# censo do cemitério (completo, por função/exceção/dia)
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py djen_censo_falhas djen_backfill'

# quem passou do alerta de memória, com o número
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py shell -c "
from tribunals.models import IngestionRun
for r in IngestionRun.objects.filter(fonte=\"djen\", erros__contains=[{\"erro\":\"memoria_acima_do_alerta\"}])[:20]:
    print(r.tribunal.sigla, r.janela_inicio, r.erros)"'
```

## `success` não é prova — a integridade tem que ser MEDIDA (24/08/2026)

As três perdas do CLAUDE.md tinham a mesma assinatura: **run verde, log limpo,
número redondo**. Até 24/08/2026 nada no sistema comparava, sozinho, o que o dia
trouxe com o que a FONTE declara — a regra nº 5 existia no papel e era exercida
só à mão, em investigação de incidente. Comando manual é coisa que ninguém roda.

### A prova de um dia: `djen_provar_dias`

```bash
# um dia (o molde: TJDFT 2026-08-21)
manage.py djen_provar_dias --tribunal TJDFT --dia 2026-08-21

# amostra ALEATÓRIA de tamanho e semente declarados entre os dias fechados
manage.py djen_provar_dias --n 6 --seed 20260824 --horas 24 --so-recuperados
```

Ele pagina a DJEN **na força bruta** e imprime QUATRO números por dia:

| número | o que é | custo |
|---|---|---|
| `fonte paginada` | ids DISTINTOS que a API entrega até a página vir incompleta | o dia inteiro (o TJDFT 2026-08-21 são 822,6 MB) |
| `fonte count` | `count` com `itensPorPagina=1` | 1 requisição, **0,66 s medido** |
| `run` | `movimentacoes_novas + movimentacoes_duplicadas` do run que fechou o dia | grátis |
| `banco` | quantos daqueles ids existem hoje em `Movimentacao` | N/1.000 SELECTs no índice único |

`run` alto com `banco` baixo é escrita perdida; `run` baixo com `fonte` alta é
coleta cortada. Os dois morrem em silêncio sem esta conferência.

⚠️ **A paginação da prova é PRÓPRIA, de fora do `iter_pages`** — usa só o
transporte (`_fetch`: proxy, retry, circuito). Se a calibração de página do
coletor voltar a cortar mudo, a prova não repete o erro junto: ela o denuncia.
Provar com o mesmo código que coleta é o que faz um teto de 10 páginas
sobreviver 17 meses.

⚠️ **O `count` tem teto de 10.000** (regra nº 3: número redondo é PISO
disfarçado). Medido: TJDFT 2026-08-21 devolve `count=10000` e a paginação
devolve **14.651**. Acima do teto, quem decide é a paginação — ou ninguém.

### O gate de completude (`conferir_dias_fechados`, etapa 5 do watchdog)

A cada tique (5 min) o watchdog pega os dias de 1 dia que fecharam `success`
desde o tique anterior e enfileira a conferência na fila **`djen_audit`** — que
tem workers próprios e ociosos, e não disputa com a ingestão.

| caminho | quando | teto por tique | custo |
|---|---|---|---|
| barato (`count_window`) | `novas+dup` < 10.000 | 20 dias | 1 requisição/dia (0,66 s) |
| caro (paginação) | `novas+dup` >= 10.000 | 1 dia | o dia inteiro |
| abstenção | `count` >= 10.000 e sem paginação | — | nada é afirmado (regra nº 6) |

**O custo, medido antes de ligar.** A frota fecha 50-80 dias/h (medido em
24/08: 83 runs de 1 dia na hora das 14 h). 20 conferências baratas por tique são
240 requisições/h de 1 item contra as ~10 mil páginas/h que a ingestão já pede —
**2,4%**, e nenhuma delas no caminho da requisição do site. No Postgres o gate
lê 1 linha por dia conferido (índice `(fonte, tribunal, janela_inicio)`) e só
ESCREVE quando acha divergência. A marca de "já conferido" mora no Redis
(`djen:conferido:<sigla>:<dia>`, TTL 8 dias): perdê-la custa uma requisição
repetida, não uma conferência perdida.

Divergência não levanta exceção e não apaga nada: vira `{"erro":
"cobertura_divergente", "fonte": …, "run": …, "gap": …}` no `IngestionRun.erros`
**e** uma linha de log com os dois números. Quem decide o que fazer é gente.

```bash
# o que o gate já achou
ssh 100.100.144.57 'docker exec voyager-web-1 python manage.py shell -c "
from tribunals.models import IngestionRun
for r in IngestionRun.objects.filter(fonte=\"djen\",
        erros__contains=[{\"erro\":\"cobertura_divergente\"}])[:20]:
    print(r.tribunal_id, r.janela_inicio, r.erros[-1])"'
```

### "worker crashou" mentia em 6,5% dos casos

`WATCHDOG_RUN_ZOMBIE_SECONDS` marca `failed` todo run `running` há mais de 1 h,
com a inferência "o worker crashou". A inferência é falsa justamente no dia que
mais importa. **Medido em 24/08/2026**: dos **170** runs de 1 dia que fecharam
`success` em 12 h, **11 (6,5%) passaram de 60 min** e o pior — um TJSP — levou
**357 min**. Todos foram marcados "worker crashou" com o worker trabalhando, e
só voltaram a `success` ao terminar.

O estrago era duplo: o censo de falhas ficava com fantasma (parte dos 640
"worker crashou" contados no dia) e o `UPDATE` **sobrescrevia** `run.erros` —
apagando o `resposta_acima_do_teto_de_bytes` e o drift daquele dia, que é a
evidência que a regra nº 2 manda guardar.

Agora `_matar_zumbis` desempata de graça: o job_id é determinístico
(`f2:<SIGLA>:<dia>`), então quem ainda tem o dia na mão aparece no
`StartedJobRegistry`. Com job vivo, o run ganha até
`WATCHDOG_RUN_ZOMBIE_COM_JOB_S` (7 h = o `job_timeout` de 6 h + folga); sem ele,
nada muda. E o `erros` virou **append** (`jsonb || jsonb`), nunca substituição.

### As DUAS réguas da recuperação, reconciliadas com número

A tela `/dashboard/completude/` mede a "Recuperação do teto por UF" com duas
réguas (`dashboard/completude_warm.py::_recuperacao_por_tribunal`), de propósito:
razão itens/página >= 700 (o dia saiu pelo caminho flat) e run `success`
posterior ao `CORTE_FLAT` (18/08 09:48). Cruzando as duas nos **3.945 dias-alvo**
da Fase 2 (24/08/2026, 15h30):

|  | refeito pós-corte | NÃO refeito |
|---|---:|---:|
| **razão >= 700** | 3.328 | 320 |
| **razão < 700** | **141** | **156** |

* **141 dias (47,5% dos 297 "falta") são FALSO POSITIVO da razão.** Foram
  refeitos pelo caminho novo e a razão continua baixa — porque o conserto do OOM
  (24/08) tornou o `itensPorPagina` **dinâmico**: num tribunal de publicação
  pesada a página cai pra 100-300 itens por orçamento de BYTES. Exemplos do
  mesmo dia: TJGO 2026-08-24 razão 207 (62.612 itens em 302 páginas), TRF4
  2026-08-24 razão 100, TRF2 2026-08-24 razão 439 — todos coletados hoje, pelo
  caminho flat. **A régua da razão passou a medir o peso da publicação, não o
  caminho da coleta**, e por isso a tela não pode chegar a 100% com ela.
* **320 dias (razão alta, nunca refeitos)** são o outro lado: dia que já era bom
  nunca precisou ser refeito. É a subestimação conhecida da régua da data.
* **156 dias são pendência de verdade** — nunca refeitos pelo caminho novo:
  TJRS 121 · TJSP 19 · TRF3 11 · TJRJ 3 · TRF6 1 · TRF2 1.

**Por que os 156 nunca voltaram:** o seletor do
`djen_reprocessar_janelas_capped` exige `paginas_lidas >= 100` **e**
`novas+dup >= 10000`. O dia fatiado do TJRS tem 10-18 mil itens em **~20-37
páginas** (as 27 fatias de UF terminam cada uma numa página parcial), então ele
nunca foi selecionado. Medido: **187 dos 297 (63%) dias que a tela conta como
falta estão FORA do seletor da recuperação.**

E não adianta enfileirá-los de novo: conferido em 24/08, **os 156 já estão na
fila** (120 do TJRS como `adiado:<sigla>:<dia>`, o resto como `f2:`), com
posição mediana **638 de 1.964**. O que falta ali é VAZÃO, não descoberta —
enfileirar de novo só duplicaria requisição contra o CNJ.

## Comandos manuais

```bash
djen_descobrir_inicio <sigla> [--force] [--floor 2022-01-01]
    Busca binária pelo primeiro dia com count>0. Salva em data_inicio_disponivel.

djen_backfill <sigla> [--inicio YYYY-MM-DD] [--sync]
    Enfileira run_backfill (ou roda inline com --sync). Retoma de onde parou.

djen_run_now <sigla> [--dias N] [--inicio ...] [--fim ...]
    Roda ingest_window inline (sem fila, sem checagem de backfill).
    Útil pra debug.

djen_status
    Snapshot CLI: tribunais, último run de cada, drift alerts abertos, status do pool.
```

## Materialized View `mv_pipeline_diario`

Criada na migration `0029`. Agrega contagens de Process por tribunal, dia e fonte de enriquecimento.

```sql
-- formato LONG: uma linha por (tribunal_id, dia, fonte)
SELECT tribunal_id, data_enriquecimento_datajud::date AS dia,
       'datajud'::text AS fonte, COUNT(*)::int AS processos
  FROM tribunals_process
 WHERE data_enriquecimento_datajud IS NOT NULL GROUP BY 1,2
UNION ALL
SELECT tribunal_id, enriquecido_em::date, 'pje', COUNT(*)::int
  FROM tribunals_process WHERE enriquecido_em IS NOT NULL GROUP BY 1,2
UNION ALL
SELECT tribunal_id, classificacao_em::date, 'classif', COUNT(*)::int
  FROM tribunals_process WHERE classificacao_em IS NOT NULL GROUP BY 1,2;
```

Colunas: `tribunal_id` (sigla), `dia` (date), `fonte` (text: `'datajud'` | `'pje'` | `'classif'`), `processos` (int).
Índice único em `(tribunal_id, dia, fonte)` — permite `REFRESH CONCURRENTLY`.

**Nota:** DJEN **não está** na MV. É lido live de `IngestionRun` com
`MAX(janela_fim)` por tribunal/dia para não duplicar overlap de janelas
(dois runs com janela sobreposta contam o mesmo dia duas vezes se somados).

### Cobertura e `_dia_coberto`

`_dia_coberto` e `_dias_cobertos` (`djen/jobs.py`) determinam se um dia já foi
processado — usado pelo backfill pra pular janelas feitas e pelo watchdog de
re-enfileiramento.

**Regra de horizonte recente (bug latente fechado):** dias dentro de
`hoje − overlap_dias` só contam cobertos se o `success` teve dados reais
(`novas | duplicadas | paginas > 0`) — **mas essa exigência de dados se aplica
apenas a dias úteis** (`weekday() < 5`). Exemplos:

| Situação | Resultado |
|---|---|
| Dia útil recente, `success` com `novas=0` (vazio) | **não coberto** — backfill re-tentará |
| Sábado ou domingo recente, `success` vazio | **coberto** — DJEN não publica fim de semana; sem retry desnecessário |
| Qualquer dia fora do horizonte, `success` (qualquer) | **coberto** — backfill histórico de feriado/recesso intacto |

Isso fecha o bug latente onde um `success` vazio sobre-escrevia dias recentes
como cobertos, silenciando lacunas de ingestão que deveriam ser vistas no
heatmap de saúde como vermelho, sem gerar falsos retries em fins de semana.

**Ressalva — feriado forense:** não há calendário de feriados forenses (Corpus
Christi, feriados estaduais, recesso). Um feriado recente com `success` vazio é
tratado como dia útil perdido → o fan-out diário e o `tick_backfill_retroativo`
o re-tentarão repetidamente até ele envelhecer fora do horizonte. Custo baixo
(proxy/worker), não é outage; consistente com a nota "Falso-vermelho em feriado
forense" do `DASHBOARD.md`.

### Refresh

| Job | Schedule | Como |
|---|---|---|
| `refresh_materialized_views` | cron 03:00 diário | `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_pipeline_diario` |
| `warm_pipeline_diario` | a cada 1h (inline no scheduler) | re-aquece cache da MV após VACUUM |

Ambos rodam inline no `ThreadPoolExecutor(20)` do scheduler (`.32`) — sem fila RQ. Ver ADR-017.

## Rate limiting / volume

A DJEN aceita **paginação ilimitada** mas tem WAF. Observado:
- Datacenter proxies (ProxyScrape) bloqueados pelo WAF em ~**100%** dos IPs
  (medido 2026-06-17: 0/29 IPs do pool, 28× HTTP 403 + 1× 500). A ingestão DJEN
  é, na prática, **Cortex-only** — o pool **não** serve de fallback (devolve 403).
- Cortex (residencial fixo) sempre aceito → **SPOF**: se o gateway Cortex cair, a
  ingestão DJEN para (o pool não cobre). Ele **flapa** (caiu 100% por ~15min em
  2026-06-17 e voltou). Num incidente de ingestão, priorize a saúde do Cortex —
  o pool não resolve. (e-SAJ TJSP/TJAL é o caso oposto: responde pelo pool. Ver
  `.ia/DECISIONS.md` ADR-006/ADR-021.)
- 504 Gateway Timeout aparece em ondas — backoff longo + esperar é a única coisa a fazer

Volume típico:
- ~3.000-3.500 movs/min em ritmo cruzeiro (com Cortex + alguns proxies bons)
- ~10.000 movs por chunk de 30d em TRFs medianos
- Backfill TRF1 completo (5 anos): ~6-8h
- Backfill TJSP completo: estimado 3-5 dias (volume ~5x maior)

### ⚠️ Teto de concorrência contra a API do CNJ

O que a DJEN enxerga é o **produto** `réplicas de worker_ingestion ×
DJEN_PAGINAS_PARALELAS`, não cada fator isolado. Medido em 19/08/2026:

| réplicas | páginas | concorrentes | resultado |
|---:|---:|---:|---|
| 8 | 8 | 64 | ✅ funcionava |
| 14 | 8 | **112** | 🔴 5xx em massa → circuit-breaker abriu, **648 dias derrubados em 25 min** |
| 14 | 4 | 56 | ✅ |

**Regra: mantenha o produto em 64 ou menos.** Ao escalar `worker_ingestion`,
ajuste `DJEN_PAGINAS_PARALELAS` na mesma proporção.

Não há perda de dado quando isso acontece — o circuit-breaker é a proteção
funcionando, os runs viram `failed` e voltam pra fila. Mas é trabalho jogado
fora, e é martelar um serviço público.

**Sintoma:** `IngestionRun.erros` com `DJEN circuito aberto (sobrecarregado)`.
**Cura:** baixar o produto, `cache.delete('djen:circuit_open')` +
`cache.delete('djen:5xx_recent')`, e reenfileirar os dias derrubados.
