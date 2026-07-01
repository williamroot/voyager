"""Cliente HTTP fino para a API de busca semântica do Zordon.

Uso:
    from dashboard.zordon_client import buscar

    resultado = buscar("precatório federal INSS", limit=10)
    # {"results": [...], "erro": None}

Configuração (via .env / settings):
    ZORDON_URL      URL base do serviço Zordon  (ex.: http://localhost:8011)
    ZORDON_API_KEY  Api-Key para o header Authorization
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger('voyager.zordon_client')

_TIMEOUT = (5, 20)  # (connect, read) em segundos — busca/chunks
# Extração é RAG + LLM local (gpt-oss:20b) — bem mais lenta. Read maior, porém
# < timeout do gunicorn do web (60s) pra degradar com nota em vez de derrubar o worker.
_EXTRACT_TIMEOUT = (5, 50)


def buscar(
    query: str,
    *,
    limit: int = 10,
    cnj: str | None = None,
    rerank: bool = True,
) -> dict:
    """Chama GET {ZORDON_URL}/api/search e retorna o payload normalizado.

    Retorno em caso de sucesso::

        {
            "results": [
                {
                    "doc_tipo":   "movimentacao",
                    "numero_cnj": "0000000-00.0000.0.00.0000",
                    "score":      0.87,
                    "snippet":    "texto relevante...",
                },
                ...
            ],
            "erro": None,
        }

    Em caso de falha de rede ou HTTP >= 400 retorna::

        {"results": [], "erro": "<mensagem amigável>"}

    A função nunca propaga exceções — degrada graciosamente.
    """
    base_url = getattr(settings, 'ZORDON_URL', '').rstrip('/')
    api_key = getattr(settings, 'ZORDON_API_KEY', '')

    if not base_url:
        return {'results': [], 'erro': 'ZORDON_URL não configurado'}

    params: dict = {'q': query, 'limit': limit, 'rerank': str(rerank).lower()}
    if cnj:
        params['cnj'] = cnj

    headers = {}
    if api_key:
        headers['Authorization'] = f'Api-Key {api_key}'

    try:
        resp = requests.get(
            f'{base_url}/api/search',
            params=params,
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            'results': data.get('results', []),
            'erro': None,
        }
    except requests.exceptions.ConnectionError:
        logger.warning('zordon: falha de conexão em %s', base_url)
        return {'results': [], 'erro': 'Serviço de busca indisponível (falha de conexão)'}
    except requests.exceptions.Timeout:
        logger.warning('zordon: timeout em %s', base_url)
        return {'results': [], 'erro': 'Serviço de busca não respondeu a tempo'}
    except requests.exceptions.HTTPError as exc:
        logger.warning('zordon: HTTP %s em %s', exc.response.status_code, base_url)
        return {'results': [], 'erro': f'Serviço de busca retornou erro HTTP {exc.response.status_code}'}
    except Exception as exc:  # pragma: no cover — catch-all defensivo
        logger.exception('zordon: erro inesperado: %s', exc)
        return {'results': [], 'erro': 'Erro inesperado ao contatar o serviço de busca'}


def extrair(cnj: str, *, timeout: tuple | None = None) -> dict:
    """Chama GET {ZORDON_URL}/api/extract/<cnj> e retorna os campos estruturados.

    Retorno em caso de sucesso::

        {
            "natureza":             "Precatório",
            "valor_principal":      150000.00,
            "valor_juros_mora":     12000.00,
            "data_oficio":          "2024-03-15",
            "numero_parcelas_rra":  3,
            "fundamento_resumo":    "Benefício previdenciário...",
            "confianca":            0.87,
            "erro":                 None,
        }

    Quando o processo não está indexado no Zordon retorna::

        {"erro": "sem_contexto"}

    Em caso de falha de rede ou HTTP >= 400 retorna::

        {"erro": "<mensagem amigável>"}

    A função nunca propaga exceções — degrada graciosamente.
    """
    base_url = getattr(settings, 'ZORDON_URL', '').rstrip('/')
    api_key = getattr(settings, 'ZORDON_API_KEY', '')

    if not base_url:
        return {'erro': 'ZORDON_URL não configurado'}

    headers = {}
    if api_key:
        headers['Authorization'] = f'Api-Key {api_key}'

    try:
        resp = requests.get(
            f'{base_url}/api/extract/{cnj}',
            headers=headers,
            timeout=timeout or _EXTRACT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        # Propaga "sem_contexto" como sinal especial (processo não indexado)
        if data.get('erro') == 'sem_contexto':
            return {'erro': 'sem_contexto'}
        return {**data, 'erro': None}
    except requests.exceptions.ConnectionError:
        logger.warning('zordon: falha de conexão em %s (extrair %s)', base_url, cnj)
        return {'erro': 'Serviço Zordon indisponível (falha de conexão)'}
    except requests.exceptions.Timeout:
        logger.warning('zordon: timeout em %s (extrair %s)', base_url, cnj)
        return {'erro': 'Serviço Zordon não respondeu a tempo'}
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code
        logger.warning('zordon: HTTP %s em %s (extrair %s)', status, base_url, cnj)
        if status == 404:
            return {'erro': 'sem_contexto'}
        return {'erro': f'Serviço Zordon retornou erro HTTP {status}'}
    except Exception as exc:  # pragma: no cover — catch-all defensivo
        logger.exception('zordon: erro inesperado (extrair %s): %s', cnj, exc)
        return {'erro': 'Erro inesperado ao contatar o serviço Zordon'}


def chunks(cnj: str) -> dict:
    """Chama GET {ZORDON_URL}/api/chunks/<cnj> e retorna os chunks do auto.

    Retorno em caso de sucesso::

        {
            "chunks": [
                {
                    "id":     "abc123",
                    "texto":  "Trecho do documento...",
                    "pagina": 1,
                },
                ...
            ],
            "erro": None,
        }

    Em caso de falha ou processo não indexado retorna::

        {"chunks": [], "erro": "<mensagem amigável>"}

    A função nunca propaga exceções — degrada graciosamente.
    """
    base_url = getattr(settings, 'ZORDON_URL', '').rstrip('/')
    api_key = getattr(settings, 'ZORDON_API_KEY', '')

    if not base_url:
        return {'chunks': [], 'erro': 'ZORDON_URL não configurado'}

    headers = {}
    if api_key:
        headers['Authorization'] = f'Api-Key {api_key}'

    try:
        resp = requests.get(
            f'{base_url}/api/chunks/{cnj}',
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            'chunks': data.get('chunks', []),
            'erro': None,
        }
    except requests.exceptions.ConnectionError:
        logger.warning('zordon: falha de conexão em %s (chunks %s)', base_url, cnj)
        return {'chunks': [], 'erro': 'Serviço Zordon indisponível (falha de conexão)'}
    except requests.exceptions.Timeout:
        logger.warning('zordon: timeout em %s (chunks %s)', base_url, cnj)
        return {'chunks': [], 'erro': 'Serviço Zordon não respondeu a tempo'}
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code
        logger.warning('zordon: HTTP %s em %s (chunks %s)', status, base_url, cnj)
        return {'chunks': [], 'erro': f'Serviço Zordon retornou erro HTTP {status}'}
    except Exception as exc:  # pragma: no cover — catch-all defensivo
        logger.exception('zordon: erro inesperado (chunks %s): %s', cnj, exc)
        return {'chunks': [], 'erro': 'Erro inesperado ao contatar o serviço Zordon'}


# Cache da extração (autos são imutáveis → TTL longo; o warm reescreve).
EXTRACT_CACHE_TTL = 30 * 24 * 3600


def extract_cache_key(cnj: str) -> str:
    return f'zordon_extract:{cnj}'


def listar_processos() -> dict:
    """GET {ZORDON_URL}/api/processos — CNJs no acervo (pré-aquecimento).

    Retorno: {"processos": [{"numero_cnj":..., "chunks":N}], "erro": None}.
    Degrada como as demais: nunca propaga exceção.
    """
    base_url = getattr(settings, 'ZORDON_URL', '').rstrip('/')
    api_key = getattr(settings, 'ZORDON_API_KEY', '')
    if not base_url:
        return {'processos': [], 'erro': 'ZORDON_URL não configurado'}
    headers = {'Authorization': f'Api-Key {api_key}'} if api_key else {}
    try:
        resp = requests.get(f'{base_url}/api/processos', headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        return {'processos': resp.json().get('processos', []), 'erro': None}
    except Exception as exc:  # noqa: BLE001 — degrada graciosamente
        logger.warning('zordon: falha em /api/processos: %s', exc)
        return {'processos': [], 'erro': str(exc)[:120]}
