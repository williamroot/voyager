import django_rq
from django.conf import settings
from django.core.management.base import BaseCommand
from rq.registry import FailedJobRegistry


class Command(BaseCommand):
    help = 'Re-enfileira todos os failed jobs de todas as filas RQ.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--queue', '-q',
            dest='queues',
            metavar='QUEUE',
            action='append',
            help='Fila específica (pode repetir). Padrão: todas.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Lista os jobs sem re-enfileirar.',
        )

    def handle(self, *args, **opts):
        queue_names = opts['queues'] or list(settings.RQ_QUEUES.keys())
        dry_run = opts['dry_run']

        total_ok = 0
        total_err = 0

        for name in queue_names:
            queue = django_rq.get_queue(name)
            registry = FailedJobRegistry(queue=queue)
            job_ids = registry.get_job_ids()

            if not job_ids:
                continue

            self.stdout.write(self.style.HTTP_INFO(f'\n--- {name} ({len(job_ids)} failed) ---'))

            for job_id in job_ids:
                job = queue.fetch_job(job_id)
                if job is None:
                    self.stdout.write(self.style.WARNING(f'  {job_id[:16]}  expirado/não encontrado'))
                    total_err += 1
                    continue

                func_name = getattr(job, 'func_name', None) or repr(job.func)
                exc_info = (job.exc_info or '').strip().splitlines()
                last_exc = exc_info[-1][:100] if exc_info else ''

                if dry_run:
                    self.stdout.write(f'  [dry] {job_id[:16]}  {func_name}  {last_exc}')
                    total_ok += 1
                    continue

                try:
                    registry.requeue(job_id)
                    self.stdout.write(f'  ✓  {job_id[:16]}  {func_name}')
                    total_ok += 1
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f'  ✗  {job_id[:16]}  {func_name}  {exc}'))
                    total_err += 1

        action = 'encontrados' if dry_run else 're-enfileirados'
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style(f'\n{total_ok} jobs {action}, {total_err} erros.'))
