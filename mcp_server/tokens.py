"""Estado do OAuth do MCP no Redis: tokens, codes, registros DCR e pedidos.

Tokens são opacos (`secrets.token_urlsafe`) e só o **sha256** deles vai para o
Redis — um dump do Redis não dá acesso a nada. Revogar usa o mesmo hash
(`_chave_access`/`_chave_refresh`): um único jeito de chegar à chave.

Índice por usuário (`u:{id}`): as chaves vivas dele, para derrubar TODAS as
sessões de uma vez quando a allowlist nega.

Família (`familia:{id}`): todos os tokens que descendem do mesmo code (o par
inicial e cada rotação). Refresh ou code REAPRESENTADO depois de usado é sinal
de roubo, e a resposta é derrubar a família inteira (RFC 9700 §4.14.2).

Cliente Redis próprio, e não o `cache` do Django: precisamos de GETDEL
(uso único atômico), conjuntos e pipeline. Com teto de espera, porque está no
caminho de toda request do /mcp.
"""
import functools
import hashlib
import json
import secrets
from dataclasses import dataclass

import redis
from django.conf import settings

ESCOPO = 'voyager.leitura'
_PREFIXO = 'mcp:oauth:'

@functools.cache
def r():
    return redis.Redis.from_url(
        settings.REDIS_URL, socket_timeout=5, socket_connect_timeout=3, decode_responses=True,
    )


def chave(*partes):
    return _PREFIXO + ':'.join(str(p) for p in partes)


def sha(valor):
    return hashlib.sha256(valor.encode()).hexdigest()


def _chave_access(token):
    return chave('access', sha(token))


def _chave_refresh(token):
    return chave('refresh', sha(token))


def _chave_indice(usuario_id):
    return chave('u', usuario_id)


def _chave_familia(familia):
    return chave('familia', familia)


def _chave_refresh_usado(token):
    return chave('refresh_usado', sha(token))


def _chave_code_usado(code):
    return chave('code_usado', sha(code))


@dataclass(frozen=True)
class Sessao:
    """O que um access token vale: quem é e por qual app se conectou."""

    usuario_id: int
    client_id: str
    resource: str
    familia: str = ''


@dataclass(frozen=True)
class ParDeTokens:
    access: str
    refresh: str
    expira_em_s: int
    familia: str = ''


def emitir(usuario_id, client_id, resource, familia=None):
    """Emite um par; sem `familia`, abre uma nova (troca de code)."""
    familia = familia or secrets.token_urlsafe(12)
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    dados = json.dumps({
        'usuario_id': int(usuario_id), 'client_id': client_id,
        'resource': resource, 'familia': familia,
    })
    ttl_access = settings.MCP_ACCESS_TOKEN_TTL_S
    ttl_refresh = settings.MCP_REFRESH_TOKEN_TTL_S
    k_access, k_refresh = _chave_access(access), _chave_refresh(refresh)
    indice, k_familia = _chave_indice(usuario_id), _chave_familia(familia)
    pipe = r().pipeline(transaction=True)
    pipe.set(k_access, dados, ex=ttl_access)
    pipe.set(k_refresh, dados, ex=ttl_refresh)
    pipe.sadd(indice, k_access, k_refresh)
    pipe.expire(indice, ttl_refresh)
    pipe.sadd(k_familia, k_access, k_refresh)
    pipe.expire(k_familia, ttl_refresh)
    pipe.execute()
    return ParDeTokens(access=access, refresh=refresh, expira_em_s=ttl_access, familia=familia)


def _sessao(cru):
    if not cru:
        return None
    dados = json.loads(cru)
    return Sessao(
        usuario_id=int(dados['usuario_id']),
        client_id=str(dados.get('client_id', '')),
        resource=str(dados.get('resource', '')),
        familia=str(dados.get('familia', '')),
    )


def resolver_access(token):
    """Access token -> sessão (None se não for token nosso ou se expirou)."""
    if not token:
        return None
    return _sessao(r().get(_chave_access(token)))


def ler_refresh(token):
    """Lê sem consumir (para checar a allowlist antes de gastar o refresh)."""
    if not token:
        return None
    return _sessao(r().get(_chave_refresh(token)))


def consumir_refresh(token):
    """GETDEL: refresh é de uso único (rotação) — reapresentar o mesmo falha."""
    if not token:
        return None
    k = _chave_refresh(token)
    sessao = _sessao(r().getdel(k))
    if sessao is not None:
        r().srem(_chave_indice(sessao.usuario_id), k)
        if sessao.familia:
            r().srem(_chave_familia(sessao.familia), k)
            r().set(_chave_refresh_usado(token), sessao.familia, ex=settings.MCP_REFRESH_TOKEN_TTL_S)
    return sessao


def familia_de_refresh_usado(token):
    return r().get(_chave_refresh_usado(token)) if token else None


def marcar_code_usado(code, familia):
    r().set(_chave_code_usado(code), familia, ex=settings.MCP_REFRESH_TOKEN_TTL_S)


def familia_de_code_usado(code):
    return r().get(_chave_code_usado(code)) if code else None


def revogar_familia(familia):
    """Derruba todos os tokens da família (reuso detectado). Devolve quantos."""
    if not familia:
        return 0
    k = _chave_familia(familia)
    membros = r().smembers(k)
    apagados = int(r().delete(*membros)) if membros else 0
    r().delete(k)
    return apagados


def revogar_usuario(usuario_id):
    """Derruba todas as sessões vivas do usuário (access e refresh)."""
    indice = _chave_indice(usuario_id)
    chaves = r().smembers(indice)
    apagados = int(r().delete(*chaves)) if chaves else 0
    r().delete(indice, chave('veredito', usuario_id))
    return apagados
