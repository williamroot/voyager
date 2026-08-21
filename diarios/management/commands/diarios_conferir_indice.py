"""Confere se a edição COLETADA virou edição BUSCÁVEL — e repara o que faltar.

Entrada manual do mesmo gate que o scheduler roda de 15 em 15 minutos
(`diarios.jobs.conferir_indice`). Existe separado porque o caso que criou o
gate foi uma coleta MANUAL, com o agendamento desligado — e nessa hora ninguém
quer descobrir o buraco pelo log do scheduler.

    manage.py diarios_conferir_indice                       # tudo que falta conferir
    manage.py diarios_conferir_indice --fonte tjsp-dje
    manage.py diarios_conferir_indice --tribunal TJSP --dia 2025-03-12
    manage.py diarios_conferir_indice --tribunal TJSP --dia 2025-03-12 --so-medir

Saída JSON, no mesmo espírito do `diarios_status`: é o que o critério de aceite
confere com `| jq`, sem depender de leitura humana de tabela bonita.
"""

import datetime as dt
import json

from django.core.management.base import BaseCommand, CommandError

from diarios.indice import conferir_dia, reparar_dia
from diarios.jobs import conferir_indice


class Command(BaseCommand):
    help = 'Gate de completude do índice ES para os dias de diário já coletados.'

    def add_arguments(self, parser):
        parser.add_argument('--fonte', help='slug da fonte (ex.: tjsp-dje)')
        parser.add_argument('--tribunal', help='sigla — exige --dia; ignora o carimbo')
        parser.add_argument('--dia', help='YYYY-MM-DD — exige --tribunal')
        parser.add_argument('--limite', type=int, default=20,
                            help='quantos (tribunal, dia) por passada')
        parser.add_argument('--carencia-min', type=int, default=0,
                            help='minutos desde a coleta antes de conferir (0 = agora)')
        parser.add_argument('--so-medir', action='store_true',
                            help='mede os dois lados e NÃO re-enfileira nada')

    def handle(self, *args, **o):
        if bool(o['tribunal']) != bool(o['dia']):
            raise CommandError('--tribunal e --dia andam juntos')

        if o['tribunal']:
            dia = dt.date.fromisoformat(o['dia'])
            saida = conferir_dia(o['tribunal'], dia)
            # Abstenção NUNCA vira reparo: re-enfileirar o dia inteiro porque o
            # ES não respondeu seria transformar "não sei" em 283 mil jobs.
            if saida['faltando'] and not o['so_medir']:
                saida['reparo'] = reparar_dia(o['tribunal'], dia)
        else:
            saida = conferir_indice(
                fonte=o['fonte'], limite=o['limite'],
                carencia_min=o['carencia_min'], reparar=not o['so_medir'],
            )
        self.stdout.write(json.dumps(saida, indent=2, ensure_ascii=False, default=str))
