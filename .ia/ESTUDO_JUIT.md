# Estudo: API de Jurisprudência da JUIT × acervo do Voyager

> **Data das medições: 2026-08-30.** Tudo abaixo que é número saiu de uma
> consulta registrada neste documento. Onde não deu pra medir, está na §8 —
> não há estimativa disfarçada de fato.
>
> **Veredito em uma linha:** temos o *volume* (1,55 bi de publicações, 59
> tribunais) e não temos o *produto* — jurisprudência é **decisão com ementa e
> inteiro teor**, e disso temos **~2,2 milhões de acórdãos com teor** contra
> "+70 milhões de decisões" que a JUIT declara. O buraco não é de escala, é de
> **natureza da fonte**: o DJEN publica a *intimação do* acórdão, não o acórdão.
>
> **E não é falha nossa:** a Res. CNJ 455/2022, art. 13, manda o DJEN publicar
> só "o *dispositivo* das sentenças e a *ementa* dos acórdãos". Inteiro teor
> por essa porta é impossível **por norma** (§5.2).
>
> **Os dois achados aproveitáveis:**
> 1. 88,8% dos acórdãos que contêm "ementa" têm a ementa **recortável por
>    regex** do texto que já está no disco ⇒ **~2,7 milhões de ementas com
>    zero requisição a tribunal** (§3.5) — e a norma acima explica por quê.
> 2. O **STJ publica as íntegras diárias sob CC-BY** desde 2021, com `ementa`,
>    `teseJuridica` e `ministroRelator` já estruturados; o **CJPG do TJSP**
>    devolve sentença inteira por GET sem captcha. **Duas portas abertas que
>    nunca tentamos** (§5.3) — enquanto 12 dos 13 portais grandes estão atrás
>    de captcha/WAF.
>
> **Procedência:** os números das §2–§3 são medições minhas em produção
> (comandos no apêndice). As §1, §5.3 e §7 citam fontes públicas externas,
> com URL em cada afirmação.

---

## 1. O que a JUIT vende

### 1.1 A oferta, na palavra deles

| Afirmação | Fonte |
|---|---|
| "92 tribunais, +70 milhões de decisões, dados estruturados e atualizados diariamente" | `juit.com.br/produtos/api-de-jurisprudencia` |
| "STF + 5 Tribunais Superiores + 6 TRFs + 27 TJs + 24 TRTs + 27 TREs + 3 TJMs" | idem |
| "conteúdo integral para análises reais" (em contraste com quem dá só "dados de capa e andamentos") | `juit.com.br/descubra/api-de-jurisprudencia` |
| "Não se preocupe se o site do tribunal cair, ou se o layout dos dados for alterado" | idem |
| "Diminua os riscos fazendo RAG com os nossos dados" | idem |
| "Teste com dados de produção. Sem cartão de crédito." | idem |
| "a maior base de jurisprudência acessível via API do Brasil" | `docs.juit.io/llms.txt` |
| "Pare de construir crawlers e scrapers. Integre dados que funcionam." | `descubra/api-de-jurisprudencia` |
| Os 4 selos do produto: "Rápida", "Segura", "Dados limpos", **"Busca no conteúdo"** | idem |
| "Todos os LLMs alucinam na hora de citar precedentes. Diminua os riscos fazendo RAG com os nossos dados." | idem |
| "Essa mesma camada de dados também alimenta o **GPT Jurídico da JUIT**, disponível para clientes da API e do JUIT Rimor." | `docs.juit.io/pt/home` |
| Base é a mesma do produto **JUIT Rimor** (`rimor_url` volta em cada item) | schema OpenAPI, campo `rimor_url` |

Duas inconsistências do próprio material, para quem for citar o número:

- **A decomposição não fecha com o hero.** `1 STF + 5 superiores + 6 TRFs + 27
  TJs + 24 TRTs + 27 TREs + 3 TJMs = **93**`, e o título diz **92**.
- **O snippet de código da landing aponta para host morto.** Ele usa
  `url = "https://api.juit.dev/jurisprudence"` — e `api.juit.dev` é
  **NXDOMAIN**. O host real é `api.juit.io` (§1.2).

Mas esse mesmo snippet morto entrega o dado mais útil da página inteira:
`"search_on": "ementa,integra"`. **É a confirmação, no material público, de que
a busca cobre ementa E íntegra** — os dois campos que nós não temos.

**Profundidade histórica declarada: não publicado.** Nenhuma página diz "desde
o ano X".

**Preço: não publicado.** O acesso é por `comercial@juit.io`; a doc pública é
`protectedRoutes: ["/pt/jurisprudence/*"]` — o guia e as receitas exigem login.

### 1.2 O contrato técnico (fonte primária, não marketing)

O portal (`docs.juit.io`, Zudoku) não expõe as páginas de doc, mas **empacota o
OpenAPI no bundle JS**. Recuperado o spec inteiro via sourcemap:

```
curl -s https://docs.juit.io/assets/entry.client-BYIqei3k.js         # descobre o chunk
curl -s https://docs.juit.io/assets/pt-jurisprudence-api-search-external.yaml-CF1PCWge.js.map \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sourcesContent"][0])'
```

- `openapi: 3.1.1`, `info.title: "API de Busca"`, `version: 0.0.1`
- server: **`https://api.juit.io/v1/data-products/search`**
- auth: **`basicAuth`** (HTTP Basic). Sem OAuth, sem API-key header.
- **2 endpoints. Só isso.**

```
GET /jurisprudence                      "Esse endpoint realiza a busca na base
                                         da JUIT e retorna 10 jurisprudências."
GET /jurisprudence/{juit_id}/artifact   "Download de um artefato específico"
                                        → application/pdf | rtf | docx
```

**Unidade de resposta = o DOCUMENTO DECISÓRIO**, não o processo e não o
andamento. É a diferença conceitual inteira deste estudo.

Parâmetros de `GET /jurisprudence` (nomes literais do spec):

| Param | Obrig. | Valores / nota |
|---|:--:|---|
| `query` | ✔ | ≤512 chars. "Suporta operadores `E`, `OU`, `MASNAO`, `PARÊNTESES` e busca por termo exato com aspas." |
| `owner` | ✔ | e-mail de quem consulta (auditoria por usuário final) |
| `search_on` | ✔ | `title` \| `headnote` \| `full_text` — **onde buscar** |
| `disable_synonym_on` | | mesmos campos. **Sinônimo é ligado por padrão** |
| `search_id` | | UUID devolvido pela API; amarra a sessão de busca |
| `sort_by_field` | | `score` \| `order_date` \| `juit_id` (default `[score, juit_id]`) |
| `sort_by_direction` | | `asc`\|`desc` |
| `next_page_token` | | paginação por token opaco |
| `order_date`, `judgment_date`, `publication_date`, `release_date`, `signature_date` | | `YYYYMMDD` com `$gt`/`$gte`/`$lt`/`$lte` — **cinco datas distintas** |
| `court_code` | | array |
| `degree` | | `1ª Instância`\|`2ª Instância`\|`Tribunal Superior`\|`Administrativo` |
| `process_origin_state` | | enum das 27 UFs |
| `district` | | **comarca** |
| `document_matter_list` | | "Assuntos do CNJ (TPU)" |
| `process_class_name_list` | | classe |
| `judgment_body` | | órgão julgador (ex.: `20ª Câmara de Direito Privado`) |
| `trier` | | **relator/magistrado** |
| `document_type` | | `Acórdão`, `Decisão Monocrática`, `Sentença`, `Decisão`, `Despacho`, `Admissibilidade`, `Dúvida de Competência`, `Nâo identificado` *(sic)* |
| `justice_type` | | `Juízo Comum` \| `Juizado Especial` |

Resposta:

```json
{"next_page_token":"WzEyMzQsMTIzNF0=", "total":500, "size":10,
 "search_info":{"search_id":"18fa6ff2-...","elapsed_time_in_ms":872},
 "items":[{ "id":"ffff45f6c298edf77f960b20c3468926",
   "juit_id":"ffff45f6c298edf77f960b20c3468926",
   "cnj_unique_number":"2037843-28.2024.8.26.0000", "court_code":"TJSP",
   "degree":"2ª Instância", "district":"Avaré",
   "document_matter_list":["cartão de crédito"], "document_type":"Acórdão",
   "full_text":"Texto da íntegra ...",
   "headnote":"TUTELA DE URGÊNCIA - Ação de obrigação de fazer ...",
   "judgment_body":"20ª Câmara de Direito Privado",
   "process_class_name_list":["Agravo de instrumento"],
   "process_origin_state":"SP",
   "judgment_date":"2024-05-07T00:00:00Z", "publication_date":"...",
   "release_date":"...", "signature_date":"...", "order_date":"...",
   "title":"TJSP / Acórdão / 2037843-28.2024.8.26.0000",
   "trier":"Correia Lima", "justice_type":"Juízo Comum",
   "rimor_url":"https://rimor2.juit.io/busca_jurisprudencia/ffff45f6...",
   "artifacts":[{"checksum":"d41d8...","collected_date":"2024-10-01T20:23:17Z",
                 "document_type":"Acórdão","is_original":true,
                 "mime_type":"application/pdf",
                 "filename":"00005325220168130332_20241001.pdf"}]}]}
```

Sonda do endpoint vivo (30/08/2026, sem credencial):

```
GET https://api.juit.io/v1/data-products/search/jurisprudence  →  401 application/json
  {"status":401,"error":"unauthorized","message":"missing credentials",
   "details":{"trace_id":"B-E-21766660345302786400",
              "short_message":"Não autorizado."}}
GET https://api.juit.io/                                       →  404 text/plain
DNS: api.juit.io → juit-production-api-gateway-alb-...sa-east-1.elb.amazonaws.com
     api.juit.dev → NXDOMAIN   (o host do exemplo da landing NÃO existe;
                                o host real, do spec, é api.juit.io)
```

Limites do spec: `size` máx **50**, default 10. `total` é inteiro sem
`relation` — declaram total exato. Erros `400/403/404/500` com
`{status,error,message,details.trace_id}`; o `trace_id` tem prefixo de camada
(`B`ackend/`F`rontend/`D`atabase) — detalhe que denuncia backend em MySQL
(`UUID_SHORT()` citado no spec).

### 1.3 O que NÃO está no contrato

Nada de: busca vetorial/semântica exposta, embeddings, classificação de
resultado (procedente/improcedente), extração de tese, citação entre julgados
(`cited_by`), súmula, jurimetria agregada, webhook/monitoramento, bulk/dump.
A palavra "RAG" aparece só no marketing — o produto entregue é **busca
lexical com sinônimo + filtros ricos + inteiro teor + PDF original**.

### 1.4 O mercado — e o achado competitivo

Verificação externa em 30/08/2026, com URL por afirmação.

| Player | Entrega inteiro teor via API? | Cobertura declarada | Preço público |
|---|---|---|---|
| **JUIT** | ✔ é o argumento central | "92 tribunais, +70 milhões de decisões" | **não** — só `comercial@juit.io` |
| **Jusbrasil** | ❌ **declara que não** | "96 tribunais", "500M+ processos" | ✔ "**a partir de R$ 1.000/mês**" |
| **Turivius** | ✔ anuncia "ementa, inteiro teor" | "+130 milhões de decisões" | **não tem API** (`turivius.com/api` → 404) |
| **jurisprudencias.ai** | ⚠️ só **ementa** (`summary`) | "16 tribunais e bases", "+20M decisões" | ✔ R$ 599/ano · R$ 59,90/mês |
| **Escavador / Digesto** | ❌ (PDF de autos) | — | não (Digesto virou "Jusbrasil Soluções") |
| **Judit.io** (≠ JUIT) | ❌ consulta processual | "+450 milhões de processos" | ✔ tabela completa por transação |
| **DataJud (CNJ)** | ❌ só metadados | 182 tribunais | gratuito, **com trava comercial** |

O achado do quadro está na segunda linha:

> **"A API fornece acesso à base de jurisprudência ou ao Jus IA? A API é
> voltada a dados estruturados de processos e monitoramento jurídico. **Não
> disponibiliza jurisprudência** nem Jus IA via API."**
> — `conteudo.jusbrasil.com.br/api`, FAQ oficial

**O maior player do Brasil declara que não vende jurisprudência por API.** O
único concorrente que anuncia inteiro teor (Turivius, "+130 milhões") **não tem
API**. Ou seja: a JUIT ocupa uma faixa que quase ninguém disputa — e a disputa
não é por volume de processo, é por **texto de decisão com ementa**.

🔴 **E há uma trava jurídica que morde direto o nosso maior ativo:**

> "3.3. A API é fornecida exclusivamente para fins legais, **não comerciais** e
> autorizados" / "3.8. O usuário concorda em não modificar, distribuir,
> **vender ou explorar comercialmente** a API ou qualquer informação derivada
> dela" — `datajud-wiki.cnj.jus.br/api-publica/termo-uso`, reafirmado na
> **Portaria CNJ nº 374, de 14/08/2026**

Os **344.603.487** docs do `voyager-acervo` vieram dessa porta. Qualquer
produto comercial construído sobre eles precisa passar pelo jurídico **antes**
da engenharia. Não é um detalhe de rodapé.

---

---

## 2. O que o Voyager tem hoje (medido em 2026-08-30)

Acesso: `ssh 100.98.141.91` → `docker exec -i voyager-worker_monitoring-1 python -`.
ES em `192.168.30.128:9200`, 1 nó (`voyager-es-01`), cluster **yellow**,
disco **66% usado / 1.000,9 GB livres de 2,9 TB**.

### 2.1 Volume

```python
es.count(index=idx, request_timeout=120)["count"]     # _count, nunca _cat
```

| Índice | `_count` | store (primaries) | bytes/doc |
|---|---:|---:|---:|
| `voyager-movimentacoes` (-v2) | **1.551.928.735** | 1.787,0 GB | 1.151 |
| `voyager-processos` | **103.707.711** | 62,6 GB | 307 |
| `voyager-acervo` (esqueleto Datajud) | **344.603.487** | 121,3 GB | 352 |
| `voyager-entidades` | 1.131.058 | — | — |

Postgres (`pg_stat_user_tables`): `tribunals_movimentacao` 1.549.783.784 ·
`tribunals_process` 103.613.250 · `tribunals_processoparte` 99.925.767 ·
`tribunals_parte` 25.462.255 · **`pdf_storage_pdfarquivo` = 0 linhas**
(nunca baixamos um PDF).

### 2.2 Cobertura por tribunal

`terms` agg em `tribunal`: **59 tribunais** em `voyager-processos` e os mesmos
59 em `voyager-acervo`.

```
27 TJs  +  24 TRTs  +  6 TRFs  +  STJ  +  TST   = 59
faltando p/ os 92 da JUIT: STF, TSE, STM, 27 TREs, 3 TJMs = 33
```

Top-5 movimentações: TJMG 314.109.260 · TRF3 152.570.074 · TJSP 152.542.499 ·
TJDFT 89.686.382 · TJGO 56.647.235.

### 2.3 Profundidade histórica

`date_histogram` anual em `publish_date` (índice inteiro):

```
1999    333.130      2016   15.715.001      2023  153.495.874
2005  1.546.838      2018   26.075.501      2024  229.589.386
2010  5.830.487      2020   40.355.777      2025  480.307.791
2014 11.311.349      2022  107.386.769      2026  296.263.007 (até 30/08)
```

Série longa existe (há linhas até 1899, e ~1,2 mil com ano `2400` — lixo de
data), mas a massa é recente: **2023-2026 somam 1.159.656.058 = 74,7% do índice**.

E, para o que interessa aqui, a profundidade **desaba**:

| ano | movimentações tipadas como `Acórdão` |
|---|---:|
| ≤2020 | 1.586 |
| 2021 | 4.077 |
| 2022 | 41.390 |
| 2023 | 62.456 |
| **2024** | **1.799.721** |
| **2025** | **3.859.726** |
| **2026** | **2.527.037** |

*(esta tabela e o recorte por tribunal do §3.4 usam o conjunto
`Acórdão` de 8 grafias — universo 8.296.769. O §3.1 usa 11 grafias, incluindo
`Ementa`, `Relatório e Voto` e `Voto` — universo 8.812.885. Os dois estão
declarados para que ninguém compare denominadores diferentes.)*

**98,7% do acórdão tipado que temos é de 2024 em diante.** Isso não é acervo
histórico de jurisprudência; é o fluxo dos últimos 30 meses.

### 2.4 Cobertura HONESTA de campo (a armadilha do `exists`)

`exists` conta string vazia como preenchido. Medido dos dois jeitos, com
**`proc` como campo de controle** (tem que dar 100% — deu):

`voyager-movimentacoes` (1.551.928.735):

| campo | `exists` | vazio | **real** |
|---|---:|---:|---:|
| `proc` *(controle)* | 100,00% | 0 | **100,00%** ✔ |
| `tipo_comunicacao` | 100,0% | 28.563.773 | 98,16% |
| `classe_nome` | 100,0% | 144.068 | 99,99% |
| `nome_orgao` | 100,0% | 167.808 | 99,99% |
| **`tipo_documento`** | 25,25% | **128.696.777** | **16,96%** |
| `cnjs_citados` | 2,22% | — | 2,22% |

`voyager-processos` (103.707.711):

| campo | `exists` | vazio | **real** |
|---|---:|---:|---:|
| `proc` *(controle)* | 100,00% | 0 | **100,00%** ✔ |
| `uf` | 100,0% | 0 | 100,00% |
| `grau` | 100,0% | 21.757.251 | 79,02% |
| `classe_nome` | 100,0% | 66.937.390 | **35,46%** |
| `codigo_classe` | 100,0% | 67.507.952 | 34,91% |
| `orgao_julgador` | 100,0% | 81.103.805 | **21,80%** |
| `assunto_codigo` | 100,0% | 90.002.466 | **13,22%** |
| `juizo` | 100,0% | 100.526.891 | 3,07% |

Partes: `nested participacoes` com ≥1 filho em **19.654.404 (18,95%)**.
*(O flag `enriquecido=True` marca 99,63% — ele é derivado de 4 datas de
enriquecimento e **não prova parte**. Quem ler `enriquecido` como cobertura
lê 99,63% e erra por 5×.)*

### 2.5 Grau — o container do acórdão

`terms` em `grau`:

| grau | `voyager-acervo` (Datajud, país) | `voyager-processos` (nosso) | temos |
|---|---:|---:|---:|
| G1 | 205.285.823 | 55.654.242 | 27,1% |
| JE | 74.473.109 | 20.285.995 | 27,2% |
| **G2** | **42.223.941** | **5.075.240** | **12,02%** |
| SUP | 8.178.993 | 338.429 | 4,14% |
| TR | 14.371.828 | 595.397 | 4,14% |
| *(vazio)* | — | 21.757.251 | — |

**Segunda instância — onde nasce o acórdão — é justamente nosso pior grau.**

---

## 3. A medição central: quanto de *jurisprudência* nós temos

### 3.1 O universo tipado

`terms` agg em `tipo_documento`, `size=2000` (3,8 s, `sum_other_doc_count`
= 2.962, `doc_count_error = 0`). Agrupando as grafias por família:

| família (grafias agrupadas) | movimentações |
|---|---:|
| Decisão (`Decisão`, `DESPACHO/DECISÃO`, `DECISÃO/DESPACHO`, …) | 21.462.601 |
| Despacho | 13.602.409 |
| **Acórdão** (`Acórdão`, `ACORDAO`, `Ementa`, `EMENTA / ACORDÃO`, `Relatório e Voto`, `Voto`, `Intimação de acórdão`…) | **8.812.885** |
| **Sentença** (12 grafias) | **8.214.866** |
| Decisão Monocrática | 1.715.715 |
| **soma "decisórios"** | **53.808.476** (3,47% do índice) |
| `tipo_documento` = `''` | 128.696.777 |
| sem `tipo_documento` | 1.160.006.468 |

O vocabulário é sujo por construção — cada tribunal escreve o que quer:
`Acórdão`, `ACORDAO`, `ACÓRDÃO SEGUNDO GRAU`, `Acórdão (expediente)`,
`Decosão Monocrática` *(sic)*, `80`, `Despacho_Intimação_Intimação (Outros)`.
Normalizar isso é trabalho de tabela de-para, não de regex esperta.

### 3.2 O texto: é inteiro teor ou é intimação?

Campo `text` só se mede por amostra (regra nº 4 do CLAUDE.md). Amostra
aleatória uniforme via `function_score` + `random_score(field=_seq_no)`,
**n = 1.000 por família** (4 × 250, seeds distintos), controle `proc`
preenchido em **1.000/1.000 em todas as famílias**.

Definição operacional de **"inteiro teor plausível"**:
`len(body) ≥ 5.000 chars` **E** contém pelo menos um marcador de julgado
(`ementa` | `vistos, relatad` | `acordam` | `ante o exposto` | `isto posto` |
`julgo procedente/improcedente/extinto` | `nego/dou provimento`).
É um **teto**, não um piso: um trecho de 5k com "ante o exposto" pode ser só o
dispositivo publicado, não a íntegra.

| família | universo | `p50` do `body` | **≥5k + marcador** | contém "ementa" | **teor estimado** |
|---|---:|---:|---:|---:|---:|
| Acórdão | 8.812.885 | 1.752 | **25,2%** | 35,0% | **2.220.847** |
| Sentença | 8.214.866 | 1.658 | 24,0% | 7,7% | 1.971.567 |
| Dec. Monocrática | 1.715.715 | **11.895** | **70,4%** | 36,8% | 1.207.863 |
| Decisão | 21.462.601 | 1.921 | 8,5% | 3,0% | 1.824.321 |
| Despacho | 13.602.409 | 886 | 1,6% | 0,8% | 217.638 |
| **subtotal tipado** | **53.808.476** | | | | **7.442.236** |
| sem `tipo_documento` | 1.160.006.468 | 740 | 2,0% | 1,2% | 23.200.129 |
| `tipo_documento` vazio | 128.696.777 | 20 | 0,0% | 0,0% | 0 |
| **TOTAL** | 1.551.928.735 | | | | **≈ 30,6 milhões** |

Erro de amostragem a n=1.000: ±~2,7 pp (95%) → o total de 30,6M carrega uma
faixa de ±~30M nos untyped… não: a faixa é ±2,7 pp *sobre 1,16 bi* =
**±31 milhões**. Ou seja: **a estimativa dos untyped (23,2M) é a mais frágil
do documento** e está na §8.

### 3.3 A prova por frase (exata, não amostral)

`match_phrase` no `body` restrito ao conjunto Acórdão-tipado (aqui com 8
grafias, universo **8.296.769**) e Sentença-tipada (**8.150.606**):

| frase no `body` | em Acórdão | % | em Sentença | % |
|---|---:|---:|---:|---:|
| `ementa` | 2.537.520 | **30,6%** | 687.814 | 8,4% |
| `vistos relatados e discutidos` | 826.699 | **10,0%** | 21.459 | 0,3% |
| `acordam os desembargadores` | 497.486 | 6,0% | 8.789 | 0,1% |
| `ante o exposto` | 724.720 | 8,7% | 2.835.039 | 34,8% |
| `nego provimento` | 914.953 | 11,0% | 35.020 | 0,4% |
| **`poderá ser acessado`** | **1.216.972** | **14,7%** | 353 | 0,0% |
| `cujo teor poderá ser acessado` | 479.346 | 5,8% | 2 | 0,0% |

A última linha é o retrato do problema. Um acórdão real, colhido na amostra:

```
--- TRT23  0000950-80.2022.5.23.0031  2025-09-04   len=620
PODER JUDICIÁRIO ... 1ª TURMA  Relator: TARCISIO REGIS VALENTE
AP 0000950-80.2022.5.23.0031  AGRAVANTE: ... AGRAVADO: ...
Ficam as partes intimadas do acórdão proferido nos autos do processo
Agravo de Petição 0000950-80.2022.5.23.0031, cujo teor poderá ser
acessado pelo site: https://pje.trt23.jus.br/segundograu/login.seam
```

**620 caracteres. O acórdão não está aqui — está atrás de um login.**
Isso é o DJEN funcionando exatamente como foi desenhado: ele é veículo de
*comunicação* (Res. CNJ 455/2022), e a comunicação de um acórdão é o aviso de
que ele existe.

`match_phrase` no índice inteiro, sem filtro de tipo:
`ementa` → **14.843.444** · `vistos relatados e discutidos` → **4.056.693**
(25,8 s) · `acordam os` → **5.429.911** (35,9 s). Esse é o **pool bruto** de
onde uma extração de ementa poderia sair.

### 3.4 Quantos processos, e de quem

- Processos **distintos** com ≥1 movimentação tipada como acórdão:
  `cardinality(proc, precision_threshold=40000)` → **4.455.157** (9,2 s).
- Acórdão-tipado por tribunal (top 10): TJSP 1.714.324 · TRT2 585.575 ·
  TRT3 577.017 · TST 510.533 · TRT15 468.092 · TRT1 460.016 · TRT4 374.638 ·
  TRT5 364.392 · TRT9 339.800 · TRT6 282.563. **STJ 141.610. STF: zero.**

O perfil é *trabalhista + TJSP*. Uma "API de jurisprudência" construída sobre
isso hoje seria uma API de jurisprudência do TRT.

### 3.5 A ementa É delimitável — medido, não suposto

Antes de dizer "não temos ementa" e parar, testei se ela é **recortável** do
`body` que já está no disco. Amostra de **n = 1.000 acórdãos que contêm a
palavra "ementa"** (controle `proc`: 1.000/1.000), recorte por regex pura:

```
abre  = \bEMENTA\b[:\-–]?
fecha = ACORDAM | ACÓRDÃO | RELATÓRIO | VISTOS, RELATAD | VOTO
```

| resultado | n | % |
|---|---:|---:|
| **ementa recortada, ≥120 chars** | **888** | **88,8%** |
| bloco entre marcadores curto demais (<120) | 108 | 10,8% |
| sem marcador de fechamento | 4 | 0,4% |

Tamanho da ementa extraída: `p10 = 309` · `p50 = 869` · `p90 = 3.248` ·
`max = 9.206` caracteres. Exemplo (TRT7, `0002203-88.2024.5.07.0028`):

```
DIREITO PROCESSUAL DO TRABALHO. AGRAVO DE PETIÇÃO DO EXEQUENTE. EXECUÇÃO
TRABALHISTA. SALDO REMANESCENTE DE CRÉDITO. AUSÊNCIA DE DECISÃO FORMAL DE
EXTINÇÃO. VIOLAÇÃO AO CONTRADITÓRIO. PROSSEGUIMENTO DA EXECUÇÃO. RECURSO
PROVIDO. I. CASO EM EXAME  1. Agravo de petição interposto pela parte ...
```

**Dimensionamento:** 8.812.885 acórdãos × 35,0% que contêm "ementa" ≈ **3,08M**;
× 88,8% delimitáveis ≈ **~2,7 milhões de ementas**, por regex determinística,
com **zero requisição a tribunal**. O pool bruto do índice inteiro é maior:
14.843.444 movs contêm a frase "ementa".

E o `trier` (relator)? Amostra independente de n = 1.000 acórdãos
(controle 1.000/1.000): **67,1%** trazem o marcador `Relator:` / `Relatora:` /
`Rel.` seguido de nome. Mas a sonda ingênua leva junto o que vem depois —
`'BRASILINO SANTOS RAMOS  ROT 0000871-97.2022.5.10.0'` — porque **não há
terminador**: o cabeçalho da intimação emenda relator, classe e CNJ numa linha
só. O sinal existe em 2/3 dos acórdãos; o recorte limpo é trabalho de extrator,
não de regex.

Esses são os únicos números deste documento que dizem "dá pra fazer hoje".

### 3.6 A porta que nunca abrimos: `docurl`

Amostra de 400 acórdãos: **374 (93,5%) têm `docurl` preenchido** — e ele aponta
para o documento no tribunal, não para o diário:

```
pje.trt3.jus.br/pjekz/validacao/24100309153024100000118207984?instancia=2
pjett.trf5.jus.br/pje/Processo/ConsultaDocumento/listView.seam?x=2607...
www.dje.tjsp.jus.br/...          projudi.tjgo.jus.br/...
```

Sonda (GET público, sem login): `HTTP 200`, mas devolve a **SPA Angular** do
PJe-KZ; o documento vem de uma chamada JSON interna
(`/pje-comum-api/...` responde `404 application/json`, ou seja, o gateway
existe e a rota é outra). **A porta existe e nunca foi tentada** —
`pdf_storage_pdfarquivo` tem **0 linhas**.

### 3.7 Latência (importa para vender API)

`match_phrase` no `body` + filtro `terms tipo_documento` (acórdão),
`track_total_hits=True`, `size=10`, com highlight:

| consulta | total | frio | morno |
|---|---:|---:|---:|
| `dano moral` | 569.557 | 18,84 s | 1,75 s |
| `repercussão geral` | 517.653 | 8,78 s | 1,05 s |
| `cumprimento de sentença contra a fazenda` | 4.960 | 29,59 s | **27,95 s** |

A JUIT devolve `elapsed_time_in_ms: 872` no exemplo do próprio spec.
**Buscar jurisprudência dentro de um índice de 1,55 bi de andamentos é o
problema errado** — a frase rara é a mais cara porque varre tudo. Um índice
dedicado de ~30M docs muda a ordem de grandeza sozinho.

---

## 4. Tabela de aderência

Campo a campo do schema `Jurisprudence` da JUIT contra o que temos:

| Capacidade JUIT | Temos? | Evidência numérica (30/08/2026) |
|---|---|---|
| **Unidade = documento decisório** | ⚠️ parcial | nossa unidade é `Movimentacao` (publicação). 53.808.476 têm tipo decisório; 16,96% do índice tem `tipo_documento` real |
| `full_text` — **inteiro teor** | ❌ **não** | acórdão: 25,2% ≥5k+marcador (n=1.000) ⇒ ~2,22M de 8,81M. 14,7% do acórdão-tipado *diz explicitamente* que o teor está no tribunal |
| `headnote` — **ementa** | ❌ **não** — mas **extraível** | **campo não existe** em nenhum mapping. 30,6% dos acórdãos contêm "ementa" no corpo e **88,8% deles são recortáveis por regex** (n=1.000) ⇒ **~2,7M ementas** disponíveis sem rede (§3.5) |
| `document_type` (8 valores) | ⚠️ parcial | temos o conceito, com vocabulário sujo: 2.000 grafias, `tipo_documento` real em 16,96% |
| `court_code` | ✔ sim | `tribunal`, 100%. Mas **59 de 92** tribunais (64,1%) |
| `cnj_unique_number` | ✔ sim | `proc`, **100,00%** (campo de controle) |
| `process_origin_state` | ✔ sim | `uf` 100% em processos; em movs é derivável do `tribunal` (`search/geo.py`) |
| `degree` | ⚠️ parcial | `grau` real em 79,02% dos processos; G2 = 5.075.240 = **12,02%** do G2 nacional |
| `judgment_body` (órgão julgador) | ⚠️ parcial | `Process.orgao_julgador` real em **21,80%**; em movs só `nome_orgao` = órgão que *publicou* |
| `trier` (**relator**) | ❌ **não** | campo inexistente. Marcador `Relator:` presente em **67,1%** dos acórdãos (n=1.000), mas sem terminador — exige extrator. Única fonte já estruturada: DJe do STF (`.ia/DIARIOS.md`), não ligado |
| `district` (comarca) | ❌ **não** | campo inexistente |
| `judgment_date` / `signature_date` | ❌ **não** | só `publish_date` e `available_at` |
| `publication_date` / `release_date` | ✔ sim | `publish_date` / `available_at`, 100% |
| `document_matter_list` (TPU) | ⚠️ fraco | `assunto_codigo` real em **13,22%** dos processos |
| `process_class_name_list` | ⚠️ misto | 99,99% nas movs, **35,46%** nos processos |
| `justice_type` | ⚠️ derivável | `grau=JE` (20.285.995) aproxima "Juizado Especial" |
| `artifacts` (PDF/RTF/DOCX original) | ❌ **não** | `PdfArquivo` = **0 linhas**. Mas 93,5% dos acórdãos têm `docurl` |
| Busca full-text com operadores | ✔ sim | `/api/v1/busca/movimentacoes/?q=` (ES `match` AND + highlight). Falta `OU`/`MASNAO`/parênteses |
| Sinônimo ligável/desligável por campo | ❌ não | sem dicionário de sinônimo no analyzer |
| Filtro por 5 datas independentes | ❌ não | temos 1 (`publish_date`) |
| Paginação por token | ⚠️ equivalente | `next_cursor` (`search_after`) já existe |
| `total` exato | ⚠️ | hoje a API devolve `{"value":10000,"relation":"gte"}`; `track_total_hits` resolve (medido: 1,05–27,95 s) |
| Atualização diária | ✔ **sim, e melhor** | 296.263.007 movs só de 2026; ingestão contínua em 59 tribunais |
| Cobertura nacional de *processos* | ✔ **temos algo que a JUIT não anuncia** | `voyager-acervo`: **344.603.487** processos do país (esqueleto Datajud) |
| Partes/advogados/OAB | ✔ **fora do escopo da JUIT** | 99.925.767 `ProcessoParte`; nested em 18,95% dos processos |

---

## 5. A lacuna real — sem álibi

### 5.1 O número

```
                    JUIT declara            Voyager mede (30/08/2026)
                    ─────────────           ─────────────────────────
decisões            "+70 milhões"           53.808.476 tipadas como decisórias
com inteiro teor    "conteúdo integral"     ~7,4M no tipado / ~30,6M no total (teto)
acórdãos c/ teor    —                       ~2.220.847
ementa (headnote)   campo de 1ª classe      0 indexado — ~2,7M extraíveis (§3.5)
relator             campo de 1ª classe      0 indexado — marcador em 67,1%
comarca             campo de 1ª classe      0  (não existe em fonte nenhuma nossa)
tribunais           92                      59            (64,1%)
2ª instância        —                       12,02% do G2 nacional
histórico           não publicado           98,7% do acórdão tipado é 2024+
```

**Em uma frase honesta:** se alguém pedisse hoje "me devolve o acórdão inteiro
com ementa e relator", nós atenderíamos **~2,2 milhões de vezes** — e hoje sem
a ementa e sem o relator, porque nenhum dos dois campos existe (embora a ementa
seja extraível, §3.5). Contra "+70 milhões de decisões" com "conteúdo
integral", **isso é ~3,2%**. E mesmo esses 2,2M não
estão delimitados: o inteiro teor está *misturado* ao cabeçalho da intimação,
dentro de um `body` único.

### 5.2 Por que é assim (e por que não é bug)

Não é falha de ingestão. É a **porta**:

```
DJEN  ──► "Ficam as partes intimadas do acórdão ... cujo teor poderá
          ser acessado pelo site https://pje.trtXX.jus.br/..."
                                    │
                                    └──► o acórdão mora AQUI, e nunca fomos lá

Datajud ─► classe, assunto, órgão, movimentos.  SEM texto de decisão.
           (344.603.487 processos de esqueleto — nosso maior ativo, e mudo)

Diários próprios (3ª porta) ─► texto verbatim do caderno.
           tjsp-dje ligado com orçamento de 8 unidades/24h;
           dejt fora do ar; stf não ligado.   diarios_edicaodiario = 379 linhas
```

As três portas que temos trazem **comunicação e cadastro**. Jurisprudência
está numa **quarta porta que nunca abrimos**: os *bancos de jurisprudência*
dos tribunais e os documentos apontados pelo `docurl`.

#### E não é acidente — está na norma

A medição do §3.2 (25,2% dos acórdãos com teor, 24,0% das sentenças) tinha
cara de falha de coleta. **Não é.** A Resolução CNJ 455/2022, que criou o DJEN,
diz exatamente o que entra:

> "Art. 13. Serão objeto de publicação no DJEN: I – o conteúdo dos despachos,
> das decisões interlocutórias, **do *dispositivo* das sentenças e da *ementa*
> dos acórdãos**, conforme previsão do § 3º do art. 205 do CPC/2015"
> — `atos.cnj.jus.br/atos/detalhar/4509`

E a própria spec do DJEN nomeia o campo pelo que ele é: `texto` = "**Teor da
Comunicação**"; `link` = "Link para o inteiro teor **da comunicação**"
(`comunicaapi.pje.jus.br/swagger/djen.yml`).

Três consequências que valem mais que qualquer estimativa:

1. **Inteiro teor de acórdão pelo DJEN é impossível por desenho.** Nenhum
   volume de engenharia de ingestão resolve. Parar de tentar é economia real.
2. **A ementa do acórdão, ao contrário, é o que a lei manda publicar.** Isso
   explica por que 88,8% delas são recortáveis (§3.5) — e transforma o
   Caminho 2 de "ideia esperta" em **colher o que a norma mandou entregar**.
3. **Da sentença, o esperado é o dispositivo** — casa com os 34,8% de
   "ante o exposto" e os 0,3% de "vistos relatados e discutidos" do §3.3.

### 5.3 O que custaria — e o mapa de portas (verificado em 30/08/2026)

Antes de orçar scraper, vale saber **quais portas estão abertas**. Foram
sondadas uma a uma:

| Fonte | Coleta por HTTP simples? | Obstáculo medido | O que dá |
|---|---|---|---|
| 🟢 **STJ — dados abertos (CKAN)** | ✔ **melhor do país** | nenhum | **íntegras diárias, licença CC-BY** |
| 🟢 **TJSP 1º grau (CJPG)** | ✔ **sem captcha** | exige termo textual (não varre por data pura) | **sentença inteira no HTML** |
| 🟡 **STM** | ✔ GET puro, sem captcha | `robots.txt: Disallow: /` | ementa + inteiro teor |
| 🟡 **TRF2 / TRF4 (eproc)** | ✔ | — | "Inteiro teor" / "Caput da Ementa" |
| 🟡 **TJRS** | ⚠️ parcial | resultados via AJAX/Angular; acervo antigo em **TIFF** | íntegra de 2º grau |
| 🔴 **TJSP 2º grau (CJSG)** | ❌ | **reCAPTCHA v3** | — |
| 🔴 **TJMG** | ❌ | captcha de imagem, HTTP 401 | — |
| 🔴 **TJRJ** | ❌ | reCAPTCHA v3 | — |
| 🔴 **CJF (TRF1–6 unificada)** | ❌ | reCAPTCHA + JSF ViewState | — |
| 🔴 **STF** | ❌ | **AWS WAF challenge** no host inteiro | — |
| 🔴 **TSE** | ❌ | hCaptcha | — |
| 🔴 **TST + 24 TRTs (FALCÃO)** | ❌ | **HTTP 401** na API; robots `Disallow: /` | — |

**O achado que muda a conta — o STJ publica inteiro teor em dados abertos:**

> "Íntegras de Decisões Terminativas e Acórdãos do Diário da Justiça — Este
> conjunto de dados contém **as íntegras e metadados das decisões terminativas
> e acórdãos** publicados no Diário da Justiça. **As atualizações são
> realizadas diariamente**." — `dadosabertos.web.stj.jus.br`,
> licença **Creative Commons Atribuição (cc-by)**, `isopen: true`,
> **2.578 recursos**, de `20210104` a `20260824`

E, ao lado, **10 datasets "Espelhos de acórdãos"** (por Turma/Seção) com os
campos que a JUIT vende e nós não temos, **já estruturados pela fonte**:
`ementa`, `teseJuridica`, `decisao`, `jurisprudenciaCitada`,
`referenciasLegislativas`, `acordaosSimilares`, **`ministroRelator`**,
`dataPublicacao`, `tema`. Um único arquivo conferido (3ª Turma, 30/06/2026)
tem **4.162 registros**.

Contraste que resume o Brasil: a **tela** do STJ (`scon.stj.jus.br`) está atrás
de Cloudflare e devolve 403; o **dado** está aberto sob CC-BY. Hoje temos
**141.610** movimentações tipadas como acórdão do STJ — e zero uso dessa porta.

O CNJ, aliás, **tem** o inteiro teor do país inteiro e não abre:

> "Art. 2º O Codex é alimentado com dados, metadados processuais e **inteiro
> teor dos documentos e atos proferidos** relativos a todos os processos
> eletrônicos" — Resolução CNJ 446/2022. Sem API pública.
> A Portaria CNJ 316/2024 abre uma via — e exige do interessado "capacidade
> técnica para gestão de identidade e controle de acesso de pelo menos
> **200 (duzentos) mil usuários únicos por mês**".

Com isso, o orçamento fica:

| Caminho | Volume-alvo | Custo grosso | Licença |
|---|---:|---|---|
| **0. Portas oficiais abertas (STJ CKAN + TJSP CJPG)** | STJ: 2.578 arquivos-dia; TJSP CJPG: 633.834 num só termo | **dias, não meses**. 1 coletor cada, zero captcha | **CC-BY** no STJ ✅ |
| **A. Puxar o `docurl` dos acórdãos** | 8,81M | ~8,3M GETs; 1 parser por sistema (PJe-KZ, e-SAJ, Projudi, eproc). **12 dos 13 portais grandes estão atrás de captcha/WAF** | cinza |
| **B. Extrair ementa/teor do `body` que já temos** | pool de 14,8M movs com "ementa" | **0 requisições** | é dado que já é nosso |
| **C. Colher os bancos de jurisprudência dos 92** | — | N parsers × manutenção eterna | cinza |
| **D. Comprar/licenciar** | 70M | preço **não publicado** | contrato |

## 6. Caminhos possíveis

### Caminho 0 — As portas oficiais que já estão abertas (STJ + TJSP CJPG)

**Esforço:** baixo. **Risco:** baixo. **Licença:** limpa (CC-BY no STJ).
**Não existia neste plano até a sondagem do §5.3.**

Dois coletores, no contrato de fonte que o `diarios/` já define
(`ColetorDiario`, `EdicaoDiario`, kill switch, orçamento diário):

```
STJ CKAN  ──► íntegras diárias (2021-01-04 → hoje, 2.578 arquivos-dia, CC-BY)
          └─► "Espelhos de acórdãos" (10 datasets) já trazem
              ementa · teseJuridica · ministroRelator · jurisprudenciaCitada
              ── ou seja: headnote + trier + citações, SEM extração nenhuma

TJSP CJPG ──► sentença INTEIRA no HTML, GET sem captcha
              (633.834 resultados num único termo de teste)
```

- **Desbloqueia:** os três campos que faltam (`headnote`, `trier`,
  `judgment_body`) num tribunal superior inteiro, **de graça e com licença
  explícita**; e inteiro teor de sentença no maior tribunal do país.
- **Limite honesto:** cobre STJ + 1º grau do TJSP. **Não** cobre os 92
  tribunais, e o STJ declara que "não estão disponíveis dados de processos em
  segredo de justiça".
- **Por que é o primeiro passo:** é o único caminho deste documento com
  licença de uso **escrita pela fonte**. Todos os outros são cinza — e o
  DataJud, que já usamos, é explicitamente **não comercial** (§1.4).

### Caminho 1 — Índice `voyager-julgados` (reprojeção do que já temos)

**Esforço:** médio (2-4 semanas). **Risco:** baixo. **Rede:** zero.

Novo índice ES, unidade = **documento decisório**, alimentado por reprojeção do
`voyager-movimentacoes` filtrado por `tipo_documento ∈ famílias decisórias`.

```
voyager-movimentacoes (1,55 bi, 1.787 GB)
        │  filtro: tipo_documento ∈ {acórdão, sentença, monocrática, decisão}
        │  + normalização de 2.000 grafias → 8 valores (de-para, como a JUIT)
        ▼
voyager-julgados  (~54M docs)
   proc · tribunal · uf · grau · document_type · publish_date · body
   + headnote  (extraída)      + trier (extraído)
   + judgment_body             + artifact_url (= docurl)
```

- **Desbloqueia:** latência (30M–54M docs em vez de 1,55 bi — a consulta rara
  cai de 27,95 s para a casa do segundo), filtros por tipo, e um contrato de
  API igual ao da JUIT em ~70% dos campos.
- **Custo de disco:** ~54M × ~4 KB ≈ **200-250 GB**. Cabe nos 1.000,9 GB
  livres, **mas concorre com os 772 GB que o backfill do DJE/TJSP projeta**
  (`.ia/DIARIOS.md` §13). Um dos dois espera, ou o nó cresce.
- **Não resolve:** inteiro teor de verdade (continua 25% dos acórdãos).

### Caminho 2 — Extração de `headnote` + `trier` do `body`

**Esforço:** BAIXO para a ementa, médio para o relator. **Risco:** baixo.
**Rede:** zero. **É o caminho com melhor razão entrega/custo do documento.**

Já medido no §3.5: **88,8%** dos acórdãos que contêm "ementa"
(n = 1.000, controle 1.000/1.000) têm a ementa **recortável por regex pura**,
entre `EMENTA` e `ACORDAM`/`RELATÓRIO`/`VOTO`, com `p50 = 869` chars.
Dimensionado: **~2,7 milhões de ementas** sem uma única requisição a tribunal.
Pool bruto do índice inteiro: 14.843.444 movs contêm "ementa";
4.056.693 contêm "vistos relatados e discutidos"; 5.429.911 contêm "acordam os".

O `trier` (relator) é o passo seguinte e mais sujo: o marcador está em
**67,1%** dos acórdãos (§3.5), mas sem terminador — a regex ingênua devolve
`BRASILINO SANTOS RAMOS  ROT 0000871-97.2022.5.10.0`. Aí entra o extrator que
já existe (Qwen QLoRA, frota de GPU, `.ia/EXTRACAO_ROADMAP.md` / `MODELOS.md`)
com a norma da casa: **abster > chutar**.

- **Desbloqueia:** o campo que mais define o produto da JUIT (`headnote`) e que
  hoje nós não temos *de jeito nenhum*.
- **Gate obrigatório:** amostra rotulada à mão antes de indexar. Ementa errada
  é pior que ementa ausente — regra nº 6 do CLAUDE.md.

### Caminho 3 — Abrir a quarta porta: `docurl` → inteiro teor

**Esforço:** alto. **Risco:** alto. **Rede:** ~8,3M requisições.

- **O mapa do §5.3 rebaixa este caminho:** dos 13 portais grandes sondados,
  **12 estão atrás de captcha, WAF ou HTTP 401** — STF (AWS WAF), TJSP-CJSG,
  TJMG, TJRJ, CJF, TSE (captcha), TST+24 TRTs (401). E é justamente no TST/TRTs
  que mora metade do nosso acórdão-tipado.
- Começar por **UM** tribunal com maior razão volume/atrito. TJSP tem 1,71M
  acórdãos e `docurl` no `dje.tjsp.jus.br` (mesmo domínio que o coletor
  `tjsp-dje` já sabe falar). Os TRTs somam ~4,5M mas caem no PJe-KZ.
- **Bloqueio provável:** disco. ~1,7 TB de PDF (com o 200 KB/doc **suposto**) contra 1,0 TB livre. Ou vai pro
  MinIO (que já existe, `pdf_storage`), ou não vai.
- **Regra da casa que se aplica direto:** teto é alerta, nunca corte mudo — um
  coletor de 8,3M documentos precisa declarar o que não conseguiu.

### Caminho 4 — Colher os bancos de jurisprudência dos tribunais

**Esforço:** muito alto (N parsers, manutenção perpétua). **Risco:** alto.

É o produto da JUIT. Entrega ementa **já delimitada pelo tribunal**, relator,
órgão julgador e comarca — os quatro campos que faltam — sem inferência.
É a única rota que fecha a lacuna *de verdade*, e é também a mais cara.

### Caminho 5 — Não construir

**Esforço:** zero.

O Voyager tem um produto que a JUIT **não** tem: 344.603.487 processos do país
com partes, advogados e OAB, e monitoramento push. A JUIT vende *pesquisa de
precedente*; nós vendemos *quem é a parte, quanto vale, e o que aconteceu
ontem*. São mercados vizinhos, não o mesmo.

---

## 7. Recomendação

**Fazer o Caminho 0, depois o 2, depois o 1. Não fazer o 4. Adiar o 3.**

Por quê, em ordem:

1. **O Caminho 0 é dinheiro no chão.** O STJ publica **as íntegras diárias
   sob CC-BY** desde 2021 e, ao lado, os "Espelhos de acórdãos" com `ementa`,
   `teseJuridica`, `ministroRelator` e `jurisprudenciaCitada` **já
   estruturados**. São exatamente `headnote`, `trier` e citações — os campos
   que a JUIT tem e nós não temos — de graça, sem captcha e com licença
   escrita. Hoje temos 141.610 acórdãos do STJ colhidos pela porta errada
   (a intimação) enquanto a porta certa está aberta. O TJSP CJPG é o
   segundo: sentença inteira por GET, sem captcha, 633.834 num só termo.

2. **O Caminho 2 usa um ativo já pago e o teste já passou.** 88,8% dos
   acórdãos com "ementa" no corpo têm ementa recortável **por regex**
   (n = 1.000, `p50` = 869 chars) ⇒ **~2,7 milhões de ementas** sem uma
   requisição a tribunal. E agora sabemos *por que* funciona: a Res. CNJ
   455/2022 art. 13 **manda publicar a ementa do acórdão no DJEN**. Não é
   sorte de parser — é colher o que a norma obriga a fonte a entregar.

3. **O Caminho 1 conserta um problema que já é nosso.** `/api/v1/busca/
   movimentacoes/` leva **27,95 s morno** numa frase rara e devolve
   `total: {"value": 10000, "relation": "gte"}`. Um índice de julgados
   (~54M em vez de 1,55 bi) conserta latência e contagem para *qualquer*
   consumidor, tenhamos ou não uma API de jurisprudência.

4. **O Caminho 4 é o produto de outra empresa, e o mapa fechou a porta.**
   Dos 13 portais grandes, **12 estão atrás de captcha/WAF/401** — incluindo
   TST e os 24 TRTs, onde mora metade do nosso acórdão-tipado. Replicar 92
   tribunais é assinar manutenção perpétua de N scrapers contra defesas
   ativas, pelo lado fraco (temos 12,02% do G2 nacional), enquanto o lado
   forte (344M de esqueleto + partes + OAB) segue subexplorado.

5. **O Caminho 3 fica bloqueado por disco antes da engenharia** (~1,7 TB de
   PDF *suposto* contra 1,0 TB livre, com o backfill do DJE/TJSP já
   reivindicando 772 GB do mesmo espaço) — e agora também por captcha.

### Duas coisas que precisam ir para o jurídico antes da engenharia

- 🔴 **DataJud é declaradamente não comercial** ("3.3 (…) fins legais, **não
  comerciais**"; "3.8 (…) não **vender ou explorar comercialmente** (…)
  qualquer informação derivada dela"). Os **344.603.487** docs do
  `voyager-acervo` vieram dali. Isso não invalida o uso interno, mas
  **precede** qualquer decisão de vender API.
- 🟡 **`robots.txt` diverge por tribunal.** `esaj.tjsp.jus.br` **não tem**
  robots.txt (404) e o `www.tjsp.jus.br` não bloqueia jurisprudência; já
  `jurisprudencia.tst.jus.br` e `jurisprudencia.stm.jus.br` publicam
  `Disallow: /`. Coletar do STM seria contrariar diretiva explícita — o STJ,
  não.

### O que eu NÃO recomendaria dizer, em hipótese alguma

Que temos "54 milhões de decisões". Temos 53.808.476 *publicações tipadas como
decisórias*, das quais ~7,4M carregam texto de julgado e **nenhuma** tem ementa
delimitada hoje. Vender esse número como jurisprudência é a assinatura das três
perdas do CLAUDE.md: **run verde, log limpo, número redondo.**

---

## 8. O que eu NÃO consegui medir (e por quê)

| # | O que ficou de fora | Por quê |
|---|---|---|
| 1 | **Preço, cota e rate limit da JUIT** | não publicado. Doc técnica é `protectedRoutes` (login); o spec OpenAPI que recuperei não declara `429` nem cabeçalho de quota. `size` máx 50 é o único teto explícito |
| 2 | **Profundidade histórica da base da JUIT** | nenhuma página informa desde que ano. Não dá para dizer se os "+70M" são 5 ou 25 anos |
| 3 | **Se os "+70M" são documentos ou processos** | o marketing diz "decisões" e a unidade da API é documento, mas não há gabarito para conferir dos dois lados (regra nº 5 do CLAUDE.md) |
| 4 | **Nossa fatia untyped (23,2M) tem erro grande** | amostra de n=1.000 sobre 1.160.006.468 docs ⇒ ±2,7 pp ⇒ **±31 milhões**. O número 30,6M do §3.2 é ordem de grandeza, não medida |
| 5 | **"Inteiro teor" é heurística, não prova** | `≥5.000 chars + marcador`. Não abri 1.000 documentos para conferir à mão se o texto é a íntegra ou o dispositivo. É um **teto** |
| 6 | **A rota JSON do PJe-KZ atrás do `docurl`** | `GET .../pjekz/validacao/<id>` devolve a SPA Angular (`HTTP 200`, 4.994 B). `/pje-comum-api/...` responde `404 application/json` — o gateway existe, a rota certa exige ler o bundle Angular. **Não medi o custo real do Caminho 3** |
| 7 | **`voyager-acervo.no_acervo`** | `term no_acervo:true` → 0 e `:false` → 0 nos 344.603.487 docs. O campo está no mapping e **não está populado** — não consegui cruzar "quantos do esqueleto nacional viraram processo de verdade" por esse caminho |
| 8 | **`ano_cnj` do `voyager-acervo`** | o `terms` desc devolve `9999`, `9900`, `9800`, `9700`… (23.369 docs em `9900`). São valores-lixo, não anos. Não montei a distribuição histórica do esqueleto |
| 9 | **Preço de API de Escavador, Digesto, Turivius, Predictus, Data Lawyer** | nenhum publica. Só Jusbrasil ("a partir de R$ 1.000/mês"), Judit.io e jurisprudencias.ai têm tabela pública |
| 9b | **Profundidade histórica de TODOS os concorrentes** | nenhum publica desde que ano cobre. Idem para o acervo declarado de qualquer tribunal (STF, STJ, TJSP, TJRJ, TJMG, CJF…) |
| 9c | **Volume real das portas abertas do Caminho 0** | conferi que o STJ CKAN tem 2.578 recursos e que UM arquivo-dia rende 4 íntegras e UM espelho rende 4.162 registros. **Não baixei a série toda** — não sei quantos acórdãos o Caminho 0 entrega ao final |
| 9d | **TRF3 (`web.trf3.jus.br/base-textual`)** | HTTP 000 / timeout na sondagem. **Não verificado** — pode ser porta aberta e eu não sei |
| 10 | **Custo de disco real do `voyager-julgados`** | ~200-250 GB é extrapolação de `bytes/doc = 1.151` do índice atual aplicada a um subconjunto com `body` maior que a média. Só um índice piloto de 1M docs mede isso |
| 11 | ~~A tabela de campos do DataJud~~ | **MEDIDO depois**: o Glossário oficial lista `id, tribunal, numeroProcesso, dataAjuizamento, grau, nivelSigilo, formato, sistema, classe, assuntos, orgaoJulgador, movimentos, dataHoraUltimaAtualizacao, @timestamp` — **nenhum campo de texto, documento, ementa, teor ou parte**. Confirmado também pela Portaria CNJ 160/2020, art. 4º (rol taxativo de 12 incisos) |
| 12 | **A referência técnica completa da JUIT** | `docs.juit.io/robots.txt` traz `Disallow: /pt/jurisprudence/`. Respeitei — o spec do §1.2 veio do bundle JS público, não das rotas fechadas. Pode haver parâmetro que eu não vi |
| 13 | **Se a licença CC-BY do STJ cobre uso comercial derivado** | CC-BY *permite* uso comercial com atribuição, mas não li os termos completos do portal nem consultei jurídico. **Não trate como parecer** |

---

## Apêndice — como reproduzir

```bash
ssh 100.98.141.91
docker exec -i voyager-worker_monitoring-1 python - <<'PY'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE","core.settings"); django.setup()
from search.client import get_es
es = get_es()

# 1. volume — SEMPRE _count, nunca _cat (nested mente)
es.count(index="voyager-movimentacoes", request_timeout=120)["count"]

# 2. cobertura HONESTA de keyword: exists MENOS string vazia
ex  = es.count(index="voyager-movimentacoes",
               query={"exists":{"field":"tipo_documento"}}, request_timeout=300)["count"]
vaz = es.count(index="voyager-movimentacoes",
               query={"bool":{"filter":[{"exists":{"field":"tipo_documento"}},
                                        {"term":{"tipo_documento":""}}]}},
               request_timeout=300)["count"]
real = ex - vaz          # controle: repetir com "proc" — tem que dar 100%

# 3. campo `text` só por AMOSTRA aleatória uniforme
es.search(index="voyager-movimentacoes", size=250, request_timeout=300,
          source_includes=["body","proc"],
          query={"function_score":{"query":{"terms":{"tipo_documento":["Acórdão"]}},
                                   "random_score":{"seed":1000,"field":"_seq_no"}}})
PY
```

Spec da JUIT (fonte primária, sem login):

```bash
curl -s https://docs.juit.io/assets/pt-jurisprudence-api-search-external.yaml-CF1PCWge.js.map \
 | python3 -c 'import json,sys; print(json.load(sys.stdin)["sourcesContent"][0])'
```
