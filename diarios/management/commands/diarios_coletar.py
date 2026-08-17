"""Ponto de entrada único da coleta de diários — vale para as quatro fontes.

Existe para que o critério de aceite de cada fonte seja verificável por COMANDO,
não por opinião: quem revisa roda a mesma linha, com a mesma saída JSON, para
qualquer fonte.

    manage.py diarios_coletar tjsp-dje --de 2015-07-15 --ate 2015-07-15 --dry-run
    manage.py diarios_coletar tjsp-dje --de 2015-07-15 --ate 2015-07-15 --catalogar
    manage.py diarios_coletar tjsp-dje --limite 1 --sync
"""

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from diarios.base import catalogar_fonte, coletar_unidade, listar, obter
from diarios.models import EdicaoDiario


class Command(BaseCommand):
    help = 'Cataloga e/ou coleta unidades de uma fonte de diário oficial.'

    def add_arguments(self, parser):
        parser.add_argument('fonte', nargs='?', help='slug da fonte (vazio = lista as registradas)')
        parser.add_argument('--de', help='YYYY-MM-DD')
        parser.add_argument('--ate', help='YYYY-MM-DD')
        parser.add_argument('--catalogar', action='store_true',
                            help='só cataloga (grava EdicaoDiario pendentes), não baixa')
        parser.add_argument('--dry-run', action='store_true',
                            help='enumera o catálogo e imprime, SEM gravar nada')
        parser.add_argument('--limite', type=int, default=0,
                            help='quantas unidades pendentes coletar inline')
        parser.add_argument('--chave', help='coleta uma unidade específica pela chave')
        parser.add_argument('--sync', action='store_true',
                            help='roda inline (default); sem isto e com --enfileirar, vai pra fila')
        parser.add_argument('--enfileirar', action='store_true', help='enfileira em vez de rodar inline')
        parser.add_argument('--sobrepor', action='store_true',
                            help='ignora a janela de exclusividade da fonte (risco de duplicar com o DJEN)')

    def handle(self, *args, **o):
        if not o['fonte']:
            self.stdout.write(json.dumps({'fontes_registradas': listar()}, ensure_ascii=False))
            return
        coletor = obter(o['fonte'])
        saida: dict = {'fonte': coletor.slug,
                       'janela': [str(coletor.janela_inicio), str(coletor.janela_fim)]}

        if o['dry_run']:
            if not (o['de'] and o['ate']):
                raise CommandError('--dry-run exige --de e --ate')
            unidades = list(coletor.catalogar(date.fromisoformat(o['de']),
                                              date.fromisoformat(o['ate'])))
            saida['unidades'] = [
                {'chave': u.chave, 'data': str(u.data), 'tribunal': u.tribunal_sigla,
                 'rotulo': u.rotulo, 'na_janela': coletor.dentro_da_janela(u.data)}
                for u in unidades
            ]
            saida['total'] = len(unidades)
            self.stdout.write(json.dumps(saida, ensure_ascii=False, indent=2))
            return

        if o['de'] and o['ate']:
            saida['catalogo'] = catalogar_fonte(
                coletor, date.fromisoformat(o['de']), date.fromisoformat(o['ate']),
                sobrepor=o['sobrepor'],
            )
        if o['catalogar']:
            self.stdout.write(json.dumps(saida, ensure_ascii=False, indent=2))
            return

        qs = EdicaoDiario.objects.filter(fonte=coletor.slug)
        if o['chave']:
            qs = qs.filter(chave=o['chave'])
        else:
            qs = qs.filter(status__in=[EdicaoDiario.PENDENTE, EdicaoDiario.FALHA]).order_by('-data')
        if o['limite']:
            qs = qs[:o['limite']]

        if o['enfileirar']:
            from diarios.jobs import coletar as job_coletar
            n = 0
            for edicao in qs:
                job_coletar.delay(coletor.slug, edicao.chave, o['sobrepor'])
                n += 1
            saida['enfileiradas'] = n
        else:
            saida['coletas'] = [
                coletar_unidade(coletor, edicao, sobrepor=o['sobrepor']) for edicao in qs
            ]
        self.stdout.write(json.dumps(saida, ensure_ascii=False, indent=2, default=str))
