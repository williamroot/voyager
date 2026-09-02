"""Backfill: promove os destinatários da DJEN a `Parte`/`ProcessoParte`.

    # ver o que faria, sem escrever nada
    python manage.py backfill_partes_djen --de 36000000 --ate 36100000 --dry-run

    # piloto de uma faixa contígua de pk
    python manage.py backfill_partes_djen --de 36000000 --ate 36100000

    # retomar de onde parou (checkpoint no Redis), com teto de tempo
    python manage.py backfill_partes_djen --shard nacional --max-segundos 3600

    # PARAR AGORA (efeito em segundos, chave no Redis, sem deploy)
    python manage.py backfill_partes_djen --pausar
    python manage.py backfill_partes_djen --despausar

Duas travas que vieram da autópsia dos outros backfills longos (02/09/2026 —
351 reinícios com progresso líquido ZERO entre `r105_fase_1` e `r105_fase_3`):

    1. **`--shard` é obrigatório** para faixa aberta. Sem checkpoint, todo
       restart volta ao pk inicial e a varredura nunca chega ao fim; agora isso
       é `CommandError` na largada em vez de descoberta três dias depois. Faixa
       fechada de teste: `--sem-checkpoint --de <lo> --ate <hi>`.

    2. **Teto sai com `exit ≠ 0`.** Era o `exit 0` que tornava "estourei o
       teto" indistinguível de "terminei" — e, com `--restart unless-stopped`,
       alimentava o laço. O checkpoint já está gravado quando o erro sobe:
       sair com erro não perde lugar, só torna o teto visível.

    E o teto de custo (`--parar-ms-id`) mede **ms por ID VARRIDO**, nunca por
    linha encontrada. Numa faixa já preenchida o segundo denominador vai a
    zero e um bloco normal vira "16.808 ms/linha" — o freio se declarava
    estrangulado onde não havia estrangulamento nenhum.

Por que a faixa de `Process.id` e não `--tribunal`, que seria o natural:

    `tribunals/models.py` declara `Index(fields=['tribunal', '-id'],
    name='proc_tribunal_id_idx')`, mas o índice que EXISTE no banco com esse
    nome é `btree (tribunal_id)` — uma coluna só. Conferido em `pg_indexes` em
    25/08/2026. Sem `(tribunal_id, id)`, `WHERE tribunal_id='TJPR' ORDER BY id
    LIMIT 500` cai em `Index Scan using tribunals_process_pkey … Filter:
    tribunal_id = 'TJPR'` (custo 39.100.789) e um `LIMIT 1` desses ficou
    **1.318 s** em produção antes de ser cancelado.

    Faixa de pk fechada usa a PK e é barata e previsível: `Index Scan using
    tribunals_process_pkey, Index Cond: (id >= X AND id < Y)`, custo 60.209
    para 50.000 ids. Medido ponta a ponta: **1,26 s por 1.000 processos**.
    E a faixa contígua é o que torna o reindex do ES viável depois.

    `--tribunal` continua existindo e filtra DENTRO da faixa (mesmo custo de
    plano); serve para piloto, não para varrer o acervo.
"""
import logging
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from tribunals.services.partes_djen import (
    JANELA_MOVS,
    promover_lote,
    sem_processoparte,
)

logger = logging.getLogger('voyager.partes_djen')

#: Kill switch, mesmo estilo do `enrich:pausados` (`enrichers/jobs.py`): set no
#: cache Redis, sem TTL (sobrevive a restart), honrado a cada lote.
PAUSA_KEY = 'bf:partes_djen:pausado'
#: Checkpoint do cursor por shard — para parar e retomar sem reler.
CURSOR_KEY = 'bf:partes_djen:%s:cursor'

#: Teto superior do espaço de pk. Não é `max(id)`: buscar o máximo real custa
#: uma consulta na tabela quente e o valor só cresce. É teto de varredura, e
#: atingi-lo é fim de shard, não corte mudo.
PK_MAX_PADRAO = 110_000_000


class Command(BaseCommand):
    help = 'Promove destinatários da DJEN a Parte/ProcessoParte por faixa de Process.id.'

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, default=None,
                            help='pk inicial (inclusive). Default: checkpoint do shard, ou 0.')
        parser.add_argument('--ate', type=int, default=None,
                            help='pk final (exclusive). Default: PK_MAX_PADRAO.')
        parser.add_argument('--lote', type=int, default=500,
                            help='processos por passada (default 500).')
        parser.add_argument('--tribunal', default=None,
                            help='filtra DENTRO da faixa de pk (piloto).')
        parser.add_argument('--shard', default=None,
                            help='nome do checkpoint no Redis. Sem isto, não há checkpoint.')
        parser.add_argument('--janela-movs', type=int, default=JANELA_MOVS)
        parser.add_argument('--max-processos', type=int, default=0,
                            help='teto de processos lidos. 0 = sem teto.')
        parser.add_argument('--max-segundos', type=int, default=0,
                            help='teto de tempo. 0 = sem teto.')
        parser.add_argument('--carga', type=float, default=0.5,
                            help='fração do tempo em que pode ocupar o banco. '
                                 '0,5 = descansa o mesmo tempo que trabalhou.')
        parser.add_argument('--parar-ms-id', type=float, default=25.0,
                            help='teto de custo em ms por ID VARRIDO (não por '
                                 'linha encontrada — ver a docstring). Acima '
                                 'disso o banco está em contenção e o backfill '
                                 'sai com ERRO. 0 = sem teto.')
        parser.add_argument('--sem-checkpoint', action='store_true',
                            help='varre uma faixa fechada sem guardar cursor. '
                                 'Exige --de e --ate — ver a docstring.')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--pausar', action='store_true')
        parser.add_argument('--despausar', action='store_true')

    def handle(self, *args, **o):
        if o['pausar'] or o['despausar']:
            cache.set(PAUSA_KEY, bool(o['pausar']), timeout=None)
            estado = 'PAUSADO' if o['pausar'] else 'liberado'
            self.stdout.write(self.style.WARNING(f'backfill de partes DJEN: {estado}'))
            return
        if o['lote'] < 1:
            raise CommandError('--lote tem que ser >= 1')
        if not 0 < o['carga'] <= 1:
            raise CommandError('--carga tem que estar em (0, 1]')
        # Sem checkpoint E sem faixa fechada, um restart recomeça do ZERO — o
        # defeito medido em 02/09/2026 nos outros backfills (351 reinícios,
        # progresso líquido zero). Aqui isso é ERRO na largada, não surpresa
        # três dias depois.
        if not o['shard'] and not o['sem_checkpoint']:
            raise CommandError(
                'sem --shard não há checkpoint: um restart recomeçaria do pk '
                'inicial e a varredura nunca chegaria ao fim. Use --shard '
                '<nome>, ou --sem-checkpoint com --de e --ate para uma faixa '
                'fechada.')
        if o['sem_checkpoint'] and (o['de'] is None or o['ate'] is None):
            raise CommandError('--sem-checkpoint exige --de e --ate (faixa fechada).')

        shard = o['shard']
        ini = o['de']
        if ini is None:
            ini = cache.get(CURSOR_KEY % shard) if shard else None
            ini = int(ini) if ini is not None else 0
        fim = o['ate'] if o['ate'] is not None else PK_MAX_PADRAO
        if ini >= fim:
            self.stdout.write(f'nada a fazer: faixa vazia [{ini}, {fim})')
            return

        t_inicio = time.time()
        cursor = ini
        tot = {k: 0 for k in (
            'janela', 'alvo', 'pulados_com_parte', 'sem_movimentacao',
            'sem_destinatario', 'so_segredo', 'descartados_segredo',
            'com_marcador_segredo', 'polo_abstido', 'linhas_tentadas',
            'linhas_confirmadas',
        )}
        lotes = 0
        motivo_parada = 'faixa concluída'
        pk_min_tocado = pk_max_tocado = None
        # Custo em ms por ID VARRIDO, suavizado. Ver `_custo_ms_id`.
        ms_id = None

        while cursor < fim:
            if cache.get(PAUSA_KEY):
                motivo_parada = 'kill switch'
                break
            if o['max_segundos'] and (time.time() - t_inicio) >= o['max_segundos']:
                motivo_parada = 'teto de tempo'
                break
            if o['max_processos'] and tot['janela'] >= o['max_processos']:
                motivo_parada = 'teto de processos'
                break

            t_lote = time.time()
            cursor_antes = cursor
            ids, proximo = self._janela(cursor, fim, o['lote'], o['tribunal'])
            if not ids and proximo >= fim:
                cursor = fim
                break
            cursor = proximo

            if ids:
                tot['janela'] += len(ids)
                alvo = sem_processoparte(ids)
                tot['pulados_com_parte'] += len(ids) - len(alvo)
                res = promover_lote(alvo, janela=o['janela_movs'], dry_run=o['dry_run'])
                for k in ('alvo', 'sem_movimentacao', 'sem_destinatario', 'so_segredo',
                          'descartados_segredo', 'com_marcador_segredo', 'polo_abstido',
                          'linhas_tentadas', 'linhas_confirmadas'):
                    tot[k] += getattr(res, k)
                if res.processos_tocados:
                    lo, hi = res.processos_tocados[0], res.processos_tocados[-1]
                    pk_min_tocado = lo if pk_min_tocado is None else min(pk_min_tocado, lo)
                    pk_max_tocado = hi if pk_max_tocado is None else max(pk_max_tocado, hi)
                lotes += 1

            if shard and not o['dry_run']:
                cache.set(CURSOR_KEY % shard, cursor, timeout=None)

            gasto = time.time() - t_lote
            ms_id = self._custo_ms_id(ms_id, gasto, cursor - cursor_antes)
            if lotes % 20 == 1:
                self.stdout.write(
                    f'  pk<{cursor:>10}  janela {tot["janela"]:>8}  '
                    f'alvo {tot["alvo"]:>8}  linhas {tot["linhas_confirmadas"]:>8}  '
                    f'({gasto:.2f}s/lote · {ms_id:.2f} ms/id varrido)')
                self.stdout.flush()
            # Teto de custo. Denominador = IDs VARRIDOS, e por isso ele não
            # explode numa faixa que já estava toda preenchida — ver
            # `_custo_ms_id`. Precisa de lastro: um lote lento é ruído.
            if o['parar_ms_id'] and lotes >= 10 and ms_id > o['parar_ms_id']:
                motivo_parada = 'teto de custo'
                break
            # Freio proporcional: o banco é disk-I/O-bound e há drainers
            # aplicando ~126 mil events/h. Pausa fixa é freio fraco onde mais
            # importa — quanto mais caro o lote, mais tempo devolvido.
            descanso = gasto * (1.0 - o['carga']) / o['carga']
            if descanso > 0.01:
                time.sleep(min(descanso, 30.0))

        dur = time.time() - t_inicio
        self._relatorio(tot, lotes, dur, cursor, motivo_parada,
                        pk_min_tocado, pk_max_tocado, o, ms_id)
        # Regra nº 2, e a lição de 02/09/2026: teto que sai com `exit 0` é
        # indistinguível de "terminou". Foi assim que 351 reinícios de outro
        # backfill passaram por progresso. O checkpoint já está gravado, então
        # sair com erro NÃO perde lugar — só torna o teto visível.
        if motivo_parada in ('teto de tempo', 'teto de processos', 'teto de custo'):
            raise CommandError(
                f'TETO ATINGIDO ({motivo_parada}) em pk {cursor} de {fim} — a '
                f'faixa NÃO terminou. Retome com '
                f'`backfill_partes_djen --shard {shard}`.')

    # -- driver: faixa CONTÍGUA de pk (ver docstring do módulo) --------------
    def _janela(self, de: int, ate: int, lote: int, tribunal: str | None):
        """Devolve `(ids, proximo_cursor)`.

        Cresce a largura da faixa até juntar `lote` processos ou bater em
        `ate` — o espaço de pk tem buracos (processos apagados, ids de outras
        tabelas), então largura fixa devolveria lotes de tamanho errático.
        """
        largura = max(lote * 2, 1000)
        ids: list[int] = []
        cursor = de
        while cursor < ate and len(ids) < lote:
            topo = min(cursor + largura, ate)
            sql = ('SELECT id FROM tribunals_process '
                   'WHERE id >= %s AND id < %s')
            params = [cursor, topo]
            if tribunal:
                sql += ' AND tribunal_id = %s'
                params.append(tribunal)
            sql += ' ORDER BY id'
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute('SET LOCAL statement_timeout = %s', [120_000])
                    cur.execute(sql, params)
                    ids.extend(r[0] for r in cur.fetchall())
            cursor = topo
            if not ids:
                # Faixa deserta (buraco de pk ou tribunal ausente): abre o passo
                # em vez de rastejar de mil em mil.
                largura = min(largura * 4, 2_000_000)
        return ids[:lote], (ids[lote] if len(ids) > lote else cursor)

    #: Suavização do custo. Um lote sozinho não decide: `checkpoint`, `VACUUM`
    #: e a busca do site produzem picos de segundos que somem na passada
    #: seguinte, e um teto que dispara no primeiro pico é teto que ninguém liga.
    ALFA = 0.2

    @staticmethod
    def _custo_ms_id(anterior, gasto_s: float, ids_varridos: int) -> float:
        """ms por ID VARRIDO — o denominador é a LARGURA de pk percorrida.

        É aqui que mora a lição de 02/09/2026. Os outros backfills longos
        dividiam o tempo do bloco pelas linhas ENCONTRADAS, e numa faixa que já
        estava preenchida o denominador ia a zero: o freio lia "16.808
        ms/linha", o processo se declarava estrangulado e saía — com `exit 0`,
        o `unless-stopped` ressuscitando e o cursor voltando ao início. **351
        reinícios, progresso líquido zero.**

        O tempo de um bloco é gasto VARRENDO pk, não processando linha: a faixa
        deserta custa uma consulta barata e devolve largura grande (o
        `_janela` abre o passo até 2 M), então ela pontua ms/id ~0, que é a
        verdade — não custou nada. E a faixa densa e cara pontua alto, que
        também é a verdade. O número mede o BANCO, não a sorte da faixa.
        """
        atual = 1000.0 * gasto_s / max(1, ids_varridos)
        return atual if anterior is None else (
            Command.ALFA * atual + (1 - Command.ALFA) * anterior)

    def _relatorio(self, tot, lotes, dur, cursor, motivo, pk_lo, pk_hi, o, ms_id=None):
        w = self.stdout.write
        w('')
        w(f'lotes {lotes} · {dur:.0f}s · parou em pk {cursor} · motivo: {motivo}')
        w(f'  janela (processos lidos) ....... {tot["janela"]:>10,}')
        w(f'  pulados (já tinham parte) ...... {tot["pulados_com_parte"]:>10,}')
        w(f'  alvo (zero ProcessoParte) ...... {tot["alvo"]:>10,}')
        w(f'    sem movimentação ............. {tot["sem_movimentacao"]:>10,}')
        w(f'    com movimentação, JSONB vazio  {tot["sem_destinatario"]:>10,}')
        w(f'    só destinatário de segredo ... {tot["so_segredo"]:>10,}')
        w(f'  destinatários descartados ...... {tot["descartados_segredo"]:>10,}')
        w(f'  processos com marcador segredo . {tot["com_marcador_segredo"]:>10,}')
        w(f'  polo ABSTIDO (janela se contradiz) {tot["polo_abstido"]:>7,}  '
          f'→ foram para `outros`')
        w(f'  ProcessoParte oferecidas ....... {tot["linhas_tentadas"]:>10,}')
        w(f'  ProcessoParte confirmadas ...... {tot["linhas_confirmadas"]:>10,}  '
          f'(contagem independente, fonte=djen)')
        if pk_lo is not None:
            w(f'  faixa de pk TOCADA ............. [{pk_lo}, {pk_hi}]  '
              f'← passe esta faixa ao reindex do ES')
        if tot['janela']:
            w(f'  custo .......................... {dur / tot["janela"] * 1000:.2f}s '
              f'por 1.000 processos (com o freio de --carga {o["carga"]})')
        if ms_id is not None:
            w(f'  custo por ID VARRIDO ........... {ms_id:.2f} ms  '
              f'(teto --parar-ms-id {o["parar_ms_id"]})')
        # Regra nº 2: teto atingido é ERRO registrado com o número REAL, nunca
        # um `return` discreto.
        if motivo in ('teto de tempo', 'teto de processos', 'teto de custo'):
            logger.error(
                'backfill_partes_djen parou por %s ANTES de terminar a faixa', motivo,
                extra={'motivo': motivo, 'cursor': cursor, 'ate': o['ate'],
                       'processos': tot['janela'], 'linhas': tot['linhas_confirmadas'],
                       'ms_id': ms_id, 'shard': o['shard']})
            w(self.style.ERROR(
                f'TETO ATINGIDO ({motivo}) em pk {cursor} — a faixa NÃO terminou. '
                f'{tot["janela"]:,} processos lidos, {tot["linhas_confirmadas"]:,} linhas.'))
        elif motivo == 'kill switch':
            logger.error('backfill_partes_djen PAUSADO pelo kill switch em pk %s', cursor)
            w(self.style.WARNING(f'PAUSADO pelo kill switch em pk {cursor}.'))
