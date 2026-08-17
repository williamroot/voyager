"""Frescor do Querido Diário por município — o selo que o campo `level` não dá.

POR QUE ESTE COMANDO EXISTE
===========================
A API do Querido Diário tem um campo `level` por município (0 a 3) que parece
dizer "quão bem coberto ele está". Ele MENTE, e a medição de 16/08/2026 é
inequívoca:

  · Fortaleza tem `level="1"` e `total_gazettes=0` — zero gazetas, nunca;
  · São Paulo capital, que é o maior estoque de precatório MUNICIPAL do país,
    tem 20 gazetas no acervo INTEIRO, a última de 24/03/2025;
  · dos 28 maiores municípios, só 9 estavam em dia (gazeta de agosto/2026).

Confiar no `level` sem medir a última data é repetir o erro dos "180 milhões de
PDFs": aceitar o número que a fonte declara sem conferir o que ela entrega.
A única verdade é a DATA DA ÚLTIMA GAZETA, e ela só sai perguntando.

    manage.py entes_frescor                       # os 28 maiores municípios
    manage.py entes_frescor --ibge 2704302 3550308
    manage.py entes_frescor --dias 45             # só quem está atrasado
"""

import json
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from diarios_entes.fontes.querido_diario import QueridoDiarioColetor

#: Os maiores municípios por população (IBGE 7 dígitos) — recorte de trabalho,
#: porque estoque de precatório municipal segue tamanho do ente. Não é lista
#: canônica: passe `--ibge` para medir os entes que interessam à carteira.
MAIORES = {
    '3550308': 'São Paulo/SP',        '3304557': 'Rio de Janeiro/RJ',
    '5300108': 'Brasília/DF',         '2927408': 'Salvador/BA',
    '2304400': 'Fortaleza/CE',        '3106200': 'Belo Horizonte/MG',
    '1302603': 'Manaus/AM',           '4106902': 'Curitiba/PR',
    '2611606': 'Recife/PE',           '5208707': 'Goiânia/GO',
    '1501402': 'Belém/PA',            '4314902': 'Porto Alegre/RS',
    '3518800': 'Guarulhos/SP',        '3509502': 'Campinas/SP',
    '2111300': 'São Luís/MA',         '3304904': 'São Gonçalo/RJ',
    '2704302': 'Maceió/AL',           '3301702': 'Duque de Caxias/RJ',
    '5002704': 'Campo Grande/MS',     '2408102': 'Natal/RN',
    '2211001': 'Teresina/PI',         '3548708': 'São Bernardo do Campo/SP',
    '4209102': 'Joinville/SC',        '3552205': 'Sorocaba/SP',
    '3547809': 'Santo André/SP',      '2507507': 'João Pessoa/PB',
    '5103403': 'Cuiabá/MT',           '3303500': 'Nova Iguaçu/RJ',
}
# Os códigos acima foram conferidos um a um contra o `/cities` da própria API
# (fixture `qd_cities.json`, 5.570 municípios) — digitar IBGE de cabeça erra:
# 3304144 é Queimados, não Nova Iguaçu, e 5103403 é Cuiabá, não Campo Grande.


class Command(BaseCommand):
    help = 'Mede a data da última gazeta de cada município no Querido Diário.'

    def add_arguments(self, parser):
        parser.add_argument('--ibge', nargs='*', help='códigos IBGE de 7 dígitos')
        parser.add_argument('--dias', type=int, default=0,
                            help='lista só quem está com mais de N dias de atraso')

    def handle(self, *args, **o):
        alvos = o['ibge'] or [t for t in MAIORES if t.isdigit() and len(t) == 7]
        frescor = QueridoDiarioColetor().frescor_por_municipio(alvos)
        hoje = date.today()
        corte = hoje - timedelta(days=o['dias']) if o['dias'] else None

        linhas = []
        for tid, ultima in frescor.items():
            d = date.fromisoformat(ultima) if ultima else None
            atraso = (hoje - d).days if d else None
            if corte and d and d > corte:
                continue
            linhas.append({
                'ibge': tid, 'municipio': MAIORES.get(tid, ''),
                'ultima_gazeta': ultima, 'dias_de_atraso': atraso,
                # sem gazeta NENHUMA é diferente de atrasado: o município está
                # no mapa da API mas nunca foi raspado (o caso de Fortaleza).
                'situacao': 'sem_gazeta' if d is None else (
                    'em_dia' if atraso is not None and atraso <= 7 else 'defasado'),
            })
        linhas.sort(key=lambda x: (x['ultima_gazeta'] or '0000-00-00'))
        self.stdout.write(json.dumps({'medido_em': hoje.isoformat(), 'municipios': linhas},
                                     ensure_ascii=False, indent=2))
