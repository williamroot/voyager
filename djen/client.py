import logging
import random
import time
from datetime import date
from typing import Iterator, Optional

import requests
from django.conf import settings

from .proxies import ProxyScrapePool

logger = logging.getLogger('voyager.djen.client')


class DjenClientError(Exception):
    pass


class DJENClient:
    """Cliente HTTP da DJEN com paginação, retry exponencial e rotação de proxies."""

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        self.base_url = settings.DJEN_BASE_URL
        self.page_sleep = settings.DJEN_PAGE_SLEEP_SECONDS
        self.max_retries = settings.DJEN_MAX_RETRIES
        self.max_proxy_rotations = getattr(settings, 'DJEN_MAX_PROXY_ROTATIONS', 50)
        self.timeout = (settings.DJEN_REQUEST_TIMEOUT_CONNECT, settings.DJEN_REQUEST_TIMEOUT_READ)
        self.user_agent = settings.DJEN_USER_AGENT
        self.pool = pool or ProxyScrapePool.singleton()
        self.session = requests.Session()
        # Quando True (cliques manuais via fila `manual`), tenta Cortex
        # primeiro — proxy residencial premium, success rate muito maior
        # que pool ProxyScrape rotativo. Click do user retorna em ~3-10s
        # em vez de 30s+ rotacionando proxies queimados.
        self.prefer_cortex = prefer_cortex
        # Em modo normal (não-manual) intercala Cortex/Pool por request via
        # sorteio nesta proporção. Cada request sai com IP diferente —
        # diversifica de verdade quando o WAF bloqueia datacenter em onda.
        self.cortex_ratio = getattr(settings, 'DJEN_CORTEX_RATIO', 0.5)

    def count_window(self, sigla_djen: str, data_inicio: date, data_fim: date) -> int:
        """Devolve o total de movimentações que a DJEN diz existir nessa janela.
        Faz 1 request com itensPorPagina=1 — barato, usado em auditoria."""
        payload = self._fetch(sigla_djen, data_inicio, data_fim, pagina=1)
        return int(payload.get('count') or 0)

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

    def _fetch(self, sigla_djen: str, data_inicio: date, data_fim: date, pagina: int,
               itens_por_pagina: int = 100, extra_params: Optional[dict] = None) -> dict:
        params = {
            'pagina': pagina,
            'itensPorPagina': itens_por_pagina,
            'siglaTribunal': sigla_djen,
            'dataDisponibilizacaoInicio': data_inicio.isoformat(),
            'dataDisponibilizacaoFim': data_fim.isoformat(),
        }
        if extra_params:
            params.update(extra_params)
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}

        last_exc: Optional[Exception] = None
        last_failed_source: Optional[str] = None
        proxy_rotations = 0
        transport_retries = 0

        while True:
            proxy_url, using = self._pick_proxy(prefer_other_than=last_failed_source)
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

            t0 = time.monotonic()
            try:
                resp = self.session.get(self.base_url, params=params, headers=headers,
                                        proxies=proxies, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                proxy_label = using if proxy_url else 'direct'
                # 403/429: IP bloqueado → marca proxy ruim e troca.
                # Backoff progressivo quando muitas rotações falham seguidas:
                # WAF da DJEN tipicamente "abre" se pausarmos um momento.
                if resp.status_code in (403, 429):
                    if using == 'pool' and proxy_url:
                        self.pool.mark_bad(proxy_url)
                    elif using == 'cortex':
                        self.pool.mark_cortex_bad()  # usa CORTEX_BAD_TTL_SECONDS
                    last_failed_source = using
                    proxy_rotations += 1
                    if proxy_rotations > self.max_proxy_rotations:
                        raise DjenClientError(
                            f'DJEN {resp.status_code} após {proxy_rotations} rotações de proxy: {resp.text[:200]}'
                        )
                    logger.warning(
                        '🔄 %s bloqueado via %s → rotação %d/%d',
                        resp.status_code, proxy_label, proxy_rotations, self.max_proxy_rotations,
                    )
                    pause_after = getattr(settings, 'DJEN_ROTATION_PAUSE_AFTER', 10)
                    pause_step = getattr(settings, 'DJEN_ROTATION_PAUSE_STEP', 5.0)
                    pause_max = getattr(settings, 'DJEN_ROTATION_PAUSE_MAX', 30.0)
                    if proxy_rotations >= pause_after and proxy_rotations % pause_after == 0:
                        wait = min(pause_max, pause_step * (proxy_rotations // pause_after))
                        logger.warning(
                            'WAF wave: %d rotações falhando seguidas, pausando %ds',
                            proxy_rotations, wait,
                        )
                        time.sleep(wait)
                    continue
                # 5xx: erro do servidor → backoff longo, limite de retries de transporte.
                if 500 <= resp.status_code < 600:
                    transport_retries += 1
                    if transport_retries >= self.max_retries:
                        raise DjenClientError(
                            f'DJEN {resp.status_code} após {self.max_retries} tentativas: {resp.text[:200]}'
                        )
                    logger.warning(
                        '⏳ %s servidor via %s → retry #%d', resp.status_code, proxy_label, transport_retries,
                    )
                    self._sleep_backoff(transport_retries, factor=3.0, max_wait=180.0)
                    continue
                if 400 <= resp.status_code < 500:
                    raise DjenClientError(f'DJEN {resp.status_code}: {resp.text[:200]}')
                resp.raise_for_status()
                logger.debug(
                    '✅ %s pg=%d → %d via %s %dms [rot=%d retry=%d]',
                    sigla_djen, pagina, resp.status_code, proxy_label,
                    latency_ms, proxy_rotations, transport_retries,
                )
                return resp.json()
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                last_exc = exc
                if proxy_url and using == 'pool':
                    self.pool.mark_bad(proxy_url)
                last_failed_source = using
                transport_retries += 1
                logger.warning(
                    '🔌 transporte #%d via %s: %s',
                    transport_retries, using if proxy_url else 'direct', str(exc)[:120],
                )
                if transport_retries >= self.max_retries:
                    raise DjenClientError(
                        f'erro de transporte após {self.max_retries} tentativas: {exc}'
                    ) from exc
                self._sleep_backoff(transport_retries)
                continue

    def _pick_proxy(self, prefer_other_than: Optional[str] = None) -> tuple[Optional[str], str]:
        from .proxies import cortex_proxy_url

        cortex = cortex_proxy_url(self.pool)
        # Modo manual (cliques do user): Cortex sempre primeiro — latência
        # baixa importa mais que diversificar fontes.
        if self.prefer_cortex:
            if cortex and prefer_other_than != 'cortex':
                return cortex, 'cortex'
            proxy = self.pool.get()
            if proxy:
                return proxy, 'pool'
            return (cortex, 'cortex') if cortex else (None, 'direct')

        # Modo normal: sorteia entre Cortex e Pool em cada request. IP varia
        # entre fontes a cada chamada, distribuindo carga e contornando
        # ondas de WAF que bloqueiam só datacenter ou só residencial.
        # `prefer_other_than` (passado em retry) força a fonte alternativa.
        if prefer_other_than == 'cortex':
            quer_cortex = False
        elif prefer_other_than == 'pool':
            quer_cortex = True
        else:
            # Quando pool degradado, joga 90% via Cortex — datacenter queimado
            # não vale a aposta de 50/50.
            ratio = 0.9 if self.pool.is_degraded() else self.cortex_ratio
            quer_cortex = random.random() < ratio

        if quer_cortex and cortex:
            return cortex, 'cortex'
        proxy = self.pool.get()
        if proxy:
            return proxy, 'pool'
        # Fallback final: usa o que sobrou.
        if cortex:
            return cortex, 'cortex'
        return None, 'direct'


    def _sleep_backoff(self, attempt: int, factor: float = 1.0, max_wait: float = 60.0) -> None:
        wait = min(max_wait, 3.0 * factor * (2 ** attempt) + random.uniform(0, 2))
        time.sleep(wait)

    def count_only(self, sigla_djen: str, data_inicio: date, data_fim: date) -> int:
        payload = self._fetch(sigla_djen, data_inicio, data_fim, 1)
        return int(payload.get('count') or 0)

    def iter_pages_processo(self, sigla_djen: str, numero_cnj: str) -> Iterator[list[dict]]:
        """Itera todas as movimentações de UM processo (sem filtro de data).

        DJEN aceita numeroProcesso=<CNJ formatado ou sem máscara> + siglaTribunal.
        Retorna o histórico completo do processo paginado de 100 em 100.
        """
        pagina = 1
        while True:
            payload = self._fetch_processo(sigla_djen, numero_cnj, pagina)
            items = payload.get('items') or []
            if not items:
                return
            yield items
            count = payload.get('count') or 0
            if pagina * 100 >= count:
                return
            pagina += 1
            time.sleep(self.page_sleep)

    def _fetch_processo(self, sigla_djen: str, numero_cnj: str, pagina: int) -> dict:
        # DJEN aceita ambas as formas; usamos sem máscara pra evitar problemas de URL encoding
        unmask = numero_cnj.replace('-', '').replace('.', '')
        params = {
            'pagina': pagina,
            'itensPorPagina': 100,
            'siglaTribunal': sigla_djen,
            'numeroProcesso': unmask,
        }
        # Reaproveita pipeline de retry/proxy chamando _fetch genérico via params custom.
        # Como _fetch hoje recebe data_inicio/data_fim, vamos chamar diretamente o session.get
        # com a mesma estratégia de proxy.
        return self._fetch_generic(params)

    def _fetch_generic(self, params: dict) -> dict:
        """Versão genérica do _fetch que aceita qualquer params dict.
        Usa a mesma estratégia de proxy + retry de _fetch.
        """
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
                logger.info('djen request (processo)', extra={
                    'params': params, 'attempt': attempt,
                    'proxy': using if proxy_url else 'direct',
                    'status_code': resp.status_code, 'latency_ms': latency_ms,
                })
                if resp.status_code in (403, 429):
                    if proxy_url and using == 'pool':
                        self.pool.mark_bad(proxy_url)
                    last_failed_source = using
                    if attempt >= self.max_retries:
                        raise DjenClientError(f'DJEN {resp.status_code} após {self.max_retries} tentativas')
                    self._sleep_backoff(attempt)
                    continue
                if 500 <= resp.status_code < 600:
                    if attempt >= self.max_retries:
                        raise DjenClientError(f'DJEN {resp.status_code} após {self.max_retries} tentativas')
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
                if attempt >= self.max_retries:
                    raise DjenClientError(f'erro de transporte: {exc}') from exc
                self._sleep_backoff(attempt)
                continue
        raise DjenClientError(f'esgotadas tentativas: {last_exc}')
