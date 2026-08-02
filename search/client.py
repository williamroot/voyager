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