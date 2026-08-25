# Schema canônico do Elasticsearch

> Além de `voyager-processos` e `voyager-movimentacoes`, existe
> **`voyager-acervo`** — o esqueleto nacional vindo do Datajud (343M docs
> previstos, sem parte/valor/movimento). Schema e o porquê do índice separado
> em [`ACERVO_CNJ.md`](ACERVO_CNJ.md) e `search/mappings.py::ACERVO_MAPPING`.

Fonte de verdade dos índices ES (VM dedicada `192.168.30.128:9200`, ES 8.19).
Postgres continua sendo a fonte de verdade dos DADOS; o ES é a camada de
leitura/busca/agregação (o Postgres de prod é contido — ver COMERCIAL_MAPA.md).

**REGRA DE OURO: não adicionar campo em doc builder sem (1) mapping em
`search/mappings.py`, (2) `PUT _mapping` aplicado em prod e (3) plano de
reindex/backfill.** O teste `tests/test_es_schema_partes.py::
test_mapping_*_cobre_todo_campo_do_doc` trava builder ⊆ mapping no CI.
Campo sem mapping vira dynamic mapping silencioso (tipo errado pra sempre).

## Índices

| Índice | Docs (2026-08-11) | Doc builder | Mapping |
|---|---|---|---|
| `voyager-processos` | 71,1M | `search/documents.py::processo_to_doc` | `PROC_MAPPING` |
| `voyager-movimentacoes` | 1,16B | `movimentacao_to_doc[_sem_partes]` | `MOV_MAPPING` |
| `voyager-entidades` | 1,14M (build 12/08) | `search/entidades.py::grupo_to_doc` (+ `n_processos` por `contar_processos_entidades`) | `ENTIDADE_MAPPING` |

> ### ⚠️ `_cat/indices` NÃO conta processos — conta documentos Lucene
>
> `voyager-processos` tem `participacoes` como **`nested`**, e no Lucene cada
> objeto aninhado é um documento próprio. Medido em 24/08/2026, no mesmo
> instante:
>
> | pergunta | resposta |
> |---|---:|
> | `_cat/indices` `docs.count` | **104.594.795** |
> | `_count` `match_all` | **87.709.209** |
> | diferença (filhos `nested`) | 16.885.586 |
>
> Naquele dia o Postgres tinha ~102 milhões de linhas em `tribunals_process`.
> Quem contasse pelo `_cat` leria "o índice tem MAIS processos do que o banco"
> e fecharia a investigação — enquanto **13,99% do acervo estava fora da
> busca**. É a regra nº 3 do CLAUDE.md (desconfie de número redondo) na forma
> mais cara: o número não inventa acervo, ele ESCONDE o buraco.
>
> **Toda contagem de processos usa `_count`.** O mesmo vale para
> `voyager-movimentacoes` (`assunto_norm` também é `nested`).

`_id` = pk do Postgres (idempotente). Formato compat Jusbrasil/Digesto.
Exceção: em `voyager-entidades` o `_id` é a chave canônica (`cnpj:29979036` /
`nome:<sha1>`) — não existe pk de "entidade" no Postgres.

## voyager-processos (schema de negócio)

| Campo | Tipo | Fonte (Process.*) | Caso de uso |
|---|---|---|---|
| id, source | long, integer | pk, FonteDiario | join/compat |
| tribunal, uf | keyword | tribunal_id, geo.py | agregação, mapa comercial |
| proc | keyword | numero_cnj | busca exata por CNJ formatado |
| proc_digits | keyword | derivado (só dígitos) | busca "colável" de CNJ sem máscara |
| classe_nome, codigo_classe | keyword | classe_* | filtro/agg por classe |
| assunto, assunto_codigo | text, keyword | assunto_* | busca/filtro TPU |
| advs, partes | text | concat de ProcessoParte | full-text compat (legado) |
| **participacoes** | **nested** | ProcessoParte+Parte | **busca estruturada por parte** (abaixo) |
| orgao_julgador, juizo | keyword | orgao_julgador_nome, juizo | filtro/agg por vara |
| valor_causa | double | valor_causa | faixa de valor (federal=NULL: fato de dado) |
| ano_cnj, data_autuacao | integer, date | idem | idade do processo |
| primeira/ultima_movimentacao_em, total_movimentacoes | date, int | agregados | jurimetria de duração/atividade |
| inserido_em | date | idem | crescimento da base |
| segredo_justica | boolean | idem | filtro |
| classificacao, classificacao_score, classificacao_versao, classificacao_em | keyword, double, keyword, date | idem | leads (confirmado), freshness, A/B de modelo |
| enriquecido, enriquecido_em, enriquecimento_status | boolean, date, keyword | derivado das 4 datas; status | cobertura honesta (nao_encontrado ≠ pendente) |
| tem_sinal_precatorio | boolean | idem | potencial (Fase 0) |
| tem_ente_publico_passivo | boolean | derivado (RE_ENTE_PUBLICO no passivo) | coração do precatório |

### participacoes (nested)

```
{parte_id: long, nome: text(+.raw keyword), documento: keyword,
 oab: keyword, tipo: keyword(pf|pj|advogado|desconhecido),
 polo: keyword(ativo|passivo|outros), papel: keyword(AUTOR|EXECUTADO|…),
 eh_advogado: boolean}
```

Exemplo — "processos onde João é EXECUTADO com valor > 1M em SP":

```json
{"query": {"bool": {"filter": [
  {"term": {"uf": "SP"}},
  {"range": {"valor_causa": {"gt": 1000000}}},
  {"nested": {"path": "participacoes", "query": {"bool": {"filter": [
    {"match": {"participacoes.nome": "joão da silva"}},
    {"term": {"participacoes.papel": "EXECUTADO"}}]}}}]}}}
```

**Decisão (ADR local): nested no doc do processo, NÃO índice `voyager-partes`
separado.** Motivo: ES não tem join; o caso de uso rei (leads/API) filtra
PROCESSOS por atributos de parte combinados com atributos do processo — nested
resolve numa query. Índice separado exigiria 2 hops e estoura `terms` pra
partes de alta cardinalidade (ente público em milhões de processos).
Cardinalidade por doc é pequena (mediana <10 participações; limite ES
`nested_objects` = 10k/doc). A visão parte-cêntrica (`/dashboard/partes`,
"em quantos processos a parte X está") continua no Postgres via pontes
`ParteTribunal`/`PartePapel` — se um dia precisar sair do PG, aí sim nasce o
`voyager-partes` (parte → stats), sem conflito com o nested.

#### `participacoes.documento` — taxonomia MEDIDA das grafias (13/08/2026)

O campo é `keyword` e guarda o documento **SEMPRE mascarado**. Varrido o índice
inteiro (302.609 participações com o campo preenchido, de 6,6M):

| forma | n | exemplo |
|---|---|---|
| CNPJ canônico | 108.892 | `29.979.036/0001-40` |
| CPF canônico | 108.826 | `038.499.054-11` |
| CPF LGPD | 32.468 | `040.***.***-**` |
| CPF LGPD | 30.348 | `136.XXX.XXX-XX` |
| CPF LGPD **com DV** | 14.023 | `872.***.***-97` (3 dígitos + verificadores) |
| CNPJ LGPD | 7.500 | `29.9**.***/****-**` |
| CNPJ LGPD | 436 | `00.3XX.XXX/XXXX-XX` |
| lixo | 116 | `14921092000157131436` (18-20 dígitos colados) |

**`regexp` de 11 e de 14 dígitos puros devolve ZERO.** Não existe documento sem
máscara no índice: quem digita `29979036000140` acha 0 e quem digita
`29.979.036/0001-40` acha 23.217. Quem consome tem de gerar a forma mascarada
canônica — `search.busca_api.normalizar_documento` faz isso (entrada→dígitos→
máscara), valida o DV e devolve também a raiz e as formas LGPD compatíveis.

`prefix` na raiz do CNPJ (`29.979.036/`) une matriz e filiais: 23.217 → **27.044**
processos (+16,5%), custo 11 ms frio / 1 ms morno.

DV como filtro de entrada é seguro: em 8.000 documentos distintos varridos,
**1** (0,014%) tem DV inválido — recusar entrada malformada não esconde acervo.
Já `documentos_secundarios` (`.ia` decisão 12) lembra que DV VÁLIDO não prova
identidade: 3 dos 4 CNPJs errados do INSS têm DV perfeito.

Limitações conhecidas do nested: o vínculo advogado→representado
(`ProcessoParte.representa`) não é modelado no ES (consultar no PG pelo id).
`participacoes.oab` grava `PE23255` (UF colada, sem separador, às vezes com
sufixo de letra: `CE5864A`).

#### `orgao_julgador` / `juizo` — a vara, e a armadilha da string vazia

Medido em 71.308.806 processos: `orgao_julgador` **existe em 100%** dos docs mas
é **STRING VAZIA em 53.417.123** deles (74,9%) → universo real **17.891.683
(25,09%)**, 29.488 grafias distintas. `juizo` existe em 4.469.787 (6,3%) e
3.169.792 desses são vazios → **1.299.995 (1,82%)**, 1.270 grafias.
Contar `exists` nesses campos MEDE 100% e mente por construção.

São `keyword` sem analyzer, e as grafias são incompatíveis entre tribunais
("VARA DE EXECUCAO FISCAL…" no TRF1, "1ª Vara de Execução Fiscal do DF" no
TJDFT). Busca por vara = `regexp` com classe de caracteres por letra acentuável
+ `case_insensitive` (`search.busca_api.regexp_contendo`): `.*[eéèêë]x[eéèêë]
[cç][uúùûü][cç][aáàâãä][oóòôõö] fiscal.*` devolve **341.697** = 313.105 (com
acento) + 28.592 (sem), em **28 ms frio / 8 ms morno**. A regexp sempre tem
conteúdo literal, então o doc com campo vazio se auto-exclui do filtro.
`terms` agg SOLTO nesses campos custa **5,1 s** — autocomplete só com o regexp
na frente (73 ms frio / 13 ms morno).

## voyager-movimentacoes

| Campo | Tipo | Fonte | Caso de uso |
|---|---|---|---|
| id, recorte_id, processo_id, source | long/int | pks | join/compat |
| tribunal | keyword | tribunal_id | filtro |
| publish_date, available_at, detected_at | date | data_disponibilizacao, inserido_em | range de data |
| body | text (pt+ascii) | texto | **busca de decisão/teor** |
| proc, proc_digits | keyword | numero_cnj (+só dígitos) | CNJ com/sem máscara |
| advs, partes | text | ProcessoParte (vazio no backfill `--sem-partes`) | full-text |
| assunto, assunto_norm | text, nested | Process.assunto_nome, tipos_norm | classificação Jusbrasil |
| classe_nome, codigo_classe | keyword | mov ou processo | filtro |
| tipo_comunicacao | keyword | idem | Intimação/Citação/Edital… |
| tipo_documento | keyword | idem | teor: Sentença/Despacho/Acórdão… |
| nome_orgao, secao_diario, periodico_* | keyword | idem, FonteDiario | filtro por órgão/diário |
| ativo | boolean | idem | exclui canceladas |
| docurl, cached_docurl | keyword (não indexado) | link, pdf_storage | link do PDF |

Busca de decisão = `body` (match) + `tipo_documento`/`tipo_comunicacao`
(term) + `publish_date` (range) + `tribunal` (term). **UF em movimentações:
resolver no app** (`uf → [tribunais]` via `search/geo.py`) — não vale reindexar
1,16B docs pra denormalizar um campo derivável do `tribunal`.

## voyager-entidades — cadastro canônico de "quem deve"

Base do **autocomplete** que substitui a busca por texto livre no mapa e na
listagem. Uma linha por ENTIDADE, não por `Parte`. Serviço:
`search/entidades.py`. Build: `manage.py construir_indice_entidades`.

**O problema, em números medidos (12/08/2026):** a raiz de CNPJ `29.979.036`
(INSS) são **610 linhas** de `tribunals_parte`, **610 CNPJs distintos** e **11
grafias de nome**. Nenhuma delas é o INSS; todas são. Digitar "INSS" não acha
"Instituto Nacional do Seguro Social" e vice-versa.

| Campo | Tipo | Caso de uso |
|---|---|---|
| `entidade_id` | keyword | = `_id`: `cnpj:29979036` ou `nome:<sha1[:20]>` |
| `chave` | keyword | **procedência**: `cnpj` (identidade PROVADA) \| `nome` (heurística) |
| `raiz_cnpj` | keyword | 8 dígitos — une matriz e filiais |
| `nome_canonico` | text + `.raw` + `.autocomplete` | rótulo exibido = a grafia MAIS FREQUENTE |
| `nome_normalizado` | keyword | chave de fusão por nome (debug/join) |
| `variantes[]` | text + `.raw` | **RECALL** — todas as grafias, freq. desc (o OR) |
| `variantes_n[]` | integer | frequência de cada grafia (mesma ordem) |
| `variantes_busca[]` | text + `.autocomplete` | **PRECISÃO** — só as grafias com peso (o autocomplete busca aqui) |
| `n_variantes`, `variantes_truncadas` | integer, boolean | tamanho / bateu `MAX_VARIANTES` (300) |
| `nome_suspeito`, `nome_suspeito_motivo` | boolean, keyword | a frase não identifica ninguém: `token_unico` \| `truncado` (decisão 13). **Fora da contagem e do autocomplete.** Ausente = índice anterior à decisão |
| `documentos[]`, `n_documentos` | keyword, integer | CNPJs formatados (mascarados NÃO entram) |
| `documentos_secundarios[]`, `n_documentos_secundarios` | keyword, integer | CNPJs que o tribunal digitou ERRADO pra esta entidade (decisão 12) — evidência do erro, nunca "o CNPJ dela" |
| `entidades_absorvidas[]` | keyword | `entidade_id` de cada entidade engolida (decisões 12 e 14) — auditoria e reversão |
| `n_partes_absorvidas` | integer | quantas de `n_partes` vieram de entidade ABSORVIDA. A dominância das decisões 12 e 14 é sobre a atestação **própria** (`n_partes` − este) — sem o desconto, o cadastro emprestado numa passada vira dominância emprestada na seguinte. Ausente = 0 |
| `documentos_mascarados` | integer | linhas com CNPJ mascarado que não fundiram por raiz |
| `tipo` | keyword | `pj`\|`desconhecido`\|… (o dominante do grupo) |
| `eh_ente_publico`, `ente_publico_por_complemento` | boolean | `agg_estado.eh_ente_publico` (RE_ENTE_PUBLICO + complemento) |
| `grupos_absorvidos` | integer | grupos-por-nome consolidados neste (auditoria) |
| `n_partes` | integer | linhas de `Parte` fundidas — **proxy fraco de prevalência**; hoje serve de ATESTAÇÃO e de fallback (decisão 8) |
| `n_processos` | long | **contagem REAL** em `voyager-processos` (INSS = 4.402.239). **Ausente = "não contamos" ≠ 0** |
| `n_processos_em` | date | quando a contagem foi feita (o número envelhece) |
| `parte_id_min` | long | âncora pro join de volta no Postgres |
| `atualizado_em` | date | freshness do build |

### Decisões (o porquê)

1. **Chave = raiz do CNPJ (8 dígitos).** A raiz é a PJ; os 4 seguintes são a
   filial. Pelo CNPJ completo cada gerência do INSS viraria um devedor
   diferente (`/0001-40` 23.230 processos, `/0012-01` 2.067, `/0002-21` 925,
   `/0988-76` 158). Pelo nome, 11 grafias virariam 11 entidades.
2. **CNPJ mascarado (`29.9**.***/****-**`) NÃO funde.** A máscara deixa 2-3
   dígitos de raiz — `29.9**` casa com 29,9 milhões de CNPJs. Fundir por
   prefixo parcial juntaria entidades diferentes num índice que responde "quem
   deve". Mascarado é tratado como **ausente**: cai no agrupamento por nome, e
   o documento não entra em `documentos[]`. O descarte é contado
   (`documentos_mascarados`, e o total no relatório do comando).
3. **Sem documento → chave por nome normalizado.** Medido: numa janela de 200k
   ids, dos nomes que casam a regex de ente público, **704 eram
   `tipo=desconhecido` sem documento** contra 270 `pj` com CNPJ. Exigir CNPJ
   descartaria justamente município/estado/fazenda.
4. **Escopo = PJ + entes públicos. Advogado FORA** (`tipo='advogado'` OU `oab`
   preenchida — nenhum dos dois cobre 100%): ele representa o devedor. **PF
   fora do MVP**; CPF no documento vence o `tipo` do cadastro (prova > rótulo).
5. **Nome canônico = grafia mais frequente**, não a mais longa (seria
   "AUTORIDADE COATORA … GERENTE EXECUTIVO(A) DO INSS EM CARUARU/PE", 1 linha)
   nem a primeira lida. Sufixo de papel do PJe (`(REQUERIDO(A))`, `- EXECUTADO`)
   sai do rótulo (`entidades.limpar_rotulo`).
6. **Autocomplete = `search_as_you_type`**, não edge-ngram: o ES já gera
   `._2gram`/`._3gram`/`._index_prefix`, o `multi_match type=bool_prefix` casa
   "fazenda sao pau" sem analyzer manual, e o `analyzer:
   portuguese_asciifolding` faz `uniao` achar `UNIÃO`. O custo de índice (ponto
   fraco do tipo) é irrelevante em ~1,5M docs curtos. `variantes` também é
   autocompletável — quem digita "inss" precisa achar a entidade cujo nome
   canônico é "INSTITUTO NACIONAL DO SEGURO SOCIAL".
7. **Ranking com `dis_max`, não `bool.should`.** Medido no ES de prod: com
   `bool_prefix` de 1 termo o score textual satura (os 338 casamentos de "inss"
   pontuaram 2,0), e somando os dois campos quem casa nos dois ganha o dobro —
   "Gerente Executivo do INSS em Manaus" (7 linhas) passava na frente do INSS
   (610), que casa "inss" só pela variante. Com `dis_max` + prevalência
   (hoje `log2p(4 × n_processos) × log2p(4 × n_partes)`, ver 8c) o INSS volta
   pro topo.
7b. **`operator: and` primeiro, `or` como rede.** `bool_prefix` casa por OR por
   padrão: "fazenda sao paulo" devolvia "FAZENDA SÃO MARCELO LTDA" no topo (2
   de 3 termos num campo curto) e a "FAZENDA DO ESTADO DE SÃO PAULO" (21
   linhas, existe no índice) ficava fora do top-5. Digitar MAIS palavras tem
   que ESTREITAR. A cláusula `or` fica com boost menor, só pra typo não
   devolver vazio.
7c. **Recall e precisão em campos SEPARADOS.** `variantes` tem tudo (é o OR);
   `variantes_busca` tem só as grafias com ≥2 linhas e ≥1% da entidade (top-3
   sempre). Motivo medido: o INSS tem UMA linha grafada "Instituto Nacional do
   Seguro Social (UNIÃO)" entre 764 — o cartório digitou as duas partes no
   mesmo campo — e com ela indexada o INSS vinha em **1º lugar na busca por
   "uniao"**. Num índice de 1,1M entidades, o typo de quem é grande sequestra a
   busca de quem é o alvo.
8. **`n_processos` vem do ES, nunca do Postgres — e é ESCOPO PARCIAL.**
   `Parte.total_processos` está preenchido em **39,3%** da base: precomputar
   dali mostraria "0 processos" em 6 de cada 10 entidades. O build NÃO escreve
   o campo; ele sai de uma 2ª passada **ES→ES** (`contar_processos_entidades`),
   que roda o OR de `match_phrase` contra `voyager-processos` e grava por
   `update` parcial. Escopo = quem disputa o autocomplete (`n_partes >= 2` OU
   ente público = **182.196** de 1,14M, 16%): medido em 55 termos reais, cobre
   **94,5%** das entidades que aparecem no top-10 e **98,0%** das do top-3
   (contra 89,6%/96,6% do corte `>= 3`, que economizaria só ~2 min).
   **Ausente ≠ zero**: quem ficou fora não tem o campo (o ranking cai no
   `n_partes`); zero é medição e ranqueia ABAIXO de desconhecido.
   **Um rebuild do índice apaga a contagem** (o `_bulk` do build usa `index`) —
   depois de reconstruir, rode o contador de novo.
8b. **Por que `n_partes` não bastava.** Ele conta CADASTRO, não litígio. Medido
   no índice real: buscar "inss" devolvia em 1º o "Gerente Executivo do INSS de
   São Paulo/Centro" — **109 linhas de `Parte` e 21 processos** — à frente da
   autarquia (764 linhas, **4.402.239** processos). Com a contagem real a
   gerência sai até do top-10.
8c. **Ranking = texto × prevalência × ATESTAÇÃO.** Só trocar o proxy pela
   contagem não bastou: `n_processos` é propriedade da FRASE, então as FACETAS
   do INSS (`INSS` 53 linhas/4,25 MI, `CEAB - INSS` 4/2,09 MI, `Procuradoria da
   CEAB-DJ INSS` 1/1,18 MI) casam quase os mesmos processos, o `log2p` achata
   4,40 MI e 4,25 MI em 3,5% e o texto — que prefere o nome curto — volta a
   decidir: a autarquia caía pro **11º**. O que separa a entidade das suas
   facetas é o cadastro (764 contra 53), então quem foi contado ranqueia por
   `log2p(4·n_processos) × log2p(4·n_partes)`. A atestação NÃO se aplica a quem
   não foi contado (lá `n_partes` já é a única evidência e não pode contar 2×).
   Validado fora dos 5 termos do gate, em 55 termos: mediana de processos do 1º
   colocado 11.147 → 16.718, top-1 irrelevante (≤100 processos) 7 → 2, e
   "fragmento no topo" (≤2 linhas de cadastro com ≥100k processos) 6 → **0**
   (é a atestação que zera isso; sem ela eram 6).
9. **Consolidação nome→cnpj** com dupla prova: o nome normalizado tem que bater
   com o nome CANÔNICO do grupo-por-CNPJ (não com uma variante qualquer, senão
   a grafia solta "UNIÃO FEDERAL" pendurada no CNPJ da AGU engoliria o grupo
   "União Federal" inteiro) **e** apontar pra um CNPJ inequívoco: único, ou
   dominante (≥90% das linhas e ≥10× o segundo). A dominância existe porque o
   INSS tem, além da raiz certa (610 linhas), **5 CNPJs errados** digitados por
   tribunal com o mesmo nome (1 linha cada) — o empate falso barrava a fusão.
   Homônimo de verdade (3 "AUTO POSTO SÃO JOSÉ LTDA" com volume parecido) fica
   separado — abstém.
10. **Falso-merge > falso-split em gravidade.** O primeiro build completo
   provou: a normalização herdada do `agg_estado` colapsava **144 empresas** na
   chave "INDUSTRIA COMERCIO", 88 em "EMPREENDIMENTOS IMOBILIARIOS" e 54 em
   "TRANSPORTES" — descartava o PRIMEIRO segmento curto como sigla (mas ali
   mora a marca: "HENRIMAR - INDUSTRIA E COMERCIO LTDA") e tratava inicial de
   1 letra como conectivo ("A&E" e "A. O." → "TRANSPORTES"). Corrigido: sigla
   só cai DEPOIS do primeiro segmento; token de 1 letra nunca é stopword.
   **Por isso `normalizar_nome` (entidades) ≠ `normalizar_entidade`
   (agg_estado)** — as chaves não são intercambiáveis.
11. **Placeholder não é entidade.** "INFORMAÇÃO PROTEGIDA" (segredo de justiça)
   somava 4.212 linhas e era a MAIOR entidade do índice — NULL disfarçado no
   topo do autocomplete. `NOMES_PLACEHOLDER` corta.
12. **Consolidação cnpj→cnpj: o CNPJ que o tribunal digitou errado.** O top-5 de
   "quem mais litiga no Brasil" era **cinco vezes o INSS** — a autarquia (raiz
   29.979.036, 764 linhas) e mais 4 entidades de UMA linha, mesmo nome
   canônico, penduradas em CNPJs errados. A decisão 9 não resolve: os dois
   lados já têm chave `cnpj`. Só que fundir por nome mataria homônimo legítimo
   — medido, **6.925 nomes normalizados são compartilhados por 2+ entidades de
   CNPJ** (15.917 entidades), e a maioria é template replicado: 85 "FUNDO
   MUNICIPAL DE SAÚDE", 46 "IGREJA EVANGÉLICA ASSEMBLEIA DE DEUS", 44 "SERVIÇO
   AUTÔNOMO DE ÁGUA E ESGOTO", 12 "CONDOMÍNIO EDIFÍCIO SÃO JOSÉ".
   **O discriminante é o perfil de cadastro**: template replicado é PLANO (85
   entidades × 1 linha — ninguém domina), entidade com typo é PICO (764 × 1·1·
   1·1). Três provas, todas necessárias (`entidades.consolidacao_cnpj`):
   (a) **dominância** ≥90% das linhas e ≥10× o segundo — sozinha derruba 6.899
   dos 6.925 grupos, e como o vice tem ≥1 linha o líder precisa de ≥10;
   (b) **minoria de 1 linha** (sem atestação independente);
   (c) **grafia IDÊNTICA** (`mesma_grafia`, não `normalizar_nome`) — é o que
   impede a S.A. de comer a ME: `normalizar_nome` descarta forma societária, e
   sem essa prova "DROGARIA SAO PAULO LTDA - ME" seria engolida pela "DROGARIA
   SAO PAULO S.A", assim como "LOJAS AVENIDA LTDA - ME" e "CARAMURU ALIMENTOS
   LTDA - ME". Resultado medido: **14 grupos, 20 entidades absorvidas, 6.910
   homônimos preservados**.
   **DV de CNPJ não serve de discriminante** — medido: dos 4 CNPJs errados do
   INSS, TRÊS têm dígito verificador VÁLIDO (03.500.696/0001-03 é um CNPJ
   perfeitamente formado, só não é do INSS). DV não separa typo de entidade
   alheia; cadastro separa.
   O CNPJ errado **não é jogado fora**: vai pra `documentos_secundarios` (e o id
   da entidade engolida pra `entidades_absorvidas`), que é o que torna a fusão
   auditável e reversível.
13. **Frase que não identifica ninguém não conta processo** (`nome_suspeito`).
   "JOSÉ" — 2 linhas de cadastro, uma grafia só — aparecia com **1.796.174**
   processos: `match_phrase` de UM token casa todo José do país. "MUNICIPIO DE"
   (cadastro cortado no meio) marcava 467.493. A poda de sub-frase não pega
   isso — ela compara grafias DENTRO da mesma entidade, e aqui a entidade tem
   uma grafia só. Dois motivos:
   * `token_unico` — grafia de 1 token com menos de `NOME_1TOKEN_MIN_LINHAS`
     (=5) linhas de cadastro. **O corte é por ATESTAÇÃO, não por tamanho do
     nome**: "INSS" (53 linhas) e "UNIAO" (58) ficam. PETROBRAS e ITAÚ não
     correm risco e não por sorte — no dado do tribunal a empresa grande vem
     com a forma societária colada ("PETROLEO BRASILEIRO S A PETROBRAS", "ITAU
     UNIBANCO S.A", "AMERICANAS S.A", "CLARO S.A"), então a FRASE tem 2+ tokens
     e nem chega ao teste. Medido: das 1.141.630 entidades, só **535** têm
     grafia de 1 token, e **518** caem.
   * `truncado` — a grafia termina em conectivo ("MUNICIPIO DE", "…LTDA EM",
     "…DO ESTADO DO"). Fato sintático, sem escape por atestação: **117**
     entidades, a maior com 4 linhas de cadastro.
   **O que se perde ao marcar os borderline (3-4 linhas): nada.** Medido caso a
   caso — em 11 de 12 a organização também está no índice pelo nome COMPLETO e
   com mais cadastro (CAPEMISA 4→13 linhas, BANPARÁ 3→38, DECOLAR 4→13, TNT
   4→24, SELECON 4→5, IPSEMG 9 bare→12 completo, ALIEXPRESS 3→4). E o bare
   costuma reivindicar MAIS processos que a entidade real (Decolar 3.394 vs
   2.863; SELECON 653 vs 192; TNT 267 vs 137) — que é a promiscuidade da frase
   medida em números.
   O suspeito **fica no índice** (alguém pode auditar o cadastro do tribunal);
   ele só sai do `escopo_contagem` e do autocomplete (`post_filter`), e perde
   `n_processos` — ausência é "não contamos", que é a verdade.

### Como se consome (enquanto o nested não está reindexado)

`participacoes` (nested) é a forma estruturalmente correta, mas está em **1,9%**
dos docs (reindex em curso). O campo TEXTO `partes` está em **100%** dos 71,1M.
Então o autocomplete devolve a entidade e a busca vira um **OR de
`match_phrase` das `variantes` contra `partes`** (`entidades.query_variantes`).
Quando o reindex fechar, a query passa a filtrar `participacoes.documento` /
`participacoes.parte_id` e o OR vira fallback.

```python
from search.entidades import query_autocomplete, query_variantes
es.search(index='voyager-entidades', body=query_autocomplete('inss'))
es.count(index='voyager-processos',
         body=query_variantes(doc['variantes'],
                              ocorrencias=doc['variantes_n'], min_ocorrencias=2))
```

Quem exibe "N processos" tem que ler `n_processos` como **tri-estado**:
número = medido; `None`/ausente = fora do escopo da contagem (mostre "—", não
"0"); `0` = medido e vazio. E `n_processos_em` diz a idade do número.

### Corrigir um índice já construído (decisões 12 e 13)

As duas correções são do BUILD — um índice novo já sai correto. Pra consertar um
índice **existente** sem reconstruir (que custa ~5 min lendo 16,7M linhas do
**Postgres de produção** + ~11 min de escrita, e ainda apaga `n_processos` de
182k entidades, obrigando a recontar), existe o passe **ES→ES**:

```bash
# ensaio: monta o plano inteiro e mostra o que faria. Não escreve nada.
manage.py corrigir_indice_entidades --indice entidades-teste --dry-run
manage.py corrigir_indice_entidades --indice entidades-teste
# 2 líderes ficam sem n_processos (o OR mudou) — a retomada barata recompõe:
manage.py contar_processos_entidades --indice entidades-teste --somente-faltantes
```

Mesma decisão do build (`entidades.consolidacao_cnpj` / `entidades.nome_suspeito`
/ `entidades.fundir_doc`), **zero Postgres**, idempotente (rodar 2× encontra 0 a
fazer). Medido em 13/08/2026 sobre as 1.141.630 entidades: **120 s**, 236
requisições ao ES, **pico de 0,87 s**, CPU do nó 3-8%, **zero rejeição** de
thread pool. Uma varredura de `search_after` com `_source` de 5 campos monta o
plano em memória ANTES de qualquer escrita — por isso o `--dry-run` é fiel.

Resultado: 1.141.630 → **1.141.610** entidades (20 absorvidas), **635** marcadas
como suspeitas, **9,88 milhões** de processos-fantasma removidos de 161
contagens. O top-10 por `n_processos` passou a ter o INSS **uma vez só** e
perdeu "JOSÉ", "ANTONIO", "Fazenda", "João", "ANA", "FRANCISCO", "MUNICIPIO DE",
"Fernando", "ANDRÉ", "Rafael" e "THIAGO".

**O que ficou de fora (residual conhecido).** 58 entidades com ≤4 linhas de
cadastro ainda reivindicam ≥50k processos, e nenhuma cai nas decisões 12/13:
são nomes institucionais GENÉRICOS de 2+ tokens, corretamente separados porque
são entidades diferentes — "POLICIA CIVIL" (3 CNPJs, 157.903 cada),
"PROCURADORIA GERAL DO ESTADO"/"DO MUNICIPIO" (um por UF/município), "UNIÃO
FEDERAL" (2 CNPJs de 1 linha, sem dominante pra decidir qual é o certo) — e
facetas do INSS ("CEAB - INSS", "Procuradoria da CEAB-DJ INSS") — estas últimas
resolvidas depois pela **decisão 14** (abaixo).

### Decisão 14 — faceta não é devedor novo (`consolidar_entidades`)

Contadas as 182.026 entidades do escopo, o top-20 de "quem mais deve" abria com
**oito** linhas do INSS e **três** da União. Nenhuma errada: `n_processos` mede
uma FRASE, e "CEAB - INSS" é setor do INSS. Mas a tela precisa de uma linha por
devedor.

**O critério, em uma frase:** funde-se uma entidade **sem CNPJ próprio** na
entidade **provada por CNPJ** cujo nome inteiro está dentro do nome dela (ou de
quem ela é a **sigla atestada**), quando essa é a **única** dona possível e
**domina** o cadastro próprio em 10×.

As provas (`entidades.plano_facetas`), todas necessárias:

1. **léxica** — o nome canônico da dona é frase CONTÍGUA dentro do da faceta;
2. **contenção do conjunto** — TODA grafia contada da faceta contém uma grafia
   da dona, então o OR da dona já casa 100% dos processos dela e a fusão não
   inventa processo (medido: o INSS saiu da fusão com os MESMOS 4.402.239);
3. **assimetria de documento** — `pode_ser_faceta` exige `chave='nome'` e
   `pode_ser_dona` exige `chave='cnpj'`. Quem o tribunal documentou nunca é
   engolido (Bradesco × Bradesco Financiamentos, S.A. × ME, INSS × PGF,
   município homônimo), e o balde genérico sem CNPJ nunca engole ninguém
   (o MPF não vira "MINISTÉRIO PÚBLICO", a DPMG não vira "DEFENSORIA PÚBLICA");
4. **dona única** — 2 donas elegíveis com raízes diferentes ⇒ abstém. É o que
   mantém "INSS Procuradoria-Geral Federal" (994.913) fora da fusão;
5. **dominância** sobre `n_partes` − `n_partes_absorvidas`;
6. **cortes de classe**: `RE_ENTE_TERRITORIAL` (Estado/Município/DF/União nunca
   é dona — o nome de um território é complemento em milhares de instituições
   que não são ele) e `RE_PJ_ASSOCIATIVA` (sindicato/associação/cooperativa/
   fundação são PJ próprias e citam o órgão dos associados).

```bash
manage.py consolidar_entidades --indice entidades-teste --dry-run
manage.py consolidar_entidades --indice entidades-teste --verificar
manage.py contar_processos_entidades --indice entidades-teste --somente-faltantes
```

O comando ainda **recusa antes de escrever** qualquer fusão que faria o número
do líder encolher (`_recusar_quem_encolhe`): a poda de `grafias_para_contagem`
derruba a grafia curta quando a longa que a engole é ao menos tão frequente, e
toda grafia de faceta é uma versão longa da grafia da dona — numa dona de
cadastro raso o empate 1×1 tira o nome real do OR. Medido: 6 recusas de 164.

**Resultado medido (13/08/2026, `voyager-entidades-teste`):** 1.141.610 →
1.131.058 entidades; 6 grupos por grafia idêntica cruzando chaves + 10.516
facetas em 164 donas; **13.222 abstenções** (7.404 por dona ambígua, 5.818 por
dominância). Top-20: INSS **1×** (4.402.239, inalterado), União Federal **1×**.
Homônimos cnpj↔cnpj preservados 6.912 → **6.906**; os 6 grupos que saíram são
pares de CNPJ de 1 linha da MESMA entidade única (União Federal, Estado de
Alagoas, municípios de Juiz de Fora, Belo Horizonte, Poços de Caldas e
Contagem), nenhum homônimo entre UFs. Custo no ES: 300 requisições, **pico
1,19 s**.

**Residual conhecido.** (a) "Central de Análise de Benefício - Ceab/Inss"
(910.751) abstém por causa de UMA grafia com o typo "Ceab/INS", que não contém
"INSS"; (b) donas que são balde genérico COM CNPJ ("MINISTÉRIO PÚBLICO
ESTADUAL", "SECRETARIA DE SAÚDE") absorvem variantes territoriais de baixo
volume — o corte territorial só pega a dona, não a faceta; (c) o comando não é
idempotente no sentido forte: fundir remove donas possíveis e pode desempatar um
caso antes ambíguo na rodada seguinte — rode uma vez por build e leia o
`--dry-run` antes de repetir. O que resolve tudo isso de vez é trocar o OR de
`match_phrase` por filtro em `participacoes.parte_id`/`documento` quando o
reindex do nested fechar.

### Contagem de processos (`n_processos`)

```bash
# passada completa do escopo (182k entidades, ~6 min, ES→ES, ZERO Postgres)
manage.py contar_processos_entidades --indice entidades-teste --lote 250 --top 20
# ensaio: conta e mede latência, não escreve
manage.py contar_processos_entidades --dry-run --limite 2000
# retomada de uma corrida interrompida (não revisita quem já tem número)
manage.py contar_processos_entidades --somente-faltantes
manage.py contar_processos_entidades --desde '<cursor final do relatório>'
```

`_msearch` em lote (1 requisição por 250 entidades — 182k requisições
individuais seriam ~1h só de RTT), `search_after` no `entidade_id` (chave única
e imutável; o cursor só anda pra frente, então escrever nos docs já visitados
não desloca o que vem depois), escrita por `_bulk`/`update` **parcial** (nunca
`index`: o resto do doc é o produto do build). Idempotente — rodar 2× reescreve
o mesmo número. O lote é cronometrado e ENCOLHE sozinho (metade, piso 50) se
passar de `--alvo-lote-s`, porque o ES serve a dashboard de produção.

Medido em 12/08/2026 (182.196 entidades): **341 s**, 535 entidades/s, `_msearch`
média 0,21 s / p95 0,40 s / pico 1,33 s, `_bulk` média 0,19 s / pico 0,96 s.
Impacto no ES: CPU 20-38%, `search` thread pool com no máximo 10 ativos e fila
1, **zero rejeições**. 1.518 zeros medidos; p50=3, p90=28, p99=794 processos.

**Poda de over-match (obrigatória).** `match_phrase` de frase CURTA casa toda
frase longa que a contém. Na 1ª contagem completa o top-10 de "quem mais litiga
no Brasil" saiu com `Procuradoria - Allianz` (4 linhas de cadastro) em 1º com
**7.222.852** processos — porque uma das grafias da entidade era só
"PROCURADORIA" — e `INSTITUTO NACIONAL LTDA` (2 linhas) com 4.469.999, pela
grafia "INSTITUTO NACIONAL". `entidades.grafias_para_contagem` corta isso:
**entre duas grafias da MESMA entidade em que uma é sub-frase da outra, a mais
curta só sobrevive se for MAIS FREQUENTE que a mais longa** ("INSTITUTO NACIONAL
DO SEGURO SOCIAL" 610× ⊂ "… - INSS" 17× → é o NOME, fica; "S." 1× ⊂ "S.A.
(VIAÇÃO AÉREA RIO-GRANDENSE) - FALIDA" 1× → empate, é TRUNCAMENTO, sai). A
comparação passa pelo `limpar_rotulo` antes, senão "X (REQUERIDO(A))" engoliria
"X" e toda entidade carimbada pelo cartório seria subcontada.

### Build

```bash
# passada completa (canônica) — ~16,7M linhas
manage.py construir_indice_entidades --lote 20000 --sleep 0.05
# ensaio sem escrever
manage.py construir_indice_entidades --dry-run --limite 200000
# top-up incremental (só partes novas), mesclando com o que já está no índice
manage.py construir_indice_entidades --desde-id <ultimo_id> --merge-es
```

Leitura por **keyset** (`id > :ultimo ORDER BY id LIMIT :lote`) — índice puro na
PK. **Nunca `COUNT(*)`**: o total é `reltuples` do `pg_class`. Cada lote é
cronometrado e ENCOLHE sozinho (metade, piso 500) se passar de `--alvo-lote-s`.
Nada de faixa fixa de id: os 16,68M de linhas estão espalhadas em 626M de ids
(2,7% de densidade) — uma faixa devolveria de 0 a 70k linhas.

A agregação é **global em memória** e o ES só é escrito no fim (não dá pra
fechar uma entidade antes do fim da leitura — a última linha pode ser mais uma
grafia do INSS). `--checkpoint` grava o progresso da LEITURA; retomar do meio
gera um índice que cobre só a janela lida. O modo incremental correto é
`--desde-id` + `--merge-es` (mget → une variantes/documentos, soma `n_partes`);
nele a consolidação nome→cnpj é PULADA, porque "aponta pra exatamente 1 CNPJ"
não é verificável sem a base toda em memória.

### Limitações conhecidas

- `chave='nome'` é heurística: dois municípios homônimos em UFs diferentes
  colapsam numa entidade. Por isso a procedência vai no doc.
- `eh_ente_publico` herda os falsos positivos da regex compartilhada: `\bfazenda\b`
  marca "FAZENDA SÃO RAIMUNDO LTDA" (uma fazenda de verdade) como ente público.
- Uma grafia genérica pendurada num CNPJ específico polui o OR ("UNIÃO FEDERAL"
  entre as variantes da AGU). Defesa: `variantes_n` + `min_ocorrencias` na
  busca, `grafias_para_contagem` na contagem.
- `n_partes` é proxy de prevalência, não contagem de processos (decisão 8).
- **`n_processos` mede a FRASE, não a entidade.** A poda pega a grafia truncada,
  mas não o nome de UM TOKEN que é genérico por natureza: a entidade `JOSÉ`
  (2 linhas de cadastro) marca 1.796.174 porque `match_phrase("josé")` casa
  todo José do país. Sobrou no top-10 da contagem; o lever é do BUILD (nome de
  1 token sem CNPJ é cadastro truncado, candidato a `NOMES_PLACEHOLDER`), não
  do contador. No autocomplete o dano é contido pela atestação (8c).
- **A mesma entidade sai em vários docs e o topo do `n_processos` mostra isso**:
  o INSS aparece 5× (a raiz certa + os 4 CNPJs errados digitados por tribunal,
  1 linha cada), todos com os mesmos 4.402.239. A consolidação nome→cnpj não
  os funde porque os 4 têm chave `cnpj` própria (ela só absorve grupos por
  NOME). Candidato natural a uma consolidação cnpj→cnpj por nome canônico
  idêntico + dominância.
- Não há write-through: o índice é reconstruído por comando (candidato a cron
  diário `--desde-id` + `--merge-es`). Parte renomeada/dedupada só some no
  rebuild completo.

## Write-through — quem indexa o quê (mapa honesto)

| Caminho de escrita | Dispara indexação ES? | Via |
|---|---|---|
| `Movimentacao.save()` / delete | ✅ | signal → fila `es_index` (worker na .102) |
| Ingestão DJEN (`bulk_create`) | ❌ signal não dispara | **tail do `reindexar_movimentacoes`** (keyset + checkpoint) + `sync_es_incremental` |
| **Diários próprios** (`diarios/base.py::persistir_movimentacoes`, `bulk_create`) | ❌ signal não dispara | ✅ **enfileira o lote na hora** (`_entregar_ao_indice`, `on_commit`, 500 por job) + **gate `diarios.jobs.conferir_indice`** que confere PG×ES do dia e repara. Ver `.ia/DIARIOS.md` §12 |
| **Porta do Datajud — movimentações** (`datajud/ingestion.py::sync_processo`, `bulk_create`) | ❌ signal não dispara | ✅ **enfileira o lote na hora** (`_entregar_ao_indice`, `on_commit`, lote de 500) + **gate `datajud.jobs.conferir_indice_datajud`** (cron 15 min) que confere a JANELA DE ESCRITA pelos dois lados e repara. Dívida de 21/08 PAGA em 24/08/2026 |
| **Porta do Datajud — meta do `Process`** (`.update()` de classe/assunto/órgão/valor/totais) | ❌ signal não dispara **e `atualizado_em` não muda** | ✅ mesmo `_entregar_ao_indice` + `atualizado_em` carimbado à mão (o poller de processos é keyset por ele) + o mesmo gate |
| `Process.save()` (apply_event, classificação por save, admin) | ✅ | signal |
| Ingestão DJEN / varredura Datajud (Process novo via `bulk_create`) | ❌ signal não dispara | ✅ `sync_es_incremental` (keyset por id, daqui pra frente) + **`manage.py es_backfill_processos`** para o PASSIVO + **sentinela diária** `search.jobs.conferir_indice_processos`. Dívida de 24/08 PAGA em 24/08/2026 — ver a seção abaixo |
| Drainer `apply_batch` (`bulk_update`/`bulk_create`) | ✅ | enqueue explícito `search.jobs.indexar_processos_bulk` no fim do batch (fix 2026-08) |
| ProcessoParte (create/delete individual) | — sem signal DE PROPÓSITO | o processo é reindexado pelo `processo.save()` que fecha todo enriquecimento; signal por-linha multiplicaria a fila ~2N por processo |
| Comandos de manutenção em massa (dedup_partes, recategorizar_tipo_partes, SQL cru) | ❌ | rodar `reindexar_processos` direcionado depois |

## A porta do Datajud entrega ao índice (24/08/2026)

A dívida registrada em 21/08/2026 nesta mesma tabela foi paga. Ela era MAIOR do
que parecia: a porta escreve por dois caminhos e nenhum dos dois chegava ao ES.

Medido em produção com amostra aleatória e `_mget` por id (resposta exata por
documento, não estimativa), no dia em que o poller estava SAUDÁVEL — atraso de
122.604 ids, ~1 tick:

| idade da escrita | linhas datajud na janela | amostra | fora do índice |
|---|---:|---:|---:|
| 0-5 min | 3.088 | 3.000 | **3.000 (100,00%)** |
| 5-15 min | 3.927 | 3.000 | 1.268 (42,27%) |
| 15-30 min | 4.133 | 3.000 | 0 |
| 30-60 min · 1-2 h · 2-4 h | 15.362 · 20.000+ · 20.000+ | 3.000 | 0 |

Vazão da porta, por hora cheia no mesmo dia — ela varia **28x**, e a primeira
medição (27.468/h) caiu num vale:

| hora (UTC) | linhas | hora (UTC) | linhas |
|---|---:|---|---:|
| 03h | 59.250 | 09h | 403.346 |
| 04h | 268.218 | 10h | 385.846 |
| 05h | 367.495 | 11h | 479.568 |
| 06h | 385.359 | **12h** | **798.824** (pico) |
| 07h | 360.631 | 13h | 36.464 |
| 08h | 357.186 | **14h** | **28.610** (vale) |

Média das 12 horas medidas: **327.566 linhas/h**. A conta fecha com o teto de
100 rpm da APIKey compartilhada: 5.628 processos numa hora x 74 movimentos por
processo = 416.186 linhas, exatamente o que o gate contou naquela hora.

E o buraco que NÃO tinha poller nenhum — `Process.objects.filter(pk=…)
.update(...)` não dispara `post_save` **e não mexe em `atualizado_em`**, porque
`auto_now` só roda em `Model.save()`; e `atualizado_em` é justamente a chave do
keyset de `sync_processos_atualizados`. Critério exato: o doc está em dia com
esta porta se `doc.enriquecido_em >= PG.data_enriquecimento_datajud`.

| janela de `data_enriquecimento_datajud` | amostra | doc ausente | doc atrasado | em dia |
|---|---:|---:|---:|---:|
| 30-15 min | 500 | 18 | 482 | **0** |
| 2h-30min | 500 | 57 | 443 | **0** |
| 1d-2h | 500 | 9 | 483 | 8 (1,6%) |
| 3d-1d | 500 | 3 | 399 | 98 (19,6%) |
| 7d-3d | 500 | 29 | 398 | 73 (14,6%) |
| 30d-7d | 500 | 0 | 500 | **0** |

Confirmação independente, restrita aos tribunais SEM enricher (onde a classe só
pode ter vindo do Datajud): das que têm classe no Postgres, chegaram ao índice
**2 de 345 (0,6%)** na janela 2h-30min e 117 de 354 (33,1%) em 3d-1d.

População do buraco: **22.475.738** processos com `data_enriquecimento_datajud`,
**1.703.782** nos últimos 30 dias, **775.484** nos últimos 7, ~5.000/h.

### O recorte do gate, e por que não é `(tribunal, dia)`

O gate do diário recorta por `(tribunal, dia)` porque lá a unidade de coleta é
um caderno de um dia. Aqui não serve: o Datajud devolve o histórico INTEIRO do
processo numa requisição, então uma sincronização espalha linhas por décadas de
`data_disponibilizacao`. O recorte que corresponde ao trabalho desta porta é a
**janela de escrita** (`inserido_em`), e ela é barata porque o índice
`mov_inserido_tribunal_idx (inserido_em DESC, tribunal_id)` existe de verdade
(conferido por coluna em `pg_index`).

Custo medido com `EXPLAIN (ANALYZE, BUFFERS)` em produção:

| janela | linhas datajud | descartadas pelo filtro | tempo | blocos |
|---|---:|---:|---:|---:|
| 15 min | 4.103 | 13.529 | **2,33 s** | 5.201 |
| 15 min (2ª vez) | 4.103 | 13.529 | 0,01 s | 5.201 (hit) |
| 60 min | 28.983 | 78.943 | 6,64 s | 31.175 |
| 4 h | 200.000 (TETO) | 124.950 | 8,50 s | 62.551 |

Por isso o passo COMEÇA em **15 min**. Comparar com o diário, onde recortar UMA
edição custava 29,2 s e 65.846 blocos: lá o recorte teve que SUBIR para o dia;
aqui o recorte fino é o barato.

**O passo é ponto de partida, não regra.** Na hora de pico, 15 min são 200 mil
linhas e batem o teto de leitura SEMPRE — e um gate que PARA no teto congela o
watermark no mesmo instante para sempre, que é o oposto do que ele existe para
impedir. `_conferir_passo` **divide a janela ao meio** quando o teto aparece,
igual ao que `search/jobs.py::_enviar_bulk` faz com o 413 do ES: o erro não é
de dado, é de tamanho. Só o recorte MÍNIMO (1 min) que ainda não couber vira
ERRO registrado e dívida visível — o que exigiria 12 milhões de linhas/hora,
15x o pico medido. O lado dos processos usa
`proc_datajud_em_idx (data_enriquecimento_datajud)` e custou **0,29 s** para
1.296 processos na mesma janela de 15 min.

`external_id` é filtrado com **LIKE** (`__startswith`), nunca com `__gte/__lt`:
a collation `en_US.UTF-8` ignora pontuação e `external_id < 'datajud;'` compara
como se fosse `datajud`, devolvendo 0 linhas. Já custou um alarme falso.

### Validado em produção (24/08/2026)

O que a régua mediu ANTES do deploy, no mesmo recorte e com o mesmo critério
com que mede DEPOIS (`manage.py datajud_conferir_indice --sem-reparo`, faixa
10:00-11:30 -03): 46.362 movimentações conferidas, 0 fora do índice (o poller
já tinha alcançado aquelas janelas) e **7.543 de 7.543 processos (100,00%) com
o doc anterior à escrita**.

Depois de ligar a entrega na gravação, medido às 13:09 -03 por idade da
escrita:

| idade da escrita | movs conferidas | fora do índice | processos | doc atrasado |
|---|---:|---:|---:|---:|
| 0-3 min | 3.063 | **0** | 303 | **0** |
| 3-6 min | 2.383 | **0** | 298 | **0** |
| 6-10 min | 3.038 | **0** | 410 | **0** |
| 10-15 min | 3.415 | **0** | 503 | **0** |
| 15-30 min | 9.298 | **0** | 1.484 | **0** |
| 30-60 min | 16.843 | **0** | 2.609 | **0** |
| 1-2 h | 29.938 | 0 | 5.042 | 4.412 (87,5%) |
| 2-4 h | 200.000 (teto) | - | 10.630 | 8.499 (80,0%) |

As duas últimas linhas são escrita ANTERIOR ao deploy: o cron ainda estava
alcançando aquela faixa (watermark em 11:07 UTC, avançando 60 min por passada).

O gate rodou sozinho, duas passadas seguidas, sem ninguém pedir:

    12:27 -03  reancorou=True  atraso 6,0 h  4 recortes
               416.186 movs (0 fora) · 5.628 processos · 5.628 re-enfileirados
    12:59 -03  reancorou=False atraso 5,53 h 4 recortes
               376.220 movs (0 fora) · 4.714 processos · 4.714 re-enfileirados

E o reparo dele foi conferido depois: a faixa 09:07-10:07 UTC, que ele
consertou às 12:27, voltou a ser medida e deu **0 de 5.628 atrasados**.

Backfill histórico executado (3 dias de escrita, só o lado dos processos,
passo de 60 min, 79 recortes em menos de 3 minutos):

    418.273 processos conferidos · 356.739 com doc anterior à escrita (85,3%)
    356.739 re-enfileirados · fila `es_index` drenada a zero

Conferido em seguida no mesmo recorte: **418.267 conferidos, 0 atrasados**.

O `TETO` também foi visto funcionando em produção, num recorte de hora de pico:

    09:00 +15,0 min  167.977 movs   1,78 s
    09:15 +15,0 min  200.000 (TETO) 0,40 s   -> dividiu
    09:15 + 7,5 min   86.749 movs   0,84 s
    09:22 + 7,5 min  119.731 movs   1,07 s

### Peças e operação

| peça | papel |
|---|---|
| `datajud/ingestion.py::_entregar_ao_indice` | entrega no `on_commit`: movimentações novas em lote + o doc do processo |
| `datajud/indice.py` | a régua: `conferir_movs`, `conferir_processos`, `conferir_janela`, `tick` |
| `datajud/jobs.py::conferir_indice_datajud` | o job (fila `default`, `job_id` fixo) |
| `djen/scheduler.py` | cron de 15 min |
| `manage.py datajud_conferir_indice --desde … --ate …` | backfill de faixa arbitrária; **não toca o watermark do cron** |
| `search/gate.py` | primitivas compartilhadas com o gate do diário (`ausentes_no_bloco`, `enfileirar_*`) |

Kill switches sem deploy: `DATAJUD_INDEXAR_AO_GRAVAR=0` (para a entrega) e
`DATAJUD_GATE_INDICE_ENABLED=0` (para o gate). Telemetria da última passada em
`cache['datajud:gate:ultimo']`; watermark em `cache['datajud:gate:wm']` — se
ele sumir, o gate **re-ancora 6 h atrás e loga ERRO**, nunca no topo (re-ancorar
no topo é o defeito do poller que custou 27.619 linhas ao diário).

## O passivo do índice de processos — 14,2 milhões fora da busca (24/08/2026)

Os dois gates acima (diário e Datajud) cobrem a **janela de escrita** de uma
porta cada um: eles garantem que o que a porta grava HOJE chega ao índice. Não
cobrem — e nunca cobriram — o que já estava gravado. Esse passivo foi medido e
fechado em 24/08/2026.

### A régua, e por que não é `reltuples − _count`

`reltuples` é ESTIMATIVA do planner, e `_cat/indices` conta doc Lucene (acima).
A régua que sustenta o número é **amostra aleatória por faixa de pk com semente
declarada, perguntada ao ES por `_mget`** — realtime GET por `_id`, resposta
exata por documento. 4.000 candidatos sorteados em cada oitavo do espaço de pk
(`search/backfill_processos.py::amostrar`, seed `20260824`):

| faixa de pk | existem no PG | fora do índice | % |
|---|---:|---:|---:|
| 3.520–13.042.773 | 3.987 | 0 | **0,00%** |
| 13.042.774–26.082.028 | 3.984 | 0 | **0,00%** |
| 26.082.029–39.121.283 | 3.988 | 0 | **0,00%** |
| 39.121.284–52.160.538 | 3.993 | 0 | **0,00%** |
| 52.160.539–65.199.793 | 3.989 | 0 | **0,00%** |
| 65.199.794–78.239.048 | 3.390 | 1.559 | **45,99%** |
| 78.239.049–91.278.303 | 3.992 | 377 | **9,44%** |
| 91.278.304–104.317.558 | 3.978 | 2.444 | **61,44%** |
| **TOTAL** | **31.301** | **4.380** | **13,99%** |

Duas leituras que só a amostra dá:

- **O acervo antigo está inteiro.** Zero ausentes nos cinco primeiros oitavos,
  em 19.941 processos amostrados. O buraco é dos processos NOVOS — os da
  varredura do Datajud e da recuperação nacional, que entraram por
  `bulk_create`/`.update()` e portanto nunca dispararam `post_save`.
- **O índice é subconjunto do banco.** Dos 699 pks sorteados que caíram em
  buraco de sequência (valor queimado por `bulk_create(ignore_conflicts=True)`),
  **0** estavam no índice. Não há doc órfão para limpar.

### O backfill: censo barato, reparo caro só no que falta

`manage.py es_backfill_processos` (`search/backfill_processos.py`). Ele **não**
é o `reindexar_processos`: aquele relê 102 milhões de linhas com
`participacoes` e `parte` para consertar 14 milhões. Aqui o censo lê só o `id`
(index-only scan sobre a PK) e pergunta ao ES quais desses ele não tem, com
`search/gate.py::ausentes_no_bloco` — a MESMA primitiva dos dois gates. Só o
ausente paga a leitura cara.

| medido em produção, 24/08/2026 | número |
|---|---:|
| censo puro (bloco de 10.000 ids, PG + ES) | **8.600 ids/s** |
| censo + reparo (faixa com 98,49% ausente) | **863 docs/s** (196.947 em 228 s) |

```bash
manage.py es_backfill_processos --so-amostra          # a régua, sem escrever
manage.py es_backfill_processos --sem-reparo          # censo de uma faixa
manage.py es_backfill_processos --sleep 0.1           # a corrida (retomável)
manage.py es_backfill_processos --zerar-checkpoint
```

Checkpoint em `cache['search:backfill_proc:wm']`, telemetria em
`cache['search:backfill_proc:ultimo']`, e **botão de parar sem deploy**:
`cache.set('search:backfill_proc:off', True)` — encerra na virada do bloco, com
o checkpoint salvo.

### O buraco é CONCENTRADO, e só o censo mostra isso

A amostra por oitavo diz que a faixa 65.199.794–78.239.048 tem **45,99%** de
ausentes. O censo dos primeiros 600.000 pks dessa mesma faixa (24/08/2026) achou
**0 fora, em 60 blocos de 10.000**.

Não é contradição: a amostra mede a EXISTÊNCIA e o TAMANHO do buraco, não a
FORMA dele. Uma faixa com 46% de média pode ser 46% espalhado uniformemente ou
0% num pedaço e 90% em outro — e é o segundo caso. Consequências práticas:

- **Não dá para estimar o custo do backfill pela taxa média.** A vazão alterna
  entre censo puro (3.100–8.600 ids/s medidos) e reparo (863 docs/s medidos);
  a taxa instantânea muda uma ordem de grandeza conforme o pedaço.
- **Um backfill "amostral" seria pior do que inútil**: reparar por amostragem
  numa distribuição concentrada deixa buracos inteiros de pé e produz um
  relatório verde. O censo tem de percorrer TODO pk (`gate.ausentes_no_bloco`
  custa ~40 ms por 10.000 ids justamente para isso ser viável).

### O freio: a busca do site tem prioridade sobre o backfill

O ES é de **um nó só**, 1,74 TB, heap em 71%, I/O-bound. Indexar em massa
compete com a busca. O backfill mede a latência da busca REAL (as mesmas
funções que a tela chama, `busca_api.buscar_processos` e `buscar_movimentacoes`)
a cada 30 blocos e ajusta a própria vazão.

**A calibração exigiu medir o que ninguém tinha medido: a mesma busca de
conteúdo, 7 vezes seguidas, com o backfill PARADO:**

    83,9 · 88,7 · 136,5 · 289,7 · 346,4 · 2.545,2 · 10.116,5 ms

São dois regimes, não ruído — termo frio custa segundos, termo repetido custa
décimos. Disso saem três decisões:

1. **A sonda rotaciona 9 termos.** Repetir a mesma busca mediria "o disco já
   tem isto em cache". Com rotação, a MEDIANA da busca de conteúdo é
   **7.109 ms** (medida em 24/08/2026) — esse é o custo real de um termo frio.
2. **Decide pela mediana de 3 sondas.** Um 10 s solto é normal aqui; um freio
   que dispara por acaso é desligado pelo primeiro operador que o vê.
3. **Limiares com piso E teto.** Só relativo não serve: 4× de 7.109 ms daria
   28.435 ms, praticamente o `ES_TIMEOUT` do cliente — um freio calibrado ali
   só age depois que a busca já morreu.

| sonda | caminho real | tolerância | freio | parada |
|---|---|---:|---:|---:|
| processos (índice ESCRITO, 30 GB) | `busca_ui.buscar_processos_ui` | `ES_TIMEOUT` 30 s | 1.000 ms (piso) | 3.000 ms (piso) |
| conteúdo (1,74 TB, termo frio) | `busca_ui.buscar_conteudo_da_querystring` | `ES_TIMEOUT` 30 s | 13.029–14.217 ms | 25.000 ms (teto) |
| **texto** | `busca_api.ids_por_texto` (listagem de movs + filtro `q` da API) | **`IDS_TEXTO_TIMEOUT` 12 s** | **baseline +20 pp de ABORTO** | **baseline +40 pp** |

#### A terceira sonda existe porque latência alta ali não é espera, é FALHA

`ids_por_texto` passa `request_timeout=12 s` e levanta
`BuscaIndisponivelError(demorou=True)`: acima disso o usuário **não** vê um
resultado lento, vê "a busca demorou mais que o limite e foi interrompida".
Medir só as duas primeiras sondas descreve como "p90 de 14,3 s" o que naquele
caminho é **100% de falha**.

E vigiar só latência tem um buraco lógico: **busca que aborta responde
RÁPIDO.** Um freio que olhasse só o relógio leria 200 ms e concluiria "está
ótimo" exatamente enquanto a busca do usuário falha. Por isso o limiar dessa
sonda é **taxa de aborto**, e é relativo à baseline da própria corrida — se o
cluster já aborta sozinho, exigir 0 travaria o backfill por uma condição que
ele não causou. Com 3 sondas por avaliação a granularidade é 33 pp, e os
limiares casam com ela: **1 aborto em 3 freia, 2 em 3 para**.

Vigia as três, e freia por qualquer uma. Depois de 10 pausas de 60 s sem
melhora a corrida **ABORTA** — com ERRO registrado e checkpoint salvo, que é
dívida visível, não fim silencioso.

**Busca que estoura o timeout conta pelo tempo que gastou, não como erro de
medição.** Para quem está na tela, timeout não é "medição indisponível" — é a
pior latência possível. (Na primeira execução em produção o timeout do cliente
derrubou o comando inteiro antes do primeiro bloco.)

### Quanto o backfill CUSTA à busca — A/B/A/B medido (24/08/2026)

Não é de graça. Medido com **seis janelas alternadas** de 9 sondas cada, A =
backfill parado, B = backfill escrevendo, **ordem invertida a cada rodada**
(A,B / B,A / A,B) para o aquecimento progressivo do page cache não favorecer
sistematicamente o segundo de cada par. Horários UTC.

| janela | início | processos p50 | conteúdo p50 | texto p50 | conteúdo >12 s | **carga de terceiros** (movs/s) |
|---|---|---:|---:|---:|---:|---:|
| A r1 | 20:05:33 | 517 | 5.365 | 1.662 | 0 | 41,4 |
| B r1 | 20:08:52 | 625 | 5.658 | 1.474 | 0 | 58,1 |
| B r2 | 20:12:30 | **908** | **7.541** | **3.095** | **3** | 59,0 |
| A r2 | 20:16:59 | 514 | 5.949 | 1.692 | 0 | 89,5 |
| A r3 | 20:19:09 | 434 | 5.563 | 1.604 | 0 | 97,6 |
| B r3 | 20:22:33 | **846** | **7.219** | **3.506** | **1** | 73,3 |

| sonda | A (parado) | B (escrevendo) | delta |
|---|---:|---:|---:|
| **processos** (o índice ESCRITO) | 514 ms | 846 ms | **+64,6%** |
| conteúdo (1,74 TB) | 5.563 ms | 7.218 ms | **+29,8%** |
| texto (o caminho que aborta aos 12 s) | 1.662 ms | 3.095 ms | **+86,3%** |

Abortos reais: **0 de 27 em A, 0 de 27 em B**. `search.rejected` = 0 nas seis.

**O delta acima é PISO, não teto — a deriva foi ADVERSA.** A última coluna é o
motivo de ela estar na tabela: a carga de terceiros (indexação em
`voyager-movimentacoes-v2`, vinda dos diários e do Datajud) foi **maior nas
janelas A** — mediana 89,5 docs/s contra 59,0 em B. As janelas sem backfill
tinham MAIS concorrência e mesmo assim foram mais rápidas. Sem essa coluna, a
comparação seria uma afirmação; com ela, é verificável.

Confiança por sonda, porque ela não é a mesma: em **`processos` a separação é
perfeita** (pior B = 625 > melhor A = 517) e as janelas A são estáveis entre si
(517 · 514 · 434) — é a sonda mais limpa, e faz sentido, é o índice que o
backfill escreve. Em `conteúdo` e `texto` há sobreposição (B r1 caiu dentro da
faixa dos A's), então nessas duas o "piorou" vale menos.

#### Os 62 milissegundos que mudaram o freio

Nenhum limiar foi cruzado. Mas em B r2 o **p90 da sonda `texto` foi 11.938 ms
contra o corte de 12.000 ms** do `ids_por_texto` — 62 ms de margem. Zero
abortos aconteceram, e zero abortos é a verdade; a distância até a primeira
busca falhando é que não era margem.

Por isso a sonda `texto` passou a ser vigiada **também por latência**, com a
mesma fórmula relativa das outras duas (`_limiares`, 2x/4x com piso e teto) —
regra que já existia, aplicada a uma sonda que ficara de fora. Com a baseline
de 1.662 ms isso dá freio a 3.324 ms, muito antes dos 12.000 ms em que a busca
falha. **Aborto é indicador ATRASADO: quando acende, o usuário já não recebeu
resultado.**

#### Política de operação ≠ limiar do freio

`processos` em B mediu 846 ms, **85% do limiar de freio de 1.000 ms**. Operar
colado no freio apaga a margem e transforma a última linha de defesa no modo
normal de funcionamento. Por isso as corridas usam
`--freio-proc-ms 700`: cede vazão antes, sem tocar no limiar declarado (a
PARADA continua em 3.000 ms). Política mais conservadora que o freio, sempre.

### As 652 falhas da fila `es_index` — medir antes de reprocessar

Quem abrir o `FailedJobRegistry` da `es_index` vai ver centenas de jobs mortos e
querer reprocessar. **Meça primeiro.** Medido em 24/08/2026, amostra ALEATÓRIA
de n=300 com seed declarada (`get_job_ids()` ordena por EXPIRAÇÃO, não por
tempo — isso já disparou alarme falso neste projeto):

| n | job | erro | quando (UTC) |
|---:|---|---|---|
| 259 | `indexar_processos_bulk` | `ValueError: Invalid attribute name` | 15/08 22h |
| 41 | `indexar_movimentacoes_bulk` | `elastic_transport.ConnectionTimeout` | 24/08 19h |

Os 41 jobs de timeout referenciam **19.758 pks**. Amostrados 3.000 e
perguntados ao ES por `_mget`: **8 fora do índice — 0,27%.**

**Timeout de indexação não é erro de indexação.** O `_bulk` chegou, o ES gravou
e a resposta demorou mais que o teto do cliente. Reprocessar os 652 empurraria
~43 mil documentos de movimentação num nó já com fila de escrita, para
recuperar 0,27% — o remédio que piora exatamente a doença que o causou.

Régua para refazer a conta antes de qualquer decisão:

```python
from rq.registry import FailedJobRegistry
from rq.job import Job
import django_rq, random
from search import gate
from search.client import index_name

q = django_rq.get_queue('es_index')
rng = random.Random(20260824)                       # semente DECLARADA
amostra = rng.sample(FailedJobRegistry(queue=q).get_job_ids(), 300)
pks = [p for jid in amostra
       for p in (Job.fetch(jid, connection=q.connection).args[0] or [])]
gate.ausentes_no_bloco(rng.sample(pks, 3000), index_name('movimentacoes'))
```

### ⚠️ Fuso ao ler estas medições

Os horários deste documento são **UTC**. O `asctime` do log do Django neste
projeto é **-03** (America/Sao_Paulo) enquanto o relógio do host, o `mtime` dos
arquivos, o `timezone.now()` e o `ended_at` do RQ são UTC — 3 h de divergência
dentro do mesmo processo. A medição e a causa estão em
[`PATTERNS.md` § Logs](PATTERNS.md). Já custou uma linha do tempo errada num
relatório desta mesma tarefa.

### O gate: o reparo FICOU (amostra aleatória, 24/08/2026)

Indexar não é o mesmo que ficar indexado. Conferido depois, com amostra
aleatória de 4.000 pks por faixa (seed 20260824) e `_mget` por id — a MESMA
régua da medição inicial:

| faixa tratada | pk | existem | fora do índice |
|---|---|---:|---:|
| fatia de reparo (196.947 docs, era **98,49% ausente**) | 100.000.001–100.200.000 | 3.999 | **0 (0,000%)** |
| perna 1 (censo puro) | 65.199.794–65.801.230 | 3.991 | **0 (0,000%)** |
| janelas B do A/B | 65.801.231–67.465.183 | 3.992 | **0 (0,000%)** |

A primeira linha é a que prova o mecanismo: aquela faixa tinha 98,49% dos
processos fora do índice e passou a ter 0 em 3.999 amostrados.

### Estado da corrida (24/08/2026, 21:35 UTC)

Contagem EXATA do próprio censo, por perna — não estimativa:

| perna | blocos | conferidos | fora do índice | indexados |
|---|---:|---:|---:|---:|
| fatia de teste (pk 100,0–100,2M) | 20 | 199.958 | 196.947 | 196.947 |
| perna 1 (pk 65,2–65,8M) | 60 | 600.000 | 0 | 0 |
| janelas B do A/B | 60 | 600.000 | 0 | 0 |
| perna com `--sleep 0.5` | 511 | 5.110.000 | 1.253.476 | 1.252.976 ¹ |
| perna com freio proporcional | 60 | 300.000 | 299.783 | 299.783 |
| **TOTAL** | **711** | **6.809.958** | **1.750.206** | **1.749.706** |

¹ os 500 da diferença são o bloco cujo `_bulk` deu `Connection timed out` — e a
reconferência mostrou que eles ENTRARAM (ver abaixo). O número efetivo é
1.750.206.

Do outro lado, o índice: `voyager-processos` (`_count`, raízes) foi de
**87.709.209 → 89.698.048**, **+1.988.839**. A diferença para os 1.750.206 do
censo é o poller e o gate do Datajud trabalhando em paralelo — o backfill não
reivindica os dois números.

Pela amostra por faixa (`--teto-pk 104317558`, o MESMO sorteio da medição
inicial): **13,99% → 12,87%** fora do índice, ou **14.277.984 → 13.127.270**
extrapolado por densidade × largura.

Checkpoint em `id=74.872.182`; faltam ~29,6 milhões de pks até o topo.
**A corrida não termina numa sessão** — termina por retomada, e é para isso que
o checkpoint existe.

### Timeout no `_bulk` NÃO é documento perdido

Apareceu em produção 20 min depois de a corrida entrar em faixa densa:

    ERROR  Erro no bulk de 500 processos: Connection timed out
    ERROR  bloco id 73913493-73923492 tinha 9574 fora do índice e só 9074
           entraram - 500 NÃO indexados.

Conferido no mesmo bloco logo depois: **10.000 processos no PG, 0 fora do
índice.** Os 500 tinham entrado — o corpo chegou, o ES gravou, e o teto do
cliente estourou antes da resposta. Mesmíssimo caso dos 41 jobs mortos por
`ConnectionTimeout` (19.758 pks, 3.000 amostrados, 8 fora = 0,27%).

As duas reações intuitivas são ruins: deixar no log deixa dívida invisível;
reindexar por precaução empurra documentos num nó já saturado para recuperar
quase nada. Por isso `_reparar` **pergunta de novo** — reconfere pelo índice,
enfileira na `es_index` só o que realmente ficou fora, e trata ES mudo como
dívida visível em vez de sucesso.

### Runbook da corrida

```bash
# retomar (lê o checkpoint sozinho), com a política de operação
manage.py es_backfill_processos --sleep 0.5 --freio-proc-ms 700

# parar AGORA, sem deploy (encerra na virada do bloco, checkpoint salvo)
manage.py shell -c "from django.core.cache import cache; \
    cache.set('search:backfill_proc:off', True)"

# onde parou
manage.py shell -c "from django.core.cache import cache; \
    print(cache.get('search:backfill_proc:wm'), cache.get('search:backfill_proc:ultimo'))"

# a régua, repetindo a medição de 24/08 no MESMO espaço de pk
manage.py es_backfill_processos --so-amostra --teto-pk 104317558
```

Se os diários (ou qualquer porta de coleta) pedirem espaço no ES, **o backfill
cede primeiro**: ele é passivo histórico, retomável por checkpoint e sem prazo;
coleta que não roda hoje é caderno que ninguém vai buscar de novo.

### A sentinela — para o buraco não reabrir calado

`search.jobs.conferir_indice_processos`, cron diário às 06:40
(`SEARCH_SENTINELA_PROCESSOS_ENABLED=0` desliga). Amostra pequena (1.000
candidatos × 8 faixas ≈ 7.800 processos, ~3 s) com a mesma régua; acima de
**1,00%** fora do índice ela loga ERRO nomeando as piores faixas de pk.

Não é um gate de porta e não substitui os dois de cima: eles pegam a escrita
FRESCA de uma porta; ela olha o acervo INTEIRO, porque o buraco não veio de uma
porta só — veio de todo caminho que escreve em lote. Faixa em que o ES não
responde vira "medição INCOMPLETA", nunca "0 fora" (regra nº 6).

### `indexar_processos_bulk` passou a fechar por BYTES

Mesmo defeito que do lado das movimentações matou 83 jobs com `ApiError(413)` e
deixou 45.313 publicações fora do índice (21/08/2026): o lote era contado em
DOCUMENTOS. O doc do processo tem o mesmo formato de risco — `partes`/`advs`
concatenam TODAS as participações. Agora fecha em 20 MB, reusa `_enviar_bulk`
(que divide ao meio no 413) e **devolve quantos documentos o ES aceitou**;
devolver `None` escondia a diferença entre "mandei 500" e "entraram 400".

## Linha de base do índice de processos — 25/08/2026 (medida, antes do dado do E1)

Medição feita ANTES de o dado novo (partes do DJEN, assunto de folha, segredo do
e-SAJ) começar a entrar, para que o "depois" tenha com o que comparar. Régua e
semente declaradas; `scripts/sonda_es_partes.py` reproduz.

### Os dois lados, no mesmo instante

| pergunta | resposta | como |
|---|---:|---|
| `voyager-processos`, raízes | **92.706.806** | `_count` |
| `voyager-processos`, `_cat/indices` | 110.486.709 | conta filho `nested` — **não usar** |
| `tribunals_process` | ~102.149.080 | `reltuples` (ESTIMATIVA) |
| `max(id)` do Postgres | 104.615.119 | exato |
| `voyager-movimentacoes` | 1.521.521.677 | `_count` |
| disco do nó de ES | 1,8 TB usados / 2,9 TB, **1,0 TB livre**, 63% | `_cat/allocation` |
| fila `es_index` | **0** | `q.count` |

### A régua de PRESENÇA (mesma semente e mesmo teto de pk das anteriores)

`manage.py es_backfill_processos --so-amostra --teto-pk 104317558` (seed
`20260824`, 4.000 candidatos por oitavo). A série, sempre no MESMO espaço de pk:

| data | fora do índice |
|---|---:|
| 24/08/2026 (medição inicial) | 13,99% |
| 24/08/2026 (fim da 1ª corrida) | 12,87% |
| **25/08/2026** | **8,94%** (2.799 de 31.301) |

| faixa de pk | existem | fora | % |
|---|---:|---:|---:|
| 3.520–13.042.773 | 3.987 | 0 | 0,00 |
| 13.042.774–26.082.028 | 3.984 | 0 | 0,00 |
| 26.082.029–39.121.283 | 3.988 | 0 | 0,00 |
| 39.121.284–52.160.538 | 3.993 | 0 | 0,00 |
| 52.160.539–65.199.793 | 3.989 | 0 | 0,00 |
| 65.199.794–78.239.048 | 3.390 | 256 | 7,55 (era 45,99) |
| 78.239.049–91.278.303 | 3.992 | 377 | **9,44 (INALTERADO)** |
| 91.278.304–104.317.558 | 3.978 | 2.166 | 54,45 (era 61,44) |

A faixa 6 não mudou um ponto porque o backfill **ainda não chegou nela** — o
checkpoint estava em `id=77.427.989`. Isso é o mapa de onde o backfill já passou,
e é o que decide se dado novo pega carona nele ou precisa de reindex explícito.

### Por que 8,94% aqui e 9,4% na auditoria — as duas estão certas

A auditoria de `.ia/ENRICHMENT.md` mediu **9,4%** com 11.160 processos
estratificados **por tribunal** (200 de cada). Esta mede **por faixa de pk**. Não
é contradição, é desenho diferente medindo coisa diferente:

- **por tribunal** responde "que tribunal está fora da busca" — cada tribunal
  pesa igual, então TRT8 (62%) e o TJSP (26,5%) contam o mesmo;
- **por faixa de pk** responde "que PEDAÇO do acervo está fora" — e mostra o que
  a estratificação por tribunal dilui: **zero nos cinco primeiros oitavos e
  54,45% no último**. O buraco não é de tribunal, é de IDADE DE ESCRITA.

Uma terceira régua, por CONGLOMERADO de pk (a mesma de
`scripts/auditoria_campos.py`, seed `20260825`, 1.500 âncoras × 20 pks
consecutivos = **30.000 processos**), pesa cada tribunal pelo tamanho do acervo
dele e dá o número NACIONAL: **10,64% fora** (3.191 de 30.000). Os três números
convivem; o que não vale é citar um deles sem dizer o desenho.

    tribunal      n     fora   fora%   pg_parte%   doc_parte%
    TJSP      4.753    1.044   22,0%       12,3%       15,7%
    TJRS      1.809      552   30,5%        0,0%        0,0%
    TJSE        431      260   60,3%        0,0%        0,0%
    TRF3      1.523      393   25,8%       66,7%       89,9%
    TJPR      1.771        0    0,0%        0,0%        0,0%

### `exists` do ES mente — reproduzido HOJE, no campo `partes`, e por 5x

A regra nº 4 do CLAUDE.md medida de novo, no índice de produção, no mesmo
instante:

| pergunta | resposta |
|---|---:|
| `exists` no campo `partes` | **92.707.849 = 100,0%** |
| `partes` com CONTEÚDO (amostra de 26.809 docs presentes) | **18,1%** |
| `nested` com ≥1 filho em `participacoes` (contagem EXATA do índice) | **3.645.848 = 3,93%** |

`partes` é `text`; um doc gravado com string vazia continua tendo o campo, e o
`exists` conta. Quem medisse a cobertura de partes por `exists` publicaria
**100%** de um campo que vale **18%**. Foi assim que `partes`/`advs` já foram
servidos como 100% valendo 20%, e continua sendo assim.

E as duas medidas certas **também não são a mesma coisa**: 18,1% têm o texto
legado e só 3,93% têm o `participacoes` estruturado. A diferença — cerca de
**13 milhões de docs** — é doc construído ANTES de o `nested` entrar no builder e
nunca reindexado. A busca estruturada por parte (polo/papel/OAB/documento)
alcança 3,93% do índice, não 18%.

### O buraco de PARTES é de DADO, não de índice — a matriz 2x2

O cruzamento que separa dois problemas com donos diferentes (amostra por
conglomerado, seed `20260825`, 30.000 processos; 26.809 estão no índice):

| | doc TEM `partes` | doc NÃO tem |
|---|---:|---:|
| **PG tem `ProcessoParte`** | 4.853 | **45** ← buraco de ÍNDICE |
| **PG NÃO tem** | **0** | **21.911** ← buraco de DADO |

- **21.911 de 21.956 (99,8%)** dos docs sem partes estão assim porque **o
  Postgres não tem parte nenhuma** para aquele processo. Não há o que indexar.
- **45 (0,2%)** são atraso de índice de verdade.
- **0** casos de doc com parte que o PG não tem — o índice nunca inventa parte.

⇒ Os "89,3% sem partes" da auditoria **não são falha de write-through**. São a
ausência de `ProcessoParte` no banco, que é exatamente o buraco nº 1 da auditoria
(`destinatarios` do DJEN nunca promovidos). O conserto é de DADO primeiro,
índice depois — e o índice não pega esse dado de graça (abaixo).

Na mesma amostra, PG tem o campo e o doc não: `data_autuacao` 5.453 (20,3% —
progresso do reindex, não defeito), `classe_nome` 184, `orgao_julgador` 177,
`juizo` 177, `assunto` 171, `valor_causa` 40.

### O backfill em curso NÃO leva dado novo de carona (exceto onde o doc falta)

`search/backfill_processos.py::rodar` pergunta
`gate.ausentes_no_bloco(ids)` e só reindexa **o que está AUSENTE**. Doc presente
com conteúdo velho ele pula — é censo de presença, não de frescor. Consequência
direta para qualquer dado novo escrito em massa:

| destino | processos | pega carona no backfill? |
|---|---:|---|
| pk ≤ 77,43 M (abaixo do checkpoint) | ~76 M | **não** — o backfill já passou |
| pk > 77,43 M **e ausente** do índice | ~9,1 M | **sim**, de graça, se o dado for escrito ANTES de ele chegar lá |
| pk > 77,43 M **e presente** | ~18 M | **não** |

Ritmo medido em 25/08/2026, em faixa 100% densa (todo bloco de 5.000 com 5.000
fora): **117 pk/s** (35.089 pks em 300,0 s), contra 863 docs/s medidos em faixa
solta — a diferença é o freio pela latência da busca, que é a política correta.

### Latência da busca — a baseline de "não degradou", COM o backfill rodando

`backfill_processos.baseline(n=9)`, as mesmas funções que a tela chama:

| sonda | p50 | pior |
|---|---:|---:|
| processos (`buscar_processos_ui`) | **608,6 ms** | 10.544 ms |
| conteúdo (`buscar_conteudo_da_querystring`) | **5.493,9 ms** | 20.968 ms |
| texto (`ids_por_texto`, corta em 12 s) | **1.773,7 ms** | 12.021 ms |
| **abortos da sonda `texto`** | **2 de 9 = 22,2%** | — |

Os 22,2% de aborto são o estado do nó HOJE, com backfill + diários + Datajud
escrevendo ao mesmo tempo: **2 em cada 9 buscas de texto não devolvem
resultado**, elas são interrompidas em 12 s. Não é "demorou": é falha. Qualquer
carga nova de escrita tem que ser medida contra isto, não contra um nó ocioso.

### Dois pollers com o teto colado — medido nos logs do scheduler

`search/sync_incremental.py` bate o teto em **todo tick**, e o teto é um `return`
mudo (regra nº 2: teto é ALERTA, nunca corte mudo):

| tick (‑03) | `proc_novos` | `proc_atualizados` | `movs_novas` |
|---|---:|---:|---:|
| 21:24 | 20.000 (TETO) | 10.000 (TETO) | 49.300 |
| 21:34 | 20.000 (TETO) | 10.000 (TETO) | 83.027 |
| 21:45 | 20.000 (TETO) | 10.000 (TETO) | 94.347 |
| 21:55 | 20.000 (TETO) | 10.000 (TETO) | 68.014 |
| 22:05 | 20.000 (TETO) | 10.000 (TETO) | 81.860 |
| 22:16 | 20.000 (TETO) | **erro** (lock timeout) | 63.602 |

- **`proc_novos`**: watermark em `id=96.776.271` contra `max(id)=104.615.119` —
  **7,84 milhões de pks atrás**. Avanço medido: 100.030 pks em 52 min =
  **115.419 pks/h**. A base cresce ~12.400 pks/h, então ele CONVERGE, mas leva
  ~76 h. `LIMITE_MOVS_NOVAS` já foi de 50 mil para 400 mil por este mesmo motivo;
  `LIMITE_PROC_NOVOS = 20.000` nunca foi revisitado.
- **`proc_atualizados`**: watermark em **2026-08-19 18:07** — **6 dias atrás**, e
  avança ~30-40 s de relógio por tick de 10 min. Ele NÃO converge: perde ~17:1.
  E desde 22:16 falha com `LockNotAvailable` na leitura de `atualizado_em`,
  quando o watermark não anda de jeito nenhum.
- Nenhum dos dois pega escrita em `ProcessoParte`: ela não muda `Process.id` nem
  `Process.atualizado_em`.

### As 858 falhas da `es_index` — medidas por TIPO, no índice CERTO

Amostra aleatória de 300 (seed `20260825`). A armadilha aqui é medir junto:
`indexar_movimentacoes_bulk` carrega pk de MOVIMENTAÇÃO, e perguntá-los ao índice
de PROCESSOS devolve "99,67% fora" — número que não significa nada. Separando:

| job | n na amostra | erro | pks referenciados | amostrados | fora do índice |
|---|---:|---|---:|---:|---:|
| `indexar_processos_bulk` | 202 | `ValueError: Invalid attribute name` | 216 | 216 | **0 (0,00%)** |
| `indexar_movimentacoes_bulk` | 51 | `ConnectionTimeout` | 43.949 | 3.000 | 273 (9,10%) |
| `indexar_movimentacoes_bulk` | 45 | `statement_timeout` no SELECT do PG | (idem) | | |

Os 202 jobs de processo que morreram com `Invalid attribute name` **nunca
rodaram** e mesmo assim não deixaram buraco: outra via (backfill/poller) já
levou aqueles processos ao índice. Do lado das movimentações há ~11 mil
publicações fora, extrapolando — dívida pequena e visível, não emergência.

### O que custa levar as partes do E1 ao índice (a conta, ANTES de executar)

Tamanho do documento medido em produção, com o builder real (`processo_to_doc`),
sobre amostra por conglomerado (seed `20260825`):

| doc | n | JSON p50 | JSON média | filhos `nested` |
|---|---:|---:|---:|---:|
| COM parte | 412 | 2.304 B | 2.369 B | p50 **5**, média 5,10, máx 18 |
| SEM parte | 600 | 913 B | 953 B | 0 |
| **delta** | | | **+1.416 B/doc** | **+5,1 docs Lucene/doc** |

Escalando para os ~86,7 M processos que o E1 alcança:

    +1.416 B x 86,7 M = 122,8 GB de _source
    store hoje = 26,8 GB / 92,6 M docs = 289 B/doc contra ~953 B de _source
      => razão store/_source ~ 0,30  =>  ~37 GB de índice a mais
    filhos nested: 86,7 M x 5,1 = ~442 milhões de docs Lucene a mais

Disco: **~37 GB** de 1,0 TB livre — cabe, mas sai do MESMO orçamento que o
backfill do `tjsp-dje` reivindica (~772 GB, `.ia/DIARIOS.md` §13.2).

Tempo, à vazão medida nesta casa (863 docs/s em faixa solta; 117-200 docs/s sob
o freio da política de operação):

| fatia | processos | a 863/s | a 200/s |
|---|---:|---:|---:|
| carona no backfill (pk > checkpoint e ausente) | ~9,1 M | grátis | grátis |
| **reindex direcionado (o resto)** | **~77,6 M** | **25,0 h** | **107,8 h** |

## Plano de reindex (backfill dos campos novos)

Pré-requisito: `PUT _mapping` aditivo nos 2 índices (o general aplica — os
campos novos de `search/mappings.py`). Sem isso o bulk cria dynamic mapping.

1. **Fase 1 — processos enriquecidos dos tribunais Juriscope** (têm partes/
   valor; é onde a busca estruturada acende):
   `reindexar_processos --tribunal X --somente-enriquecidos --sleep 0.5`
   na ordem TJSP, TRF1, TRF3, TRF4, TJMG, TRF6, TRF5, TJAL, TRF2, TJMA, TJPR.
2. **Fase 2 — resto dos tribunais Juriscope** (sem `--somente-enriquecidos`):
   popula proc_digits/inserido_em/etc. nos não-enriquecidos (~31,7M docs no
   total das fases 1+2).
3. **Fase 3 — demais tribunais** (~39,4M docs).
4. **Movimentações**: NÃO reindexar 1,16B de uma vez. `tipo_documento`/
   `proc_digits` valem pra docs novos via tail; retroativo sob demanda:
   `reindexar_movimentacoes --tribunal X --desde YYYY-MM-DD --sleep`.
5. **Contínuo**: cron diário `reindexar_processos --desde-id <max_id da última
   rodada>` cobre os Process novos da ingestão (bulk_create não tem signal).

Throttle: `--sleep 0.5` em horário comercial, `0.2` de madrugada — o DB é
I/O-sensível (regra da casa). Rodar no container web de prod com `docker exec
-d`, monitorar `SHOW POOLS` no pgbouncer. ETA a ~800–1.200 docs/s efetivos:
fases 1+2 ≈ 8–11h, total 71M ≈ 18–25h de execução líquida.

## Lacunas registradas (não resolver aqui)

- **valor real do precatório (Falcon)**: `valor_causa` é o valor DA CAUSA
  (esparso; federal NULL). O valor de ofício/requisitório vive no Falcon —
  integração futura (campo novo `valor_precatorio` quando houver pipeline).
- **`destinatarios`/`destinatario_advogados` (JSON da DJEN)**: partes cruas
  pré-enriquecimento; não indexadas (ruído/duplicação vs ProcessoParte).
- **`proc_alt`/`proc_apens`**: sempre NULL (compat de schema Jusbrasil) —
  candidatos a receber os CNJ vinculados (tema TJSP incidentes CNJ).
- **`data_envio`, `meio`, `numero_comunicacao`, `hash`**: operacionais,
  ficam só no PG.

## Freshness contínua — sync incremental (11/08)

O write-through por signal cobre `.save()` unitário. TRÊS fluxos de volume escrevem
em LOTE e não disparam signal — eram buracos por onde o ES envelhecia:

| Fluxo | Escrita | Cobertura |
|---|---|---|
| Ingestão DJEN (Process/Movimentacao) | `bulk_create` | ❌ era → ✅ `sync_es_incremental` |
| Reclassificação em lote | `.update()` | ❌ era (QA mediu −26% no confirmado) → ✅ idem |
| `tem_sinal_precatorio` de processo NOVO | nada computava | ❌ era → ✅ computado no tick |
| Enriquecimento (drainer `apply_batch`) | `bulk_update` | ✅ enfileira bulk (fix 11/08) |
| `.save()` unitário | ORM | ✅ signal |

**`search/sync_incremental.py`** → job `sync_es_incremental` no scheduler (10 min, INLINE):
- **processos novos**: keyset por `id > watermark` → computa o sinal **antes** de indexar
  (senão o doc entra com `tem_sinal_precatorio` NULL) → enfileira bulk na `es_index`;
- **processos atualizados**: watermark por `atualizado_em`; se bate o teto, avança só
  até o último lido (não pula o resto);
- **movimentações novas**: keyset por `id`.
- 1º tick **ancora** os watermarks no topo (não reprocessa a base — pra isso existe o
  `reindexar_processos`). Tetos por tick, kill-switch `cache.set('sync_es:off', True)`,
  bloco que falha não derruba o tick. 7 testes em `tests/test_sync_incremental.py`.

### Três coisas que este poller NÃO é (21/08/2026)

Aprendidas medindo a primeira coleta real de diário próprio — os 8 cadernos do
DJE/TJSP de 12/03/2025, 220.544 linhas (`.ia/DIARIOS.md` §12):

1. **Não é write-through, é POLLER.** Entre um tick e o seguinte, nada se move.
   Uma coleta que termina às 21:44:43 fica esperando o tick das 21:53:01 — e
   quem mediu duas vezes dentro dessa janela leu "parado" onde era "espera".
   Foram **27.619** publicações fora do índice, com a edição marcada `ok`.
   Quem escreve em lote e precisa de garantia **enfileira o próprio lote**;
   depender só daqui é depender de um watermark em cache, de um freio por
   tamanho de fila e de um kill-switch — três coisas invisíveis para quem
   escreveu o dado.
2. **Não perdoa falha de enqueue.** Até 21/08/2026 `_enfileirar_movs` engolia a
   exceção, devolvia 0, e quem chamava avançava o watermark **assim mesmo**. O
   keyset só anda pra frente: um soluço do Redis apagava do índice, para
   sempre, a leva inteira daquele tick, com um `WARNING` no log. Agora o erro
   propaga e o **watermark não anda**.
3. **A "corrida do watermark" não é o que parece.** Um salto de 1,48 milhão de
   `id` num tick que enfileirou só 23.598 linhas assusta, mas foi medido: em
   5 de 5 faixas, PG = ES = o que o tick enfileirou. O buraco de `id` é valor
   de sequência queimado por `bulk_create(ignore_conflicts=True)`, não linha
   invisível.

### O `_bulk` fecha por BYTES, não só por documento

`search/jobs.py::indexar_movimentacoes_bulk` montava um `_bulk` com 500
documentos, e o texto de uma publicação não tem tamanho fixo. Medido em
21/08/2026 no `FailedJobRegistry` da fila `es_index`: **91 jobs mortos**, 83
deles `ApiError(413)` (corpo acima do `http.max_content_length` do ES, 100 MB
por default) e 8 `JobTimeoutException`. Eles referenciavam **45.500**
publicações, **45.313 fora do índice** — e **ninguém reprocessa o
`FailedJobRegistry`**. Um teto do lado do ES virou corte mudo do nosso lado.

Agora: `BULK_MAX_BYTES = 20 MB` fecha o lote por tamanho, `413` **divide ao
meio e reenvia** (`_enviar_bulk`), documento que sozinho não cabe vira ERRO
registrado, e o `DEFAULT_TIMEOUT` da fila `es_index` subiu de 120 s para 600 s
(os 120 s eram de quando o job valia UMA publicação).
