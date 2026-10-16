"""Catálogo do MCP para as telas (landing pública do /mcp e /dashboard/mcp/).

Sai do `server.TOOLS` em tempo de execução — a lista que o cliente MCP recebe
em `tools/list` é a mesma que a tela mostra. A página de setup tinha "Tools
disponíveis (17)" escrito à mão quando o servidor já servia 43.

`GRUPOS` só ORGANIZA: tool que existe no servidor e não está em grupo nenhum
cai em "Outras" (aparece, nunca some), e `tests/test_mcp_catalogo.py` falha
pra lembrar de catalogar.
"""
from dataclasses import dataclass, field

#: (slug, título, descrição, ícone do sprite voyager-icons.svg, tools)
GRUPOS = (
    ('panorama', 'Panorama e perguntas sobre a base',
     'Quanto temos, contra o que o CNJ declara, e qualquer contagem, soma ou série com filtro.',
     'telescope',
     ('visao_geral', 'descrever_base', 'agregar', 'buscar_processos', 'status_cobertura', 'listar_fontes')),
    ('precatorios', 'Precatórios e devedores',
     'Onde estão os precatórios, quem deve, a saúde fiscal do devedor e quanto o crédito vale hoje.',
     'target',
     ('mapa_precatorios', 'panorama_estado', 'ranking_entidades', 'ficha_entidade', 'ente_fiscal',
      'capag_rating', 'consultar_cnpj', 'valor_presente', 'atualizar_valor')),
    ('processo', 'Um processo a fundo',
     'Do CNJ ao dossiê: estágio, partes, valores, linha do tempo e classificação de lead.',
     'dossier',
     ('dossie_jurimetrico', 'contexto_processo', 'get_processo', 'linha_do_tempo', 'list_movimentacoes',
      'get_partes', 'classificacao_lead')),
    ('diarios', 'Diários e publicações',
     'O texto dos diários oficiais — DJEN, tribunais e municípios — e o PDF de cada publicação.',
     'transmission',
     ('buscar_diarios', 'get_documento', 'get_pdf', 'djen_publicacoes', 'querido_diario')),
    ('partes', 'Partes, advogados e entidades',
     'Quem aparece em quais processos, por nome, documento ou OAB, e o histórico de cada um.',
     'users',
     ('buscar_entidades', 'buscar_por_parte', 'historico_parte', 'buscar_valores')),
    ('jurimetria', 'Jurimetria, precedentes e autos',
     'Padrões de julgamento, casos parecidos, teses do STJ e o texto dos autos vetorizados.',
     'scale',
     ('jurimetria', 'jurimetria_agregada', 'precedentes', 'casos_similares', 'stj_temas_repetitivos',
      'buscar_zordon', 'ler_chunks', 'explicar_modelos', 'sgt_decodificar')),
    ('monitoramento', 'Monitoramento',
     'Avisos de novidade por termo ou processo. As únicas tools que gravam algo.',
     'radar',
     ('monitorar_termo', 'monitorar_processo', 'listar_detections')),
)

#: Nome curto, em linguagem de gente, pro card. Tool sem título mostra o nome.
TITULOS = {
    'visao_geral': 'Panorama do acervo',
    'descrever_base': 'O que dá pra perguntar',
    'agregar': 'Contagens, somas e séries',
    'buscar_processos': 'Processos por trás de um número',
    'status_cobertura': 'Cobertura por área',
    'listar_fontes': 'Fontes cobertas',
    'mapa_precatorios': 'Mapa de precatórios',
    'panorama_estado': 'Um estado em detalhe',
    'ranking_entidades': 'Quem mais deve',
    'ficha_entidade': 'Ficha de uma entidade',
    'ente_fiscal': 'Saúde fiscal do devedor',
    'capag_rating': 'Rating CAPAG do estado',
    'consultar_cnpj': 'Cadastro por CNPJ',
    'valor_presente': 'Valor justo de um precatório',
    'atualizar_valor': 'Correção monetária',
    'dossie_jurimetrico': 'Dossiê jurimétrico',
    'contexto_processo': 'Contexto completo',
    'get_processo': 'Dados do processo',
    'linha_do_tempo': 'Linha do tempo',
    'list_movimentacoes': 'Movimentações',
    'get_partes': 'Partes do processo',
    'classificacao_lead': 'Classificação de lead',
    'buscar_diarios': 'Busca nos diários',
    'get_documento': 'Uma publicação',
    'get_pdf': 'PDF da publicação',
    'djen_publicacoes': 'Publicações no DJEN',
    'querido_diario': 'Diários municipais',
    'buscar_entidades': 'Busca por nome, documento ou OAB',
    'buscar_por_parte': 'Processos de uma parte',
    'historico_parte': 'Histórico de parte ou advogado',
    'buscar_valores': 'Processos por valor da causa',
    'jurimetria': 'Estatísticas do acervo',
    'jurimetria_agregada': 'Padrões de julgamento',
    'precedentes': 'Precedentes relevantes',
    'casos_similares': 'Casos parecidos',
    'stj_temas_repetitivos': 'Temas repetitivos do STJ',
    'buscar_zordon': 'Busca semântica nos autos',
    'ler_chunks': 'Texto dos autos',
    'explicar_modelos': 'Como os modelos decidem',
    'sgt_decodificar': 'Códigos do CNJ (TPU)',
    'monitorar_termo': 'Monitorar um termo',
    'monitorar_processo': 'Monitorar um processo',
    'listar_detections': 'Detecções recentes',
}

#: As três bases — vêm das `instructions` do servidor, em forma de tela.
BASES = (
    ('processos', 'O que conhecemos em detalhe', 'partes, valor da causa, classificação de lead'),
    ('acervo', 'O que os tribunais declararam ao CNJ', 'o universo — sem partes nem valor'),
    ('publicacoes', 'O texto dos diários oficiais', 'cerca de 1,8 bilhão de publicações'),
)

#: Regras de honestidade com o dado (as mesmas das `instructions`).
REGRAS = (
    'Resposta marcada como parcial, com "outros" ou com avisos não é o universo — e o Claude diz isso.',
    'Campo ausente é desconhecido, não zero nem "não". Valor da causa falta em muitos processos.',
    '"Não achei em processos" não é "não existe": o acervo do CNJ é conferido antes de afirmar.',
)


@dataclass
class Tool:
    nome: str
    titulo: str
    resumo: str
    escrita: bool
    externa: bool


@dataclass
class Grupo:
    slug: str
    titulo: str
    descricao: str
    icone: str
    tools: list = field(default_factory=list)


def _resumo(descricao):
    """Primeira frase da descrição (é o que cabe num card)."""
    texto = ' '.join(descricao.split())
    for fim in ('. ', '? '):
        corte = texto.find(fim)
        if 0 < corte < 220:
            return texto[:corte + 1]
    return texto if len(texto) <= 220 else texto[:217].rstrip() + '…'


def _tool(t):
    anot = t.get('annotations') or {}
    return Tool(
        nome=t['name'],
        titulo=TITULOS.get(t['name'], t['name']),
        resumo=_resumo(t.get('description', '')),
        escrita=not anot.get('readOnlyHint', True),
        externa=bool(anot.get('openWorldHint')),
    )


def grupos():
    from mcp_server.server import TOOLS
    por_nome = {t['name']: t for t in TOOLS}
    usados = set()
    saida = []
    for slug, titulo, descricao, icone, nomes in GRUPOS:
        g = Grupo(slug, titulo, descricao, icone)
        for nome in nomes:
            if nome in por_nome:
                g.tools.append(_tool(por_nome[nome]))
                usados.add(nome)
        if g.tools:
            saida.append(g)
    sobra = [_tool(t) for t in TOOLS if t['name'] not in usados]
    if sobra:
        saida.append(Grupo('outras', 'Outras', 'Tools ainda sem grupo no catálogo.', 'sparkles', sobra))
    return saida


def contexto():
    """Tudo que as duas telas precisam, num dict só."""
    from mcp_server import server
    gs = grupos()
    tools = [t for g in gs for t in g.tools]
    recursos = [{'uri': r['uri'], 'descricao': r['description']} for r in server.RESOURCES.values()]
    recursos += [{'uri': r['template'], 'descricao': r['description']}
                 for r in server.RESOURCE_TEMPLATES.values()]
    prompts = [{'nome': n, 'descricao': p['description'],
                'argumentos': [a['name'] for a in p['arguments']]} for n, p in server.PROMPTS.items()]
    return {
        'grupos': gs,
        'total_tools': len(tools),
        'tools_leitura': [t.nome for t in tools if not t.escrita],
        'tools_escrita': [t.nome for t in tools if t.escrita],
        'total_externas': sum(t.externa for t in tools),
        'recursos': recursos,
        'prompts': prompts,
        'bases': BASES,
        'regras': REGRAS,
    }
