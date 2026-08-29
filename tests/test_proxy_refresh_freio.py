"""O auto-refresh do pool não pode ser disparado por CADA job.

Regressão da pendência #100 (29/08/2026). O throttle do auto-refresh era
`time.time() - self._last_refresh_attempt > 60`, um atributo de INSTÂNCIA. O
`rqworker` roda cada job num fork, e o filho nasce com o contador do pai (0.0)
— ou seja, o freio reiniciava a cada job. Resultado medido na `.102`: **4.174
chamadas à API da ProxyScrape em 10 minutos** (≈ 25 mil/h), e o Cloudflare da
ProxyScrape passou a devolver `HTTP 429` a todas — inclusive à do cron de 15
min que era a única legítima. Sem reposição, o pool de 2.500 IPs virou 25.

Um freio que mora na memória do processo é inerte num modelo fork-por-job, do
mesmo jeito que `SET LOCAL` é inerte em autocommit.
"""
from djen.proxies import ProxyScrapePool


class _RedisFake:
    def __init__(self):
        self.kv: dict = {}
        self.z: dict = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    def exists(self, k):
        return 1 if k in self.kv else 0

    def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)

    def incr(self, k):
        self.kv[k] = int(self.kv.get(k) or 0) + 1
        return self.kv[k]

    def zadd(self, k, mapping):
        self.z.setdefault(k, {}).update(mapping)

    def zremrangebyscore(self, k, lo, hi):
        pass

    def zrange(self, k, a, b):
        return list(self.z.get(k, {}))

    def zcount(self, k, a, b):
        return len(self.z.get(k, {}))

    def zcard(self, k):
        return len(self.z.get(k, {}))

    def pipeline(self, transaction=True):
        return _PipeFake(self)


class _PipeFake:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def __getattr__(self, nome):
        def _op(*a, **kw):
            self.ops.append((nome, a, kw))
            return self
        return _op

    def execute(self):
        out = []
        for nome, a, kw in self.ops:
            out.append(getattr(self.r, nome)(*a, **kw))
        self.ops = []
        return out


def _pool(redis_fake):
    p = ProxyScrapePool.__new__(ProxyScrapePool)
    p.name = 'teste'
    p.redis = redis_fake
    p.api_key = 'x'
    p.bad_ttl = 120
    p.refresh_threshold = 20
    p._list_key = 'voyager:proxies:teste:list'
    p._bad_key = 'voyager:proxies:teste:bad_zset'
    p._fail_streak_key = 'voyager:proxies:teste:fail_streak'
    p._degraded_key = 'voyager:proxies:teste:degraded'
    p._refresh_lock_key = 'voyager:proxies:teste:refresh_lock'
    p._refresh_cooldown_key = 'voyager:proxies:teste:refresh_cooldown'
    p.refresh_min_interval = 60
    p.refresh_cooldown = 300
    p._healthy_cache = []
    p._healthy_cache_ts = 0.0
    p._last_refresh_attempt = 0.0
    return p


def test_so_um_processo_da_frota_tenta_o_refresh_por_janela():
    """Cada `_pool()` é um processo novo (o fork do rqworker). O freio tem que
    valer entre eles, não dentro de um só."""
    r = _RedisFake()
    permitidos = sum(1 for _ in range(200) if _pool(r)._pode_tentar_refresh())
    assert permitidos == 1, (
        f'{permitidos} de 200 processos passariam pelo freio — '
        'cada um faz 2 chamadas à API da ProxyScrape')


def test_429_da_api_suspende_ate_o_cron_e_nao_so_o_job():
    r = _RedisFake()
    p = _pool(r)
    assert p._pode_tentar_refresh() is True
    p._armar_cooldown_refresh(quantos_429=2)
    # Nem o processo que ganhou o lock, nem nenhum outro, tenta de novo.
    assert p._pode_tentar_refresh() is False
    assert _pool(r)._pode_tentar_refresh() is False
    assert p.status()['refresh_em_cooldown'] is True


def test_cooldown_expirado_libera_de_novo():
    r = _RedisFake()
    p = _pool(r)
    p._armar_cooldown_refresh(quantos_429=1)
    assert p._pode_tentar_refresh() is False
    r.delete(p._refresh_cooldown_key)   # efeito observável do TTL vencendo
    r.delete(p._refresh_lock_key)
    assert p._pode_tentar_refresh() is True


# --- Assinatura vencida: downgrade silencioso do pool -----------------------
# Em 29/08/2026 o endpoint pago da ProxyScrape respondia
# `HTTP 401 {"status": "unauthorized", "info": "Your subscription is expired."}`.
# O `refresh()` chamava `raise_for_status()` ANTES de olhar o corpo, então o 401
# virava um `except RequestException` genérico logado como "endpoint
# indisponível" — e o código caía calado no endpoint público, que devolve 14
# proxies. O pool documentado como "2.500" era 14, e tudo o mais (pool 100%
# queimado, tráfego inteiro no Cortex, tempestade de refresh) vinha daí.


class _RespFake:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


def test_assinatura_vencida_e_erro_e_cai_no_endpoint_publico(monkeypatch):
    """O log do projeto não propaga pra raiz, então o teste pendura um handler
    no próprio logger — senão ele mediria a config de logging, não o código."""
    import logging

    import djen.proxies as mod

    respostas = [
        _RespFake(401, '{"status": "unauthorized", "info": "Your subscription is expired."}'),
        _RespFake(200, '1.2.3.4:8080\n5.6.7.8:3128\n'),
    ]
    monkeypatch.setattr(mod.requests, 'get', lambda *a, **kw: respostas.pop(0))

    capturados: list[logging.LogRecord] = []

    class _Coletor(logging.Handler):
        def emit(self, record):
            capturados.append(record)

    h = _Coletor()
    mod.logger.addHandler(h)
    try:
        assert _pool(_RedisFake()).refresh() == 2, 'devia ter usado o endpoint genérico'
    finally:
        mod.logger.removeHandler(h)

    erros = [x for x in capturados if x.levelno >= logging.ERROR]
    assert erros, 'assinatura vencida entrou sem ERROR — downgrade mudo de novo'
    assert 'expired' in erros[0].getMessage().lower()
