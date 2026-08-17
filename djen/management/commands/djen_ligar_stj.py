"""Liga (ou desliga) a ingestão DJEN do STJ — com a conta na mesa antes de gravar.

Existe porque o STJ é o caso raro em que a fonte inteira custa ZERO código e
100% de decisão. O que se decide aqui não é técnico:

  · o STJ publica ~12.000 movimentações por DIA ÚTIL (medido paginando
    13/08/2026 até o fim: 12.955 em 13 páginas; 12/08: 11.560 em 12) — sozinho
    ele é ~3x um TRF médio;
  · 92% dessas publicações trazem o CNJ da ORIGEM (8.26=TJSP, 8.13=TJMG,
    4.01=TRF1...), e só ~8% o número nativo do STJ (J=3). Isso é o que dá VALOR
    à fonte — a tese que decide o precatório grudada no processo do crédito —
    mas cria um `Process(tribunal=STJ, numero_cnj=<CNJ do TJSP>)` ao lado do
    `Process(tribunal=TJSP, ...)` que já existe, porque a unicidade é
    (tribunal, numero_cnj). Toda query que assume
    `Process.tribunal == Movimentacao.tribunal` passa a mentir;
  · abrir o retroativo até 18/11/2024 são ~5,3 MILHÕES de movimentações
    competindo por I/O com extração e vetorização.

Por isso: fronteira corrente por padrão, retroativo só com flag explícita, e
nada é gravado sem `--confirmar`.

    manage.py djen_ligar_stj                      # mostra o plano, não grava
    manage.py djen_ligar_stj --confirmar          # liga só a fronteira (7 dias)
    manage.py djen_ligar_stj --retroativo --confirmar
    manage.py djen_ligar_stj --desligar --confirmar
"""

import json
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from tribunals.models import IngestionRun, Movimentacao, Tribunal

#: Piso medido do acervo do STJ no DJEN — ver `0049_stj_horizonte_djen`.
INICIO_MEDIDO = date(2024, 11, 18)
#: Quantos dias para trás a fronteira corrente cobre por padrão. Sete dias
#: (~85 mil movimentações) é o suficiente para a fonte "entrar em dia" sem
#: abrir o retroativo — e para medir o estrago nas métricas por tribunal antes
#: de decidir o resto.
DIAS_FRONTEIRA = 7


class Command(BaseCommand):
    help = 'Liga/desliga a ingestão DJEN do STJ, com a fronteira escolhida na mão.'

    def add_arguments(self, parser):
        parser.add_argument('--fronteira', help='YYYY-MM-DD — de onde a ingestão começa')
        parser.add_argument('--retroativo', action='store_true',
                            help=f'abre o acervo inteiro desde {INICIO_MEDIDO} (~5,3M movimentações)')
        parser.add_argument('--desligar', action='store_true', help='volta o STJ para ativo=False')
        parser.add_argument('--confirmar', action='store_true', help='grava (sem isto, só mostra o plano)')

    def handle(self, *args, **o):
        t = Tribunal.objects.filter(sigla='STJ').first()
        if t is None:
            raise CommandError('Tribunal STJ não existe — rode as migrations (0041_seed_superiores).')

        if o['retroativo'] and o['fronteira']:
            raise CommandError('--retroativo e --fronteira são excludentes: escolha um.')

        hoje = date.today()
        if o['desligar']:
            alvo = {'ativo': False}
        elif o['retroativo']:
            alvo = {'ativo': True, 'data_inicio_disponivel': INICIO_MEDIDO}
        else:
            fronteira = (date.fromisoformat(o['fronteira']) if o['fronteira']
                         else hoje - timedelta(days=DIAS_FRONTEIRA))
            if fronteira < INICIO_MEDIDO:
                raise CommandError(
                    f'fronteira {fronteira} é anterior ao acervo do STJ no DJEN ({INICIO_MEDIDO}). '
                    'Use --retroativo se a intenção é abrir tudo.'
                )
            alvo = {'ativo': True, 'data_inicio_disponivel': fronteira}

        dias = ((hoje - alvo['data_inicio_disponivel']).days + 1) if alvo.get('data_inicio_disponivel') else 0
        plano = {
            'antes': {'ativo': t.ativo, 'data_inicio_disponivel': str(t.data_inicio_disponivel),
                      'backfill_concluido_em': str(t.backfill_concluido_em)},
            'depois': {k: str(v) for k, v in alvo.items()},
            'dias_a_ingerir': dias,
            # ~12k/dia útil vezes ~70% de dias úteis. É ordem de grandeza, não promessa.
            'movimentacoes_estimadas': int(dias * 0.7 * 12000),
            'ja_no_banco': {
                'movimentacoes': Movimentacao.objects.filter(tribunal=t).count(),
                'runs': IngestionRun.objects.filter(tribunal=t).count(),
            },
            'gravado': False,
        }
        if o['confirmar']:
            Tribunal.objects.filter(pk=t.pk).update(**alvo)
            plano['gravado'] = True
        else:
            plano['aviso'] = 'nada gravado — repita com --confirmar'
        self.stdout.write(json.dumps(plano, ensure_ascii=False, indent=2))
