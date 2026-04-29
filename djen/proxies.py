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

PROXY_LIST_KEY = 'voyager:proxies:scrape:list'
PROXY_BAD_ZSET = 'voyager:proxies:scrape:bad_zset'  # score = expiry unix timestamp
CORTEX_BAD_KEY = 'voyager:proxies:cortex:bad'

# In-memory cache para _healthy_list() — evita round-trip Redis em cada request.
_HEALTHY_CACHE_TTL = 30  # segundos


class ProxyScrapePool:
    """Pool rotativo de proxies vindos da API ProxyScrape, compartilhado entre workers via Redis."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.api_key = settings.PROXYSCRAPE_API_KEY
        self.bad_ttl = settings.PROXY_BAD_TTL_SECONDS
        self._healthy_cache: list[str] = []
        self._healthy_cache_ts: float = 0.0

    @classmethod
    def singleton(cls) -> 'ProxyScrapePool':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get(self) -> Optional[str]:
        proxies = self._healthy_list()
        if not proxies:
            self.refresh()
            proxies = self._healthy_list()
        if not proxies:
            return None
        return random.choice(proxies)

    def _healthy_list(self) -> list[str]:
        now = time.time()
        if now - self._healthy_cache_ts < _HEALTHY_CACHE_TTL and self._healthy_cache:
            return self._healthy_cache

        raw = self.redis.get(PROXY_LIST_KEY)
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
        pipe.zremrangebyscore(PROXY_BAD_ZSET, '-inf', now)
        pipe.zrange(PROXY_BAD_ZSET, 0, -1)
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
        self.redis.zadd(PROXY_BAD_ZSET, {url: expiry})
        self._healthy_cache_ts = 0.0  # invalida cache local
        logger.warning('proxy ruim: %s (ttl=%ds)', url, self.bad_ttl)

    def mark_cortex_bad(self, ttl: int = 60) -> None:
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
        self.redis.set(PROXY_LIST_KEY, json.dumps(proxies))
        logger.info('pool carregado do arquivo: %d proxies', len(proxies))
        return len(proxies)

    def refresh(self) -> int:
        if not self.api_key:
            logger.warning('PROXYSCRAPE_API_KEY não configurada — pool vazio')
            self.redis.set(PROXY_LIST_KEY, json.dumps([]))
            return 0
        url = (
            'https://api.proxyscrape.com/v2/account/datacenter_shared/proxy-list'
            f'?auth={self.api_key}&type=getproxies&protocol=http&format=normal&country=BR'
        )
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error('falha ao atualizar pool ProxyScrape: %s', exc)
            return 0
        proxies = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith('http'):
                line = f'http://{line}'
            proxies.append(line)
        self.redis.set(PROXY_LIST_KEY, json.dumps(proxies))
        self._healthy_cache_ts = 0.0
        logger.info('pool ProxyScrape atualizado: %d proxies BR', len(proxies))
        return len(proxies)

    def status(self) -> dict:
        raw = self.redis.get(PROXY_LIST_KEY)
        try:
            total = len(json.loads(raw)) if raw else 0
        except (TypeError, json.JSONDecodeError):
            total = 0
        now = time.time()
        bad_count = self.redis.zcount(PROXY_BAD_ZSET, now, '+inf')
        return {'total': total, 'bad': bad_count, 'saudaveis': max(total - bad_count, 0)}


def cortex_proxy_url(pool: Optional['ProxyScrapePool'] = None) -> Optional[str]:
    if not (settings.CORTEX_FALLBACK_ENABLED and settings.CORTEX_PROXY_URL):
        return None
    if pool and pool.cortex_is_bad():
        return None
    return settings.CORTEX_PROXY_URL
