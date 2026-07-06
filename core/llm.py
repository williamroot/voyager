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
    payload: dict = {'messages': messages}
    if model:
        payload['model'] = model
    if temperature is not None:
        payload['temperature'] = temperature
    if reasoning_effort:
        payload['reasoning_effort'] = reasoning_effort
    msg = _post(payload, max_tokens=max_tokens, timeout=timeout)
    return (msg or {}).get('content') or None


def _post(payload: dict, *, max_tokens: int, tools: list | None = None,
          timeout: float = 180.0) -> dict | None:
    """POST cru no endpoint OpenAI-compat. Devolve o dict `message` (com content e/ou
    tool_calls) ou None. Retry em 5xx/timeout; 4xx não é retried."""
    api_key = getattr(settings, 'OLLAMA_API_KEY', '')
    if not api_key:
        logger.info('llm._post: OLLAMA_API_KEY ausente — LLM desativado')
        return None
    base_url = getattr(settings, 'OLLAMA_BASE_URL', 'https://ollama.com/v1').rstrip('/')
    payload = dict(payload)
    payload.setdefault('model', getattr(settings, 'OLLAMA_MODEL', 'kimi-k2.6'))
    payload.setdefault('reasoning_effort', getattr(settings, 'OLLAMA_REASONING_EFFORT', 'low'))
    payload['max_tokens'] = max(max_tokens, _MIN_TOKENS)
    if tools:
        payload['tools'] = tools
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
                    logger.warning('llm._post: HTTP %s — %s', resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                return (data.get('choices') or [{}])[0].get('message') or {}
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        if attempt < 2:
            time.sleep(1.0 * (attempt + 1))
    logger.warning('llm._post: falha após retries — %s', last_exc)
    return None


def chat_agent(system: str, user: str, *, tools_specs: list, dispatch, on_step=None,
               max_rounds: int = 8, max_tokens: int = 9000, temperature: float = 0.3,
               timeout: float = 240.0) -> str | None:
    """Loop de agente com tool-calling (OpenAI/Ollama). O modelo pede tools, a gente
    executa via `dispatch(name, args)->dict` e devolve o resultado, até ele produzir a
    resposta final (sem tool_calls). `on_step(name, args)` é chamado a cada tool (p/
    telemetria/UI). Fail-closed: None se LLM indisponível."""
    import json
    if not disponivel():
        return None
    messages: list[dict] = [{'role': 'system', 'content': system},
                            {'role': 'user', 'content': user}]
    for _ in range(max_rounds):
        msg = _post({'messages': messages, 'temperature': temperature},
                    max_tokens=max_tokens, tools=tools_specs, timeout=timeout)
        if msg is None:
            return None
        tool_calls = msg.get('tool_calls') or []
        if not tool_calls:
            return msg.get('content') or None
        # ecoa a mensagem do assistente (com os tool_calls) e responde cada uma
        messages.append({'role': 'assistant', 'content': msg.get('content') or '',
                         'tool_calls': tool_calls})
        for tc in tool_calls:
            fn = (tc.get('function') or {})
            name = fn.get('name') or ''
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except (ValueError, TypeError):
                args = {}
            if on_step:
                try:
                    on_step(name, args)
                except Exception:  # noqa: BLE001
                    pass
            resultado = dispatch(name, args)
            messages.append({'role': 'tool', 'tool_call_id': tc.get('id') or name,
                             'content': json.dumps(resultado, ensure_ascii=False, default=str)[:8000]})
    # esgotou as rodadas — pede o fechamento sem tools
    msg = _post({'messages': messages + [{'role': 'user',
                 'content': 'Conclua a análise agora com o que já reuniu.'}],
                 'temperature': temperature}, max_tokens=max_tokens, timeout=timeout)
    return (msg or {}).get('content') or None
