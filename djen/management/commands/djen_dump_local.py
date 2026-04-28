"""Faz dump completo do DJEN dia-a-dia em JSONL local — abordagem simples
e robusta pra evitar gap por cap de 10k em janelas grandes.

Estrutura de saída:
  data/djen_dump/{sigla}/{YYYY-MM-DD}.jsonl

Cada arquivo contém TODAS as movimentações daquele dia (1 JSON por linha).
Idempotente: pula arquivos já existentes (retomável).

Uso:
  python manage.py djen_dump_local TRF1 [--inicio 2020-12-14] [--fim 2026-04-28]
                                  [--data-dir data/djen_dump]
"""
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand

from djen.client import DJENClient
from tribunals.models import Tribunal

logger = logging.getLogger('voyager.djen.dump')


class Command(BaseCommand):
    help = 'Dump local DJEN em JSONL dia-a-dia (idempotente, retomável).'

    def add_arguments(self, parser):
        parser.add_argument('sigla')
        parser.add_argument('--inicio', default=None,
                            help='YYYY-MM-DD; default: data_inicio_disponivel.')
        parser.add_argument('--fim', default=None,
                            help='YYYY-MM-DD; default: hoje.')
        parser.add_argument('--data-dir', default='data/djen_dump')
        parser.add_argument('--force', action='store_true',
                            help='Re-baixa mesmo se arquivo existe.')

    def handle(self, *args, sigla, inicio, fim, data_dir, force, **opts):
        t = Tribunal.objects.get(sigla=sigla)
        ini = date.fromisoformat(inicio) if inicio else t.data_inicio_disponivel
        end = date.fromisoformat(fim) if fim else date.today()
        if not ini:
            self.stderr.write(f'{sigla}: data_inicio_disponivel é NULL — passe --inicio')
            return

        out_dir = Path(data_dir) / sigla
        out_dir.mkdir(parents=True, exist_ok=True)
        client = DJENClient()

        total_dias = (end - ini).days + 1
        baixados = 0
        skipados = 0
        total_items = 0
        cur = ini
        idx = 0
        while cur <= end:
            idx += 1
            arquivo = out_dir / f'{cur.isoformat()}.jsonl'
            if arquivo.exists() and not force:
                skipados += 1
                cur += timedelta(days=1)
                continue

            try:
                items_dia = []
                for items in client.iter_pages(t.sigla_djen, cur, cur):
                    items_dia.extend(items)
            except Exception as exc:
                self.stderr.write(self.style.WARNING(
                    f'  {cur}: erro DJEN — {str(exc)[:120]}'
                ))
                cur += timedelta(days=1)
                continue

            tmp = arquivo.with_suffix('.jsonl.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                for item in items_dia:
                    f.write(json.dumps(item, ensure_ascii=False, default=str))
                    f.write('\n')
            os.replace(tmp, arquivo)
            baixados += 1
            total_items += len(items_dia)
            if baixados % 10 == 0 or len(items_dia) > 5000:
                self.stdout.write(
                    f'  [{idx}/{total_dias}] {cur}: {len(items_dia):,} movs '
                    f'(baixados={baixados} skip={skipados} acum={total_items:,})'
                )
            cur += timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(
            f'{sigla}: {baixados} arquivos baixados, {skipados} já existiam, '
            f'{total_items:,} items totais em {out_dir}'
        ))
