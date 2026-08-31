"""Preenche `Process.classe_cnj_*` a partir do `voyager-acervo` — zero requisição.

    # medir sem escrever (a régua do antes/depois)
    manage.py backfill_classe_cnj --de 9000000 --ate 9100000 --sem-reparo --json

    # a corrida (retomável pelo checkpoint)
    manage.py backfill_classe_cnj --sleep 0.1 --parar-ms-linha 12

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('datajud:backfill_classe_cnj:off', True)"

Por que esta coluna existe (#105, medido em 31/08/2026)
------------------------------------------------------
`Process.classe_codigo` tinha três escritores com significados diferentes —
enricher (classe atual no PJe), Datajud (classe cadastral do CNJ) e
`preencher_classe_via_djen` (classe da última publicação). Quem escrevia por
último ganhava, e a contagem do nicho saía com régua torta.

Amostra uniforme por pk (semente 20260831, 40.000 pks, 861 com o rótulo
`12078`): o CNJ diz OUTRA classe em 222 de 830 conferíveis (26,7%) — e em
98,6% deles o processo TEM a fase de cumprimento contra a fazenda, provada
por canal INDEPENDENTE do campo que gerou o rótulo (o texto da publicação
diz verbatim "CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA (12078) Nº
<o CNJ>" em 93,2%; as partes do PJe entregam EXEQUENTE × ente público em mais
4,5%). Não era rótulo errado: eram dois fatos empilhados num campo só.

Um CNJ tem MAIS DE UMA classe no Datajud
----------------------------------------
Isto não é ruído — é a estrutura da fonte, e foi o que fez a medição de 30/08
parecer "37% de rótulo errado". Cada GRAU é um documento próprio no Datajud, e
cada documento carrega a classe DAQUELA instância:

    5005545-67.2020.4.03.6103
      G1   7    Procedimento Comum Cível
      JE   436  Procedimento do Juizado Especial Cível
      TR   460  Recurso Inominado Cível

Medido na mesma amostra: **30,7% dos CNJs têm 2+ documentos e 29,3% têm 2+
classes DISTINTAS**. Perguntar "qual é a classe que o CNJ declara" sem dizer
*de qual grau* é pergunta mal formada.

A regra aqui é a mesma de `Process.grau`: a classe do documento do **grau de
ORIGEM** (`JE > TR > TRU > G1 > G2 > SUP`, `escolher_grau`), porque é a
instância onde o processo nasce e a que se compara com o que o tribunal
publica. Quando o grau de origem não dá para provar (o par `G1+JE` sem
`TR`/`TRU`, 0,64% do acervo) ou quando o mesmo grau traz classes divergentes,
**abstém e conta** — coluna vazia, nunca chute (regra nº 6).

O que este comando NÃO faz
--------------------------
- **Não toca `classe_codigo`.** Aquele campo continua sendo compatibilidade;
  quem quer o cadastro do CNJ lê `classe_cnj_codigo`, quem quer a fase lê
  `fase_codigo` (`backfill_fase`).
- **Não reindexa.** Toca a CAMPAINHA (`atualizado_em = now()`) e deixa o
  `sync_processos_atualizados` levar ao ES no ritmo do dreno.
- **Não vai ao CNJ.** O `voyager-acervo` é nosso; a chave de 100 rpm fica
  inteira para o enriquecimento, que é quem atende usuário.
"""
import json
import logging
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from datajud.management.commands.backfill_grau import escolher_grau
from search.client import get_es, index_name

logger = logging.getLogger('voyager.datajud.backfill_classe_cnj')

WM = 'datajud:backfill_classe_cnj:wm'
OFF = 'datajud:backfill_classe_cnj:off'
BLOCO_IDS = 20_000
LOTE_ES = 1_000


def escolher_classe(docs) -> tuple[str, str, str]:
    """Documentos do CNJ no acervo → (codigo, nome, motivo da abstenção).

    `docs` é uma lista de (grau, classe_codigo, classe_nome).

    Um só código distinto ⇒ é ele, mesmo que o grau não se resolva: a fonte
    não se contradiz e não há o que escolher. Vários ⇒ vale o documento do
    grau de ORIGEM. Se nem isso resolve (grau ambíguo, ou o mesmo grau com
    dois códigos), abstém com o motivo — meia régua é pior que régua nenhuma.
    """
    docs = [(g or '', str(c or ''), n or '') for g, c, n in docs if c]
    if not docs:
        return '', '', 'sem classe no acervo'
    codigos = {c for _, c, _ in docs}
    if len(codigos) == 1:
        c = next(iter(codigos))
        nome = next((n for _, cc, n in docs if cc == c and n), '')
        return c, nome, ''
    grau, motivo = escolher_grau([g for g, _, _ in docs])
    if not grau:
        return '', '', f'multi-classe e {motivo}'
    do_grau = {(c, n) for g, c, n in docs if g == grau}
    if len({c for c, _ in do_grau}) != 1:
        return '', '', 'mesmo grau com classes divergentes'
    c, n = next(iter(sorted(do_grau, key=lambda x: -len(x[1]))))
    return c, n, ''


class Command(BaseCommand):
    help = ('Preenche Process.classe_cnj_codigo/_nome a partir do índice '
            '`voyager-acervo`. Zero requisição ao Datajud. Retomável, com teto.')

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, default=0)
        parser.add_argument('--ate', type=int, default=0)
        parser.add_argument('--bloco', type=int, default=BLOCO_IDS)
        parser.add_argument('--lote-es', type=int, default=LOTE_ES)
        parser.add_argument('--sleep', type=float, default=0.1)
        parser.add_argument('--limite-blocos', type=int, default=0)
        parser.add_argument('--teto-linhas', type=int, default=0)
        parser.add_argument('--freio-ms-linha', type=float, default=8.0)
        parser.add_argument('--parar-ms-linha', type=float, default=12.0)
        parser.add_argument('--es-timeout', type=int, default=60,
                            help='nada no caminho sem teto de espera (regra nº 7)')
        parser.add_argument('--lock-timeout', default='5s')
        parser.add_argument('--statement-timeout', default='60s')
        parser.add_argument('--tribunal', default='',
                            help='restringe a um tribunal (ex.: TRF3)')
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
               'multi_classe': 0, 'escritos': 0, 'abstidos': 0,
               'discorda_da_classe_codigo': 0}
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
                logger.info('backfill_classe_cnj: pk %d-%d · %d linhas · %.2f ms/linha',
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
        # Regra nº 2: teto atingido é ERRO com o número real, nunca corte mudo.
        if teto_batido:
            logger.error('backfill_classe_cnj PAROU NO TETO: %d linhas (teto=%d), '
                         'pk %d de %d — FALTA de %d a %d', tot['escritos'],
                         o['teto_linhas'], cur_pk, topo, cur_pk, topo)
        if custo_caro:
            logger.error('backfill_classe_cnj PAROU POR CUSTO: %.2f ms/linha na '
                         'média de 5 blocos (teto %.1f) — FALTA de %d a %d',
                         sum(janela) / len(janela), o['parar_ms_linha'],
                         cur_pk, topo)

        saida = {**tot, 'pk_parada': cur_pk, 'pk_topo': topo, 'blocos': blocos,
                 'segundos': round(dur, 1), 'teto_batido': teto_batido,
                 'custo_caro': custo_caro, 'sem_reparo': o['sem_reparo']}
        if o['json']:
            self.stdout.write(json.dumps(saida))
            return
        self.stdout.write(
            f'lidos {tot["lidos"]:,} · no acervo {tot["no_acervo"]:,} · fora '
            f'{tot["fora_do_acervo"]:,} · multi-classe {tot["multi_classe"]:,} · '
            f'abstidos {tot["abstidos"]:,} · escritos {tot["escritos"]:,}')
        self.stdout.write(
            f'  discordam do `classe_codigo` que já estava gravado: '
            f'{tot["discorda_da_classe_codigo"]:,}')
        if tot.get('_motivos'):
            self.stdout.write('  abstenções: ' + ' · '.join(
                f'{k}={v:,}' for k, v in tot['_motivos'].items()))
        self.stdout.write(f'{blocos} blocos · {dur:.1f}s · pk {cur_pk:,}/{topo:,}')

    # ------------------------------------------------------------------ #

    def _um_bloco(self, lo: int, hi: int, tot: dict, o) -> int:
        filtro = 'AND tribunal_id = %s' if o['tribunal'] else ''
        args = [lo, hi] + ([o['tribunal']] if o['tribunal'] else [])
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            cur.execute(
                'SELECT id, numero_cnj, classe_codigo FROM tribunals_process '
                f"WHERE id > %s AND id <= %s AND classe_cnj_codigo = '' {filtro}",
                args)
            linhas = cur.fetchall()
        tot['lidos'] += len(linhas)
        if not linhas:
            return 0

        por_cnj: dict = {}
        for pk, cnj, classe_atual in linhas:
            por_cnj.setdefault(cnj, []).append((pk, classe_atual))

        es = get_es()
        indice = index_name('acervo')
        achados: dict = {}
        cnjs = list(por_cnj)
        for i in range(0, len(cnjs), o['lote_es']):
            fatia = cnjs[i:i + o['lote_es']]
            r = es.search(
                index=indice, size=len(fatia) * 6,
                query={'terms': {'proc': fatia}},
                source_includes=['proc', 'grau', 'classe_codigo', 'classe_nome'],
                track_total_hits=False,
                request_timeout=o['es_timeout'],   # regra nº 7
            )
            for h in r['hits']['hits']:
                s = h['_source']
                achados.setdefault(s.get('proc'), []).append(
                    (s.get('grau'), s.get('classe_codigo'), s.get('classe_nome')))

        trios = []
        for cnj, pares in por_cnj.items():
            docs = achados.get(cnj)
            if not docs:
                tot['fora_do_acervo'] += len(pares)
                continue
            tot['no_acervo'] += len(pares)
            if len({c for _, c, _ in docs if c}) > 1:
                tot['multi_classe'] += len(pares)
            cod, nome, motivo = escolher_classe(docs)
            if not cod:
                tot['abstidos'] += len(pares)
                tot.setdefault('_motivos', {})
                tot['_motivos'][motivo] = tot['_motivos'].get(motivo, 0) + len(pares)
                continue
            for pk, classe_atual in pares:
                # a MEDIÇÃO que motivou a coluna, contada durante a corrida:
                # quantas linhas o cadastro do CNJ contradiz.
                if classe_atual and classe_atual != cod:
                    tot['discorda_da_classe_codigo'] += 1
                trios.append((pk, cod, (nome or '')[:255]))

        if o['sem_reparo'] or not trios:
            return 0

        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            for i in range(0, len(trios), 2000):
                lote = trios[i:i + 2000]
                vals = ','.join(['(%s::bigint, %s::varchar, %s::varchar)'] * len(lote))
                args = [x for t in lote for x in t]
                cur.execute(
                    # `atualizado_em = now()` é a CAMPAINHA do `sync_es`: SQL
                    # cru não dispara o `auto_now`, e o poller de processos é
                    # keyset por `atualizado_em`. Sem ela o campo fica certo no
                    # banco e ausente na busca.
                    f'UPDATE tribunals_process p SET classe_cnj_codigo = v.c, '
                    f'classe_cnj_nome = v.n, atualizado_em = now() '
                    f'FROM (VALUES {vals}) AS v(id, c, n) WHERE p.id = v.id', args)
        tot['escritos'] += len(trios)
        return len(trios)
