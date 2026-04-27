"""Audita cobertura: compara o `count` que a DJEN diz existir em cada
janela com o que temos em Movimentacao no DB. Mostra gaps por chunk.

Uso típico após o fix do cap de 10k — pra confirmar que estamos
pegando tudo entre data_inicio_disponivel e hoje.

Idempotente, só leitura. Cada chunk = 1 request DJEN (count em
itensPorPagina=1, barato). Default chunk de 30 dias; pode dividir
em 1 dia pra precisão máxima.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from djen.client import DJENClient
from djen.ingestion import chunk_dates
from tribunals.models import Movimentacao, Tribunal


DJEN_HARD_CAP = 10_000


def djen_count_real(client: DJENClient, sigla_djen: str, ini: date, fim: date) -> int:
    """Count de fato — DJEN responde count máximo 10k, então quando bate o
    cap, divide a janela em 2 metades e soma recursivamente. Pára quando
    a janela é de 1 dia (assume que naquele dia é genuíno)."""
    n = client.count_window(sigla_djen, ini, fim)
    if n < DJEN_HARD_CAP or (fim - ini).days < 1:
        return n
    meio = ini + (fim - ini) // 2
    return (djen_count_real(client, sigla_djen, ini, meio)
            + djen_count_real(client, sigla_djen, meio + timedelta(days=1), fim))


class Command(BaseCommand):
    help = "Audita cobertura DJEN vs DB (count por chunk com split adaptativo)."

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default=None, help='Sigla; default: todos ativos.')
        parser.add_argument('--inicio', default=None, help='YYYY-MM-DD; default: data_inicio_disponivel.')
        parser.add_argument('--fim', default=None, help='YYYY-MM-DD; default: hoje.')
        parser.add_argument('--chunk-days', type=int, default=30)
        parser.add_argument('--pct-threshold', type=float, default=5.0,
                            help='Marca chunks com gap > N%% (default 5%%)')

    def handle(self, *args, tribunal, inicio, fim, chunk_days, pct_threshold, **opts):
        client = DJENClient()
        siglas = [tribunal] if tribunal else list(
            Tribunal.objects.filter(ativo=True).values_list('sigla', flat=True)
        )

        for sigla in siglas:
            t = Tribunal.objects.get(sigla=sigla)
            ini = date.fromisoformat(inicio) if inicio else t.data_inicio_disponivel
            end = date.fromisoformat(fim) if fim else date.today()
            if not ini:
                self.stdout.write(self.style.WARNING(
                    f'{sigla}: data_inicio_disponivel é NULL, pule ou passe --inicio'
                ))
                continue

            total_djen = 0
            total_db = 0
            chunks_ok = 0
            chunks_gap = 0
            chunks_cap = 0
            self.stdout.write(self.style.HTTP_INFO(
                f'\n=== {sigla} {ini} → {end} (chunks de {chunk_days}d) ===\n'
                f'  {"janela":<23} {"DJEN":>10} {"DB":>10} {"gap":>10} {"%":>8}'
            ))

            for ci, cf in chunk_dates(ini, end, days=chunk_days):
                try:
                    djen_count = djen_count_real(client, t.sigla_djen, ci, cf)
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f'  {ci}→{cf}: erro DJEN: {str(exc)[:80]}'))
                    continue
                db_count = Movimentacao.objects.filter(
                    tribunal=t,
                    data_disponibilizacao__date__gte=ci,
                    data_disponibilizacao__date__lte=cf,
                ).count()
                gap = djen_count - db_count
                pct = (gap / djen_count * 100) if djen_count else 0.0
                flags = ''
                if abs(pct) > pct_threshold and djen_count > 0:
                    flags += ' ⚠GAP'
                    chunks_gap += 1
                else:
                    chunks_ok += 1
                if djen_count >= DJEN_HARD_CAP:
                    flags += ' ⚠CAP'
                    chunks_cap += 1
                total_djen += djen_count
                total_db += db_count
                self.stdout.write(
                    f'  {ci}→{cf:<11} {djen_count:>10,} {db_count:>10,} {gap:>+10,} {pct:>+7.1f}%{flags}'
                )

            gap_total = total_djen - total_db
            pct_total = (gap_total / total_djen * 100) if total_djen else 0.0
            self.stdout.write(self.style.HTTP_INFO(
                f'  {"─"*70}\n'
                f'  TOTAL {sigla:<5}      {total_djen:>10,} {total_db:>10,} {gap_total:>+10,} {pct_total:>+7.1f}%\n'
                f'  chunks: {chunks_ok} ok, {chunks_gap} com gap >{pct_threshold:.0f}%, {chunks_cap} com cap 10k'
            ))
