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


_LUA_PEGAR = """
local cred = redis.call('HGET', KEYS[1], 'cred')
if not cred then return false end
local n = redis.call('HINCRBY', KEYS[1], 'n', -1)
if n >= 0 then return cred end
return false
"""
_LUA_PUBLICAR = """
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'cred', ARGV[1], 'n', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('DEL', KEYS[2])
return 1
"""
_LUA_DESCARTAR = """
if redis.call('HGET', KEYS[1], 'cred') == ARGV[1] then
  redis.call('HSET', KEYS[1], 'n', -10000)
end
return 1
"""


class PoolCredenciais:
    """Credencial anônima compartilhada entre réplicas, no Redis da fila.

    Um HASH `{cred, n}` por par de chaves do bundle. Tudo atômico em Lua:

    * `pegar`: lê a credencial e decrementa `n` na MESMA operação — sem a
      corrida "existe? → incrementa" que podia recriar o contador sem TTL;
    * `publicar`: troca credencial e contador juntos e põe o TTL junto;
    * gerar credencial nova é de UMA réplica por vez (`SET NX` com EX): as
      outras releem o pool por alguns segundos em vez de sobrescrever a
      credencial atual — antes, sob contenção, cada réplica gerava a sua e
      jogava fora as consultas da anterior.
    """

    #: Quanto uma réplica espera a outra gerar antes de gerar a sua (sem publicar).
    ESPERA_GERACAO_S = 8.0
    PASSO_ESPERA_S = 0.1
    TRAVA_GERACAO_S = 30

    def __init__(self, chaves: tuple[int, int], conexao=None):
        self.chave = f'tjrs:cred:{chaves[0]}:{chaves[1]}'
        self.trava = self.chave + ':gerando'
        self._conexao = conexao

    @property
    def redis(self):
        if self._conexao is None:
            import django_rq
            self._conexao = django_rq.get_connection('default')
        return self._conexao

    def pegar(self) -> str | None:
        """Uma consulta da credencial atual, ou None se não há/esgotou."""
        try:
            cred = self.redis.eval(_LUA_PEGAR, 1, self.chave)
        except Exception:
            return None
        if not cred:
            return None
        return cred.decode() if isinstance(cred, bytes) else cred

    def publicar(self, cred: str, consultas: int) -> None:
        """Nova credencial para os outros — já descontada a consulta de quem gerou."""
        with contextlib.suppress(Exception):
            self.redis.eval(_LUA_PUBLICAR, 2, self.chave, self.trava,
                            cred, max(consultas - 1, 0), CREDENCIAL_TTL_S)

    def descartar(self, cred: str) -> None:
        """401/429: ninguém mais usa esta (se ainda for a atual)."""
        with contextlib.suppress(Exception):
            self.redis.eval(_LUA_DESCARTAR, 1, self.chave, cred)

    def pegar_ou_gerar(self, gerar) -> str:
        """Do pool; sem consulta lá, UMA réplica gera (`gerar() -> (cred, n)`) e
        publica, e as outras releem — e, se a nova também esgotar enquanto
        esperam, uma delas pega a trava e gera a próxima. Se o Redis falhar ou
        a espera estourar, gera para si sem publicar: a coleta não para por
        causa do pool."""
        import time as _time
        prazo = _time.monotonic() + self.ESPERA_GERACAO_S
        while True:
            cred = self.pegar()
            if cred:
                return cred
            try:
                dono = bool(self.redis.set(self.trava, '1', nx=True, ex=self.TRAVA_GERACAO_S))
            except Exception:
                cred, _n = gerar()
                return cred
            if dono:
                try:
                    cred, consultas = gerar()
                except Exception:
                    with contextlib.suppress(Exception):
                        self.redis.delete(self.trava)
                    raise
                self.publicar(cred, consultas)
                return cred
            if _time.monotonic() >= prazo:
                cred, _n = gerar()
                return cred
            _time.sleep(self.PASSO_ESPERA_S)
