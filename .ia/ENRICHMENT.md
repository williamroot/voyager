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
| TJMG | PJe consulta pública (sem login) | **Sim** | `enrichers/tjmg.py` (subclasse) — `pje-consulta-publica.tjmg.jus.br/pje/...`. **Sem `valor_causa`** — a fonte não publica (ver §"Valor da causa"). |
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

## Roteamento de proxy: Cortex quando datacenter morre (2026-07-06)
O pool datacenter (ProxyScrape) degrada em ondas: WAF dos tribunais bloqueia os IPs
(403) e a API do proxyscrape rate-limita a conta (429 → não repõe a lista). Isso
trava backfill DJEN + enriquecimento. Roteamento (config por env):
- **DJEN** (`djen/client.py`): pool degradado → `DJEN_CORTEX_RATIO_DEGRADED` (default
  **1.0** = 100% Cortex; era 0.9 hardcoded). Normal: `DJEN_CORTEX_RATIO` (0.5).
- **Enrichment** (`enrichers/jobs.py::enriquecer_processo`): `prefer_cortex` resolve de
  `ENRICH_PREFER_CORTEX` (default **True** = Cortex-first em TODOS os enqueues, inclusive
  o reabastecer em massa). Residencial (Cortex) passa o WAF; datacenter vira fallback.
- Cortex tem cooldown próprio (`cortex_is_bad`) se rate-limitar. O 429 do proxyscrape
  costuma resetar sozinho (janela da conta).

## Correções e-SAJ / classe (2026-07-06)

Dois bugs que faziam leads de precatório do TJSP sumirem — achados a partir de um
Cumprimento contra Fazenda com "expedição de precatório" marcado `nao_encontrado`+NAO_LEAD:

1. **`nao_encontrado` de falha transitória** (`enrichers/esaj.py`, `8bfd9f7`): o e-SAJ
   às vezes devolve **HTTP 200 com página que não é detalhe nem not-found** (soft-error/
   throttle). O código tratava qualquer 200 sem campos do detalhe como `nao_encontrado`
   **terminal** (nunca re-tenta) → **3,25M TJSP presos** como falso-negativo. Agora:
   detalhe = `classeProcesso`/redirect; not-found = **só** com marcador "Não existem
   informações"; **200 ambíguo → rotaciona/`erro` (re-tentável)**. Recuperação dos presos:
   tick agendado `enrichers.jobs.tick_reenrich_esaj_legacy` (scheduler, id `reenrich_esaj_legacy`,
   cada 5min) devolve `nao_encontrado` com `enriquecido_em < 2026-07-06` a `pendente`,
   **auto-limitante** (só quando `pendente < 20k`, 50k/tribunal/tick) — sobrevive a restart,
   sem loop (re-enriquecidos pós-fix ganham timestamp recente e saem do alvo). Cobre TJSP/TJAL/TJAC.
2. **Blanking de classe no drainer** (`enrichers/drainer.py::normalize_dados`, `bcf809e`):
   gravava `classe_codigo=''` quando o e-SAJ traz a classe só com **nome** (sem código TPU),
   apagando o código herdado do DJEN → F1=0 → lead revertia a NAO_LEAD **a cada
   re-enriquecimento** (afetava 87% dos TJSP enriquecidos). Agora só sobrescreve o código
   se vier código; senão atualiza só o nome e **preserva** o código. Idem `assunto`.

Fonte de classe pra e-SAJ (nome-only) = **backfill DJEN** (`preencher_classe_via_djen`,
mov mais recente). Ver `.ia/CLASSIFICACAO.md` §2 (regra de sinal TJSP + esses fixes).

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

## Valor da causa — quem publica e quem não (probe 2026-08-13)

`Process.valor_causa` **só se preenche onde a fonte manda o campo**. Medição
por sistema (amostra dos 2.000 `ok` mais recentes por tribunal, em prod):

| Sistema | Tribunais | Campo na fonte | Cobertura medida |
|---|---|---|---|
| Portal REST próprio | TJPA | `valorCausaFormatado` | 2000/2000 (17% são `0,00`) |
| SPA + REST | TJMT | `valorCausa` | 1903/2000 |
| e-SAJ | TJSP, TJAL, TJAC | `#valorAcaoProcesso` | TJSP 991/2000 · TJAL 1557/2000 |
| **PJe consulta pública** | **TJMG, TRF1, TRF3, TRF5, TJMA, TJCE, TJAP, TJPE, TJRJ, TJRO** | **não existe** | **0/2000 (TJMG e TRF3 conferidos)** |
| PJe SPA/REST | TJDFT | não existe em `/dados` | 0 |

**O PJe consulta pública não publica valor da causa — em nenhum tribunal.**
Conferido no HTML cru do detalhe de 6 processos TJMG (JEC, comum, inventário e
3 execuções fiscais) + 2 do TRF3: a string `valor` **não aparece nenhuma vez**
na página. O `propertyView` do PJe entrega só Número, Data da Distribuição,
Classe Judicial, Assunto, Jurisdição, Órgão Julgador (e às vezes Processo
referência). O ramo `'valor' in chave and 'causa' in chave` de
`BasePjeEnricher._extrair_dados` é, na prática, **código morto** — fica pra
quando algum PJe passar a expor.

Não adianta trocar de fonte pra MG:
- **Datajud** (`api_publica_tjmg`): `valorCausa` **não existe no schema** —
  0/20 docs aleatórios e 0/9 dirigidos (exec. fiscal, cumprimento, comum). O
  `_meta_updates_from_source` de `datajud/ingestion.py` já lê o campo; nunca
  acha. Idem TRF3/TJSP/TJMT/TJPA no Datajud.
- **Portal legado `www4.tjmg.jus.br/juridico/sf/proc_resultado.jsp`**: HTTP 401
  + captcha numérico (DWR `ValidacaoCaptchaAction`, input `captcha_text`) —
  exigiria captcha-solver por processo.

⇒ MG aparecer como "valor não informado" no mapa está **correto**: o tribunal
não publica. Reprocessar o que já baixamos preenche **0** processos. Fixtures da
evidência e teste-canário (falha se o TJMG passar a publicar):
`tests/test_enricher_tjmg.py` + `tests/fixtures/tjmg/`.

> **`0,00` ≠ ausente.** Onde a fonte manda o campo, `parse_valor_brl('R$ 0,00')`
> devolve `Decimal('0')` e o drainer grava 0 (TJPA: 336/2000; TJMT: 74/2000).
> Filtro/mapa que trata `valor_causa=0` como "informado" mostra "R$ 0,00" na
> ficha. Ausência real é `NULL`.

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

## Justiça do Trabalho (TST + 24 TRTs) — `consultaprocessual` (recon 2026-07-04)

Registrados (migration `0040_seed_trts`) e **ativos** (ingestão DJEN roda; DJEN
carrega trabalhista via `sigla_djen='TRTn'/'TST'`). **Enrichment ainda NÃO ligado.**

Recon da consulta pública (SPA Angular `pje.trt<N>.jus.br/consultaprocessual/`,
API `/pje-consulta-api/api`):

| Tier | Endpoint | Dados | Captcha |
|---|---|---|---|
| **A** | `GET /processos/dadosbasicos/{cnj20dig}` + header `X-Grau-Instancia: 1\|2` | `[{id, numero, classe(abbrev), codigoOrgaoJulgador, segredoJustica}]` | ❌ aberto |
| **B** | `GET /processos/{id}` | partes/valor/movimentos | ✅ **captcha de imagem** (`tokenDesafio`+`imagem` JPEG) |

- **Grau**: OOOO do CNJ = `0000` → 2º grau (TRT), senão 1º grau (vara). Empty (204) → tentar o outro.
- **Alcance direto** (probe dummy-CNJ): 🟢 400/aberto: TRT 2,4,6,8,10,11,14,16,17,18,19,20,21,24 · 🟡 403 datacenter (precisa pool/Cortex): TRT 1,5,7,9,12,13,15,22,23 · ⚠️ TRT3=405 (checar path).
- **Tier B exige CapSolver** (ImageToText). Estratégia econômica: enrich rico só nos **leads classificados** (não nos ~40M). Sem saldo CapSolver → adiado.
- **classe vem do DJEN** também (`preencher_classe_via_djen`) — Tier A é redundante p/ classe; só agrega `codigoOrgaoJulgador`.
- ⚠️ **Scheduler**: ativar muitos tribunais estourava `hour = 4 + idx//2` (>23). Corrigido p/ janela madrugada com módulo (`djen/scheduler.py`, fix 2026-07-04).

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

## Kill-switch por tribunal (pausa)

Quando um tribunal bloqueia o método de acesso (ex.: **TJRO/TJAP** devolvendo
`403` pra todos os proxies em 2026-07 — WAF/bloqueio total), continuar tentando
só queima proxy/CPU e enche `enriquecimento_erro`. Pausa via cache Redis
(`enrich:pausados`, sem TTL — sobrevive a restart), honrada em 3 pontos:
refill (`_reabastecer_impl` loga `pausado`), `enqueue_enriquecimento` (no-op) e
o job `enriquecer_processo` (retorna `{skip: pausado}` SEM tocar no status → a
fila residual drena sem bater na fonte; o processo segue `pendente`).

```bash
python manage.py enrich_pausa TJRO TJAP    # pausa
python manage.py enrich_pausa --off TJRO   # despausa (refill repõe sozinho)
python manage.py enrich_pausa --list       # mostra pausados
```

## Watermark no auto-enqueue (não inflar fila)

O `reabastecer_filas_enriquecimento` respeita `QUEUE_HIGH_WATER=100_000`, mas o
**auto-enqueue da ingestão** (`djen/ingestion.py`, quando cria processo novo)
NÃO respeitava — sob backfill isso inflou filas a MILHÕES (TJRO 3,6M, TJRJ 2,6M
em 2026-07-11). Agora o auto-enqueue checa `len(queue) < QUEUE_HIGH_WATER`
(gate cacheado 30s por sigla); acima do teto deixa pro refill (o processo já
fica `pendente`, nada se perde).

## Runbook: fila de enrichment inflada

1. `enrich_pausa <SIGLA>` se o tribunal está bloqueado (403/WAF total).
2. Esvaziar a fila (não perde nada — pendentes seguem no DB, refill repõe):
   ```python
   # manage.py shell
   import django_rq; django_rq.get_queue('enrich_tjro').empty()
   ```
3. Re-tentar erros após corrigir a causa:
   `manage.py enriquecer_pendentes --tribunal <SIGLA> --status erro --max-tentativas 3 --limit 0`

## AWS WAF challenge (PJe)

Alguns PJe (ex.: **TJPE**) ficam atrás de AWS WAF: retornam `HTTP 202` + página
`awsWafCookie`/`challenge.js` no lugar do form JSF — **intermitente** (~40% dos
proxies passam). `enrichers/pje.py::_request_with_rotation` detecta
(`202/405/403/429` + `awswaf` no corpo) e **rotaciona o proxy** como um 403, até
achar um IP que o WAF libera (não é bug de parser — não pausar). Bloqueio TOTAL
(TJRO/TJAP: 0 proxies passam) → pausar.

---

## Plano de cobertura por VALOR (Juriscope) — 2026-08-06

**Contexto.** Auditoria da cobertura de enriquecimento do acervo (75,6M): ~50M sem
enriquecimento completo. Brute-force é inviável — o Datajud é **1 APIKey pública
compartilhada** (teto ~100 rpm, ~15s/job na API do CNJ → ~94/min → 278 dias só o
Track A). **Decisão: enriquecer por VALOR, não por contagem** — só os tribunais que o
Juriscope de fato lê (precatório vira lead lá).

**Alvo = 10 tribunais do Falcon/Juriscope + TJPR** (add do usuário). Corta 50M → **~14M**.
Falcon (`datamodel_process` @ 10.10.0.51): TJSP, TRF1, TRF3, TRF4, TJMG, TRF6, TRF5,
TJAL, TRF2, TJMA (2,43M precatórios conhecidos).

### O buraco no alvo
```
Track A — datajud (sem enricher): TJPR 5,83M · TRF4 2,28M · TRF6 1,02M · TRF2 0,86M   → ~10,0M
Track B — enricher (PJe/e-SAJ):   {TJSP,TRF1,TRF3,TRF5,TJMG,TJAL,TJMA}
                                   20,5M tot · 54% ok · pendente 2,78M · erro 1,14M    → gap ~3,9M
TOTAL ALVO ≈ 13,9M  (vs ~50M do acervo inteiro)
```
Nota: isso DESprioriza os maiores backlogs de enricher (TJRJ 1,3M/386k-erro, TJPE, TJPA,
TJMT, TJCE) — Juriscope não cobre esses.

### Fases
- **Fase 0 — alvo fino (pré-filtro DJEN).** Dentro do alvo, enriquecer PRIMEIRO os CNJ
  cujo texto DJEN (já ingerido, sem custo) menciona precatório/requisitório/RPV/ofício.
  Troca "74 dias pro Track A" por "dias pro que importa".
- **Fase 1 — Track A priorizado.** Refill do datajud puxa por (alvo-Juriscope → sinal-DJEN
  → newest), não FIFO/spread cego. Teto de 100 rpm é permanente (APIKey única) → foco em
  gastar a quota no que vale. TJPR é o grosso (5,8M).
- **Fase 2 — Track B higiene.** (a) requeue dos **1,14M em erro** (muito é transitório de
  proxy/WAF; parte é parser — ver fix ViewState TJPE/TRF1); (b) escalar scrapers dos
  maiores do alvo sob o kill-switch existente. Não mexer nos não-alvo.
- **Fase 3 — instrumentação.** Página de **cobertura por tribunal** (%ok, pendente, erro,
  datajud-null) — hoje só há freshness (lag), não cobertura. Responde "quais faltam"
  continuamente. Barato; fazer primeiro.

### Restrições firmes
- Datajud = 1 APIKey compartilhada → **sem lever de rate-limit** (não existe key dedicada).
  Escala do Track A = priorização, não throughput.
- Enricher = proxy/captcha/WAF por tribunal; kill-switch + watermark já existem.

### Execução (2026-08-09) — o que saiu
- **Fase 1 ✅**: refill datajud prioriza o alvo (fila 92% TJPR/TRF4/TRF6/TRF2). Bug
  pego: `order_by('-inserido_em')` por-tribunal = 35s → timeout 300s; fix = sem sort.
- **Fase 3 ✅**: `/dashboard/tribunais/cobertura/` (warm 6h). Gap do alvo = 13,9M.
- **Fase 2 ✅**: `tick_requeue_erros_enricher` (10min) devolve erro→pendente nos 7
  tribunais Juriscope∩enricher (SEM floor — senão pulava os grandes). Erro caindo
  ~50k/batch (1,14M→…). reabastecer só pega 'pendente', por isso os erros ficavam presos.
- **Fase 0 ⛔ (não é quick-win)**: pré-filtro por `classificacao` NÃO serve nos
  tribunais datajud — TJPR tem 4,2M classificados mas **lead=0** (sem datajud o
  classificador não acha precatório: chicken-egg). Pré-filtro real = flag DJEN-texto
  denormalizado + backfill (migration + scan de movimentações) → item próprio.

---

## Valor da causa: onde existe e onde NÃO existe (triagem 2026-08-13)

**Pergunta que motivou.** Só 5 UFs têm `valor_causa` no ES; TJRJ (3,46M), TJMA
(2,70M), TJPE, TJCE e TJAP estão em 0,0%. Hipótese inicial: "falta parser nesses
enrichers" (os arquivos `enrichers/tjrj.py` etc. não mencionam "valor").

**Veredicto: a hipótese é FALSA — e escrever parser não preencheria nada.**

`BasePjeEnricher._extrair_dados` (`enrichers/pje.py`, ramo
`elif 'valor' in chave and 'causa' in chave`) **já** extrai o valor; os 5
enrichers são subclasses de 13 linhas e herdam isso. O ramo nunca dispara porque
**a consulta pública do PJe clássico não publica o valor da causa**.

### Quem publica o valor é o SISTEMA, não o tribunal

| Sistema | Campo na fonte | Valor? | Tribunais |
|---|---|---|---|
| e-SAJ | `#valorAcaoProcesso` (só 1º grau/cpopg) | **Sim** | TJSP, TJAL, TJAC |
| REST próprio (TJPA) | `valorCausaFormatado` | **Sim** | TJPA |
| REST próprio (TJMT) | `valorCausa` | **Sim** | TJMT |
| **PJe consulta pública** (clássico e SPA) | — não existe — | **Não** | TJRJ, TJMA, TJPE, TJCE, TJAP, TJMG, TJRO, TRF1, TRF3, TRF5, TJDFT |
| **Datajud** (API CNJ) | `valorCausa` **ausente do `_source`** | **Não** | todos os acima |

O detalhe do PJe consulta pública expõe exatamente 6 campos em `div.propertyView`:
Número Processo · Data da Distribuição · Classe Judicial · Assunto · Jurisdição ·
Órgão Julgador. Fim. A string "valor" **não aparece uma única vez** no HTML
inteiro (70–140 KB por página).

### Evidência (probes reais, 2026-08-13)

- **16 processos reais** buscados ao vivo pelo próprio enricher (search + detalhe,
  via Cortex), 5 tribunais: `ocorrencias_de_"valor"_no_HTML_CRU = 0` em **todos**.
  Fixtures de detalhe reais no repo pros 5 (TJPE/TJAP capturadas nesta triagem).
- **Datajud**: 14 CNJ dos 5 tribunais → `valorCausa=None` e a chave sequer existe
  no `_source` (`['@timestamp','assuntos','classe','dataAjuizamento',...]`).
- **Prod, amostra por `(tribunal, -id)` com LIMIT** (nunca `valor_causa__isnull`
  solto — vira seq scan no tabelão): 0/300 com valor em TJRJ, TJMA, TJPE, TJCE,
  TJAP **e também em TRF1, TRF3, TJMG** (mesma base PJe). Contraprova no mesmo
  corte: TJPA 300/300, TJMT 288/300, TJSP 183/300.

⇒ O ramo de valor em `pje.py` é **código morto em todos os PJe**. Mantido de
propósito: se um tribunal passar a publicar, ele volta a funcionar sozinho.

### Quanto custaria obter mesmo assim

Não sai de graça: exigiria **uma requisição extra por processo** num sistema
*diferente* (portal legado do tribunal), cada um com layout próprio e captcha.
Para TJRJ+TJMA isso é ~6,2M requisições adicionais sob o mesmo orçamento de
proxy/WAF que hoje já limita o enriquecimento básico — e reprocessar o que já
temos **não preencheria um único registro**, porque o dado nunca esteve no
payload. Só faz sentido com decisão explícita de custo (captcha-solver + scraper
novo por tribunal), não como conserto de parser.

### Onde vale investir (se o objetivo é cobertura de valor)

Nos tribunais **e-SAJ e REST próprio** — é onde o dado existe. TJSP está em
183/300 (~61%): fechar essa lacuna (2º grau não traz valor; ver §cpopg/cposg)
rende mais que qualquer coisa nos 5 PJe.

### Regressão

`tests/test_valor_causa_pje.py` trava a constatação com as fixtures reais dos 5:
se algum tribunal passar a publicar o valor, o teste de ausência quebra e avisa
que dá pra ligar a extração. Cobre também o formato BR do `parse_valor_brl`
(milhar `.` vs decimal `,` — inverter erra por 1000×) e o contrato
**sem valor → `None`, nunca `Decimal('0')`** (0 afirmaria "a causa vale R$ 0,00").

---

## Auditoria de completude do DADO — matriz tribunal × campo (24–25/08/2026)

Pergunta diferente da do acervo. Não é "temos o processo?" (ver
[`ACERVO_CNJ.md`](ACERVO_CNJ.md)) — é **"dos processos que já temos, extraímos
tudo que a fonte dá?"**. A resposta curta: **não, e o maior buraco não é de
scraping — é de dado já baixado que ninguém promove**.

### Como foi medido (amostra, semente, custo)

Nada aqui usa `exists` do ES (regra nº 4 do CLAUDE.md: string vazia conta como
presente) nem `reltuples`. Todo número sai de **conteúdo**, por amostra.

| # | amostra | tamanho | como | custo |
|---|---|---:|---|---|
| A | matriz de campos (PG) | **120.000 processos** | 3.000 âncoras uniformes em `id ∈ [3.520, 104.602.261]`, `random.Random(20260824)`, bloco de 40 pks consecutivos por âncora (`WHERE id >= a ORDER BY id LIMIT 40`) | 38,7 s |
| B | partes | 11.160 (200/tribunal) + **todos os 17.376 `ok`** da amostra A | `processoparte ⋈ parte` por `processo_id = ANY(...)` | 5 s + 47 s |
| C | destinatários DJEN | 3 movimentações mais recentes de cada um dos 11.160 | `LATERAL … ORDER BY data_disponibilizacao DESC LIMIT 3` | 18,5 s |
| D | portas | 20 processos reais, 1 por tribunal, priorizando lead | probe ao vivo no Datajud, 1 req/2 s | 40 s |
| E | ES × PG | `_mget` dos mesmos 11.160 em `voyager-processos` | resposta exata por id, nunca `exists` | 6 s |
| F | e-SAJ ao vivo | 11 processos TJSP `ok` **sem partes** | `open.do` + `search.do` diretos, 3 s entre requisições | ~1 min |

**Controle positivo da sonda** (verificação com input vazio reporta sucesso):
a função de "preenchido" foi rodada contra 2 linhas sintéticas — uma cheia, uma
com `-` / `não informado` / `0` / `N/A` — e reportou 50% em todos os campos,
classificando cada placeholder pelo valor. Sem esse controle, "0,0%" não
distingue "campo vazio" de "sonda quebrada".

**Aferição da amostra A contra a contagem exata do acervo** (102.296.406):

| status | contagem exata | amostra A | erro |
|---|---:|---:|---:|
| pendente | 76,2% | 77,27% | +1,1 pp |
| ok | 15,2% | 14,48% | −0,7 pp |
| nao_encontrado | 7,0% | 6,74% | −0,3 pp |
| erro | 1,6% | 1,52% | −0,1 pp |

⇒ o desenho por conglomerados (40 pks vizinhos) tem efeito de desenho ≈ **1 pp**.
Números abaixo de ~2 pp de diferença entre tribunais **não são achado**.

### A matriz (% dos processos com o campo REALMENTE preenchido)

Vazio = `NULL`, string vazia **ou** placeholder (`-`, `não informado`, `0`,
`N/A`, …). Nenhum placeholder foi encontrado nos 120.000 — os campos deste
banco são cheios ou vazios de verdade, nunca "cheios de nada".

| tribunal | acervo est. | n | ok% | classe cód | assunto cód | assunto nome | autuação | valor | órgão cód | órgão nome | juízo | ≥1 parte | dos `ok`, com parte |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **TJSP** | 17.254.846 | 20241 | 10.0 | 24.8 | 4.9 | 13.4 | 13.0 | 7.7 | 6.2 | 13.5 | 10.4 | 6 | 82 |
| **TJMG** | 7.815.445 | 9168 | 32.1 | 55.2 | 17.4 | 41.4 | 41.5 | 0.0 | 18.2 | 18.2 | 0.0 | 36 | 99 |
| **TJPR** | 6.943.369 | 8145 | 0.0 | 1.2 | 1.2 | 1.2 | 1.2 | 0.0 | 1.2 | 1.2 | 0.0 | 0 | · |
| **TJRJ** | 5.624.597 | 6598 | 15.7 | 36.9 | 0.3 | 18.1 | 18.1 | 0.0 | 0.4 | 0.4 | 0.0 | 21 | 100 |
| **TJRS** | 5.082.426 | 5962 | 0.0 | 3.6 | 3.6 | 3.6 | 3.4 | 0.0 | 3.6 | 3.6 | 0.0 | 0 | · |
| **TRF3** | 4.941.769 | 5797 | 63.4 | 70.5 | 3.6 | 67.9 | 68.0 | 0.0 | 4.7 | 68.0 | 0.0 | 69 | 100 |
| **TRF4** | 3.352.765 | 3933 | 0.0 | 21.2 | 21.2 | 20.8 | 21.2 | 0.0 | 21.2 | 21.2 | 0.0 | 0 | · |
| **TJSC** | 3.314.404 | 3888 | 0.0 | 3.6 | 3.6 | 3.6 | 3.6 | 0.0 | 3.6 | 3.6 | 0.0 | 0 | · |
| **TJMA** | 2.909.480 | 3413 | 17.1 | 38.1 | 24.7 | 41.5 | 41.2 | 0.0 | 30.1 | 41.6 | 0.0 | 17 | 100 |
| **TJMT** | 2.653.739 | 3113 | 29.9 | 43.3 | 21.2 | 21.1 | 43.9 | 29.7 | 21.2 | 43.9 | 0.0 | 32 | 100 |
| **TRF1** | 2.457.671 | 2883 | 74.1 | 82.0 | 15.5 | 74.8 | 74.8 | 0.0 | 13.0 | 74.8 | 0.0 | 76 | 98 |
| **TJBA** | 2.365.604 | 2775 | 0.0 | 8.3 | 8.3 | 8.3 | 8.3 | 0.0 | 8.3 | 8.3 | 0.0 | 0 | · |
| **TJGO** | 2.090.257 | 2452 | 0.0 | 24.1 | 24.1 | 23.9 | 24.1 | 0.0 | 24.1 | 24.1 | 0.0 | 0 | · |
| **TJPA** | 1.954.714 | 2293 | 18.8 | 33.5 | 28.3 | 28.3 | 28.4 | 20.5 | 10.1 | 28.4 | 20.5 | 21 | 100 |
| **TJCE** | 1.931.697 | 2266 | 11.5 | 37.0 | 28.1 | 36.7 | 36.7 | 0.0 | 29.7 | 36.7 | 0.0 | 12 | 100 |
| **TJRR** | 1.705.793 | 2001 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0 | · |
| **TRF5** | 1.695.563 | 1989 | 86.4 | 91.3 | 23.8 | 90.8 | 88.4 | 0.0 | 44.2 | 59.3 | 0.0 | 85 | 100 |
| **TRF6** | 1.630.775 | 1913 | 0.0 | 20.8 | 20.8 | 20.8 | 20.6 | 0.0 | 20.8 | 20.8 | 0.0 | 0 | · |
| **TJPE** | 1.421.920 | 1668 | 12.1 | 41.6 | 6.8 | 20.7 | 20.4 | 0.0 | 7.8 | 20.8 | 0.0 | 18 | 100 |
| **TRT2** | 1.413.395 | 1658 | 0.0 | 3.4 | 3.4 | 3.4 | 3.4 | 0.0 | 3.4 | 3.4 | 0.0 | 0 | · |
| **TRT15** | 1.335.821 | 1567 | 0.0 | 6.5 | 6.5 | 6.5 | 6.5 | 0.0 | 6.5 | 6.5 | 0.0 | 0 | · |
| **TJMS** | 1.291.492 | 1515 | 0.0 | 19.1 | 19.1 | 19.1 | 19.1 | 0.0 | 19.1 | 19.1 | 0.0 | 0 | · |
| **TJAM** | 1.279.558 | 1501 | 0.0 | 88.2 | 33.4 | 33.4 | 33.4 | 0.0 | 33.4 | 33.4 | 0.0 | 0 | · |
| **TST** | 1.278.705 | 1500 | 0.0 | 17.2 | 17.2 | 17.2 | 17.2 | 0.0 | 17.2 | 17.2 | 0.0 | 0 | · |
| **TJSE** | 1.227.557 | 1440 | 0.0 | 7.8 | 7.8 | 7.8 | 7.8 | 0.0 | 7.8 | 7.8 | 0.0 | 0 | · |
| **TJPI** | 1.177.261 | 1381 | 0.0 | 10.3 | 10.3 | 10.3 | 10.3 | 0.0 | 10.3 | 10.3 | 0.0 | 0 | · |
| **TJDFT** | 1.099.686 | 1290 | 72.6 | 95.4 | 82.7 | 82.7 | 82.7 | 0.0 | 70.9 | 82.7 | 0.0 | 70 | 100 |
| **STJ** | 1.098.834 | 1289 | 0.0 | 54.0 | 1.4 | 1.4 | 1.4 | 0.0 | 1.4 | 1.4 | 0.0 | 0 | · |
| **TRT1** | 1.088.604 | 1277 | 0.0 | 2.4 | 2.4 | 2.4 | 2.4 | 0.0 | 2.4 | 2.4 | 0.0 | 0 | · |
| **TJPB** | 1.069.850 | 1255 | 0.0 | 23.8 | 23.8 | 23.8 | 23.8 | 0.0 | 23.8 | 23.8 | 0.0 | 0 | · |
| **TJRN** | 1.064.735 | 1249 | 0.0 | 2.3 | 2.3 | 2.3 | 2.3 | 0.0 | 2.3 | 2.3 | 0.0 | 0 | · |
| **TRF2** | 1.043.423 | 1224 | 0.0 | 23.2 | 23.2 | 23.0 | 22.5 | 0.0 | 23.2 | 23.2 | 0.0 | 0 | · |
| **TJRO** | 1.008.472 | 1183 | 0.0 | 38.5 | 1.9 | 1.9 | 1.9 | 0.0 | 1.9 | 1.9 | 0.0 | 0 | · |
| **TJAL** | 715.222 | 839 | 46.4 | 93.3 | 42.6 | 84.1 | 85.6 | 45.1 | 78.5 | 84.4 | 49.5 | 47 | 88 |
| **TJAP** | 316.266 | 371 | 7.0 | 67.7 | 31.0 | 37.5 | 37.5 | 0.0 | 34.5 | 37.5 | 0.0 | 16 | 100 |
| **TJAC** | 136.395 | 160 | 42.5 | 100.0 | 61.3 | 81.9 | 78.1 | 20.6 | 77.5 | 81.2 | 36.9 | 37 | 78 |

---

## Vazão: onde o tempo vai e o que estava comendo a frota (medição 25/08/2026)

Pergunta que abriu: "por que o enriquecimento está tão lento?". A resposta não
era escala — era **desperdício**. Todos os números abaixo foram medidos em
produção, não estimados.

### O tempo de um job é REDE. Parse é ruído; banco é zero.

Cronômetro por fase dentro de um container da `.102` (mesma rota de rede dos
workers), 5–6 processos `pendente` por tribunal, amostra aleatória semente
`20260824`, `stream.publish` no-op:

| tribunal | mediana/job | rede busca (soma) | rede detalhe | parse | % rede |
|---|---|---|---|---|---|
| TRF1 | 1,29 s | 4,18 s | 3,69 s | 0,08 s | 98,4% |
| TRF3 | 1,49 s | 4,18 s | 4,85 s | 0,07 s | 99,2% |
| TRF5 | 2,19 s | 9,77 s | 8,62 s | 0,09 s | 99,4% |
| TJMG | 7,90 s | 51,91 s | 5,31 s | 0,07 s | 99,9% |
| TJMA | 2,63 s | 13,09 s | 5,36 s | 0,06 s | 99,7% |
| TJCE | 2,03 s | 6,10 s | 5,67 s | 0,07 s | 99,7% |
| TJPE | 2,72 s | 25,70 s | 4,26 s | 0,06 s | 99,9% |
| TJPA | 1,09 s | 10,28 s | — | 0,00 s | 99,8% |
| TJMT | 0,63 s | 12,80 s | — | 0,00 s | 100% |
| TJRJ | 0,65 s | 2,53 s | 1,87 s | 0,03 s | 100% |
| TJDFT | **0,095 s** | 0,18 s | 0,07 s | 0,00 s | 62,5% |
| TJRO | 8,95 s | 49,42 s | — | 0,00 s | 100% (tudo `erro`) |

**Banco não aparece porque o worker não toca o Postgres**: ele publica o
resultado no stream Redis e o drainer escreve. Otimizar parser ou banco não
compra vazão nenhuma aqui — o que compra é *menos requisição desperdiçada* e
*IP que a fonte aceita*.

O TJDFT é a régua do que dá pra ser: API REST, 0,095 s por processo, **35,9 mil
jobs/h com 2 réplicas**. PJe clássico custa 3 viagens HTTP (GET listView → POST
pesquisa → GET detalhe) e nunca desce de ~1 s.

### O gargalo real: 77,1% das raspagens repetiam trabalho já enfileirado

`_reabastecer_impl` enche cada fila até `QUEUE_HIGH_WATER` (100.000). Com a
frota atual — **146 workers de enrich, 146 ocupados, 0 ocioso** — 100k jobos
significam **20 a 74 horas de espera na fila** (medido pelo `enqueued_at` do job
na frente). E o `Process` só sai de `pendente` quando o drainer aplica o
resultado, lá no fim dessa espera. Resultado: a cada 2 minutos o refill
re-seleciona a MESMA cabeça do índice e enfileira de novo.

Censo COMPLETO das 16 filas (todos os ids, não amostra), 00:20 UTC:

| fila | ids | process_id distintos | cópias | máx. de 1 pid | desperdício |
|---|---|---|---|---|---|
| enrich_tjmt | 99.818 | 1.418 | 70,4 | 136 | 98,6% |
| enrich_tjac | 99.976 | 1.929 | 51,8 | 607 | 98,1% |
| enrich_tjap | 99.968 | 3.426 | 29,2 | 515 | 96,6% |
| enrich_tjal | 99.993 | 6.433 | 15,5 | 849 | 93,6% |
| enrich_tjro | 99.979 | 8.486 | 11,8 | **1.926** | 91,5% |
| enrich_tjpa | 99.952 | 10.130 | 9,9 | 832 | 89,9% |
| enrich_tjma | 99.927 | 10.711 | 9,3 | 564 | 89,3% |
| enrich_tjce | 99.954 | 12.008 | 8,3 | 336 | 88,0% |
| enrich_tjpe | 99.942 | 12.895 | 7,8 | 499 | 87,1% |
| enrich_trf5 | 99.948 | 31.546 | 3,2 | 535 | 68,4% |
| enrich_trf3 | 99.939 | 35.265 | 2,8 | 672 | 64,7% |
| enrich_trf1 | 99.935 | 35.675 | 2,8 | 484 | 64,3% |
| enrich_tjdft | 100.767 | 54.277 | 1,9 | 32 | 46,1% |
| enrich_tjmg | 99.947 | 74.931 | 1,3 | 192 | 25,0% |
| enrich_tjrj | 251.534 | 251.534 | 1,0 | 1 | **0,0%** |
| enrich_tjsp | 227.678 | 227.503 | 1,0 | 2 | **0,1%** |

**1.400.007 jobos para 321.130 processos distintos nas 14 filas que o refill
administra = 77,1% de cópia.** As duas filas limpas são exatamente as que estão
ACIMA do teto — o refill as PULA, e a duplicação desaparece. O teto redondo de
100k não era o total da fila: era a fábrica de cópia.

O drainer não é gargalo e prova isso: aplica **126.264 events/h** (4 partições,
5 min de log) com `skipped_idempotent` ≈ 0 e `XLEN` das partições entre 0 e 44.
O trabalho chega — é que dois terços dele já tinham sido feitos.

### O efeito colateral: a cópia queima o pool, que é compartilhado

Cada job que falha gasta até 10 IPs (`MAX_PROXY_ROTATIONS`), e cada 403 chama
`pool.mark_bad` com TTL de 120 s. Estado do pool medido: **2.500 IPs na lista,
289 saudáveis (11,6%), 2.211 no `bad_zset`, `degraded=1`, `fail_streak=11.011`**.
Os três tribunais de rendimento ~zero (TJRO, TJAP, TJAL) sozinhos queimavam
~58 mil IP/h; a TTL de 120 s sustenta ~1.930 IPs marcados a qualquer instante —
praticamente o `bad_zset` inteiro. Ou seja: o desperdício de um tribunal degrada
a coleta de **todos** os outros.

### Bug: o escalonamento pro Cortex nunca escalou

`BasePjeEnricher._request_with_rotation` declarava `bloqueios_dc = 0` e
consultava `force_cortex=bloqueios_dc >= 3` — **e nunca incrementava a
variável**. O ramo residencial era inalcançável: com 2.500 IPs no pool, as 10
rotações jamais o esgotam, então o fallback do fim de `_next_proxy` também
nunca é atingido. Tribunal que bloqueia datacenter terminava 100% em `erro` sem
nunca ter tentado o residencial. O `BaseEsajEnricher` não tinha o escalonamento
de forma alguma.

Prova A/B (mesmos 5 pids, semente `20260824`, um container da `.102`):

| tribunal | rota | mediana | desfechos |
|---|---|---|---|
| TJAP | pool datacenter | 7,91 s | **erro 5/5** |
| TJAP | Cortex residencial | **0,95 s** | ok 1 + nao_encontrado 4, **0 erro** |
| TJAL | pool datacenter | 3,42 s (73,1 s no total) | ok 5/5 |
| TJAL | Cortex residencial | **1,22 s** (17,0 s no total) | ok 5/5 |
| TJRO | pool datacenter | 13,91 s | erro 5/5 |
| TJRO | Cortex residencial | 8,28 s | **erro 5/5** |

TJAP não estava bloqueado na fonte — `curl` direto e via Cortex devolvem HTTP
200. Estava bloqueado **no caminho que o código insistia em usar**.

### TJRO: bloqueio total, e zero `ok` em 1,09 milhão de processos

`curl` ao `pjepg-consulta.tjro.jus.br` devolve **403 pelo IP direto E pelo
Cortex residencial**. O balanço do tribunal: `pendente` 720.344 · `erro`
367.176 · **`ok` 0** · `nao_encontrado` 1. O enricher do TJRO nunca entregou um
único processo. É o caso exato do kill switch — que estava com a lista vazia.

### Desfecho por tribunal (15 min de log dos 146 workers, ×4 = por hora)

| tribunal | jobs/h | ok/h | nao_encontrado/h | erro/h | % ok |
|---|---|---|---|---|---|
| TJDFT | 34.184 | 25.892 | 8.148 | 0 | 75,7% |
| TJRJ | 21.892 | 4.956 | 16.860 | 0 | 22,6% |
| TJMT | 16.864 | 16.152 | 668 | 0 | 95,8% |
| TJSP | 10.580 | 64 | 10.480 | 0 | **0,6%** |
| TJCE | 5.380 | 3.972 | 1.396 | 0 | 73,8% |
| TJMA | 5.988 | 5.100 | 860 | 0 | 85,2% |
| TJPE | 4.352 | 3.168 | 1.040 | 128 | 72,8% |
| TJAP | 3.648 | 0 | 0 | 3.648 | **0%** |
| TJMG | 3.844 | 3.092 | 748 | 0 | 80,4% |
| TRF1 | 3.352 | 3.228 | 116 | 0 | 96,3% |
| TRF3 | 3.072 | 2.988 | 64 | 0 | 97,3% |
| TJPA | 3.060 | 2.592 | 0 | 456 | 84,7% |
| TRF5 | 2.496 | 2.432 | 52 | 0 | 97,4% |
| TJRO | 1.388 | 0 | 0 | 1.388 | **0%** |
| TJAL | 944 | 44 | 0 | 900 | **4,7%** |

> **TJSP com 0,6% de `ok` é achado aberto e NÃO é problema de vazão.** 10.480
> processos por hora sendo marcados `nao_encontrado` (que é TERMINAL pós
> 2026-07-06) enquanto o mesmo enricher, na mesma máquina, acerta 6 de 6 numa
> amostra da cabeça do índice. A população que está na fila do TJSP é outra
> (CNJ sequenciais `4xxxxxx-…2026.8.26.…`). Isso é qualidade/parser — passado
> pra auditoria de qualidade, não corrigido aqui.

### O que mudou no código

1. **`enrichers/pje.py`** — `bloqueios_dc += 1` no ramo de 403/WAF de proxy do
   pool. O escalonamento pro Cortex passa a existir de fato.
2. **`enrichers/esaj.py`** — `_next_proxy` ganha `force_cortex` e os dois laços
   de rotação (`_buscar_em_grau`, `_fetch_incidente`) contam bloqueios de
   datacenter e escalam após 3. Antes o e-SAJ não tinha escalonamento nenhum.
3. **`enrichers/jobs.py::_reabastecer_impl`** — duas travas:
   - `_profundidade_alvo()`: o teto da fila deixa de ser 100k fixo e passa a ser
     `ENRICH_QUEUE_ALVO_S` (1.800 s) de trabalho, calculado pela vazão REAL da
     fila (`len(FinishedJobRegistry)/result_ttl`, dado que o RQ já mantém).
     `QUEUE_HIGH_WATER` continua como teto duro; `ENRICH_QUEUE_MIN` (2.000) como
     piso pra fila lenta não secar.
   - `_filtrar_em_voo()`: ZSET `enr:inflight:<sigla>` com os `process_id` já
     enfileirados, podado por score a cada passada. Um pid só volta pra fila
     depois de `ENRICH_INFLIGHT_TTL_S` (3.600 s). O refill agora lê MAIS
     candidatos que a capacidade (`capacidade + em_voo + 1.000`, teto 60k) —
     senão, com a cabeça do índice inteira em voo, ele não avançaria.
4. **`enrichers/jobs.py::tick_requeue_erros_enricher`** — teto de
   `REENRICH_MAX_TENTATIVAS` (12) raspagens por processo. Sem ele o tick era
   esteira infinita: o processo 24179591 (TJAL) estava em
   `enriquecimento_tentativas=360`. Conforme a regra nº 2 do CLAUDE.md, o teto
   é **alerta** — loga em ERROR quantos processos ficaram estacionados por
   tribunal, nunca corta em silêncio.

### O que NÃO é o gargalo (conferido, pra não voltar a ser investigado)

- **Workers ociosos**: `Worker.all()` → 146 de 146 workers de enrich em `busy`.
  Os "145 jobs rodando" do relato eram a frota inteira trabalhando.
- **Drainer / Postgres**: 126.264 events/h aplicados, `XLEN` das 4 partições
  entre 0 e 44, `skipped_idempotent` ≈ 0.
- **Kill switch preso**: `enrich:pausados` estava **vazio** — nada pausado.
- **Código velho**: `FailedJobRegistry` em **0 nas 16 filas**; frota reciclada
  em 24/08 (ver OPS.md).
- **Parser**: 0,06–0,09 s por 6 jobs, entre 0,1% e 1,6% do tempo.

### Backlog real por tribunal (contagem por índice, 25/08/2026)

| sigla | pendente | erro | ok | nao_encontrado |
|---|---|---|---|---|
| TJSP | 12.101.245 | 0 | 1.634.135 | 2.591.568 |
| TJMG | 3.556.408 | 0 | 2.479.816 | 2.255.297 |
| TJRJ | 3.394.492 | 594.533 | 1.051.748 | 1.040.036 |
| TJMA | 2.252.663 | 0 | 440.799 | 69.661 |
| TJPA | 1.688.451 | 157.920 | 329.324 | 1 |
| TJMT | 1.547.426 | 14.797 | 638.987 | 77.903 |
| TRF3 | 1.517.936 | 0 | 3.450.446 | 344.925 |
| TJCE | 1.243.006 | 23.027 | 252.843 | 70.116 |
| TJPE | 869.550 | 361.591 | 213.551 | 130.296 |
| TJRO | 720.344 | 367.176 | **0** | 1 |
| TRF1 | 450.672 | 0 | 1.917.351 | 89.754 |
| TJAL | 247.478 | 61 | 362.899 | 54.053 |
| TJAP | 175.063 | 105.959 | 20.533 | 7.515 |
| TRF5 | 70.629 | 0 | 1.697.025 | 112.705 |
| TJAC | 64.030 | 9.556 | 49.589 | 32.768 |
| TJDFT | 58.387 | 5.483 | 997.955 | 329.738 |
| **soma** | **29.957.780** | **1.640.103** | **15.537.001** | **7.206.337** |

> `erro = 0` nos 7 tribunais Juriscope não quer dizer que não há erro: o
> `tick_requeue_erros_enricher` devolve todos a `pendente` a cada 10 min.

### A conta do horizonte

- Pendentes no país: **77.923.013**. Em tribunais **com** enricher:
  **29.957.780** (38,4%). Os outros **47.965.233 (61,6%)** estão em tribunais
  sem enricher (TJPR, TRF2/4/6, TRTs, …) e **o enricher nunca vai alcançá-los**
  — a porta deles é o Datajud, com APIKey pública a ~100 rpm ⇒ 144 mil/dia ⇒
  **≈ 333 dias**. Esse é o número desconfortável, e nenhum ajuste de worker
  muda: o limite é por chave, não por IP.
- Com enricher, somando `pendente + erro` = **31,6 milhões**. À taxa medida de
  processos distintos concluídos (41.571/h na melhor hora), **≈ 32 dias**; à
  média de 24 h (25.277/h), **≈ 52 dias**.
- Eliminando a cópia (o desperdício medido é 77,1% nas filas administradas pelo
  refill), a mesma frota, com a MESMA carga sobre as fontes, tende à vazão bruta
  de jobos já medida (~125 mil/h) ⇒ **≈ 11 dias**. É esse o tamanho da
  correção: uma ordem de grandeza, sem um worker a mais e sem uma requisição a
  mais no tribunal.
