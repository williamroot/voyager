"""Serving do modelo de sobrevivência DIREITO_CREDITÓRIO→precatório.

Lê o artefato leve (Kaplan-Meier estratificado, `data/surv_strata.json`) e prevê,
pra um processo DIREITO_CREDITORIO, a chance de virar precatório em 12/24m e o
tempo mediano. Sem lib de ML no serving — só lookup por estrato {ente_tipo|natureza},
com fallback {ente_tipo|*} → _overall. Determinístico e auditável (traz n/eventos).
"""
from __future__ import annotations

import functools
import json
import os

from django.conf import settings


@functools.lru_cache(maxsize=2)
def _load(nome: str) -> dict:
    path = os.path.join(settings.BASE_DIR, 'dashboard', 'data', nome)
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — sem artefato = feature off
        return {}


def _strata() -> dict:
    return _load('surv_strata.json')


def ente_tipo(tribunal: str | None, ente_nome: str | None) -> str:
    t = (tribunal or '').upper()
    nome = (ente_nome or '').upper()
    if t.startswith('TRF') or t.startswith('JF'):
        return 'federal'
    if 'MUNIC' in nome or 'PREFEIT' in nome:
        return 'municipal'
    if t.startswith('TJ'):
        return 'estadual'
    return 'outro'


def _meses(dias) -> int | None:
    return round(dias / 30.44) if dias else None


def prever(tribunal: str | None, natureza: str | None, ente_nome: str | None = None) -> dict | None:
    """Predição de sobrevivência DC→precatório. None se o artefato não existe."""
    s = _strata()
    if not s:
        return None
    et = ente_tipo(tribunal, ente_nome)
    nat = (natureza or 'DESCONHECIDA').upper() or 'DESCONHECIDA'
    return _lookup(s, et, nat)


def _lookup(s: dict, et: str, nat: str) -> dict | None:
    for key in (f'{et}|{nat}', f'{et}|*', '_overall'):
        st = s.get(key)
        if st:
            out = {'estrato': key, 'ente_tipo': et, 'n': st.get('n'), 'eventos': st.get('eventos'),
                   'tempo_mediano_meses': _meses(st.get('tempo_mediano_dias'))}
            for h in ('12m', '24m', '36m', '60m'):
                if f'chance_{h}' in st:
                    out[f'chance_{h}'] = st[f'chance_{h}']
            return out
    return None
