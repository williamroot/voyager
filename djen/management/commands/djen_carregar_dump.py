"""Carrega dump local (JSONL) pro Postgres via bulk_create.

Lê os arquivos {data-dir}/{sigla}/*.jsonl, parseia com `parse_item` (mesmo
parser usado na ingestão), insere Process + Movimentacao em bulk com
ignore_conflicts (idempotente — UNIQUE(tribunal, external_id) dedupe).

Uso:
  python manage.py djen_carregar_dump TRF1 [--data-dir data/djen_dump]
                                          [--batch-size 5000]
"""
import json
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from djen.parser import parse_item
from tribunals.models import IngestionRun, Movimentacao, Process, Tribunal

logger = logging.getLogger('voyager.djen.carregar')


class Command(BaseCommand):
    help = 'Carrega dump JSONL local pro Postgres (idempotente, bulk).'

    def add_arguments(self, parser):
        parser.add_argument('sigla')
        parser.add_argument('--data-dir', default='data/djen_dump')
        parser.add_argument('--batch-size', type=int, default=5000)
        parser.add_argument('--inicio', default=None, help='YYYY-MM-DD pula arquivos antes.')
        parser.add_argument('--fim', default=None, help='YYYY-MM-DD pula arquivos depois.')

    def handle(self, *args, sigla, data_dir, batch_size, inicio, fim, **opts):
        t = Tribunal.objects.get(sigla=sigla)
        ini = date.fromisoformat(inicio) if inicio else None
        end = date.fromisoformat(fim) if fim else None

        in_dir = Path(data_dir) / sigla
        if not in_dir.is_dir():
            self.stderr.write(f'Diretório não existe: {in_dir}')
            return

        arquivos = sorted(in_dir.glob('*.jsonl'))
        if ini:
            arquivos = [f for f in arquivos if date.fromisoformat(f.stem) >= ini]
        if end:
            arquivos = [f for f in arquivos if date.fromisoformat(f.stem) <= end]

        if not arquivos:
            self.stderr.write('Nenhum arquivo no range.')
            return

        run = IngestionRun.objects.create(
            tribunal=t, status=IngestionRun.STATUS_RUNNING,
            janela_inicio=date.fromisoformat(arquivos[0].stem),
            janela_fim=date.fromisoformat(arquivos[-1].stem),
        )

        total_files = len(arquivos)
        total_items = 0
        novas_movs = 0
        novos_procs = 0
        try:
            for idx, arq in enumerate(arquivos, 1):
                items = []
                with open(arq, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            items.append(json.loads(line))
                if not items:
                    continue
                processados, n_proc, n_mov = self._inserir_chunk(
                    t, items, run, batch_size,
                )
                total_items += processados
                novos_procs += n_proc
                novas_movs += n_mov
                if idx % 30 == 0 or idx == total_files:
                    self.stdout.write(
                        f'  [{idx}/{total_files}] {arq.stem}: {processados} items '
                        f'(acum: {total_items:,} items, +{novos_procs:,} procs, +{novas_movs:,} movs)'
                    )
            run.status = IngestionRun.STATUS_SUCCESS
        except Exception as exc:
            run.status = IngestionRun.STATUS_FAILED
            run.erros.append({'erro': 'execucao', 'detalhe': str(exc)[:500]})
            self.stderr.write(self.style.ERROR(f'Falha: {exc}'))
            raise
        finally:
            from django.utils import timezone as tz
            run.finished_at = tz.now()
            run.movimentacoes_novas = novas_movs
            run.processos_novos = novos_procs
            run.paginas_lidas = total_files
            run.save()

        self.stdout.write(self.style.SUCCESS(
            f'{sigla}: {total_files} arquivos, {total_items:,} items, '
            f'+{novos_procs:,} processos, +{novas_movs:,} movimentações'
        ))

    def _inserir_chunk(self, tribunal, items, run, batch_size):
        """Parseia + bulk_create em batches. Retorna (lidos, procs_novos, movs_novas)."""
        parsed = [p for p in (parse_item(it, tribunal, run) for it in items) if p is not None]
        if not parsed:
            return len(items), 0, 0

        # Process — dedupe por (tribunal, numero_cnj) via constraint
        cnjs = {p.cnj for p in parsed}
        existentes = dict(
            Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
            .values_list('numero_cnj', 'pk')
        )
        a_criar_proc = [Process(tribunal=tribunal, numero_cnj=c)
                        for c in cnjs - existentes.keys()]
        n_proc_novos = len(a_criar_proc)
        if a_criar_proc:
            Process.objects.bulk_create(a_criar_proc, ignore_conflicts=True, batch_size=batch_size)
            existentes = dict(
                Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
                .values_list('numero_cnj', 'pk')
            )

        # Movimentacao — bulk em batches
        ext_ids = [p.external_id for p in parsed]
        ja_existem = set(
            Movimentacao.objects.filter(tribunal=tribunal, external_id__in=ext_ids)
            .values_list('external_id', flat=True)
        )

        movs = []
        for p in parsed:
            kwargs = p.to_movimentacao_kwargs()
            if p.codigo_classe:
                kwargs['classe_id'] = p.codigo_classe
            movs.append(Movimentacao(
                processo_id=existentes[p.cnj],
                tribunal=tribunal,
                **kwargs,
            ))
        Movimentacao.objects.bulk_create(movs, ignore_conflicts=True, batch_size=batch_size)
        n_mov_novos = len(ext_ids) - len(ja_existem)
        return len(items), n_proc_novos, n_mov_novos
