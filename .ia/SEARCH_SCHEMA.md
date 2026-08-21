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
| `Process.save()` (apply_event, classificação por save, admin) | ✅ | signal |
| Ingestão DJEN (Process novo via `bulk_create`) | ❌ | **reindex incremental `--desde-id`** (cron sugerido) |
| Drainer `apply_batch` (`bulk_update`/`bulk_create`) | ✅ | enqueue explícito `search.jobs.indexar_processos_bulk` no fim do batch (fix 2026-08) |
| ProcessoParte (create/delete individual) | — sem signal DE PROPÓSITO | o processo é reindexado pelo `processo.save()` que fecha todo enriquecimento; signal por-linha multiplicaria a fila ~2N por processo |
| Comandos de manutenção em massa (dedup_partes, recategorizar_tipo_partes, SQL cru) | ❌ | rodar `reindexar_processos` direcionado depois |

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
