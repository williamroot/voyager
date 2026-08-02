"""Jobs RQ de indexação Elasticsearch (fila es_index)."""
import logging

from tribunals.models import Movimentacao, Process

from .client import get_es, index_name
from .documents import movimentacao_to_doc, processo_to_doc

logger = logging.getLogger('voyager.search.jobs')


def indexar_movimentacao(mov_pk: int):
    """Indexa uma Movimentacao no ES. Idempotente (upsert by id)."""
    try:
        mov = Movimentacao.objects.select_related('processo', 'tribunal').get(pk=mov_pk)
    except Movimentacao.DoesNotExist:
        logger.warning('Movimentacao %s não encontrada pra indexar', mov_pk)
        return
    try:
        es = get_es()
        doc = movimentacao_to_doc(mov)
        es.index(index=index_name('movimentacoes'), id=mov.id, document=doc)
        logger.debug('Movimentacao %s indexada', mov_pk)
    except Exception as e:
        logger.error('Erro indexando Movimentacao %s: %s', mov_pk, e)


def desindexar_movimentacao(mov_id: int):
    """Remove uma Movimentacao do ES pelo id. Idempotente."""
    try:
        es = get_es()
        es.delete(index=index_name('movimentacoes'), id=mov_id, ignore=[404])
        logger.debug('Movimentacao %s desindexada', mov_id)
    except Exception as e:
        logger.error('Erro desindexando Movimentacao %s: %s', mov_id, e)


def indexar_processo(proc_pk: int):
    """Indexa um Process no ES. Idempotente."""
    try:
        proc = Process.objects.select_related('tribunal').get(pk=proc_pk)
    except Process.DoesNotExist:
        logger.warning('Process %s não encontrado pra indexar', proc_pk)
        return
    try:
        es = get_es()
        doc = processo_to_doc(proc)
        es.index(index=index_name('processos'), id=proc.id, document=doc)
        logger.debug('Process %s indexado', proc_pk)
    except Exception as e:
        logger.error('Erro indexando Process %s: %s', proc_pk, e)


def desindexar_processo(proc_id: int):
    """Remove um Process do ES pelo id. Idempotente."""
    try:
        es = get_es()
        es.delete(index=index_name('processos'), id=proc_id, ignore=[404])
        logger.debug('Process %s desindexado', proc_id)
    except Exception as e:
        logger.error('Erro desindexando Process %s: %s', proc_id, e)