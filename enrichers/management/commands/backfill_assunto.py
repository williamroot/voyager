"""Recupera o código e o nome da FOLHA do assunto — do texto que já está no banco.

    # medir sem escrever nada, uma fatia (o número que eu levo pro antes/depois)
    manage.py backfill_assunto --de 42809753 --ate 42849753 --sem-reparo

    # o catálogo (2.677 linhas): mostra o plano linha a linha
    manage.py backfill_assunto --modo catalogo --sem-reparo

    # a corrida (retomável: sem --de continua do checkpoint)
    manage.py backfill_assunto --sleep 0.2 --freio-ms-linha 15

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('enrichers:backfill_assunto:off', True)"

Por que existe
--------------
O detalhe do PJe entrega o assunto hierárquico com o código da folha SEM o
parêntese de fecho::

    DIREITO PREVIDENCIÁRIO (195)  -  Benefícios em Espécie (6094)  -  \
    Auxílio-Acidente (Art. 86) (6107
                                    ↑ sem ')'

O regex do drainer exigia o fecho, então `assunto_codigo` ia para `''` e
`assunto_nome` recebia a HIERARQUIA INTEIRA truncada em 255. Medido em produção
em 25/08/2026 (12 âncoras, semente 20260825, blocos de 40.000 pks — 480.000
processos varridos):

    processos com `assunto_nome`                209.580
    … sem `assunto_codigo`                      120.755  (57,6%)
    … com o código recuperável DO TEXTO         105.690  (87,5% desses)
    … abstenções (truncado em 255 / sem código)  15.065  (12,5%)

99,9% dos códigos recuperados (105.624 de 105.690) já existem em
`tribunals_assunto`, e 150 de 150 pares (código → nome da folha) sorteados
batem com o `voyager-acervo` — o esqueleto nacional lido do Datajud, fonte
INDEPENDENTE da nossa. O controle negativo do mesmo teste: o nome ATUAL
(hierárquico) bate 0 de 150.

**Nenhuma requisição de rede.** O dado está no nosso banco.

O que este comando NÃO faz
--------------------------
- Não reindexa no Elasticsearch. `bulk_update` não dispara `post_save`, então
  o doc de `voyager-processos` mantém o assunto velho até o reindex passar.
- Não mexe em linha com CONFLITO (o código guardado difere do código no texto:
  418 de 88.825 = 0,47% na amostra). Ali dois writers discordam sobre QUAL
  assunto é o do processo, e escolher no chute seria pior que deixar como está.
- Não faz merge de linha de catálogo cujo código não bate com a folha (21 de
  2.677). `Assunto.codigo` é PK e `Process.assunto` é FK `PROTECT`: merge é
  decisão de arquitetura, não de backfill.
"""
import json
import logging
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from enrichers.drainer import SEP_HIERARQUIA, split_assunto_folha
from tribunals.models import Assunto, Process

logger = logging.getLogger('voyager.enrichers.backfill_assunto')

WM = 'enrichers:backfill_assunto:wm'          # checkpoint (última pk varrida)
OFF = 'enrichers:backfill_assunto:off'        # kill switch
BLOCO_IDS = 20_000                            # pks por bloco (não linhas)


def planejar(pk: int, nome: str, codigo: str, assunto_id) -> tuple[str, dict]:
    """Uma linha de `Process` → (veredito, campos a gravar).

    Vereditos:
      `recupera`   — não tinha código e o texto tem: grava código + folha.
      `folha`      — já tinha código; só o nome vira a folha (o código veio
                     DESTE mesmo texto, então o par é consistente por
                     construção).
      `conflito`   — já tinha código e o texto aponta OUTRO. Abstém.
      `abstem`     — não dá pra provar a folha (truncado em 255 sem `\\n`, ou
                     formato sem código nenhum). Abstém.
      `nada`       — nome já é a folha e o código já está lá.
    """
    truncado = len(nome) == 255
    folha, cod_txt = split_assunto_folha(nome, truncado=truncado)

    if not codigo:
        if not cod_txt:
            return 'abstem', {}
        campos = {'assunto_codigo': cod_txt, 'assunto_nome': folha}
        if assunto_id is None:
            campos['assunto_id'] = cod_txt
        return 'recupera', campos

    if cod_txt and cod_txt != codigo:
        return 'conflito', {}
    if folha == nome:
        # nada a corrigir no nome; ainda vale fechar a FK se ficou órfã
        if assunto_id is None:
            return 'folha', {'assunto_id': codigo}
        return 'nada', {}
    campos = {'assunto_nome': folha}
    if assunto_id is None:
        campos['assunto_id'] = codigo
    return 'folha', campos


class Command(BaseCommand):
    help = ('Recupera assunto_codigo e o nome da FOLHA a partir do texto já '
            'gravado em Process.assunto_nome. Retomável, com teto e freio.')

    def add_arguments(self, parser):
        parser.add_argument('--modo', choices=['processos', 'catalogo'],
                            default='processos')
        parser.add_argument('--de', type=int, default=0,
                            help='pk inicial (exclusivo). 0 = continua do checkpoint.')
        parser.add_argument('--ate', type=int, default=0,
                            help='pk final (inclusivo). 0 = max(id) ao começar.')
        parser.add_argument('--bloco', type=int, default=BLOCO_IDS,
                            help=f'pks por bloco (default {BLOCO_IDS}).')
        parser.add_argument('--sleep', type=float, default=0.2,
                            help='pausa (s) entre blocos. O freio pode aumentá-la.')
        parser.add_argument('--limite-blocos', type=int, default=0)
        parser.add_argument('--teto-linhas', type=int, default=0,
                            help='para depois de N linhas escritas. Atingir o '
                                 'teto é ERRO registrado, não fim silencioso.')
        # O freio mede CUSTO POR LINHA, não duração de bloco. A primeira
        # versão media o bloco inteiro contra 1.500 ms e, na faixa fria de pk
        # baixo, TODO bloco de 20.000 pks leva 30-57 s — o freio disparava
        # sempre e a pausa só subia (0,15 → 4,8 s em 5 blocos), estrangulando
        # a vazão sem que nada estivesse errado. Duração de bloco depende do
        # tamanho do bloco; ms/linha é comparável entre faixas.
        parser.add_argument('--freio-ms-linha', type=float, default=15.0,
                            help='acima disto a pausa DOBRA (cede vazão); '
                                 'abaixo da metade, a pausa volta pela metade. '
                                 'Referência medida: 8,8 ms/linha.')
        parser.add_argument('--parar-ms-linha', type=float, default=17.6,
                            help='média móvel de 5 blocos acima disto = PARA '
                                 'com ERROR. 17,6 = 2x os 8,8 ms medidos.')
        parser.add_argument('--lock-timeout', default='5s')
        parser.add_argument('--statement-timeout', default='60s')
        parser.add_argument('--sem-reparo', action='store_true',
                            help='só mede; não escreve.')
        parser.add_argument('--sem-checkpoint', action='store_true')
        parser.add_argument('--zerar-checkpoint', action='store_true')
        parser.add_argument('--json', action='store_true')

    # ------------------------------------------------------------------ #

    def handle(self, *args, **o):
        if o['zerar_checkpoint']:
            cache.delete(WM)
            self.stdout.write('checkpoint apagado.')
            return
        if o['modo'] == 'catalogo':
            self._catalogo(o)
        else:
            self._processos(o)

    # ---------- catálogo (2.677 linhas, cabe na memória) ---------------- #

    def _catalogo(self, o):
        """Corrige `Assunto.nome` para o nome da FOLHA. NUNCA apaga linha:
        `Assunto.codigo` é PK e `Process.assunto` é FK `on_delete=PROTECT`."""
        linhas = list(Assunto.objects.all().values('codigo', 'nome', 'total_processos'))
        plano, conflitos, abstidos = [], [], []
        for r in linhas:
            nome, cod = r['nome'], r['codigo']
            truncado = len(nome) == 255
            folha, cod_txt = split_assunto_folha(nome, truncado=truncado)
            # a abstenção vem ANTES do `folha == nome`: quando o texto está
            # cortado em 255 sem `\n`, o parser devolve o nome intacto de
            # propósito, e contar isso como "nada a fazer" esconderia 21 linhas
            # que continuam mostrando a hierarquia no dropdown.
            if truncado and '\n' not in nome and SEP_HIERARQUIA in nome:
                abstidos.append((cod, nome))
                continue
            if folha == nome:
                continue
            if cod_txt and cod_txt != cod:
                conflitos.append((cod, cod_txt, nome, r['total_processos']))
                continue
            plano.append((cod, nome, folha, r['total_processos']))

        if o['json']:
            self.stdout.write(json.dumps({
                'linhas': len(linhas), 'corrigir': len(plano),
                'conflitos': len(conflitos), 'abstidos': len(abstidos),
                'exemplos': [{'codigo': c, 'antes': a, 'depois': d}
                             for c, a, d, _ in plano[:10]],
            }, ensure_ascii=False))
        else:
            self.stdout.write(f'catálogo: {len(linhas):,} linhas · '
                              f'corrigir {len(plano):,} · '
                              f'conflitos {len(conflitos)} · abstidos {len(abstidos)}')
            for c, a, d, q in plano[:10]:
                self.stdout.write(f'  {c} ({q} proc)\n    - {a[:120]}\n    + {d}')
            for c, ct, n, q in conflitos:
                self.stdout.write(self.style.WARNING(
                    f'  CONFLITO linha={c} texto={ct} ({q} proc) · {n[:100]}'))
            for c, n in abstidos:
                self.stdout.write(self.style.WARNING(
                    f'  ABSTÉM (truncado em 255) {c} · {n[:100]}'))

        if o['sem_reparo'] or not plano:
            return

        objs = [Assunto(codigo=c, nome=d) for c, _, d, _ in plano]
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            Assunto.objects.bulk_update(objs, ['nome'], batch_size=500)
        logger.info('backfill_assunto catálogo: %d nomes corrigidos, '
                    '%d conflitos abstidos', len(objs), len(conflitos))
        self.stdout.write(self.style.SUCCESS(f'{len(objs):,} nomes corrigidos.'))

    # ---------- processos ---------------------------------------------- #

    def _processos(self, o):
        topo = o['ate'] or (Process.objects.order_by('-id').values_list(
            'id', flat=True).first() or 0)
        cur_pk = o['de'] or (0 if o['sem_checkpoint'] else (cache.get(WM) or 0))
        bloco = max(1, o['bloco'])
        sleep = o['sleep']

        tot = {'lidos': 0, 'recupera': 0, 'folha': 0, 'conflito': 0,
               'abstem': 0, 'nada': 0, 'escritos': 0, 'codigos_novos': 0}
        blocos = 0
        teto_batido = False
        custo_caro = False
        janela: list = []          # ms/linha dos últimos 5 blocos
        t0 = time.monotonic()

        while cur_pk < topo:
            if cache.get(OFF):
                self.stdout.write(self.style.WARNING('kill switch ligado — parando.'))
                break
            if o['limite_blocos'] and blocos >= o['limite_blocos']:
                break

            hi = min(cur_pk + bloco, topo)
            tb = time.monotonic()
            n_escritos, novos = self._um_bloco(cur_pk, hi, tot, o)
            dt_ms = (time.monotonic() - tb) * 1000
            tot['escritos'] += n_escritos
            tot['codigos_novos'] += novos

            cur_pk = hi
            blocos += 1
            if not o['sem_checkpoint']:
                cache.set(WM, cur_pk, None)

            if o['teto_linhas'] and tot['escritos'] >= o['teto_linhas']:
                teto_batido = True
                break

            if n_escritos:
                sleep, caro = self._freio(dt_ms / n_escritos, janela, sleep,
                                          n_escritos, cur_pk - bloco, cur_pk, o)
                if caro:
                    custo_caro = True
                    break
            time.sleep(sleep)

        dur = time.monotonic() - t0
        if custo_caro:
            media = sum(janela) / len(janela)
            logger.error(
                'backfill_assunto PAROU POR CUSTO: %.2f ms/linha na média dos '
                'últimos 5 blocos (teto %.1f, referência medida 8,8) — %d '
                'linhas escritas, pk parada em %d de %d, FALTA de %d a %d',
                media, o['parar_ms_linha'], tot['escritos'], cur_pk, topo,
                cur_pk, topo,
            )
        if teto_batido:
            # Regra nº 2 do CLAUDE.md: teto é ERRO registrado, nunca `return`
            # discreto. O número real vai junto para ninguém achar que acabou.
            logger.error(
                'backfill_assunto PAROU NO TETO: %d linhas escritas (teto=%d), '
                'pk parada em %d de %d — FALTA rodar de %d a %d',
                tot['escritos'], o['teto_linhas'], cur_pk, topo, cur_pk, topo,
            )

        saida = {**tot, 'pk_parada': cur_pk, 'pk_topo': topo, 'blocos': blocos,
                 'segundos': round(dur, 1), 'teto_batido': teto_batido,
                 'custo_caro': custo_caro, 'sleep_final': round(sleep, 2),
                 'ms_linha_janela': [round(x, 2) for x in janela],
                 'sem_reparo': o['sem_reparo']}
        if o['json']:
            self.stdout.write(json.dumps(saida))
            return
        self.stdout.write(
            f'lidos {tot["lidos"]:,} · recupera {tot["recupera"]:,} · '
            f'folha {tot["folha"]:,} · conflito {tot["conflito"]:,} · '
            f'abstem {tot["abstem"]:,} · nada {tot["nada"]:,}')
        self.stdout.write(
            f'escritos {tot["escritos"]:,} · códigos novos no catálogo '
            f'{tot["codigos_novos"]:,} · {blocos} blocos · {dur:.1f}s · '
            f'pk {cur_pk:,}/{topo:,}')
        if teto_batido:
            self.stdout.write(self.style.ERROR(
                f'TETO ATINGIDO em {tot["escritos"]:,} linhas — FALTA de '
                f'{cur_pk:,} a {topo:,}'))
        if custo_caro:
            self.stdout.write(self.style.ERROR(
                f'PAROU POR CUSTO: {sum(janela)/len(janela):.2f} ms/linha — '
                f'FALTA de {cur_pk:,} a {topo:,}'))

    @staticmethod
    def _freio(ms_linha: float, janela: list, sleep: float, n: int,
               lo: int, hi: int, o) -> tuple[float, bool]:
        """Ajusta a pausa pelo CUSTO POR LINHA e diz se é hora de parar.

        Mede ms/linha, não duração de bloco: duração depende do tamanho do
        bloco, ms/linha é comparável entre faixas. A primeira versão comparava
        o bloco inteiro contra 1.500 ms e, na faixa de pk baixo, TODO bloco de
        20.000 pks leva 30-57 s — o freio disparava sempre e a pausa só subia
        (0,15 → 4,8 s em 5 blocos), estrangulando a vazão sem que nada
        estivesse errado.
        """
        janela.append(ms_linha)
        del janela[:-5]
        logger.info('backfill_assunto: pk %d-%d · %d linhas · %.2f ms/linha · '
                    'pausa %.2fs', lo, hi, n, ms_linha, sleep)
        if ms_linha > o['freio_ms_linha']:
            sleep = min(sleep * 2, 30.0)
            logger.warning('backfill_assunto: %.2f ms/linha (> %.1f) — pausa '
                           'sobe para %.2fs', ms_linha, o['freio_ms_linha'], sleep)
        elif ms_linha < o['freio_ms_linha'] / 2 and sleep > o['sleep']:
            # o freio TEM que soltar: sem isso a primeira faixa fria deixa a
            # pausa lá em cima pelo resto da corrida
            sleep = max(o['sleep'], sleep / 2)
        caro = len(janela) == 5 and sum(janela) / 5 > o['parar_ms_linha']
        return sleep, caro

    def _um_bloco(self, lo: int, hi: int, tot: dict, o) -> tuple[int, int]:
        # `SET LOCAL` só vale DENTRO de transação; solto no autocommit ele é
        # descartado com um WARNING e o teto não existe. Foi assim que um
        # `SET` de laço deixou um ALTER TABLE preso atrás de consulta longa e
        # produção foi a 63 sessões esperando Lock em 25/08/2026.
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            cur.execute(
                """
                SELECT id, assunto_nome, assunto_codigo, assunto_id
                FROM tribunals_process
                WHERE id > %s AND id <= %s AND assunto_nome <> ''
                """,
                [lo, hi],
            )
            linhas = cur.fetchall()

        tot['lidos'] += len(linhas)
        objs, precisa_cat = [], {}
        for pk, nome, codigo, assunto_id in linhas:
            veredito, campos = planejar(pk, nome, codigo or '', assunto_id)
            tot[veredito] += 1
            if not campos:
                continue
            if 'assunto_codigo' in campos:
                precisa_cat[campos['assunto_codigo']] = campos['assunto_nome']
            p = Process(id=pk)
            for k, v in campos.items():
                setattr(p, k, v)
            objs.append((p, tuple(sorted(campos))))

        if o['sem_reparo'] or not objs:
            return 0, 0

        novos = self._garantir_catalogo(precisa_cat)

        escritos = 0
        # `bulk_update` exige o MESMO conjunto de campos por chamada
        por_campos: dict = {}
        for p, campos in objs:
            por_campos.setdefault(campos, []).append(p)
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            for campos, lote in por_campos.items():
                Process.objects.bulk_update(lote, list(campos), batch_size=500)
                escritos += len(lote)
        return escritos, novos

    @staticmethod
    def _garantir_catalogo(precisa: dict) -> int:
        """Cria em `Assunto` os códigos que ainda não existem, com o nome da
        FOLHA. 99,9% já existem (medido) — isto é a cauda."""
        if not precisa:
            return 0
        existem = set(Assunto.objects.filter(codigo__in=list(precisa))
                      .values_list('codigo', flat=True))
        faltam = {c: n for c, n in precisa.items() if c not in existem}
        if not faltam:
            return 0
        Assunto.objects.bulk_create(
            [Assunto(codigo=c, nome=(n or c)[:255]) for c, n in faltam.items()],
            ignore_conflicts=True,
        )
        return len(faltam)
