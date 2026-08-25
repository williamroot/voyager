"""Censo das recusas por FAIXA — o que deixamos de perguntar, e por quê.

Um enricher pode saber, sem gastar requisição, que a fonte dele não tem aquele
processo. O caso medido em 25/08/2026 é o **TJSP**, que roda **eproc** em
paralelo ao e-SAJ: os processos nascidos no eproc têm sequencial de CNJ
começando em `4` e **16 de 16** sondas ao vivo devolveram a mesma página de
70.439 bytes, "Não existem informações disponíveis".

Recusar essa faixa economiza ~10 mil requisições/h e até 8 IPs do pool
COMPARTILHADO por job. Mas recusa que não se anuncia é corte mudo — e corte
mudo é exatamente o `for pagina in range(1, 11)` que custou 43,6% do TJSP por
17 meses. Este comando é a superfície onde a recusa aparece.

    python manage.py enrich_fora_do_esaj
    python manage.py enrich_fora_do_esaj --zerar     # recomeça a contagem

O contador é acumulativo desde o último `--zerar` (hash no Redis, sem TTL):
mede quantas CONSULTAS foram poupadas, não quantos processos distintos existem
na faixa — um mesmo processo recusado duas vezes conta duas. Para o tamanho da
faixa, ver `.ia/OPS.md` §"TJSP: o segundo sistema (eproc)".
"""
from django.core.management.base import BaseCommand

from enrichers.jobs import _FORA_DO_ESAJ_KEY, censo_fora_do_esaj


class Command(BaseCommand):
    help = 'Mostra quantos processos foram recusados por faixa (e por qual motivo).'

    def add_arguments(self, parser):
        parser.add_argument('--zerar', action='store_true',
                            help='apaga o contador e recomeça do zero')

    def handle(self, *args, **opts):
        if opts['zerar']:
            import django_rq
            django_rq.get_connection('default').delete(_FORA_DO_ESAJ_KEY)
            self.stdout.write(self.style.WARNING('contador zerado'))
            return

        censo = censo_fora_do_esaj()
        if not censo:
            self.stdout.write('nenhuma recusa por faixa registrada')
            return

        largura = max(len(k) for k in censo)
        total = 0
        self.stdout.write(f'{"tribunal|motivo".ljust(largura)}  {"consultas poupadas":>19s}')
        for chave, n in sorted(censo.items(), key=lambda kv: -kv[1]):
            total += n
            self.stdout.write(f'{chave.ljust(largura)}  {n:>19,}'.replace(',', '.'))
        self.stdout.write(self.style.SUCCESS(
            f'{"TOTAL".ljust(largura)}  {total:>19,}'.replace(',', '.')))
