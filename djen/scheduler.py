"""Registra crons no rq-scheduler de forma idempotente.

Sempre cancela as schedules existentes desta aplicação antes de re-registrar,
evitando duplicação a cada restart do container `scheduler`.
"""
import logging

import django_rq
from rq_scheduler import Scheduler

from tribunals.models import Tribunal

from .jobs import refresh_proxy_pool, run_daily_ingestion, watchdog_ingestao

logger = logging.getLogger('voyager.djen.scheduler')

SCHEDULE_TAG = 'voyager-cron'


def _get_scheduler() -> Scheduler:
    return django_rq.get_scheduler('default')


def _cancel_existing(scheduler: Scheduler) -> int:
    cancelled = 0
    for job in list(scheduler.get_jobs()):
        meta = job.meta or {}
        if meta.get('tag') == SCHEDULE_TAG:
            scheduler.cancel(job)
            cancelled += 1
    return cancelled


def register_all() -> dict:
    scheduler = _get_scheduler()
    cancelados = _cancel_existing(scheduler)

    novos = 0
    # Tribunais ativos: 1 cron diário cada, escalonado de 30 em 30 min a partir das 04:00.
    ativos = list(Tribunal.objects.filter(ativo=True).order_by('sigla'))
    for idx, t in enumerate(ativos):
        hour, minute = 4, (idx * 30) % 60 + (30 if idx >= 2 else 0)
        # ex.: TRF1 04:00, TRF3 04:30, TRF? 05:00, ...
        hour = 4 + (idx // 2)
        minute = 0 if idx % 2 == 0 else 30
        cron = f'{minute} {hour} * * *'
        sched_job = scheduler.cron(
            cron,
            func=run_daily_ingestion,
            args=[t.sigla],
            queue_name='djen_ingestion',
            use_local_timezone=True,
            repeat=None,
        )
        sched_job.meta['tag'] = SCHEDULE_TAG
        sched_job.save_meta()
        novos += 1
        logger.info('agendado run_daily_ingestion', extra={'tribunal': t.sigla, 'cron': cron})

    # refresh_proxy_pool a cada 15 min
    proxy_job = scheduler.cron(
        '*/15 * * * *',
        func=refresh_proxy_pool,
        queue_name='default',
        use_local_timezone=True,
        repeat=None,
    )
    proxy_job.meta['tag'] = SCHEDULE_TAG
    proxy_job.save_meta()
    novos += 1

    # watchdog_ingestao a cada 5 min — mata zumbis e re-enfileira backfill/daily
    # quando algum sumiu da fila (worker crashou, redis perdeu state, etc.).
    wd_job = scheduler.cron(
        '*/5 * * * *',
        func=watchdog_ingestao,
        queue_name='default',
        use_local_timezone=True,
        repeat=None,
    )
    wd_job.meta['tag'] = SCHEDULE_TAG
    wd_job.save_meta()
    novos += 1

    logger.info('schedules registrados', extra={'cancelados': cancelados, 'novos': novos})
    return {'cancelados': cancelados, 'novos': novos}
