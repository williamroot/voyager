# Enriquecimento via consulta pública

DJEN dá só metadata da movimentação (texto, tipo, órgão). Pra **partes** (autores, réus, advogados com OAB) e classe/assunto/valor estruturados, precisamos consultar o sistema do tribunal direto.

## Estado atual

| Tribunal | Sistema | Implementado | Notas |
|---|---|---|---|
| TRF1 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf1.py` (subclasse) |
| TRF3 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf3.py` (subclasse) |
| TRF5 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf5.py` (subclasse) — path `/pjeconsulta/` |
| TJMA | PJe consulta pública (sem login) | **Sim** | `enrichers/tjma.py` (subclasse) — `pje.tjma.jus.br/pje/...`, sem WAF; script de pesquisa é `executarPesquisa` (sem `ReCaptcha`) — capturado via fallback `A4J.AJAX.Submit` na base; CPF/CNPJ sem máscara |
| TRF2 | E-PROC | **Não (só DJEN+Datajud ativos)** | Subdomain público `eproc-consulta.trf2.jus.br` existe mas exige captcha (`#divInfraCaptcha`) e tem IDs randomizados por sessão. Sistema interno tem login + 2FA. Parser autenticado de referência em `~/projetos/JURISCOPE/falcon/datamodel/processors/trf2.py` (965 linhas). |
| TRF4 | E-PROC | **Não (só DJEN+Datajud ativos desde 2026-05-24)** | Mesmo cenário do TRF2. |
| TRF6 | E-PROC | **Não (só DJEN+Datajud ativos desde 2026-05-24)** | Mesmo cenário do TRF2. |
| TJSP | e-SAJ | **Sim** (2026-05-24) | `enrichers/esaj.py::TjspEnricher` (subclasse de `BaseEsajEnricher`, não herda BasePjeEnricher). HTTP puro (sem Selenium): `open.do` → `search.do?NUMPROC` (302) → `show.do` → parse. Selectors portados de `ESAJSPProcessDataProcessor` do JURISCOPE. Limitação: e-SAJ público mascara CPF/CNPJ, então `documento` fica vazio (OAB e nome são preservados). |
| TJAL | e-SAJ | **Sim / ativo** (2026-05-30; ativado 2026-05-31) | `enrichers/esaj.py::TjalEnricher` (subclasse de `BaseEsajEnricher`). Mesmo software/fluxo do TJSP, host `www2.tjal.jus.br`. Teste e2e em `tests/test_enricher_tjal.py`. **Roteia pelo pool ProxyScrape** (`PREFER_CORTEX=False` desde 2026-06-17 — o pool responde ~37% dos IPs; antes era Cortex-only por premissa equivocada). `worker_tjal` em 24 réplicas. Ver `.ia/DECISIONS.md` ADR-021. |
| TJMG | PJe consulta pública (sem login) | **Sim** | `enrichers/tjmg.py` (subclasse) — `pje.tjmg.jus.br/pje/...` |
| TJDFT | PJe SPA Angular + REST API (sem login) | **Sim** (2026-05-26) | `enrichers/tjdft.py` (classe própria, não herda BasePjeEnricher). API REST Spring Boot em `pje-consultapublica-api.tjdft.jus.br/v1/`. CPF/CNPJ sem máscara. Limitação: rota `/dados` não expõe `valor_causa`. |
| TJCE | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjce.py` (subclasse) — host `pje-consulta.tjce.jus.br`, path `/pje1grau/`. reCaptcha `if(false)`. |
| TJAP | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjap.py` (subclasse) — `pje.tjap.jus.br`, path `/1g/`. |
| TJPE | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjpe.py` (subclasse) — `pje.cloud.tjpe.jus.br`, path `/1g/`. |
| TJRJ | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjrj.py` (subclasse) — `tjrj.pje.jus.br`, path `/pje/`. |
| TJRO | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjro.py` (subclasse) — `pjepg-consulta.tjro.jus.br`, path `/consulta/`. Atenção: host pode devolver 403 a IPs datacenter — validar pelo pool/Cortex em prod. |
| TJAC | e-SAJ clássico (sem login) | **Sim** (2026-06-29) | `enrichers/esaj.py::TjacEnricher` (subclasse) — `esaj.tjac.jus.br`, 2º grau `cposg5`. |
| TJMT | PJe SPA Angular + REST (sem login) | **Sim** (2026-06-29) | `enrichers/tjmt.py` (classe própria). API gateway `hellsgate.tjmt.jus.br/consultaprocessual/ProcessosJudiciais/v2?numeroUnico=`. Exige header `X-Fingerprint` = base64(HMAC-SHA256("UA-resolução-lang-timestamp_ms", chave `A_mesma_mao_que_aplaude_e_a_que_vaia!`)) gerado fresco por request. Sem login/captcha. OAB sem UF (assume MT). |
| TJPA | Portal próprio "Consulta Unificada" (SPA + REST) | **Sim** (2026-06-29) | `enrichers/tjpa.py` (classe própria). `GET consulta-processual-unificada-prd.tjpa.jus.br/consilium-rest/processobycnj/{cnj}` (UA de browser, throttle p/ 429). reCAPTCHA só no front, não enforced. `cpfcnpj` vazio na consulta pública (tipo pf/pj por `tppessoa`). |
| **Bloqueados (recon 2026-06-29)** | vários | **Não — captcha/login/anti-bot** | Consulta pública gated, inviável headless sem captcha-solver ou credenciais: **captcha** (hCaptcha/reCaptcha/Tencent) — TJBA, TJPB, TJRR, TJSE, TJMS (e-SAJ virou SPA Next.js c/ captcha), TJGO+TJPR (PROJUDI), TJAM (PROJUDI atrás de F5 anti-bot); **login obrigatório** — TJES, TJPI, TJTO; **indeterminado** (sem amostra/host estável) — TJRN. eproc (TRF2/4/6, TJRS, TJSC) segue exigindo login+2FA (ver linhas TRF acima). Desbloqueio exige decisão: serviço de captcha-solving (2captcha etc.) ou credenciais/OTP. Veredictos completos do recon no histórico do commit. |

## Arquitetura

`enrichers/pje.py::BasePjeEnricher` concentra **toda** a lógica de PJe consulta pública (form JSF, parsing do detalhe, polos, partes). Subclasses configuram só:

```python
class Trf1Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g-consultapublica.trf1.jus.br'
    LIST_URL = f'{BASE_URL}/consultapublica/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF1'
    LOG_NAME = 'voyager.enrichers.trf1'

class Trf3Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g.trf3.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF3'
    LOG_NAME = 'voyager.enrichers.trf3'
```

## Fluxo (`BasePjeEnricher.enriquecer`)

```
GET LIST_URL ────────────► HTML inicial
   │                         │
   │                         ├─ extrai javax.faces.ViewState
   │                         ├─ todos <input>/<select> do form fPP
   │                         └─ encontra script id dinâmico (executarPesquisaReCaptcha)
   │
POST LIST_URL ───────────► resposta AJAX
   {form_fields, CNJ}        │
                             ├─ regex DETALHE_PATH/[^"']+  (path varia: trf1=/consultapublica/, trf3=/pje/)
                             └─ ou idProcessoTrf:NNN → constrói URL fallback
   │
GET detalhe ──────────────► HTML completo
                             │
                             ├─ div.propertyView .name>label + .value
                             │   → classe, assunto, autuação, valor
                             ├─ <b>Órgão Julgador</b><br/>NOME → orgao_julgador
                             └─ div#poloAtivo / div#poloPassivo / div#outrosInteressados
                                 → tabelas com partes
   │
WITH transaction.atomic:
  Process.objects.select_for_update().get(pk=)    ◀── serializa workers concorrentes
  _aplicar_dados(processo, dados)                       no mesmo Process
  _aplicar_partes(processo, partes)
  processo.enriquecimento_status = OK
  processo.save(update_fields=[...])
```

**Particularidades PJe:**
- O botão `fPP:searchProcessos` é só trigger visual — o **script real** com `executarPesquisaReCaptcha` tem id `fPP:j_idXXX` dinâmico. `_find_search_script_id` localiza.
- hCaptcha presente no JS mas com flag `if (false)` — desabilitado.
- jsessionid é mantido pelo `requests.Session` (cookie automático).
- Path varia por TRF: TRF1 usa `/consultapublica/`, TRF3 usa `/pje/`. `DETALHE_PATH` parametriza.

## Documentos mascarados

TRF3 PJe consulta pública mascara CPF/CNPJ por privacidade:
```
TRF1: GRACILENE ROSA LIMA - CPF: 123.456.789-00     ← real
TRF3: GRACILENE ROSA LIMA   639.XXX.XXX-XX          ← mascarado
```

`enrichers/parsers.py`:
- `CPF_RE` / `CNPJ_RE` aceitam `[\dX*]` em posições privadas
- `parse_documento(text)` devolve (string, tipo) — preserva máscara
- `is_documento_mascarado(doc)` — testa por X/* na string
- `real_casa_com_mascara(real, mascara)` — testa compatibilidade posição-a-posição (`29.979.036/0001-40` casa com `29.9XX.XXX/XXXX-XX`)

## Dedupe de partes (`_upsert_parte`)

3 caminhos em ordem de confiança:

1. **OAB** (advogados) — chave estável, precedência total.
2. **Documento real** — PK natural global (CPF/CNPJ unique constraint).
3. **Documento mascarado**:
   - Antes de criar Parte mascarada, busca Parte com mesmo nome e doc REAL que case com a máscara → reusa (TRF1 viu CNPJ completo, TRF3 vê mascarado, é a MESMA PJ).
   - Senão, dedupe por `(nome, documento)` — homônimos com máscaras distintas ficam separados.
4. **Sem doc nem OAB**: `get_or_create((nome, tipo))` — evita explosão de "Procuradoria Regional Federal" replicada em N processos.

Constraints partial em `Parte.Meta`:
```python
UniqueConstraint(documento) WHERE doc != '' AND doc NOT LIKE '%X%' AND NOT LIKE '%*%'
   → uniq_parte_documento_real
UniqueConstraint(nome, documento) WHERE doc LIKE '%X%' OR LIKE '%*%'
   → uniq_parte_documento_mascarado
UniqueConstraint(oab) WHERE oab != ''
```

### Armadilha: CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS

Os 3 índices únicos parciais de `Parte` ficaram **inválidos** em 2026
(migration 0017): `CREATE UNIQUE INDEX CONCURRENTLY` falha na validação se
a tabela já tem duplicatas, deixando o índice `indisvalid=false`; o
`IF NOT EXISTS` fez re-execuções pularem o husk morto. Índice inválido não
enforça unicidade — o `bulk_create(ignore_conflicts)` do drainer parou de
deduplicar e a tabela inflou de ~4M pra ~84M linhas.

Corrigido pelo command `dedup_partes` (colapso por chave exata: oab /
documento real / `(nome,documento)` mascarado — anti-homônimo — mais
absorção masked→real com trava de candidato único) seguido da migration
`0030_recriar_indices_unicos_parte`, que **dropa** o husk e **verifica
`indisvalid`** após recriar. Monitorar com `manage.py check_parte_indexes`
(exit 1 se algum índice único estiver inválido).

## Catálogo de classes/assuntos

`tribunals.ClasseJudicial` e `tribunals.Assunto` (PK = código TPU/CNJ). FKs em `Process.classe`, `Process.assunto`, `Movimentacao.classe`. Habilita filtros de dropdown sem `DISTINCT` em milhões de linhas e resolve discrepância de capitalização entre PJe (UPPERCASE) e DJEN (CamelCase quebrado).

`_upsert_catalogo` é race-safe via `bulk_create(ignore_conflicts=True) + get(codigo=)` — não levanta `IntegrityError` quando 2 workers veem o mesmo código pela 1ª vez.

## Filas per-tribunal

```python
RQ_QUEUES = {
    'default':         ...,       # fallback
    'enrich_trf1':     ...,       # 4 workers dedicados
    'enrich_trf3':     ...,       # 4 workers dedicados
    'djen_ingestion':  ...,
    'djen_backfill':   ...,
}
```

Helper `enrichers.jobs.enqueue_enriquecimento(pid, sigla)` roteia pra `enrich_<sigla.lower()>`. Auto-enqueue em `djen/ingestion.py` ao bulk_create de novos `Process` usa esse helper. Filas separadas evitam que TRF3 (volume alto, mais lento) sufoque TRF1.

## Comandos

```bash
# Foreground (debug, roda inline)
docker compose exec web python manage.py enriquecer_processo <CNJ_ou_ID>

# Async (vai pra fila do tribunal)
docker compose exec web python manage.py enriquecer_processo <CNJ> --async

# Bulk: todos pendentes do TRF3
docker compose exec web python manage.py enriquecer_pendentes --tribunal TRF3 --limit 0

# Reprocessar erros (proxy ruim, rate limit) do TRF1
docker compose exec web python manage.py enriquecer_pendentes --tribunal TRF1 --status erro --limit 0

# Funde Partes mascaradas em reais (one-shot, idempotente)
docker compose exec web python manage.py consolidar_partes_mascaradas --dry-run --limit 100
docker compose exec web python manage.py consolidar_partes_mascaradas

# Via dashboard: botão "↻ Atualizar dados públicos" no detalhe do processo
```

## PJe novo (SPA + REST) — caso TJDFT

Alguns tribunais já migraram do PJe clássico (JSF/Seam, form `fPP`,
`javax.faces.ViewState`) pra uma SPA Angular consumindo REST API Spring
Boot. **`BasePjeEnricher` não funciona nesse caso** — não há HTML form
pra parsear. Detecção: GET na URL `listView.seam` redireciona pra um
domínio `pje-consultapublica*.tjxxx.jus.br` cujo body é um shell Angular
(`<title>Consulta pública · Processo Judicial Eletrônico</title>`,
bundles `main-*.js`).

Padrão das rotas (caso TJDFT, validado 2026-05-26):

```
GET /v1/processos?page=0&numeroProcesso=<CNJ_FORMATADO>
    → result[0].idProcesso (token opaco URL-safe ~70 chars)

GET /v1/processos/{idProcesso}/dados
    → { classeJudicial, assunto (hierárquico), orgaoJulgador,
        dataDistribuicao (ISO8601), jurisdicao, endereco, ... }

GET /v1/processos/{idProcesso}/poloAtivo?page=N
GET /v1/processos/{idProcesso}/poloPassivo?page=N
GET /v1/processos/{idProcesso}/outrosInteressados?page=N
    → result: [ { participante (texto), nome, tipo, procuradoria, ... } ]
      pageInfo: { current, last, size, count }
```

Wrapper de resposta é uniforme:
`{ "status":"ok", "code":"200", "messages":[...], "result":..., "pageInfo":? }`.
Quando `status != "ok"`, levante erro — sem fallback.

Implementação fica em `enrichers/tjdft.py` (referência). Pontos-chave:
- Headers `Referer` + `Origin` apontando pra SPA oficial — sem isso o ALB
  pode devolver 403 em alguns endpoints.
- `participante` é o texto cru `"NOME - OAB UF<num> - CPF: ... (TIPO)"`.
  Reutilize `parse_documento` / `parse_oab` dos parsers compartilhados.
- O `tipo` da API (AUTOR/REU/ADVOGADO/FISCAL DA LEI/INTERESSADO/...)
  determina se a entrada é principal ou advogado. Agrupe advogados
  subsequentes como `representantes` do principal anterior — mesmo
  contrato que `BasePjeEnricher._parse_polo`.
- Assunto vem hierárquico (`"RAIZ (cod) - FILHO (cod) - ... - FOLHA (codFolha)"`);
  pegar **só o último segmento** pra entrar no catálogo `Assunto`.
- `dataDistribuicao` é ISO; converter pra `DD/MM/YYYY` antes de publicar
  (drainer usa `parse_data_br`).
- `valor_causa` **não** vem na API pública do TJDFT (limitação aceita).

Como mapear endpoints de um TJ novo nesse formato:
1. Abra a SPA, faça uma busca real (Playwright ou DevTools).
2. Capture os XHRs com `browser_network_requests` filtrando pelo
   subdomínio `*-api.*`.
3. Compare os campos do JSON com a tabela do `Process` (classe,
   assunto, orgao_julgador, data_autuacao) — costuma ser 1:1 com o caso
   TJDFT mas mudam nomes de campos.

## Como adicionar enricher pra outro PJe

1. Criar `enrichers/<sigla>.py` (15 linhas):
   ```python
   from .pje import BasePjeEnricher
   class TrfNEnricher(BasePjeEnricher):
       BASE_URL = '...'
       LIST_URL = f'{BASE_URL}/...'
       DETALHE_PATH = '/...'
       TRIBUNAL_SIGLA = 'TRFN'
       LOG_NAME = 'voyager.enrichers.trfN'
   ```
2. Adicionar em `enrichers/jobs.py::_ENRICHERS`.
3. Adicionar em `djen/ingestion.py::TRIBUNAIS_COM_ENRICHER` pra ativar auto-enqueue.
4. Adicionar fila `enrich_trfN` em `core/settings.py::RQ_QUEUES`.
5. Adicionar serviço `worker_trfN` em `docker-compose-prod.yml` com `replicas: 4`.
6. Restart scheduler pra registrar daily cron.

## Como adicionar enricher pra outro e-SAJ (TJSP, TJAL, ...)

e-SAJ é idêntico entre tribunais — só muda o host. `BaseEsajEnricher`
(`enrichers/esaj.py`) tem toda a lógica; o split do CNJ é por segmento
(independente de tribunal). Caso de referência: **TJAL** (2026-05-30).

1. Subclasse em `enrichers/esaj.py` (3 linhas):
   ```python
   class TjalEnricher(BaseEsajEnricher):
       BASE_URL = 'https://www2.tjal.jus.br'   # host do e-SAJ do TJ
       TRIBUNAL_SIGLA = 'TJAL'
       LOG_NAME = 'voyager.enrichers.tjal'
   ```
2. `enrichers/jobs.py::_ENRICHERS` + import.
3. `djen/ingestion.py::TRIBUNAIS_COM_ENRICHER` (auto-enqueue). Dimensione a fila
   antes pra tribunal de volume alto — TJSP entrou aqui em 2026-05-30 já com
   `worker_tjsp` em 60 réplicas (auto-enqueue só pega Process novos por janela
   diária; o backlog drena via `reabastecer_filas_enriquecimento`, capado em
   `QUEUE_HIGH_WATER`).
4. Fila `enrich_<sigla>` em `core/settings.py::RQ_QUEUES`.
5. Serviço `worker_<sigla>` em `docker-compose-workers.yml` (ramp 10 réplicas).
6. Seed migration `update_or_create` o `Tribunal` com `ativo=False`.
7. Ativação em prod: `djen_descobrir_inicio <sigla>` → flip `ativo=True` →
   backfill (ver .ia/OPS.md). Botão de enrich manual: condição no template
   `dashboard/templates/dashboard/processo_detail.html`.

Limitação herdada do TJSP: e-SAJ público mascara CPF/CNPJ → `documento` vazio
(OAB e nome preservados).

> **Gotcha `tipo` vs `papel` (corrigido 2026-06-10):** a tabela e-SAJ
> `#tablePartesPrincipais` traz o **papel** processual (Exeqte/Reqdo/Agravante/
> Apelado/...). Esse valor vai pra `ProcessoParte.papel` (uppercased) — **nunca**
> pra `Parte.tipo`. `Parte.tipo` é a categoria canônica
> (`pf`/`pj`/`advogado`/`desconhecido`), derivada de doc/oab via
> `classificar_tipo_parte`. Bug original: `esaj._extrair_partes` gravava o papel
> cru em `tipo`, poluindo o donut "Distribuição por tipo" da `/dashboard/partes/`
> com centenas de papéis e fragmentando Partes sem-doc por papel (o lookup de
> dedupe usa `tipo` na chave). Como o e-SAJ mascara doc, quase toda pessoa vira
> `desconhecido` (sem doc não dá pra distinguir pf/pj) — correto. Limpeza dos
> dados históricos: `manage.py recategorizar_tipo_partes` (ver .ia/OPS.md).

### 1º vs 2º grau (cpopg / cposg)

`BaseEsajEnricher` roteia por grau automaticamente: **foro de origem `OOOO == '0000'`
⇒ 2º grau** (processo originário do tribunal — agravos, recursos na Presidência,
competência originária). 1º grau usa `/cpopg/`; 2º grau usa `/{CPOSG_PATH}/`
(**TJSP = `cposg`**, **TJAL = `cposg5`** — varia por tribunal, override na subclasse).

Sem isso, todo processo de 2º grau caía em falso "não encontrado" (o cpopg só
tem 1º grau). Os dois grais são e-SAJ clássico (mesmo HTML); diferem só em:
- search param do CNJ: 1g `dadosConsulta.valorConsultaNuUnificado`, 2g `dePesquisaNuUnificado`;
- selectors do detalhe: 1g `foroProcesso`/`varaProcesso`/`dataHoraDistribuicao`/`valorAcao`;
  2g `secaoProcesso`/`orgaoJulgadorProcesso`/`relatorProcesso` (sem data/valor).
Partes (`#tablePartesPrincipais`) são idênticas nos dois.

## Stream sharded (drainer × N)

### Por que shard

O drainer original era **single-replica** porque múltiplas instâncias deadlocavam:
o XREADGROUP do Redis distribui entries aleatoriamente entre consumers, e dois
drainers podiam pegar events do **mesmo `process_id`** (uma re-publicação após retry,
por exemplo) e competir em `DELETE FROM tribunals_processoparte WHERE processo_id=…`
seguido de `INSERT`. Resultado: PG deadlock detector mata um dos lados.

A consequência operacional era throughput hard-cap em ~1k entries/min — pra cada
~100k events publicados, o drainer ficava 1.5h atrás. Sob carga pesada (re-fix do
backfill TRF3, deploy do TJMG) a lag chegou a 460k entries.

### Como funciona

Cada `process_id` é hashado (`process_id % STREAM_PARTITIONS`) pra escolher uma
das N partições. Workers publicam direto na partição certa via
`stream.publish(payload)` — a função olha `payload['process_id']` e escolhe o
stream físico:

```
voyager:enrichment:results:0
voyager:enrichment:results:1
voyager:enrichment:results:2
voyager:enrichment:results:3
```

Cada drainer roda com `--partition I` e consome **apenas** seu stream físico.
**O mesmo `process_id` SEMPRE cai no mesmo drainer**, então as serializações de
`DELETE+INSERT` por proc continuam sequenciais (sem deadlock entre drainers).
Entre `process_id`s diferentes, os 4 drainers paralelizam.

`STREAM_PARTITIONS=4` (vide `enrichers/stream.py`). Mudar este valor exige
quiescer o pipeline (parar workers + drenar streams existentes) — senão events
publicados sob N antigo ficam órfãos em partições que ninguém lê.

### Stream legado

`voyager:enrichment:results` (sem suffix) é o **stream legado** — usado antes
do shard. O serviço `enrichment_drainer` (sem suffix) continua processando-o
até que `XLEN` chegue a zero, momento em que pode ser desligado:

```bash
ssh ubuntu@192.168.30.100 redis-cli XLEN voyager:enrichment:results
# quando = 0:
ssh ubuntu@192.168.30.103 docker compose -f docker-compose-prod.yml stop enrichment_drainer
```

### Operação

```bash
# Ver lag por partição
for p in 0 1 2 3; do
  redis-cli -h 192.168.30.100 XLEN voyager:enrichment:results:$p
done

# Stats do consumer group de uma partição
redis-cli -h 192.168.30.100 XINFO GROUPS voyager:enrichment:results:0
```

### Capacity model

Drainer único → 1k entries/min. Com 4 partições + drainer dedicado por shard,
throughput nominal = 4k/min. Limite real é PG write-throughput em
`tribunals_processoparte` (DELETE+INSERT em batch + UPSERT em catálogos
`Parte`/`ClasseJudicial`/`Assunto`).

Sob 4× a carga, observar `pg_stat_activity` filtrado por
`wait_event_type='Lock'` — se contention crescer, considerar:
1. Aumentar `STREAM_PARTITIONS` (hot redeploy: drenar + reconfigurar).
2. Trocar `wipe + reinsert` de partes por `INSERT … ON CONFLICT DO UPDATE`.
3. Particionar `tribunals_processoparte` por hash(processo_id).

### Rollback emergencial (modo `--partition all`)

Se algum shard tiver bug grave em produção (ex: `apply_event` falhando só
pra partição N), pode-se voltar imediatamente pra topologia 1-drainer
sem refactor:

```bash
# Para 4 dos 5 services sharded
docker compose -f docker-compose-prod.yml stop \
  enrichment_drainer_p0 enrichment_drainer_p1 \
  enrichment_drainer_p2 enrichment_drainer_p3

# Reconfigura o legacy pra processar TUDO (round-robin entre legado + 4 shards)
docker compose -f docker-compose-prod.yml \
  run -d --name voyager-enrichment_drainer-rollback \
  enrichment_drainer python manage.py enrichers_drain --partition all \
  --batch-size 1000 --block-ms 500
```

Modo `all` faz round-robin entre todos os streams num único drainer.
Reintroduz a possibilidade de deadlock (mesmo problema do drainer
pré-shard) — usar **só pra rescue de curto prazo** enquanto se diagnostica
o bug do shard. Tempo: ~2min pra ativar. Sem perda de dados.

### Monitoramento (TODO follow-up)

- **Alerta por partition**: cron a cada 5min checa `XLEN voyager:enrichment:results:I`
  e se `> 5_000` por 3 ciclos consecutivos, envia alerta. Sem isso, lag
  numa partição é invisível pro usuário do dashboard.
- **Auto-stop legacy**: cron checa se `XLEN voyager:enrichment:results == 0`
  por 30min consecutivos, então `docker stop voyager-enrichment_drainer-1`.
  Manual hoje — risco de zombie consumindo entries antigas indefinidamente.

### Out-of-order safety

`apply_batch` (drainer.py:680) tem guard: se `proc.enriquecido_em >=
event.scraped_at`, o event é descartado (contado em `skipped`). Isso protege
contra:
- Re-publicação tardia do legacy stream sobrescrevendo dados frescos
  do shard
- Click manual ("Atualizar dados públicos") chegando antes de um batch
  agendado mais antigo

Dentro do mesmo batch, dedupe por `process_id` mantém o event de
`scraped_at` mais recente (drainer.py:662).
