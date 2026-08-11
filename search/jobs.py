"""Jobs RQ de indexação Elasticsearch (fila es_index)."""
import logging

from django.db.models import Prefetch

from tribunals.models import Movimentacao, Process, ProcessoParte

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


def indexar_processos_bulk(proc_pks: list[int]):
    """Indexa N Process num único `_bulk` — write-through dos caminhos em massa.

    O drainer de enriquecimento (`enrichers.drainer.apply_batch`) escreve via
    bulk_update/bulk_create, que NÃO disparam post_save — sem este job os
    enriquecidos só chegariam ao ES no próximo reindex manual. Ele enfileira
    os pks do batch aqui ao final da transação. Idempotente (index by _id).
    """
    if not proc_pks:
        return
    # prefetch das participações (+parte) → _serialize_partes não faz query
    # por processo (N+1) — mesmo padrão do reindexar_processos.
    procs = (Process.objects
             .filter(pk__in=proc_pks)
             .select_related('tribunal')
             .prefetch_related(Prefetch('participacoes',
                                        queryset=ProcessoParte.objects.select_related('parte'))))
    ops = []
    idx = index_name('processos')
    for proc in procs:
        ops.append({'index': {'_index': idx, '_id': proc.id}})
        ops.append(processo_to_doc(proc))
    if not ops:
        return
    try:
        es = get_es()
        resp = es.bulk(operations=ops)
        if resp.get('errors'):
            erros = [it for it in resp.get('items', [])
                     if it.get('index', {}).get('status', 200) >= 300]
            logger.error('indexar_processos_bulk: %d/%d docs com erro (ex: %s)',
                         len(erros), len(proc_pks), str(erros[:1])[:300])
        else:
            logger.debug('indexar_processos_bulk: %d docs indexados', len(proc_pks))
    except Exception as e:
        logger.error('Erro no bulk de %d processos: %s', len(proc_pks), e)


def desindexar_processo(proc_id: int):
    """Remove um Process do ES pelo id. Idempotente."""
    try:
        es = get_es()
        es.delete(index=index_name('processos'), id=proc_id, ignore=[404])
        logger.debug('Process %s desindexado', proc_id)
    except Exception as e:
        logger.error('Erro desindexando Process %s: %s', proc_id, e)