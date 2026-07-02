"""Rate-limiter global (token-bucket em Redis) para o Datajud.

A API pública do CNJ tem rate limit **global por chave** (a APIKey é compartilhada).
Sem pacing, os workers estouram a quota e o CNJ passa a **pendurar** as queries
(incidente 2026-07-02: 640 workers → chave throttled, _search em timeout). Este
bucket coordena TODOS os workers via Redis pra ficar sob `DATAJUD_RATE_LIMIT_RPM`
requisições/minuto no total.

`acquire()` bloqueia (espera) até um token liberar, com teto `max_wait`.
"""
import time

import django_rq
from django.conf import settings

# Token bucket atômico em Lua. KEYS[1]=bucket. ARGV: rate(tokens/s), cap, now(ms).
# Retorna {allowed(0/1), tokens_restantes}.
_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local cap  = tonumber(ARGV[2])
local now  = tonumber(ARGV[3])
local d = redis.call('hmget', key, 'tokens', 'ts')
local tokens = tonumber(d[1])
local ts = tonumber(d[2])
if tokens == nil then tokens = cap; ts = now end
local delta = math.max(0, now - ts) / 1000.0
tokens = math.min(cap, tokens + delta * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('hset', key, 'tokens', tokens, 'ts', now)
redis.call('pexpire', key, 120000)
return {allowed, tostring(tokens)}
"""

_BUCKET_KEY = 'datajud:ratebucket'


def _rpm() -> int:
    return int(getattr(settings, 'DATAJUD_RATE_LIMIT_RPM', 100))


def acquire(max_wait: float = 30.0) -> bool:
    """Pega 1 token; espera até `max_wait`s. True se conseguiu, False se estourou.

    `DATAJUD_RATE_LIMIT_RPM <= 0` desliga o limite (retorna True direto).
    """
    rpm = _rpm()
    if rpm <= 0:
        return True
    rate = rpm / 60.0          # tokens por segundo
    cap = max(1, rpm)          # burst de até 1 min
    conn = django_rq.get_connection('datajud')
    script = conn.register_script(_LUA)
    deadline = time.monotonic() + max_wait
    while True:
        now_ms = int(time.time() * 1000)
        allowed, _tokens = script(keys=[_BUCKET_KEY], args=[rate, cap, now_ms])
        if int(allowed) == 1:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(1.0 / rate, 1.0))
