"""Cliente Elasticsearch singleton (lazy)."""
from django.conf import settings
from elasticsearch import Elasticsearch

_es = None


def get_es() -> Elasticsearch:
    global _es
    if _es is None:
        _es = Elasticsearch(
            settings.ELASTICSEARCH_URL,
            request_timeout=settings.ES_TIMEOUT,
            verify_certs=False,
        )
    return _es


def index_name(suffix: str) -> str:
    return f'{settings.ELASTICSEARCH_INDEX_PREFIX}-{suffix}'


def indices_espelho(suffix: str) -> list[str]:
    """Índices que devem receber a MESMA escrita — o de produção e o espelho.

    Existe pelo tempo de uma migração de índice: enquanto o novo é preenchido
    por cópia, tudo que a ingestão escreve no antigo precisa cair nos dois, ou o
    cutover perde a janela inteira da migração (horas de publicação). Uma cópia
    seguida de "catch-up por id" não resolve sozinha: UPDATE em doc antigo
    (enriquecimento reescrevendo movs de um processo) não tem id novo.

    Vazio ⇒ nada muda. Ligar com `ES_INDICE_ESPELHO='movimentacoes:movimentacoes-v2'`.
    """
    espelhos = getattr(settings, 'ES_INDICE_ESPELHO', '') or ''
    saida = [index_name(suffix)]
    for par in espelhos.split(','):
        if ':' not in par:
            continue
        origem, destino = (x.strip() for x in par.split(':', 1))
        if origem == suffix and destino:
            saida.append(index_name(destino))
    return saida