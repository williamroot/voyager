"""Snapshot da coleta de diários — o `djen_status` das fontes novas.

Saída JSON de propósito: é o que os critérios de aceite conferem (`| jq`), sem
depender de leitura humana de tabela bonita.
"""

import json

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Sum

from diarios.base import listar, obter
from diarios.models import EdicaoDiario
from tribunals.models import IngestionRun


class Command(BaseCommand):
    help = 'Estado do catálogo/coleta por fonte de diário.'

    def add_arguments(self, parser):
        parser.add_argument('fonte', nargs='?')

    def handle(self, *args, **o):
        fontes = [o['fonte']] if o['fonte'] else sorted(
            set(listar()) | set(EdicaoDiario.objects.values_list('fonte', flat=True).distinct())
        )
        out = {}
        for slug in fontes:
            qs = EdicaoDiario.objects.filter(fonte=slug)
            por_status = {r['status']: r['n'] for r in
                          qs.values('status').annotate(n=Count('id')).order_by()}
            agg = qs.aggregate(gravados=Sum('itens_gravados'), ultima=Max('data'),
                               coletado_em=Max('coletado_em'))
            info = {
                'unidades': qs.count(),
                'por_status': por_status,
                'itens_gravados': agg['gravados'] or 0,
                'ultima_data_catalogada': str(agg['ultima']) if agg['ultima'] else None,
                'ultima_coleta_em': str(agg['coletado_em']) if agg['coletado_em'] else None,
                'runs': IngestionRun.objects.filter(fonte=slug).count(),
                'runs_falha': IngestionRun.objects.filter(
                    fonte=slug, status=IngestionRun.STATUS_FAILED).count(),
            }
            try:
                c = obter(slug)
                info['registrada'] = True
                info['janela'] = [str(c.janela_inicio), str(c.janela_fim)]
                info['destino'] = c.destino
                # A janela é o período em que a fonte é a ÚNICA porta (é o que
                # decide a dedupe); `segmentavel_desde` é o pedaço dela que o
                # parser ATUAL sabe ler. No DEJT os dois divergem em 10 anos
                # (janela 2008, parser 2018 = 49% do inventário), e mostrar só a
                # janela faz o operador achar que o resto está a caminho.
                # Abstenção declarada tem que aparecer na MESMA tela do progresso.
                limite = getattr(c, 'segmentavel_desde', None)
                if limite and (c.janela_inicio is None or limite > c.janela_inicio):
                    info['segmentavel_desde'] = str(limite)
                    info['nota'] = (f'a janela começa em {c.janela_inicio}, mas o parser atual só '
                                    f'lê a partir de {limite} — o anterior NÃO é catalogado')
            except KeyError:
                info['registrada'] = False
            out[slug] = info
        self.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
