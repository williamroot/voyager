"""Jobs RQ de indexação Elasticsearch (fila es_index)."""
import logging

from django.db.models import Prefetch
from elasticsearch import ApiError

from tribunals.models import Movimentacao, Process, ProcessoParte

from .client import get_es, index_name, indices_espelho
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
        for indice in indices_espelho('movimentacoes'):
            es.index(index=indice, id=mov.id, document=doc)
        logger.debug('Movimentacao %s indexada', mov_pk)
    except Exception as e:
        logger.error('Erro indexando Movimentacao %s: %s', mov_pk, e)


#: Teto de BYTES por `_bulk`, e não só de documentos.
#:
#: Medido em produção em 21/08/2026: dos 91 jobs `indexar_movimentacoes_bulk`
#: parados no `FailedJobRegistry`, **83 morreram com `ApiError(413)`** — o corpo
#: da requisição passou do `http.max_content_length` do ES (100 MB por default).
#: O lote era contado em DOCUMENTOS (500) e o texto de uma publicação não tem
#: tamanho fixo: 500 x 200 KB já estoura. Os 91 jobs referenciam **45.500**
#: publicações, das quais **45.313 estavam fora do índice** — e ninguém
#: reprocessa o `FailedJobRegistry`. Ou seja: um teto que existia (o do ES) era
#: um corte MUDO do nosso lado, que é o formato exato da regra nº 2 do
#: CLAUDE.md. 20 MB deixa 5x de folga para o pico e cabe num `_bulk` rápido.
BULK_MAX_BYTES = 20 * 1024 * 1024
#: Custo fixo estimado por documento fora do `body` (metadados + linha de ação
#: + partes/advogados concatenados). Grosseiro de propósito: serve para o teto
#: não ser furado por um lote de 500 documentos curtos com 30 campos cada.
BULK_OVERHEAD_DOC = 4 * 1024


def _status_http(e: ApiError) -> int | None:
    """Código HTTP do erro do ES. `status_code` é alias legado; `meta.status` é o atual."""
    return getattr(e, 'status_code', None) or getattr(getattr(e, 'meta', None), 'status', None)


def _enviar_bulk(ops: list, rotulo: str = 'indexar_movimentacoes_bulk') -> int:
    """Manda UM `_bulk` e conta os documentos aceitos. Divide ao meio no 413.

    O 413 não é erro de dado, é erro de tamanho: a mesma lista dividida passa.
    Dividir aqui (em vez de deixar o job morrer no registry) é a diferença
    entre uma retentativa automática e 45.313 publicações fora da busca, que é
    o que estava acontecendo.

    Documento que sozinho não cabe vira ERRO REGISTRADO, nunca descarte mudo.
    """
    if not ops:
        return 0
    es = get_es()
    try:
        resp = es.bulk(operations=ops)
    except ApiError as e:
        if _status_http(e) != 413 or len(ops) <= 2:
            if _status_http(e) == 413:
                logger.error('%s: documento único acima do teto '
                             'do ES (_id=%s) — NÃO indexado', rotulo,
                             ops[0].get('index', {}).get('_id'))
                return 0
            raise
        meio = (len(ops) // 4) * 2 or 2      # corta em par (ação, doc)
        logger.warning('%s: 413 com %d docs — dividindo em %d/%d',
                       rotulo, len(ops) // 2, meio // 2, (len(ops) - meio) // 2)
        return (_enviar_bulk(ops[:meio], rotulo)
                + _enviar_bulk(ops[meio:], rotulo))
    if resp.get('errors'):
        erros = [it for it in resp.get('items', [])
                 if it.get('index', {}).get('status', 200) >= 300]
        logger.error('%s: %d/%d docs com erro (ex: %s)',
                     rotulo, len(erros), len(ops) // 2, str(erros[:1])[:300])
        return len(ops) // 2 - len(erros)
    return len(ops) // 2


def indexar_movimentacoes_bulk(mov_pks: list[int]):
    """Indexa N Movimentacao num único `_bulk`. Molde do `indexar_processos_bulk`.

    Por que existe: `indexar_movimentacao` é UM job por publicação — um SELECT
    no Postgres e um `es.index()` por item. Medido em 18/08/2026, com só a
    recuperação do TJSP rodando, a fila `es_index` estava em **1,68 milhão e
    subindo** (dreno ~200 mil/h contra injeção em burst), e o índice já corria
    ~178 milhões de documentos atrás do Postgres.

    Isso não é lentidão, é a terceira perda da tabela do CLAUDE.md se repetindo:
    dado coletado que a tela não enxerga vale zero. Com 212 milhões de
    publicações pra recuperar, coletar sem indexar em lote é enterrar 212
    milhões.

    Idempotente (`index` by `_id`), como o de processos.
    """
    if not mov_pks:
        return
    movs = (Movimentacao.objects
            .filter(pk__in=mov_pks)
            .select_related('processo', 'tribunal'))
    indices = list(indices_espelho('movimentacoes'))
    ops: list = []
    bytes_no_lote = 0
    enviados = 0
    for mov in movs:
        doc = movimentacao_to_doc(mov)
        # O corpo do ato domina o tamanho do documento; os outros ~30 campos são
        # curtos. `len(texto)` é aproximação por baixo (UTF-8 multibyte), e a
        # folga do teto absorve isso — a alternativa seria serializar o JSON só
        # para medir, no caminho de escrita.
        tamanho = (len(doc.get('body') or '') + BULK_OVERHEAD_DOC) * len(indices)
        if ops and bytes_no_lote + tamanho > BULK_MAX_BYTES:
            enviados += _enviar_bulk(ops)
            ops, bytes_no_lote = [], 0
        for indice in indices:
            ops.append({'index': {'_index': indice, '_id': mov.id}})
            ops.append(doc)
        bytes_no_lote += tamanho
    if ops:
        enviados += _enviar_bulk(ops)
    logger.debug('indexar_movimentacoes_bulk: %d docs em %d pks', enviados, len(mov_pks))


def desindexar_movimentacao(mov_id: int):
    """Remove uma Movimentacao do ES pelo id. Idempotente."""
    try:
        es = get_es()
        for indice in indices_espelho('movimentacoes'):
            es.delete(index=indice, id=mov_id, ignore=[404])
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


def indexar_processos_bulk(proc_pks: list[int]) -> int:
    """Indexa N Process num único `_bulk` — write-through dos caminhos em massa.

    O drainer de enriquecimento (`enrichers.drainer.apply_batch`) escreve via
    bulk_update/bulk_create, que NÃO disparam post_save — sem este job os
    enriquecidos só chegariam ao ES no próximo reindex manual. Ele enfileira
    os pks do batch aqui ao final da transação. Idempotente (index by _id).

    **Fecha por BYTES, não por documentos** — a mesma lição que já custou 83
    jobs mortos com `ApiError(413)` e 45.313 publicações fora do índice do lado
    das movimentações (21/08/2026). Aqui o documento também não tem tamanho
    fixo: `partes`/`advs` são a concatenação de TODAS as participações do
    processo, e um processo de ente público com centenas de partes gera um doc
    ordens de grandeza maior que a mediana (<10 participações). Contar 500
    documentos é contar a coisa errada; quem estoura o `http.max_content_length`
    do ES são os bytes.

    Devolve quantos documentos o ES aceitou. Chamador que compara com
    `len(proc_pks)` enxerga o buraco; até 24/08/2026 esta função devolvia
    `None` e engolia a diferença.
    """
    if not proc_pks:
        return 0
    # prefetch das participações (+parte) → _serialize_partes não faz query
    # por processo (N+1) — mesmo padrão do reindexar_processos.
    procs = (Process.objects
             .filter(pk__in=proc_pks)
             .select_related('tribunal')
             .prefetch_related(Prefetch('participacoes',
                                        queryset=ProcessoParte.objects.select_related('parte'))))
    idx = index_name('processos')
    ops: list = []
    bytes_no_lote = 0
    enviados = 0
    try:
        for proc in procs:
            doc = processo_to_doc(proc)
            # `partes`/`advs` dominam o tamanho; os outros campos são curtos e
            # cabem no overhead fixo. Aproximação por baixo (UTF-8 multibyte),
            # absorvida pela folga de 5x do teto — a alternativa seria
            # serializar o JSON só para medir, no caminho de escrita.
            tamanho = (len(doc.get('partes') or '') + len(doc.get('advs') or '')
                       + BULK_OVERHEAD_DOC)
            if ops and bytes_no_lote + tamanho > BULK_MAX_BYTES:
                enviados += _enviar_bulk(ops, 'indexar_processos_bulk')
                ops, bytes_no_lote = [], 0
            ops.append({'index': {'_index': idx, '_id': proc.id}})
            ops.append(doc)
            bytes_no_lote += tamanho
        if ops:
            enviados += _enviar_bulk(ops, 'indexar_processos_bulk')
    except Exception as e:  # noqa: BLE001 — job de fila não pode derrubar o worker
        logger.error('Erro no bulk de %d processos: %s', len(proc_pks), e)
        return enviados
    logger.debug('indexar_processos_bulk: %d docs indexados', enviados)
    return enviados


def desindexar_processo(proc_id: int):
    """Remove um Process do ES pelo id. Idempotente."""
    try:
        es = get_es()
        es.delete(index=index_name('processos'), id=proc_id, ignore=[404])
        logger.debug('Process %s desindexado', proc_id)
    except Exception as e:
        logger.error('Erro desindexando Process %s: %s', proc_id, e)


def conferir_indice_processos() -> dict:
    """Sentinela do ÍNDICE DE PROCESSOS: o passivo de 14,2 milhões reabriu?

    O que ela vigia, medido em produção em 24/08/2026 com amostra aleatória de
    4.000 pks por faixa (seed 20260824) e `_mget` por id: **13,99%** dos
    processos do Postgres (4.380 de 31.301 amostrados) estavam FORA do
    Elasticsearch — concentrados nas faixas de pk mais NOVAS (45,99% · 9,44% ·
    61,44% nos três últimos oitavos, 0,00% nos cinco primeiros).

    Nada acusava isso. A fila `es_index` marcava zero, os runs fechavam verdes,
    e o `_cat/indices` mostrava 104.594.795 documentos contra 87.709.209 do
    `_count` — porque `participacoes` é `nested` e cada objeto aninhado é um
    doc Lucene. Ou seja: a contagem mais à mão dizia que o índice tinha MAIS
    processos do que o banco tem linhas.

    Ver `search/backfill_processos.py`.
    """
    from search.backfill_processos import conferir_indice_processos as _sentinela
    return _sentinela()


def agendar_conferencia_indice_processos() -> dict:
    """Enfileira UMA passada da sentinela, com `job_id` fixo. Chamado pelo cron.

    `job_id` determinístico impede empilhamento: se a passada anterior ainda
    não rodou, o RQ substitui em vez de acrescentar. Fila `default`, nunca
    `es_index` — a fila da indexação pode ter dezenas de milhares de jobs à
    frente durante um backfill, e sentinela que roda tarde é sentinela que não
    roda.
    """
    import django_rq

    django_rq.get_queue('default').enqueue(
        conferir_indice_processos, job_id='search:conferir_indice_processos',
        job_timeout=900)
    return {'enfileirado': True}
