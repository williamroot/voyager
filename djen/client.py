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

        last_exc: Optional[Exception] = None
        last_failed_source: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            proxy_url, using = self._pick_proxy(prefer_other_than=last_failed_source)
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
                # 403/429: proxy bloqueado/limitado → marca ruim e tenta outro proxy.
                # 5xx: erro do servidor DJEN → backoff longo e retry mantendo o proxy.
                # 4xx restantes: erro real de request → sem retry.
                if resp.status_code in (403, 429):
                    if proxy_url and using == 'pool':
                        self.pool.mark_bad(proxy_url)
                    last_failed_source = using
                    if attempt >= self.max_retries:
                        raise DjenClientError(
                            f'DJEN {resp.status_code} após {self.max_retries} tentativas: {resp.text[:200]}'
                        )
                    self._sleep_backoff(attempt)
                    continue
                if 500 <= resp.status_code < 600:
                    if attempt >= self.max_retries:
                        raise DjenClientError(
                            f'DJEN {resp.status_code} após {self.max_retries} tentativas: {resp.text[:200]}'
                        )
                    # 504 e outros 5xx geralmente são transitórios mas demoram pra
                    # estabilizar. Backoff mais longo (factor 3, máx 180s).
                    self._sleep_backoff(attempt, factor=3.0, max_wait=180.0)
                    continue
                if 400 <= resp.status_code < 500:
                    raise DjenClientError(f'DJEN {resp.status_code}: {resp.text[:200]}')
                resp.raise_for_status()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                last_exc = exc
                if proxy_url and using == 'pool':
                    self.pool.mark_bad(proxy_url)
                last_failed_source = using
                logger.warning('djen request transport error', extra={
                    'attempt': attempt, 'erro': str(exc), 'proxy': using if proxy_url else 'direct',
                })
                if attempt >= self.max_retries:
                    raise DjenClientError(f'erro de transporte após {self.max_retries} tentativas: {exc}') from exc
                self._sleep_backoff(attempt)
                continue
        raise DjenClientError(f'esgotadas tentativas: {last_exc}')

    def _pick_proxy(self, prefer_other_than: Optional[str] = None) -> tuple[Optional[str], str]:
        """Estratégia híbrida: 80% Cortex (residencial fixo, alta taxa de sucesso) + 20% pool ProxyScrape.

        Em retries, evita repetir a fonte que acabou de falhar — força a alternar.
        """
        cortex = cortex_proxy_url()
        pool_proxy = self.pool.get()

        candidatos = []
        if cortex:
            candidatos.append((cortex, 'cortex', 80))
        if pool_proxy:
            candidatos.append((pool_proxy, 'pool', 20))

        if not candidatos:
            return None, 'direct'

        if prefer_other_than:
            outros = [c for c in candidatos if c[1] != prefer_other_than]
            if outros:
                candidatos = outros

        if len(candidatos) == 1:
            url, source, _ = candidatos[0]
            return url, source

        total = sum(c[2] for c in candidatos)
        roll = random.uniform(0, total)
        acc = 0
        for url, source, weight in candidatos:
            acc += weight
            if roll <= acc:
                return url, source
        url, source, _ = candidatos[-1]
        return url, source

    def _sleep_backoff(self, attempt: int, factor: float = 1.0, max_wait: float = 60.0) -> None:
        wait = min(max_wait, 3.0 * factor * (2 ** attempt) + random.uniform(0, 2))
        time.sleep(wait)

    def count_only(self, sigla_djen: str, data_inicio: date, data_fim: date) -> int:
        payload = self._fetch(sigla_djen, data_inicio, data_fim, 1)
        return int(payload.get('count') or 0)
