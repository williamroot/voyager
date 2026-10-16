"""Allowlist: quem pode usar o MCP pelo OAuth (`MCPAcesso` no Django admin).

Conferida na tela de autorização, de novo no consentimento, em cada refresh e,
com cache curto, em cada request. Quando a negação chega, todas as sessões do
usuário são apagadas na hora, para o Claude reabrir o login em vez de insistir.

Fail-closed: banco fora do ar NÃO libera ninguém. A diferença é o que o cliente
vê — "indisponível" (503) não derruba sessão; "negado" derruba.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from mcp_server import tokens
from mcp_server.models import MCPAcesso

logger = logging.getLogger('voyager.mcp.acesso')

PERMITIDO, NEGADO, INDISPONIVEL = 'permitido', 'negado', 'indisponivel'

MENSAGEM_SEM_ACESSO = (
    'A sua conta do Voyager não está liberada para o conector do Claude (ou foi '
    'desativada). Peça a um superusuário para incluir você em '
    '“Acessos ao MCP” no Django admin.'
)

# `ultimo_uso` é para o admin saber quem usa, não auditoria: gravar no máximo
# uma vez a cada tanto por pessoa.
_INTERVALO_ULTIMO_USO = timedelta(minutes=5)


def acesso_liberado(usuario_id):
    """`MCPAcesso` ativo de um usuário ativo, ou None. Única regra de "quem entra"."""
    if usuario_id is None:
        return None
    return (
        MCPAcesso.objects.select_related('usuario')
        .filter(ativo=True, usuario_id=usuario_id, usuario__is_active=True)
        .first()
    )


def _marcar_uso(acesso):
    agora = timezone.now()
    if acesso.ultimo_uso is None or agora - acesso.ultimo_uso > _INTERVALO_ULTIMO_USO:
        MCPAcesso.objects.filter(pk=acesso.pk).update(ultimo_uso=agora)


def verificar(usuario_id, *, usar_cache=True):
    """PERMITIDO | NEGADO | INDISPONIVEL. Só o permitido é cacheado; negado
    derruba todas as sessões do usuário."""
    k = tokens.chave('veredito', usuario_id)
    ttl = settings.MCP_ALLOWLIST_CACHE_S
    if usar_cache and ttl > 0:
        try:
            if tokens.r().get(k):
                return PERMITIDO
        except Exception as e:  # Redis fora: cai pro banco, não nega
            logger.warning('cache da allowlist indisponível: %s', e)
    try:
        acesso = acesso_liberado(usuario_id)
        if acesso is not None:
            _marcar_uso(acesso)
    except DatabaseError as e:
        logger.warning('allowlist do MCP indisponível: %s', e)
        return INDISPONIVEL
    if acesso is None:
        derrubadas = tokens.revogar_usuario(usuario_id)
        logger.info('MCP acesso negado usuario=%s sessoes_revogadas=%s', usuario_id, derrubadas)
        return NEGADO
    if ttl > 0:
        tokens.r().set(k, '1', ex=ttl)
    return PERMITIDO
