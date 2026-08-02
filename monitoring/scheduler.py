"""Registro do cron de varredura diária no APScheduler."""
import logging

logger = logging.getLogger('voyager.monitoring.scheduler')


def register_monitoring_jobs(scheduler):
    """Registra crons de monitoramento. Chamado por djen/scheduler.py::create_scheduler."""
    from monitoring.jobs import varredura_diaria

    # Varredura diária às 05:00.
    scheduler.add_job(
        varredura_diaria,
        trigger='cron',
        hour=5,
        minute=0,
        id='monitoring_varredura_diaria',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info('Cron de varredura diária registrado (05:00)')