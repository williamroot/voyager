"""Preenche `Process.fase_*` com a classe da PUBLICAÇÃO mais recente do processo.

    # medir sem escrever (a régua do antes/depois)
    manage.py backfill_fase --de 9000000 --ate 9020000 --sem-reparo --json

    # a corrida (retomável pelo checkpoint)
    manage.py backfill_fase --sleep 0.1 --parar-ms-linha 12

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('tribunals:backfill_fase:off', True)"

O FREIO MEDE ms POR ID ESCANEADO, NÃO ms POR LINHA ESCRITA (02/09/2026, #119)
-----------------------------------------------------------------------------
A primeira versão dividia o tempo do bloco pelas linhas ENCONTRADAS. Num
trecho de pk já preenchido — que é o estado normal de uma retomada — o bloco
devolve 1 a 10 linhas residuais, e `dt/n` explode: 16.808 ms/linha num bloco
que levou 16,8 s de relógio. O freio interpretava "barato e vazio" como
"carésimo" e mandava PARAR.

Isso não é teoria. Medido em 02/09/2026: o shard `r105_fase_3` (pk 77,22 M →
79,80 M, faixa que uma amostra provou **100% preenchida**) rodou **17
reinícios** em poucas horas — sobe, varre ~6 blocos, bate no freio, sai com
código **0**, o `restart: unless-stopped` do Docker o ressuscita, e como ele
rodava `--sem-checkpoint` voltava sempre para o `--de`. Progresso líquido:
zero, para sempre, queimando I/O de um banco que é disk-I/O-bound. Run verde,
log limpo, número redondo — a assinatura das três perdas do `CLAUDE.md`.

O custo real de um bloco é o que ele custa ao BANCO, e isso é proporcional aos
ids varridos, não às linhas que o `WHERE` deixou passar. Na faixa densa os dois
números coincidem (19.835 linhas em 20.000 ids ⇒ 4,97 ms/linha e 4,93 ms/id),
então os limiares `--freio-ms-linha` / `--parar-ms-linha` continuam querendo
dizer o que sempre quiseram — eles só param de mentir quando a faixa é rala.

CHECKPOINT É POR FAIXA, E SAIR NO TETO É EXIT != 0
--------------------------------------------------
A watermark era UMA chave global: com 4 shards simultâneos eles se
sobrescreviam, e por isso todos rodavam `--sem-checkpoint` — o que faz cada
reinício recomeçar do `--de` e jogar fora horas de varredura. Agora a chave é
`tribunals:backfill_fase:wm:<de>-<ate>`: cada shard tem a sua, e reinício
retoma de onde parou.

E parar no teto (`--teto-linhas`) ou no freio agora sai com código **3**, não
0. Enquanto saía 0, `docker ps -a` mostrava `Exited (0)` para "terminou" e
para "desisti no meio" — dois estados opostos com o mesmo carimbo.

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
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

logger = logging.getLogger('voyager.tribunals.backfill_fase')

WM = 'tribunals:backfill_fase:wm'
OFF = 'tribunals:backfill_fase:off'
BLOCO_IDS = 20_000


def wm_key(de: int, ate: int) -> str:
    """Watermark POR FAIXA. Uma chave global fazia os shards se sobrescreverem
    (e por isso todos rodavam `--sem-checkpoint`, perdendo tudo a cada
    reinício). A faixa está no nome porque a faixa É a identidade do shard."""
    return f'{WM}:{de}-{ate}'

#: A publicação mais recente COM classe de cada processo da faixa. `meio <>
#: 'datajud'` porque movimento do Datajud carrega a classe cadastral (ver
#: docstring). O LATERAL usa `mov_processo_data_disp_idx (processo, -data)`.
SQL_LER = """
SELECT p.id, p.classe_codigo, p.fase_codigo, p.fase_em, x.codigo_classe,
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
        chave = wm_key(o['de'], o['ate'])
        if o['zerar_checkpoint']:
            cache.delete(chave)
            self.stdout.write(f'checkpoint apagado ({chave}).')
            return

        with connection.cursor() as cur:
            cur.execute('SELECT max(id) FROM tribunals_process')
            topo = o['ate'] or (cur.fetchone()[0] or 0)
        # O checkpoint só ADIANTA dentro da faixa deste shard: `--de` continua
        # sendo o piso (quem passa `--de` está declarando onde a faixa começa)
        # e o checkpoint nunca pode empurrar para fora dela.
        cur_pk = o['de']
        if not o['sem_checkpoint']:
            salvo = cache.get(chave)
            if salvo is not None and o['de'] < int(salvo) < topo:
                cur_pk = int(salvo)
                # Vai para o LOG sempre e para o stdout só fora do `--json`:
                # com `--json`, a saída inteira tem que ser um objeto JSON, e
                # uma linha solta na frente quebra quem consome o comando.
                logger.info('backfill_fase: retomando de %d (checkpoint %s); '
                            '--de era %d', cur_pk, chave, o['de'])
                if not o['json']:
                    self.stdout.write(
                        f'retomando de {cur_pk:,} (checkpoint {chave}); '
                        f'--de era {o["de"]:,}')
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
            lo = cur_pk
            hi = min(cur_pk + bloco, topo)
            tb = time.monotonic()
            n = self._um_bloco(lo, hi, tot, o)
            dt_ms = (time.monotonic() - tb) * 1000
            cur_pk = hi
            blocos += 1
            if not o['sem_checkpoint']:
                cache.set(chave, cur_pk, None)
            if o['teto_linhas'] and tot['escritos'] >= o['teto_linhas']:
                teto_batido = True
                break

            # O CUSTO É POR ID VARRIDO, não por linha encontrada. Ver a
            # docstring: dividir pelas linhas faz um bloco já preenchido (o
            # estado normal de uma retomada) parecer 3.000× mais caro do que é,
            # e foi assim que um shard entrou em laço de reinício. E a medição
            # roda MESMO com n == 0: bloco vazio custa I/O igual.
            ms_por_id = dt_ms / max(1, hi - lo)
            janela.append(ms_por_id)
            del janela[:-5]
            logger.info('backfill_fase: pk %d-%d · %d linhas · %.2f ms/id · '
                        '%.0f ms no bloco', lo, hi, n, ms_por_id, dt_ms)
            if ms_por_id > o['freio_ms_linha']:
                sleep = min(sleep * 2, 30.0)
            elif ms_por_id < o['freio_ms_linha'] / 2 and sleep > o['sleep']:
                sleep = max(o['sleep'], sleep / 2)
            if len(janela) == 5 and sum(janela) / 5 > o['parar_ms_linha']:
                custo_caro = True
                break
            time.sleep(sleep)

        dur = time.monotonic() - t0
        # Regra nº 2: teto atingido é ERRO com o número real, não corte mudo.
        motivo = None
        if teto_batido:
            motivo = (f'TETO: {tot["escritos"]:,} linhas (teto={o["teto_linhas"]:,}), '
                      f'FALTA de {cur_pk:,} a {topo:,}')
            logger.error('backfill_fase PAROU NO TETO: %d linhas (teto=%d), pk %d '
                         'de %d — FALTA de %d a %d', tot['escritos'],
                         o['teto_linhas'], cur_pk, topo, cur_pk, topo)
        if custo_caro:
            media = sum(janela) / len(janela)
            motivo = (f'CUSTO: {media:.2f} ms/id na média de 5 blocos '
                      f'(teto {o["parar_ms_linha"]:.1f}), FALTA de {cur_pk:,} '
                      f'a {topo:,}')
            logger.error('backfill_fase PAROU POR CUSTO: %.2f ms/id na média de '
                         '5 blocos (teto %.1f) — FALTA de %d a %d',
                         media, o['parar_ms_linha'], cur_pk, topo)

        saida = {**tot, 'pk_parada': cur_pk, 'pk_topo': topo, 'blocos': blocos,
                 'segundos': round(dur, 1), 'teto_batido': teto_batido,
                 'custo_caro': custo_caro, 'sem_reparo': o['sem_reparo']}
        if o['json']:
            self.stdout.write(json.dumps(saida))
        else:
            self.stdout.write(
                f'com publicação classificada {tot["com_publicacao"]:,} · '
                f'fase nova/mais recente {tot["sobe_fase"]:,} · '
                f'escritos {tot["escritos"]:,}')
            self.stdout.write(
                '  a fase DIFERE do `classe_codigo` gravado em '
                f'{tot["fase_difere_da_classe_codigo"]:,} — é o tamanho da colisão '
                'nesta faixa')
            self.stdout.write(f'{blocos} blocos · {dur:.1f}s · pk {cur_pk:,}/{topo:,}')

        # Sair no teto NÃO é terminar. Enquanto isto saía com código 0, o
        # `docker ps -a` carimbava `Exited (0)` tanto em "varreu a faixa
        # inteira" quanto em "desisti na primeira curva" — e a diferença entre
        # os dois é justamente o que a tabela do CLAUDE.md chama de perda
        # silenciosa.
        if motivo:
            raise CommandError(f'backfill_fase parou antes do fim — {motivo}',
                               returncode=3)

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
        for pk, classe_atual, _fase_atual, _fase_em, cod, nome, dt in linhas:
            # A MEDIÇÃO que motivou a coluna, contada durante a corrida: em
            # quantas linhas a FASE publicada difere do `classe_codigo` que
            # estava gravado. Compara contra `classe_codigo` (o campo de
            # compatibilidade, que é o rótulo do produto hoje) e NÃO contra
            # `fase_codigo` — comparar a fase com ela mesma dá 0 por
            # construção na primeira passada, que é quando o número
            # interessa. Erro cometido e corrigido em 31/08/2026.
            if classe_atual and classe_atual != cod:
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
