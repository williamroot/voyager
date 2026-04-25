from django.core.management.base import BaseCommand
from django.utils import timezone

from djen.proxies import ProxyScrapePool
from tribunals.models import IngestionRun, SchemaDriftAlert, Tribunal


class Command(BaseCommand):
    help = 'Mostra status operacional: último run por tribunal, drift alerts abertos, status do pool de proxies.'

    def handle(self, *args, **opts):
        self.stdout.write(self.style.HTTP_INFO('=== Tribunais ==='))
        for t in Tribunal.objects.order_by('sigla'):
            ult = IngestionRun.objects.filter(tribunal=t).order_by('-started_at').first()
            line = f'  {t.sigla}  ativo={t.ativo}  inicio_disp={t.data_inicio_disponivel}  backfill_ok={t.backfill_concluido_em}'
            if ult:
                lag_h = (timezone.now() - ult.started_at).total_seconds() / 3600
                line += f'  ult_run=#{ult.pk} {ult.status} (há {lag_h:.1f}h, novas={ult.movimentacoes_novas})'
            self.stdout.write(line)

        self.stdout.write(self.style.HTTP_INFO('=== Drift alerts abertos ==='))
        abertos = SchemaDriftAlert.objects.filter(resolvido=False).select_related('tribunal')
        if not abertos.exists():
            self.stdout.write('  (nenhum)')
        for a in abertos:
            self.stdout.write(f'  #{a.pk} {a.tribunal_id} {a.tipo} chaves={a.chaves} desde={a.detectado_em}')

        self.stdout.write(self.style.HTTP_INFO('=== Proxies ==='))
        st = ProxyScrapePool.singleton().status()
        self.stdout.write(f'  ProxyScrape: total={st["total"]} bad={st["bad"]} saudaveis={st["saudaveis"]}')
