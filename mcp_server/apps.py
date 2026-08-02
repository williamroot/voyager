import logging

from django.apps import AppConfig

logger = logging.getLogger('voyager.mcp')


class McpServerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mcp_server'

    def ready(self):
        # O SDK `mcp` pode não estar instalado em todos os ambientes.
        # O app não declara models próprios nem depende do import pra funcionar
        # (as views em server.py são HTTP/JSON-RPC puras, sem usar o SDK).
        # Tentamos importar só pra validar; se falhar, logamos e seguimos.
        try:
            import mcp  # noqa: F401
        except ImportError:
            logger.info('SDK mcp não instalado — MCP server opera em modo HTTP/JSON-RPC nativo')