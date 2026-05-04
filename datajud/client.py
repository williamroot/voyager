"""Cliente da API pública Datajud do CNJ.

Wiki: https://datajud-wiki.cnj.jus.br/api-publica/

Datajud expõe um endpoint Elasticsearch por tribunal:
  https://api-publica.datajud.cnj.jus.br/api_publica_<sigla_tribunal>/_search

Aceita queries DSL Elasticsearch (POST com body JSON). Retorna o
processo + lista de movimentos. Rate-limit é razoável; usa proxies
quando disponíveis pra escalar.

Diferente do DJEN (publicações em diário), Datajud expõe TODA a
movimentação do processo — inclusive as que não viraram publicação no
diário. Mais completo pra histórico per-processo.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Iterator, Optional

import requests
from django.conf import settings

from djen.proxies import ProxyScrapePool, cortex_proxy_url

logger = logging.getLogger('voyager.datajud.client')


class DatajudClientError(Exception):
    pass


# API key pública oficial (documentada no wiki Datajud / CNJ).
DEFAULT_API_KEY = (
    'APIKey cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=='
)

# Mapping sigla → índice Datajud. Cobre todos os 27 TRTs, 6 TRFs, STF, STJ,
# TST, CSJT, TJs estaduais (todos seguem o padrão sigla minúsculo).
# Consulta de tribunal não-mapeado cai no padrão default (usa a sigla).
_INDEX_OVERRIDES: dict[str, str] = {
    # Adicione exceções aqui se algum tribunal usar índice fora do padrão.
}


def index_for(sigla_tribunal: str) -> str:
    """Mapeia sigla → endpoint Datajud (api_publica_<sigla>).

    Datajud usa lowercased sigla pra todos os tribunais. Ex.:
      TRF1 → api_publica_trf1
      TJSP → api_publica_tjsp
      STJ  → api_publica_stj
    """
    sigla = (sigla_tribunal or '').strip().lower()
    return _INDEX_OVERRIDES.get(sigla_tribunal, f'api_publica_{sigla}')


class DatajudClient:
    """Cliente HTTP da API Datajud com rotação de proxies.

    `prefer_cortex=True` faz tentar Cortex (proxy residencial) antes do
    pool ProxyScrape — usado pra cliques manuais que precisam de latência
    baixa. Pra backfill em massa, default `prefer_cortex=False` usa o
    pool primeiro.
    """

    BASE_URL = 'https://api-publica.datajud.cnj.jus.br'

    def __init__(self, pool: Optional[ProxyScrapePool] = None,
                 prefer_cortex: bool = False, api_key: Optional[str] = None):
        if pool is None:
            datajud_proxy_key = getattr(settings, 'DATAJUD_PROXYSCRAPE_API_KEY', '')
            self.pool = (
                ProxyScrapePool.singleton(name='datajud', api_key=datajud_proxy_key)
                if datajud_proxy_key
                else ProxyScrapePool.singleton()
            )
        else:
            self.pool = pool
        self.api_key = api_key or getattr(settings, 'DATAJUD_API_KEY', None) or DEFAULT_API_KEY
        self.prefer_cortex = prefer_cortex
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': self.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': getattr(settings, 'DJEN_USER_AGENT', 'voyager-datajud/0.1'),
        })
        self.timeout = (10, 60)
        self.max_retries = getattr(settings, 'DJEN_MAX_RETRIES', 8)
        self.max_proxy_rotations = getattr(settings, 'DJEN_MAX_PROXY_ROTATIONS', 50)

    def _pick_proxy(self, exclude: set) -> tuple[Optional[str], str]:
        if self.prefer_cortex:
            cortex = cortex_proxy_url(self.pool)
            if cortex and cortex not in exclude:
                return cortex, 'cortex'
        proxy = self.pool.get()
        if proxy and proxy not in exclude:
            return proxy, 'pool'
        cortex = cortex_proxy_url(self.pool)
        if cortex and cortex not in exclude:
            return cortex, 'cortex'
        return None, 'direct'

    def _post(self, sigla_tribunal: str, body: dict) -> dict:
        """POST no índice do tribunal com rotação automática de proxies."""
        url = f'{self.BASE_URL}/{index_for(sigla_tribunal)}/_search'
        tentados: set = set()
        proxy_rotations = 0
        transport_retries = 0
        last_exc: Optional[Exception] = None

        while True:
            proxy_url, using = self._pick_proxy(tentados)
            if proxy_url:
                tentados.add(proxy_url)
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
            t0 = time.monotonic()
            try:
                resp = self.session.post(url, json=body, proxies=proxies, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                if resp.status_code in (403, 429):
                    if using == 'pool' and proxy_url:
                        self.pool.mark_bad(proxy_url)
                    elif using == 'cortex':
                        self.pool.mark_cortex_bad(ttl=60)
                    proxy_rotations += 1
                    if proxy_rotations > self.max_proxy_rotations:
                        raise DatajudClientError(
                            f'Datajud {resp.status_code} após {proxy_rotations} rotações'
                        )
                    logger.warning(
                        '🔄 datajud %s bloqueado via %s → rotação %d/%d',
                        resp.status_code, using, proxy_rotations, self.max_proxy_rotations,
                    )
                    continue
                if 500 <= resp.status_code < 600:
                    transport_retries += 1
                    if transport_retries >= self.max_retries:
                        raise DatajudClientError(
                            f'Datajud {resp.status_code} após {self.max_retries} retries'
                        )
                    logger.warning(
                        '⏳ datajud %s servidor → retry #%d', resp.status_code, transport_retries,
                    )
                    self._sleep_backoff(transport_retries, factor=3.0, max_wait=120.0)
                    continue
                if 400 <= resp.status_code < 500:
                    raise DatajudClientError(
                        f'Datajud {resp.status_code} {sigla_tribunal}: {resp.text[:200]}'
                    )
                resp.raise_for_status()
                logger.debug(
                    '✅ datajud %s → %d via %s %dms [rot=%d ret=%d]',
                    sigla_tribunal, resp.status_code, using, latency_ms,
                    proxy_rotations, transport_retries,
                )
                return resp.json()
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_exc = exc
                if proxy_url and using == 'pool':
                    self.pool.mark_bad(proxy_url)
                transport_retries += 1
                logger.warning(
                    '🔌 datajud transporte #%d via %s: %s',
                    transport_retries, using, str(exc)[:120],
                )
                if transport_retries >= self.max_retries:
                    raise DatajudClientError(
                        f'erro de transporte Datajud após {self.max_retries} retries'
                    ) from exc
                self._sleep_backoff(transport_retries)

    def _sleep_backoff(self, attempt: int, factor: float = 1.0, max_wait: float = 60.0):
        wait = min(max_wait, 1.5 * factor * (2 ** attempt) + random.uniform(0, 1.5))
        time.sleep(wait)

    # ---------- queries de alto nível ----------

    def fetch_processo(self, sigla_tribunal: str, numero_cnj: str) -> Optional[dict]:
        """Busca um processo pelo CNJ. Retorna o `_source` (dict com classe,
        orgaoJulgador, movimentos, etc.) ou None se não encontrar.

        Datajud aceita CNJ formatado ou raw. Normalizamos pra raw (20 dígitos)
        pra evitar match parcial.
        """
        cnj_raw = ''.join(c for c in (numero_cnj or '') if c.isdigit())
        body = {
            'size': 1,
            'query': {'match': {'numeroProcesso': cnj_raw}},
        }
        data = self._post(sigla_tribunal, body)
        hits = (data.get('hits') or {}).get('hits') or []
        if not hits:
            return None
        return hits[0].get('_source')

    def iter_movimentos(self, sigla_tribunal: str, numero_cnj: str) -> Iterator[list[dict]]:
        """Yields movimentos do processo em chunks. Datajud retorna o array
        completo no primeiro hit — então é 1 chunk só por processo (não há
        paginação interna dos movimentos)."""
        src = self.fetch_processo(sigla_tribunal, numero_cnj)
        if not src:
            return
        movs = src.get('movimentos') or []
        if movs:
            yield movs

    def search_by_date(self, sigla_tribunal: str, data_inicio: str, data_fim: str,
                       size: int = 100, search_after: Optional[list] = None) -> dict:
        """Query por janela de datas (dataAjuizamento ou dataHora dos
        movimentos). Útil pra backfill em massa.

        Usa search_after pra paginar (mais eficiente que from/size). Retorna
        a resposta completa pro caller iterar com search_after = last_sort.
        """
        body = {
            'size': size,
            'sort': [{'@timestamp': {'order': 'asc'}}, {'_id': {'order': 'asc'}}],
            'query': {
                'range': {
                    'dataAjuizamento': {'gte': data_inicio, 'lte': data_fim},
                },
            },
        }
        if search_after:
            body['search_after'] = search_after
        return self._post(sigla_tribunal, body)
