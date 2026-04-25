import logging
import random
import time
from datetime import date
from typing import Iterator, Optional

import requests
from django.conf import settings

from .proxies import ProxyScrapePool, cortex_proxy_url

logger = logging.getLogger('voyager.djen.client')


class DjenClientError(Exception):
    pass


class DJENClient:
    """Cliente HTTP da DJEN com paginação, retry exponencial e rotação de proxies."""

    def __init__(self, pool: Optional[ProxyScrapePool] = None):
        self.base_url = settings.DJEN_BASE_URL
        self.page_sleep = settings.DJEN_PAGE_SLEEP_SECONDS
        self.max_retries = settings.DJEN_MAX_RETRIES
        self.timeout = (settings.DJEN_REQUEST_TIMEOUT_CONNECT, settings.DJEN_REQUEST_TIMEOUT_READ)
        self.user_agent = settings.DJEN_USER_AGENT
        self.pool = pool or ProxyScrapePool.singleton()
        self.session = requests.Session()

    def iter_pages(self, sigla_djen: str, data_inicio: date, data_fim: date) -> Iterator[list[dict]]:
        pagina = 1
        while True:
            payload = self._fetch(sigla_djen, data_inicio, data_fim, pagina)
            items = payload.get('items') or []
            if not items:
                return
            yield items
            count = payload.get('count') or 0
            if pagina * 100 >= count:
                return
            pagina += 1
            time.sleep(self.page_sleep)

    def _fetch(self, sigla_djen: str, data_inicio: date, data_fim: date, pagina: int) -> dict:
        params = {
            'pagina': pagina,
            'itensPorPagina': 100,
            'siglaTribunal': sigla_djen,
            'dataDisponibilizacaoInicio': data_inicio.isoformat(),
            'dataDisponibilizacaoFim': data_fim.isoformat(),
        }
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}

        cortex_attempts_left = 1 if cortex_proxy_url() else 0
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            proxy_url = self.pool.get()
            using = 'pool'
            if proxy_url is None and cortex_attempts_left > 0:
                proxy_url = cortex_proxy_url()
                using = 'cortex'
                cortex_attempts_left -= 1
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

            t0 = time.monotonic()
            try:
                resp = self.session.get(self.base_url, params=params, headers=headers,
                                        proxies=proxies, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.info('djen request', extra={
                    'sigla_djen': sigla_djen, 'pagina': pagina, 'attempt': attempt,
                    'proxy': using if proxy_url else 'direct', 'status_code': resp.status_code,
                    'latency_ms': latency_ms,
                })
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if proxy_url and using == 'pool':
                        self.pool.mark_bad(proxy_url)
                    if attempt >= self.max_retries:
                        raise DjenClientError(
                            f'DJEN {resp.status_code} após {self.max_retries} tentativas'
                        )
                    self._sleep_backoff(attempt)
                    continue
                if 400 <= resp.status_code < 500:
                    raise DjenClientError(f'DJEN {resp.status_code}: {resp.text[:200]}')
                resp.raise_for_status()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if proxy_url and using == 'pool':
                    self.pool.mark_bad(proxy_url)
                logger.warning('djen request transport error', extra={
                    'attempt': attempt, 'erro': str(exc), 'proxy': using if proxy_url else 'direct',
                })
                if attempt >= self.max_retries:
                    raise DjenClientError(f'erro de transporte após {self.max_retries} tentativas: {exc}') from exc
                self._sleep_backoff(attempt)
                continue
        raise DjenClientError(f'esgotadas tentativas: {last_exc}')

    def _sleep_backoff(self, attempt: int) -> None:
        wait = min(60.0, 3.0 * (2 ** attempt) + random.uniform(0, 2))
        time.sleep(wait)

    def count_only(self, sigla_djen: str, data_inicio: date, data_fim: date) -> int:
        payload = self._fetch(sigla_djen, data_inicio, data_fim, 1)
        return int(payload.get('count') or 0)
