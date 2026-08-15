"""Confere a varredura contra a fonte: quanto o CNJ declara × quanto temos.

    datajud_conferir_acervo                 # tabela de cobertura
    datajud_conferir_acervo --enfileirar    # e devolve pra fila quem está incompleto

Por que existe: `status='fim'` diz que o LAÇO terminou, não que o acervo está
completo. Um job pode terminar cedo por erro tratado, por cota, ou por um
milissegundo que não coube no fatiamento. A única resposta honesta é contar dos
dois lados — o total declarado pelo tribunal ao CNJ e o nosso `_count` no
`voyager-acervo`.

O total declarado é lido AO VIVO (1 requisição por tribunal): ele cresce todo
dia, e comparar com um número anotado ontem daria falso incompleto.
"""
import django_rq
from django.core.management.base import BaseCommand

from datajud.client import DatajudClient
from search.client import get_es, index_name
from tribunals.models import Tribunal

#: tolerância: abaixo disso o tribunal é considerado incompleto. Não é 100%
#: porque o acervo se move enquanto varremos — processo novo entra, e o nosso
#: número pode ficar acima OU abaixo do declarado por alguns milhares.
LIMIAR = 0.995


class Command(BaseCommand):
    help = 'Compara o acervo declarado ao CNJ com o que a varredura trouxe'

    def add_arguments(self, p):
        p.add_argument('--enfileirar', action='store_true',
                       help='devolve pra fila os tribunais incompletos')
        p.add_argument('--limiar', type=float, default=LIMIAR)

    def handle(self, *a, **o):
        from datajud.jobs import DATAJUD_RETRY, varrer_acervo
        from rq.registry import StartedJobRegistry

        cli = DatajudClient(prefer_cortex=False)
        es = get_es()
        indice = index_name('acervo')
        fila = django_rq.get_queue('varredura')
        rodando = {j.split(':')[-1] for j in StartedJobRegistry(queue=fila).get_job_ids()}
        na_fila = {j.split(':')[-1] for j in fila.get_job_ids()}

        linhas = []
        for sigla in Tribunal.objects.order_by('sigla').values_list('sigla', flat=True):
            try:
                d = cli._post(sigla, {'size': 0, 'track_total_hits': True,
                                      'query': {'match_all': {}}}, cota='varredura')
                declarado = d['hits']['total']['value']
            except Exception as e:  # noqa: BLE001 — índice inexistente (STF) não é falha nossa
                linhas.append((sigla, None, None, None, str(e)[:40]))
                continue
            nosso = es.count(index=indice, query={'term': {'tribunal': sigla}})['count']
            cob = (nosso / declarado) if declarado else 0
            estado = ('rodando' if sigla in rodando
                      else 'na fila' if sigla in na_fila
                      else 'OK' if cob >= o['limiar'] else 'INCOMPLETO')
            linhas.append((sigla, declarado, nosso, cob, estado))

        tot_d = sum(x[1] or 0 for x in linhas)
        tot_n = sum(x[2] or 0 for x in linhas)
        self.stdout.write(f'{"trib":8} {"declarado":>13} {"temos":>13} {"cob":>7}  estado')
        for sigla, dec, nos, cob, est in sorted(
                linhas, key=lambda x: -(x[1] or 0)):
            if dec is None:
                self.stdout.write(f'{sigla:8} {"—":>13} {"—":>13} {"—":>7}  {est}')
                continue
            marca = '✔' if est == 'OK' else ' '
            self.stdout.write(
                f'{sigla:8} {dec:>13,} {nos:>13,} {cob:>6.1%} {marca} {est}')
        self.stdout.write(
            f'\nTOTAL    {tot_d:>13,} {tot_n:>13,} {(tot_n/tot_d if tot_d else 0):>6.1%}')

        if o['enfileirar']:
            faltam = [x[0] for x in linhas
                      if x[4] == 'INCOMPLETO']
            for s in faltam:
                fila.enqueue(varrer_acervo, s, job_id=f'varr:{s}', retry=DATAJUD_RETRY)
            self.stdout.write(self.style.SUCCESS(
                f'\nenfileirados {len(faltam)}: {faltam}'))
