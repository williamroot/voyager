import logging
from datetime import date, timedelta
from typing import Iterator

from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import IngestionRun, Movimentacao, Process, Tribunal

from .client import DJENClient
from .parser import parse_item

logger = logging.getLogger('voyager.djen.ingestion')

BATCH_SIZE = 500

# Tribunais com enricher implementado. Process novo nesses tribunais é
# auto-enfileirado pra enriquecimento via consulta pública.
TRIBUNAIS_COM_ENRICHER = {'TRF1'}


def ingest_processo(processo, client: DJENClient | None = None) -> dict:
    """Sincroniza movimentações de UM processo via DJEN.

    Útil pra preencher movs históricas que ainda não foram cobertas pelo backfill,
    ou pra atualizar um processo específico sob demanda. Reusa _process_page —
    INSERT idempotente via bulk_create(ignore_conflicts=True).
    """
    client = client or DJENClient()
    tribunal = processo.tribunal
    run = IngestionRun.objects.create(
        tribunal=tribunal,
        status=IngestionRun.STATUS_RUNNING,
        # Sem janela específica — usamos data_autuacao..hoje como aproximação pra auditoria.
        janela_inicio=processo.data_autuacao or date(2020, 1, 1),
        janela_fim=date.today(),
    )
    cnjs_tocados: set[str] = set()
    try:
        for items in client.iter_pages_processo(tribunal.sigla_djen, processo.numero_cnj):
            _process_page(items, tribunal, run, cnjs_tocados)
        if cnjs_tocados:
            _atualizar_resumo_processos(tribunal, cnjs_tocados)
        run.status = IngestionRun.STATUS_SUCCESS
    except Exception as exc:
        run.status = IngestionRun.STATUS_FAILED
        run.erros.append({'erro': 'execucao_processo', 'cnj': processo.numero_cnj, 'detalhe': str(exc)[:500]})
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'erros', 'finished_at'])
        raise
    finally:
        if run.status == IngestionRun.STATUS_SUCCESS:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])
    return {
        'run_id': run.pk,
        'cnj': processo.numero_cnj,
        'novas': run.movimentacoes_novas,
        'duplicadas': run.movimentacoes_duplicadas,
        'paginas': run.paginas_lidas,
    }


def chunk_dates(start: date, end: date, days: int = 30) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def ingest_window(tribunal: Tribunal, data_inicio: date, data_fim: date,
                  client: DJENClient | None = None) -> IngestionRun:
    """Ingere uma janela contínua de dias para um tribunal. 1 IngestionRun por chamada."""
    client = client or DJENClient()
    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=data_inicio, janela_fim=data_fim,
    )
    cnjs_tocados: set[str] = set()
    try:
        for items in client.iter_pages(tribunal.sigla_djen, data_inicio, data_fim):
            _process_page(items, tribunal, run, cnjs_tocados)
        if cnjs_tocados:
            _atualizar_resumo_processos(tribunal, cnjs_tocados)
        run.status = IngestionRun.STATUS_SUCCESS
    except Exception as exc:
        run.status = IngestionRun.STATUS_FAILED
        run.erros.append({'erro': 'execucao', 'detalhe': str(exc)[:500]})
        logger.exception('ingestion_run failed', extra={'run_id': run.pk, 'tribunal': tribunal.sigla})
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'erros', 'finished_at'])
        raise
    finally:
        if run.status == IngestionRun.STATUS_SUCCESS:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])
    return run


def _process_page(items: list[dict], tribunal: Tribunal, run: IngestionRun,
                  cnjs_tocados: set[str]) -> None:
    """Processa uma página da DJEN — toda a unidade é atômica para garantir consistência da métrica."""
    parsed = []
    for item in items:
        p = parse_item(item, tribunal, run)
        if p is not None:
            parsed.append(p)

    if not parsed:
        run.paginas_lidas += 1
        run.save(update_fields=['paginas_lidas', 'erros'])
        return

    cnjs_pagina = {p.cnj for p in parsed}
    ext_ids_pagina = [p.external_id for p in parsed]

    with transaction.atomic():
        existentes_cnj = dict(
            Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina)
            .values_list('numero_cnj', 'pk')
        )
        novos_processos = [
            Process(tribunal=tribunal, numero_cnj=c)
            for c in cnjs_pagina - existentes_cnj.keys()
        ]
        if novos_processos:
            Process.objects.bulk_create(novos_processos, ignore_conflicts=True, batch_size=BATCH_SIZE)
            cnjs_realmente_novos = {p.numero_cnj for p in novos_processos} - existentes_cnj.keys()
            existentes_cnj = dict(
                Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina)
                .values_list('numero_cnj', 'pk')
            )
            # Auto-enfileira enriquecimento dos processos novos (após commit) — gate por
            # tribunais com enricher implementado pra evitar ValueError no job.
            if tribunal.sigla in TRIBUNAIS_COM_ENRICHER:
                ids_pra_enriquecer = [existentes_cnj[c] for c in cnjs_realmente_novos
                                       if c in existentes_cnj]
                transaction.on_commit(lambda ids=ids_pra_enriquecer: _enfileirar_enrichers(ids))

        ja_existem_extids = set(
            Movimentacao.objects.filter(tribunal=tribunal, external_id__in=ext_ids_pagina)
            .values_list('external_id', flat=True)
        )

        movs = []
        for p in parsed:
            kwargs = p.to_movimentacao_kwargs()
            movs.append(Movimentacao(
                processo_id=existentes_cnj[p.cnj],
                tribunal=tribunal,
                **kwargs,
            ))
        Movimentacao.objects.bulk_create(movs, ignore_conflicts=True, batch_size=BATCH_SIZE)

        # Métrica aproximada: TOCTOU possível entre SELECT e bulk_create. Documentado:
        # workers concorrentes podem dupli-contar como "novos" o mesmo external_id;
        # `ignore_conflicts` garante que dados não sejam duplicados, só a métrica.
        novos_count = len(ext_ids_pagina) - len(ja_existem_extids)
        run.movimentacoes_novas += novos_count
        run.movimentacoes_duplicadas += len(ja_existem_extids)
        run.processos_novos += len(novos_processos)
        run.paginas_lidas += 1
        run.save(update_fields=[
            'movimentacoes_novas', 'movimentacoes_duplicadas',
            'processos_novos', 'paginas_lidas', 'erros',
        ])
        cnjs_tocados.update(cnjs_pagina)


def _atualizar_resumo_processos(tribunal: Tribunal, cnjs: set[str]) -> None:
    """Recalcula primeira/ultima_movimentacao_em e total_movimentacoes em batch."""
    chunk = []
    for cnj in cnjs:
        chunk.append(cnj)
        if len(chunk) >= 1000:
            _flush_resumo(tribunal, chunk)
            chunk = []
    if chunk:
        _flush_resumo(tribunal, chunk)


def _flush_resumo(tribunal: Tribunal, cnjs: list[str]) -> None:
    procs = Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
    aggregates = (
        Movimentacao.objects.filter(tribunal=tribunal, processo__in=procs)
        .values('processo_id')
        .annotate(
            primeira=Min('data_disponibilizacao'),
            ultima=Max('data_disponibilizacao'),
            total=Count('id'),
        )
    )
    by_proc = {a['processo_id']: a for a in aggregates}
    to_update = []
    for p in procs:
        agg = by_proc.get(p.pk)
        if not agg:
            continue
        p.primeira_movimentacao_em = agg['primeira']
        p.ultima_movimentacao_em = agg['ultima']
        p.total_movimentacoes = agg['total']
        to_update.append(p)
    if to_update:
        Process.objects.bulk_update(
            to_update,
            fields=['primeira_movimentacao_em', 'ultima_movimentacao_em', 'total_movimentacoes'],
            batch_size=500,
        )


def _enfileirar_enrichers(process_ids: list[int]) -> None:
    """Enfileira jobs de enriquecimento. Importação local pra evitar ciclo
    djen ↔ enrichers (enrichers usa Tribunal/Process do tribunals)."""
    if not process_ids:
        return
    from enrichers.jobs import enriquecer_processo
    for pid in process_ids:
        try:
            enriquecer_processo.delay(pid)
        except Exception as exc:
            logger.warning('falha ao enfileirar enrichment', extra={'process_id': pid, 'erro': str(exc)})
