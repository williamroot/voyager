import json
import logging
import random
import threading
from typing import Optional

import redis
import requests
from django.conf import settings

logger = logging.getLogger('voyager.proxies')

PROXY_LIST_KEY = 'voyager:proxies:scrape:list'
PROXY_BAD_PREFIX = 'voyager:proxies:scrape:bad:'


class ProxyScrapePool:
    """Pool rotativo de proxies vindos da API ProxyScrape, compartilhado entre workers via Redis."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.api_key = settings.PROXYSCRAPE_API_KEY
        self.bad_ttl = settings.PROXY_BAD_TTL_SECONDS

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
        raw = self.redis.get(PROXY_LIST_KEY)
        if not raw:
            return []
        try:
            all_proxies = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not all_proxies:
            return []
        bad = self.redis.keys(PROXY_BAD_PREFIX + '*')
        bad_set = {b.split(':')[-1] for b in bad}
        return [p for p in all_proxies if p not in bad_set]

    def mark_bad(self, url: str) -> None:
        if not url:
            return
        key = PROXY_BAD_PREFIX + url
        self.redis.set(key, '1', ex=self.bad_ttl)
        logger.warning('proxy marcado ruim', extra={'proxy': url, 'ttl': self.bad_ttl})

    def refresh(self) -> int:
        if not self.api_key:
            logger.warning('PROXYSCRAPE_API_KEY não configurada — pool vazio')
            self.redis.set(PROXY_LIST_KEY, json.dumps([]))
            return 0
        url = (
            'https://api.proxyscrape.com/v2/account/datacenter_shared/proxy-list'
            f'?auth={self.api_key}&type=getproxies&protocol=http&format=normal&country=all'
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
        logger.info('pool ProxyScrape atualizado', extra={'count': len(proxies)})
        return len(proxies)

    def status(self) -> dict:
        raw = self.redis.get(PROXY_LIST_KEY)
        try:
            total = len(json.loads(raw)) if raw else 0
        except (TypeError, json.JSONDecodeError):
            total = 0
        bad_count = len(self.redis.keys(PROXY_BAD_PREFIX + '*'))
        return {'total': total, 'bad': bad_count, 'saudaveis': max(total - bad_count, 0)}


def cortex_proxy_url() -> Optional[str]:
    if settings.CORTEX_FALLBACK_ENABLED and settings.CORTEX_PROXY_URL:
        return settings.CORTEX_PROXY_URL
    return None
