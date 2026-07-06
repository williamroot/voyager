"""Servidor MCP (Model Context Protocol) de jurimetria — transporte stdio, JSON-RPC 2.0.

Expõe as MESMAS tools do agente in-process (dashboard/jurimetria_tools.py) via MCP, pra
qualquer cliente (Claude Desktop, Horizon, etc.) usar a jurimetria do Voyager. Sem SDK:
implementa o subconjunto do protocolo (initialize / tools/list / tools/call) em JSON-RPC
newline-delimited. Rode: `python manage.py mcp_jurimetria` (fala por stdin/stdout).

Config no cliente MCP (ex.: claude_desktop_config.json):
  "voyager-jurimetria": {"command": "python", "args": ["manage.py", "mcp_jurimetria"],
                          "cwd": "/opt/voyager"}
"""
import json
import sys

from django.core.management.base import BaseCommand

PROTOCOL_VERSION = '2024-11-05'


class Command(BaseCommand):
    help = 'Servidor MCP de jurimetria (stdio JSON-RPC).'

    def handle(self, *args, **options):
        from dashboard import jurimetria_tools
        tools = jurimetria_tools.TOOLS

        def _send(obj):
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
            sys.stdout.flush()

        def _result(rid, result):
            _send({'jsonrpc': '2.0', 'id': rid, 'result': result})

        def _error(rid, code, message):
            _send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            method = req.get('method')
            rid = req.get('id')
            # notificações (sem id) não respondem
            if method == 'initialize':
                _result(rid, {
                    'protocolVersion': PROTOCOL_VERSION,
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'voyager-jurimetria', 'version': '1.0.0'},
                })
            elif method in ('notifications/initialized', 'initialized'):
                continue
            elif method == 'ping':
                _result(rid, {})
            elif method == 'tools/list':
                _result(rid, {'tools': [
                    {'name': t['name'], 'description': t['description'],
                     'inputSchema': t['parameters']} for t in tools]})
            elif method == 'tools/call':
                params = req.get('params') or {}
                name = params.get('name')
                args = params.get('arguments') or {}
                out = jurimetria_tools.dispatch(name, args)
                is_error = isinstance(out, dict) and 'erro' in out and len(out) == 1
                _result(rid, {
                    'content': [{'type': 'text',
                                 'text': json.dumps(out, ensure_ascii=False, default=str)}],
                    'isError': bool(is_error),
                })
            elif rid is not None:
                _error(rid, -32601, f'method não suportado: {method}')
