"""Resolução de CAPTCHA via CapSolver (compartilhado entre enrichers).

Cobre os tipos que aparecem nas consultas públicas dos tribunais:
- imagem texto (PJe legado, PROJUDI)  → ImageToTextTask
- Cloudflare Turnstile                → AntiTurnstileTaskProxyLess
- reCAPTCHA v2 / v3                    → ReCaptchaV2/V3TaskProxyLess
- hCaptcha                            → HCaptchaTaskProxyLess

Chave em settings.CAPSOLVER_API_KEY (env CAPSOLVER_API_KEY). Padrão portado do
falcon/datamodel/processors (Juriscope). NÃO usa proxy: as tasks *Proxyless do
CapSolver resolvem no lado deles; o token resultante é submetido pelo nosso IP.
"""
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger('voyager.enrichers.captcha')

_CREATE = 'https://api.capsolver.com/createTask'
_RESULT = 'https://api.capsolver.com/getTaskResult'


class CaptchaError(Exception):
    pass


def _api_key() -> str:
    key = getattr(settings, 'CAPSOLVER_API_KEY', '') or ''
    if not key:
        raise CaptchaError('CAPSOLVER_API_KEY não configurada')
    return key


def _run_task(task: dict, *, timeout_total: int = 120, poll: float = 2.0) -> dict:
    """createTask + poll getTaskResult; retorna o dict `solution`."""
    key = _api_key()
    r = requests.post(_CREATE, json={'clientKey': key, 'task': task}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get('errorId', 0) != 0:
        raise CaptchaError(f"createTask: {data.get('errorDescription', data)}")
    # ImageToTextTask resolve SÍNCRONO: a solução já vem no createTask
    # (status 'ready'); não há getTaskResult pra esse tipo.
    if data.get('status') == 'ready' or data.get('solution'):
        return data.get('solution', {}) or {}
    task_id = data.get('taskId')
    if not task_id:
        raise CaptchaError('createTask sem taskId')

    deadline = timeout_total / poll
    n = 0
    while n < deadline:
        n += 1
        time.sleep(poll)
        r = requests.post(_RESULT, json={'clientKey': key, 'taskId': task_id}, timeout=30)
        r.raise_for_status()
        data = r.json()
        status = data.get('status')
        if status == 'ready':
            return data.get('solution', {}) or {}
        if status == 'failed' or data.get('errorId', 0) != 0:
            raise CaptchaError(f"task falhou: {data.get('errorDescription', data)}")
    raise CaptchaError('timeout aguardando getTaskResult')


def solve_image(image_b64: str, *, module: str = 'common', case_sensitive: bool = True) -> str:
    """CAPTCHA de imagem-texto → string reconhecida."""
    sol = _run_task({
        'type': 'ImageToTextTask',
        'body': image_b64,
        'module': module,
        'case': case_sensitive,
    })
    txt = (sol.get('text') or '').strip()
    if not txt:
        raise CaptchaError('ImageToText vazio')
    return txt


def solve_turnstile(website_url: str, site_key: str, *, action: str = '', cdata: str = '') -> str:
    task = {
        'type': 'AntiTurnstileTaskProxyLess',
        'websiteURL': website_url,
        'websiteKey': site_key,
    }
    meta = {}
    if action:
        meta['action'] = action
    if cdata:
        meta['cdata'] = cdata
    if meta:
        task['metadata'] = meta
    sol = _run_task(task)
    tok = sol.get('token') or ''
    if not tok:
        raise CaptchaError('Turnstile sem token')
    return tok


def solve_recaptcha_v2(website_url: str, site_key: str, *, invisible: bool = False) -> str:
    sol = _run_task({
        'type': 'ReCaptchaV2TaskProxyLess',
        'websiteURL': website_url,
        'websiteKey': site_key,
        'isInvisible': invisible,
    })
    tok = sol.get('gRecaptchaResponse') or ''
    if not tok:
        raise CaptchaError('reCAPTCHA v2 sem token')
    return tok


def solve_recaptcha_v3(website_url: str, site_key: str, *, page_action: str = 'verify',
                       min_score: float = 0.7) -> str:
    sol = _run_task({
        'type': 'ReCaptchaV3TaskProxyLess',
        'websiteURL': website_url,
        'websiteKey': site_key,
        'pageAction': page_action,
        'minScore': min_score,
    })
    tok = sol.get('gRecaptchaResponse') or ''
    if not tok:
        raise CaptchaError('reCAPTCHA v3 sem token')
    return tok


def solve_hcaptcha(website_url: str, site_key: str, *, invisible: bool = False) -> str:
    sol = _run_task({
        'type': 'HCaptchaTaskProxyLess',
        'websiteURL': website_url,
        'websiteKey': site_key,
        'isInvisible': invisible,
    })
    tok = sol.get('gRecaptchaResponse') or sol.get('token') or ''
    if not tok:
        raise CaptchaError('hCaptcha sem token')
    return tok


def balance() -> float:
    r = requests.post('https://api.capsolver.com/getBalance',
                      json={'clientKey': _api_key()}, timeout=30)
    r.raise_for_status()
    return float(r.json().get('balance', 0.0))
