from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from djen.client import DJENClient
from tribunals.models import Tribunal

PROBE_DAYS = 7
DEFAULT_FLOOR = date(2022, 1, 1)


class Command(BaseCommand):
    help = 'Descobre a primeira data com resultados na DJEN para um tribunal (busca binária).'

    def add_arguments(self, parser):
        parser.add_argument('sigla')
        parser.add_argument('--force', action='store_true')
        parser.add_argument('--floor', default=DEFAULT_FLOOR.isoformat(),
                            help='Data mínima a considerar (YYYY-MM-DD). Default: 2022-01-01.')

    def handle(self, *args, sigla, force, floor, **opts):
        try:
            t = Tribunal.objects.get(sigla=sigla)
        except Tribunal.DoesNotExist:
            raise CommandError(f'Tribunal {sigla} não cadastrado')

        if t.data_inicio_disponivel and not force:
            self.stdout.write(self.style.WARNING(
                f'{t.sigla} já tem data_inicio_disponivel={t.data_inicio_disponivel}. '
                'Use --force para sobrescrever.'
            ))
            return

        floor_dt = date.fromisoformat(floor)
        today = date.today()
        client = DJENClient()

        encontrada = self._busca_binaria(client, t.sigla_djen, floor_dt, today)
        if not encontrada:
            self.stdout.write(self.style.ERROR(
                f'{t.sigla}: nenhum dia com resultados encontrado entre {floor_dt} e {today}.'
            ))
            return

        t.data_inicio_disponivel = encontrada
        t.save(update_fields=['data_inicio_disponivel'])
        self.stdout.write(self.style.SUCCESS(
            f'{t.sigla}: data_inicio_disponivel = {encontrada}'
        ))

    def _busca_binaria(self, client: DJENClient, sigla_djen: str, lo: date, hi: date) -> date | None:
        if not self._tem_resultados(client, sigla_djen, lo, hi):
            return None
        while (hi - lo).days > PROBE_DAYS:
            mid = lo + (hi - lo) // 2
            if self._tem_resultados(client, sigla_djen, lo, mid):
                hi = mid
            else:
                lo = mid + timedelta(days=1)
        for d_offset in range(0, (hi - lo).days + 1):
            d = lo + timedelta(days=d_offset)
            if self._tem_resultados(client, sigla_djen, d, d):
                return d
        return None

    def _tem_resultados(self, client: DJENClient, sigla_djen: str, ini: date, fim: date) -> bool:
        try:
            count = client.count_only(sigla_djen, ini, fim)
            self.stdout.write(f'  probe {sigla_djen} {ini}..{fim} → {count}')
            return count > 0
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'  probe falhou: {exc}'))
            return False
