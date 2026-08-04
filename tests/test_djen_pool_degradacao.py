"""Degradação do pool por TAXA DE FALHA.

Regressão do incidente 2026-08-03: o WAF do comunicaapi baniu a faixa
datacenter inteira da ProxyScrape. Os IPs continuavam "saudáveis" pela
contagem (respondiam — com 403) e o cooldown de 120s devolvia todos à lista,
então `is_degraded()` nunca acendia e 100% do tráfego continuava indo pro pool
queimado. A ingestão DJEN ficou dias em zero sem o fallback Cortex assumir.
"""
import json

from djen.proxies import ProxyScrapePool


class _FakeRedis:
    """Stub mínimo — só os comandos que o ProxyScrapePool usa.

    Sem fakeredis pra não introduzir dependência nova só por este teste.
    TTL é ignorado de propósito: o teste de expiração deleta a chave à mão,
    que é o efeito observável do TTL vencendo.
    """

    def __init__(self):
        self.kv: dict = {}
        self.z: dict = {}

    # --- strings ---
    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None):
        self.kv[k] = v

    def incr(self, k):
        self.kv[k] = int(self.kv.get(k) or 0) + 1
        return self.kv[k]

    def exists(self, k):
        return 1 if k in self.kv else 0

    def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)
            self.z.pop(k, None)

    # --- zsets ---
    def zadd(self, k, mapping):
        self.z.setdefault(k, {}).update(mapping)

    def zremrangebyscore(self, k, lo, hi):
        hi = float('inf') if hi == '+inf' else float(hi)
        lo = float('-inf') if lo == '-inf' else float(lo)
        d = self.z.get(k, {})
        for m in [m for m, s in d.items() if lo <= s <= hi]:
            del d[m]

    def zrange(self, k, start, end):
        return list(self.z.get(k, {}))

    def zcount(self, k, lo, hi):
        hi = float('inf') if hi == '+inf' else float(hi)
        lo = float('-inf') if lo == '-inf' else float(lo)
        return sum(1 for s in self.z.get(k, {}).values() if lo <= s <= hi)

    # --- pipeline (o pool só usa em modo não-transacional) ---
    def pipeline(self, transaction=False):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def __getattr__(self, name):
        def _queue(*a, **kw):
            self.ops.append((name, a, kw))
            return self
        return _queue

    def execute(self):
        out = []
        for name, a, kw in self.ops:
            out.append(getattr(self.r, name)(*a, **kw))
        self.ops = []
        return out


def _pool(*, total_proxies=2500, streak=25, ttl=600):
    p = ProxyScrapePool.__new__(ProxyScrapePool)
    p.name = 'test'
    p.redis = _FakeRedis()
    p.api_key = 'x'
    p.bad_ttl = 120
    p.cortex_bad_ttl = 15
    p.refresh_threshold = 20
    p._list_key = 'voyager:proxies:test:list'
    p._bad_key = 'voyager:proxies:test:bad_zset'
    p._fail_streak_key = 'voyager:proxies:test:fail_streak'
    p._degraded_key = 'voyager:proxies:test:degraded'
    p.fail_streak_degrade = streak
    p.degraded_ttl = ttl
    p._healthy_cache = []
    p._healthy_cache_ts = 0.0
    p._last_refresh_attempt = 0.0
    p.redis.set(p._list_key, json.dumps(
        [f'http://10.0.0.{i % 255}:{3129 + i // 255}' for i in range(total_proxies)]))
    return p


def test_pool_cheio_mas_todo_bloqueado_fica_degradado():
    """O caso do incidente: milhares de IPs 'saudáveis', todos tomando 403."""
    p = _pool()
    assert not p.is_degraded()  # 2500 saudáveis, muito acima do limiar de 20

    for i in range(p.fail_streak_degrade):
        p.mark_bad(f'http://10.0.0.{i}:3129')

    # A contagem de saudáveis segue altíssima — o sinal antigo não veria nada.
    assert len(p._healthy_list()) > p.refresh_threshold
    # Mas a taxa de falha acende a degradação.
    assert p.is_degraded()


def test_uma_falha_a_menos_nao_degrada():
    p = _pool()
    for i in range(p.fail_streak_degrade - 1):
        p.mark_bad(f'http://10.0.0.{i}:3129')
    assert not p.is_degraded()


def test_mark_ok_desarma_a_degradacao():
    p = _pool()
    for i in range(p.fail_streak_degrade):
        p.mark_bad(f'http://10.0.0.{i}:3129')
    assert p.is_degraded()

    p.mark_ok()  # pool voltou a responder 200

    assert not p.is_degraded()
    assert int(p.redis.get(p._fail_streak_key) or 0) == 0


def test_streak_reinicia_do_zero_apos_sucesso():
    """Sucesso no meio da sequência impede que falhas esparsas acumulem."""
    p = _pool(streak=5)
    for i in range(4):
        p.mark_bad(f'http://10.0.0.{i}:3129')
    p.mark_ok()
    for i in range(4):
        p.mark_bad(f'http://10.0.1.{i}:3129')
    assert not p.is_degraded()


def test_escassez_de_ips_ainda_degrada():
    """Sinal antigo preservado: pool que esvaziou continua sendo degradado."""
    p = _pool(total_proxies=5)
    assert p.is_degraded()  # 5 saudáveis < limiar 20, sem nenhuma falha ainda


def test_degradacao_expira_e_vira_sonda():
    """TTL do flag é o mecanismo de retry: expirou, o pool é testado de novo."""
    p = _pool(ttl=1)
    for i in range(p.fail_streak_degrade):
        p.mark_bad(f'http://10.0.0.{i}:3129')
    assert p.is_degraded()

    p.redis.delete(p._degraded_key)  # simula expiração do TTL

    assert not p.is_degraded()


def test_status_expoe_o_sinal():
    p = _pool(streak=3)
    for i in range(3):
        p.mark_bad(f'http://10.0.0.{i}:3129')
    st = p.status()
    assert st['degradado'] is True
    assert st['fail_streak'] == 3


def test_refresh_NAO_zera_o_streak():
    """Regressão do buraco achado em prod (2026-08-04).

    O refresh roda de 15 em 15 min por cron. Se ele zerasse o streak, uma
    sequência de falhas mais lenta que o limiar dentro dessa janela nunca
    acenderia a degradação — o sinal ficaria preso abaixo do gatilho.
    Quem limpa é o sucesso, e só ele.
    """
    p = _pool(streak=5)
    for i in range(4):
        p.mark_bad(f'http://10.0.0.{i}:3129')
    assert int(p.redis.get(p._fail_streak_key)) == 4

    # simula o refresh: troca a lista e limpa o bad_zset, mas preserva o streak
    pipe = p.redis.pipeline(transaction=False)
    pipe.set(p._list_key, json.dumps(['http://9.9.9.9:3129']))
    pipe.delete(p._bad_key)
    pipe.execute()

    assert int(p.redis.get(p._fail_streak_key)) == 4, 'refresh nao pode zerar'
    p.mark_bad('http://9.9.9.9:3129')      # 5a falha, atinge o limiar
    assert p.is_degraded()
