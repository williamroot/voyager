"""Criação/garantia de indexes ES com mappings."""
import logging

from .client import get_es, index_name
from .mappings import ACERVO_MAPPING, MOV_MAPPING, PROC_MAPPING

logger = logging.getLogger('voyager.search')

INDEXES = {
    'movimentacoes': MOV_MAPPING,
    'processos': PROC_MAPPING,
    'acervo': ACERVO_MAPPING,
}


def ensure_indexes():
    """Cria indexes que não existem ainda. Idempotente."""
    es = get_es()
    for suffix, mapping in INDEXES.items():
        name = index_name(suffix)
        if not es.indices.exists(index=name):
            es.indices.create(index=name, body=mapping)
            logger.info('Índice ES criado: %s', name)
        else:
            logger.debug('Índice ES já existe: %s', name)


def ensure_index(suffix: str):
    """Cria um índice específico. Idempotente."""
    es = get_es()
    name = index_name(suffix)
    if not es.indices.exists(index=name):
        mapping = INDEXES[suffix]
        es.indices.create(index=name, body=mapping)
        logger.info('Índice ES criado: %s', name)