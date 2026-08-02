"""Auth e rate limit do MCP server."""
import logging
import time

from django.conf import settings
from django.core.cache import cache

from tribunals.models import ApiClient

logger = logging.getLogger('voyager.mcp.auth')


def validate_mcp_token(token):
    """Valida o mcp_token e retorna o ApiClient, ou None."""
    if not token:
        return None
    try:
        return ApiClient.objects.get(mcp_token=token, ativo=True)
    except (ApiClient.DoesNotExist, ValueError):
        return None


def check_rate_limit(cliente):
    """Token-bucket Redis. Retorna True se dentro do limite.

    Fail-open: se Redis está cheio (noeviction OOM) ou indisponível,
    permite a request em vez de 500 — rate limit é best-effort.
    """
    key = f'mcp:rate:{cliente.pk}'
    rpm = settings.MCP_RATE_LIMIT_RPM
    if rpm <= 0:
        return True
    try:
        count = cache.get(key, 0)
        if count >= rpm:
            return False
        cache.set(key, count + 1, timeout=60)
        return True
    except Exception as e:
        logger.warning('Rate limit Redis falhou (fail-open): %s', e)
        return True