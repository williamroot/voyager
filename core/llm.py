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
    if not msg:
        return None
    # content é a resposta; se vier vazio (reasoning-model despejou tudo no
    # raciocínio), cai pro reasoning como último recurso — melhor prosa útil que vazio.
    return msg.get('content') or msg.get('reasoning') or msg.get('reasoning_content') or None


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


def chat_stream(messages: list[dict], *, max_tokens: int = 9000, temperature: float = 0.3,
                model: str | None = None, timeout: float = 240.0):
    """Gera chunks {type, text} do stream OpenAI da Ollama. type ∈ {'reasoning','content'}.
    Não levanta pro caller: em erro/indisponível, apenas encerra o gerador."""
    import json
    api_key = getattr(settings, 'OLLAMA_API_KEY', '')
    if not api_key:
        return
    base_url = getattr(settings, 'OLLAMA_BASE_URL', 'https://ollama.com/v1').rstrip('/')
    payload = {
        'model': model or getattr(settings, 'OLLAMA_MODEL', 'kimi-k2.6'),
        'messages': messages, 'max_tokens': max(max_tokens, _MIN_TOKENS),
        'temperature': temperature, 'stream': True,
        'reasoning_effort': getattr(settings, 'OLLAMA_REASONING_EFFORT', 'low'),
    }
    try:
        with requests.post(f'{base_url}/chat/completions',
                           headers={'Authorization': f'Bearer {api_key}'},
                           json=payload, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            resp.encoding = 'utf-8'  # requests default seria ISO-8859-1 → mojibake
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data:'):
                    continue
                data = line[len('data:'):].strip()
                if data == '[DONE]':
                    return
                try:
                    obj = json.loads(data)
                except (ValueError, TypeError):
                    continue
                delta = (obj.get('choices') or [{}])[0].get('delta') or {}
                rz = delta.get('reasoning') or delta.get('reasoning_content')
                if rz:
                    yield {'type': 'reasoning', 'text': rz}
                if delta.get('content'):
                    yield {'type': 'content', 'text': delta['content']}
    except Exception as exc:  # noqa: BLE001
        logger.warning('llm.chat_stream: %s', exc)
        return


def chat_agent_stream(messages: list[dict], *, tools_specs: list, dispatch,
                      max_rounds: int = 8, max_tokens: int = 9000,
                      temperature: float = 0.3, timeout: float = 240.0):
    """Loop de agente com tool-calling E streaming, multi-turno (chat de jurimetria).

    `messages` é o histórico completo (system + turnos user/assistant). A cada rodada
    faz POST stream=True com `tools`; os deltas de texto saem na hora e os
    `delta.tool_calls` (que chegam FRAGMENTADOS por index — id/name numa parte,
    arguments em pedaços) são acumulados. Rodada com tool_calls → executa via
    `dispatch(name, args)` e volta ao loop; sem tool_calls → resposta final.

    Yields (nunca levanta; em falha emite 'error' e encerra):
      {'type': 'reasoning', 'text': ...}
      {'type': 'content',   'text': ...}                    # token-a-token
      {'type': 'tool_call', 'name': ..., 'args': {...}}
      {'type': 'tool_result', 'name': ..., 'ok': bool, 'resumo': str}
      {'type': 'done',  'content': texto_final}
      {'type': 'error', 'code': 'llm_indisponivel'|'llm_falha', 'text': ...}
    """
    import json
    if not disponivel():
        yield {'type': 'error', 'code': 'llm_indisponivel',
               'text': 'LLM não configurado (OLLAMA_API_KEY ausente).'}
        return
    api_key = getattr(settings, 'OLLAMA_API_KEY', '')
    base_url = getattr(settings, 'OLLAMA_BASE_URL', 'https://ollama.com/v1').rstrip('/')
    msgs = [dict(m) for m in messages]  # cópia local — o caller persiste só user/assistant

    for rodada in range(max_rounds):
        ultima = rodada == max_rounds - 1
        payload = {
            'model': getattr(settings, 'OLLAMA_MODEL', 'kimi-k2.6'),
            'messages': msgs, 'max_tokens': max(max_tokens, _MIN_TOKENS),
            'temperature': temperature, 'stream': True,
            'reasoning_effort': getattr(settings, 'OLLAMA_REASONING_EFFORT', 'low'),
        }
        if tools_specs and not ultima:  # última rodada fecha sem tools (força conclusão)
            payload['tools'] = tools_specs
        else:
            msgs = msgs + [{'role': 'user', 'content':
                            'Conclua AGORA a resposta final ao usuário com o que já reuniu. '
                            'Não peça mais dados.'}]
            payload['messages'] = msgs
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # tool_calls fragmentados: index -> {'id', 'function': {'name', 'arguments'}}
        tc_acc: dict[int, dict] = {}
        try:
            with requests.post(f'{base_url}/chat/completions',
                               headers={'Authorization': f'Bearer {api_key}'},
                               json=payload, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                resp.encoding = 'utf-8'  # requests default seria ISO-8859-1 → mojibake
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith('data:'):
                        continue
                    data = line[len('data:'):].strip()
                    if data == '[DONE]':
                        break
                    try:
                        obj = json.loads(data)
                    except (ValueError, TypeError):
                        continue
                    delta = (obj.get('choices') or [{}])[0].get('delta') or {}
                    rz = delta.get('reasoning') or delta.get('reasoning_content')
                    if rz:
                        reasoning_parts.append(rz)
                        yield {'type': 'reasoning', 'text': rz}
                    if delta.get('content'):
                        content_parts.append(delta['content'])
                        yield {'type': 'content', 'text': delta['content']}
                    for frag in delta.get('tool_calls') or []:
                        idx = frag.get('index', 0)
                        acc = tc_acc.setdefault(idx, {'id': '', 'function': {'name': '', 'arguments': ''}})
                        if frag.get('id'):
                            acc['id'] = frag['id']
                        fn = frag.get('function') or {}
                        if fn.get('name'):
                            acc['function']['name'] += fn['name']
                        if fn.get('arguments'):
                            acc['function']['arguments'] += fn['arguments']
        except Exception as exc:  # noqa: BLE001
            logger.warning('llm.chat_agent_stream: %s', exc)
            yield {'type': 'error', 'code': 'llm_falha',
                   'text': 'Falha ao consultar o modelo. Tente novamente.'}
            return

        content = ''.join(content_parts)
        if not tc_acc:  # sem tools nesta rodada → resposta final
            # reasoning-model pode despejar tudo no canal de raciocínio e deixar
            # content vazio — melhor prosa útil que turno perdido (mesmo fallback
            # do chat()). O texto já foi mostrado ao usuário como 'reasoning'.
            yield {'type': 'done', 'content': content or ''.join(reasoning_parts)}
            return

        # ecoa a mensagem do assistente (com os tool_calls) e responde cada uma
        tool_calls = [{'id': acc['id'] or f'call_{i}', 'type': 'function',
                       'function': acc['function']} for i, acc in sorted(tc_acc.items())]
        msgs.append({'role': 'assistant', 'content': content, 'tool_calls': tool_calls})
        for tc in tool_calls:
            name = tc['function'].get('name') or ''
            try:
                args = json.loads(tc['function'].get('arguments') or '{}')
            except (ValueError, TypeError):
                args = {}
            yield {'type': 'tool_call', 'name': name, 'args': args}
            resultado = dispatch(name, args)
            ok = not (isinstance(resultado, dict) and resultado.get('erro'))
            resumo = ''
            if isinstance(resultado, dict):
                resumo = str(resultado.get('erro') or '')[:160] if not ok else \
                    ', '.join(f'{k}' for k in list(resultado)[:6])
            yield {'type': 'tool_result', 'name': name, 'ok': ok, 'resumo': resumo}
            msgs.append({'role': 'tool', 'tool_call_id': tc['id'],
                         'content': json.dumps(resultado, ensure_ascii=False, default=str)[:8000]})

    # max_rounds esgotado com a última rodada ainda pedindo tools (não deveria — a
    # última vai sem specs), mas por segurança fecha com o que acumulou
    yield {'type': 'done', 'content': ''}


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
