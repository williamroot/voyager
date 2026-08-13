"""Mappings Elasticsearch — schema compatível com Jusbrasil/Digesto."""

ANALYZER_SETTINGS = {
    "analysis": {
        "analyzer": {
            "portuguese_asciifolding": {
                "tokenizer": "standard",
                "filter": ["lowercase", "asciifolding"],
            }
        }
    }
}

MOV_MAPPING = {
    "settings": ANALYZER_SETTINGS,
    "mappings": {
        "properties": {
            "id":              {"type": "long"},
            "tribunal":        {"type": "keyword"},
            "source":          {"type": "integer"},
            "publish_date":    {"type": "date"},
            "available_at":    {"type": "date"},
            "detected_at":     {"type": "date"},
            "body":            {"type": "text", "analyzer": "portuguese_asciifolding"},
            "docurl":          {"type": "keyword", "index": False},
            "cached_docurl":   {"type": "keyword", "index": False},
            "proc":            {"type": "keyword"},
            "proc_alt":        {"type": "keyword"},
            "proc_apens":      {"type": "keyword"},
            "advs":            {"type": "text", "analyzer": "portuguese_asciifolding"},
            "partes":          {"type": "text", "analyzer": "portuguese_asciifolding"},
            "assunto":         {"type": "text", "analyzer": "portuguese_asciifolding"},
            "assunto_norm":    {"type": "nested", "properties": {
                                    "tipo": {"type": "integer"},
                                    "subtipo": {"type": "integer"}}},
            "processo_id":     {"type": "long"},
            "classe_nome":     {"type": "keyword"},
            "codigo_classe":   {"type": "keyword"},
            "secao_diario":    {"type": "keyword"},
            "periodico_diario_slug": {"type": "keyword"},
            "periodico_orgao_slug":  {"type": "keyword"},
            "periodico_caderno_slug": {"type": "keyword"},
            "ativo":           {"type": "boolean"},
            "recorte_id":      {"type": "long"},
            "tipo_comunicacao": {"type": "keyword"},
            "tipo_documento":  {"type": "keyword"},        # teor: Sentença/Despacho/Edital…
            "nome_orgao":      {"type": "keyword"},
            # CNJ só dígitos (20) — busca "colável": aceita o número com ou sem
            # máscara. Derivado no doc builder (não exige reanalyzer no índice).
            "proc_digits":     {"type": "keyword"},
        }
    },
}

PROC_MAPPING = {
    "settings": ANALYZER_SETTINGS,
    "mappings": {
        "properties": {
            "id":              {"type": "long"},
            "tribunal":        {"type": "keyword"},
            "uf":              {"type": "keyword"},        # mapa comercial: agrega por estado
            "tem_sinal_precatorio": {"type": "boolean"},   # Fase 0: possível precatório (sinal DJEN)
            "source":          {"type": "integer"},
            "proc":            {"type": "keyword"},
            "proc_digits":     {"type": "keyword"},        # CNJ só dígitos (busca colável)
            "classe_nome":     {"type": "keyword"},
            "codigo_classe":   {"type": "keyword"},
            "assunto":         {"type": "text", "analyzer": "portuguese_asciifolding"},
            "assunto_codigo":  {"type": "keyword"},        # TPU: filtro exato por assunto
            "advs":            {"type": "text", "analyzer": "portuguese_asciifolding"},
            "partes":          {"type": "text", "analyzer": "portuguese_asciifolding"},
            # Partes ESTRUTURADAS (nested) — habilita "processos onde X é EXECUTADO",
            # filtro por polo/papel/CPF-CNPJ/OAB combinado com filtros do processo
            # (valor, uf, classificacao) numa query só. Decisão: nested aqui em vez
            # de índice voyager-partes separado — ES não tem join, e o caso de uso
            # rei (leads) filtra PROCESSOS por atributos de parte. Cardinalidade
            # por doc é pequena (mediana <10; limite ES nested_objects=10k). A visão
            # parte-cêntrica (/dashboard/partes) segue no Postgres (pontes).
            "participacoes":   {"type": "nested", "properties": {
                                    "parte_id":     {"type": "long"},
                                    "nome":         {"type": "text",
                                                     "analyzer": "portuguese_asciifolding",
                                                     "fields": {"raw": {"type": "keyword",
                                                                        "ignore_above": 256}}},
                                    "documento":    {"type": "keyword"},   # CPF/CNPJ (pode vir mascarado)
                                    "oab":          {"type": "keyword"},
                                    "tipo":         {"type": "keyword"},   # pf|pj|advogado|desconhecido
                                    "polo":         {"type": "keyword"},   # ativo|passivo|outros
                                    "papel":        {"type": "keyword"},   # AUTOR|EXEQUENTE|ADVOGADO…
                                    "eh_advogado":  {"type": "boolean"}}},
            "orgao_julgador":  {"type": "keyword"},
            "juizo":           {"type": "keyword"},
            "valor_causa":     {"type": "double"},
            "ano_cnj":         {"type": "integer"},
            "data_autuacao":   {"type": "date"},           # idade real do processo
            "primeira_movimentacao_em": {"type": "date"},  # duração (jurimetria)
            "total_movimentacoes": {"type": "integer"},
            "ultima_movimentacao_em": {"type": "date"},
            "inserido_em":     {"type": "date"},           # crescimento da base
            "segredo_justica": {"type": "boolean"},
            "classificacao":   {"type": "keyword"},
            "classificacao_score": {"type": "double"},
            "classificacao_versao": {"type": "keyword"},   # qual modelo classificou (v6, v7…)
            "classificacao_em": {"type": "date"},          # freshness do confirmado
            # cobertura direto no ES: % validado calculável por QUALQUER agregação/filtro
            # (antes só existia no cache do Postgres, agregado por tribunal)
            "enriquecido":     {"type": "boolean"},
            "enriquecido_em":  {"type": "date"},
            # granularidade além do boolean: nao_encontrado ≠ pendente ≠ erro
            # (cobertura honesta — pré-PJe/físico não é "falta enriquecer")
            "enriquecimento_status": {"type": "keyword"},
            # devedor público no polo passivo (coração do precatório) — derivado das
            # partes que o doc builder já carrega (regex RE_ENTE_PUBLICO do estágio)
            "tem_ente_publico_passivo": {"type": "boolean"},
        }
    },
}

# --------------------------------------------------------------------------- #
# voyager-entidades — cadastro canônico de "quem deve" (base do autocomplete)
# --------------------------------------------------------------------------- #
# Uma linha por ENTIDADE (não por `Parte`): o INSS são 610 linhas de
# tribunals_parte / 610 CNPJs / 11 grafias, e vira 1 documento aqui.
# Doc builder: search/entidades.py::grupo_to_doc. Ver .ia/SEARCH_SCHEMA.md.
#
# AUTOCOMPLETE = `search_as_you_type` (subcampo `.autocomplete`), NÃO edge-ngram.
# Por quê:
#   - o ES já gera os shingles (`._2gram`, `._3gram`) e o `._index_prefix`, e o
#     `multi_match type=bool_prefix` casa "fazenda sao pau" sem analyzer feito à
#     mão — edge-ngram exigiria um par analyzer/search_analyzer manual (o erro
#     clássico é esquecer o `search_analyzer` e o índice casar ngram×ngram);
#   - `analyzer: portuguese_asciifolding` faz `uniao` achar `UNIÃO` — requisito
#     medido: o usuário digita sem acento;
#   - o custo do índice (o ponto fraco do search_as_you_type) é irrelevante
#     aqui: são ~1-2M docs curtos, não os 71M de processos.
# `variantes` também é autocompletável: quem digita "inss" precisa achar a
# entidade cujo `nome_canonico` é "INSTITUTO NACIONAL DO SEGURO SOCIAL".
ENTIDADE_MAPPING = {
    "settings": {
        **ANALYZER_SETTINGS,
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "properties": {
            # `cnpj:29979036` ou `nome:<sha1[:20]>` — igual ao `_id` (idempotência)
            "entidade_id":     {"type": "keyword"},
            # procedência da chave: 'cnpj' = identidade PROVADA por documento;
            # 'nome' = heurística de grafia. Quem consome PRECISA saber.
            "chave":           {"type": "keyword"},
            "raiz_cnpj":       {"type": "keyword"},        # 8 dígitos: une matriz+filiais
            "nome_canonico":   {"type": "text",
                                "analyzer": "portuguese_asciifolding",
                                "fields": {
                                    "raw": {"type": "keyword", "ignore_above": 256},
                                    "autocomplete": {
                                        "type": "search_as_you_type",
                                        "analyzer": "portuguese_asciifolding"}}},
            # chave de fusão por nome (debug/join) — não é pra busca humana
            "nome_normalizado": {"type": "keyword", "ignore_above": 256},
            # O PRODUTO: todas as grafias, ordenadas por frequência desc. Vira o
            # OR de match_phrase contra o campo texto `partes` (100% dos docs).
            # É o campo de RECALL — inclui grafia de 1 linha (typo de cartório
            # que mesmo assim aparece em processo real).
            "variantes":       {"type": "text",
                                "analyzer": "portuguese_asciifolding",
                                "fields": {
                                    "raw": {"type": "keyword", "ignore_above": 256}}},
            # O campo de PRECISÃO: só as grafias com peso (≥2 linhas e ≥1% da
            # entidade, top-3 sempre). É ele que o autocomplete busca. Sem essa
            # separação, o INSS — que tem UMA linha grafada "Instituto Nacional
            # do Seguro Social (UNIÃO)" entre 764 — vinha em 1º na busca por
            # "uniao" (medido 12/08): o grande sequestra a busca do alheio.
            "variantes_busca": {"type": "text",
                                "analyzer": "portuguese_asciifolding",
                                "fields": {
                                    "autocomplete": {
                                        "type": "search_as_you_type",
                                        "analyzer": "portuguese_asciifolding"}}},
            # frequência de cada grafia, MESMA ORDEM de `variantes` — deixa o
            # consumidor cortar cauda antes de montar o OR (over-match)
            "variantes_n":     {"type": "integer"},
            "n_variantes":     {"type": "integer"},
            "variantes_truncadas": {"type": "boolean"},    # bateu MAX_VARIANTES
            # a frase do nome não identifica ninguém: 1 token sem atestação de
            # cadastro ("JOSÉ", 2 linhas, casava 1.796.174 processos) ou grafia
            # truncada num conectivo ("MUNICIPIO DE", 467.493). Fica no índice
            # pra auditoria, mas FORA do escopo de contagem e do autocomplete
            # (search/entidades.py::nome_suspeito, decisão 13).
            # Ausente = índice construído antes desta decisão: continua valendo.
            "nome_suspeito":   {"type": "boolean"},
            "nome_suspeito_motivo": {"type": "keyword"},   # token_unico|truncado
            "documentos":      {"type": "keyword"},        # CNPJs formatados (sem mascarados)
            "n_documentos":    {"type": "integer"},
            # CNPJs que o tribunal digitou ERRADO pra esta entidade (decisão
            # 12). SEPARADOS de `documentos` — não são desta PJ — e guardados
            # porque são a evidência do erro de cadastro do tribunal.
            "documentos_secundarios": {"type": "keyword"},
            "n_documentos_secundarios": {"type": "integer"},
            # `entidade_id` de cada entidade-CNPJ engolida pela decisão 12 —
            # o id é determinístico, então a fusão é auditável e reversível
            "entidades_absorvidas": {"type": "keyword"},
            # linhas cujo CNPJ veio MASCARADO (LGPD do tribunal) e por isso NÃO
            # fundiram por raiz — auditoria da decisão de não fundir por máscara
            "documentos_mascarados": {"type": "integer"},
            "tipo":            {"type": "keyword"},        # pj|desconhecido|… (dominante)
            # grupos-por-nome absorvidos pela consolidação nome→cnpj
            "grupos_absorvidos": {"type": "integer"},
            "eh_ente_publico": {"type": "boolean"},        # RE_ENTE_PUBLICO (+complemento)
            "ente_publico_por_complemento": {"type": "boolean"},
            # nº de linhas de `Parte` fundidas. PROXY DE PREVALÊNCIA — NÃO é
            # contagem de processos, e é um proxy FRACO: medido 12/08, o
            # "Gerente Executivo do INSS de São Paulo/Centro" tem 109 linhas
            # contra 764 do INSS inteiro, e por isso vinha na FRENTE dele no
            # autocomplete de "inss". Continua no doc como fallback de ranking
            # (ver `n_processos`) e como âncora de auditoria do build.
            "n_partes":        {"type": "integer"},
            # quantas de `n_partes` vieram de entidade ABSORVIDA. A dominância
            # da decisão 14 é sobre atestação PRÓPRIA (`n_partes` menos este) —
            # sem isso a fusão vira bola de neve: o cadastro emprestado pela 1ª
            # passada dava dominância a quem não a tinha na 2ª.
            "n_partes_absorvidas": {"type": "integer"},
            # A contagem REAL: quantos docs de `voyager-processos` casam o OR de
            # `match_phrase` das `variantes_busca` (search/entidades.py::
            # query_contagem). Medido: INSS = 4.402.239 contra 764 `n_partes`.
            # NÃO sai do build (`grupo_to_doc` não escreve este campo): vem do
            # comando `contar_processos_entidades`, que é ES→ES e escreve por
            # `_bulk`/`update` parcial. `long` porque é contagem sobre um índice
            # de 77M docs que cresce todo dia.
            #
            # AUSENTE = "não contamos" — NÃO é zero. O escopo da contagem é
            # deliberadamente parcial (as ~182k entidades que disputam o
            # autocomplete, de 1,14M): quem ficou fora não tem o campo, e
            # `query_autocomplete` cai no `n_partes`. Zero é resposta MEDIDA
            # (o OR não achou processo nenhum) e ranqueia ABAIXO de desconhecido.
            # Quem consumir precisa distinguir os dois — por isso ausência, e
            # nunca 0 de enchimento.
            "n_processos":     {"type": "long"},
            # quando a contagem foi feita: o número envelhece (a base de
            # processos cresce), então quem exibe precisa saber a idade dele
            "n_processos_em":  {"type": "date"},
            "parte_id_min":    {"type": "long"},           # âncora pro join no Postgres
            "atualizado_em":   {"type": "date"},
        }
    },
}