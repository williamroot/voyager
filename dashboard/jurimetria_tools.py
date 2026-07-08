"""Tools de jurimetria — funções sobre DADOS REAIS (Voyager + Juriscope + Zordon).

Núcleo compartilhado pelo agente in-process (dashboard/jurimetria_agente.py) e pelo
servidor MCP (mcp_jurimetria.py). Cada tool devolve dict JSON-serializável; NUNCA
inventa — se não tem o dado, devolve vazio/None. O LLM decide o que chamar e narra.

Contrato de cada tool: {name, description, parameters(JSON schema), handler(**args)}.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _bounded(fn, ms: int = 1500):
    """Roda `fn()` (uma leitura ORM) sob statement_timeout na MESMA transação —
    evita seq-scan longo em icontains sobre milhões de linhas travar a narrativa.
    None se estourar o tempo/erro."""
    from django.db import connection, transaction
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout=%s", [ms])
            return fn()
    except Exception:  # noqa: BLE001
        return None


def _count_bounded(qs, ms: int = 1500):
    return _bounded(qs.count, ms)


# ---------------- handlers ----------------

def dossie_jurimetrico(cnj: str) -> dict:
    """Dossiê determinístico: diagnóstico (estágio/veredito/recomendação), indicadores,
    survival (Kaplan-Meier), cronograma de pagamento, Juriscope e classificação."""
    from .jurimetria_dossie import montar_dossie
    d = montar_dossie(cnj)
    if d.get('erro') or d.get('processando'):
        return {'erro': d.get('erro') or 'processo ainda em enriquecimento', 'cnj': cnj}
    c, dg, pc = d.get('cabecalho', {}), d.get('diagnostico', {}), d.get('precatorio', {})
    js = pc.get('juriscope') or {}
    return {
        'cnj': d.get('cnj'),
        'tribunal': c.get('tribunal'), 'classe': c.get('classe_nome'),
        'assunto': c.get('assunto_nome'), 'orgao_julgador': c.get('orgao_julgador'),
        'data_autuacao': str(c.get('data_autuacao') or ''),
        'diagnostico': {'estagio': dg.get('estagio'), 'veredito': dg.get('veredito'),
                        'recomendacao': (dg.get('recomendacao') or {}).get('label'),
                        'sinais': dg.get('sinais'), 'indicadores': dg.get('indicadores')},
        'survival_kaplan_meier': dg.get('chance'),
        'cronograma_pagamento': pc.get('pagamento'),
        'homologacao_calculos': pc.get('homologacao'),
        'juriscope': {k: js.get(k) for k in ('natureza', 'ente_nome', 'valor_acao',
                      'valor_acao_corrigido', 'ordem_orcamentaria', 'ano_ordem_orcamentaria',
                      'data_oficio', 'files_downloaded')} if js else {},
        'classificacao': pc.get('classificacao'),
    }


def linha_do_tempo(cnj: str) -> dict:
    """Movimentações (DJEN) do processo em ordem + ritmo (movs, span em dias, dias/mov)."""
    from tribunals.models import Process
    from .jurimetria_narrativa import _ritmo_processual
    p = Process.objects.filter(numero_cnj=cnj).first()
    if not p:
        return {'cnj': cnj, 'n': 0, 'itens': [], 'aviso': 'processo não está no acervo'}
    r = _ritmo_processual(p)
    r['aviso'] = 'visão via DJEN — pode ser parcial p/ processos antigos/e-SAJ'
    return r


def precedentes(query: str, limit: int = 6) -> dict:
    """Busca semântica de acórdãos/precedentes no Zordon (com desfecho quando houver)."""
    from . import zordon_client
    res = zordon_client.buscar(query=(query or '')[:300], limit=min(limit, 12))
    itens = (res or {}).get('results') or []
    return {'query': query, 'total': len(itens),
            'itens': [{'cnj': it.get('numero_cnj'), 'tipo': it.get('doc_tipo'),
                       'orgao': it.get('orgao'), 'relator': it.get('relator'),
                       'resultado': it.get('resultado') or it.get('desfecho'),
                       'trecho': (it.get('snippet') or '')[:280]} for it in itens[:limit]],
            'erro': (res or {}).get('erro')}


def jurimetria_agregada(metrica: str, tema: str = '', tribunal: str = '') -> dict:
    """Agregações do Zordon sobre acórdãos. metrica ∈ {relatores, orgaos, classes,
    temas, serie, resumo}. 'relatores' = juízes/desembargadores e seus padrões."""
    from . import zordon_client
    if metrica not in ('relatores', 'orgaos', 'classes', 'temas', 'serie', 'resumo'):
        return {'erro': f'métrica inválida: {metrica}', 'validas': ['relatores', 'orgaos', 'classes', 'temas', 'serie', 'resumo']}
    params = {}
    if tema:
        params['q'] = tema[:200]
    if tribunal:
        params['tribunal'] = tribunal
    res = zordon_client.jurimetria(metrica, **params)
    return {'metrica': metrica, 'tema': tema, 'resultado': res}


def historico_parte(nome: str, limit: int = 15) -> dict:
    """Outras ações da parte/advogado no acervo Voyager + distribuição de classificação
    (quantos viraram precatório/pré-precatório) — o track-record da parte."""
    from django.db.models import Count
    from tribunals.models import ProcessoParte
    if not nome or len(nome) < 4:
        return {'erro': 'nome muito curto'}
    # busca por documento (exato, indexado) quando parece CPF/CNPJ; senão nome
    # (icontains, sem índice trigram → só best-effort com timeout curto).
    import re as _re
    doc = _re.sub(r'\D', '', nome)
    if len(doc) in (11, 14):
        qs = ProcessoParte.objects.filter(parte__documento__contains=doc).select_related('processo')
    else:
        qs = ProcessoParte.objects.filter(parte__nome__icontains=nome[:80]).select_related('processo')
    total = _count_bounded(qs.values('processo_id').distinct())
    amostra = _bounded(lambda: list(qs.values('processo__numero_cnj', 'processo__classificacao',
                                              'papel', 'polo')[:limit])) or []
    distrib: dict = {}
    for a in amostra:
        k = a['processo__classificacao'] or 'sem_classificacao'
        distrib[k] = distrib.get(k, 0) + 1
    if total is None and not amostra:
        return {'nome': nome, 'aviso': 'busca por nome sem índice — use CPF/CNPJ p/ track-record completo'}
    return {'nome': nome, 'total_processos': total, 'distribuicao_classificacao': distrib,
            'amostra': [{'cnj': a['processo__numero_cnj'], 'classificacao': a['processo__classificacao'],
                         'papel': a['papel'], 'polo': a['polo']} for a in amostra]}


def casos_similares(assunto: str, tribunal: str = '', limit: int = 10) -> dict:
    """Casos do mesmo assunto/tema no acervo Voyager (distribuição de desfecho via
    classificação) + precedentes com resultado no Zordon — o 'venceu/perdeu' do tema."""
    from django.db.models import Count
    from tribunals.models import Process
    base = Process.objects.filter(assunto_nome__icontains=(assunto or '')[:80])
    if tribunal:
        base = base.filter(tribunal_id=tribunal)
    total = _count_bounded(base)
    # distribuição bounded via amostra (evita GROUP BY seq-scan em milhões)
    amostra = _bounded(lambda: list(base.values_list('classificacao', flat=True)[:500])) or []
    distrib: dict = {}
    for cl in amostra:
        k = cl or 'sem_classificacao'
        distrib[k] = distrib.get(k, 0) + 1
    prec = precedentes(f'{assunto} {tribunal}'.strip(), limit=limit)
    return {'assunto': assunto, 'tribunal': tribunal or 'todos', 'total_no_acervo': total,
            'distribuicao_classificacao': distrib, 'precedentes_com_desfecho': prec.get('itens', [])}


# ---------------- registry ----------------

def _fp(nome: str):
    """Handler que aponta pra uma função de dashboard/fontes_publicas (lazy import)."""
    def _handler(**kwargs):
        from . import fontes_publicas
        return getattr(fontes_publicas, nome)(**kwargs)
    _handler.__name__ = nome
    return _handler


TOOLS: list[dict] = [
    {'name': 'dossie_jurimetrico',
     'description': 'Dossiê determinístico do processo por CNJ: estágio no ciclo de vida (direito creditório/pré-precatório/precatório), veredito, indicadores, previsão de virar precatório (Kaplan-Meier), cronograma de pagamento, dados do Juriscope. Comece SEMPRE por aqui.',
     'parameters': {'type': 'object', 'properties': {'cnj': {'type': 'string', 'description': 'Número CNJ do processo'}}, 'required': ['cnj']},
     'handler': dossie_jurimetrico},
    {'name': 'linha_do_tempo',
     'description': 'Movimentações do processo em ordem cronológica + ritmo processual (nº de movs, dias de tramitação, dias por movimentação).',
     'parameters': {'type': 'object', 'properties': {'cnj': {'type': 'string'}}, 'required': ['cnj']},
     'handler': linha_do_tempo},
    {'name': 'precedentes',
     'description': 'Busca semântica de acórdãos/precedentes relevantes (corpus Zordon), com órgão, relator e desfecho quando disponível.',
     'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'tema/assunto para buscar precedentes'}, 'limit': {'type': 'integer', 'default': 6}}, 'required': ['query']},
     'handler': precedentes},
    {'name': 'jurimetria_agregada',
     'description': 'Agregações estatísticas sobre acórdãos (Zordon): relatores (juízes/desembargadores e seus padrões), orgaos (câmaras/varas), classes, temas, serie (evolução temporal), resumo. Use para dados de juiz e padrões decisórios.',
     'parameters': {'type': 'object', 'properties': {'metrica': {'type': 'string', 'enum': ['relatores', 'orgaos', 'classes', 'temas', 'serie', 'resumo']}, 'tema': {'type': 'string'}, 'tribunal': {'type': 'string'}}, 'required': ['metrica']},
     'handler': jurimetria_agregada},
    {'name': 'historico_parte',
     'description': 'Track-record de uma parte ou advogado: quantas outras ações tem no acervo e quantas viraram precatório/pré-precatório (distribuição de classificação).',
     'parameters': {'type': 'object', 'properties': {'nome': {'type': 'string'}, 'limit': {'type': 'integer', 'default': 15}}, 'required': ['nome']},
     'handler': historico_parte},
    {'name': 'casos_similares',
     'description': 'Casos do mesmo assunto/tema: distribuição de desfecho (classificação) no acervo Voyager + precedentes com resultado no Zordon. Use para "quantos venceram/perderam" no tema.',
     'parameters': {'type': 'object', 'properties': {'assunto': {'type': 'string'}, 'tribunal': {'type': 'string'}, 'limit': {'type': 'integer', 'default': 10}}, 'required': ['assunto']},
     'handler': casos_similares},
    # ── Fontes PÚBLICAS externas (sem login) — dashboard/fontes_publicas.py ──
    {'name': 'ente_fiscal',
     'description': 'Saúde fiscal do ENTE DEVEDOR (SICONFI/Tesouro): estoque de precatórios vencidos, Dívida Consolidada Líquida e RCL. Estima a banda de pagamento anual da EC 136/2025 (1–5% da RCL conforme estoque/RCL). Use pra avaliar se/quando o ente paga. Passe uf (ex. "SP") pro estado.',
     'parameters': {'type': 'object', 'properties': {'uf': {'type': 'string', 'description': 'sigla do estado devedor, ex. SP'}, 'id_ente': {'type': 'string', 'description': 'código IBGE (município=7 díg); alternativa a uf'}, 'ano': {'type': 'integer', 'default': 2023}}, 'required': []},
     'handler': _fp('ente_fiscal')},
    {'name': 'consultar_cnpj',
     'description': 'Cadastro público (RFB) por CNPJ: razão social, situação, natureza jurídica, município. Use pro ente devedor ou partes pessoa-jurídica.',
     'parameters': {'type': 'object', 'properties': {'cnpj': {'type': 'string'}}, 'required': ['cnpj']},
     'handler': _fp('consultar_cnpj')},
    {'name': 'stj_temas_repetitivos',
     'description': 'Temas repetitivos e TESES FIRMADAS do STJ (Dados Abertos oficiais) por assunto. Traz precedentes qualificados REAIS (tese, questão, súmula, tema STF vinculado). Use pra fundamentar a análise com jurisprudência.',
     'parameters': {'type': 'object', 'properties': {'assunto': {'type': 'string', 'description': 'termo/tema, ex. "precatório", "honorários", "juros fazenda"'}, 'limit': {'type': 'integer', 'default': 8}}, 'required': ['assunto']},
     'handler': _fp('stj_temas_repetitivos')},
    {'name': 'djen_publicacoes',
     'description': 'Publicações oficiais (DJEN/Comunica) por CNJ, CPF/CNPJ da parte ou OAB — texto integral das intimações/despachos. Fonte pública nacional, on-demand.',
     'parameters': {'type': 'object', 'properties': {'numero_processo': {'type': 'string'}, 'documento': {'type': 'string', 'description': 'CPF ou CNPJ da parte'}, 'numero_oab': {'type': 'string'}, 'uf_oab': {'type': 'string'}, 'limit': {'type': 'integer', 'default': 20}}, 'required': []},
     'handler': _fp('djen_publicacoes')},
    {'name': 'sgt_decodificar',
     'description': 'Traduz um código do DataJud (classe/assunto/movimento/documento) para a descrição oficial via tabelas TPU do CNJ. tipo_tabela: C=classe, A=assunto, M=movimento, D=documento.',
     'parameters': {'type': 'object', 'properties': {'tipo_tabela': {'type': 'string', 'enum': ['C', 'A', 'M', 'D']}, 'codigo': {'type': 'string'}}, 'required': ['tipo_tabela', 'codigo']},
     'handler': _fp('sgt_decodificar')},
    {'name': 'querido_diario',
     'description': 'Busca em diários oficiais MUNICIPAIS (Querido Diário) por termo — útil pra editais/fila de precatório municipal. Traz trechos + link do texto integral.',
     'parameters': {'type': 'object', 'properties': {'termo': {'type': 'string'}, 'municipio_ibge': {'type': 'string'}, 'limit': {'type': 'integer', 'default': 10}}, 'required': ['termo']},
     'handler': _fp('querido_diario')},
]

_BY_NAME = {t['name']: t for t in TOOLS}


def openai_specs() -> list[dict]:
    """Tools no formato OpenAI/Ollama (function calling)."""
    return [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'],
             'parameters': t['parameters']}} for t in TOOLS]


def dispatch(name: str, args: dict) -> dict:
    """Executa uma tool pelo nome. Nunca levanta — devolve {'erro': ...} em falha."""
    tool = _BY_NAME.get(name)
    if not tool:
        return {'erro': f'tool desconhecida: {name}'}
    try:
        return tool['handler'](**(args or {}))
    except Exception as exc:  # noqa: BLE001
        logger.warning('jurimetria_tools.dispatch %s falhou: %s', name, exc)
        return {'erro': f'{type(exc).__name__}: {exc}'}
