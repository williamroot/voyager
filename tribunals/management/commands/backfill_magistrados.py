"""Lê o magistrado do texto das publicações e grava `Magistrado`/`MagistradoAtuacao`.

QUANTO ISSO GRAVA (medido, antes de rodar)
------------------------------------------
Duas amostras independentes de 03/09/2026 — 42.281 publicações no Postgres com
salto pseudoaleatório (**11,39%** produzem atribuição) e 600 sorteadas no ES
(**11,83%**) — projetam **~184 a 191 milhões de linhas** sobre as
1.618.133.888 de `tribunals_movimentacao`. Rodar a faixa inteira não é um
comando: é uma decisão de disco.

O ORÇAMENTO É EM BYTES, E ELE SE MEDE SOZINHO (03/09/2026)
----------------------------------------------------------
A projeção de espaço que autorizou este backfill era **derivada**: 36 GB /
109.258.115 linhas de `tribunals_processoparte` = ~354 B/linha, menos colunas
aqui ⇒ ~300 B/linha ⇒ ~57 GB para as 190 M. Derivada de OUTRA tabela, com
outros índices — é hipótese, não medida.

E o número que decidiria se cabe — **o disco livre do host do banco** — não é
observável de dentro: `ssh` para o `.101` recusa e o papel `voyager` não tem
`pg_read_all_settings` (`SHOW data_directory` → *permission denied to
examine*), nem `pg_ls_dir`. Conferido em 03/09/2026.

Então este comando **não aposta em espaço que ninguém vê**. Ele:

1. registra o ZERO (`pg_total_relation_size` das duas tabelas) na primeira
   largada, no Redis, e nunca mais o reescreve — o orçamento é do TRABALHO
   INTEIRO, não da rodada;
2. mede o tamanho a cada lote e calcula **bytes por linha REAIS** = bytes que
   a rodada acrescentou ÷ linhas que a rodada confirmou. As duas pontas são
   exatas: a primeira é catálogo, a segunda é o `rowcount` do
   `ON CONFLICT DO NOTHING` (por isso a inserção das atuações é SQL cru e não
   `bulk_create` — o `ignore_conflicts` do Django não devolve quantas linhas
   entraram de verdade, e sem esse denominador "bytes por linha" seria
   estimativa vestida de medição);
3. **para no orçamento de BYTES** (`--orcamento-bytes`, default 20 GiB) com
   `exit ≠ 0`, dizendo quantas linhas entraram, quantos bytes custaram e qual
   é a extrapolação MEDIDA para as 190 M.

Se a medição divergir dos 300 B/linha derivados, quem manda é a medição.

ENTRA POR FAIXA FECHADA DE PK — NUNCA POR `--tribunal`
------------------------------------------------------
Não é preferência. `tribunals_movimentacao` não tem índice que sirva a
"filtra por tribunal, ordena por id": em `tribunals_process` o índice com esse
nome existe com **uma coluna só**, e o `LIMIT 1` que dele dependia ficou
**1.318 s** em produção e enfileirou 63 sessões atrás de um `ALTER TABLE`
(`.ia/DATA_MODEL.md`). Faixa fechada de pk usa a PK, é contígua, é retomável e
é o que torna o trabalho paralelizável em shards sem sobreposição — o mesmo
desenho do `backfill_partes_djen`.

ITERA, NÃO ACUMULA (regra nº 1) — E `.iterator()` AQUI NÃO ITERA
----------------------------------------------------------------
A primeira versão lia a faixa com `.iterator(chunk_size=500)`. Isso **não**
protege esta base: `core/settings.py` liga `DISABLE_SERVER_SIDE_CURSORS = True`
(obrigatório com pgbouncer em transaction mode), e sem cursor no servidor o
psycopg baixa **o resultado inteiro** no cliente antes do primeiro `yield` — o
`chunk_size` só fatia o que já está na RAM. Numa faixa de 10 M de pks isso é o
`texto` de ~10 M de publicações em memória: o OOM do DJEN outra vez, com outra
roupa (56 KB por publicação medidos no TJDFT).

Por isso a leitura é uma JANELA de pk por vez (`id >= cursor AND id < topo
LIMIT n`), com `statement_timeout` próprio, exatamente como no
`backfill_partes_djen`. É a janela que também dá o checkpoint e o custo por id
varrido — os três saem do mesmo laço.

TETO É ALERTA, NUNCA CORTE MUDO (regra nº 2)
--------------------------------------------
Todos os tetos (bytes, tempo, publicações, custo, fim de faixa por kill
switch) saem com **`CommandError`**, ou seja `exit ≠ 0`, com o pk onde parou e
o comando de retomada. Era o `exit 0` que tornava "estourei o teto"
indistinguível de "terminei" — e, com `--restart unless-stopped`, alimentava o
laço de **351 reinícios com progresso líquido zero** medido em 02/09/2026
entre os shards do `backfill_fase`.

⚠️ E o custo se mede em **ms por id VARRIDO**, nunca por linha encontrada. Na
faixa em que só 11% das publicações produzem linha, o denominador "linhas
gravadas" faz um lote normal parecer estrangulamento — foi assim que um bloco
virou "16.808 ms/linha" e o processo se matou sozinho.

O QUE ELE NÃO FAZ
-----------------
Não conta nada durante o backfill de faixa. `Magistrado.n_publicacoes` fica
`NULL` — porque uma contagem feita sobre uma faixa parcial seria um número
verdadeiro respondendo a outra pergunta. A contagem sai do modo `--contar`,
que recomputa TODOS os magistrados a partir de `MagistradoAtuacao` num
`GROUP BY` só e carimba `n_publicacoes_em`. Contagem sem data de medição
envelhece em silêncio.

    # medir antes: a faixa e o que ela promete
    manage.py backfill_magistrados --de 1 --ate 50000 --dry-run

    # gravar com checkpoint e orçamento (o jeito normal)
    manage.py backfill_magistrados --shard nacional --orcamento-bytes 20GB

    # parar AGORA, sem deploy (kill switch no Redis, honrado a cada lote)
    manage.py backfill_magistrados --pausar
    manage.py backfill_magistrados --despausar

    # onde está o orçamento, sem escrever nada
    manage.py backfill_magistrados --estado

    # depois de fechar as faixas, e só depois:
    manage.py backfill_magistrados --contar
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.utils import OperationalError
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import Magistrado, MagistradoAtuacao
from tribunals.services import magistrados as mag

logger = logging.getLogger('voyager.magistrados')

#: Publicação do TJDFT mede 56 KB. 500 × 56 KB ≈ 28 MB de texto vivo — é o
#: teto de BYTES disfarçado de teto de linhas, e está aqui escrito para que
#: quem subir o número saiba o que está comprando.
CHUNK_LEITURA = 500
LOTE_ESCRITA = 2000

#: Teto de varredura do espaço de pk. **Não** é `max(id)`: o máximo real cresce
#: 24 h por dia (a ingestão escreve nesta tabela) e buscá-lo a cada largada
#: seria uma consulta a mais na tabela quente por nada. Em 03/09/2026 o
#: `max(id)` valia 2.105.988.980. Atingir este teto é FIM DE FAIXA, não corte
#: mudo — e a cauda nova é de quem rodar com `--de` no topo antigo.
PK_MAX_PADRAO = 2_400_000_000

#: As DUAS tabelas que este backfill enche. O orçamento é a soma: a fan-out por
#: órgão (2.549 linhas de `Magistrado` para 877 pessoas, medida em 03/09/2026)
#: é pequena perto das atuações, mas ela também ocupa disco e não vai ficar
#: fora da conta só por ser pequena.
TABELAS = ('tribunals_magistrado', 'tribunals_magistradoatuacao')

#: Chaves no Redis (AOF ligado desde 31/08/2026 — sobrevivem a restart).
PAUSA_KEY = 'bf:magistrados:pausado'
#: Passo do avanço às cegas quando o salto de deserto não responde. Largo o
#: bastante para atravessar um vazio típico sem rastejar, curto o bastante para
#: a janela seguinte ainda medir o custo antes de andar de novo.
PASSO_CEGO = 5_000_000

CURSOR_KEY = 'bf:magistrados:%s:cursor'
#: O ZERO do orçamento. Gravado UMA vez, na primeira largada com a tabela
#: vazia, e nunca reescrito: o orçamento mede o trabalho inteiro, não a rodada.
#: Se a chave sumir (Redis novo, flush), o zero é remedido — e aí ele passa a
#: valer "a partir de agora", o que o `--estado` diz na cara em vez de fingir.
ZERO_KEY = 'bf:magistrados:zero_bytes:v1'

#: 20 GiB. Sugerido pelo dono do orçamento como PRIMEIRA fatia, contra uma
#: projeção derivada de ~57 GB para o acervo inteiro. Bater nele é erro, e a
#: saída traz a extrapolação medida — é com ela que se pede a fatia seguinte.
ORCAMENTO_PADRAO = 20 * 1024 ** 3

#: Projeção de linhas para o acervo inteiro (R124, duas amostras: 11,39% e
#: 11,83% de 1.618.133.888 movimentações). Serve só para a EXTRAPOLAÇÃO do
#: relatório — nada no laço depende dela.
LINHAS_PROJETADAS = 190_000_000
MOVIMENTACOES_TOTAL = 1_618_133_888

_RE_TAMANHO = re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*([kmgt]?i?b?)\s*$', re.I)
_MULT = {'': 1, 'b': 1, 'k': 1000, 'kb': 1000, 'ki': 1024, 'kib': 1024,
         'm': 1000 ** 2, 'mb': 1000 ** 2, 'mi': 1024 ** 2, 'mib': 1024 ** 2,
         'g': 1000 ** 3, 'gb': 1000 ** 3, 'gi': 1024 ** 3, 'gib': 1024 ** 3,
         't': 1000 ** 4, 'tb': 1000 ** 4, 'ti': 1024 ** 4, 'tib': 1024 ** 4}


def tamanho_em_bytes(txt: str) -> int:
    """`'20GB'`, `'20GiB'`, `'21474836480'` → bytes. Lixo levanta erro.

    Aceitar sufixo não é conforto: um orçamento digitado em bytes crus é um
    número de 11 dígitos, e número de 11 dígitos digitado à mão erra de 10× em
    silêncio. Errar aqui é errar o único freio que este comando tem.
    """
    m = _RE_TAMANHO.match(str(txt))
    if not m:
        raise CommandError(f'não entendi o tamanho {txt!r} (ex.: 20GB, 500MB, 1TiB)')
    num, suf = m.group(1).replace(',', '.'), m.group(2).lower()
    if suf not in _MULT:
        raise CommandError(f'sufixo desconhecido em {txt!r}')
    return int(float(num) * _MULT[suf])


def bytes_ocupados() -> int:
    """`pg_total_relation_size` das duas tabelas: heap + TOAST + TODOS os índices.

    `pg_total_relation_size` e não `pg_relation_size`: são quatro índices por
    linha de atuação (pk, a unique `(magistrado, movimentacao_id)`,
    `atu_mag_data_idx` e `atu_processo_idx`) e eles são a maior parte do custo.
    Medir só o heap publicaria menos da metade da conta — número redondo que
    encerra a investigação (regra nº 3).
    """
    with connection.cursor() as c:
        c.execute('SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) '
                  'FROM pg_class c WHERE c.relname = ANY(%s)', [list(TABELAS)])
        return int(c.fetchone()[0])


def zero_do_orcamento(agora_bytes: int) -> dict:
    """O tamanho no começo do TRABALHO. Gravado uma vez; nunca reescrito."""
    guardado = cache.get(ZERO_KEY)
    if guardado and 'bytes' in guardado:
        return guardado
    z = {'bytes': agora_bytes, 'em': timezone.now().isoformat(),
         'tabela_vazia': agora_bytes < 1024 ** 2}
    cache.set(ZERO_KEY, z, timeout=None)
    logger.info('backfill_magistrados: ZERO do orçamento gravado em %s bytes '
                '(%s)', agora_bytes,
                'tabelas vazias' if z['tabela_vazia'] else
                'ATENÇÃO: as tabelas JÁ tinham dado — o orçamento conta a '
                'partir de agora, não desde a primeira linha')
    return z


class Command(BaseCommand):
    help = 'Extrai magistrado do texto das publicações numa faixa de pk.'

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, help='pk inicial (inclusivo)')
        parser.add_argument('--ate', type=int,
                            help=f'pk final (EXCLUSIVO). Default: {PK_MAX_PADRAO}')
        parser.add_argument('--tribunal', default=None,
                            help='varre SÓ este tribunal (sigla). Não muda o '
                                 'cursor: continua a mesma faixa de pk, só '
                                 'deixa de LER o `texto` de quem não é dele — '
                                 'e ler o texto é 97%% do custo do lote.')
        parser.add_argument('--shard', default=None,
                            help='nome do checkpoint no Redis. Sem isto não há '
                                 'checkpoint e um restart volta ao pk inicial.')
        parser.add_argument('--sem-checkpoint', action='store_true',
                            help='faixa FECHADA sem cursor. Exige --de e --ate.')
        parser.add_argument('--orcamento-bytes', default=str(ORCAMENTO_PADRAO),
                            help='teto de DISCO das duas tabelas somadas, desde '
                                 'o zero gravado no Redis. Aceita 20GB/20GiB. '
                                 '0 = sem teto (só com decisão de quem opera o disco).')
        parser.add_argument('--lote', type=int, default=CHUNK_LEITURA,
                            help=f'publicações lidas por janela (default {CHUNK_LEITURA}). '
                                 'É teto de BYTES disfarçado: 500 × 56 KB ≈ 28 MB.')
        parser.add_argument('--carga', type=float, default=0.5,
                            help='fração do tempo em que pode ocupar o banco. '
                                 '0,5 = descansa o mesmo tempo que trabalhou.')
        parser.add_argument('--parar-ms-id', type=float, default=25.0,
                            help='teto de custo em ms por ID VARRIDO (nunca por '
                                 'linha encontrada). 0 = sem teto.')
        parser.add_argument('--max-publicacoes', type=int, default=0,
                            help='teto da rodada em publicações lidas. 0 = sem teto')
        parser.add_argument('--max-segundos', type=int, default=0,
                            help='teto de relógio da rodada. 0 = sem teto')
        parser.add_argument('--conferir', action='store_true',
                            help='ao fim da faixa, CONTA as publicações do '
                                 'trecho varrido no banco e compara com as '
                                 'lidas. Divergência é ERRO. Caro numa faixa '
                                 'grande — é controle de piloto, não de rotina.')
        parser.add_argument('--dry-run', action='store_true',
                            help='lê e conta, não grava nada')
        parser.add_argument('--pausar', action='store_true')
        parser.add_argument('--despausar', action='store_true')
        parser.add_argument('--estado', action='store_true',
                            help='imprime orçamento, cursores e bytes/linha. '
                                 'Não escreve nada.')
        parser.add_argument('--contar', action='store_true',
                            help='modo contagem: recomputa n_publicacoes de '
                                 'TODOS os magistrados a partir das atuações')

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        if opts['pausar'] or opts['despausar']:
            cache.set(PAUSA_KEY, bool(opts['pausar']), timeout=None)
            self.stdout.write(self.style.WARNING(
                'backfill de magistrados: '
                + ('PAUSADO' if opts['pausar'] else 'liberado')))
            return
        if opts['estado']:
            self._estado()
            return
        if opts['contar']:
            self._contar()
            return

        if not opts['shard'] and not opts['sem_checkpoint']:
            raise CommandError(
                'sem --shard não há checkpoint: um restart recomeçaria do pk '
                'inicial e a varredura nunca chegaria ao fim (foi assim que '
                'outro backfill reiniciou 351 vezes com progresso zero). Use '
                '--shard <nome>, ou --sem-checkpoint com --de e --ate.')
        if opts['sem_checkpoint'] and (opts['de'] is None or opts['ate'] is None):
            raise CommandError('--sem-checkpoint exige --de e --ate (faixa fechada).')
        if opts['tribunal']:
            opts['tribunal'] = opts['tribunal'].upper()
            from tribunals.models import Tribunal
            if not Tribunal.objects.filter(sigla=opts['tribunal']).exists():
                raise CommandError(
                    f'tribunal {opts["tribunal"]!r} não existe na tabela '
                    f'`tribunals_tribunal`. Sigla errada varreria 2,4 bilhões '
                    f'de pk para achar zero linha, com run verde no fim.')
            if not opts['shard'] and not opts['sem_checkpoint']:
                pass    # já barrado abaixo
            elif opts['shard'] and opts['tribunal'].lower() not in opts['shard'].lower():
                raise CommandError(
                    f'--shard {opts["shard"]!r} não menciona '
                    f'{opts["tribunal"]}: o cursor é POR SHARD, e reaproveitar '
                    f'o cursor de uma varredura completa numa varredura '
                    f'filtrada marcaria como visto o que nunca foi lido. '
                    f'Use --shard {opts["tribunal"].lower()}_only.')
        if not 0 < opts['carga'] <= 1:
            raise CommandError('--carga tem que estar em (0, 1]')
        if opts['lote'] < 1:
            raise CommandError('--lote tem que ser >= 1')

        de = opts['de']
        if de is None:
            guardado = cache.get(CURSOR_KEY % opts['shard']) if opts['shard'] else None
            de = int(guardado) if guardado is not None else 0
        ate = opts['ate'] if opts['ate'] is not None else PK_MAX_PADRAO
        if ate <= de:
            raise CommandError(f'--ate ({ate}) tem de ser maior que --de ({de})')
        self._backfill(de, ate, opts)

    # ------------------------------------------------------------------ #
    def _backfill(self, de: int, ate: int, o: dict) -> None:
        orcamento = tamanho_em_bytes(o['orcamento_bytes'])
        bytes_ini = bytes_ocupados()
        # `--dry-run` não grava o ZERO: ele é estado, e um ensaio que carimba
        # o marco zero do orçamento faria a primeira rodada de verdade contar
        # a partir do lugar errado — em silêncio.
        zero = (cache.get(ZERO_KEY) or {'bytes': bytes_ini, 'em': None,
                                        'tabela_vazia': None}) \
            if o['dry_run'] else zero_do_orcamento(bytes_ini)
        gasto_antes = bytes_ini - int(zero['bytes'])
        if orcamento and gasto_antes >= orcamento:
            raise CommandError(
                f'ORÇAMENTO JÁ ESTOURADO ANTES DE COMEÇAR: as duas tabelas '
                f'ocupam {self._gb(bytes_ini)} e o zero era '
                f'{self._gb(int(zero["bytes"]))} — {self._gb(gasto_antes)} '
                f'gastos contra um orçamento de {self._gb(orcamento)}. '
                f'Nada foi lido. Suba --orcamento-bytes de propósito ou pare.')

        self.stdout.write(
            f'orçamento {self._gb(orcamento) if orcamento else "SEM TETO"} · '
            f'já gastos {self._gb(gasto_antes)} · faixa [{de}, {ate}) · '
            f'shard {o["shard"] or "—"}'
            + (f' · SÓ {o["tribunal"]}' if o['tribunal'] else ''))

        c: Counter = Counter()
        erros: Counter = Counter()
        cache_mag: dict[tuple, int] = {}
        pendentes: list[dict] = []
        cursor = de
        lotes = 0
        ms_id = None
        bytes_agora = bytes_ini
        t_inicio = time.time()
        motivo = 'faixa concluída'

        while cursor < ate:
            if cache.get(PAUSA_KEY):
                motivo = 'kill switch'
                break
            if o['max_segundos'] and (time.time() - t_inicio) >= o['max_segundos']:
                motivo = 'teto de tempo'
                break

            t_lote = time.time()
            cursor_antes = cursor
            linhas, cursor = self._janela(cursor, ate, o['lote'], o['tribunal'])
            if not linhas and cursor >= ate:
                break

            for pk, proc_id, trib, orgao, data, texto in linhas:
                # O teto de publicações é conferido POR LINHA, não por lote.
                # Conferido por lote ele passaria do número pedido em até
                # `--lote − 1` publicações e ainda diria que parou no teto —
                # um teto que não é o teto é pior que teto nenhum, porque
                # ninguém desconfia dele.
                if o['max_publicacoes'] and c['lidas'] >= o['max_publicacoes']:
                    motivo = 'teto de publicações'
                    cursor = pk            # o pk NÃO lido: a retomada começa nele
                    break
                c['lidas'] += 1
                leitura = mag.ler(texto)
                erros.update(leitura.erros)
                if leitura.marcadores_vistos:
                    c['com_marcador'] += 1
                if not leitura.atribuicoes:
                    continue
                c['com_atribuicao'] += 1
                for a in leitura.atribuicoes:
                    c['atribuicoes'] += 1
                    pendentes.append({
                        'chave': (trib, mag.normalizar_orgao(orgao), a.chave_nome),
                        'nome': a.nome, 'orgao': orgao or '', 'cargo': a.cargo,
                        'formato': a.formato, 'processo_id': proc_id,
                        'movimentacao_id': pk,
                        'publicado_em': data.date() if data else None,
                    })
            if len(pendentes) >= LOTE_ESCRITA or motivo == 'teto de publicações':
                self._gravar(pendentes, cache_mag, c, o['dry_run'])
                pendentes.clear()

            if o['shard'] and not o['dry_run']:
                cache.set(CURSOR_KEY % o['shard'], cursor, timeout=None)
            if motivo == 'teto de publicações':
                break

            lotes += 1
            gasto_s = time.time() - t_lote
            ms_id = self._custo_ms_id(ms_id, gasto_s, cursor - cursor_antes)
            bytes_agora = bytes_ocupados() if not o['dry_run'] else bytes_ini

            if lotes % 20 == 1:
                self.stdout.write(
                    f'  pk<{cursor:>12,}  lidas {c["lidas"]:>9,}  '
                    f'atuacoes {c["atuacoes_confirmadas"]:>9,}  '
                    f'+{self._gb(bytes_agora - bytes_ini)}  '
                    f'({self._bpl(bytes_agora - bytes_ini, c["atuacoes_confirmadas"]) or "—"} '
                    f'B/linha · {gasto_s:.2f}s/lote · {ms_id:.2f} ms/id)')
                self.stdout.flush()

            if orcamento and (bytes_agora - int(zero['bytes'])) >= orcamento:
                motivo = 'orçamento de BYTES'
                break
            if o['parar_ms_id'] and lotes >= 10 and ms_id > o['parar_ms_id']:
                motivo = 'teto de custo'
                break
            # Freio proporcional: o banco é disk-I/O-bound e a recuperação da
            # Fase 3 divide o mesmo disco. Pausa fixa é freio fraco onde mais
            # importa — quanto mais caro o lote, mais tempo devolvido.
            descanso = gasto_s * (1.0 - o['carga']) / o['carga']
            if descanso > 0.01:
                time.sleep(min(descanso, 30.0))

        self._gravar(pendentes, cache_mag, c, o['dry_run'])
        if not o['dry_run']:
            bytes_agora = bytes_ocupados()
        if o['conferir']:
            self._conferir(de, cursor, c, o['tribunal'])
        self._resumo(c, erros, de, cursor, ate, o, motivo,
                     bytes_ini, bytes_agora, int(zero['bytes']), orcamento,
                     time.time() - t_inicio, ms_id)

        if motivo != 'faixa concluída':
            # `kill switch` não é teto — é decisão humana. Sai com erro do
            # mesmo jeito (o `exit 0` é que confunde "parei" com "terminei"),
            # mas com a palavra certa: quem lê o log tem de saber se foi o
            # freio automático ou alguém.
            raise CommandError(
                f'{"PARADO" if motivo == "kill switch" else "TETO ATINGIDO"} '
                f'({motivo}) no pk {cursor} de {ate} — a faixa '
                f'NÃO terminou. Retome com '
                f'`backfill_magistrados --shard {o["shard"] or "<shard>"}`'
                + ('' if motivo != 'orçamento de BYTES' else
                   ' DEPOIS de decidir o orçamento novo: os bytes/linha desta '
                   'rodada estão medidos acima, e a extrapolação também.'))

    # -- leitura: uma JANELA de pk por vez ---------------------------------- #
    def _janela(self, de: int, ate: int, lote: int, tribunal: str | None = None):
        """`(linhas, proximo_cursor)` — nunca o resultado inteiro na RAM.

        Cresce a largura até juntar `lote` publicações ou bater em `ate`: o
        espaço de pk tem buracos (cancelamentos, ids de outras tabelas), e
        largura fixa devolveria lotes de tamanho errático — cheios demais num
        trecho denso (o `texto` é o campo grande), vazios num deserto.

        ⚠️ **O cursor avança para depois da ÚLTIMA LINHA LIDA, não para o topo
        da janela** — e essa distinção é a diferença entre varrer e fingir que
        varreu. A primeira versão fazia `cursor = topo` sempre: quando a janela
        de pk continha mais linhas do que o `LIMIT` levava, o resto era pulado
        **em silêncio**. Medido no dev em 03/09/2026, antes de sair daqui:
        33.500 publicações lidas de 147.589 na mesma faixa — **22,7%**, com run
        verde e log limpo. É o `for pagina in range(1, 11)` do `CLAUDE.md` com
        outra roupa, e por isso `--conferir` existe.
        """
        # Com filtro de tribunal a janela tem de ser MAIS LARGA na mesma
        # proporção da fatia dele no acervo, senão cada lote vira uma dezena de
        # idas ao banco. O TJSP é 11,3% do índice, o TJMG 18,2% — 12× cobre o
        # caso ruim sem estourar a memória, porque o `texto` só vem para quem
        # passa no filtro.
        largura = max(lote * (24 if tribunal else 2), 2_000)
        filtro = ' AND tribunal_id = %s' if tribunal else ''
        linhas: list = []
        cursor = de
        while cursor < ate and len(linhas) < lote:
            topo = min(cursor + largura, ate)
            pedido = lote - len(linhas)
            args = [cursor, topo] + ([tribunal] if tribunal else []) + [pedido]
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute('SET LOCAL statement_timeout = %s', [120_000])
                    cur.execute(
                        'SELECT id, processo_id, tribunal_id, nome_orgao, '
                        '       data_disponibilizacao, texto '
                        '  FROM tribunals_movimentacao '
                        ' WHERE id >= %s AND id < %s' + filtro +
                        ' ORDER BY id LIMIT %s',
                        args)
                    rows = cur.fetchall()
            linhas.extend(rows)
            if len(rows) >= pedido:
                # A janela PODE ter sobrado linhas — retoma logo depois da
                # última que entrou, nunca no topo.
                cursor = rows[-1][0] + 1
            elif rows:
                cursor = topo               # janela esgotada de verdade
            else:
                # Faixa DESERTA. Abrir o passo em potências de dois seria
                # rastejar: o espaço de pk vai a 2,4 bilhões e a cauda vazia
                # custaria centenas de consultas para não achar nada. Pergunte
                # ao índice onde está a próxima linha — é um `Index Only Scan`
                # que para no primeiro casamento, e ele responde EXATO em vez
                # de adivinhar o tamanho do buraco.
                proximo, esgotou = self._proxima_pk(topo, ate, tribunal)
                if esgotou:
                    # o salto NÃO respondeu no tempo. Deixar a exceção subir
                    # mataria o processo, e com `--restart unless-stopped` o
                    # container voltaria no mesmo cursor, no mesmo deserto,
                    # para morrer de novo — o `exit 0` que alimentou 351
                    # reinícios com progresso zero, agora com outra roupa.
                    # Avançar um passo largo é pior que o salto exato e MUITO
                    # melhor que um loop: o cursor anda, e o trecho pulado
                    # continua dentro da faixa das próximas janelas.
                    cursor = min(topo + PASSO_CEGO, ate)
                    self.stderr.write(self.style.WARNING(
                        f'salto de deserto não respondeu em [{topo}, {ate}) — '
                        f'avançando {PASSO_CEGO:,} pk às cegas. Não é perda: '
                        f'o trecho é varrido pela janela seguinte.'))
                    continue
                if proximo is None:
                    return linhas, ate      # não há mais nada até `ate`
                cursor = proximo
        return linhas, cursor

    @staticmethod
    def _proxima_pk(de: int, ate: int,
                    tribunal: str | None = None) -> tuple[int | None, bool]:
        """Menor pk existente em `[de, ate)`, ou `None`. Salta o buraco.

        Com `--tribunal`, o deserto é o deserto DELE: perguntar pela próxima
        linha qualquer devolveria a de outro tribunal, a janela seguinte não
        acharia nada e o salto viraria um passo. O `min(id)` filtrado usa
        `mov_tribunal_ativo_idx`? Não — não há `(tribunal_id, id)`. Então este
        caminho pode ser caro numa cauda longa, e é por isso que ele tem teto
        próprio: melhor um `statement_timeout` explodindo do que uma varredura
        que rasteja em silêncio.
        """
        sql = 'SELECT min(id) FROM tribunals_movimentacao WHERE id >= %s AND id < %s'
        args = [de, ate]
        if tribunal:
            sql += ' AND tribunal_id = %s'
            args.append(tribunal)
        # O teto da SONDA não é o teto de uma consulta de tela: ela é uma
        # travessia, e cortá-la cedo é o pior dos mundos — paga-se o tempo
        # inteiro e não se leva a resposta. Medido em 05/09/2026: a sonda
        # filtrada varre ~555 mil pk/s, então 120 s atravessam 66 M — e o
        # deserto do TJSP entre 1,05 bi e 1,50 bi tem 450 M. Com o teto curto,
        # a banda `tjsp_b` ficou 100% do tempo em sonda que morria, avançando
        # 5 M por 120 s: 3 horas para atravessar o que UMA consulta de ~13 min
        # resolve. Sem filtro o `min(id)` usa o índice de pk e responde na
        # hora, então o teto largo só vale para o caso caro.
        teto = 900_000 if tribunal else 120_000
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute('SET LOCAL statement_timeout = %s', [teto])
                    cur.execute(sql, args)
                    return cur.fetchone()[0], False
        except OperationalError:
            # nem com 900 s: aí o deserto é grande demais para uma travessia
            # só. Quem chama avança às cegas — a verdade sai daqui, a decisão
            # não.
            return None, True

    #: Suavização do custo. Um lote sozinho não decide: `checkpoint`, `VACUUM`
    #: e a busca do site produzem picos que somem na passada seguinte.
    ALFA = 0.2

    @staticmethod
    def _custo_ms_id(anterior, gasto_s: float, ids_varridos: int) -> float:
        """ms por ID VARRIDO — o denominador é a LARGURA de pk percorrida.

        Aqui só ~11% das publicações produzem linha: dividir o tempo pelas
        linhas ENCONTRADAS faria um lote normal pontuar ~9× o custo real, e um
        trecho sem nenhuma atribuição pontuaria infinito. Foi esse denominador
        que produziu "16.808 ms/linha" no `backfill_fase` e o `exit 0` que
        alimentou 351 reinícios.
        """
        atual = 1000.0 * gasto_s / max(1, ids_varridos)
        return atual if anterior is None else (
            Command.ALFA * atual + (1 - Command.ALFA) * anterior)

    # -- escrita ------------------------------------------------------------ #
    def _gravar(self, pendentes, cache_mag, c, dry) -> None:
        if not pendentes or dry:
            return
        # 1) os magistrados que a faixa ainda não conhece
        novos = {}
        for p in pendentes:
            if p['chave'] in cache_mag or p['chave'] in novos:
                continue
            trib, orgao_chave, nome_chave = p['chave']
            novos[p['chave']] = Magistrado(
                tribunal_id=trib, nome=p['nome'], nome_chave=nome_chave,
                orgao=p['orgao'], orgao_chave=orgao_chave, cargo=p['cargo'],
                formato=p['formato'], fonte=Magistrado.FONTE_TEXTO)
        if novos:
            Magistrado.objects.bulk_create(list(novos.values()),
                                           ignore_conflicts=True)
            # `ignore_conflicts` NÃO devolve pk das linhas que colidiram — e a
            # colisão é o caso normal aqui (o mesmo magistrado assina milhares
            # de publicações). Quem dá o id é sempre uma leitura de volta,
            # nunca o objeto em memória. As três colunas entram no filtro: só
            # `tribunal × nome` seria produto cartesiano e traria linhas de
            # outros órgãos para o cache.
            achados = Magistrado.objects.filter(
                tribunal_id__in={k[0] for k in novos},
                orgao_chave__in={k[1] for k in novos},
                nome_chave__in={k[2] for k in novos},
            ).values_list('tribunal_id', 'orgao_chave', 'nome_chave', 'id')
            for trib, orgao_chave, nome_chave, mid in achados:
                cache_mag[(trib, orgao_chave, nome_chave)] = mid
            c['magistrados_vistos'] = len(cache_mag)

        # 2) as atuações
        linhas, sem_dono = [], 0
        for p in pendentes:
            mid = cache_mag.get(p['chave'])
            if mid is None:
                sem_dono += 1        # não deveria acontecer; conta em vez de sumir
                continue
            linhas.append((mid, p['processo_id'], p['movimentacao_id'],
                           p['formato'] or '', p['cargo'] or '',
                           p['publicado_em']))
        c['atuacao_sem_dono'] += sem_dono
        if linhas:
            c['atuacoes_oferecidas'] += len(linhas)
            c['atuacoes_confirmadas'] += self._inserir_atuacoes(linhas)

    @staticmethod
    def _inserir_atuacoes(linhas: list[tuple]) -> int:
        """Insere e devolve **quantas linhas entraram de verdade**.

        SQL cru, e não `bulk_create(ignore_conflicts=True)`, por um motivo só:
        o Django não devolve o número de linhas efetivamente inseridas quando
        há conflito, e é esse número o DENOMINADOR de "bytes por linha". Sem
        ele, a medição de espaço seria bytes reais ÷ linhas OFERECIDAS — que
        infla o denominador e faz a tabela parecer mais barata do que é,
        justamente na direção que autoriza gastar mais disco.

        `ON CONFLICT DO NOTHING` cobre a unique `(magistrado, movimentacao_id)`
        e mantém o backfill idempotente: reprocessar uma faixa não duplica nem
        levanta.
        """
        agora = timezone.now()
        vals = ','.join(['(%s,%s,%s,%s,%s,%s,%s)'] * len(linhas))
        params: list = []
        for mid, proc, mov, formato, cargo, pub in linhas:
            params.extend([mid, proc, mov, formato, cargo, pub, agora])
        with connection.cursor() as cur:
            cur.execute(
                'INSERT INTO tribunals_magistradoatuacao '
                '(magistrado_id, processo_id, movimentacao_id, formato, cargo, '
                ' publicado_em, criado_em) VALUES ' + vals +
                ' ON CONFLICT DO NOTHING', params)
            return max(0, cur.rowcount)

    def _conferir(self, de: int, ate: int, c: Counter,
                  tribunal: str | None = None) -> None:
        """Regra nº 5: medir dos DOIS lados. Quantas linhas o trecho TINHA?

        A contagem própria (`lidas`) não prova varredura nenhuma — ela prova
        só que o laço rodou. O que prova é comparar com a fonte: um
        `count(*)` do MESMO trecho de pk. Foi este controle que pegou o cursor
        pulando para o topo da janela (22,7% de cobertura com run verde), e é
        por isso que ele existe em vez de uma afirmação na docstring.
        """
        # Com `--tribunal` o controle tem de contar O MESMO universo que o
        # laço leu. Um `count(*)` sem o filtro acusaria "li 195M de 1,62bi" e
        # reprovaria uma varredura correta — controle que grita errado ensina a
        # ignorar controle, que é pior do que não ter.
        sql = ('SELECT count(*) FROM tribunals_movimentacao '
               'WHERE id >= %s AND id < %s')
        args = [de, ate]
        if tribunal:
            sql += ' AND tribunal_id = %s'
            args.append(tribunal)
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('SET LOCAL statement_timeout = %s', [600_000])
                cur.execute(sql, args)
                no_banco = cur.fetchone()[0]
        c['conferido_no_banco'] = no_banco
        falta = no_banco - c['lidas']
        if falta > 0:
            self.stderr.write(self.style.ERROR(
                f'CONTROLE REPROVADO: o trecho [{de}, {ate}) tem {no_banco:,} '
                f'publicações no banco e eu li {c["lidas"]:,} — '
                f'{falta:,} ({100 * falta / no_banco:.2f}%) NÃO foram lidas. '
                f'A faixa NÃO está coberta; não a marque como feita.'))
            logger.error('backfill_magistrados: varredura INCOMPLETA em '
                         '[%s, %s): %s no banco, %s lidas', de, ate,
                         no_banco, c['lidas'])
        else:
            self.stdout.write(
                f'  controle: {c["lidas"]:,} lidas de {no_banco:,} no trecho '
                f'[{de}, {ate}) — cobertura {100 * c["lidas"] / max(1, no_banco):.2f}%')

    # -- relatório ---------------------------------------------------------- #
    @staticmethod
    def _gb(n: int) -> str:
        if n is None:
            return '—'
        return f'{n / 1024 ** 3:.3f} GiB' if abs(n) >= 1024 ** 3 else \
               f'{n / 1024 ** 2:.1f} MiB'

    @staticmethod
    def _bpl(bytes_delta: int, linhas: int):
        """Bytes por linha. `None` quando não há denominador — nunca 0."""
        if not linhas:
            return None
        return round(bytes_delta / linhas, 1)

    def _resumo(self, c, erros, de, cursor, ate, o, motivo, bytes_ini,
                bytes_fim, zero_bytes, orcamento, dur, ms_id) -> None:
        w = self.stdout.write
        lidas = c['lidas'] or 1
        delta = bytes_fim - bytes_ini
        bpl = self._bpl(delta, c['atuacoes_confirmadas'])
        w('')
        w(f'faixa [{de}, {ate})  parou em pk {cursor:,}  motivo: {motivo}  '
          f'({dur:.0f}s)')
        w(f"  lidas .......................... {c['lidas']:>12,}")
        w(f"  com marcador ................... {c['com_marcador']:>12,}  "
          f"({100 * c['com_marcador'] / lidas:.2f}%)")
        w(f"  com atribuição ................. {c['com_atribuicao']:>12,}  "
          f"({100 * c['com_atribuicao'] / lidas:.2f}%)")
        w(f"  abstenções ..................... "
          f"{max(0, c['com_marcador'] - c['com_atribuicao']):>12,}")
        w(f"  magistrados vistos na rodada ... {c['magistrados_vistos']:>12,}")
        w(f"  atuações oferecidas ............ {c['atuacoes_oferecidas']:>12,}")
        w(f"  atuações CONFIRMADAS ........... {c['atuacoes_confirmadas']:>12,}  "
          f"(rowcount do ON CONFLICT — é este o denominador)")
        if erros:
            w('  abstenções por motivo: ' + ', '.join(
                f'{k}={v}' for k, v in erros.most_common(8)))
        if ms_id is not None:
            w(f'  custo por ID VARRIDO ........... {ms_id:.2f} ms  '
              f'(teto {o["parar_ms_id"]})')
        if o['dry_run']:
            w(self.style.WARNING('  [DRY-RUN: nada gravado, nada medido em disco]'))
            return

        w('')
        w(f'  disco: {self._gb(bytes_ini)} → {self._gb(bytes_fim)}  '
          f'(+{self._gb(delta)} nesta rodada)')
        w(f'  orçamento: {self._gb(bytes_fim - zero_bytes)} de '
          f'{self._gb(orcamento) if orcamento else "SEM TETO"} gastos desde o zero')
        if bpl:
            w(f'  BYTES POR LINHA MEDIDO ......... {bpl:,.1f} B  '
              f'(delta de disco ÷ linhas confirmadas)')
            w(f'  extrapolação para {LINHAS_PROJETADAS:,} linhas: '
              f'{self._gb(int(bpl * LINHAS_PROJETADAS))}')
            if c['lidas']:
                por_mov = c['atuacoes_confirmadas'] / lidas
                w(f'  … e pela taxa MEDIDA nesta faixa ({por_mov:.4f} '
                  f'linha/publicação × {MOVIMENTACOES_TOTAL:,} movimentações): '
                  f'{int(por_mov * MOVIMENTACOES_TOTAL):,} linhas = '
                  f'{self._gb(int(bpl * por_mov * MOVIMENTACOES_TOTAL))}')
        else:
            w('  BYTES POR LINHA ................ não medível: 0 linhas '
              'confirmadas nesta rodada (ausente ≠ zero)')
        if c['atuacao_sem_dono']:
            self.stderr.write(self.style.ERROR(
                f"{c['atuacao_sem_dono']} atuações sem magistrado resolvido — "
                f'isto não deveria acontecer; investigue antes de confiar na faixa.'))
        if motivo != 'faixa concluída':
            logger.error(
                'backfill_magistrados parou por %s no pk %s: %s linhas '
                'confirmadas, %s bytes gastos na rodada, %s B/linha medidos, '
                'extrapolação %s bytes para %s linhas',
                motivo, cursor, c['atuacoes_confirmadas'], delta, bpl,
                int((bpl or 0) * LINHAS_PROJETADAS), LINHAS_PROJETADAS)

    # ------------------------------------------------------------------ #
    def _estado(self) -> None:
        """Orçamento, cursores e bytes/linha — sem escrever nada."""
        agora = bytes_ocupados()
        zero = cache.get(ZERO_KEY) or {}
        with transaction.atomic():
            with connection.cursor() as cur:
                # `SET LOCAL` FORA de transação não faz nada e não avisa —
                # seria um teto de espera que só existe no código (regra nº 7).
                cur.execute('SET LOCAL statement_timeout = 120000')
                cur.execute('SELECT count(*) FROM tribunals_magistradoatuacao')
                n_atu = cur.fetchone()[0]
                cur.execute('SELECT count(*) FROM tribunals_magistrado')
                n_mag = cur.fetchone()[0]
        w = self.stdout.write
        w(f'tabelas ......... {self._gb(agora)}')
        if zero:
            w(f'zero ............ {self._gb(int(zero["bytes"]))} em {zero.get("em")}'
              + ('' if zero.get('tabela_vazia') else
                 '  ⚠ as tabelas NÃO estavam vazias no zero'))
            w(f'gasto ........... {self._gb(agora - int(zero["bytes"]))}')
        else:
            w('zero ............ NUNCA GRAVADO — a próxima largada o grava')
        w(f'magistrados ..... {n_mag:,}')
        w(f'atuações ........ {n_atu:,}')
        bpl = self._bpl(agora - int(zero.get('bytes', 0)), n_atu)
        w(f'bytes/linha ..... {bpl if bpl is not None else "—"}  '
          f'(medido sobre o total, não sobre uma rodada)')
        if bpl:
            w(f'projeção {LINHAS_PROJETADAS:,} linhas: '
              f'{self._gb(int(bpl * LINHAS_PROJETADAS))}')
        w(f'kill switch ..... {"PAUSADO" if cache.get(PAUSA_KEY) else "liberado"}')

    def _contar(self) -> None:
        """Recomputa `n_publicacoes`, `primeira_em` e `ultima_em` de TODOS.

        Um `GROUP BY` só. Roda depois que as faixas fecharam — contar durante
        o backfill produziria um número verdadeiro para outra pergunta.
        """
        agora = timezone.now()
        agregado = (MagistradoAtuacao.objects
                    .values('magistrado_id')
                    .annotate(n=Count('id'), pri=Min('publicado_em'),
                              ult=Max('publicado_em')))
        atualizados = 0
        lote: list[Magistrado] = []
        for linha in agregado.iterator(chunk_size=5000):
            lote.append(Magistrado(
                id=linha['magistrado_id'], n_publicacoes=linha['n'],
                n_publicacoes_em=agora, primeira_em=linha['pri'],
                ultima_em=linha['ult']))
            if len(lote) >= LOTE_ESCRITA:
                Magistrado.objects.bulk_update(
                    lote, ['n_publicacoes', 'n_publicacoes_em',
                           'primeira_em', 'ultima_em'])
                atualizados += len(lote)
                lote.clear()
        if lote:
            Magistrado.objects.bulk_update(
                lote, ['n_publicacoes', 'n_publicacoes_em',
                       'primeira_em', 'ultima_em'])
            atualizados += len(lote)
        total = Magistrado.objects.count()
        self.stdout.write(
            f'contados {atualizados:,} de {total:,} magistrados. Os '
            f'{total - atualizados:,} restantes ficam com n_publicacoes NULL — '
            f'"não contamos", que é diferente de zero.')
