"""MCP Server — expõe tools e resources do Voyager pra LLMs/agentes."""
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import delegates
from .auth import check_rate_limit, validate_mcp_token

logger = logging.getLogger('voyager.mcp.server')

TOOLS = [
    {
        'name': 'buscar_diarios',
        'description': 'Busca textual em diários oficiais (compatível Jusbrasil/Digesto). '
                       'Retorna publicações que contêm o termo pesquisado.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Termo ou frase de busca (sintaxe Elasticsearch query_string)'},
                'tribunal': {'type': 'string', 'description': 'Sigla do tribunal (ex: TRF1, TJSP) — opcional'},
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
}


def _extract_token(request):
    """Extrai MCP token do header Authorization ou query param."""
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    return request.GET.get('token', '')


def _auth_or_403(request):
    """Valida auth. Retorna (cliente, None) ou (None, JsonResponse 403)."""
    token = _extract_token(request)
    if not token:
        return None, JsonResponse({'error': 'token_ausente'}, status=403)
    cliente = validate_mcp_token(token)
    if cliente is None:
        return None, JsonResponse({'error': 'token_invalido'}, status=403)
    if not check_rate_limit(cliente):
        return None, JsonResponse({'error': 'rate_limit_excedido'}, status=429)
    return cliente, None


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
        'auth': {'type': 'bearer', 'header': 'Authorization'},
    })


@csrf_exempt
def mcp_initialize(request):
    """POST /mcp/ — handshake MCP initialize."""
    cliente, err = _auth_or_403(request)
    if err:
        return err
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}
    return JsonResponse({
        'jsonrpc': '2.0',
        'id': body.get('id', 0),
        'result': {
            'protocolVersion': '2024-11-05',
            'capabilities': {
                'tools': {},
                'resources': {},
            },
            'serverInfo': {
                'name': 'voyager',
                'version': '1.0.0',
            },
        },
    })


@csrf_exempt
def mcp_messages(request):
    """POST /mcp/messages — JSON-RPC 2.0 endpoint pra tools/list e tools/call."""
    cliente, err = _auth_or_403(request)
    if err:
        return err

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({'jsonrpc': '2.0', 'error': {'code': -32700, 'message': 'Parse error'}}, status=400)

    method = body.get('method', '')
    msg_id = body.get('id')
    params = body.get('params', {})

    if method == 'tools/list':
        return JsonResponse({
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': {'tools': TOOLS},
        })

    if method == 'tools/call':
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})
        if tool_name not in TOOL_MAP:
            return JsonResponse({
                'jsonrpc': '2.0',
                'id': msg_id,
                'error': {'code': -32601, 'message': f'Tool não encontrada: {tool_name}'},
            }, status=400)

        schema = next((t for t in TOOLS if t['name'] == tool_name), None)
        if schema:
            required = schema['inputSchema'].get('required', [])
            missing = [r for r in required if r not in arguments]
            if missing:
                return JsonResponse({
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'error': {'code': -32602, 'message': f'Argumentos obrigatórios faltando: {missing}'},
                }, status=400)

        try:
            result = TOOL_MAP[tool_name](**arguments)
            logger.info('MCP tool=%s cliente=%s', tool_name, cliente.nome if cliente else '?')
            return JsonResponse({
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'content': [{'type': 'text', 'text': json.dumps(result, default=str, ensure_ascii=False)}],
                },
            })
        except Exception as e:
            logger.error('MCP tool=%s erro: %s', tool_name, e)
            return JsonResponse({
                'jsonrpc': '2.0',
                'id': msg_id,
                'error': {'code': -32603, 'message': f'Erro interno: {e}'},
            }, status=500)

    return JsonResponse({
        'jsonrpc': '2.0',
        'id': msg_id,
        'error': {'code': -32601, 'message': f'Método não suportado: {method}'},
    }, status=400)


@csrf_exempt
def mcp_sse(request):
    """GET /mcp/sse — SSE stream (placeholder pra compat com clientes SSE).
    Implementação mínima: envia keepalive. Clientes devem usar /mcp/messages pra RPC."""
    response = HttpResponse(content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response.write('event: ready\ndata: {}\n\n')
    return response


RESOURCES = {
    'processo': {
        'template': 'voyager://processo/{cnj}',
        'description': 'Dados completos de um processo por CNJ',
    },
    'movimentacao': {
        'template': 'voyager://movimentacao/{id}',
        'description': 'Detalhe de uma publicação por ID',
    },
    'parte': {
        'template': 'voyager://parte/{id}',
        'description': 'Detalhe de uma parte por ID',
    },
    'fontes': {
        'template': 'voyager://fontes',
        'description': 'Lista de fontes/diários cobertos',
    },
}


@csrf_exempt
def mcp_resources(request):
    """POST /mcp/resources — lista e lê resources."""
    cliente, err = _auth_or_403(request)
    if err:
        return err

    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    method = body.get('method', '')
    msg_id = body.get('id')
    params = body.get('params', {})

    if method == 'resources/list':
        return JsonResponse({
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': {'resources': [
                {'uri': r['template'], 'description': r['description'], 'name': name}
                for name, r in RESOURCES.items()
            ]},
        })

    if method == 'resources/read':
        uri = params.get('uri', '')
        if uri.startswith('voyager://processo/'):
            cnj = uri.split('/')[-1]
            return JsonResponse({
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {'contents': [{'uri': uri, 'text': json.dumps(delegates.get_processo(cnj), default=str)}]},
            })
        if uri.startswith('voyager://movimentacao/'):
            doc_id = int(uri.split('/')[-1])
            return JsonResponse({
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {'contents': [{'uri': uri, 'text': json.dumps(delegates.get_documento(doc_id), default=str)}]},
            })
        if uri == 'voyager://fontes':
            return JsonResponse({
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {'contents': [{'uri': uri, 'text': json.dumps(delegates.listar_fontes(), default=str)}]},
            })

    return JsonResponse({
        'jsonrpc': '2.0',
        'id': msg_id,
        'error': {'code': -32601, 'message': f'Recurso ou método não suportado: {method}'},
    }, status=400)


urlpatterns = [
    path('.well-known/mcp.json', mcp_descriptor, name='mcp-descriptor'),
    path('', mcp_initialize, name='mcp-initialize'),
    path('messages', mcp_messages, name='mcp-messages'),
    path('sse', mcp_sse, name='mcp-sse'),
    path('resources', mcp_resources, name='mcp-resources'),
]