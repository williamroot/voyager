"""Scheduler APScheduler para crons do Voyager.

Usa BlockingScheduler (sem persistência em banco — jobs são leves e
idempotentes, então re-registrar a cada restart é OK).

Padrão: jobs leves e de enfileiramento usam .delay() (RQ). Jobs de warm
do dashboard rodam INLINE no thread pool do scheduler — evita acúmulo
na fila `warm` e elimina dependência de workers externos para o dashboard.
Cada função de warm tem _with_lock próprio; max_instances=1 no APScheduler
é a segunda camada de proteção contra execuções sobrepostas.
"""
import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from django.db import close_old_connections

from tribunals.models import Tribunal

from dashboard.tasks import (
    refresh_materialized_views,
    warm_charts_leves,
    warm_charts_pesados,
    warm_estatisticas_tribunal,
    warm_filtros_movimentacoes,
    warm_ingestao_por_hora,
    warm_kpis,
    warm_partes,
    warm_workers_cache_inline,
)

from .jobs import (
    refresh_proxy_pool,
    run_daily_ingestion,
    tick_backfill_retroativo,
    watchdog_ingestao,
)

logger = logging.getLogger('voyager.djen.scheduler')


def _close_db(event):
    close_old_connections()


def create_scheduler() -> BlockingScheduler:
    """Cria e configura o BlockingScheduler com todos os crons do Voyager."""
    ativos = list(Tribunal.objects.filter(ativo=True).order_by('sigla'))

    # 20 threads: jobs de warm podem rodar horas em paralelo (kpis, charts_pesados,
    # estatisticas, filtros) sem bloquear os ticks curtos (watchdog, backfill).
    scheduler = BlockingScheduler(
        timezone='America/Sao_Paulo',
        executors={'default': ThreadPoolExecutor(20)},
    )
    scheduler.add_listener(
        _close_db,
        EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )

    # Ingestão diária — escalonado de 30 em 30 min a partir das 04:00
    for idx, t in enumerate(ativos):
        hour = 4 + (idx // 2)
        minute = 0 if idx % 2 == 0 else 30
        scheduler.add_job(
            run_daily_ingestion.delay,
            'cron',
            args=[t.sigla],
            hour=hour,
            minute=minute,
            id=f'daily_ingestion_{t.sigla}',
            replace_existing=True,
        )
        logger.info('agendado daily_ingestion %s %02d:%02d', t.sigla, hour, minute)

    # Tick de backfill retroativo: a cada 10 min por tribunal
    for t in ativos:
        scheduler.add_job(
            tick_backfill_retroativo.delay,
            'interval',
            args=[t.sigla],
            minutes=10,
            id=f'tick_backfill_{t.sigla}',
            replace_existing=True,
        )
        logger.info('agendado tick_backfill %s (cada 10min)', t.sigla)

    # Refresh do pool de proxies: a cada 15 min
    scheduler.add_job(
        refresh_proxy_pool.delay,
        'interval',
        minutes=15,
        id='refresh_proxies',
        replace_existing=True,
    )

    # Watchdog de ingestão: a cada 5 min
    scheduler.add_job(
        watchdog_ingestao.delay,
        'interval',
        minutes=5,
        id='watchdog_ingestao',
        replace_existing=True,
    )

    # Reabastece filas de enriquecimento: a cada 2 min
    from enrichers.jobs import reabastecer_filas_enriquecimento
    scheduler.add_job(
        reabastecer_filas_enriquecimento.delay,
        'interval',
        minutes=2,
        id='refill_enrichers',
        replace_existing=True,
    )

    # Reabastece fila Datajud: análogo ao PJe — drena backlog histórico
    # de Process com data_enriquecimento_datajud=NULL. Sem este job,
    # processos antigos ficavam pra sempre sem Datajud (observado 96%
    # TRF3 e 38% TRF1 backlog).
    from datajud.jobs import reabastecer_fila_datajud
    scheduler.add_job(
        reabastecer_fila_datajud.delay,
        'interval',
        minutes=2,
        id='refill_datajud',
        replace_existing=True,
    )

    # Aquecimento do dashboard — inline no thread pool do scheduler.
    # Sem fila RQ: sem acúmulo de duplicatas, sem dependência de workers externos.
    # _with_lock em cada função é a proteção primária contra sobreposição;
    # max_instances=1 + coalesce=True no APScheduler é a segunda camada.
    for warm_fn, job_id, interval_kwargs in (
        (warm_kpis,                  'warm_kpis',                  {'minutes': 30}),
        (warm_charts_leves,          'warm_charts_leves',          {'minutes': 30}),
        (warm_ingestao_por_hora,     'warm_ingestao_por_hora',     {'hours': 4}),
        (warm_partes,                'warm_partes',                {'minutes': 30}),
        (warm_estatisticas_tribunal, 'warm_estatisticas_tribunal', {'minutes': 30}),
        (warm_filtros_movimentacoes, 'warm_filtros_movimentacoes', {'minutes': 30}),
        (warm_charts_pesados,        'warm_charts_pesados',        {'minutes': 30}),
    ):
        scheduler.add_job(
            warm_fn,
            'interval',
            **interval_kwargs,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # REFRESH MV CONCURRENTLY — diário às 3h, inline.
    # lock_timeout=5s na query aborta se outro REFRESH segura lock.
    scheduler.add_job(
        refresh_materialized_views,
        'cron',
        hour=3,
        minute=0,
        id='refresh_materialized_views',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Workers snapshot — INLINE no thread do scheduler (sem RQ). É leve
    # (só lê Redis) e enfileirar fazia pile-up na fila default quando
    # workers ficavam ocupados no warm_dashboard_all pesado.
    scheduler.add_job(
        warm_workers_cache_inline,
        'interval',
        seconds=30,
        id='warm_workers_cache',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Classificação por prioridade — único cron, substitui todos os fluxos inline.
    # Grupo 1: desatualizados (classificacao_em < ultima_movimentacao_em), mais recentes primeiro.
    # Grupo 2 (fallback): classificados há mais tempo (classificacao_em ASC).
    # Idle quando tudo está atualizado — zero enqueue desnecessário.
    # max_instances=1 + coalesce=True: job coordenador é rápido (só enfileira),
    # mas os batches têm timeout 10min; sem proteção acumulariam na fila.
    from tribunals.jobs import reclassificar_por_prioridade
    scheduler.add_job(
        reclassificar_por_prioridade.delay,
        'interval',
        minutes=20,
        id='reclassificar_por_prioridade',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler
