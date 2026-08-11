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