"""Scheduler APScheduler para crons do Voyager.

Usa BlockingScheduler (sem persistência em banco — jobs são leves e
idempotentes, então re-registrar a cada restart é OK).

Padrão: o scheduler enfileira jobs RQ via .delay(); a execução pesada
fica nos workers, não no processo do scheduler.
"""
import logging

import django_rq
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.blocking import BlockingScheduler
from django.db import close_old_connections
from rq.exceptions import NoSuchJobError
from rq.job import Job

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


def _enqueue_singleton(fn, queue_name: str, job_id: str):
    """Enfileira fn na queue apenas se não há job pendente/executando com esse job_id."""
    q = django_rq.get_queue(queue_name)
    try:
        existing = Job.fetch(job_id, connection=q.connection)
        if existing.get_status() in ('queued', 'started'):
            logger.debug('singleton skip %s (já na fila)', job_id)
            return
    except NoSuchJobError:
        pass
    q.enqueue(fn, job_id=job_id)


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

    # Aquecimento do dashboard — 6 jobs independentes na fila `warm`.
    # Cada um tem lock + statement_timeout próprios; falha de um não
    # bloqueia os outros. Worker `warm` dedicado em .30 garante isolation
    # do `default` (que tem ticks/watchdogs). REFRESH MV separado em
    # cron diário pra não interferir no warm path quente.
    # Charts leves + jobs rápidos: cron 5min
    for warm_job, job_id in (
        (warm_kpis, 'warm_kpis'),
        (warm_charts_leves, 'warm_charts_leves'),
        (warm_ingestao_por_hora, 'warm_ingestao_por_hora'),
        (warm_partes, 'warm_partes'),
        (warm_estatisticas_tribunal, 'warm_estatisticas_tribunal'),
        (warm_filtros_movimentacoes, 'warm_filtros_movimentacoes'),
    ):
        scheduler.add_job(
            _enqueue_singleton,
            'interval',
            args=[warm_job, 'warm', job_id],
            minutes=15,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Charts pesados (GROUP BY em 187M rows): cron 30min — não fazem
    # parte do hot-path da home; podem ficar com dados de até 2h velhos.
    scheduler.add_job(
        _enqueue_singleton,
        'interval',
        args=[warm_charts_pesados, 'warm', 'warm_charts_pesados'],
        minutes=30,
        id='warm_charts_pesados',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # REFRESH MV CONCURRENTLY — diário às 3h. Fora do warm path porque
    # pode travar 1-2h em tabelas grandes; lock_timeout=5s aborta cedo
    # se outro REFRESH segura lock (visto empilhar 11 conexões + crash).
    scheduler.add_job(
        refresh_materialized_views.delay,
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

    # Re-classifica processos com mov nova nos últimos 7 dias + drena
    # backlog de nunca-classificados (cap default 500k por run).
    # Cada hora — backlog inicial de ~2.4M (96% TRF1 + 99.5% TRF3) leva
    # alguns dias pra drenar nesse ritmo. Auto-enqueue per-batch da
    # ingestão DJEN cobre o caminho quente; este cron drena o frio.
    # max_instances=1 + coalesce=True: o job tem timeout 4h em interval 1h —
    # se atrasar, novos triggers são consolidados em vez de empilhar
    # (evita 2 schedulers competindo no mesmo lote).
    from tribunals.jobs import reclassificar_recentes
    scheduler.add_job(
        reclassificar_recentes.delay,
        'interval',
        hours=1,
        id='reclassificar_recentes',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler
