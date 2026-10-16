"""Credencial anônima da consulta pública do TJRS: chaves do bundle + pool.

Duas coisas que o recon de 15/10/2026 errou e a revisão do PR #27 achou:

1. **As constantes da `obfuscation()` mudam com o deploy da SPA.** Em 16/10/2026
   ~00:00 UTC o `main.*.js` trocou `+BigInt(92120)` por `+BigInt(92125)` e toda
   credencial calculada com a constante antiga passou a levar 401 — o que foi
   lido, na hora, como "a API caiu" (até a SPA num Chromium levava 401, porque o
   Chromium ainda tinha o bundle velho em cache). Então as chaves vêm do bundle
   VIVO: `index.html` → `main.<hash>.js` → `%BigInt(m)+BigInt(s)`. Cache de 1 h
   no Redis; numa série de 401 o enricher relê o bundle UMA vez antes de
   concluir que a fonte está fora.

2. **Cada credencial vale 7 consultas e custa token + submit + prova de
   trabalho.** Gastar uma por processo era jogar 6 fora e triplicar as
   requisições ao TJRS. O pool no Redis divide a credencial entre réplicas:
   a credencial atual + um contador de consultas restantes, decrementado
   atomicamente (`DECR`). Quem tira um valor ≥ 0 usa; quem tira negativo gera
   outra e publica. Corrida entre réplicas no máximo gera uma credencial a
   mais — nunca usa a mesma mais de 7 vezes.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging

from .fingerprints import (
    CHAVES_TJRS_CONHECIDAS,
    BundleTjrsMudouError,
    bundle_principal_tjrs,
    chaves_do_bundle_tjrs,
)

logger = logging.getLogger('voyager.enrichers.tjrs')

SPA_URL = 'https://consulta.tjrs.jus.br/consulta-processual/'
_CHAVES_KEY = 'tjrs:spa:chaves'
_CHAVES_ULTIMO_KEY = 'tjrs:spa:chaves:ultimo'
CHAVES_TTL_S = 3600

#: Vida da credencial no pool. A API não declara validade; 15 min é folga para
#: as 7 consultas e curto o bastante para não carregar credencial morta. Se ela
#: morrer antes, o 401 a descarta.
CREDENCIAL_TTL_S = 900


def _cache():
    from django.core.cache import cache
    return cache


def chaves_tjrs(sessao, forcar: bool = False, timeout=(10, 30)) -> tuple[int, int]:
    """`(MODULO, SOMA)` da `obfuscation()` do bundle vivo da SPA.

    Ordem: cache de 1 h → bundle → último par lido com sucesso → par embutido.
    Um bundle que não sabemos ler (`BundleTjrsMudouError`) NÃO derruba o enricher:
    sai em ERROR e usa o último par — se ele ainda vale, a coleta segue; se
    não, a série de 401 arma o respiro e o alerta.
    """
    cache = _cache()
    if not forcar:
        try:
            atual = cache.get(_CHAVES_KEY)
        except Exception:
            atual = None
        if atual:
            return tuple(atual)
    try:
        r = sessao.get(SPA_URL, timeout=timeout)
        r.raise_for_status()
        bundle = bundle_principal_tjrs(r.text)
        r = sessao.get(SPA_URL + bundle, timeout=timeout)
        r.raise_for_status()
        chaves = chaves_do_bundle_tjrs(r.text)
    except BundleTjrsMudouError as exc:
        logger.error('TJRS: bundle da SPA mudou de formato — %s', exc)
        chaves = None
    except Exception as exc:
        logger.warning('TJRS: não li o bundle da SPA (%s)', str(exc)[:200])
        chaves = None
    if chaves is None:
        try:
            ultimo = cache.get(_CHAVES_ULTIMO_KEY)
        except Exception:
            ultimo = None
        return tuple(ultimo) if ultimo else CHAVES_TJRS_CONHECIDAS
    try:
        anterior = cache.get(_CHAVES_ULTIMO_KEY)
        cache.set(_CHAVES_KEY, list(chaves), timeout=CHAVES_TTL_S)
        cache.set(_CHAVES_ULTIMO_KEY, list(chaves), timeout=None)
        if anterior and tuple(anterior) != chaves:
            logger.error('TJRS: constantes da credencial mudaram no bundle: %s → %s',
                         tuple(anterior), chaves)
    except Exception:
        pass
    return chaves


class PoolCredenciais:
    """Credencial anônima compartilhada entre réplicas (Redis via cache Django)."""

    def __init__(self, chaves: tuple[int, int]):
        self.prefixo = f'tjrs:cred:{chaves[0]}:{chaves[1]}'

    @staticmethod
    def _contador(cred: str) -> str:
        return 'tjrs:cred:n:' + hashlib.sha1(cred.encode()).hexdigest()[:16]

    def pegar(self) -> str | None:
        """Uma consulta da credencial atual, ou None se não há/esgotou."""
        cache = _cache()
        try:
            cred = cache.get(self.prefixo + ':atual')
            if not cred:
                return None
            restante = cache.decr(self._contador(cred))
        except ValueError:          # contador expirou/sumiu
            return None
        except Exception:
            return None
        return cred if restante >= 0 else None

    def publicar(self, cred: str, consultas: int) -> None:
        """Nova credencial para os outros — já descontada a consulta de quem gerou."""
        cache = _cache()
        try:
            cache.set(self._contador(cred), max(consultas - 1, 0), timeout=CREDENCIAL_TTL_S)
            cache.set(self.prefixo + ':atual', cred, timeout=CREDENCIAL_TTL_S)
        except Exception:
            pass

    def descartar(self, cred: str) -> None:
        """401/429: ninguém mais usa esta."""
        with contextlib.suppress(Exception):
            _cache().set(self._contador(cred), -10_000, timeout=CREDENCIAL_TTL_S)
