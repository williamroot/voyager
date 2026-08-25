"""Preenche `Process.grau` a partir do `voyager-acervo` — zero requisição ao CNJ.

    # medir sem escrever (a régua)
    manage.py backfill_grau --de 9000000 --ate 9100000 --sem-reparo --json

    # a corrida (retomável pelo checkpoint)
    manage.py backfill_grau --sleep 0.1 --parar-ms-linha 6

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('datajud:backfill_grau:off', True)"

Por que dá para fazer sem rede
------------------------------
A varredura do Datajud já grava `grau` em cada documento do `voyager-acervo`
(`datajud/varredura.py::doc_do_datajud`) — o índice é NOSSO. Medido em
25/08/2026 por `_count` de termo (nunca `exists`, que conta string vazia como
presente), no índice inteiro:

    G1 203.782.129 · JE 73.791.952 · G2 41.972.803
    TR  14.272.244 · SUP 8.159.129 · TRU    68.645
    soma = 342.046.902 = total do índice ⇒ `grau` presente em 100% dos docs

A chave pública do CNJ (~100 rpm, hoje a **94,4 req/min** medidos) não é tocada.

O CNJ tem MAIS DE UM grau, e `Process.grau` é escalar
-----------------------------------------------------
Medido em 5 faixas de pk (5.000 processos: TJRS, TRF5, TJPR, TJSC, TJMG):

    processos achados no acervo ......... 4.173 / 5.000 = 83,5%
    com 1 documento .......................... 3.277
    com 2 ...................................... 722
    com 3 ...................................... 161
    com 4 ....................................... 13

G1 e G2 do mesmo processo são documentos DIFERENTES no Datajud. Então um campo
escalar precisa de uma regra, e a regra aqui é o **grau de ORIGEM**, porque é
ele que decide o produto: quem nasceu no Juizado Especial paga por RPV, quem
nasceu na vara comum paga por precatório. `TR`/`TRU` são as instâncias
recursais DO juizado, então também indicam origem no juizado.

    JE > TR > TRU > G1 > G2 > SUP

Quem tem só `G2`/`SUP` (177 + 0 na amostra) fica com o que tem: é o que a fonte
sabe, e inventar o G1 que ela não mostrou seria chute.
"""
import json
import logging
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from datajud.ingestion import GRAUS_CONHECIDOS
from search.client import get_es, index_name

logger = logging.getLogger('voyager.datajud.backfill_grau')

WM = 'datajud:backfill_grau:wm'
OFF = 'datajud:backfill_grau:off'
BLOCO_IDS = 20_000
LOTE_ES = 1_000

#: Origem primeiro. Ver a docstring: é o que separa RPV de precatório.
PRIORIDADE = ['JE', 'TR', 'TRU', 'G1', 'G2', 'SUP']


def escolher_grau(graus) -> str:
    """Lista de graus dos documentos de um CNJ → o grau de ORIGEM.

    Valor fora do domínio medido é descartado, não normalizado (regra nº 6).
    """
    vistos = {g for g in graus if g in GRAUS_CONHECIDOS}
    for g in PRIORIDADE:
        if g in vistos:
            return g
    return ''


class Command(BaseCommand):
    help = ('Preenche Process.grau a partir do índice `voyager-acervo`. '
            'Zero requisição ao Datajud. Retomável, com teto e freio.')

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, default=0)
        parser.add_argument('--ate', type=int, default=0)
        parser.add_argument('--bloco', type=int, default=BLOCO_IDS)
        parser.add_argument('--lote-es', type=int, default=LOTE_ES,
                            help=f'CNJs por `terms` (default {LOTE_ES}). '
                                 'Medido: 1.000 custa 109 ms quente e '
                                 '1,0-2,8 s frio.')
        parser.add_argument('--sleep', type=float, default=0.1)
        parser.add_argument('--limite-blocos', type=int, default=0)
        parser.add_argument('--teto-linhas', type=int, default=0)
        parser.add_argument('--freio-ms-linha', type=float, default=8.0)
        parser.add_argument('--parar-ms-linha', type=float, default=12.0)
        parser.add_argument('--es-timeout', type=int, default=60,
                            help='teto de espera do ES. Nada no caminho sem '
                                 'teto de espera (regra nº 7).')
        parser.add_argument('--lock-timeout', default='5s')
        parser.add_argument('--statement-timeout', default='60s')
        parser.add_argument('--sem-reparo', action='store_true')
        parser.add_argument('--sem-checkpoint', action='store_true')
        parser.add_argument('--zerar-checkpoint', action='store_true')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **o):
        if o['zerar_checkpoint']:
            cache.delete(WM)
            self.stdout.write('checkpoint apagado.')
            return

        with connection.cursor() as cur:
            cur.execute('SELECT max(id) FROM tribunals_process')
            topo = o['ate'] or (cur.fetchone()[0] or 0)
        cur_pk = o['de'] or (0 if o['sem_checkpoint'] else (cache.get(WM) or 0))
        bloco = max(1, o['bloco'])
        sleep = o['sleep']

        tot = {'lidos': 0, 'no_acervo': 0, 'fora_do_acervo': 0,
               'multi_grau': 0, 'escritos': 0, 'sem_grau_util': 0}
        por_grau: dict = {}
        blocos, janela = 0, []
        teto_batido = custo_caro = False
        t0 = time.monotonic()

        while cur_pk < topo:
            if cache.get(OFF):
                self.stdout.write(self.style.WARNING('kill switch — parando.'))
                break
            if o['limite_blocos'] and blocos >= o['limite_blocos']:
                break
            hi = min(cur_pk + bloco, topo)
            tb = time.monotonic()
            n = self._um_bloco(cur_pk, hi, tot, por_grau, o)
            dt_ms = (time.monotonic() - tb) * 1000
            cur_pk = hi
            blocos += 1
            if not o['sem_checkpoint']:
                cache.set(WM, cur_pk, None)
            if o['teto_linhas'] and tot['escritos'] >= o['teto_linhas']:
                teto_batido = True
                break
            if n:
                sleep, custo_caro = self._freio(dt_ms / n, janela, sleep, n,
                                                hi - bloco, hi, o)
                if custo_caro:
                    break
            time.sleep(sleep)

        dur = time.monotonic() - t0
        # Regra nº 2: teto é ERRO registrado com o número real, nunca `return`.
        if teto_batido:
            logger.error('backfill_grau PAROU NO TETO: %d linhas (teto=%d), pk '
                         '%d de %d — FALTA de %d a %d', tot['escritos'],
                         o['teto_linhas'], cur_pk, topo, cur_pk, topo)
        if custo_caro:
            logger.error('backfill_grau PAROU POR CUSTO: %.2f ms/linha na média '
                         'de 5 blocos (teto %.1f) — %d linhas, FALTA de %d a %d',
                         sum(janela) / len(janela), o['parar_ms_linha'],
                         tot['escritos'], cur_pk, topo)

        saida = {**tot, 'por_grau': por_grau, 'pk_parada': cur_pk,
                 'pk_topo': topo, 'blocos': blocos, 'segundos': round(dur, 1),
                 'teto_batido': teto_batido, 'custo_caro': custo_caro,
                 'sem_reparo': o['sem_reparo']}
        if o['json']:
            self.stdout.write(json.dumps(saida))
            return
        self.stdout.write(
            f'lidos {tot["lidos"]:,} · no acervo {tot["no_acervo"]:,} · fora '
            f'{tot["fora_do_acervo"]:,} · multi-grau {tot["multi_grau"]:,} · '
            f'escritos {tot["escritos"]:,}')
        self.stdout.write('  ' + ' · '.join(
            f'{k}={v:,}' for k, v in sorted(por_grau.items(), key=lambda x: -x[1])))
        self.stdout.write(f'{blocos} blocos · {dur:.1f}s · pk {cur_pk:,}/{topo:,}')

    # ------------------------------------------------------------------ #

    @staticmethod
    def _freio(ms_linha: float, janela: list, sleep: float, n: int,
               lo: int, hi: int, o) -> tuple[float, bool]:
        """Pausa pelo CUSTO POR LINHA; devolve (pausa, hora de parar?).

        Mede ms/linha e não duração de bloco: duração depende do tamanho do
        bloco, ms/linha é comparável entre faixas. E o freio SOLTA quando o
        custo cai — sem isso a primeira faixa fria deixa a pausa lá em cima
        pelo resto da corrida.
        """
        janela.append(ms_linha)
        del janela[:-5]
        logger.info('backfill_grau: pk %d-%d · %d linhas · %.2f ms/linha',
                    lo, hi, n, ms_linha)
        if ms_linha > o['freio_ms_linha']:
            sleep = min(sleep * 2, 30.0)
        elif ms_linha < o['freio_ms_linha'] / 2 and sleep > o['sleep']:
            sleep = max(o['sleep'], sleep / 2)
        caro = len(janela) == 5 and sum(janela) / 5 > o['parar_ms_linha']
        return sleep, caro

    def _um_bloco(self, lo: int, hi: int, tot: dict, por_grau: dict, o) -> int:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            cur.execute(
                "SELECT id, numero_cnj FROM tribunals_process "
                "WHERE id > %s AND id <= %s AND grau = ''", [lo, hi])
            linhas = cur.fetchall()
        tot['lidos'] += len(linhas)
        if not linhas:
            return 0

        por_cnj: dict = {}
        for pk, cnj in linhas:
            por_cnj.setdefault(cnj, []).append(pk)

        es = get_es()
        indice = index_name('acervo')
        achados: dict = {}
        cnjs = list(por_cnj)
        for i in range(0, len(cnjs), o['lote_es']):
            fatia = cnjs[i:i + o['lote_es']]
            r = es.search(
                index=indice, size=len(fatia) * 4,
                query={'terms': {'proc': fatia}},
                source_includes=['proc', 'grau'],
                track_total_hits=False,
                request_timeout=o['es_timeout'],   # regra nº 7
            )
            for h in r['hits']['hits']:
                s = h['_source']
                achados.setdefault(s.get('proc'), []).append(s.get('grau'))

        pares = []
        for cnj, pks in por_cnj.items():
            graus = achados.get(cnj)
            if not graus:
                tot['fora_do_acervo'] += len(pks)
                continue
            tot['no_acervo'] += len(pks)
            if len({g for g in graus if g}) > 1:
                tot['multi_grau'] += len(pks)
            g = escolher_grau(graus)
            if not g:
                tot['sem_grau_util'] += len(pks)
                continue
            por_grau[g] = por_grau.get(g, 0) + len(pks)
            pares.extend((pk, g) for pk in pks)

        if o['sem_reparo'] or not pares:
            return 0

        # UPDATE ... FROM (VALUES) — uma declaração por lote, sem CASE-WHEN
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            for i in range(0, len(pares), 2000):
                lote = pares[i:i + 2000]
                vals = ','.join(['(%s::bigint, %s::varchar)'] * len(lote))
                args = [x for par in lote for x in par]
                cur.execute(
                    f'UPDATE tribunals_process p SET grau = v.g '
                    f'FROM (VALUES {vals}) AS v(id, g) WHERE p.id = v.id', args)
        tot['escritos'] += len(pares)
        return len(pares)
