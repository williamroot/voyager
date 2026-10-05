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
import json

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
    p.subaccount_id = 'sub-uuid'
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
#
# Em 05/10/2026 o acesso migrou para a Account API v4 e o fallback público
# saiu: recusa da API é ERRO e o pool fica com a última lista boa.


class _RespFake:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class _Coletor:
    """O log do projeto não propaga pra raiz, então o teste pendura um handler
    no próprio logger — senão ele mediria a config de logging, não o código."""

    def __init__(self, logger):
        import logging

        self.registros = []
        coletor = self

        class _H(logging.Handler):
            def emit(self, record):
                coletor.registros.append(record)

        self.logger, self.h = logger, _H()

    def __enter__(self):
        self.logger.addHandler(self.h)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.h)

    def erros(self):
        import logging
        return [x for x in self.registros if x.levelno >= logging.ERROR]


def _api(monkeypatch, *respostas):
    import djen.proxies as mod

    fila = list(respostas)
    chamadas = []

    def _get(url, **kw):
        chamadas.append((url, kw))
        return fila.pop(0)

    monkeypatch.setattr(mod.requests, 'get', _get)
    return chamadas


def test_refresh_usa_api_v4_da_subconta_com_header_api_token(monkeypatch):
    chamadas = _api(monkeypatch, _RespFake(200, '1.2.3.4:3129\n5.6.7.8:3129\n'))
    r = _RedisFake()
    p = _pool(r)

    assert p.refresh() == 2
    url, kw = chamadas[0]
    assert url.startswith(
        'https://api.proxyscrape.com/v4/account/sub-uuid/datacenter_shared/proxy-list?')
    assert 'protocol=http' in url and 'country%5B%5D=all' in url
    assert 'auth=' not in url
    assert kw['headers'] == {'api-token': 'x'}
    assert json.loads(r.get(p._list_key)) == ['http://1.2.3.4:3129', 'http://5.6.7.8:3129']


def test_assinatura_vencida_e_erro_e_mantem_a_lista_anterior(monkeypatch):
    import djen.proxies as mod

    chamadas = _api(monkeypatch, _RespFake(
        401, '{"status": "unauthorized", "info": "Your subscription is expired."}'))
    r = _RedisFake()
    p = _pool(r)
    r.set(p._list_key, json.dumps(['http://9.9.9.9:3129']))

    with _Coletor(mod.logger) as log:
        assert p.refresh() == 0

    assert len(chamadas) == 1, 'não pode cair em outro endpoint (proxies públicos)'
    assert json.loads(r.get(p._list_key)) == ['http://9.9.9.9:3129']
    assert log.erros(), 'assinatura vencida entrou sem ERROR — downgrade mudo de novo'
    assert 'expired' in log.erros()[0].getMessage().lower()


def test_200_sem_proxies_nao_apaga_a_lista(monkeypatch):
    _api(monkeypatch, _RespFake(200, '<!DOCTYPE html><title>Not Found</title>'))
    r = _RedisFake()
    p = _pool(r)
    r.set(p._list_key, json.dumps(['http://9.9.9.9:3129']))

    assert p.refresh() == 0
    assert json.loads(r.get(p._list_key)) == ['http://9.9.9.9:3129']


def test_429_no_refresh_arma_o_cooldown(monkeypatch):
    _api(monkeypatch, _RespFake(429, 'error code: 1015'))
    r = _RedisFake()
    p = _pool(r)

    assert p.refresh() == 0
    assert p.status()['refresh_em_cooldown'] is True
