"""Recupera `classe_codigo` do texto que já está gravado em `Process.classe_nome`.

    # medir sem escrever NADA (a régua do antes/depois)
    manage.py backfill_classe --de 42809753 --ate 42849753 --dry-run

    # a corrida (retomável: sem --de continua do checkpoint)
    manage.py backfill_classe --sleep 0.2 --max-segundos 3600

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('enrichers:backfill_classe:off', True)"

Por que existe
--------------
O regex do drainer era ``^(.+?)\\s*\\((\\d{2,5})\\)?\\s*$``: exigia **dois**
dígitos. A classe mais comum do país tem **um** — ``PROCEDIMENTO COMUM CÍVEL
(7)``. Todo processo que chegou pela porta do enricher com essa classe ficou
com `classe_nome` carregando o `(7)` no texto e `classe_codigo` **vazio**.
Corrigido no drainer em `08d306e` (`CLASSE_COM_CODIGO_RE`), mas o que já estava
gravado continua quebrado — é o que este comando repara.

Medido em 29/08/2026, amostra **uniforme por pk** (200.000 pks sorteados com
`random.Random(20260829)` na faixa [3.520, 105.980.219]; 195.741 existiam):

    com `classe_nome` ................................... 67.569
    … e `classe_codigo` vazio (o BURACO) ................. 4.440
    … recuperável pelo regex novo ........................ 3.477  (78,3%)
    … dos recuperáveis, código de UM dígito ..... 3.477 de 3.477  (100%)
    … que o regex ANTIGO também pegaria ...................... 0
    … abstenções (nome sem código nenhum) .................. 963

Ou seja: **100% do que dá para recuperar era o `{2,5}`**, e o texto era
`PROCEDIMENTO COMUM CÍVEL (7)` em 3.477 de 3.477 (contando as variantes
`[CÍVEL] PROCEDIMENTO COMUM CÍVEL (7)` do TJMG e `Procedimento Comum Cível (7)`).
Extrapolado ao acervo (~103,7 M linhas): **≈ 1,84 M processos**.

**Nenhuma requisição de rede.** O dado está no nosso banco.

A campainha
-----------
O UPDATE carrega `atualizado_em = now()`. `atualizado_em` é `auto_now` e
`auto_now` **só roda em `Model.save()`** — nem `.update()`, nem SQL cru. Como
`sync_processos_atualizados` é keyset por `atualizado_em`, sem a campainha o
código ficaria certo no banco e velho na busca. Ver `tests/test_campainha_sync.py`.

O que este comando NÃO faz
--------------------------
- **Não fecha FK órfã de quem já tem código.** Medido na mesma amostra: 15.499
  de 63.136 processos (24,5%) têm `classe_codigo` preenchido e `classe_id`
  NULL — ≈ 8,2 M linhas, 4,5× este trabalho. É o território de
  `repop_classe_assunto`, e emendar os dois num comando só dobraria a campainha
  (8,2 M reindexações) sem ter medido o impacto disso. Fica **contado e
  relatado**, não consertado.
- **Não corrige nome de linha do catálogo.** `ClasseJudicial.codigo` é PK e
  `Process.classe` é FK `PROTECT`; a classe `7` já existe lá.
- **Não reindexa.** Só toca a campainha e deixa o tique de 10 min trabalhar no
  ritmo do dreno — por isso o freio de fila (`FILA_ALTA`), o mesmo de
  `tocar_campainha`.
"""
import json
import logging
import random
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import OperationalError, connection, transaction

from enrichers.drainer import CLASSE_COM_CODIGO_RE
from tribunals.models import ClasseJudicial

logger = logging.getLogger('voyager.enrichers.backfill_classe')

WM = 'enrichers:backfill_classe:wm'        # checkpoint (última pk varrida)
OFF = 'enrichers:backfill_classe:off'      # kill switch
BLOCO_IDS = 20_000                         # pks por bloco (não linhas)
LOTE_UPDATE = 500                          # linhas por UPDATE
#: acima disto o comando ESPERA em vez de empurrar — engordar a fila do
#: `es_index` não aumenta vazão nenhuma, só esconde o atraso. Mesmo número de
#: `tocar_campainha`.
FILA_ALTA = 150_000
#: sessões esperando `Lock` no banco acima disto ⇒ cede a vez. R98 escreve na
#: MESMA tabela; medir antes de subir a vazão é a regra da casa.
LOCK_ALTO = 20


def planejar(nome: str, codigo: str, classe_id) -> tuple[str, dict]:
    """Uma linha de `Process` → (veredito, campos a gravar).

    Vereditos:
      `recupera`  — não tinha código e o texto tem: grava código, nome limpo e FK.
      `abstem`    — não tinha código e o texto não prova nenhum. Abstém.
      `conflito`  — já tinha código e o texto aponta OUTRO. Abstém (dois
                    escritores discordam; escolher no chute seria pior).
      `fk_orfa`   — já tem código coerente mas `classe_id` é NULL. Contado,
                    NÃO consertado (ver docstring do módulo).
      `nada`      — já está como tem que estar.
    """
    m = CLASSE_COM_CODIGO_RE.match(nome or '')
    cod_txt = m.group(2) if m else ''
    limpo = m.group(1).strip()[:255] if m else (nome or '').strip()[:255]

    if not codigo:
        if not cod_txt:
            return 'abstem', {}
        # o nome perde o `(7)` porque é o que o caminho AO VIVO grava
        # (`normalize_dados`): deixar o código no texto e na coluna duplicaria
        # o dado em dois formatos diferentes na mesma tabela.
        return 'recupera', {'classe_codigo': cod_txt, 'classe_nome': limpo}
    if cod_txt and cod_txt != codigo:
        return 'conflito', {}
    if classe_id is None:
        return 'fk_orfa', {}
    return 'nada', {}


class Command(BaseCommand):
    help = ('Recupera Process.classe_codigo a partir do texto já gravado em '
            'Process.classe_nome. Retomável, com teto, freio e campainha.')

    def add_arguments(self, p):
        p.add_argument('--de', type=int, default=0,
                       help='pk inicial (exclusivo). 0 = continua do checkpoint.')
        p.add_argument('--ate', type=int, default=0,
                       help='pk final (inclusivo). 0 = max(id) ao começar.')
        p.add_argument('--bloco', type=int, default=BLOCO_IDS,
                       help=f'pks por bloco (default {BLOCO_IDS}).')
        p.add_argument('--lote-update', type=int, default=LOTE_UPDATE,
                       help=f'linhas por UPDATE (default {LOTE_UPDATE}). Lote '
                            'pequeno de propósito: a tabela é quente e tem '
                            'outro escritor.')
        p.add_argument('--sleep', type=float, default=0.2,
                       help='pausa (s) entre blocos. O freio pode aumentá-la.')
        p.add_argument('--limite-blocos', type=int, default=0)
        p.add_argument('--teto-linhas', type=int, default=0,
                       help='para depois de N linhas escritas. Atingir o teto é '
                            'ERRO registrado com o número real, não fim silencioso.')
        p.add_argument('--max-segundos', type=int, default=0,
                       help='teto de tempo. 0 = sem teto. Atingi-lo é ERRO.')
        # O freio mede **ms por 1.000 pks VARRIDOS**, não ms por linha escrita.
        # Medido em 29/08/2026 sobre 59 blocos reais de produção:
        #
        #   ms / 1.000 pks .... p50   315 · p90   567 · max 1.093   (3,5x)
        #   ms / linha escrita  p50   8,4 · p90  16,7 · max  95,9   (25x)
        #
        # ms/linha escrita não serve aqui porque o custo é a LEITURA (Index
        # Scan na pkey + filtro no heap, ~18.000 buffers por bloco de 20.000
        # pks) e a densidade de linhas recuperáveis varia por faixa. O shard
        # `d` parou sozinho a 37,56 ms/linha numa faixa que só tinha 1.319
        # recuperáveis em 2.011 lidas — o freio estava reagindo à DENSIDADE, e
        # não a custo nenhum. É o mesmo erro que o backfill de assunto cometeu
        # ao medir duração de bloco: a métrica tem que ser comparável entre
        # faixas.
        p.add_argument('--freio-ms-kpk', type=float, default=1500.0,
                       help='ms por 1.000 pks varridos: acima disto a pausa '
                            'DOBRA; abaixo da metade, cede pela metade. '
                            'Referência medida: p50 315, p90 567.')
        p.add_argument('--parar-ms-kpk', type=float, default=3000.0,
                       help='média móvel de 5 blocos acima disto = PARA com '
                            'ERROR. 3.000 = ~9,5x a mediana medida.')
        p.add_argument('--fila-alta', type=int, default=FILA_ALTA)
        p.add_argument('--lock-alto', type=int, default=LOCK_ALTO,
                       help='sessões esperando Lock acima disto ⇒ espera.')
        p.add_argument('--lock-timeout', default='5s')
        p.add_argument('--statement-timeout', default='60s')
        p.add_argument('--tentativas-deadlock', type=int, default=3)
        p.add_argument('--shard', default='',
                       help='nome do shard. Dá a ele um checkpoint PRÓPRIO '
                            '(`…:wm:<nome>`), para rodar faixas de pk disjuntas '
                            'em paralelo sem uma passada apagar o marco da '
                            'outra. Medido em 29/08: 3 blocos frios custam 5,9 s '
                            'em série e 2,4 s em 3 threads — a storage tem '
                            'profundidade de fila. Faixas TÊM que ser disjuntas: '
                            'o comando não coordena shards.')
        p.add_argument('--com-denominador', action='store_true',
                       help='lê TODA linha com `classe_nome` (não só o buraco) '
                            'para popular os vereditos `nada`/`conflito`/'
                            '`fk_orfa`. Medido em 29/08: 342-1.117 ms por bloco '
                            'de 20.000 pks contra 18-34 ms da leitura estreita '
                            '(20-30x), porque `classe_codigo = %s` entra pelo '
                            '`proc_classe_codigo_idx`. Use para MEDIR, não para '
                            'a corrida.' % "''")
        p.add_argument('--dry-run', action='store_true', dest='dry_run',
                       help='só mede; NÃO escreve nada (nem catálogo, nem '
                            'checkpoint).')
        p.add_argument('--sem-reparo', action='store_true', dest='dry_run',
                       help='sinônimo de --dry-run (nome usado pelos backfills '
                            'irmãos).')
        p.add_argument('--sem-checkpoint', action='store_true')
        p.add_argument('--zerar-checkpoint', action='store_true')
        p.add_argument('--json', action='store_true')

    # ------------------------------------------------------------------ #

    def handle(self, *args, **o):
        wm = f"{WM}:{o['shard']}" if o['shard'] else WM
        if o['zerar_checkpoint']:
            cache.delete(wm)
            self.stdout.write(f'checkpoint apagado ({wm}).')
            return

        topo = o['ate'] or self._topo()
        cur_pk = o['de'] or (0 if o['sem_checkpoint'] else (cache.get(wm) or 0))
        bloco = max(1, o['bloco'])
        sleep = o['sleep']

        tot = {'lidos': 0, 'recupera': 0, 'abstem': 0, 'conflito': 0,
               'fk_orfa': 0, 'nada': 0, 'escritos': 0, 'codigos_novos': 0,
               'deadlocks': 0}
        blocos = esperas = 0
        teto_batido = custo_caro = tempo_estourou = False
        janela: list = []
        t0 = time.monotonic()

        self.stdout.write(
            f'faixa=({cur_pk:,}, {topo:,}] bloco={bloco:,} sleep={sleep}s'
            + ('  DRY-RUN (não escreve)' if o['dry_run'] else ''))
        self.stdout.flush()

        while cur_pk < topo:
            if cache.get(OFF):
                self.stdout.write(self.style.WARNING('kill switch ligado — parando.'))
                break
            if o['limite_blocos'] and blocos >= o['limite_blocos']:
                break

            espera = self._motivo_de_esperar(o)
            if espera:
                esperas += 1
                if esperas % 10 == 1:
                    self.stdout.write(f'  esperando: {espera}')
                    self.stdout.flush()
                time.sleep(15)
                continue

            lo = cur_pk
            hi = min(cur_pk + bloco, topo)
            tb = time.monotonic()
            n_escritos, novos = self._um_bloco(lo, hi, tot, o)
            dt_ms = (time.monotonic() - tb) * 1000
            tot['escritos'] += n_escritos
            tot['codigos_novos'] += novos

            cur_pk = hi
            blocos += 1
            if not (o['sem_checkpoint'] or o['dry_run']):
                cache.set(wm, cur_pk, None)

            if o['teto_linhas'] and tot['escritos'] >= o['teto_linhas']:
                teto_batido = True
                break
            if o['max_segundos'] and (time.monotonic() - t0) >= o['max_segundos']:
                tempo_estourou = True
                break
            # o freio roda em TODO bloco: faixa sem nada a reparar continua
            # custando I/O, e era justamente ela que escapava do `if escritos`.
            sleep, custo_caro = self._freio(
                dt_ms / max(1e-9, (hi - lo) / 1000), janela, sleep,
                n_escritos, lo, hi, o)
            if custo_caro:
                break
            time.sleep(sleep)

        dur = time.monotonic() - t0
        falta = max(0, topo - cur_pk)

        # Regra nº 2 do CLAUDE.md: teto atingido é ERRO registrado com o número
        # REAL, nunca `return` discreto. "Escrevi 300 mil" sem dizer que faltam
        # 40 milhões de pks é o corte mudo de novo.
        for cond, rotulo, extra in (
            (teto_batido, 'TETO DE LINHAS', f'teto={o["teto_linhas"]}'),
            (tempo_estourou, 'TETO DE TEMPO', f'teto={o["max_segundos"]}s'),
            (custo_caro, 'CUSTO DE VARREDURA',
             f'{(sum(janela) / len(janela)) if janela else 0:.0f} ms por 1.000 '
             f'pks na média de 5 blocos, teto {o["parar_ms_kpk"]:.0f}'),
        ):
            if not cond:
                continue
            msg = (f'backfill_classe[{o["shard"] or "unico"}] PAROU NO '
                   f'{rotulo} ({extra}): '
                   f'{tot["escritos"]} linhas escritas, pk parada em {cur_pk} '
                   f'de {topo} — FALTA rodar de {cur_pk} a {topo} '
                   f'({falta} pks)')
            logger.error(msg)
            self.stderr.write(self.style.ERROR(msg))

        saida = {**tot, 'shard': o['shard'], 'checkpoint': wm,
                 'pk_parada': cur_pk, 'pk_topo': topo, 'blocos': blocos,
                 'esperas': esperas, 'segundos': round(dur, 1),
                 'ms_por_linha': round(dur * 1000 / tot['escritos'], 2)
                 if tot['escritos'] else None,
                 'teto_batido': teto_batido, 'tempo_estourou': tempo_estourou,
                 'custo_caro': custo_caro, 'sleep_final': round(sleep, 2),
                 'com_denominador': o['com_denominador'],
                 'ms_kpk_janela': [round(x) for x in janela],
                 'dry_run': o['dry_run']}
        if o['json']:
            self.stdout.write(json.dumps(saida))
            return
        self.stdout.write(
            f'lidos {tot["lidos"]:,} · recupera {tot["recupera"]:,} · '
            f'abstem {tot["abstem"]:,} · conflito {tot["conflito"]:,} · '
            f'fk_orfa {tot["fk_orfa"]:,} (contado, NÃO consertado) · '
            f'nada {tot["nada"]:,}'
            + ('' if o['com_denominador'] else
               '   [leitura estreita: `conflito`/`fk_orfa`/`nada` são 0 por '
               'construção — use --com-denominador para medi-los]'))
        self.stdout.write(
            f'escritos {tot["escritos"]:,} · códigos novos no catálogo '
            f'{tot["codigos_novos"]:,} · deadlocks retentados '
            f'{tot["deadlocks"]:,} · {blocos} blocos · {esperas} esperas · '
            f'{dur:.1f}s · pk {cur_pk:,}/{topo:,}')

    # ------------------------------------------------------------------ #

    @staticmethod
    def _topo() -> int:
        with transaction.atomic(), connection.cursor() as c:
            c.execute("SET LOCAL statement_timeout = '30s';")   # regra nº 7
            c.execute('SELECT max(id) FROM tribunals_process')
            return c.fetchone()[0] or 0

    def _motivo_de_esperar(self, o) -> str:
        """Fila do `es_index` cheia ou banco esperando lock ⇒ cede a vez.

        Os dois números são LIDOS, não estimados; e se a leitura falhar o
        comando NÃO freia por um número inventado.
        """
        try:
            import django_rq
            fila = django_rq.get_queue('es_index').count
        except Exception:
            fila = -1
        if fila > o['fila_alta']:
            return f'fila es_index em {fila:,} (> {o["fila_alta"]:,})'
        try:
            # o `atomic()` não é enfeite: `SET LOCAL` em autocommit é
            # DESCARTADO com um WARNING, e aí nem a sonda do freio tem teto de
            # espera (regra nº 7).
            with transaction.atomic(), connection.cursor() as c:
                c.execute("SET LOCAL statement_timeout = '5s';")
                c.execute("SELECT count(*) FROM pg_stat_activity "
                          "WHERE wait_event_type = 'Lock'")
                travados = c.fetchone()[0]
        except Exception:
            return ''
        if travados > o['lock_alto']:
            return f'{travados} sessões esperando Lock (> {o["lock_alto"]})'
        return ''

    @staticmethod
    def _freio(ms_kpk: float, janela: list, sleep: float, n: int,
               lo: int, hi: int, o) -> tuple[float, bool]:
        """Pausa pelo custo de VARRER; devolve (pausa, hora de parar?).

        A unidade é ms por 1.000 pks varridos — comparável entre faixas, ao
        contrário de duração de bloco (depende do tamanho do bloco) e de
        ms/linha escrita (depende da densidade de linhas quebradas, que varia
        25x). E o freio SOLTA quando o custo cai: sem isso a primeira faixa
        fria deixa a pausa lá em cima pelo resto da corrida.
        """
        janela.append(ms_kpk)
        del janela[:-5]
        logger.info('backfill_classe: pk %d-%d · %d linhas · %.0f ms/1k pks · '
                    'pausa %.2fs', lo, hi, n, ms_kpk, sleep)
        if ms_kpk > o['freio_ms_kpk']:
            sleep = min(sleep * 2, 30.0)
        elif ms_kpk < o['freio_ms_kpk'] / 2 and sleep > o['sleep']:
            sleep = max(o['sleep'], sleep / 2)
        caro = len(janela) == 5 and sum(janela) / 5 > o['parar_ms_kpk']
        return sleep, caro

    def _um_bloco(self, lo: int, hi: int, tot: dict, o) -> tuple[int, int]:
        # `SET LOCAL` só vale DENTRO de transação: solto no autocommit ele é
        # descartado com um WARNING e o teto simplesmente não existe.
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            # A leitura ESTREITA é o default e não é economia estética: os
            # vereditos `nada`/`conflito`/`fk_orfa` exigem `classe_codigo <> ''`
            # por construção, então ler o acervo inteiro só serve para CONTAR
            # denominador — e denominador de proporção se mede por amostra
            # uniforme de pk, não varrendo tudo (`.ia/ACERVO_CNJ.md`).
            filtro = ("" if o['com_denominador']
                      else " AND classe_codigo = ''")
            cur.execute(
                'SELECT id, classe_nome, classe_codigo, classe_id '
                'FROM tribunals_process '
                "WHERE id > %s AND id <= %s AND classe_nome <> ''" + filtro,
                [lo, hi],
            )
            linhas = cur.fetchall()

        tot['lidos'] += len(linhas)
        pares, precisa_cat = [], {}
        for pk, nome, codigo, classe_id in linhas:
            veredito, campos = planejar(nome or '', codigo or '', classe_id)
            tot[veredito] += 1
            if not campos:
                continue
            precisa_cat[campos['classe_codigo']] = campos['classe_nome']
            pares.append((pk, campos['classe_codigo'], campos['classe_nome']))

        if o['dry_run'] or not pares:
            return 0, 0

        # a FK é `PROTECT`: o catálogo tem que existir ANTES do UPDATE.
        novos = self._garantir_catalogo(precisa_cat)
        # ordem total por pk — sem ordem comum entre escritores existe ciclo de
        # espera e o Postgres mata um dos lados (`.ia/PATTERNS.md`).
        pares.sort(key=lambda t: t[0])

        escritos = 0
        for i in range(0, len(pares), max(1, o['lote_update'])):
            escritos += self._um_update(pares[i:i + o['lote_update']], tot, o)
        return escritos, novos

    def _um_update(self, lote: list, tot: dict, o) -> int:
        vals = ','.join(['(%s::bigint, %s::varchar, %s::varchar)'] * len(lote))
        args = [x for par in lote for x in par]
        sql = (
            # `atualizado_em = now()` é a CAMPAINHA do `sync_es`: `auto_now`
            # não roda em SQL cru, e `sync_processos_atualizados` é keyset por
            # `atualizado_em`. Sem isto o código fica certo no banco e velho na
            # busca — foi assim que 2,6 M de partes ficaram invisíveis.
            f'UPDATE tribunals_process p '
            f'SET classe_codigo = v.cod, classe_nome = v.nome, '
            f'    classe_id = v.cod, atualizado_em = now() '
            f'FROM (VALUES {vals}) AS v(id, cod, nome) WHERE p.id = v.id'
        )
        # Deadlock (40P01) é transitório e PODE ser retentado — com teto,
        # backoff+jitter e o número de tentativas REGISTRADO.
        for tentativa in range(1, o['tentativas_deadlock'] + 1):
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
                    cur.execute(
                        f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
                    cur.execute(sql, args)
                return len(lote)
            except OperationalError as e:
                if getattr(e.__cause__, 'sqlstate', None) != '40P01' \
                        or tentativa == o['tentativas_deadlock']:
                    raise
                tot['deadlocks'] += 1
                espera = 0.2 * (2 ** (tentativa - 1)) + random.uniform(0, 0.2)
                logger.warning('backfill_classe: deadlock no lote de %d '
                               '(tentativa %d/%d) — %.2fs e retenta',
                               len(lote), tentativa, o['tentativas_deadlock'],
                               espera)
                time.sleep(espera)
        return 0

    @staticmethod
    def _garantir_catalogo(precisa: dict) -> int:
        """Cria em `ClasseJudicial` os códigos que ainda não existem.

        NUNCA reescreve nome de linha existente: `codigo` é PK, `Process.classe`
        é FK `PROTECT`, e o catálogo é compartilhado com outros escritores.
        """
        if not precisa:
            return 0
        existem = set(ClasseJudicial.objects.filter(codigo__in=list(precisa))
                      .values_list('codigo', flat=True))
        faltam = {c: n for c, n in precisa.items() if c not in existem}
        if not faltam:
            return 0
        ClasseJudicial.objects.bulk_create(
            [ClasseJudicial(codigo=c, nome=(n or c)[:255])
             for c, n in sorted(faltam.items())],
            ignore_conflicts=True,
        )
        return len(faltam)
