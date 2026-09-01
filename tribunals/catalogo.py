"""Resolve um código TPU no catálogo — e ABSTÉM quando o catálogo não o tem.

Por que existe (#104, medido em produção)
-----------------------------------------
`classe_codigo`/`assunto_codigo` (string, compatibilidade) e
`classe_id`/`assunto_id` (FK do catálogo TPU) são o MESMO fato em dois
formatos. Quem grava só a string deixa a FK NULL, e o buraco se realimenta:

    31/08 22:05 UTC  classe_id IS NULL ....... 21     (fim do backfill)
    01/09 16:01 UTC  classe_id IS NULL .... 8.072     (18 h depois, sem nada
                                                       ter sido apagado)

Não era resíduo de varredura: 97,6% eram linhas ANTIGAS reescritas ao vivo, e
**0 códigos** estavam fora do catálogo — o escritor tinha com o que fechar a FK
e não fechava. O conserto é na ORIGEM (`datajud/ingestion.py`,
`datajud/hidratacao.py`), não num tique de reparo: o tique por
`atualizado_em` não restringe nada enquanto houver backfill em massa na tabela
(ver `djen/scheduler.py`).

Por que LOOKUP e não upsert
---------------------------
Este catálogo é NACIONAL e é destino de FK. A primeira corrida do
`repop_classe_assunto` criou `99999999` (TJSP) nele em menos de dois minutos
quando podia criar à vontade. Aqui não se cria nada: código que o catálogo não
conhece deixa a FK NULL, com log — regra nº 6 do `CLAUDE.md` (abster > chutar).
Quem semeia catálogo é o enricher (`upsert_catalogo`, tem o nome canônico do
PJe) e o `repop_classe_assunto --criar-catalogo`, que tem a guarda da TPU.

Custo
-----
O catálogo inteiro cabe na memória do worker: **662 classes e 4.295 assuntos**
(medido em 01/09/2026). Um `SELECT codigo` a cada 5 min por processo, e depois
disso a resolução é um `in` de set. O miss não vira varredura: uma sonda por PK
(o código É a PK) e o resultado — positivo ou negativo — fica memorizado até o
fim da janela, para que um código órfão recorrente (o `99999999` do TJSP
aparece aos milhares) não gere uma consulta por linha.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger('voyager.tribunals.catalogo')

#: janela do cache. Curta o bastante para enxergar código semeado por outro
#: escritor (enricher/backfill) sem precisar de deploy; longa o bastante para
#: o `SELECT` do catálogo não aparecer no perfil.
TTL_S = 300

_CACHE: dict[str, dict] = {}


def _modelo(qual: str):
    from tribunals.models import Assunto, ClasseJudicial
    try:
        return {'classe': ClasseJudicial, 'assunto': Assunto}[qual]
    except KeyError:
        raise ValueError(f'catálogo desconhecido: {qual!r}') from None


def _estado(qual: str) -> dict:
    st = _CACHE.get(qual)
    if st is None or time.monotonic() >= st['expira']:
        st = {
            'expira': time.monotonic() + TTL_S,
            'tem': set(_modelo(qual).objects.values_list('codigo', flat=True)),
            'nao_tem': set(),
        }
        _CACHE[qual] = st
    return st


def limpar_cache() -> None:
    """Descarta o cache (testes, e o shell quando alguém acabou de semear)."""
    _CACHE.clear()


def resolver(qual: str, codigo) -> str | None:
    """Devolve o código se ele existe no catálogo `qual`; senão, `None`.

    `None` é ABSTENÇÃO declarada, não erro: a FK fica NULL e a linha entra na
    contagem de órfãos do `repop_classe_assunto`, que sabe criar catálogo com
    a guarda da TPU. Inventar o vínculo aqui quebraria o ORM em produção — a
    constraint não existe no banco, então `proc.classe` levantaria
    `DoesNotExist` na tela, não no INSERT.
    """
    cod = str(codigo or '').strip()
    if not cod:
        return None
    st = _estado(qual)
    if cod in st['tem']:
        return cod
    if cod in st['nao_tem']:
        return None
    # cache pode estar velho: uma sonda pela PK antes de abster
    if _modelo(qual).objects.filter(codigo=cod).exists():
        st['tem'].add(cod)
        return cod
    st['nao_tem'].add(cod)
    logger.warning('catálogo de %s não tem o código %r — FK fica NULL '
                   '(abster > chutar; ver repop_classe_assunto --criar-catalogo)',
                   qual, cod)
    return None
