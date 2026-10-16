"""MCP Server — expõe tools e resources do Voyager pra LLMs/agentes."""
import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import acesso, delegates, macro, oauth, tokens
from .auth import check_rate_limit, validate_mcp_token

logger = logging.getLogger('voyager.mcp.server')


@dataclass(frozen=True)
class Chamador:
    """Quem está chamando: `c:<ApiClient.pk>` (token legado) ou `u:<User.id>` (OAuth)."""

    chave: str
    rotulo: str

TOOLS = [
    {
        'name': 'buscar_diarios',
        'description': 'Busca textual em diários oficiais (compatível Jusbrasil/Digesto). '
                       'Retorna publicações que contêm o termo pesquisado.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Termos de busca no texto da publicação. Todos os termos precisam aparecer (E); entre aspas = frase exata.'},
                'tribunal': {'type': 'string', 'description': 'Sigla do tribunal (ex: TRF1, TJSP; várias separadas por vírgula) — opcional'},
                'data_inicio': {'type': 'string', 'description': 'Data inicial (YYYY-MM-DD) — opcional'},
                'data_fim': {'type': 'string', 'description': 'Data final (YYYY-MM-DD) — opcional'},
                'size': {'type': 'integer', 'default': 10, 'description': 'Número de resultados (máx 100)'},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'get_documento',
        'description': 'Obtém o detalhe completo de uma publicação de diário oficial pelo ID.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'doc_id': {'type': 'integer', 'description': 'ID do documento (Movimentacao.id)'},
            },
            'required': ['doc_id'],
        },
    },
    {
        'name': 'get_processo',
        'description': 'Obtém dados completos de um processo judicial pelo número CNJ.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cnj': {'type': 'string', 'description': 'Número do processo no formato CNJ (NNNNNNN-DD.AAAA.J.TR.OOOO)'},
            },
            'required': ['cnj'],
        },
    },
    {
        'name': 'list_movimentacoes',
        'description': 'Lista as movimentações/publicações de um processo.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cnj': {'type': 'string', 'description': 'Número CNJ do processo'},
                'limit': {'type': 'integer', 'default': 50, 'description': 'Máximo de resultados'},
                'offset': {'type': 'integer', 'default': 0, 'description': 'Deslocamento da paginação'},
            },
            'required': ['cnj'],
        },
    },
    {
        'name': 'get_partes',
        'description': 'Lista as partes (autores, réus, advogados) de um processo.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cnj': {'type': 'string', 'description': 'Número CNJ do processo'},
            },
            'required': ['cnj'],
        },
    },
    {
        'name': 'listar_fontes',
        'description': 'Lista os diários/tribunais cobertos pelo Voyager.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'status_cobertura',
        'description': 'Retorna a matriz de cobertura de tribunais por área (federal, estadual, trabalhista, superior).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'area': {'type': 'string', 'enum': ['federal', 'estadual', 'trabalhista', 'superior']},
            },
            'required': ['area'],
        },
    },
    {
        'name': 'monitorar_termo',
        'description': 'Cria um monitoramento de termo em diários oficiais (receber via webhook).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'term': {'type': 'string', 'description': 'Termo a monitorar (mín 3 caracteres, sem vírgula/exclamação/igual)'},
                'tribunais': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Lista de siglas de tribunais — opcional'},
            },
            'required': ['term'],
        },
    },
    {
        'name': 'monitorar_processo',
        'description': 'Cria um monitoramento de processo por número CNJ (receber via webhook).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cnj': {'type': 'string', 'description': 'Número CNJ do processo a monitorar'},
            },
            'required': ['cnj'],
        },
    },
    {
        'name': 'listar_detections',
        'description': 'Lista detecções recentes de monitoramentos (termos, pessoas, processos).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'desde': {'type': 'string', 'description': 'Data inicial (ISO 8601) — opcional'},
                'limit': {'type': 'integer', 'default': 50, 'description': 'Máximo de resultados'},
            },
        },
    },
    {
        'name': 'get_pdf',
        'description': 'Obtém a URL do PDF armazenado de uma publicação de diário.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'doc_id': {'type': 'integer', 'description': 'ID do documento'},
            },
            'required': ['doc_id'],
        },
    },
    {
        'name': 'classificacao_lead',
        'description': 'Obtém a classificação e score de ML de um processo (lead de precatório/direito creditório). '
                       'Requer autorização (MCP_ENABLE_CLASSIFICACAO=True).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cnj': {'type': 'string', 'description': 'Número CNJ do processo'},
            },
            'required': ['cnj'],
        },
    },
    {
        'name': 'buscar_entidades',
        'description': 'Busca processos por entidade (parte, advogado, pessoa física/jurídica) via nome, '
                       'documento (CPF/CNPJ) ou OAB. Retorna processos + estatísticas agregadas.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'nome': {'type': 'string', 'description': 'Nome da parte (parcial, case-insensitive)'},
                'documento': {'type': 'string', 'description': 'CPF ou CNPJ (parcial)'},
                'oab': {'type': 'string', 'description': 'Número OAB (ex: SP123456)'},
                'tribunal': {'type': 'string', 'description': 'Filtrar por tribunal (ex: TRF1) — opcional'},
                'size': {'type': 'integer', 'default': 20, 'description': 'Máximo de processos'},
            },
        },
    },
    {
        'name': 'buscar_valores',
        'description': 'Busca processos por faixa de valor da causa. Retorna processos ordenados por valor '
                       'decrescente + estatísticas (soma, média, mediana, min, max).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'tribunal': {'type': 'string', 'description': 'Filtrar por tribunal — opcional'},
                'classe': {'type': 'string', 'description': 'Filtrar por classe processual (parcial) — opcional'},
                'valor_min': {'type': 'number', 'description': 'Valor mínimo da causa — opcional'},
                'valor_max': {'type': 'number', 'description': 'Valor máximo da causa — opcional'},
                'size': {'type': 'integer', 'default': 20, 'description': 'Máximo de processos'},
            },
        },
    },
    {
        'name': 'jurimetria',
        'description': 'Estatísticas de jurimetria: volume de processos, distribuição por tribunal/classe/ano, '
                       'valores, classificação de leads, tipos de andamentos mais comuns.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'tribunal': {'type': 'string', 'description': 'Filtrar por tribunal — opcional'},
                'classe': {'type': 'string', 'description': 'Filtrar por classe — opcional'},
                'ano': {'type': 'integer', 'description': 'Filtrar por ano CNJ — opcional'},
                'metrica': {
                    'type': 'string',
                    'enum': ['volume', 'valores', 'classificacao', 'andamentos'],
                    'default': 'volume',
                    'description': 'Tipo de estatística: volume (processos), valores (causa), classificacao (leads), andamentos (tipos)',
                },
            },
        },
    },
    {
        'name': 'contexto_processo',
        'description': 'Dossiê completo de um processo: dados cadastrais, partes (polo ativo/passivo/outros), '
                       'últimas 10 movimentações, timeline anual, classificação de lead, valor da causa, '
                       'órgão julgador, enriquecimento. Use para dar contexto jurídico completo sobre um processo.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cnj': {'type': 'string', 'description': 'Número CNJ do processo'},
            },
            'required': ['cnj'],
        },
    },
    {
        'name': 'buscar_por_parte',
        'description': 'Busca processos por nome de parte (pessoa/empresa) e retorna estatísticas da parte: '
                       'quantos processos, em quais tribunais, em qual polo (ativo/passivo), valores, classificação.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'nome': {'type': 'string', 'description': 'Nome da parte (parcial, case-insensitive, mín 3 chars)'},
                'tribunal': {'type': 'string', 'description': 'Filtrar por tribunal — opcional'},
                'size': {'type': 'integer', 'default': 20, 'description': 'Máximo de processos por parte'},
            },
            'required': ['nome'],
        },
    },
]

TOOL_MAP = {
    'buscar_diarios': delegates.buscar_diarios,
    'get_documento': delegates.get_documento,
    'get_processo': delegates.get_processo,
    'list_movimentacoes': delegates.list_movimentacoes,
    'get_partes': delegates.get_partes,
    'listar_fontes': delegates.listar_fontes,
    'status_cobertura': delegates.status_cobertura,
    'monitorar_termo': delegates.monitorar_termo,
    'monitorar_processo': delegates.monitorar_processo,
    'listar_detections': delegates.listar_detections,
    'get_pdf': delegates.get_pdf,
    'classificacao_lead': delegates.classificacao_lead,
    'buscar_entidades': delegates.buscar_entidades,
    'buscar_valores': delegates.buscar_valores,
    'jurimetria': delegates.jurimetria,
    'contexto_processo': delegates.contexto_processo,
    'buscar_por_parte': delegates.buscar_por_parte,
}


# --------------------------------------------------------------------------- #
# Tools MACRO — panorama do acervo e perguntas sobre a base (mcp_server/macro.py)
# --------------------------------------------------------------------------- #
_FILTROS_MAPA = {
    'tribunal': {'type': 'string', 'description': 'Sigla do tribunal (ex: TJSP)'},
    'classificacao': {'type': 'string', 'enum': list(macro.CLASSIFICACOES)},
    'parte': {'type': 'string', 'description': 'Texto no nome das partes'},
    'entidade_id': {'type': 'string', 'description': 'ID canônico (de ranking_entidades) — mais preciso que `parte`'},
    'ano_min': {'type': 'integer', 'description': 'Ano CNJ mínimo'},
    'ano_max': {'type': 'integer', 'description': 'Ano CNJ máximo'},
}

MACRO_TOOLS = [
    {
        'name': 'visao_geral',
        'description': 'PANORAMA do acervo inteiro: quantos processos conhecemos em detalhe, quanto o CNJ '
                       'declara (acervo Datajud), publicações (total, últimos 7 dias, data mais recente), '
                       'entidades, distribuição por classificação, e por tribunal a razão "temos ÷ existe". '
                       'Comece por aqui pra qualquer pergunta macro. Cache de 1h.',
        'inputSchema': {'type': 'object', 'properties': {
            'atualizar': {'type': 'boolean', 'default': False, 'description': 'Ignora o cache (~15s)'},
        }},
    },
    {
        'name': 'descrever_base',
        'description': 'Descreve os índices consultáveis (processos, acervo, publicacoes), os campos que '
                       'cada um aceita em `agregar`, os valores de classificação, exemplos de perguntas e '
                       'as RESSALVAS de cada dado. Leia antes de montar uma agregação.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'agregar',
        'description': 'Responde perguntas sobre a base: contagem/soma/média/mediana/min/max, com filtros e '
                       'agrupamento por até 2 campos (campo de data vira série por ano/mês/dia). Total '
                       'sempre exato. Ex.: precatórios por tribunal; soma do valor da causa por ano no TJSP; '
                       'publicações por mês com um termo. Campos aceitos: ver descrever_base.',
        'inputSchema': {'type': 'object', 'properties': {
            'indice': {'type': 'string', 'enum': list(macro.INDICES), 'default': 'processos'},
            'agrupar_por': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2,
                            'description': 'Campos de agrupamento (0 a 2), ex.: ["tribunal"], ["uf", "classificacao"]'},
            'filtros': {'type': 'object', 'description': 'Igualdade por campo: {"tribunal": "TJSP"}, '
                                                         'lista = qualquer um: {"uf": ["SP", "RJ"]}, null = campo ausente'},
            'faixas': {'type': 'object', 'description': 'Intervalos: {"valor_causa": {"gte": 100000}, '
                                                        '"publish_date": {"gte": "2026-01-01", "lte": "2026-06-30"}}'},
            'texto': {'type': 'string', 'description': 'Filtra por texto (partes/advogados/assunto em processos; '
                                                       'corpo em publicacoes). Todos os termos (E); entre aspas = frase exata'},
            'metrica': {'type': 'string', 'enum': list(macro.METRICAS), 'default': 'contagem',
                        'description': 'Métricas de valor usam valor_causa (processos) / valores_citados (publicacoes)'},
            'intervalo': {'type': 'string', 'enum': list(macro.INTERVALOS), 'description': 'Pra agrupar por data'},
            'top': {'type': 'integer', 'default': macro.TOP_DEFAULT, 'description': f'Grupos devolvidos (máx {macro.TOP_MAX})'},
            'ordenar': {'type': 'string', 'enum': list(macro.ORDENACOES), 'default': 'n'},
        }},
    },
    {
        'name': 'mapa_precatorios',
        'description': 'Mapa de precatórios do Brasil: por UF (sem `uf`) ou os tribunais de uma UF (com `uf`). '
                       'Cada grupo traz volume, valor, possíveis (sinal no texto), confirmados (ML), '
                       'cobertura de enriquecimento e densidade. É o mesmo número da tela /overview.',
        'inputSchema': {'type': 'object', 'properties': {
            'uf': {'type': 'string', 'description': 'UF pra abrir os tribunais (ex: SP)'},
            'tipo': {'type': 'string', 'enum': ['potencial', 'confirmado'],
                     'description': 'Só processos com sinal de precatório / só confirmados'},
            'valor_min': {'type': 'number'}, 'valor_max': {'type': 'number'},
            **_FILTROS_MAPA,
        }},
    },
    {
        'name': 'panorama_estado',
        'description': 'Um estado em detalhe: precatórios por tipo de processo, ano, tribunal, entidade '
                       'devedora e classificação. Mesmo dado da página de estado do Overview.',
        'inputSchema': {'type': 'object', 'properties': {
            'uf': {'type': 'string', 'description': 'Sigla da UF (ex: SP)'},
            'metrica': {'type': 'string', 'enum': ['possiveis', 'confirmados', 'todos'], 'default': 'todos'},
            **_FILTROS_MAPA,
        }, 'required': ['uf']},
    },
    {
        'name': 'ranking_entidades',
        'description': 'Quem mais litiga / quem deve: o cadastro canônico de entidades (grafias unificadas, '
                       'ex.: as 104 grafias do INSS viram uma). Busque por nome (`q`) pra achar o `id` e depois '
                       'abra a ficha_entidade.',
        'inputSchema': {'type': 'object', 'properties': {
            'q': {'type': 'string', 'description': 'Nome, sigla ou CNPJ'},
            'ente': {'type': 'boolean', 'description': 'Só entes públicos'},
            'ordenar': {'type': 'string', 'enum': ['n_processos', 'n_partes', 'n_variantes', 'nome', 'relevancia']},
            'tamanho': {'type': 'integer', 'default': 20},
            'cursor': {'type': 'string', 'description': 'Próxima página (do resultado anterior)'},
        }},
    },
    {
        'name': 'ficha_entidade',
        'description': 'Os processos de UMA entidade (id de ranking_entidades), explodidos por UF, tribunal, '
                       'ano e classificação.',
        'inputSchema': {'type': 'object', 'properties': {
            'entidade_id': {'type': 'string'},
            'uf': {'type': 'string'},
            'tribunal': {'type': 'string'},
            'classificacao': {'type': 'string', 'enum': list(macro.CLASSIFICACOES)},
            'ano_min': {'type': 'integer'}, 'ano_max': {'type': 'integer'},
        }, 'required': ['entidade_id']},
    },
    {
        'name': 'buscar_processos',
        'description': 'Lista processos com os filtros da tela de busca, total EXATO e paginação por cursor. '
                       'Use depois de agregar, pra ver os processos por trás de um número.',
        'inputSchema': {'type': 'object', 'properties': {
            'parte': {'type': 'string', 'description': 'Nome de parte (entre aspas = nome exato)'},
            'advogado': {'type': 'string'},
            'filtros': {'type': 'object', 'description': 'Chaves aceitas: cnj, documento (CPF/CNPJ), oab, '
                                                         'tribunal, uf, classificacao, codigo_classe, assunto_codigo, '
                                                         'ano_min, ano_max, valor_min, valor_max, parte_polo '
                                                         '(ativo|passivo), vara'},
            'ordenar': {'type': 'string', 'enum': ['relevancia', 'recente', 'valor_desc', 'valor_asc']},
            'size': {'type': 'integer', 'default': 20, 'description': 'máx 100'},
            'cursor': {'type': 'string'},
        }},
    },
]

TOOLS.extend(MACRO_TOOLS)
TOOL_MAP.update({
    'visao_geral': macro.visao_geral,
    'descrever_base': macro.descrever_base,
    'agregar': macro.agregar,
    'mapa_precatorios': macro.mapa_precatorios,
    'panorama_estado': macro.panorama_estado,
    'ranking_entidades': macro.ranking_entidades,
    'ficha_entidade': macro.ficha_entidade,
    'buscar_processos': macro.buscar_processos,
})


# --------------------------------------------------------------------------- #
# Tools do agente de jurimetria (dashboard/jurimetria_tools.py), as MESMAS que o
# chat in-process e o MCP stdio `mcp_jurimetria` usam: uma definição, três portas.
# --------------------------------------------------------------------------- #
#: `ler_arquivo` lê anexo do chat do dashboard pelo id — fora do chat não tem
#: dono pra conferir, e um id adivinhado leria o arquivo de outra pessoa.
JURIMETRIA_FORA = {'ler_arquivo'}

#: Tools que consultam serviço de fora (BCB, Tesouro, RFB, STJ, CNJ, Querido Diário).
MUNDO_EXTERNO = {'ente_fiscal', 'capag_rating', 'consultar_cnpj', 'stj_temas_repetitivos',
                 'djen_publicacoes', 'sgt_decodificar', 'atualizar_valor', 'querido_diario'}


def _jurimetria(nome):
    def _handler(**kwargs):
        from dashboard import jurimetria_tools
        return jurimetria_tools.dispatch(nome, kwargs)
    _handler.__name__ = nome
    return _handler


def _registrar_jurimetria():
    from dashboard import jurimetria_tools
    for t in jurimetria_tools.TOOLS:
        if t['name'] in JURIMETRIA_FORA or t['name'] in TOOL_MAP:
            continue
        TOOLS.append({'name': t['name'], 'description': t['description'], 'inputSchema': t['parameters']})
        TOOL_MAP[t['name']] = _jurimetria(t['name'])


_registrar_jurimetria()

#: As duas únicas tools que ESCREVEM (cadastram monitoramento).
ESCRITA = {'monitorar_termo', 'monitorar_processo'}

for _t in TOOLS:
    _t['annotations'] = {
        'readOnlyHint': _t['name'] not in ESCRITA,
        'openWorldHint': _t['name'] in MUNDO_EXTERNO,
    }


def _extract_token(request):
    """Extrai MCP token do header Authorization ou query param."""
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    esquema, _, valor = auth.partition(' ')
    if esquema.lower() == 'bearer' and valor.strip():
        return valor.strip()
    return request.GET.get('token', '')


def _negar(status, erro):
    """401 leva o desafio OAuth (RFC 9728): é assim que o Claude descobre o
    login. Até 16/10/2026 era 403 seco — e o conector do claude.ai, que só fala
    OAuth, não tinha por onde entrar."""
    resp = JsonResponse({'error': erro}, status=status)
    resp['Cache-Control'] = 'no-store'
    if status == 401 and oauth.base():
        resp['WWW-Authenticate'] = oauth.desafio()
    return resp


def _origem_recusada(request):
    """Origin fora da allowlist ⇒ 403 (MUST da spec contra DNS rebinding).
    Sem Origin (Claude Code, curl, servidor→servidor) passa."""
    origem = request.META.get('HTTP_ORIGIN')
    if origem is None:
        return False
    permitidas = {o.rstrip('/') for o in settings.MCP_ALLOWED_ORIGINS}
    if oauth.base():
        permitidas.add(oauth.base())
    return origem.rstrip('/') not in permitidas


def _auth(request):
    """Valida auth. Retorna (Chamador, None) ou (None, resposta de erro).

    Dois caminhos: o token OAuth (login do dashboard + MCPAcesso, o do conector
    do Claude) e o legado `ApiClient.mcp_token` (UUID, integrações e configs
    antigas de Claude Code com header fixo).
    """
    if _origem_recusada(request):
        return None, _negar(403, 'origin_recusada')
    token = _extract_token(request)
    if not token:
        return None, _negar(401, 'token_ausente')

    chamador = None
    cliente = validate_mcp_token(token)
    if cliente is not None:
        chamador = Chamador(chave=f'c:{cliente.pk}', rotulo=f'cliente={cliente.nome}')
    elif oauth.base():
        sessao = tokens.resolver_access(token)
        if sessao is not None:
            if not oauth.resource_valido(sessao.resource):
                # Token emitido para outro recurso (audiência errada) não vale aqui
                return None, _negar(401, 'token_invalido')
            veredito = acesso.verificar(sessao.usuario_id)
            if veredito == acesso.INDISPONIVEL:
                return None, _negar(503, 'allowlist_indisponivel')
            if veredito != acesso.PERMITIDO:
                # verificar() já derrubou as sessões: o Claude reabre o login.
                return None, _negar(401, 'acesso_revogado')
            chamador = Chamador(chave=f'u:{sessao.usuario_id}', rotulo=f'usuario={sessao.usuario_id}')
    if chamador is None:
        return None, _negar(401, 'token_invalido')
    if not check_rate_limit(chamador.chave):
        return None, _negar(429, 'rate_limit_excedido')
    return chamador, None


@csrf_exempt
def mcp_descriptor(request):
    """GET /mcp/.well-known/mcp.json — descriptor do server."""
    return JsonResponse({
        'name': 'voyager',
        'version': '1.0.0',
        'description': 'Voyager — busca e monitoramento em diários oficiais brasileiros',
        'transport': 'http',
        'tools': [{'name': t['name'], 'description': t['description'], 'inputSchema': t['inputSchema']}
                  for t in TOOLS],
        'auth': {'type': 'oauth2' if oauth.base() else 'bearer', 'header': 'Authorization'},
    })


#: Versões do protocolo que o servidor fala. O cliente pede uma no `initialize`:
#: se a conhecemos, devolvemos a mesma; senão, a mais nova (é o que a spec manda).
PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')


class _RpcError(Exception):
    """Erro de protocolo JSON-RPC. `http_status` só vale nos endpoints legados."""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


#: Vai no `initialize`: o cliente põe no contexto do modelo antes de qualquer tool.
INSTRUCTIONS = """Voyager: acervo judicial brasileiro — processos, publicações em diários
oficiais, partes/entidades e precatórios.

Três bases, que NÃO são a mesma coisa:
- processos: o que conhecemos em detalhe (partes, valor da causa, classificação de lead);
- acervo: o que os tribunais declararam ao CNJ (sem partes nem valor);
- publicacoes: o texto dos diários (~1,8 bilhão).

Como trabalhar:
- Pergunta macro ou "quanto temos" → visao_geral; o que dá pra perguntar → descrever_base.
- Contagens/somas/séries → agregar. Pra ver os processos por trás de um número → buscar_processos.
- Precatórios por região → mapa_precatorios / panorama_estado. Devedores → ranking_entidades → ficha_entidade.
- Um processo → dossie_jurimetrico (comece por ele) ou contexto_processo.

Honestidade com o dado (regra da casa):
- Se a resposta trouxer `parcial: true`, `outros` ou `avisos`, o número NÃO é o universo — diga isso.
- Campo ausente não é zero nem "não": é desconhecido. Valor da causa falta em muitos processos.
- "Não achei em processos" não é "não existe": confira em acervo antes de afirmar.
"""


def _initialize(params):
    pedida = params.get('protocolVersion')
    return {
        'protocolVersion': pedida if pedida in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
        'capabilities': {
            'tools': {},
            'resources': {},
            'prompts': {},
        },
        'serverInfo': {
            'name': 'voyager',
            'version': '1.1.0',
        },
        'instructions': INSTRUCTIONS,
    }


def _call_tool(params, cliente):
    tool_name = params.get('name', '')
    arguments = params.get('arguments') or {}
    if tool_name not in TOOL_MAP:
        raise _RpcError(-32601, f'Tool não encontrada: {tool_name}')

    schema = next((t for t in TOOLS if t['name'] == tool_name), None)
    if schema:
        required = schema['inputSchema'].get('required', [])
        missing = [r for r in required if r not in arguments]
        if missing:
            raise _RpcError(-32602, f'Argumentos obrigatórios faltando: {missing}')

    try:
        result = TOOL_MAP[tool_name](**arguments)
    except Exception as e:
        logger.error('MCP tool=%s erro: %s', tool_name, e)
        raise _RpcError(-32603, f'Erro interno: {e}', http_status=500) from e
    logger.info('MCP tool=%s %s', tool_name, cliente.rotulo if cliente else '?')
    return {
        'content': [{'type': 'text', 'text': json.dumps(result, default=str, ensure_ascii=False)}],
    }


def _read_resource(uri):
    if uri.startswith('voyager://processo/'):
        dado = delegates.get_processo(uri.split('/')[-1])
    elif uri.startswith('voyager://movimentacao/'):
        try:
            doc_id = int(uri.split('/')[-1])
        except ValueError:
            raise _RpcError(-32602, f'ID de movimentação inválido: {uri}') from None
        dado = delegates.get_documento(doc_id)
    elif uri == 'voyager://fontes':
        dado = delegates.listar_fontes()
    elif uri == 'voyager://base':
        dado = macro.descrever_base()
    elif uri == 'voyager://visao-geral':
        dado = macro.visao_geral()
    elif uri.startswith('voyager://contexto/'):
        dado = delegates.contexto_processo(uri.split('/')[-1])
    else:
        raise _RpcError(-32601, f'Recurso ou método não suportado: {uri}')
    return {'contents': [{'uri': uri, 'mimeType': 'application/json',
                          'text': json.dumps(dado, default=str, ensure_ascii=False)}]}


def _dispatch(method, params, cliente):
    """Resolve um request JSON-RPC → `result`. Erro de protocolo ⇒ `_RpcError`."""
    if method == 'initialize':
        return _initialize(params)
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        return _call_tool(params, cliente)
    if method == 'resources/list':
        return {'resources': [
            {'uri': r['uri'], 'name': name, 'description': r['description'], 'mimeType': 'application/json'}
            for name, r in RESOURCES.items()
        ]}
    if method == 'resources/templates/list':
        return {'resourceTemplates': [
            {'uriTemplate': r['template'], 'name': name, 'description': r['description'],
             'mimeType': 'application/json'}
            for name, r in RESOURCE_TEMPLATES.items()
        ]}
    if method == 'resources/read':
        return _read_resource(params.get('uri', ''))
    if method == 'prompts/list':
        return {'prompts': [
            {'name': nome, 'description': p['description'], 'arguments': p['arguments']}
            for nome, p in PROMPTS.items()
        ]}
    if method == 'prompts/get':
        return _get_prompt(params.get('name', ''), params.get('arguments') or {})
    raise _RpcError(-32601, f'Método não suportado: {method}')


def _parse_body(request):
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        raise _RpcError(-32700, 'Parse error') from None
    if not isinstance(body, dict):
        raise _RpcError(-32600, 'Invalid Request: batch não suportado')
    return body


def _rpc_error(msg_id, err, status):
    return JsonResponse({
        'jsonrpc': '2.0',
        'id': msg_id,
        'error': {'code': err.code, 'message': err.message},
    }, status=status)


def _quer_html(request):
    """Navegador abrindo a URL do conector (e não cliente MCP pedindo stream)."""
    aceita = request.META.get('HTTP_ACCEPT', '')
    return 'text/html' in aceita and 'text/event-stream' not in aceita


#: CSP da landing: nada de fora, script só o arquivo estático (botão Copiar).
CSP_LANDING = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


def landing(request):
    """GET /mcp no navegador: o que é o conector, como conectar e o catálogo.
    Pública — não mostra dado do acervo, só o que o `tools/list` já diz."""
    from django.shortcuts import render

    from . import catalogo
    base = oauth.base() or request.build_absolute_uri('/').rstrip('/')
    resp = render(request, 'mcp_server/landing.html', {
        **catalogo.contexto(),
        'url_conector': f'{base}/mcp',
        'versao_protocolo': PROTOCOL_VERSIONS[0],
    })
    resp['Content-Security-Policy'] = CSP_LANDING
    resp['X-Frame-Options'] = 'DENY'
    resp['Vary'] = 'Accept'
    return resp


@csrf_exempt
def mcp_endpoint(request):
    """/mcp/ — transporte Streamable HTTP da spec MCP: TODO request JSON-RPC
    (initialize, tools/list, tools/call, resources/*, ping) chega aqui por POST.

    Antes este endpoint respondia `initialize` a qualquer método, e o Claude
    Code/Desktop conectava mas nunca via uma tool. Notificação (sem `id`) ⇒ 202.
    Não abrimos stream servidor→cliente: GET ⇒ 405, como a spec permite —
    salvo navegador (Accept text/html), que recebe a landing do conector.
    """
    if request.method == 'GET' and _quer_html(request):
        return landing(request)
    if request.method != 'POST':
        return HttpResponse(status=405, headers={'Allow': 'POST'})
    cliente, err = _auth(request)
    if err:
        return err
    try:
        body = _parse_body(request)
    except _RpcError as e:
        return _rpc_error(None, e, 400)

    if 'id' not in body:
        return HttpResponse(status=202)

    msg_id = body.get('id')
    try:
        result = _dispatch(body.get('method', ''), body.get('params') or {}, cliente)
    except _RpcError as e:
        # Erro JSON-RPC viaja no corpo com 200: o cliente lê `code`/`message`
        # em vez de um "HTTP 400" opaco.
        return _rpc_error(msg_id, e, 200)
    return JsonResponse({'jsonrpc': '2.0', 'id': msg_id, 'result': result})


def _legacy(request):
    """Endpoints antigos (/mcp/messages, /mcp/resources): mesmo dispatch, mas
    erro de protocolo continua saindo com o status HTTP de antes."""
    cliente, err = _auth(request)
    if err:
        return err
    try:
        body = _parse_body(request)
    except _RpcError as e:
        return _rpc_error(None, e, 400)
    msg_id = body.get('id')
    try:
        result = _dispatch(body.get('method', ''), body.get('params') or {}, cliente)
    except _RpcError as e:
        return _rpc_error(msg_id, e, e.http_status)
    return JsonResponse({'jsonrpc': '2.0', 'id': msg_id, 'result': result})


@csrf_exempt
def mcp_messages(request):
    """POST /mcp/messages — JSON-RPC 2.0 (legado; clientes novos usam /mcp/)."""
    return _legacy(request)


@csrf_exempt
def mcp_sse(request):
    """GET /mcp/sse — SSE stream (placeholder pra compat com clientes SSE).
    Implementação mínima: envia keepalive. Clientes devem usar POST /mcp/ pra RPC."""
    response = HttpResponse(content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response.write('event: ready\ndata: {}\n\n')
    return response


#: Recursos concretos (aparecem em resources/list).
RESOURCES = {
    'fontes': {'uri': 'voyager://fontes', 'description': 'Lista de fontes/diários cobertos'},
    'base': {'uri': 'voyager://base',
             'description': 'Índices, campos consultáveis, valores de classificação e ressalvas do dado'},
    'visao_geral': {'uri': 'voyager://visao-geral',
                    'description': 'Panorama do acervo (totais, por tribunal, razão temos ÷ CNJ)'},
}

#: Recursos parametrizados (resources/templates/list).
RESOURCE_TEMPLATES = {
    'processo': {
        'template': 'voyager://processo/{cnj}',
        'description': 'Dados completos de um processo por CNJ',
    },
    'movimentacao': {
        'template': 'voyager://movimentacao/{id}',
        'description': 'Detalhe de uma publicação por ID',
    },
    'contexto_processo': {
        'template': 'voyager://contexto/{cnj}',
        'description': 'Dossiê completo de um processo (partes, valores, movimentações, jurimetria)',
    },
}


#: Prompts prontos (prompts/list): roteiros que encadeiam as tools certas.
PROMPTS = {
    'panorama_do_acervo': {
        'description': 'Panorama do acervo: tamanho, cobertura contra o CNJ, onde estão os precatórios',
        'arguments': [{'name': 'foco', 'description': 'Tribunal ou UF pra aprofundar (opcional)', 'required': False}],
        'texto': (
            'Monte um panorama do acervo do Voyager{foco}. Use visao_geral para os totais e a razão '
            '"temos ÷ declarado ao CNJ" por tribunal; mapa_precatorios para onde há precatórios; e '
            'agregar para qualquer recorte que faltar. Aponte onde a cobertura é baixa. Cite os números '
            'com a fonte (tool) e sinalize tudo que vier parcial.'
        ),
    },
    'responder_pergunta': {
        'description': 'Responder uma pergunta quantitativa sobre a base, com o número e de onde ele veio',
        'arguments': [{'name': 'pergunta', 'description': 'A pergunta', 'required': True}],
        'texto': (
            'Pergunta: {pergunta}\n\nChame descrever_base para ver os campos e as ressalvas, traduza a '
            'pergunta em uma ou mais chamadas de agregar (ou mapa_precatorios / ranking_entidades quando '
            'servirem melhor) e responda com o número, o filtro exato usado e as ressalvas que se aplicam. '
            'Se o dado não permite responder, diga o que falta — não estime.'
        ),
    },
    'analisar_processo': {
        'description': 'Análise de um processo: estágio, partes, valores, chance de virar precatório',
        'arguments': [{'name': 'cnj', 'description': 'Número CNJ', 'required': True}],
        'texto': (
            'Analise o processo {cnj}. Comece por dossie_jurimetrico; complete com contexto_processo e '
            'linha_do_tempo. Se houver ente devedor, avalie com ente_fiscal e capag_rating. Separe o que é '
            'dado do processo do que é estimativa de modelo.'
        ),
    },
    'perfil_devedor': {
        'description': 'Perfil de um devedor/entidade: volume, onde litiga, precatórios, saúde fiscal',
        'arguments': [{'name': 'nome', 'description': 'Nome, sigla ou CNPJ da entidade', 'required': True}],
        'texto': (
            'Monte o perfil de "{nome}". Ache a entidade com ranking_entidades(q=...), abra a '
            'ficha_entidade com o id encontrado e, se for ente público, traga ente_fiscal e capag_rating. '
            'Se houver mais de uma entidade candidata, liste e diga qual usou.'
        ),
    },
}


def _get_prompt(nome, argumentos):
    prompt = PROMPTS.get(nome)
    if not prompt:
        raise _RpcError(-32602, f'Prompt não encontrado: {nome}')
    faltando = [a['name'] for a in prompt['arguments'] if a['required'] and not argumentos.get(a['name'])]
    if faltando:
        raise _RpcError(-32602, f'Argumentos obrigatórios faltando: {faltando}')
    valores = {a['name']: argumentos.get(a['name']) or '' for a in prompt['arguments']}
    if 'foco' in valores:
        valores['foco'] = f', com foco em {valores["foco"]}' if valores['foco'] else ''
    return {
        'description': prompt['description'],
        'messages': [{'role': 'user', 'content': {'type': 'text', 'text': prompt['texto'].format(**valores)}}],
    }


@csrf_exempt
def mcp_resources(request):
    """POST /mcp/resources — resources/list e resources/read (legado)."""
    return _legacy(request)


urlpatterns = [
    path('.well-known/mcp.json', mcp_descriptor, name='mcp-descriptor'),
    path('', mcp_endpoint, name='mcp-endpoint'),
    path('messages', mcp_messages, name='mcp-messages'),
    path('sse', mcp_sse, name='mcp-sse'),
    path('resources', mcp_resources, name='mcp-resources'),
]
