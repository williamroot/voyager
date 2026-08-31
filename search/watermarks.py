"""Watermark que sobrevive ao Redis — e que sabe a diferença entre nascer e perder.

## O problema, medido em 31/08/2026

`search/sync_incremental.py` guardava as três watermarks só no cache:

    wm = cache.get(_WM_PROC_TS)
    if wm is None:
        cache.set(_WM_PROC_TS, (agora, 0), None)   # ancora em AGORA

Chave ausente significava **primeiro tique da vida do sistema**. Ancorar no topo
nesse caso é certo. Em toda perda posterior é uma amputação: o keyset só anda
para frente, então tudo escrito antes da re-ancoragem nunca mais é revisitado.
Sem erro, sem fila, sem registro — a watermark seguinte fica jovem e saudável.

## A prova de que o gatilho existe (colhida em produção, 31/08/2026)

Redis `192.168.30.100`, lido de dentro do container `voyager-web-1`:

    save ...................... ''            (RDB desligado)
    appendonly ................ no            (AOF desligado)
    maxmemory-policy .......... noeviction
    evicted_keys .............. 0
    uptime_in_seconds ......... 469.336   ⇒ reiniciou em 2026-08-26 06:59:02 UTC
    ttl de sync_es:wm:* ....... -1            (as três, sem expiração)

Eviction e TTL estão descartados por medição. O que sobra é o restart — e o
restart é **fatal por configuração**: sem RDB e sem AOF, subir o Redis é subir
um banco vazio. Não é "pode ter acontecido": aconteceu em 26/08, dentro da
janela entre a última posição conhecida da watermark de `proc_atualizados`
(19/08, medida em 25/08) e os 524.945 documentos com o campo `null` achados em
31/08.

O que NÃO deu para provar: o tamanho do intervalo órfão. O log do scheduler é
só `stdout` do container e o arquivo mais antigo começa em 28/08 00:48 UTC —
dois dias DEPOIS do restart. Não existe sink durável de log. A ausência de
rastro é a própria assinatura do defeito.

## O contrato daqui

`obter()` devolve `(valor, estado)`, e o estado tem três valores que o chamador
é obrigado a tratar de forma diferente:

    'ok'            veio do cache — caminho normal
    'restaurada'    o cache não tinha, o BANCO tinha. Nada se perdeu; é ERRO
                    registrado assim mesmo, porque perder o Redis é incidente.
    'primeiro'      não há linha no banco: é o primeiro tique de verdade.
                    Ancorar no topo aqui é CERTO.
    'perdida'       há linha, mas sem valor legível. Re-ancoragem é inevitável
                    ⇒ ERRO com o intervalo órfão, e a âncora vai para o MENOR
                    não-sincronizado, nunca para `agora`.

Escrever no banco a cada tique custa um `UPDATE` de uma linha a cada 10 min.
Falha de escrita no banco **não** derruba o tique — mas vira ERRO, porque
significa que a próxima queda do Redis volta a ser fatal.
"""
import logging
from datetime import datetime

from django.core.cache import cache
from django.db import transaction
from django.utils.dateparse import parse_datetime

logger = logging.getLogger('voyager.search.sync')

#: `cache.set(..., None)` já é "sem expiração" no backend Redis do Django. O
#: nome existe para o leitor não confundir com o `TIMEOUT = 3600` global de
#: `core/settings.py::CACHES`, que é o default de quem NÃO passa o terceiro
#: argumento — e que teria matado as watermarks em uma hora.
SEM_EXPIRAR = None


def _codificar(valor):
    """Watermark → JSON. Aceita inteiro puro ou o par `(atualizado_em, id)`."""
    if isinstance(valor, (tuple, list)) and len(valor) == 2:
        ts, ident = valor
        return {'t': 'ts_id',
                'ts': ts.isoformat() if isinstance(ts, datetime) else ts,
                'id': int(ident or 0)}
    return {'t': 'int', 'v': int(valor)}


def _decodificar(bruto):
    """JSON → watermark. `None` quando não dá para ler (abster > chutar)."""
    if not isinstance(bruto, dict):
        return None
    if bruto.get('t') == 'int':
        try:
            return int(bruto['v'])
        except (KeyError, TypeError, ValueError):
            return None
    if bruto.get('t') == 'ts_id':
        ts = parse_datetime(bruto.get('ts') or '')
        if ts is None:
            return None
        return (ts, int(bruto.get('id') or 0))
    return None


def obter(chave: str) -> tuple[object | None, str]:
    """`(valor, estado)` — ver o contrato no docstring do módulo."""
    valor = cache.get(chave)
    if valor is not None:
        return valor, 'ok'

    from search.models import Watermark
    try:
        linha = Watermark.objects.filter(chave=chave).first()
    except Exception:
        # Banco mudo não autoriza re-ancorar: quem chama trata 'indisponivel'
        # como "não faço nada neste tique". Nunca como "primeiro tique".
        logger.error('sync_es: não consegui LER a watermark %s do banco. Este '
                     'tique não ancora nada — repetir é barato, amputar o '
                     'passado não tem volta.', chave, exc_info=True)
        return None, 'indisponivel'

    if linha is None:
        return None, 'primeiro'

    valor = _decodificar(linha.valor)
    if valor is None:
        logger.error(
            'sync_es: a watermark %s TEM linha no banco (ancorada em %s) mas o '
            'valor está ilegível (%r). Re-ancoragem é inevitável — ela vai para '
            'o MENOR não-sincronizado, nunca para agora.',
            chave, linha.ancorada_em, linha.valor)
        return None, 'perdida'

    cache.set(chave, valor, SEM_EXPIRAR)
    logger.error(
        'sync_es: a watermark %s SUMIU do Redis e foi RESTAURADA do banco '
        '(valor=%s, gravado em %s). NADA foi perdido — mas o Redis desta casa '
        'roda sem RDB e sem AOF, então isso é incidente, não rotina.',
        chave, valor, linha.atualizada_em)
    return valor, 'restaurada'


def gravar(chave: str, valor) -> bool:
    """Grava nos DOIS lados. Devolve se o lado durável aceitou."""
    cache.set(chave, valor, SEM_EXPIRAR)
    from search.models import Watermark
    try:
        with transaction.atomic():
            Watermark.objects.update_or_create(
                chave=chave, defaults={'valor': _codificar(valor)})
        return True
    except Exception:
        logger.error(
            'sync_es: watermark %s gravada no cache mas NÃO no banco. Enquanto '
            'isso durar, uma queda do Redis volta a apagar o passado.',
            chave, exc_info=True)
        return False


def esquecer(chave: str) -> None:
    """Some com a watermark dos dois lados. Só para teste e para operação."""
    cache.delete(chave)
    from search.models import Watermark
    Watermark.objects.filter(chave=chave).delete()
