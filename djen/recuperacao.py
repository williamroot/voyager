"""Fase 3 da recuperação nacional — processo, não mutirão.

## O que é a Fase 3

A Fase 2 refez, dia a dia, os 9 tribunais onde o crédito contra a Fazenda nasce
e é pago (`completude_medicoes.FASE_2`). Ela fechou: 3.999 dias-alvo, 1 nunca
refeito em 02/09/2026.

Sobram **19 tribunais** que a mesma varredura de 18/08/2026 provou que sangram
(`RECUPERAVEL_POR_TRIBUNAL` menos `FASE_2`) e que **nunca entraram na fila**:
7.167 dias nunca refeitos de 8.404 dias-alvo — **14,7% honesto** contra os
100,0% da Fase 2, medido em 02/09/2026 com a régua desta tela.

## Por que isto é um cron e não um container solto

A Fase 2 foi um mutirão manual. Ele terminou, ninguém religou, e a máquina
parou **em silêncio**: de 27/08 em diante o país inteiro fecha ~59 runs de
janela-1-dia por dia, que é exatamente a coleta diária dos 59 tribunais e mais
nada. O pico medido tinha sido 1.641/dia. Nenhum alarme disparou, porque não
havia nada dizendo "isto deveria estar acontecendo".

Um processo que para em silêncio é o defeito, não o acidente. Este módulo é o
conserto, e ele tem três propriedades que o mutirão não tinha:

* **retomável** — não guarda estado em memória nem cursor em arquivo. A cada
  tique ele RECALCULA quais dias ainda faltam a partir do `IngestionRun`, com a
  MESMA régua que a tela publica. Reiniciar worker, scheduler ou Redis não
  perde lugar nenhum;
* **auto-curável** — dia que falhou, que foi adiado pelo circuito aberto ou que
  morreu com o work-horse simplesmente reaparece no conjunto pendente do tique
  seguinte. Não depende de FailedJobRegistry nem de ninguém reenfileirar;
* **falante** — teto atingido, circuito aberto, fila cheia e vazão zero saem
  como ERRO **com o número real** (regra nº 2), e vão para o cache que a
  página `/dashboard/ingestao/saude/` lê. Parar é permitido; parar calado não.

## A régua é IMPORTADA, nunca copiada

`RAZAO_CAMINHO_FLAT`, `MIN_ITENS_DIA_GRANDE` e `CORTE_FLAT` vêm de
`dashboard.completude_medicoes`. Se a régua da tela mudar, a fila de trabalho
muda junto — e é isso que se quer. Uma segunda cópia dos limiares aqui
produziria o pior resultado possível: a tela dizendo que falta e o coletor
achando que acabou.

Controle da medição: rodada em 02/09/2026 contra os números que a tela publica,
o SQL deste módulo devolveu **8.404 alvo / 1.140 flat / 7.167 nunca**, dígito
por dígito iguais aos da tela. Régua que não reproduz o número conhecido não
serve para escolher trabalho.

## Por que NÃO se usa `djen.jobs.backfill_dia`

Ele faz skip por `_dia_coberto`, e **todo** dia da Fase 3 já tem run `success`
— foi assim que o teto por UF os decapitou: verde, log limpo, número redondo.
`backfill_dia` pularia os 7.167 sem coletar uma linha.

O que se mantém dele é a **trava** `_chave_coleta` (`SET NX EX` no Redis). Sem
ela, medido em 27/08/2026, o TJSP de um dia foi coletado por 12 runs
concorrentes — 7.437 páginas contra as 264 que o dia exige, 28×. Não perde
dado: queima a banda do CNJ, e é ela que abre o circuito e adia os dias dos
outros tribunais.

## O TJPR está fora, e é decisão comercial

1.152 dias e 38,8 milhões de publicações estimadas — 43% de todo o volume que
resta. Ele ficou fora da Fase 2 por decisão do dono do produto, não por
limitação técnica. Entra com UMA linha, sem deploy:

    manage.py djen_recup_f3 --tjpr-on

## Vazão e o teto contra o CNJ

O teto é **réplicas × `DJEN_PAGINAS_PARALELAS`** (regra dura: ≤ 64), não cada
worker sozinho. Hoje são 14 réplicas × 3 = 42 streams, e 42 já foi o ponto em
que a DJEN começou a devolver 500 em 24/08/2026. **Encher a fila não aumenta a
pressão sobre o CNJ** — ela é fixa pela frota —, então o alvo de jobs em voo
aqui é pequeno de propósito: fila é buffer, não backlog (medido em 24/08/2026:
344 de 1.945 ids da `djen_backfill` eram casca, e casca faz o watchdog achar
que o dia já está a caminho).

A `djen_backfill` é a SEGUNDA fila do `worker_ingestion`
(`rqworker djen_ingestion djen_backfill`): a fronteira diária tem prioridade
sobre a recuperação por construção do RQ, e não por gentileza deste módulo.
"""
import datetime
import logging
import os

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone
from django_rq import job

from dashboard import completude_medicoes as M

logger = logging.getLogger('voyager.djen.recuperacao')

#: Ordem de ataque: TRTs e TJs médios primeiro. Não é volume puro — é onde a
#: publicação trabalhista e os TJs de porte médio somam mais dia-alvo por
#: requisição. O TJPR NÃO está aqui (ver o topo do arquivo).
ORDEM_FASE_3 = [
    'TRT2', 'TRT3', 'TRT15', 'TRT1', 'TRT9', 'TRT4',
    'TJBA', 'TJSC', 'TJPE', 'TJCE', 'TJAM', 'TJMT', 'TJMS', 'TJMA', 'TJSE',
    'TJRR', 'TJAP', 'TJTO',
]

#: Fora por decisão comercial, pronto para entrar com uma linha.
TJPR = 'TJPR'

#: Chaves no Redis. Kill switch e escotilha do TJPR moram aqui, não em env:
#: parar tem que custar segundos, não um deploy. O Redis de prod tem AOF desde
#: 31/08/2026, então elas sobrevivem a restart.
CHAVE_ESTADO = 'djen:recup_f3:v1'
CHAVE_OFF = 'djen:recup_f3:off'
CHAVE_TJPR = 'djen:recup_f3:tjpr'
CHAVE_LIGADO_EM = 'djen:recup_f3:ligado_em'

#: prefixo do job_id. Determinístico por (tribunal, dia) — é o que permite
#: saber, sem tocar no banco, se o dia já está a caminho.
PREFIXO = 'f3'

#: Alvo de jobs da Fase 3 em voo (pendentes + em execução). Pequeno de
#: propósito: com 14 réplicas, 40 é ~3 dias de trabalho por worker à frente.
#: Encher mais não coleta mais rápido (o teto é a frota) e só produz casca.
EM_VOO_ALVO = int(os.environ.get('DJEN_F3_EM_VOO_ALVO', 40))

#: Teto de enfileiramento por tique. Atingi-lo é ERRO logado com o pendente
#: real, nunca um `return` discreto (regra nº 2).
POR_TIQUE = int(os.environ.get('DJEN_F3_POR_TIQUE', 40))

#: Quantas re-coletas pós-corte um dia pode receber antes de virar ACHADO.
#:
#: O conjunto pendente encolhe sozinho: toda re-coleta que fecha `success` tira
#: o dia da conta. Se um dia continua na conta depois de N re-coletas bem
#: sucedidas, ele não é trabalho — é sintoma (a fonte não serve mais o dia, ou
#: a régua não descreve este caso). Insistir nele queima banda do CNJ para
#: sempre. Ele sai da fila e entra no relatório, com nome e número.
TENTATIVAS_MAX = int(os.environ.get('DJEN_F3_TENTATIVAS_MAX', 3))

#: A partir de quantas horas sem UM dia refeito a vazão zero vira ERRO. Folga
#: para o caso legítimo: fim de semana não tem menos dia histórico para refazer
#: (o alvo é passado), então aqui zero por 6 h é sempre anomalia — ao contrário
#: da coleta diária, onde sábado com pouco run é normal.
VAZAO_ZERO_ALERTA_H = int(os.environ.get('DJEN_F3_VAZAO_ZERO_ALERTA_H', 6))

#: Teto de espera de toda leitura deste módulo. Regra nº 7: nada sem teto.
#: Medido em 02/09/2026: a `tribunals_ingestionrun` tem 215.132 linhas e 110 MB,
#: e o SQL abaixo roda em menos de 1 s. 30 s é ~30× a medida — só dispara com o
#: banco em contenção, e aí falha ALTO em vez de segurar o worker.
SQL_TIMEOUT_S = int(os.environ.get('DJEN_F3_SQL_TIMEOUT_S', 30))

#: TTL do retrato no cache. Maior que o tique de propósito: o que a tela mostra
#: quando o processo morre é um retrato VELHO com a idade em letra grande, não
#: um card vazio que parece "nunca existiu".
ESTADO_TTL_S = 6 * 3600


# ── A régua ───────────────────────────────────────────────────────────────
#
# `ult` = o run 1-dia MAIS RECENTE que qualifica como dia-alvo (>= 9.000 itens
# e paginas_lidas > 0). É o mesmo `ult` de `completude_warm._recuperacao`.
#
# `nunca` = razão itens/página < 700 **E** sem `success` pós-corte. As outras
# três combinações da tabela cruzada têm explicação conhecida (o `itensPorPagina`
# dinâmico do conserto do OOM derruba a razão de um dia que saiu flat), e só
# esta pode chegar a zero.
#
# `started_at AT TIME ZONE 'UTC'` e não `AT TIME ZONE 'America/Sao_Paulo'`:
# `CORTE_FLAT` é comparado no Python contra `started_at.replace(tzinfo=None)`,
# e o Django devolve o `timestamptz` já em UTC. Trocar o fuso aqui deslocaria
# o corte em 3 h e mudaria o conjunto de trabalho em silêncio.
_SQL_PENDENTES = """
WITH ult AS (
  SELECT DISTINCT ON (tribunal_id, janela_inicio)
         tribunal_id, janela_inicio, started_at, status, paginas_lidas,
         (COALESCE(movimentacoes_novas,0)+COALESCE(movimentacoes_duplicadas,0)) AS itens
    FROM tribunals_ingestionrun
   WHERE fonte = 'djen' AND janela_inicio = janela_fim
     AND paginas_lidas > 0
     AND (COALESCE(movimentacoes_novas,0)+COALESCE(movimentacoes_duplicadas,0)) >= %(minitens)s
     AND tribunal_id = ANY(%(siglas)s)
   ORDER BY tribunal_id, janela_inicio, started_at DESC
),
nunca AS (
  SELECT tribunal_id, janela_inicio
    FROM ult
   WHERE itens::float / paginas_lidas < %(razao)s
     AND NOT (started_at AT TIME ZONE 'UTC' >= %(corte)s AND status = 'success')
)
SELECT n.tribunal_id, n.janela_inicio,
       (SELECT count(*) FROM tribunals_ingestionrun r
         WHERE r.fonte = 'djen' AND r.tribunal_id = n.tribunal_id
           AND r.janela_inicio = n.janela_inicio AND r.janela_fim = n.janela_inicio
           AND r.started_at AT TIME ZONE 'UTC' >= %(corte)s) AS tentativas
  FROM nunca n
 ORDER BY n.tribunal_id, n.janela_inicio DESC
"""

#: Quantos dias-alvo da Fase 3 fecharam `success` pelo caminho novo numa janela.
#: É MEDIÇÃO de vazão, não estimativa: conta o par (tribunal, dia) que saiu do
#: conjunto pendente, não o número de jobs que rodaram.
_SQL_VAZAO = """
SELECT count(DISTINCT (tribunal_id, janela_inicio))
  FROM tribunals_ingestionrun
 WHERE fonte = 'djen' AND janela_inicio = janela_fim
   AND status = 'success'
   AND tribunal_id = ANY(%(siglas)s)
   AND started_at >= %(desde)s
   AND paginas_lidas > 0
   AND (COALESCE(movimentacoes_novas,0)+COALESCE(movimentacoes_duplicadas,0)) >= %(minitens)s
"""


def siglas_alvo() -> list[str]:
    """Tribunais da Fase 3 na ordem de ataque. TJPR só com a escotilha ligada."""
    siglas = list(ORDEM_FASE_3)
    try:
        if cache.get(CHAVE_TJPR):
            siglas.append(TJPR)
    except Exception:  # noqa: BLE001 — Redis fora não decide escopo comercial
        logger.warning('recup_f3: não li a escotilha do TJPR — sigo sem ele')
    return siglas


def _com_teto(fn):
    """Executa `fn(cursor)` sob `statement_timeout` de VERDADE. Regra nº 7.

    O `transaction.atomic()` não é decoração: o pgbouncer roda em
    transaction-mode, e um `SET statement_timeout` solto vai para uma conexão
    enquanto a query seguinte vai para outra. `SET LOCAL` dentro da transação
    vale para todas as queries dela — é o padrão da casa
    (`dashboard/tasks.py::_with_timeout`, `djen/jobs.py::_leitura_com_teto`).
    """
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [SQL_TIMEOUT_S * 1000])
        return fn(c)


def dias_pendentes(siglas: list[str]) -> list[tuple[str, str, int]]:
    """`(sigla, dia_iso, tentativas_pos_corte)` dos dias NUNCA REFEITOS.

    Recalculado a cada tique — é isto que torna o processo retomável sem
    cursor. O conjunto encolhe monotonicamente: toda re-coleta que fecha
    `success` tira o dia daqui, então ele termina.
    """
    def _q(c):
        c.execute(_SQL_PENDENTES, {
            'minitens': M.MIN_ITENS_DIA_GRANDE,
            'razao': M.RAZAO_CAMINHO_FLAT,
            'corte': M.CORTE_FLAT,
            'siglas': list(siglas),
        })
        return c.fetchall()

    return [(s, d.isoformat(), int(n)) for s, d, n in _com_teto(_q)]


def vazao(siglas: list[str], horas: int = 24) -> int:
    """Dias-alvo da Fase 3 que fecharam `success` nas últimas `horas`."""
    desde = timezone.now() - datetime.timedelta(hours=horas)

    def _q(c):
        c.execute(_SQL_VAZAO, {'siglas': list(siglas), 'desde': desde,
                               'minitens': M.MIN_ITENS_DIA_GRANDE})
        return c.fetchone()[0]

    return int(_com_teto(_q))


def _intercalar(pendentes: list[tuple[str, str, int]],
                ordem: list[str]) -> list[tuple[str, str, int]]:
    """Round-robin entre tribunais, respeitando `ordem` no desempate.

    Sem isto o primeiro tribunal da lista monopoliza a fila e os outros 18
    ficam parados — foi exatamente o que o teto global sozinho fez com os TRTs
    em 29/07/2026 (`BACKFILL_WATERMARK_POR_TRIBUNAL` nasceu daí).
    """
    por_trib: dict[str, list] = {}
    for item in pendentes:
        por_trib.setdefault(item[0], []).append(item)
    # sigla fora de `ordem` entra no fim, nunca some: tribunal novo em
    # `RECUPERAVEL_POR_TRIBUNAL` seria trabalho invisível se caísse aqui.
    fila = [s for s in ordem if s in por_trib]
    fila += [s for s in por_trib if s not in fila]
    saida = []
    while fila:
        proxima = []
        for sig in fila:
            saida.append(por_trib[sig].pop(0))
            if por_trib[sig]:
                proxima.append(sig)
        fila = proxima
    return saida


def _ocupados(fila) -> set[str]:
    """job_ids de 1 dia que já estão a caminho, em QUALQUER dos caminhos.

    Inclui os prefixos das outras rotas (`f2`, `bfd`, `adiado`) de propósito:
    um dia que o watchdog acabou de devolver, ou que o tique do backfill
    retroativo pegou, não pode ganhar uma segunda coleta aqui.

    ⚠️ id na fila NÃO é trabalho na fila. O hash do job some sem levar o id
    junto, e um id CASCA diz "já está a caminho" — sumindo com o dia para
    sempre, porque este é o único lugar que o devolveria. Medido em 24/08/2026:
    344 de 1.945 ids (17,7%) eram casca. Por isso o filtro do `jobs`.
    """
    from djen.jobs import _so_os_que_existem

    ids = set(fila.get_job_ids())
    try:
        from rq.registry import ScheduledJobRegistry, StartedJobRegistry
        ids |= set(StartedJobRegistry(queue=fila).get_job_ids())
        ids |= set(ScheduledJobRegistry(queue=fila).get_job_ids())
    except Exception as exc:  # noqa: BLE001
        logger.warning('recup_f3: não li os registros da fila (%s) — o tique '
                       'segue e pode re-enfileirar dia em voo', str(exc)[:120])
    return _so_os_que_existem(fila, ids)


def _travados() -> set[tuple[str, str]]:
    """`(sigla, dia)` com trava `djen:coletando:*` viva no Redis.

    Complementa `_ocupados`, e a diferença é justamente o caso caro: entre o
    worker pegar o job e o job terminar, o id sai da LISTA da fila mas a trava
    continua. Sem esta leitura o tique de 5 min re-enfileiraria exatamente o
    dia mais longo (o TJSP de 27/08 levou 136 min).

    A chave real carrega o prefixo do cache do Django (`:<versão>:`), então o
    corte é pelo marcador `djen:coletando:`, nunca pelo começo da string —
    casar pelo começo devolveria conjunto vazio e o defeito voltaria calado.
    """
    marcador = 'djen:coletando:'
    try:
        import django_rq
        r = django_rq.get_connection('default')
        pares = set()
        for bruta in r.scan_iter(match=f'*{marcador}*', count=1000):
            k = bruta.decode() if isinstance(bruta, bytes) else bruta
            resto = k.split(marcador, 1)[1]
            sig, _, dia = resto.partition(':')
            if sig and dia:
                pares.add((sig, dia))
        return pares
    except Exception as exc:  # noqa: BLE001 — sem isto o tique ainda funciona
        logger.warning('recup_f3: não varri as travas de coleta (%s)', str(exc)[:120])
        return set()


def estado() -> dict | None:
    """Último retrato do tique. `None` = nunca rodou (ou o cache caiu)."""
    try:
        return cache.get(CHAVE_ESTADO)
    except Exception:  # noqa: BLE001
        return None


@job('djen_backfill', timeout=86400)
def recoletar_dia(tribunal_sigla: str, dia_iso: str) -> dict:
    """Re-coleta UM dia pelo caminho flat. A unidade de trabalho da Fase 3.

    ⚠️ **Sem `_dia_coberto`, e isto é o ponto.** Todo dia da Fase 3 já tem run
    `success` — foi assim que o teto de 10 páginas por fatia de UF decapitou
    43,6% do TJSP todo dia por 17 meses sem nenhum alarme. `backfill_dia`
    pularia os 7.167 dias sem ler uma linha.

    ⚠️ **Com a trava `_chave_coleta`.** Uma coleta por (tribunal, dia) de cada
    vez. Fail-open: Redis fora deixa a coleta seguir — perder a trava custa
    duplicação, perder o dia custa acervo.

    Circuito aberto **ADIA**, não falha: devolve `skip` e o tique seguinte
    reencontra o dia no conjunto pendente. Não empilha no FailedRegistry (em
    19/08/2026 foram 4.153 runs marcados como falha que eram jobs adiados, e
    isso esconde a falha de verdade no meio do ruído).
    """
    from datetime import date

    from tribunals.models import Tribunal

    from .client import DjenBusyError
    from .ingestion import ingest_window
    from .jobs import COLETA_LOCK_TTL_S, _chave_coleta

    t = Tribunal.objects.get(sigla=tribunal_sigla)
    dia = date.fromisoformat(dia_iso)

    chave = _chave_coleta(tribunal_sigla, dia_iso)
    travou = True
    try:
        travou = bool(cache.add(chave, 1, COLETA_LOCK_TTL_S))
    except Exception as exc:  # noqa: BLE001 — Redis fora: fail-open
        logger.warning('recup_f3 %s %s: trava indisponível (%s) — sigo sem ela',
                       tribunal_sigla, dia_iso, str(exc)[:120])
    if not travou:
        logger.warning(
            'recup_f3 %s %s: já há coleta EM VOO deste dia — não abro uma '
            'segunda. Duplicar não traz publicação e queima a banda do CNJ que '
            'o circuit-breaker protege', tribunal_sigla, dia_iso)
        return {'skip': 'ja_em_coleta', 'tribunal': tribunal_sigla, 'dia': dia_iso}

    logger.info('recup_f3 inicio %s %s', tribunal_sigla, dia_iso)
    try:
        run = ingest_window(t, dia, dia)
    except DjenBusyError:
        logger.warning('recup_f3 adiado %s %s (DJEN circuito aberto) — o dia '
                       'volta sozinho no próximo tique', tribunal_sigla, dia_iso)
        return {'skip': 'circuito_aberto', 'tribunal': tribunal_sigla, 'dia': dia_iso}
    finally:
        try:
            cache.delete(chave)
        except Exception:  # noqa: BLE001
            pass                                   # o TTL cobre
    logger.info('recup_f3 fim %s %s → novas=%d dup=%d pgs=%d run=%d',
                tribunal_sigla, dia_iso, run.movimentacoes_novas,
                run.movimentacoes_duplicadas, run.paginas_lidas, run.pk)
    return {'run_id': run.pk, 'novas': run.movimentacoes_novas,
            'pgs': run.paginas_lidas, 'tribunal': tribunal_sigla, 'dia': dia_iso}


@job('default', timeout=120)
def tick_recuperacao_fase3() -> dict:
    """Alimenta a `djen_backfill` com dias nunca refeitos da Fase 3.

    Sem estado próprio: o conjunto pendente sai do banco a cada passada. É o
    que torna reinício, deploy e queda de Redis eventos sem consequência.

    Todo caminho de parada — pausado, circuito aberto, fila cheia, teto do
    tique, dia teimoso — sai no retorno E no cache que a tela lê. Nenhum
    `return` discreto (regra nº 2).
    """
    import django_rq

    from .client import circuit_is_open

    agora = timezone.now()
    r: dict = {'medido_em': agora, 'enfileirados': 0, 'motivo_parada': None}

    try:
        if cache.get(CHAVE_OFF):
            r['motivo_parada'] = 'pausado'
            logger.warning('recup_f3: PAUSADO por kill switch (%s). Nada será '
                           'enfileirado até `djen_recup_f3 --religar`', CHAVE_OFF)
            _guardar(r)
            return r
    except Exception:  # noqa: BLE001 — Redis fora não deve travar a recuperação
        logger.warning('recup_f3: não li o kill switch — sigo LIGADO')

    siglas = siglas_alvo()
    r['tjpr_ligado'] = TJPR in siglas

    # As duas medições vêm antes de qualquer decisão: mesmo parado, o retrato
    # tem que dizer o tamanho do buraco. Card vazio some da vista; número alto
    # ao lado de "parado" não.
    pendentes = dias_pendentes(siglas)
    r['pendentes'] = len(pendentes)
    por_trib: dict[str, int] = {}
    for sig, _dia, _n in pendentes:
        por_trib[sig] = por_trib.get(sig, 0) + 1
    r['por_tribunal'] = sorted(por_trib.items(), key=lambda kv: -kv[1])
    r['vazao_24h'] = vazao(siglas, horas=24)

    # Dia teimoso: re-coletado TENTATIVAS_MAX vezes pelo caminho novo e ainda
    # na conta. Sai da fila e entra no ERRO, com nome. É achado, não trabalho.
    teimosos = [(s, d, n) for s, d, n in pendentes if n >= TENTATIVAS_MAX]
    r['teimosos'] = len(teimosos)
    if teimosos:
        logger.error(
            'recup_f3: %d dias já foram re-coletados %d+ vezes pelo caminho novo '
            'e CONTINUAM na conta de nunca-refeito — isto é achado, não fila. '
            'Amostra: %s. Ou a fonte não serve mais o dia, ou a régua não '
            'descreve este caso; investigue antes de insistir',
            len(teimosos), TENTATIVAS_MAX,
            [f'{s} {d} ({n}x)' for s, d, n in teimosos[:8]])
    candidatos = [(s, d, n) for s, d, n in pendentes if n < TENTATIVAS_MAX]

    if circuit_is_open():
        r['motivo_parada'] = 'circuito_aberto'
        logger.warning(
            'recup_f3: DJEN com circuito ABERTO — %d dias pendentes esperando. '
            'Não é falha e não perde dia nenhum: o tique volta em minutos e o '
            'conjunto pendente é recalculado do zero', len(candidatos))
        _guardar(r)
        return r

    fila = django_rq.get_queue('djen_backfill')
    from .jobs import BACKFILL_WATERMARK
    q_len = len(fila)
    if q_len >= BACKFILL_WATERMARK:
        r['motivo_parada'] = 'fila_cheia'
        logger.error(
            'recup_f3: fila djen_backfill com %d jobs (teto %d) — %d dias da '
            'Fase 3 esperando e NENHUM enfileirado neste tique',
            q_len, BACKFILL_WATERMARK, len(candidatos))
        _guardar(r)
        return r

    ocupados = _ocupados(fila)
    travados = _travados()
    # Em voo = job DESTA rota que ainda existe. Conta só o `f3:`: os `f2:`,
    # `bfd:` e `adiado:` são trabalho de outras rotas e ocupam o dia sem
    # consumir a cota da Fase 3.
    em_voo = sum(1 for x in ocupados if x.startswith(f'{PREFIXO}:'))
    r['em_voo'] = em_voo
    r['travas_vivas'] = len(travados)

    livres = [(s, d) for s, d, _n in candidatos
              if (s, d) not in travados
              and not any(f'{p}:{s}:{d}' in ocupados
                          for p in (PREFIXO, 'f2', 'bfd', 'adiado'))]

    vagas = min(POR_TIQUE, max(0, EM_VOO_ALVO - em_voo),
                max(0, BACKFILL_WATERMARK - q_len))
    escolhidos = _intercalar([(s, d, 0) for s, d in livres], ORDEM_FASE_3)[:vagas]

    for sig, dia, _n in escolhidos:
        fila.enqueue(recoletar_dia, sig, dia,
                     job_id=f'{PREFIXO}:{sig}:{dia}', job_timeout=86400)
    r['enfileirados'] = len(escolhidos)

    if not escolhidos and candidatos and em_voo == 0 and not travados:
        # Nem trabalho em voo, nem trabalho enfileirado, e a conta não é zero:
        # isto é a máquina parada. Foi este estado exato que durou de 27/08 a
        # 02/09/2026 sem nenhum alarme.
        r['motivo_parada'] = 'nada_enfileirado'
        logger.error(
            'recup_f3: %d dias pendentes, NADA em voo e NADA enfileirado neste '
            'tique (fila=%d, alvo em voo=%d). A recuperação da Fase 3 está '
            'PARADA — foi este estado que durou de 27/08 a 02/09/2026 calado',
            len(candidatos), q_len, EM_VOO_ALVO)
    elif len(livres) > len(escolhidos):
        logger.info('recup_f3: +%d dias enfileirados (%d pendentes, %d em voo, '
                    'teto do tique %d)', len(escolhidos), len(candidatos),
                    em_voo, POR_TIQUE)

    _alerta_vazao_zero(r, candidatos)
    _guardar(r)
    return r


def _alerta_vazao_zero(r: dict, candidatos: list) -> None:
    """Vazão zero com trabalho na conta é ERRO — depois de um período de graça.

    O período existe porque logo depois de religar a vazão É zero por
    construção: o primeiro dia ainda está sendo coletado. Alarme que acende
    sempre é alarme gasto (foi a lição do detector de frota, 21/08/2026).
    """
    ligado_em = None
    try:
        ligado_em = cache.get(CHAVE_LIGADO_EM)
        if ligado_em is None:
            cache.set(CHAVE_LIGADO_EM, timezone.now(), None)
    except Exception:  # noqa: BLE001
        return
    if not candidatos or r.get('vazao_24h'):
        return
    if ligado_em is None:
        return
    horas = (timezone.now() - ligado_em).total_seconds() / 3600.0
    r['horas_ligado'] = round(horas, 1)
    if horas < VAZAO_ZERO_ALERTA_H:
        return
    logger.error(
        'recup_f3: VAZÃO ZERO — nenhum dos %d dias pendentes da Fase 3 fechou '
        '`success` em 24 h, com o processo ligado há %.0f h. A fila anda '
        '(%d enfileirados neste tique, %d em voo) e mesmo assim nada saiu da '
        'conta: procure o worker, não a fila',
        len(candidatos), horas, r.get('enfileirados', 0), r.get('em_voo', 0))


def _guardar(r: dict) -> None:
    """Retrato para a tela. Nunca propaga erro — medir não pode derrubar o tique."""
    try:
        cache.set(CHAVE_ESTADO, r, ESTADO_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning('recup_f3: não guardei o retrato no cache', exc_info=True)
