"""Scheduler APScheduler para crons do Voyager.

Usa BlockingScheduler (sem persistência em banco — jobs são leves e
idempotentes, então re-registrar a cada restart é OK).

Padrão: o scheduler enfileira jobs RQ via .delay(); a execução pesada
fica nos workers, não no processo do scheduler.
"""
import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.blocking import BlockingScheduler
from django.db import close_old_connections

from tribunals.models import Tribunal

from dashboard.tasks import (
    warm_chart_cache,
    warm_kpis_cache,
    warm_partes_cache,
    warm_workers_cache,
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

    scheduler = BlockingScheduler(timezone='America/Sao_Paulo')
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

    # Aquecimento dos KPIs da home: a cada 5 min
    scheduler.add_job(
        warm_kpis_cache.delay,
        'interval',
        minutes=5,
        id='warm_kpis_cache',
        replace_existing=True,
    )

    # Aquecimento do cache de charts: a cada 5 min
    scheduler.add_job(
        warm_chart_cache.delay,
        'interval',
        minutes=5,
        id='warm_chart_cache',
        replace_existing=True,
    )

    # Aquecimento do snapshot workers/filas: a cada 30s
    scheduler.add_job(
        warm_workers_cache.delay,
        'interval',
        seconds=30,
        id='warm_workers_cache',
        replace_existing=True,
    )

    # Aquecimento da distribuição por tipo de Parte: a cada 5 min
    scheduler.add_job(
        warm_partes_cache.delay,
        'interval',
        minutes=5,
        id='warm_partes_cache',
        replace_existing=True,
    )

    return scheduler
