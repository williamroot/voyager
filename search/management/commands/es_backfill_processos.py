"""Backfill do ÍNDICE de processos — censo dos dois lados, reparo só do que falta.

    # medir sem escrever nada (régua grande, ~30 s)
    manage.py es_backfill_processos --so-amostra

    # censo sem reparo, uma fatia
    manage.py es_backfill_processos --de 65000000 --ate 66000000 --sem-reparo

    # a corrida (retomável: sem --de ele continua do checkpoint)
    manage.py es_backfill_processos --sleep 0.1

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('search:backfill_proc:off', True)"
"""
import json
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand

from search import backfill_processos as bp


class Command(BaseCommand):
    help = ('Confere o índice `voyager-processos` contra o Postgres por faixa de '
            'pk e indexa só o que falta. Retomável, com freio pela latência da busca.')

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, default=0,
                            help='pk inicial (exclusivo). 0 = continua do checkpoint.')
        parser.add_argument('--ate', type=int, default=0,
                            help='pk final (inclusivo). 0 = max(id) na hora de começar.')
        parser.add_argument('--bloco', type=int, default=bp.BLOCO_CENSO,
                            help=f'ids por bloco de censo (default {bp.BLOCO_CENSO}).')
        parser.add_argument('--sleep', type=float, default=0.1,
                            help='pausa (s) entre blocos. O freio pode aumentá-la.')
        parser.add_argument('--limite-blocos', type=int, default=0,
                            help='para depois de N blocos (0 = sem limite).')
        parser.add_argument('--sem-reparo', action='store_true',
                            help='só mede; não escreve no índice.')
        parser.add_argument('--sem-checkpoint', action='store_true',
                            help='não lê nem grava o checkpoint (fatia avulsa).')
        parser.add_argument('--so-amostra', action='store_true',
                            help='só a régua: amostra aleatória por faixa de pk.')
        parser.add_argument('--faixas', type=int, default=8)
        parser.add_argument('--n-amostra', type=int, default=bp.N_AMOSTRA)
        parser.add_argument('--seed', type=int, default=bp.SEED_PADRAO)
        parser.add_argument('--teto-pk', type=int, default=0,
                            help='fixa o topo do espaço de pk na amostra. Sem '
                                 'ele as faixas se movem com a ingestão e a '
                                 'amostra "depois" não compara com a "antes". '
                                 'A medição de 24/08/2026 usou 104317558.')
        parser.add_argument('--zerar-checkpoint', action='store_true')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **o):
        if o['zerar_checkpoint']:
            cache.delete(bp.WM)
            self.stdout.write('checkpoint apagado.')
            return

        if o['so_amostra']:
            r = bp.amostrar(faixas=o['faixas'], n=o['n_amostra'], seed=o['seed'],
                            teto=o['teto_pk'] or None)
            if o['json']:
                self.stdout.write(json.dumps(r, default=str))
                return
            self.stdout.write(
                f'amostra seed={r["seed"]}  pk {r["min_pk"]:,}-{r["max_pk"]:,}'
                + (f'  (teto FIXADO; topo real {r["topo_real"]:,})'
                   if r.get('teto_fixado') else
                   '  (teto MÓVEL - as faixas mudam com a ingestão)'))
            self.stdout.write(f'{"faixa":>5} {"pk lo":>13} {"pk hi":>13} '
                              f'{"existem":>8} {"fora":>7} {"%":>7}')
            for f in r['faixas']:
                fora = '?' if f['fora'] is None else f'{f["fora"]:,}'
                pct = '?' if f['pct'] is None else f'{f["pct"]:.2f}'
                self.stdout.write(f'{f["faixa"]:>5} {f["lo"]:>13,} {f["hi"]:>13,} '
                                  f'{f["existem"]:>8,} {fora:>7} {pct:>7}')
            self.stdout.write(self.style.SUCCESS(
                f'TOTAL {r["existem"]:,} processos amostrados, {r["fora"]:,} fora '
                f'({r["pct"]}%)' + (f' — {r["abstidos"]} faixa(s) SEM RESPOSTA do ES'
                                    if r['abstidos'] else '')))
            return

        t0 = time.monotonic()

        def relatar(pk0, pk1, lidos, fora, indexados, el):
            # progresso por bloco: sem isto uma corrida de horas é uma caixa preta
            taxa = lidos and (pk1 - pk0)
            self.stdout.write(
                f'  id {pk0:>11,}-{pk1:>11,}  lidos {lidos:>6,}  fora {fora:>6,}  '
                f'indexados {indexados:>6,}  {el / 60:.1f}min', ending='\r' if fora == 0 else '\n')
            self.stdout.flush()
            del taxa

        r = bp.rodar(de=o['de'], ate=o['ate'] or None, sleep=o['sleep'],
                     bloco=o['bloco'], reparar=not o['sem_reparo'],
                     limite_blocos=o['limite_blocos'],
                     usar_checkpoint=not o['sem_checkpoint'],
                     relatar=relatar)
        if o['json']:
            self.stdout.write(json.dumps(r, default=str))
            return
        self.stdout.write('')
        self.stdout.write(
            f'lidos {r["lidos"]:,} · FORA do índice {r["fora"]:,} '
            f'({100.0 * r["fora"] / max(r["lidos"], 1):.2f}%) · indexados '
            f'{r["indexados"]:,} · blocos {r["blocos"]:,} · '
            f'{time.monotonic() - t0:.0f}s')
        self.stdout.write(
            f'baseline busca (mediana de {r["baseline"]["n"]} sondas): processos '
            f'{r["baseline"]["processos_ms"]:.0f} ms · conteúdo '
            f'{r["baseline"]["conteudo_ms"]:.0f} ms\n'
            f'pior mediana DURANTE: processos {r["pior_processos_ms"]:.0f} ms · '
            f'conteúdo {r["pior_conteudo_ms"]:.0f} ms · freadas {r["freadas"]} · '
            f'pausas {r["paradas"]} · sleep final {r["sleep_final"]:.2f}s')
        if r['fora'] != r['indexados'] and not o['sem_reparo']:
            self.stdout.write(self.style.ERROR(
                f'ATENÇÃO: {r["fora"] - r["indexados"]:,} processos estavam fora '
                f'do índice e NÃO entraram. Ver o log de ERRO.'))
        if r['parou_por']:
            self.stdout.write(self.style.WARNING(
                f'parou por: {r["parou_por"]} (checkpoint em id={r["ate"]:,})'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'faixa concluída até id={r["ate"]:,}'))
