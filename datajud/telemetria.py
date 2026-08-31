"""Telemetria ao vivo da varredura do Datajud.

POR QUE ISSO EXISTE
-------------------
A puxada nacional é um job de **horas** por tribunal. Até aqui, o único sinal
que ela dava enquanto rodava era `Tribunal.datajud_varredura_status`, uma
string de 100 caracteres escrita a cada 20 páginas, e o `logger.info` do fim.
Quem está operando não conseguia responder, sem abrir o log de um container
específico, as cinco perguntas que importam:

    quantas requisições já foram · quantos docs entraram · a que ritmo ·
    quanto falta · o que está dando errado

Sem isso a varredura é uma caixa preta de 20 horas — e caixa preta é
exatamente onde os três buracos do `CLAUDE.md` moraram: run verde, log limpo,
número redondo.

CONTRATOS
---------
1. **Escrever telemetria nunca derruba a varredura.** Todo acesso ao cache é
   protegido: se o Redis cair, a puxada continua e o operador perde a tela, não
   o dado. O contrário seria trocar o produto pelo painel.
2. **ETA se abstém.** Só existe ETA quando o `alvo` foi medido dos dois lados
   (declarado pelo CNJ − o que já temos no índice). Numa passada incremental
   não há alvo provável, e o campo fica vazio em vez de inventar um número —
   regra nº 6 do `CLAUDE.md`.
3. **Erro é contado por TIPO, não agregado.** `rate-limit` 400 vezes e
   `Fielddata is disabled` 1 vez são diagnósticos opostos; um contador único
   esconderia o segundo atrás do primeiro.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from django.core.cache import cache

logger = logging.getLogger('voyager.datajud.telemetria')

PREFIXO = 'varredura:tel:'

#: 7 dias: a telemetria tem que sobreviver ao fim do job para que o operador
#: leia o resultado da noite anterior sem depender de log rotacionado.
TTL = 7 * 24 * 3600


def _chave(sigla: str) -> str:
    return f'{PREFIXO}{sigla.upper()}'


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def ler(sigla: str) -> dict:
    try:
        return cache.get(_chave(sigla)) or {}
    except Exception:                                   # noqa: BLE001
        return {}


def _gravar(sigla: str, estado: dict) -> None:
    try:
        cache.set(_chave(sigla), estado, timeout=TTL)
    except Exception as exc:                            # noqa: BLE001
        logger.warning('telemetria %s indisponível: %s', sigla, str(exc)[:120])


def abrir(sigla: str, *, alvo: int | None = None, declarado: int | None = None,
          cursor: int | None = None, filtrada: bool = False) -> dict:
    """Zera os contadores da passada e registra o alvo, quando ele foi medido.

    `alvo` é o número que dá sentido ao ETA: quantos docs esta passada deveria
    trazer, medido DOS DOIS LADOS (declarado ao CNJ menos o que o índice já
    tem). `None` = não foi medido, e então não há ETA — a tela mostra `—`.
    """
    estado = {
        'tribunal': sigla.upper(),
        'estado': 'rodando',
        'inicio_em': _agora(),
        'atualizado_em': _agora(),
        'requisicoes': 0, 'paginas': 0, 'lidos': 0, 'gravados': 0,
        'perdidos': 0, 'esperas': 0, 'bytes': 0,
        'bytes_por_doc': None, 'pagina_atual': None,
        'cursor': cursor, 'cursor_iso': _cursor_iso(cursor),
        'declarado': declarado, 'alvo': alvo, 'restante': alvo,
        'docs_por_s': 0.0, 'eta_s': None,
        'erros': {}, 'parou_por': None, 'filtrada': filtrada,
    }
    _gravar(sigla, estado)
    return estado


def _cursor_iso(cursor: int | None) -> str | None:
    """Epoch-ms → ISO. O cursor é `@timestamp`, e ler `1755000000000` na tela
    não diz a ninguém em que ponto do tempo a varredura está."""
    if not cursor:
        return None
    try:
        return datetime.fromtimestamp(cursor / 1000, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def registrar_pagina(sigla: str, *, requisicoes: int, paginas: int, lidos: int,
                     gravados: int, perdidos: int, esperas: int, bytes_lidos: int,
                     bytes_por_doc: float | None, pagina_atual: int | None,
                     cursor: int | None, decorrido: float) -> None:
    """Atualiza o estado depois de cada página. ~1 escrita a cada 10 s."""
    estado = ler(sigla)
    if not estado:
        estado = abrir(sigla, cursor=cursor)
    estado.update({
        'atualizado_em': _agora(),
        'requisicoes': requisicoes, 'paginas': paginas, 'lidos': lidos,
        'gravados': gravados, 'perdidos': perdidos, 'esperas': esperas,
        'bytes': bytes_lidos, 'bytes_por_doc': bytes_por_doc,
        'pagina_atual': pagina_atual,
        'cursor': cursor, 'cursor_iso': _cursor_iso(cursor),
        'docs_por_s': round(lidos / decorrido, 1) if decorrido else 0.0,
    })
    alvo = estado.get('alvo')
    if alvo:
        restante = max(0, alvo - gravados)
        estado['restante'] = restante
        ritmo = estado['docs_por_s']
        estado['eta_s'] = round(restante / ritmo) if ritmo else None
    else:
        # sem alvo medido não há ETA — abster, nunca chutar (regra nº 6)
        estado['restante'] = None
        estado['eta_s'] = None
    _gravar(sigla, estado)


def registrar_erro(sigla: str, tipo: str, detalhe: str = '') -> None:
    """Conta um erro POR TIPO e guarda o último detalhe daquele tipo."""
    estado = ler(sigla) or abrir(sigla)
    erros = dict(estado.get('erros') or {})
    erros[tipo] = int(erros.get(tipo, 0)) + 1
    estado['erros'] = erros
    estado['atualizado_em'] = _agora()
    if detalhe:
        estado['ultimo_erro'] = f'{tipo}: {detalhe[:160]}'
    _gravar(sigla, estado)


def fechar(sigla: str, resumo: dict, estado_final: str | None = None) -> None:
    """Carimba o fim da passada com o resumo que a varredura devolveu."""
    estado = ler(sigla) or abrir(sigla)
    estado.update({
        'atualizado_em': _agora(),
        'estado': estado_final or resumo.get('parou_por') or 'fim',
        'parou_por': resumo.get('parou_por'),
        'paginas': resumo.get('paginas', estado.get('paginas')),
        'lidos': resumo.get('lidos', estado.get('lidos')),
        'gravados': resumo.get('gravados', estado.get('gravados')),
        'perdidos': resumo.get('perdidos', estado.get('perdidos')),
        'esperas': resumo.get('esperas', estado.get('esperas')),
        'requisicoes': resumo.get('requisicoes', estado.get('requisicoes')),
        'cursor': resumo.get('cursor', estado.get('cursor')),
        'cursor_iso': _cursor_iso(resumo.get('cursor', estado.get('cursor'))),
        'segundos': resumo.get('segundos'),
        'docs_por_s': resumo.get('docs_por_s', estado.get('docs_por_s')),
        'restante_declarado': resumo.get('restante_declarado'),
    })
    _gravar(sigla, estado)


def snapshot(siglas) -> list[dict]:
    """Estado de várias siglas de uma vez (uma ida ao Redis, não N)."""
    siglas = [s.upper() for s in siglas]
    try:
        achados = cache.get_many([_chave(s) for s in siglas]) or {}
    except Exception:                                   # noqa: BLE001
        achados = {}
    saida = []
    for s in siglas:
        est = achados.get(_chave(s))
        if est:
            saida.append(est)
    return saida


def limpar(sigla: str) -> None:
    try:
        cache.delete(_chave(sigla))
    except Exception:                                   # noqa: BLE001
        pass
