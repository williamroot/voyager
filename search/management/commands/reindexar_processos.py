"""Reindexa Process no Elasticsearch — bulk via HTTP, throttled, resumível.

Usa o endpoint ``_bulk`` do ES via ``requests`` (NDJSON) em vez da lib
``elasticsearch`` — assim roda no container ``web`` (que tem o código atualizado
por bind-mount + ``requests``), sem depender da lib que só está nos workers.

Por que ele — e não a campainha — é a ferramenta do BACKFILL DE CAMPO NOVO
(29/08/2026): a campainha (``tocar_campainha``) existe para o poller enxergar
uma escrita que já aconteceu, e o preço dela é um ``UPDATE`` por linha. Para
levar ``grau`` aos ~81 milhões de processos que já o têm no Postgres, isso
seria reescrever 78% da maior tabela do banco — WAL, bloat e lock na MESMA
tabela onde outros backfills estão escrevendo. Este comando não escreve **nada**
no Postgres: lê por keyset de pk e manda o documento pro ES.

    # o backfill inteiro, retomável, com teto de tempo por janela
    python manage.py reindexar_processos --checkpoint grau_v1 \
        --sleep 0.05 --max-segundos 21600
"""
import json
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Prefetch

from search.documents import processo_to_doc
from tribunals.models import Process, ProcessoParte

logger = logging.getLogger('voyager.search.reindex')


class Command(BaseCommand):
    help = 'Reindexa Process no Elasticsearch (bulk HTTP, throttled, resumível por tribunal).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', type=str, help='Filtrar por sigla do tribunal.')
        parser.add_argument('--limit', type=int, default=0, help='Máximo de docs (0=ilimitado).')
        parser.add_argument('--batch-size', type=int, default=2000, help='Tamanho do bulk.')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Pausa (s) entre bulks — throttle do DB (I/O sensível).')
        parser.add_argument('--desde-id', type=int, default=0,
                            help='Retoma de id > N (o progresso loga o range; resume manual).')
        parser.add_argument('--somente-enriquecidos', action='store_true',
                            help='Só processos com enriquecido_em (únicos que têm partes/'
                                 'valor — fase 1 do backfill do schema novo).')
        parser.add_argument('--ate-id', type=int, default=0,
                            help='Para em id <= N (0 = até o topo da tabela).')
        parser.add_argument('--checkpoint', type=str, default='',
                            help=('Nome do checkpoint no cache. Com ele o comando RETOMA '
                                  'de onde parou — sem ele, uma janela que termina é uma '
                                  'janela perdida, e 101 M de docs não cabem numa sessão.'))
        parser.add_argument('--max-segundos', type=int, default=0,
                            help='Teto de tempo da janela (0 = sem teto). Parar antes do '
                                 'fim é ERRO registrado com o id onde parou.')
        parser.add_argument('--reiniciar', action='store_true',
                            help='Ignora o checkpoint salvo.')
        parser.add_argument('--janela', type=int, default=10_000,
                            help=('Linhas por consulta ao Postgres. É o keyset: '
                                  'sem o LIMIT o planner ordena a tabela inteira '
                                  'em disco antes de devolver a 1ª linha.'))

    def handle(self, *args, **options):
        es = getattr(settings, 'ELASTICSEARCH_URL', 'http://elasticsearch:9200').rstrip('/')
        prefix = getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')
        idx = f'{prefix}-processos'

        chave = f'reindex_proc:{options["checkpoint"]}' if options['checkpoint'] else ''
        if chave and not options['reiniciar']:
            salvo = cache.get(chave)
            if salvo and salvo > options['desde_id']:
                options['desde_id'] = salvo
                self.stdout.write(f'retomando do checkpoint id={salvo:,}')

        # prefetch das participações (+parte) → _serialize_partes NÃO faz query por
        # processo. Sem isso, 71M processos = 71M+ queries e o DB (I/O sensível) cai.
        qs = (Process.objects
              .select_related('tribunal')
              .prefetch_related(Prefetch('participacoes',
                                         queryset=ProcessoParte.objects.select_related('parte')))
              .order_by('id'))
        if options['tribunal']:
            qs = qs.filter(tribunal_id=options['tribunal'])
        if options['desde_id']:
            qs = qs.filter(id__gt=options['desde_id'])
        if options['ate_id']:
            qs = qs.filter(id__lte=options['ate_id'])
        if options['somente_enriquecidos']:
            # enriquecido_em é setado em TODO caminho do drainer (ok/nao_encontrado/
            # erro) e é indexado — seek barato; só esses docs têm partes/valor.
            qs = qs.filter(enriquecido_em__isnull=False)

        bs = options['batch_size']
        slp = options['sleep']
        if bs < 1:
            raise CommandError('--batch-size tem que ser >= 1')
        # `count()` sem filtro é varredura de 101 M de linhas e não tem teto de
        # espera (regra nº 7). Ele só serve pra ETA — então: exato quando há
        # recorte, ESTIMATIVA do planner quando é a tabela inteira, e o texto
        # diz qual dos dois é.
        total, aprox = self._total(qs, options)
        self.stdout.write(
            f'Reindexando {"~" if aprox else ""}{total:,} processos '
            f'(tribunal={options["tribunal"] or "TODOS"}, desde_id={options["desde_id"]:,}, '
            f'batch={bs}, sleep={slp}s) → {idx}')
        self.stdout.flush()

        sess = requests.Session()
        # Falha de bulk é PERDA: aqueles documentos não voltam sozinhos (não há
        # fila, não há retry). Contar e gritar no fim com o número real é a
        # diferença entre dívida visível e o corte mudo de sempre.
        falhas = {'bulks': 0, 'docs': 0, 'primeiro_id': None, 'ultimo_id': None}

        def flush(actions):
            if not actions:
                return
            lines = []
            for pid, doc in actions:
                lines.append(json.dumps({'index': {'_index': idx, '_id': pid}}))
                lines.append(json.dumps(doc, default=str, ensure_ascii=False))
            body = ('\n'.join(lines) + '\n').encode('utf-8')

            def registrar(motivo):
                falhas['bulks'] += 1
                falhas['docs'] += len(actions)
                if falhas['primeiro_id'] is None:
                    falhas['primeiro_id'] = actions[0][0]
                falhas['ultimo_id'] = actions[-1][0]
                self.stderr.write(f'  bulk FALHOU ids {actions[0][0]}..{actions[-1][0]}'
                                  f' ({len(actions)} docs): {motivo}')
                logger.error('reindexar_processos: bulk falhou em ids %s..%s (%d docs): %s',
                             actions[0][0], actions[-1][0], len(actions), motivo)

            try:
                r = sess.post(f'{es}/_bulk', data=body,
                              headers={'Content-Type': 'application/x-ndjson'}, timeout=180)
                if r.status_code >= 300:
                    registrar(f'HTTP {r.status_code}: {r.text[:200]}')
            except Exception as e:  # noqa: BLE001 — ES hipou; registra e segue (idempotente)
                registrar(str(e)[:200])

        # KEYSET por pk, e o LIMIT não é enfeite. A primeira versão usava
        # `qs.order_by('id').iterator(chunk_size=bs)` sobre a tabela inteira:
        # medido em produção em 29/08/2026, o Postgres escolheu ordenar as
        # 103,6 M de linhas em disco (`wait_event = BuffileRead`, sort externo)
        # e ficou **175 s sem devolver a primeira linha** — o comando imprimiu
        # o cabeçalho e mais nada. Com `WHERE id > cursor ... LIMIT janela` o
        # plano vira Index Scan na pkey e a primeira linha sai na hora.
        actions = []
        enviados = 0
        cursor = options['desde_id']
        ultimo_id = cursor
        parou_no_teto = False
        t0 = time.monotonic()
        while True:
            lote = list(qs.filter(id__gt=cursor)[:options['janela']])
            if not lote:
                break
            for proc in lote:
                actions.append((proc.id, processo_to_doc(proc)))
                ultimo_id = proc.id
                if len(actions) >= bs:
                    flush(actions)
                    enviados += len(actions)
                    actions = []
                    # O checkpoint anda DEPOIS do flush, nunca antes: se o
                    # processo morrer no meio, a janela seguinte refaz o último
                    # lote (o `index` por `_id` é idempotente) em vez de pulá-lo.
                    if chave:
                        cache.set(chave, ultimo_id, None)
                    el = time.monotonic() - t0
                    rate = enviados / el if el else 0
                    eta = (total - enviados) / rate / 60 if rate else 0
                    self.stdout.write(f'  {enviados:,}/{"~" if aprox else ""}{total:,} '
                                      f'({100 * enviados / max(total, 1):.1f}%) · '
                                      f'id={ultimo_id} · {rate:.0f}/s · ETA {eta:.0f}min')
                    self.stdout.flush()
                    if slp:
                        time.sleep(slp)
            cursor = lote[-1].id
            el = time.monotonic() - t0
            if options['max_segundos'] and el >= options['max_segundos']:
                parou_no_teto = True
                break
            if options['limit'] and enviados + len(actions) >= options['limit']:
                break
        flush(actions)
        enviados += len(actions)
        if chave and ultimo_id:
            cache.set(chave, ultimo_id, None)
        self.stdout.write(self.style.SUCCESS(
            f'Concluído: {enviados:,} docs indexados em {idx} '
            f'(último id={ultimo_id:,}).'))

        # Teto atingido é ERRO com o número real, nunca `return` discreto
        # (regra nº 2). "Indexei 4 milhões" sem dizer que faltam 97 é o corte
        # mudo que já custou 179.490.613 publicações fora do índice.
        if parou_no_teto:
            topo = self._topo()
            # `None` é "não consegui medir", nunca 0 — zero leria como "não
            # falta nada", que é o contrário do que aconteceu (regra nº 6).
            quanto = (f'topo={topo:,}, faltam {topo - ultimo_id:,} ids'
                      if topo else 'e NÃO consegui medir o topo')
            retomada = ('Rode de novo com o mesmo --checkpoint para continuar daqui.'
                        if chave else
                        'SEM --checkpoint não há de onde retomar: anote o id acima.')
            self.stderr.write(
                f'ERRO: parei pelo teto de {options["max_segundos"]}s em '
                f'id={ultimo_id:,} ({quanto}). O resto NÃO está na busca. {retomada}')
        if falhas['bulks']:
            self.stderr.write(
                f'ERRO: {falhas["bulks"]:,} bulks falharam = {falhas["docs"]:,} documentos '
                f'que NÃO chegaram ao índice (ids {falhas["primeiro_id"]}..'
                f'{falhas["ultimo_id"]}). Não há fila nem retry aqui: reprocesse '
                f'essa faixa com --desde-id/--ate-id.')

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _topo() -> int | None:
        """`max(id)`, ou `None` se não deu pra medir (abster > chutar)."""
        try:
            with transaction.atomic(), connection.cursor() as c:
                c.execute("SET LOCAL statement_timeout = '5s'")
                c.execute('SELECT max(id) FROM tribunals_process')
                return c.fetchone()[0] or 0
        except Exception:
            logger.warning('reindexar_processos: não consegui ler max(id)')
            return None

    def _total(self, qs, options) -> tuple[int, bool]:
        """(total, é_estimativa). Só alimenta a barra de progresso."""
        if options['limit']:
            return options['limit'], False
        recortado = bool(options['tribunal'] or options['somente_enriquecidos']
                         or options['ate_id'])
        if recortado:
            try:
                with transaction.atomic(), connection.cursor() as c:
                    c.execute("SET LOCAL statement_timeout = '120s'")
                    return qs.count(), False
            except Exception:
                logger.warning('reindexar_processos: count exato estourou o teto; '
                               'caindo pra estimativa', exc_info=True)
        try:
            with transaction.atomic(), connection.cursor() as c:
                c.execute("SET LOCAL statement_timeout = '10s'")
                c.execute("SELECT reltuples::bigint FROM pg_class "
                          "WHERE relname = 'tribunals_process'")
                linha = c.fetchone()
                est = int(linha[0]) if linha and linha[0] else 0
            topo = self._topo() or 0
            if topo and options['desde_id']:
                est = max(int(est * (1 - options['desde_id'] / topo)), 0)
            return est, True
        except Exception:
            return 0, True
