"""Re-roda ingest_window pras janelas que bateram o cap de 10k da API DJEN.

Identificação: IngestionRun com paginas_lidas=100 e
(novas + duplicadas) >= 10000. Esses provavelmente perderam dados.

Como ingest_window agora faz split adaptativo, basta re-rodar — ele
mesmo subdivide. Idempotente (DJEN dedupe via uniq_mov_tribunal_extid).
"""
from django.core.management.base import BaseCommand

from djen.ingestion import DJEN_HARD_CAP, ingest_window
from tribunals.models import IngestionRun, Tribunal


class Command(BaseCommand):
    help = "Re-processa janelas que bateram o cap de 10k da DJEN."

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default=None,
                            help='Restringe a uma sigla; default todos.')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')

    def handle(self, *args, tribunal, limit, dry_run, **opts):
        qs = (IngestionRun.objects
              .filter(status='success', paginas_lidas__gte=100)
              .extra(where=['movimentacoes_novas + movimentacoes_duplicadas >= %s'],
                     params=[DJEN_HARD_CAP])
              .order_by('-janela_fim'))
        if tribunal:
            qs = qs.filter(tribunal_id=tribunal)
        # Dedupe — evita re-rodar a mesma (tribunal, janela) várias vezes
        # quando há multiple runs success na mesma janela.
        seen = set()
        unicos = []
        for r in qs.iterator():
            key = (r.tribunal_id, r.janela_inicio, r.janela_fim)
            if key in seen:
                continue
            seen.add(key)
            unicos.append(r)
            if limit and len(unicos) >= limit:
                break

        self.stdout.write(self.style.HTTP_INFO(
            f'{len(unicos):,} janelas únicas a reprocessar (cap 10k)'
        ))

        if dry_run:
            for r in unicos[:20]:
                dur = (r.janela_fim - r.janela_inicio).days
                self.stdout.write(
                    f'  [dry] {r.tribunal_id} {r.janela_inicio}→{r.janela_fim} '
                    f'({dur}d) novas={r.movimentacoes_novas:,}'
                )
            return

        tribs_cache = {t.sigla: t for t in Tribunal.objects.all()}
        for r in unicos:
            t = tribs_cache.get(r.tribunal_id)
            if not t:
                continue
            try:
                ingest_window(t, r.janela_inicio, r.janela_fim)
                self.stdout.write(self.style.SUCCESS(
                    f'  {r.tribunal_id} {r.janela_inicio}→{r.janela_fim} reprocessado'
                ))
            except Exception as exc:
                self.stdout.write(self.style.WARNING(
                    f'  falha em {r.tribunal_id} {r.janela_inicio}→{r.janela_fim}: {exc}'
                ))
