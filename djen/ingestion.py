import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

UF_OABS = [
    'AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS', 'MT',
    'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN', 'RO', 'RR', 'RS', 'SC', 'SE', 'SP', 'TO',
]

# Tribunais com enricher implementado. Process novo nesses tribunais é
# auto-enfileirado pra enriquecimento via consulta pública.
TRIBUNAIS_COM_ENRICHER = {'TRF1', 'TRF3'}


def ingest_processo(processo, client: DJENClient | None = None) -> dict:
    """Sincroniza movimentações de UM processo via DJEN.

    Não cria IngestionRun — esses são reservados pro backfill janela-de-dia
    via `ingest_window`. Auditoria por-processo fica em
    `Process.ultima_sinc_djen_em` + `Movimentacao.inserido_em` (do bulk insert).

    Reusa `_process_page` com run=None: bulk_create(ignore_conflicts=True)
    continua idempotente, só não atualiza contadores de run.
    """
    client = client or DJENClient()
    tribunal = processo.tribunal
    cnjs_tocados: set[str] = set()
    novas = 0
    duplicadas = 0
    paginas = 0
    for items in client.iter_pages_processo(tribunal.sigla_djen, processo.numero_cnj):
        n_novas, n_dup = _process_page(items, tribunal, None, cnjs_tocados)
        novas += n_novas
        duplicadas += n_dup
        paginas += 1
    if cnjs_tocados:
        _atualizar_resumo_processos(tribunal, cnjs_tocados)
    now_ts = timezone.now()
    Process.objects.filter(pk=processo.pk).update(
        data_enriquecimento_djen=now_ts,
        ultima_sinc_djen_em=now_ts,
    )

    if novas > 0 or processo.classificacao_em is None:
        try:
            from tribunals.classificador import classificar_e_persistir
            classificar_e_persistir(processo)
        except Exception as exc:
            logger.warning('falha ao classificar %s: %s', processo.numero_cnj, exc)

    return {
        'cnj': processo.numero_cnj,
        'novas': novas,
        'duplicadas': duplicadas,
        'paginas': paginas,
    }


def chunk_dates(start: date, end: date, days: int = 30) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


# DJEN tem cap rígido de 10k itens por janela (paginação para em 100 pgs × 100).
# Quando bate, chunks com volume alto perdem dados — split adaptativo resolve.
DJEN_HARD_CAP = 10_000


def ingest_window(tribunal: Tribunal, data_inicio: date, data_fim: date,
                  client: DJENClient | None = None,
                  forcar_uf_em_1d: bool = False) -> IngestionRun:
    """Ingere uma janela contínua de dias para um tribunal. 1 IngestionRun por chamada.

    Estratégia adaptativa ao CAP de 10k:
    - Janela > 1 dia que bate o CAP: divide em 2 metades e re-processa recursivamente,
      propagando `forcar_uf_em_1d=True` pros filhos.
    - Janela de 1 dia que bate o CAP: proba count antes e usa ufOab (27 UFs) como filtro,
      garantindo cobertura completa. Enfileira job de auditoria por órgão.

    `forcar_uf_em_1d`: quando True e a janela for de 1 dia, pula o probe
    `count_only` e vai direto pra `_ingest_day_por_uf`. Usado pelos filhos
    do split adaptativo — um count baixo nesse contexto é quase certo
    falso negativo (WAF/proxy ruim retornando payload truncado), e ignorá-lo
    foi causa documentada de perda de dados (~10k/dia/tribunal).
    """
    client = client or DJENClient()

    # Probe antecipado: dia único com CAP vai direto pra estratégia UF.
    if data_inicio == data_fim:
        if forcar_uf_em_1d:
            logger.info('djen single-day forçando UF strategy (split de janela capada)', extra={
                'tribunal': tribunal.sigla, 'dia': str(data_inicio),
            })
            return _ingest_day_por_uf(tribunal, data_inicio, client)
        count = client.count_only(tribunal.sigla_djen, data_inicio, data_fim)
        if count >= DJEN_HARD_CAP:
            logger.warning('djen single-day cap detected via probe, using UF strategy', extra={
                'tribunal': tribunal.sigla, 'dia': str(data_inicio), 'count': count,
            })
            return _ingest_day_por_uf(tribunal, data_inicio, client)

    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=data_inicio, janela_fim=data_fim,
    )
    cnjs_tocados: set[str] = set()
    t0 = time.monotonic()
    logger.info('ingest_window inicio %s %s→%s run_id=%d', tribunal.sigla, data_inicio, data_fim, run.pk)
    try:
        for items in client.iter_pages(tribunal.sigla_djen, data_inicio, data_fim):
            _process_page(items, tribunal, run, cnjs_tocados)
        if cnjs_tocados:
            _atualizar_resumo_processos(tribunal, cnjs_tocados)
            _enfileirar_todos_enrichments(tribunal, cnjs_tocados)
        run.status = IngestionRun.STATUS_SUCCESS
    except Exception as exc:
        run.status = IngestionRun.STATUS_FAILED
        run.erros.append({'erro': 'execucao', 'detalhe': str(exc)[:500]})
        logger.exception('ingestion_run failed', extra={'run_id': run.pk, 'tribunal': tribunal.sigla})
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'erros', 'finished_at'])
        raise
    finally:
        duracao = int(time.monotonic() - t0)
        if run.status == IngestionRun.STATUS_SUCCESS:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])
            logger.info(
                'ingest_window fim %s %s→%s → novas=%d dup=%d pgs=%d %ds run_id=%d',
                tribunal.sigla, data_inicio, data_fim,
                run.movimentacoes_novas, run.movimentacoes_duplicadas,
                run.paginas_lidas, duracao, run.pk,
            )

    # Split adaptativo: se bateu o cap em janela > 1 dia, divide em 2 metades.
    # Filhos sempre forçam UF strategy quando chegarem em 1 dia — count_only
    # pode mentir (WAF) e o caminho normal de paginação re-cap aria.
    if (run.movimentacoes_novas + run.movimentacoes_duplicadas) >= DJEN_HARD_CAP \
            and run.paginas_lidas >= 100 \
            and (data_fim - data_inicio).days >= 1:
        meio = data_inicio + (data_fim - data_inicio) // 2
        logger.warning('djen window hit cap, splitting', extra={
            'tribunal': tribunal.sigla, 'inicio': str(data_inicio), 'fim': str(data_fim),
            'novas': run.movimentacoes_novas, 'duplicadas': run.movimentacoes_duplicadas,
        })
        ingest_window(tribunal, data_inicio, meio, client=client, forcar_uf_em_1d=True)
        ingest_window(tribunal, meio + timedelta(days=1), data_fim, client=client,
                      forcar_uf_em_1d=True)

    return run


def _ingest_day_por_uf(tribunal: Tribunal, dia: date, client: DJENClient) -> IngestionRun:
    """Ingere um dia com >10k movs subdividindo por ufOab (27 UFs em paralelo).

    Nenhum UF isolado atinge o CAP, então a soma garante cobertura completa.
    Itens são deduplicados pelo campo 'id' antes do INSERT.
    Após ingestão, enfileira job de auditoria por órgão na fila djen_audit.
    """
    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=dia, janela_fim=dia,
    )
    cnjs_tocados: set[str] = set()

    def _fetch_uf(uf: str) -> list[dict]:
        items = []
        for pagina in range(1, 11):  # max 10 × 1000 = 10k por UF (nenhum UF chega perto)
            payload = client._fetch(
                tribunal.sigla_djen, dia, dia,
                pagina=pagina, itens_por_pagina=1000,
                extra_params={'ufOab': uf},
            )
            page = payload.get('items') or []
            items.extend(page)
            if len(page) < 1000:
                break
        return items

    try:
        all_items: list[dict] = []
        seen_ids: set = set()
        uf_erros: list[str] = []

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_uf, uf): uf for uf in UF_OABS}
            for fut in as_completed(futs):
                uf = futs[fut]
                try:
                    uf_items = fut.result()
                    novos_uf = 0
                    for it in uf_items:
                        item_id = it.get('id')
                        if item_id not in seen_ids:
                            seen_ids.add(item_id)
                            all_items.append(it)
                            novos_uf += 1
                    logger.debug('uf=%s → %d itens (%d únicos novos)', uf, len(uf_items), novos_uf)
                except Exception as exc:
                    uf_erros.append(uf)
                    run.erros.append({'erro': 'uf_fetch', 'uf': uf, 'detalhe': str(exc)[:200]})
                    logger.warning('falha ao coletar uf=%s: %s', uf, str(exc)[:120])

        if uf_erros:
            logger.warning('djen UF strategy: %d UFs falharam: %s', len(uf_erros), uf_erros)

        logger.info(
            'djen UF strategy %s %s → %d itens únicos (%d UFs, %d erros)',
            tribunal.sigla, dia, len(all_items), len(UF_OABS), len(uf_erros),
        )

        # Falha o run se a coleta foi degradada demais — antes, qualquer
        # combinação de UFs com erro virava success silencioso, mascarando
        # perda total de dados em ondas de WAF.
        limiar_erros = max(1, len(UF_OABS) // 2)  # >=14/27 UFs falhando
        if len(uf_erros) >= limiar_erros:
            raise RuntimeError(
                f'UF strategy degradada: {len(uf_erros)}/{len(UF_OABS)} UFs falharam'
            )
        if not all_items and uf_erros:
            raise RuntimeError(
                f'UF strategy: zero itens coletados com {len(uf_erros)} UFs em erro'
            )

        for i in range(0, len(all_items), BATCH_SIZE):
            _process_page(all_items[i:i + BATCH_SIZE], tribunal, run, cnjs_tocados)

        if cnjs_tocados:
            _atualizar_resumo_processos(tribunal, cnjs_tocados)
            _enfileirar_todos_enrichments(tribunal, cnjs_tocados)
        run.status = IngestionRun.STATUS_SUCCESS
    except Exception as exc:
        run.status = IngestionRun.STATUS_FAILED
        run.erros.append({'erro': 'execucao_uf', 'detalhe': str(exc)[:500]})
        logger.exception('_ingest_day_por_uf failed', extra={'run_id': run.pk, 'tribunal': tribunal.sigla})
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'erros', 'finished_at'])
        raise
    finally:
        if run.status == IngestionRun.STATUS_SUCCESS:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])

    # Enfileira auditoria de cobertura por órgão de forma assíncrona.
    try:
        from .jobs import audit_cobertura_dia
        audit_cobertura_dia.delay(tribunal.sigla, str(dia))
    except Exception as exc:
        logger.warning('falha ao enfileirar audit_cobertura_dia: %s', exc)

    return run


def _process_page(items: list[dict], tribunal: Tribunal, run: IngestionRun | None,
                  cnjs_tocados: set[str]) -> tuple[int, int]:
    """Processa uma página da DJEN. Retorna (novas, duplicadas) pra caller
    agregar quando rodando sem IngestionRun (ingest_processo).

    Quando `run` é não-None (caminho ingest_window/backfill_dia), atualiza
    os contadores no run direto. Atomicidade garante consistência da métrica.
    """
    parsed = []
    for item in items:
        p = parse_item(item, tribunal, run)
        if p is not None:
            parsed.append(p)

    if not parsed:
        if run is not None:
            run.paginas_lidas += 1
            run.save(update_fields=['paginas_lidas', 'erros'])
        return (0, 0)

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
            existentes_cnj = dict(
                Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina)
                .values_list('numero_cnj', 'pk')
            )

        ja_existem_extids = set(
            Movimentacao.objects.filter(tribunal=tribunal, external_id__in=ext_ids_pagina)
            .values_list('external_id', flat=True)
        )

        # Catálogo de classes — upsert batch dos pares (codigo, nome) da página.
        # Usa nome do DJEN só se a classe ainda não existe (Process já populou
        # nomes melhores via PJe consulta pública).
        from tribunals.models import ClasseJudicial
        classes_pagina = {
            (p.codigo_classe, p.nome_classe)
            for p in parsed if p.codigo_classe and p.nome_classe
        }
        if classes_pagina:
            ClasseJudicial.objects.bulk_create(
                [ClasseJudicial(codigo=c, nome=n) for c, n in classes_pagina],
                ignore_conflicts=True,
                batch_size=BATCH_SIZE,
            )

        movs = []
        for p in parsed:
            kwargs = p.to_movimentacao_kwargs()
            if p.codigo_classe:
                kwargs['classe_id'] = p.codigo_classe
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
        if run is not None:
            run.movimentacoes_novas += novos_count
            run.movimentacoes_duplicadas += len(ja_existem_extids)
            run.processos_novos += len(novos_processos)
            run.paginas_lidas += 1
            run.save(update_fields=[
                'movimentacoes_novas', 'movimentacoes_duplicadas',
                'processos_novos', 'paginas_lidas', 'erros',
            ])
        cnjs_tocados.update(cnjs_pagina)
        return (novos_count, len(ja_existem_extids))


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
    now_ts = timezone.now()
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
        # Marca passagem do DJEN também no caminho data-based (backfill_dia
        # / daily_ingestion). Cada page processada cobre um conjunto de
        # processos — todos têm seu data_enriquecimento_djen renovado.
        p.data_enriquecimento_djen = now_ts
        to_update.append(p)
    if to_update:
        Process.objects.bulk_update(
            to_update,
            fields=['primeira_movimentacao_em', 'ultima_movimentacao_em',
                    'total_movimentacoes', 'data_enriquecimento_djen'],
            batch_size=500,
        )


def _enfileirar_todos_enrichments(tribunal: Tribunal, cnjs: set[str]) -> None:
    """Para todo processo NOVO descoberto na ingestão DJEN, enfileira:
      1. Enriquecimento PJe (consulta pública) — partes/advogados — fila enrich_trf{N}
      2. Sincronização Datajud — movs+metadados — fila datajud

    Filtra por enriquecido_em < 24h pra evitar re-enfileirar processos
    recém-tocados. Cada fila tem workers dedicados, sem competição.

    Histórico DJEN per-processo (`sync_movimentacoes_bulk`) NÃO é mais
    enfileirado aqui: o `backfill_dia` cobre todo histórico do tribunal
    naturalmente, e Datajud já traz o histórico completo do processo em
    1 request. DJEN per-processo só sob demanda (futuro).
    """
    if not cnjs:
        return

    cutoff = timezone.now() - timedelta(hours=24)
    procs = list(
        Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
        .values('pk', 'enriquecido_em', 'ultima_sinc_djen_em')
    )
    if not procs:
        return

    enriq_eligiveis = []
    datajud_eligiveis = []
    for p in procs:
        if p['enriquecido_em'] is None or p['enriquecido_em'] < cutoff:
            enriq_eligiveis.append(p['pk'])
        if p['ultima_sinc_djen_em'] is None or p['ultima_sinc_djen_em'] < cutoff:
            datajud_eligiveis.append(p['pk'])

    # PJe enricher só pra tribunais com scraper implementado.
    if enriq_eligiveis and tribunal.sigla in TRIBUNAIS_COM_ENRICHER:
        from enrichers.jobs import enqueue_enriquecimento
        for pid in enriq_eligiveis:
            try:
                enqueue_enriquecimento(pid, tribunal.sigla)
            except Exception as exc:
                logger.warning('falha enfileirar enrichment', extra={'pid': pid, 'erro': str(exc)})

    # Datajud roda pra QUALQUER tribunal (CNJ tem index pra todos).
    if datajud_eligiveis:
        from datajud.jobs import datajud_sync_bulk
        for pid in datajud_eligiveis:
            try:
                datajud_sync_bulk.delay(pid)
            except Exception as exc:
                logger.warning('falha enfileirar datajud', extra={'pid': pid, 'erro': str(exc)})

    # Reclassificação: TODOS os processos tocados (não só os elegíveis a
    # PJe/Datajud) ganham re-classificação em batch. Movs novas alteram as
    # features (F15_logMovs, F21_diasUltMovZ, etc) — sem este caminho,
    # processos descobertos via UF strategy só seriam reclassificados no
    # cron horário, atrasando lead detection. Vai pra fila `classificacao`
    # (workers dedicados) em chunks de 500 pra não estourar timeout.
    todos_pids = [p['pk'] for p in procs]
    if todos_pids:
        try:
            from tribunals.jobs import reclassificar_batch
        except ImportError:
            logger.exception('reclassificar_batch indisponível — skip auto-reclassif')
        else:
            for i in range(0, len(todos_pids), 500):
                try:
                    reclassificar_batch.delay(todos_pids[i:i + 500])
                except Exception as exc:
                    logger.warning('falha enfileirar reclassif chunk %d: %s', i, exc)

    logger.info(
        'auto-enqueue %s → pje=%d datajud=%d reclassif=%d (de %d tocados)',
        tribunal.sigla, len(enriq_eligiveis) if tribunal.sigla in TRIBUNAIS_COM_ENRICHER else 0,
        len(datajud_eligiveis), len(todos_pids), len(procs),
    )
