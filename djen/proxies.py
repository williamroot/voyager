import json
import logging
import random
import threading
import time
from typing import Optional

import redis
import requests
from django.conf import settings

logger = logging.getLogger('voyager.proxies')

CORTEX_BAD_KEY = 'voyager:proxies:cortex:bad'

# In-memory cache para _healthy_list() — evita round-trip Redis em cada request.
_HEALTHY_CACHE_TTL = 30  # segundos


class ProxyScrapePool:
    """Pool rotativo de proxies vindos da API ProxyScrape, compartilhado entre workers via Redis.

    Suporta múltiplas contas/API keys em paralelo via `name`. Cada nome
    gera chaves Redis independentes (`voyager:proxies:<name>:*`), evitando
    que pools diferentes sobrescrevam a lista uma da outra.

    Uso padrão (conta principal):
        ProxyScrapePool.singleton()

    Conta secundária (ex: Datajud em máquina específica):
        ProxyScrapePool.singleton(name='datajud', api_key='<key>')
    """

    _instances: dict[str, 'ProxyScrapePool'] = {}
    _lock = threading.Lock()

    def __init__(self, name: str = 'default', api_key: Optional[str] = None):
        self.name = name
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.api_key = api_key or settings.PROXYSCRAPE_API_KEY
        self.bad_ttl = settings.PROXY_BAD_TTL_SECONDS
        self.cortex_bad_ttl = getattr(settings, 'CORTEX_BAD_TTL_SECONDS', 15)
        self.refresh_threshold = getattr(settings, 'DJEN_POOL_REFRESH_THRESHOLD', 20)
        self._list_key = f'voyager:proxies:{name}:list'
        self._bad_key = f'voyager:proxies:{name}:bad_zset'
        self._healthy_cache: list[str] = []
        self._healthy_cache_ts: float = 0.0
        self._last_refresh_attempt: float = 0.0

    @classmethod
    def singleton(cls, name: str = 'default', api_key: Optional[str] = None) -> 'ProxyScrapePool':
        if name not in cls._instances:
            with cls._lock:
                if name not in cls._instances:
                    cls._instances[name] = cls(name=name, api_key=api_key)
        return cls._instances[name]

    def get(self) -> Optional[str]:
        proxies = self._healthy_list()
        # Auto-refresh quando saudáveis abaixo do limiar — em ondas WAF
        # o pool fica 99% queimado e o refresh agendado (15min) demora
        # demais. Throttle de 60s entre tentativas pra não martelar a API.
        if len(proxies) < self.refresh_threshold:
            now = time.time()
            if now - self._last_refresh_attempt > 60:
                self._last_refresh_attempt = now
                logger.warning('pool[%s] degradado (%d saudáveis < %d): forçando refresh',
                               self.name, len(proxies), self.refresh_threshold)
                self.refresh()
                proxies = self._healthy_list()
        if not proxies:
            return None
        return random.choice(proxies)

    def _healthy_list(self) -> list[str]:
        now = time.time()
        if now - self._healthy_cache_ts < _HEALTHY_CACHE_TTL and self._healthy_cache:
            return self._healthy_cache

        raw = self.redis.get(self._list_key)
        if not raw:
            return []
        try:
            all_proxies = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not all_proxies:
            return []

        # Remove expirados e pega bad set num pipeline — O(log n + n_bad) em vez de KEYS O(n_total)
        pipe = self.redis.pipeline(transaction=False)
        pipe.zremrangebyscore(self._bad_key, '-inf', now)
        pipe.zrange(self._bad_key, 0, -1)
        _, bad_list = pipe.execute()
        bad_set = set(bad_list)

        healthy = [p for p in all_proxies if p not in bad_set]
        self._healthy_cache = healthy
        self._healthy_cache_ts = now
        return healthy

    def mark_bad(self, url: str) -> None:
        if not url:
            return
        expiry = time.time() + self.bad_ttl
        self.redis.zadd(self._bad_key, {url: expiry})
        self._healthy_cache_ts = 0.0  # invalida cache local
        logger.warning('proxy ruim [%s]: %s (ttl=%ds)', self.name, url, self.bad_ttl)

    def mark_cortex_bad(self, ttl: Optional[int] = None) -> None:
        ttl = ttl if ttl is not None else self.cortex_bad_ttl
        self.redis.set(CORTEX_BAD_KEY, '1', ex=ttl)
        logger.warning('cortex em cooldown por %ds (rate-limited)', ttl)

    def cortex_is_bad(self) -> bool:
        return bool(self.redis.exists(CORTEX_BAD_KEY))

    def load_from_file(self, path: str) -> int:
        proxies = []
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if not line.startswith('http'):
                    line = f'http://{line}'
                proxies.append(line)
        self.redis.set(self._list_key, json.dumps(proxies))
        logger.info('pool[%s] carregado do arquivo: %d proxies', self.name, len(proxies))
        return len(proxies)

    # Endpoints tentados em ordem. O datacenter_shared exige plano específico;
    # se retornar "Invalid session", cai no endpoint genérico (funciona com
    # qualquer plano pago). Ambos retornam ip:port por linha.
    _REFRESH_URLS = [
        'https://api.proxyscrape.com/v2/account/datacenter_shared/proxy-list'
        '?auth={key}&type=getproxies&protocol=http&format=normal&country=BR',
        'https://api.proxyscrape.com/v2/?request=getproxies'
        '&auth={key}&protocol=http&country=BR',
    ]

    def refresh(self) -> int:
        if not self.api_key:
            logger.warning('pool[%s] sem API key — pool vazio', self.name)
            self.redis.set(self._list_key, json.dumps([]))
            return 0
        text = None
        for url_tpl in self._REFRESH_URLS:
            url = url_tpl.format(key=self.api_key)
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.warning('pool[%s] endpoint indisponível, tentando próximo: %s', self.name, exc)
                continue
            if 'invalid session' in resp.text.lower() or 'unauthorized' in resp.text.lower():
                logger.warning('pool[%s] endpoint não suportado pelo plano, tentando próximo', self.name)
                continue
            text = resp.text
            break
        if text is None:
            logger.error('pool[%s] todos os endpoints falharam', self.name)
            return 0
        proxies = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('<') or line.startswith('{'):
                continue
            if not line.startswith('http'):
                line = f'http://{line}'
            proxies.append(line)
        self.redis.set(self._list_key, json.dumps(proxies))
        self._healthy_cache_ts = 0.0
        logger.info('pool[%s] ProxyScrape atualizado: %d proxies BR', self.name, len(proxies))
        return len(proxies)

    def status(self) -> dict:
        raw = self.redis.get(self._list_key)
        try:
            total = len(json.loads(raw)) if raw else 0
        except (TypeError, json.JSONDecodeError):
            total = 0
        now = time.time()
        bad_count = self.redis.zcount(self._bad_key, now, '+inf')
        return {'total': total, 'bad': bad_count, 'saudaveis': max(total - bad_count, 0)}

    def is_degraded(self) -> bool:
        """Pool está em estado crítico (saudáveis abaixo do limiar de refresh).
        Cliente DJEN usa pra forçar mais tráfego via Cortex residencial."""
        return len(self._healthy_list()) < self.refresh_threshold


def cortex_proxy_url(pool: Optional['ProxyScrapePool'] = None) -> Optional[str]:
    if not (settings.CORTEX_FALLBACK_ENABLED and settings.CORTEX_PROXY_URL):
        return None
    if pool and pool.cortex_is_bad():
        return None
    return settings.CORTEX_PROXY_URL
