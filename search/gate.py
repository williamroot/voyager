"""Primitivas do gate de índice — as mesmas para TODA porta do acervo.

Este módulo não tem lógica de porta nenhuma: ele só sabe responder duas
perguntas caras, e responder do jeito barato.

  1. "destes N pks, quais NÃO estão no índice?"  → `ausentes_no_bloco`
  2. "leve estes pks ao índice"                  → `enfileirar_movs` /
                                                    `enfileirar_processos`

Existe porque a mesma doença já foi curada duas vezes em lugares diferentes —
`diarios/indice.py` (21/08/2026, 27.619 linhas coletadas e não buscáveis) e
`datajud/indice.py` (24/08/2026, 100% das linhas escritas nos últimos 5 minutos
fora do índice e 0 de 500 processos com o doc em dia) — e a terceira cópia
dessas 30 linhas seria a que envelhece sozinha.

Os dois números que ditam os tetos aqui foram medidos, não estimados:

  · `terms` sobre o campo `id` (que é `long` e indexado): 220.544 ids em 23
    perguntas custaram **1 s**. O default de `index.max_terms_count` do ES é
    65.536, então 10.000 por pergunta fica com folga larga.
  · `_mget` é realtime GET por `_id` e devolve a lista EXATA de ausentes — o
    `terms` só devolve o total. Por isso a ordem é grossa→fina: o bloco que
    bate não paga o `_mget`.
"""
import logging

logger = logging.getLogger('voyager.search.gate')

#: Teto de espera do lado do ES. Régua NUNCA segura escrita (regra nº 7 do
#: CLAUDE.md): uma medição de rodapé sem `request_timeout` já derrubou o site.
ES_TIMEOUT = 60

#: Quantos ids por pergunta grossa (`terms` count).
BLOCO_TERMS = 10_000
#: Bloco fino do `_mget`, usado só quando um bloco grosso não bate.
BLOCO_MGET = 1_000
#: Tamanho do lote enfileirado — o mesmo que `search.jobs.indexar_*_bulk`
#: consome num `_bulk`. Acima disso o próprio job já fecha por BYTES (20 MB).
CHUNK_ENFILEIRA = 500


def _es():
    from search.client import get_es
    return get_es()


def indice_movs() -> str:
    from search.client import index_name
    return index_name('movimentacoes')


def indice_processos() -> str:
    from search.client import index_name
    return index_name('processos')


def ausentes_no_bloco(ids: list[int], indice: str | None = None) -> list[int]:
    """Quais destes ids NÃO estão no índice. Duas perguntas, da grossa pra fina.

    Primeiro um `terms` count (1 requisição por 10.000 ids): se bater, o bloco
    inteiro está lá e não se paga mais nada. Só o bloco que NÃO bate desce pro
    `_mget`, que devolve o `found` de cada id. É a diferença entre 23 perguntas
    e 221 para um dia de 220 mil linhas.

    Propaga a exceção do ES de propósito: quem chama tem que saber a diferença
    entre "nenhum falta" e "não deu pra perguntar" (regra nº 6 — abster, nunca
    chutar 0).
    """
    if not ids:
        return []
    es = _es()
    idx = indice or indice_movs()
    n = es.count(index=idx, query={'terms': {'id': ids}},
                 request_timeout=ES_TIMEOUT)['count']
    if n == len(ids):
        return []
    faltam: list[int] = []
    for i in range(0, len(ids), BLOCO_MGET):
        fino = ids[i:i + BLOCO_MGET]
        r = es.mget(index=idx, ids=[str(x) for x in fino], source=False,
                    request_timeout=ES_TIMEOUT)
        faltam.extend(int(d['_id']) for d in r['docs'] if not d.get('found'))
    return faltam


def _enfileirar(job: str, pks: list[int], chunk: int = CHUNK_ENFILEIRA) -> int:
    """Enfileira `job` na fila `es_index`, em lotes. PROPAGA erro de propósito.

    Propagar é a lição inteira: até 21/08/2026 o enqueue do poller engolia a
    exceção e devolvia 0, e quem chamava avançava o watermark assim mesmo. Um
    soluço do Redis apagava do índice, PARA SEMPRE, a leva inteira daquele tick
    — com um WARNING no log, que é o pior formato de perda.
    """
    if not pks:
        return 0
    import django_rq
    fila = django_rq.get_queue('es_index')
    for i in range(0, len(pks), chunk):
        fila.enqueue(job, pks[i:i + chunk])
    return len(pks)


def enfileirar_movs(pks: list[int], chunk: int = CHUNK_ENFILEIRA) -> int:
    """Leva N movimentações ao índice, em lote. Ver `_enfileirar`."""
    return _enfileirar('search.jobs.indexar_movimentacoes_bulk', pks, chunk)


def enfileirar_processos(pks: list[int], chunk: int = CHUNK_ENFILEIRA) -> int:
    """Leva N processos ao índice, em lote. Ver `_enfileirar`."""
    return _enfileirar('search.jobs.indexar_processos_bulk', pks, chunk)
