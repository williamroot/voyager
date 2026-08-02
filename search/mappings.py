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
            "nome_orgao":      {"type": "keyword"},
        }
    },
}

PROC_MAPPING = {
    "settings": ANALYZER_SETTINGS,
    "mappings": {
        "properties": {
            "id":              {"type": "long"},
            "tribunal":        {"type": "keyword"},
            "source":          {"type": "integer"},
            "proc":            {"type": "keyword"},
            "classe_nome":     {"type": "keyword"},
            "codigo_classe":   {"type": "keyword"},
            "assunto":         {"type": "text", "analyzer": "portuguese_asciifolding"},
            "advs":            {"type": "text", "analyzer": "portuguese_asciifolding"},
            "partes":          {"type": "text", "analyzer": "portuguese_asciifolding"},
            "orgao_julgador":  {"type": "keyword"},
            "valor_causa":     {"type": "double"},
            "ano_cnj":         {"type": "integer"},
            "total_movimentacoes": {"type": "integer"},
            "ultima_movimentacao_em": {"type": "date"},
            "classificacao":   {"type": "keyword"},
            "classificacao_score": {"type": "double"},
        }
    },
}