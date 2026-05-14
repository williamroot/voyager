"""Wrapper CLI para `tribunals.services.export_labels.exportar_labels_retreino`.

Uso:
    python manage.py exportar_labels_retreino
    python manage.py exportar_labels_retreino --min-data 2025-01-01
    python manage.py exportar_labels_retreino --output /tmp/labels.csv
    python manage.py exportar_labels_retreino --sem-humano
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tribunals.services.export_labels import (
    estatisticas_labels,
    exportar_labels_retreino,
)


class Command(BaseCommand):
    help = 'Exporta labels consolidados para retreino v7 do classificador.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--min-data', type=str, default=None,
            help='Filtro temporal (YYYY-MM-DD) — só LeadConsumption e '
                 'ProcessoValidacao a partir dessa data. CSVs legados ignoram.',
        )
        parser.add_argument(
            '--output', type=str, default=None,
            help='Caminho do CSV de saída. Default: data/labels_retreino_TS.csv',
        )
        parser.add_argument(
            '--sem-humano', action='store_true',
            help='Não inclui ProcessoValidacao.',
        )
        parser.add_argument(
            '--sem-juriscope', action='store_true',
            help='Não inclui LeadConsumption.',
        )
        parser.add_argument(
            '--sem-csvs-legados', action='store_true',
            help='Não inclui CSVs raiz (leads_trf1.csv etc).',
        )

    def handle(self, *args, **options):
        min_data_str = options.get('min_data')
        min_data = None
        if min_data_str:
            try:
                naive = datetime.strptime(min_data_str, '%Y-%m-%d')
            except ValueError as e:
                raise CommandError(
                    f'--min-data inválido (espera YYYY-MM-DD): {e}',
                ) from e
            min_data = timezone.make_aware(naive)

        output = options.get('output')
        output_path = Path(output) if output else None

        path = exportar_labels_retreino(
            min_data=min_data,
            output_path=output_path,
            incluir_humano=not options['sem_humano'],
            incluir_juriscope=not options['sem_juriscope'],
            incluir_csvs_legados=not options['sem_csvs_legados'],
        )

        stats = estatisticas_labels(path)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'CSV gerado: {path}'))
        self.stdout.write('')
        self.stdout.write('Estatísticas:')
        self.stdout.write(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
