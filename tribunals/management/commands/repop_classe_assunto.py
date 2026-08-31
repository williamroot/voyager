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
        #   só leitura (--sem-reparo) ..........  81-99 ms / 1.000 pks
        #   leitura + escrita ..................  249-460 ms / 1.000 pks
        #                                         (7.779 linhas, 4,64 ms/linha)
        #
        # Os tetos saem do segundo número, não do primeiro: calibrar o freio
        # pela medição SEM escrita faria a corrida frear contra si mesma no
        # primeiro bloco denso — e o `--parar` mataria a corrida sem que nada
        # estivesse caro.
        p.add_argument('--freio-ms-kpk', type=float, default=800.0,
                       help='ms por 1.000 pks varridos: acima disto a pausa '
                            'DOBRA; abaixo da metade, cede. Medido com escrita: '
                            '249-460.')
        p.add_argument('--parar-ms-kpk', type=float, default=2000.0,
                       help='média móvel de 5 blocos acima disto = PARA com '
                            'ERROR. 2.000 = ~5x o medido com escrita.')
        p.add_argument('--lock-alto', type=int, default=LOCK_ALTO,
                       help='sessões esperando Lock acima disto ⇒ espera.')
        p.add_argument('--lock-timeout', default='5s')
        p.add_argument('--statement-timeout', default='120s')
        p.add_argument('--tentativas-deadlock', type=int, default=3)
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

        topo = o['ate'] or self._topo(tabela, o)
        cur_pk = o['de'] or (0 if o['sem_checkpoint'] else (cache.get(wm) or 0))
        sleep = o['sleep']

        tot = {'lidos': 0, 'escritos': 0, 'deadlocks': 0, 'catalogo_criado': 0}
        for par in pares:
            tot[f'liga_{par.fk}'] = 0
            tot[f'orfao_{par.fk}'] = 0
            tot[f'orfao_sem_nome_{par.fk}'] = 0
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
            n_orf = tot[f'orfao_{par.fk}'] + tot[f'orfao_sem_nome_{par.fk}']
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
                f'órfão sem nome {tot[f"orfao_sem_nome_{par.fk}"]:,}')
        self.stdout.write(
            f'catálogo criado {tot["catalogo_criado"]:,} · deadlocks retentados '
            f'{tot["deadlocks"]:,} · {blocos} blocos · {esperas} esperas · '
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
            cur.execute(
                f'SELECT {", ".join(cols)} FROM {tabela} '
                f'WHERE id > %s AND id <= %s AND ({alvo})', [lo, hi])
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
                # abstenção declarada, com o motivo separado: código que o
                # catálogo não tem × código sem nome para criar a linha
                chave = ('orfao_sem_nome_' if cod in self._sem_nome[par.fk]
                         else 'orfao_') + par.fk
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
        # abster > chutar: código sem nome NÃO vira linha de catálogo com o
        # próprio código no lugar do nome. Fica pendente — outro bloco pode
        # trazer o nome, e aí a linha nasce certa.
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
        # Deadlock (40P01) é transitório e PODE ser retentado — com teto,
        # backoff+jitter e o número de tentativas REGISTRADO.
        for tentativa in range(1, o['tentativas_deadlock'] + 1):
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}';")
                    cur.execute(
                        f"SET LOCAL statement_timeout = '{o['statement_timeout']}';")
                    cur.execute(sql, args)
                    return cur.rowcount
            except OperationalError as e:
                if getattr(e.__cause__, 'sqlstate', None) != '40P01' \
                        or tentativa == o['tentativas_deadlock']:
                    raise
                tot['deadlocks'] += 1
                espera = 0.2 * (2 ** (tentativa - 1)) + random.uniform(0, 0.2)
                logger.warning('repop_classe_assunto: deadlock no lote de %d '
                               '(tentativa %d/%d) — %.2fs e retenta',
                               len(lote), tentativa, o['tentativas_deadlock'],
                               espera)
                time.sleep(espera)
        return 0
