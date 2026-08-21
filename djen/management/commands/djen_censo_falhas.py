"""Censo (e limpeza) do FailedJobRegistry das filas RQ.

POR QUE ISTO EXISTE (21/08/2026). O registry de falhas é o cemitério que
ninguém visita. Quando finalmente abriram, ele tinha **4.259** jobs na fila
`default` — e a leitura de olho dos "mais recentes" apontou pro job errado: a
`get_job_ids()` devolve o sorted set na ordem de EXPIRAÇÃO, não de tempo, então
o que parecia "o watchdog morrendo toda vez" eram 11 jobs de 4 dias atrás no
fim da lista, enquanto 4.247 falhas do datajud (99,7%) estavam no meio.

Regras da casa que este comando materializa:
  * censo COMPLETO por função, exceção e dia — nada de "os últimos 5";
  * id órfão contado à parte: o sorted set guarda o id depois de o hash do job
    expirar. Medido na `djen_backfill`: **3.952 de 4.297 (92%)** eram só isso.
    Contá-los como "falha" é inventar problema;
  * apagar é opt-in (`--apagar`), e o padrão é dry-run.

Uso:
    manage.py djen_censo_falhas                      # censo das 2 filas
    manage.py djen_censo_falhas default --apagar     # limpa depois de medir
    manage.py djen_censo_falhas default --apagar --mais-velhas-que 2
"""
import collections

import django_rq
from django.core.management.base import BaseCommand
from django.utils import timezone
from rq.job import Job
from rq.registry import FailedJobRegistry

LOTE = 500


def _censo(fila, ids, corte, func):
    """Percorre o registry em lotes e devolve (contadores, ids alvo, órfãos)."""
    por_func = collections.Counter()
    por_dia = collections.Counter()
    por_erro = collections.Counter()
    orfaos = 0
    alvos = []
    for i in range(0, len(ids), LOTE):
        pedaco = ids[i:i + LOTE]
        for jid, j in zip(pedaco, Job.fetch_many(pedaco, connection=fila.connection),
                          strict=False):
            if j is None:
                orfaos += 1        # hash do job expirou; sobrou o id no zset
                alvos.append((jid, False))   # sem hash pra apagar
                continue
            if func and not j.func_name.startswith(func):
                continue
            if corte and j.ended_at and j.ended_at.replace(tzinfo=corte.tzinfo) > corte:
                continue
            por_func[j.func_name] += 1
            por_dia[j.ended_at.date().isoformat() if j.ended_at else '?'] += 1
            linhas = (j.exc_info or '').strip().splitlines()
            por_erro[linhas[-1][:100] if linhas else '<sem traceback>'] += 1
            alvos.append((jid, True))
    return (por_func, por_dia, por_erro), alvos, orfaos


class Command(BaseCommand):
    help = 'Censo por função/exceção/dia do FailedJobRegistry (e limpeza opt-in).'

    def add_arguments(self, parser):
        parser.add_argument('filas', nargs='*', default=['default', 'djen_backfill'])
        parser.add_argument('--apagar', action='store_true',
                            help='Remove os jobs contados (default: só mede).')
        parser.add_argument('--mais-velhas-que', type=int, default=0,
                            dest='mais_velhas_que',
                            help='Só conta/apaga falhas com ended_at mais antigo '
                                 'que N dias (0 = todas).')
        parser.add_argument('--func', default=None,
                            help='Restringe a jobs cujo func_name começa com isto.')

    def handle(self, *args, filas, apagar, mais_velhas_que, func, **opts):
        corte = (timezone.now() - timezone.timedelta(days=mais_velhas_que)
                 if mais_velhas_que else None)
        for nome in filas:
            self._uma_fila(nome, apagar, corte, func)

    def _uma_fila(self, nome, apagar, corte, func):
        fila = django_rq.get_queue(nome)
        registro = FailedJobRegistry(queue=fila)
        ids = registro.get_job_ids()
        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== fila `{nome}`: {len(ids):,} ids no FailedJobRegistry'))

        (por_func, por_dia, por_erro), alvos, orfaos = _censo(fila, ids, corte, func)

        self.stdout.write(f'  jobs com payload .. {sum(por_func.values()):,}')
        self.stdout.write(f'  ids órfãos ........ {orfaos:,} '
                          f'(hash expirado — não são falhas novas)')
        for titulo, contador, quantos in (
            ('por função', por_func, 15),
            ('por dia', por_dia, 30),
            ('por exceção', por_erro, 10),
        ):
            if not contador:
                continue
            self.stdout.write(f'  {titulo}:')
            itens = (sorted(contador.items()) if titulo == 'por dia'
                     else contador.most_common(quantos))
            for chave, n in itens[:quantos]:
                self.stdout.write(f'    {n:7,}  {chave}')

        if not apagar:
            self.stdout.write(self.style.WARNING(
                f'  dry-run — nada apagado. Use --apagar pra remover '
                f'{len(alvos):,} entradas.'))
            return

        for jid, tem_hash in alvos:
            try:
                # órfão não tem hash: pedir `delete_job` nele só gera ruído de
                # "No such job" — o que importa é sair do sorted set.
                registro.remove(jid, delete_job=tem_hash)
            except Exception as e:  # 1 id ruim não pode parar a limpeza
                self.stderr.write(f'  falhou remover {jid}: {e}')
        self.stdout.write(self.style.SUCCESS(
            f'  removidas {len(alvos):,} entradas de `{nome}`. '
            f'Restam {len(FailedJobRegistry(queue=fila)):,}.'))
