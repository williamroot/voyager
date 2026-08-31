"""Preenche `Process.fase_*` com a classe da PUBLICAÇÃO mais recente do processo.

    # medir sem escrever (a régua do antes/depois)
    manage.py backfill_fase --de 9000000 --ate 9020000 --sem-reparo --json

    # a corrida (retomável pelo checkpoint)
    manage.py backfill_fase --sleep 0.1 --parar-ms-linha 12

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('tribunals:backfill_fase:off', True)"

O que é FASE, e por que não é a classe do CNJ (#105, 31/08/2026)
----------------------------------------------------------------
O cabeçalho que o tribunal escreve no diário diz a classe com que o processo
está tramitando NAQUELE momento:

    PODER JUDICIÁRIO JUIZADO ESPECIAL FEDERAL DA 3ª REGIÃO
    CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA (12078)
    Nº 5000472-69.2024.4.03.6202
    EXEQUENTE: … EXECUTADO: INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS

…enquanto o Datajud, para o MESMO CNJ, declara `Procedimento do Juizado
Especial Cível (436)`. Os dois estão certos: um é o cadastro, o outro é a fase.
Medido em amostra uniforme por pk: em 222 de 830 processos conferíveis que
rotulávamos `12078` o CNJ diz outra classe, e **98,6% deles TÊM a fase**,
provada por canal independente do campo que gerou o rótulo.

De onde a fase vem, e de onde NÃO vem
-------------------------------------
Vem de `Movimentacao` com `meio <> 'datajud'` — publicação em diário (DJEN ou
diário próprio). **Movimento do Datajud é excluído de propósito**: o parser
copia a classe CADASTRAL do processo em cada movimento (`datajud/parser.py`),
então incluí-lo faria a fase voltar a ser a classe do CNJ com outro nome — a
mesma colisão, um campo adiante. Foi assim que 5 dos 7 casos medidos de "fase
escondida" tinham `classe_codigo` vazio ou sobrescrito.

`fase_em` é a data da publicação, e o UPDATE só sobe (`>`). Uma recoleta de
2023 não pode rebaixar a fase de 2026 — o backfill de histórico roda o tempo
todo nesta casa.

O que este comando NÃO faz
--------------------------
- **Não toca `classe_codigo`** nem `classe_cnj_*`. São três fatos, três campos.
- **Não reindexa**: toca a CAMPAINHA (`atualizado_em = now()`) e deixa o
  `sync_processos_atualizados` levar ao ES no ritmo do dreno.
- **Não vai à rede.** O dado está no nosso Postgres.
"""
import json
import logging
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger('voyager.tribunals.backfill_fase')

WM = 'tribunals:backfill_fase:wm'
OFF = 'tribunals:backfill_fase:off'
BLOCO_IDS = 20_000

#: A publicação mais recente COM classe de cada processo da faixa. `meio <>
#: 'datajud'` porque movimento do Datajud carrega a classe cadastral (ver
#: docstring). O LATERAL usa `mov_processo_data_disp_idx (processo, -data)`.
SQL_LER = """
SELECT p.id, p.numero_cnj, p.fase_codigo, p.fase_em, x.codigo_classe,
       x.nome_classe, x.data_disponibilizacao
  FROM tribunals_process p
  JOIN LATERAL (
        SELECT m.codigo_classe, m.nome_classe, m.data_disponibilizacao
          FROM tribunals_movimentacao m
         WHERE m.processo_id = p.id
           AND m.codigo_classe <> ''
           AND m.meio <> 'datajud'
         ORDER BY m.data_disponibilizacao DESC
         LIMIT 1
  ) x ON true
 WHERE p.id > %s AND p.id <= %s
   AND (p.fase_em IS NULL OR p.fase_em < x.data_disponibilizacao)
"""


class Command(BaseCommand):
    help = ('Preenche Process.fase_codigo/_nome/_em com a classe da publicação '
            'em diário mais recente. Sem rede. Retomável, com teto e freio.')

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, default=0)
        parser.add_argument('--ate', type=int, default=0)
        parser.add_argument('--bloco', type=int, default=BLOCO_IDS)
        parser.add_argument('--sleep', type=float, default=0.1)
        parser.add_argument('--limite-blocos', type=int, default=0)
        parser.add_argument('--teto-linhas', type=int, default=0)
        parser.add_argument('--freio-ms-linha', type=float, default=8.0)
        parser.add_argument('--parar-ms-linha', type=float, default=12.0)
        parser.add_argument('--lock-timeout', default='5s')
        parser.add_argument('--statement-timeout', default='120s')
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

        tot = {'com_publicacao': 0, 'escritos': 0, 'sobe_fase': 0,
               'fase_difere_da_classe_codigo': 0}
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
            n = self._um_bloco(cur_pk, hi, tot, o)
            dt_ms = (time.monotonic() - tb) * 1000
            cur_pk = hi
            blocos += 1
            if not o['sem_checkpoint']:
                cache.set(WM, cur_pk, None)
            if o['teto_linhas'] and tot['escritos'] >= o['teto_linhas']:
                teto_batido = True
                break
            if n:
                janela.append(dt_ms / n)
                del janela[:-5]
                logger.info('backfill_fase: pk %d-%d · %d linhas · %.2f ms/linha',
                            hi - bloco, hi, n, dt_ms / n)
                if dt_ms / n > o['freio_ms_linha']:
                    sleep = min(sleep * 2, 30.0)
                elif dt_ms / n < o['freio_ms_linha'] / 2 and sleep > o['sleep']:
                    sleep = max(o['sleep'], sleep / 2)
                if len(janela) == 5 and sum(janela) / 5 > o['parar_ms_linha']:
                    custo_caro = True
                    break
            time.sleep(sleep)

        dur = time.monotonic() - t0
        # Regra nº 2: teto atingido é ERRO com o número real, não corte mudo.
        if teto_batido:
            logger.error('backfill_fase PAROU NO TETO: %d linhas (teto=%d), pk %d '
                         'de %d — FALTA de %d a %d', tot['escritos'],
                         o['teto_linhas'], cur_pk, topo, cur_pk, topo)
        if custo_caro:
            logger.error('backfill_fase PAROU POR CUSTO: %.2f ms/linha na média de '
                         '5 blocos (teto %.1f) — FALTA de %d a %d',
                         sum(janela) / len(janela), o['parar_ms_linha'], cur_pk, topo)

        saida = {**tot, 'pk_parada': cur_pk, 'pk_topo': topo, 'blocos': blocos,
                 'segundos': round(dur, 1), 'teto_batido': teto_batido,
                 'custo_caro': custo_caro, 'sem_reparo': o['sem_reparo']}
        if o['json']:
            self.stdout.write(json.dumps(saida))
            return
        self.stdout.write(
            f'com publicação classificada {tot["com_publicacao"]:,} · '
            f'fase nova/mais recente {tot["sobe_fase"]:,} · '
            f'escritos {tot["escritos"]:,}')
        self.stdout.write(
            '  a fase DIFERE do `classe_codigo` gravado em '
            f'{tot["fase_difere_da_classe_codigo"]:,} — é o tamanho da colisão '
            'nesta faixa')
        self.stdout.write(f'{blocos} blocos · {dur:.1f}s · pk {cur_pk:,}/{topo:,}')

    # ------------------------------------------------------------------ #

    def _um_bloco(self, lo: int, hi: int, tot: dict, o) -> int:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            cur.execute(SQL_LER, [lo, hi])
            linhas = cur.fetchall()
        if not linhas:
            return 0
        tot['com_publicacao'] += len(linhas)
        tot['sobe_fase'] += len(linhas)

        quadras = []
        for pk, _cnj, fase_atual, _fase_em, cod, nome, dt in linhas:
            if fase_atual and fase_atual != cod:
                tot['fase_difere_da_classe_codigo'] += 1
            quadras.append((pk, str(cod), (nome or '')[:255], dt))

        if o['sem_reparo']:
            return len(quadras)

        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            for i in range(0, len(quadras), 2000):
                lote = quadras[i:i + 2000]
                vals = ','.join(
                    ['(%s::bigint, %s::varchar, %s::varchar, %s::timestamptz)']
                    * len(lote))
                args = [x for q in lote for x in q]
                cur.execute(
                    # `atualizado_em = now()` é a CAMPAINHA do `sync_es` — SQL
                    # cru não roda o `auto_now`, e o poller de processos é
                    # keyset por ela. E o `WHERE ... IS NULL OR <` repete a
                    # guarda de recência: entre o SELECT e o UPDATE a ingestão
                    # ao vivo pode ter gravado uma fase MAIS nova.
                    f'UPDATE tribunals_process p SET fase_codigo = v.c, '
                    f'fase_nome = v.n, fase_em = v.dt, atualizado_em = now() '
                    f'FROM (VALUES {vals}) AS v(id, c, n, dt) '
                    f'WHERE p.id = v.id AND (p.fase_em IS NULL OR p.fase_em < v.dt)',
                    args)
        tot['escritos'] += len(quadras)
        return len(quadras)
