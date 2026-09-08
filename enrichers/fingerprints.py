"""Fingerprints anti-bot exigidos por tribunal. Puro: sem Django, sem I/O.

Mora fora do módulo do enricher porque é usado por DOIS caminhos — o
enriquecimento (`enrichers/tjmt.py`) e a busca por parte
(`enrichers/busca/rest.py`) — e porque precisa rodar fora do container, no
validador da matriz. Um algoritmo, um lugar.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

#: Chave do `environment.fingerPrint`, hardcoded no bundle da SPA do TJMT.
CHAVE_TJMT = 'A_mesma_mao_que_aplaude_e_a_que_vaia!'

_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/120.0.0.0 Safari/537.36')
_TELA = '1920x1080'
_LINGUA = 'pt-BR'


def tjmt(ts_ms: int | None = None) -> str:
    """Valor do header `X-Fingerprint` do TJMT (uma string JSON).

    Reproduz a função `Bd()` do bundle Angular:

        mensagem  = "{userAgent}-{screenResolution}-{language}-{timestamp_ms}"
        signature = base64(HMAC_SHA256(mensagem, CHAVE))
        header    = json({signature, timestamp, userAgent, screenResolution, language})

    `ts_ms` é injetável só para teste: em produção usa o relógio atual, porque o
    servidor valida uma janela de timestamp — por isso é gerado por requisição,
    nunca cacheado.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    msg = f'{_UA}-{_TELA}-{_LINGUA}-{ts_ms}'
    assinatura = base64.b64encode(
        hmac.new(CHAVE_TJMT.encode('utf-8'), msg.encode('utf-8'),
                 hashlib.sha256).digest()).decode('ascii')
    # Sem espaços: espelha o `JSON.stringify` do JS.
    return json.dumps({
        'signature': assinatura,
        'timestamp': ts_ms,
        'userAgent': _UA,
        'screenResolution': _TELA,
        'language': _LINGUA,
    }, ensure_ascii=False, separators=(',', ':'))
