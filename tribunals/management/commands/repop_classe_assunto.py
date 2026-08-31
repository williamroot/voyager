"""Fecha as FKs `classe`/`assunto` que ficaram NULL com o código já gravado.

    # medir sem escrever (a régua do antes/depois)
    manage.py repop_classe_assunto --de 60000000 --ate 60100000 --sem-reparo --json

    # a corrida (retomável pelo checkpoint; shards com faixas DISJUNTAS)
    manage.py repop_classe_assunto --shard a --de 0 --ate 26000000 --sleep 0.1

    # parar de qualquer lugar, sem deploy
    manage.py shell -c "from django.core.cache import cache; \\
        cache.set('tribunals:repop_classe_assunto:off', True)"

O buraco (#104), medido em 31/08/2026 em produção
-------------------------------------------------
    SELECT count(*) FROM tribunals_process
     WHERE classe_codigo IS NOT NULL AND classe_codigo <> '' AND classe_id IS NULL
    -- 8.054.334

`classe_codigo` é a coluna de compatibilidade e `classe_id` é a FK do catálogo
TPU; são o MESMO fato em dois formatos. Três escritores gravam o código sem
fechar a FK — `datajud/ingestion.py::_meta_updates_from_source`,
`datajud/hidratacao.py` e o caminho do enricher —, então o buraco não é um
resíduo histórico: ele se realimenta.

Por que a versão anterior deste comando nunca fechou o buraco
-------------------------------------------------------------
Ela varria assim::

    UPDATE t SET fk = src WHERE ctid IN (SELECT ctid FROM t
                                          WHERE src <> '' AND fk IS NULL LIMIT n)

O `LIMIT` faz o Seq Scan parar cedo **enquanto sobra linha quebrada no começo
da tabela**. Quando a frente esvazia, cada iteração varre mais fundo, e a última
varre os 28 GB inteiros para achar as últimas linhas: o custo é quadrático no
número de iterações. Com 104,1 M de linhas ele não termina — foi por isso que a
pendência ficou em 8,2 M em 30/08 e 8,05 M em 31/08 (a diferença é ingestão ao
vivo, não progresso). Aqui a varredura é por **faixa fechada de pk**, que é o
único acesso barato nesta tabela (ver `.ia/DATA_MODEL.md`, `proc_tribunal_id_idx`).

Sem campainha — de propósito, e medido
--------------------------------------
Os backfills irmãos (`backfill_classe`, `backfill_fase`) carregam
ZERO É FOTO, NÃO ESTADO — por que este backfill é RECORRENTE
------------------------------------------------------------
Medido em 31/08/2026: a corrida completa levou `classe_id IS NULL` de 8.054.334
a **21**, e `assunto_id` a **222**. Trinta minutos depois, sem nenhuma corrida
nova: **25** e **227**.

Os que voltam não são resíduo nem erro: são linhas ANTIGAS reescritas ao vivo
(datajud, hidratação, enricher) por um caminho que grava `classe_codigo` e não
resolve a FK. Espalhadas de pk 13,1 M a 103,3 M, com `atualizado_em` posterior
à passagem do shard.

⇒ Rodar uma vez e declarar "zero" é anunciar um estado que já não vale quando
a frase termina. `--desde-atualizado N` é o modo do tick: varre só o que mudou
nas últimas N horas, pelo `proc_atualizado_em_idx`, em vez de percorrer 104 M
linhas atrás de duas dezenas.

`atualizado_em = now()` porque mexem em campo que o índice de busca serve. Este
**não mexe**: `search/documents.py::processo_to_doc` publica `codigo_classe`
(de `classe_codigo`), `classe_nome`, `assunto`, `assunto_codigo` — e **nenhum**
deles vem da FK. Fechar `classe_id` não muda um byte do documento no ES.

Tocar a campainha aqui custaria **8,05 M de reindexações** para produzir
documentos idênticos aos que já estão no índice. `tests/test_repop_classe_assunto.py`
prende essa afirmação: se alguém puser `classe_id`/`assunto_id` no doc, o teste
quebra e a decisão de não tocar a campainha volta à mesa.

Órfão da TPU é achado, não ruído
--------------------------------
Não existe FK no BANCO (conferido em `pg_constraint` em 31/08/2026:
`tribunals_process` tem **zero** constraints do tipo `f`). Ou seja, o Postgres
aceitaria `classe_id = '99999'` sem catálogo — e quem quebraria seria o ORM, no
`proc.classe`, em produção. Por isso o código confere contra o catálogo **antes**
de ligar e **abstém** quando não acha (regra nº 6 do CLAUDE.md), contando o
órfão por código e por tribunal. Medido em 31/08/2026:

    classe   ..... 8.054.281 linhas · 502 códigos · 0 órfãos
    assunto  ..... 9.773.928 linhas · 604.954 órfãos em 1.255 códigos,
                   82% deles na Justiça do Trabalho (TST 128.078, TRT2 65.552…)

Os órfãos de assunto são a faixa 13xxx/14xxx da TPU trabalhista (`Verbas
Rescisórias`, `Adicional de Insalubridade`, `Horas Extras`) — o catálogo foi
semeado antes de os TRTs entrarem no acervo. `--criar-catalogo` fecha essa
lacuna a partir do nome que já está gravado na própria linha, e **só** quando há
nome: código sem nome vira `orfao_sem_nome`, contado e não inventado.

O que este comando NÃO faz
--------------------------
- **Não escreve `classe_codigo`/`assunto_codigo`.** A FK espelha o código; o
  contrário seria escolher entre três escritores no chute.
- **Não sobrescreve FK já preenchida.** O `COALESCE` no UPDATE repete a guarda
  do SELECT: entre ler e escrever, a ingestão ao vivo pode ter fechado a FK.
  Divergências `classe_id <> classe_codigo` (21.853 linhas em 31/08) ficam
  **contadas e intocadas** — são outro fato, com outro dono.
- **Não vai à rede.** O dado já é nosso.
"""
import json
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass

from django.apps import apps
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection, transaction

logger = logging.getLogger('voyager.tribunals.repop_classe_assunto')

WM = 'tribunals:repop_classe_assunto:wm'      # checkpoint (última pk varrida)
OFF = 'tribunals:repop_classe_assunto:off'    # kill switch
BLOCO_IDS = 50_000                            # pks por bloco (não linhas)
#: linhas por UPDATE. Curto de propósito: a tabela é quente, tem 25 índices e
#: outros escritores. Medido em 31/08/2026 na `tribunals_process`, lote de 500
#: derruba a duração do lote de 35 s para 1,6-2,5 s com a MESMA vazão — lote
#: grande não paga nada e segura lock.
LOTE_UPDATE = 500
#: sessões esperando `Lock` no banco acima disto ⇒ cede a vez.
LOCK_ALTO = 20
#: código que `--criar-catalogo` aceita transformar em linha do catálogo.
#: A TPU é numérica e cabe em 5 dígitos — o maior código do nosso catálogo de
#: assuntos é 57.501. Medido em 31/08/2026 sobre os 604.954 órfãos de assunto:
#: **1.249 códigos (604.822 linhas) passam** e **6 códigos (132 linhas) não**
#: — `99999999`, `40100001`, `40100002`, `40100004`, `40100009`, `40100013`.
#: Sem a guarda, a primeira corrida criou `99999999` no catálogo nacional em
#: menos de dois minutos. Fora do padrão fica ABSTIDO e contado, nunca criado.
TPU_RE = re.compile(r'^\d{1,5}$')
#: erros de lock que PODEM ser retentados, e o contador de cada um.
#: `40P01` = deadlock · `55P03` = lock_not_available (o `lock_timeout` estourou
#: esperando a linha, tipicamente atrás de outro backfill na mesma tabela).
TRANSITORIOS = {'40P01': 'deadlocks', '55P03': 'lock_timeouts'}


@dataclass(frozen=True)
class Par:
    """Um par (coluna de código, FK do catálogo) de uma tabela."""
    fk: str            # coluna da FK no banco  (ex.: 'classe_id')
    src: str           # coluna do código       (ex.: 'classe_codigo')
    nome: str          # coluna do nome         (ex.: 'classe_nome')
    modelo: str        # 'app.Model' do catálogo


ALVOS: dict[str, tuple[str, tuple[Par, ...]]] = {
    'process': ('tribunals_process', (
        Par('classe_id', 'classe_codigo', 'classe_nome', 'tribunals.ClasseJudicial'),
        Par('assunto_id', 'assunto_codigo', 'assunto_nome', 'tribunals.Assunto'),
    )),
    # 1,55 bi de linhas: sempre com `--de/--ate`, nunca a tabela inteira de uma
    # vez. O sweep é o mesmo; o que muda é o tamanho do problema.
    'movimentacao': ('tribunals_movimentacao', (
        Par('classe_id', 'codigo_classe', 'nome_classe', 'tribunals.ClasseJudicial'),
    )),
}


class Command(BaseCommand):
    help = ('Fecha Process.classe/assunto (e Movimentacao.classe) a partir do '
            'código já gravado. Varre por faixa de pk. Sem rede, sem campainha.')

    def add_arguments(self, p):
        p.add_argument('--tabela', choices=sorted(ALVOS), default='process')
        p.add_argument('--de', type=int, default=0,
                       help='pk inicial (exclusivo). 0 = continua do checkpoint.')
        p.add_argument('--ate', type=int, default=0,
                       help='pk final (inclusivo). 0 = max(id) ao começar.')
        p.add_argument('--bloco', type=int, default=BLOCO_IDS,
                       help=f'pks por bloco (default {BLOCO_IDS}).')
        p.add_argument('--lote-update', type=int, default=LOTE_UPDATE,
                       help=f'linhas por UPDATE (default {LOTE_UPDATE}).')
        p.add_argument('--sleep', type=float, default=0.1,
                       help='pausa (s) entre blocos. O freio pode aumentá-la.')
        p.add_argument('--desde-atualizado', type=float, default=0.0,
                       help='Só linhas com `atualizado_em` nas últimas N horas. '
                            'Usa `proc_atualizado_em_idx` em vez de varrer as '
                            '104 M — é o modo do tick RECORRENTE. Ver docstring.')
        p.add_argument('--limite-blocos', type=int, default=0)
        p.add_argument('--teto-linhas', type=int, default=0,
                       help='para depois de N linhas escritas. Atingir o teto é '
                            'ERRO com o número real, não fim silencioso.')
        p.add_argument('--max-segundos', type=int, default=0,
                       help='teto de tempo. 0 = sem teto. Atingi-lo é ERRO.')
        # ms por 1.000 pks VARRIDOS, e não ms por linha escrita: o custo aqui é
        # dominado pela LEITURA (Index Scan na pkey + heap) e a densidade de
        # linhas quebradas varia por faixa. Medido em produção em 31/08/2026,
        # blocos de 50.000 pks na `.101`:
        #
        #   só leitura (--sem-reparo), 1 processo ....  81-99 ms / 1.000 pks
        #   leitura + escrita, 1 shard ...............  455-1.024
        #   leitura + escrita, 5 shards em paralelo ..  até 5.087
        #
        # ⚠️ Esta métrica é CONFUNDIDA pela densidade nesta tabela, e muito: a
        # fração de linhas quebradas vai de 5% a **78%** entre blocos vizinhos
        # (2.559, 8.451, 20.973 e 39.102 linhas em blocos de 50.000 pks). Ela
        # não separa "o banco piorou" de "esta faixa tem mais trabalho", então
        # os tetos são folgados de propósito e servem só como válvula: quem
        # mede degradação de verdade é `_motivo_de_esperar`, que LÊ as sessões
        # em espera de Lock.
        #
        # Calibrei errado duas vezes e as duas custaram: primeiro pela medição
        # SEM escrita (o freio dobrou a pausa contra uma corrida saudável),
        # depois pela medição de UM shard (com 5 em paralelo um bloco denso
        # bate 5.087 e o `--parar` mataria a corrida de madrugada, com o banco
        # perfeitamente são).
        p.add_argument('--freio-ms-kpk', type=float, default=6000.0,
                       help='ms por 1.000 pks varridos: acima disto a pausa '
                            'DOBRA; abaixo da metade, cede. Medido com escrita '
                            'e 5 shards: até 5.087.')
        p.add_argument('--parar-ms-kpk', type=float, default=20000.0,
                       help='média móvel de 5 blocos acima disto = PARA com '
                            'ERROR. 20.000 = ~4x o máximo medido em paralelo.')
        p.add_argument('--lock-alto', type=int, default=LOCK_ALTO,
                       help='sessões esperando Lock acima disto ⇒ espera.')
        # A hipótese era boa e a medição NÃO a sustentou — fica registrado
        # para ninguém refazer o mesmo teste achando que é grátis.
        # `pg_stat_activity` durante a corrida mostra os UPDATEs em
        # `LWLock: WALWrite` / `WALInsert` (não CPU, não lock: `esperando_lock`
        # deu 0), então `synchronous_commit = off` deveria ganhar muito.
        # A/B em faixas vizinhas e densas de 20.000 pks, com os 7 shards e os
        # 4 varredores do #105 rodando:
        #
        #   sync   14.335 linhas / 120,3 s = 119 linhas/s   (8,39 ms/linha)
        #   sync   10.531 linhas /  77,3 s = 136 linhas/s   (7,34 ms/linha)
        #   async  15.489 linhas / 101,6 s = 152 linhas/s   (6,56 ms/linha)
        #   async   1.742 linhas /  30,9 s =  56 linhas/s   (bloco ralo, não
        #                                                    comparável)
        #
        # +19% sobre UMA amostra densa contra duas, dentro do ruído da corrida.
        # Isso NÃO paga trocar durabilidade do último commit num backfill de
        # 10,7 M de linhas, então a flag existe, é opt-in, e **não foi usada
        # na corrida**. Se um dia for: o reparo é idempotente e a régua do fim
        # é a consulta da pendência varrendo a tabela, não o checkpoint.
        p.add_argument('--commit-assincrono', action='store_true',
                       help='`SET LOCAL synchronous_commit = off` no UPDATE. '
                            'Medido: +19%% numa amostra, dentro do ruído — '
                            'opt-in, não usado na corrida. Ver o comentário.')
        p.add_argument('--lock-timeout', default='5s')
        p.add_argument('--statement-timeout', default='120s')
        p.add_argument('--tentativas-deadlock', type=int, default=6,
                       help='tentativas em erro TRANSITÓRIO de lock — deadlock '
                            '(40P01) e lock_timeout (55P03). Ver o comentário '
                            'em `_um_update`.')
        p.add_argument('--criar-catalogo', action='store_true',
                       help='cria a linha do catálogo para código ausente, a '
                            'partir do nome gravado na própria linha. Sem nome '
                            'NÃO cria (abster > chutar).')
        p.add_argument('--shard', default='',
                       help='dá um checkpoint PRÓPRIO (`…:wm:<nome>`) para rodar '
                            'faixas de pk disjuntas em paralelo. O comando não '
                            'coordena shards: as faixas TÊM que ser disjuntas.')
        p.add_argument('--dry-run', action='store_true', dest='dry_run',
                       help='só mede; NÃO escreve nada (nem catálogo, nem checkpoint).')
        p.add_argument('--sem-reparo', action='store_true', dest='dry_run',
                       help='sinônimo de --dry-run (nome dos backfills irmãos).')
        p.add_argument('--sem-checkpoint', action='store_true')
        p.add_argument('--zerar-checkpoint', action='store_true')
        p.add_argument('--json', action='store_true')

    # ------------------------------------------------------------------ #

    def handle(self, *args, **o):
        tabela, pares = ALVOS[o['tabela']]
        wm = f"{WM}:{o['tabela']}" + (f":{o['shard']}" if o['shard'] else '')
        if o['zerar_checkpoint']:
            cache.delete(wm)
            self.stdout.write(f'checkpoint apagado ({wm}).')
            return
        if o['bloco'] < 1 or o['lote_update'] < 1:
            raise CommandError('--bloco e --lote-update têm que ser >= 1')

        # catálogos em memória: 662 classes + 3.076 assuntos. `_ver_catalogo`
        # confirma no banco antes de declarar órfão — outro escritor pode ter
        # criado a linha depois deste set ser carregado.
        self._cat = {par.fk: self._codigos(par) for par in pares}
        self._nao_existem = {par.fk: set() for par in pares}
        self._sem_nome = {par.fk: set() for par in pares}
        self._fora_do_padrao = {par.fk: set() for par in pares}

        topo = o['ate'] or self._topo(tabela, o)
        cur_pk = o['de'] or (0 if o['sem_checkpoint'] else (cache.get(wm) or 0))
        sleep = o['sleep']

        tot = {'lidos': 0, 'escritos': 0, 'deadlocks': 0, 'lock_timeouts': 0,
               'catalogo_criado': 0}
        for par in pares:
            tot[f'liga_{par.fk}'] = 0
            tot[f'orfao_{par.fk}'] = 0
            tot[f'orfao_sem_nome_{par.fk}'] = 0
            tot[f'orfao_fora_do_padrao_{par.fk}'] = 0
        orfaos_cod: Counter = Counter()      # (fk, codigo)   -> n
        orfaos_trib: Counter = Counter()     # (fk, tribunal) -> n

        blocos = esperas = 0
        teto_batido = custo_caro = tempo_estourou = False
        janela: list = []
        t0 = time.monotonic()

        self.stdout.write(
            f'{tabela} faixa=({cur_pk:,}, {topo:,}] bloco={o["bloco"]:,} '
            f'lote={o["lote_update"]} sleep={sleep}s'
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

            lo, hi = cur_pk, min(cur_pk + o['bloco'], topo)
            tb = time.monotonic()
            n = self._um_bloco(tabela, pares, lo, hi, tot,
                               orfaos_cod, orfaos_trib, o)
            dt_ms = (time.monotonic() - tb) * 1000
            tot['escritos'] += n

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
            sleep, custo_caro = self._freio(
                dt_ms / max(1e-9, (hi - lo) / 1000), janela, sleep, n, lo, hi, o)
            if custo_caro:
                break
            time.sleep(sleep)

        dur = time.monotonic() - t0
        falta = max(0, topo - cur_pk)

        # Regra nº 2 do CLAUDE.md: teto atingido é ERRO com o número REAL.
        for cond, rotulo, extra in (
            (teto_batido, 'TETO DE LINHAS', f'teto={o["teto_linhas"]}'),
            (tempo_estourou, 'TETO DE TEMPO', f'teto={o["max_segundos"]}s'),
            (custo_caro, 'CUSTO DE VARREDURA',
             f'{(sum(janela) / len(janela)) if janela else 0:.0f} ms por 1.000 '
             f'pks na média de 5 blocos, teto {o["parar_ms_kpk"]:.0f}'),
        ):
            if not cond:
                continue
            msg = (f'repop_classe_assunto[{o["tabela"]}/{o["shard"] or "unico"}] '
                   f'PAROU NO {rotulo} ({extra}): {tot["escritos"]} linhas '
                   f'escritas, pk parada em {cur_pk} de {topo} — FALTA rodar de '
                   f'{cur_pk} a {topo} ({falta} pks)')
            logger.error(msg)
            self.stderr.write(self.style.ERROR(msg))

        # Órfão também é teto: silenciar é o corte mudo com outro nome.
        for par in pares:
            n_orf = (tot[f'orfao_{par.fk}'] + tot[f'orfao_sem_nome_{par.fk}']
                     + tot[f'orfao_fora_do_padrao_{par.fk}'])
            if not n_orf:
                continue
            piores = ', '.join(
                f'{cod}={n}' for (fk, cod), n in orfaos_cod.most_common()
                if fk == par.fk)[:400]
            msg = (f'repop_classe_assunto[{o["tabela"]}/{o["shard"] or "unico"}] '
                   f'ABSTEVE em {n_orf} linhas de `{par.fk}`: o código não está '
                   f'em {par.modelo} — {piores}')
            logger.error(msg)
            self.stderr.write(self.style.ERROR(msg))

        saida = {**tot, 'tabela': o['tabela'], 'shard': o['shard'],
                 'checkpoint': wm, 'pk_parada': cur_pk, 'pk_topo': topo,
                 'blocos': blocos, 'esperas': esperas, 'segundos': round(dur, 1),
                 'ms_por_linha': round(dur * 1000 / tot['escritos'], 2)
                 if tot['escritos'] else None,
                 'teto_batido': teto_batido, 'tempo_estourou': tempo_estourou,
                 'custo_caro': custo_caro, 'sleep_final': round(sleep, 2),
                 'orfaos_por_codigo': {f'{fk}:{cod}': n
                                       for (fk, cod), n in orfaos_cod.most_common(40)},
                 'orfaos_por_tribunal': {f'{fk}:{t}': n
                                         for (fk, t), n in orfaos_trib.most_common(40)},
                 'ms_kpk_janela': [round(x) for x in janela],
                 'dry_run': o['dry_run']}
        if o['json']:
            self.stdout.write(json.dumps(saida, ensure_ascii=False))
            return
        self.stdout.write(f'lidos {tot["lidos"]:,} · escritos {tot["escritos"]:,}')
        for par in pares:
            self.stdout.write(
                f'  {par.fk}: liga {tot[f"liga_{par.fk}"]:,} · '
                f'órfão da TPU {tot[f"orfao_{par.fk}"]:,} · '
                f'órfão sem nome {tot[f"orfao_sem_nome_{par.fk}"]:,} · '
                f'fora do padrão {tot[f"orfao_fora_do_padrao_{par.fk}"]:,}')
        self.stdout.write(
            f'catálogo criado {tot["catalogo_criado"]:,} · deadlocks retentados '
            f'{tot["deadlocks"]:,} · lock_timeouts retentados '
            f'{tot["lock_timeouts"]:,} · {blocos} blocos · {esperas} esperas · '
            f'{dur:.1f}s · pk {cur_pk:,}/{topo:,}')

    # ------------------------------------------------------------------ #

    @staticmethod
    def _codigos(par: Par) -> set:
        return set(apps.get_model(par.modelo).objects
                   .values_list('codigo', flat=True))

    def _topo(self, tabela: str, o) -> int:
        with transaction.atomic(), connection.cursor() as c:
            c.execute("SET LOCAL statement_timeout = '30s';")   # regra nº 7
            c.execute(f'SELECT max(id) FROM {tabela}')
            return c.fetchone()[0] or 0

    def _motivo_de_esperar(self, o) -> str:
        """Banco esperando lock ⇒ cede a vez. O número é LIDO, não estimado.

        Não há freio de fila `es_index` aqui porque este comando **não toca a
        campainha**: nada do que ele escreve aparece no documento do ES.
        """
        try:
            # `atomic()` não é enfeite: `SET LOCAL` em autocommit é DESCARTADO,
            # e aí nem a sonda do freio tem teto de espera (regra nº 7).
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
        """Pausa pelo custo de VARRER; devolve (pausa, hora de parar?)."""
        janela.append(ms_kpk)
        del janela[:-5]
        logger.info('repop_classe_assunto: pk %d-%d · %d linhas · %.0f ms/1k pks '
                    '· pausa %.2fs', lo, hi, n, ms_kpk, sleep)
        if ms_kpk > o['freio_ms_kpk']:
            sleep = min(sleep * 2, 30.0)
        elif ms_kpk < o['freio_ms_kpk'] / 2 and sleep > o['sleep']:
            sleep = max(o['sleep'], sleep / 2)
        caro = len(janela) == 5 and sum(janela) / 5 > o['parar_ms_kpk']
        return sleep, caro

    # ------------------------------------------------------------------ #

    def _um_bloco(self, tabela: str, pares: tuple[Par, ...], lo: int, hi: int,
                  tot: dict, orfaos_cod: Counter, orfaos_trib: Counter,
                  o) -> int:
        cols = ['id', 'tribunal_id']
        for par in pares:
            cols += [par.src, par.nome, par.fk]
        alvo = ' OR '.join(f"({par.src} <> '' AND {par.fk} IS NULL)"
                           for par in pares)
        with transaction.atomic(), connection.cursor() as cur:
            # `SET LOCAL` só vale DENTRO de transação.
            cur.execute(f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
            recente = ''
            params = [lo, hi]
            if o.get('desde_atualizado'):
                # O índice `proc_atualizado_em_idx` cobre isto; sem ele, o
                # planner cairia num Parallel Seq Scan de 104 M por causa de
                # ~25 linhas.
                recente = ' AND atualizado_em > now() - %s::interval'
                params.append(f"{o['desde_atualizado']} hours")
            cur.execute(
                f'SELECT {", ".join(cols)} FROM {tabela} '
                f'WHERE id > %s AND id <= %s AND ({alvo}){recente}', params)
            linhas = cur.fetchall()

        tot['lidos'] += len(linhas)
        if not linhas:
            return 0

        # 1ª passada: quais códigos deste bloco o catálogo ainda não conhece.
        # Uma consulta por par, só com os desconhecidos — nunca por linha.
        for i, par in enumerate(pares):
            vistos = {}
            for row in linhas:
                cod, nome, fk_atual = row[2 + i * 3], row[3 + i * 3], row[4 + i * 3]
                if cod and fk_atual is None and cod not in self._cat[par.fk] \
                        and cod not in self._nao_existem[par.fk]:
                    # o primeiro nome NÃO-vazio ganha: a mesma classe aparece
                    # com nome cheio em milhares de linhas e vazio em dezenas
                    if not vistos.get(cod):
                        vistos[cod] = (nome or '').strip()
            if vistos:
                self._ver_catalogo(par, vistos, tot, o)

        # 2ª passada: planeja o que ligar e conta o que abstém.
        planos = []
        for row in linhas:
            pk, trib = row[0], row[1]
            valores, algum = [], False
            for i, par in enumerate(pares):
                cod, nome, fk_atual = row[2 + i * 3], row[3 + i * 3], row[4 + i * 3]
                if not cod or fk_atual is not None:
                    valores.append(None)
                    continue
                if cod in self._cat[par.fk]:
                    valores.append(cod)
                    tot[f'liga_{par.fk}'] += 1
                    algum = True
                    continue
                # abstenção declarada, com o motivo separado: sem linha no
                # catálogo × sem nome para criá-la × fora do padrão da TPU
                if cod in self._fora_do_padrao[par.fk]:
                    chave = 'orfao_fora_do_padrao_' + par.fk
                elif cod in self._sem_nome[par.fk]:
                    chave = 'orfao_sem_nome_' + par.fk
                else:
                    chave = 'orfao_' + par.fk
                tot[chave] += 1
                orfaos_cod[(par.fk, cod)] += 1
                orfaos_trib[(par.fk, trib)] += 1
                valores.append(None)
            if algum:
                planos.append((pk, valores))

        if o['dry_run'] or not planos:
            return 0

        # ordem total por pk — sem ordem comum entre escritores existe ciclo de
        # espera e o Postgres mata um dos lados (`.ia/PATTERNS.md`).
        planos.sort(key=lambda t: t[0])
        escritos = 0
        for i in range(0, len(planos), o['lote_update']):
            escritos += self._um_update(tabela, pares,
                                        planos[i:i + o['lote_update']], tot, o)
        return escritos

    def _ver_catalogo(self, par: Par, vistos: dict, tot: dict, o) -> None:
        """Confirma no banco, cria se autorizado, e memoriza o resultado."""
        modelo = apps.get_model(par.modelo)
        existem = set(modelo.objects.filter(codigo__in=list(vistos))
                      .values_list('codigo', flat=True))
        self._cat[par.fk] |= existem
        faltam = {c: n for c, n in vistos.items() if c not in existem}
        if not faltam:
            return
        if not o['criar_catalogo'] or o['dry_run']:
            self._nao_existem[par.fk] |= set(faltam)
            return
        # 1ª guarda: o catálogo é NACIONAL e a FK é `PROTECT`. Código fora do
        # padrão da TPU não entra nele nem com nome bonito — a primeira corrida
        # criou `99999999` porque um tribunal publicou isso.
        fora = {c for c in faltam if not TPU_RE.match(c)}
        if fora:
            self._fora_do_padrao[par.fk] |= fora
            logger.error('repop_classe_assunto: %d códigos FORA do padrão TPU '
                         'em %s — abstido, não criado: %s', len(fora),
                         par.modelo, ', '.join(sorted(fora))[:200])
            faltam = {c: n for c, n in faltam.items() if c not in fora}
            if not faltam:
                return
        # 2ª guarda, abster > chutar: código sem nome NÃO vira linha de catálogo
        # com o próprio código no lugar do nome. Fica pendente — outro bloco
        # pode trazer o nome, e aí a linha nasce certa.
        com_nome = {c: n for c, n in faltam.items() if n}
        self._sem_nome[par.fk] |= (set(faltam) - set(com_nome))
        self._sem_nome[par.fk] -= set(com_nome)
        if not com_nome:
            return
        modelo.objects.bulk_create(
            [modelo(codigo=c, nome=n[:255]) for c, n in sorted(com_nome.items())],
            ignore_conflicts=True)          # race-safe: `codigo` é PK
        self._cat[par.fk] |= set(com_nome)
        tot['catalogo_criado'] += len(com_nome)
        logger.info('repop_classe_assunto: %d linhas novas em %s (%s)',
                    len(com_nome), par.modelo,
                    ', '.join(sorted(com_nome))[:200])

    def _um_update(self, tabela: str, pares: tuple[Par, ...], lote: list,
                   tot: dict, o) -> int:
        n = len(pares)
        tipos = ', '.join(['%s::bigint'] + ['%s::varchar'] * n)
        vals = ','.join([f'({tipos})'] * len(lote))
        nomes = ', '.join(['id'] + [f'c{i}' for i in range(n)])
        # `COALESCE` repete a guarda do SELECT: entre ler e escrever, a
        # ingestão ao vivo pode ter fechado a FK — e ela sabe mais do que nós.
        sets = ', '.join(f'{par.fk} = COALESCE(t.{par.fk}, v.c{i})'
                         for i, par in enumerate(pares))
        onde = ' OR '.join(f'(v.c{i} IS NOT NULL AND t.{par.fk} IS NULL)'
                           for i, par in enumerate(pares))
        args = [x for pk, valores in lote for x in [pk, *valores]]
        sql = (f'UPDATE {tabela} t SET {sets} FROM (VALUES {vals}) '
               f'AS v({nomes}) WHERE t.id = v.id AND ({onde})')
        # Deadlock (40P01) e lock_timeout (55P03) são TRANSITÓRIOS e podem ser
        # retentados — com teto, backoff+jitter e o número REGISTRADO.
        #
        # O 55P03 não é hipótese: em 31/08/2026, 12 minutos de corrida
        # derrubaram o shard `q3` com `canceling statement due to lock timeout
        # … while locking tuple (2217479,20)`. Quem segurava era o
        # `backfill_fase` do #105, escrevendo na MESMA tabela com UPDATEs de
        # 20-30 s. Sem retentar, cada encontro mata um shard: a corrida não
        # perde dado (o checkpoint segura), mas morre sozinha de madrugada e
        # ninguém fica sabendo. Retentar é mais barato que subir o
        # `lock_timeout`, que faria ESTE lote segurar as próprias linhas por
        # mais tempo enquanto espera.
        for tentativa in range(1, o['tentativas_deadlock'] + 1):
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
                    cur.execute(
                        f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
                    if o['commit_assincrono']:
                        cur.execute('SET LOCAL synchronous_commit = off;')
                    cur.execute(sql, args)
                    return cur.rowcount
            except OperationalError as e:
                estado = getattr(e.__cause__, 'sqlstate', None)
                if estado not in TRANSITORIOS or tentativa == o['tentativas_deadlock']:
                    raise
                tot[TRANSITORIOS[estado]] += 1
                espera = 0.5 * (2 ** (tentativa - 1)) + random.uniform(0, 0.5)
                logger.warning('repop_classe_assunto: %s no lote de %d '
                               '(tentativa %d/%d) — %.2fs e retenta', estado,
                               len(lote), tentativa, o['tentativas_deadlock'],
                               espera)
                time.sleep(espera)
        return 0
