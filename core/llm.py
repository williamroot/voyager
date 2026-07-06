"""Cliente Ollama (OpenAI-compatível) — espelha o padrão do Horizon/smart-mail.

Fala via httpx (sem SDK). Fail-closed: sem OLLAMA_API_KEY → devolve None e o caller
degrada (a tela nunca quebra). Usado pela narrativa de jurimetria: o LLM LÊ dados
determinísticos nossos e NARRA — nunca calcula.
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_MIN_TOKENS = 2048  # piso p/ reasoning models (gpt-oss/kimi)


def disponivel() -> bool:
    return bool(getattr(settings, 'OLLAMA_API_KEY', ''))


def chat(messages: list[dict], *, max_tokens: int = 4096,
         temperature: float | None = 0.3, model: str | None = None,
         reasoning_effort: str | None = None, timeout: float = 180.0) -> str | None:
    """Chama o endpoint OpenAI-compat da Ollama. Devolve o texto da resposta
    (choices[0].message.content) ou None se indisponível/falha. Retry em 5xx/timeout."""
    api_key = getattr(settings, 'OLLAMA_API_KEY', '')
    if not api_key:
        logger.info('llm.chat: OLLAMA_API_KEY ausente — narrativa desativada')
        return None
    base_url = getattr(settings, 'OLLAMA_BASE_URL', 'https://ollama.com/v1').rstrip('/')
    payload: dict = {
        'model': model or getattr(settings, 'OLLAMA_MODEL', 'kimi-k2.6'),
        'messages': messages,
        'max_tokens': max(max_tokens, _MIN_TOKENS),
        'reasoning_effort': reasoning_effort or getattr(settings, 'OLLAMA_REASONING_EFFORT', 'low'),
    }
    if temperature is not None:
        payload['temperature'] = temperature
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                f'{base_url}/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json=payload, timeout=timeout)
            if resp.status_code >= 500:
                last_exc = RuntimeError(f'HTTP {resp.status_code}')
            else:
                if resp.status_code >= 400:
                    logger.warning('llm.chat: HTTP %s — %s', resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                return (data.get('choices') or [{}])[0].get('message', {}).get('content') or None
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        if attempt < 2:
            time.sleep(1.0 * (attempt + 1))
    logger.warning('llm.chat: falha após retries — %s', last_exc)
    return None
