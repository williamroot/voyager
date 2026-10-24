"""Vincula o precatório da fila oficial à origem de 1º grau pelo DJEN. DRY-RUN por padrão.

    # medir em todo o estoque (só lê)
    manage.py fila_oficial_vincular_origem --tribunal TJPA
    # gravar (backfill); o cron diário da fila faz o mesmo nos últimos 3 dias
    manage.py fila_oficial_vincular_origem --tribunal TJPA --executar
    manage.py fila_oficial_vincular_origem --tribunal TJPA --executar --dias 30

Ver `filas_oficiais/vinculo.py` (regra, precisão medida, o que fica de fora).
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from filas_oficiais.vinculo import CITACAO_PRECATORIO_SQL, vincular_origens


class Command(BaseCommand):
    help = 'Fila oficial: vínculo precatório → origem pelo DJEN (dry-run por padrão).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default='TJPA', choices=sorted(CITACAO_PRECATORIO_SQL))
        parser.add_argument('--executar', action='store_true', help='sem isto, só mede')
        parser.add_argument('--dias', type=int, default=0,
                            help='só processos com movimentação nos últimos N dias (0 = todos)')

    def handle(self, *args, **o):
        desde = timezone.now() - timedelta(days=o['dias']) if o['dias'] else None
        placar = vincular_origens(o['tribunal'], desde=desde, escrever=o['executar'])
        for k, v in sorted(placar.items()):
            self.stdout.write(f'{k}: {v}')
        if not o['executar']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado. Use --executar.'))
