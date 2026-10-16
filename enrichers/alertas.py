"""Alerta operacional dos enrichers: log ERROR + Sentry + Slack, best-effort.

Os três canais que o Voyager já tem (`core/settings.py`): o log em ERROR (que
o Sentry captura quando `SENTRY_DSN` está ligado) e o webhook do Slack
(`SLACK_WEBHOOK_URL`, o mesmo dos lotes de validação). Nenhum deles pode
derrubar o job: falha de alerta vira WARNING e segue.

Quem chama decide a frequência — `armar_respiro` só alerta quando é ELE quem
arma (SET NX), então uma queda com 40 réplicas gera um alerta, não 40.
"""
from __future__ import annotations

import logging

logger = logging.getLogger('voyager.enrichers.alertas')


def alertar(titulo: str, detalhe: str) -> dict:
    from django.conf import settings

    out = {'sentry': False, 'slack': False}
    logger.error('%s — %s', titulo, detalhe)
    try:
        import sentry_sdk
        if sentry_sdk.Hub.current.client is not None:
            sentry_sdk.capture_message(f'{titulo} — {detalhe}', level='error')
            out['sentry'] = True
    except Exception:
        logger.warning('alerta: sentry falhou', exc_info=True)
    webhook = getattr(settings, 'SLACK_WEBHOOK_URL', '') or ''
    if webhook:
        try:
            import requests
            requests.post(webhook, json={'text': f'Voyager: {titulo}\n{detalhe}'}, timeout=5)
            out['slack'] = True
        except Exception:
            logger.warning('alerta: slack falhou', exc_info=True)
    return out
