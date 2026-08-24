"""Gate de completude do ÍNDICE para a porta do Datajud.

O que a porta escreve tem que virar dado BUSCÁVEL — e até 24/08/2026 nada no
sistema afirmava isso. Os dois buracos, medidos em produção com amostra
aleatória e `_mget` por id (resposta exata por documento, não estimativa):

    MOVIMENTAÇÕES (`bulk_create` ⇒ sem `post_save`)
      idade da escrita   linhas na janela   amostra   fora do índice
      0-5 min                     3.088       3.000   3.000 (100,00%)
      5-15 min                    3.927       3.000   1.268 ( 42,27%)
      15-30 min                   4.133       3.000       0
      30-60 min                  15.362       3.000       0
      1-2 h / 2-4 h              20.000+      3.000       0

    PROCESSOS (`.update()` ⇒ sem `post_save` E sem `atualizado_em`)
      janela de `data_enriquecimento_datajud`   amostra   doc em dia
      30-15 min                                     500   0
      2h-30min                                      500   0
      1d-2h                                         500   8   (1,6%)
      3d-1d                                         500   98  (19,6%)
      7d-3d                                         500   73  (14,6%)
      30d-7d                                        500   0

O primeiro buraco era ESPERA: a única coisa que levava as linhas ao índice era
o poller de 10 minutos (`search/sync_incremental.py`), e a medição acima foi
feita com ele SAUDÁVEL (atraso de 122.604 ids ≈ 1 tick). O segundo não tinha
poller nenhum: `.update()` ignora o `auto_now` de `atualizado_em`, que é
exatamente a chave do keyset de `sync_processos_atualizados`.

## O recorte, e por que ele é este

O da porta do diário é `(tribunal, dia)` porque lá a unidade de coleta é um
caderno inteiro de um dia. Aqui não serve: o Datajud entrega o histórico
INTEIRO de um processo numa requisição, então uma sincronização espalha linhas
por décadas de `data_disponibilizacao`. O recorte que corresponde ao trabalho
desta porta é a **janela de ESCRITA**, e ela é barata porque existe o índice
`mov_inserido_tribunal_idx (inserido_em DESC, tribunal_id)`.

Custo medido com `EXPLAIN (ANALYZE, BUFFERS)` em produção, 24/08/2026:

    janela    linhas datajud   descartadas pelo filtro   tempo    blocos
    15 min             4.103                    13.529   2,33 s     5.201
    15 min (2ª vez)    4.103                    13.529   0,01 s     5.201 (hit)
    60 min            28.983                    78.943   6,64 s    31.175
    4 h              200.000 (TETO)            124.950   8,50 s    62.551

Por isso o passo COMEÇA em **15 minutos**. Para comparação, o gate do diário
mediu 29,2 s e 65.846 blocos para recortar UMA edição — e por isso lá o recorte
subiu para o dia inteiro. Aqui a conta deu ao contrário: o recorte fino é o
barato.

O lado dos processos usa `proc_datajud_em_idx (data_enriquecimento_datajud)`:
a mesma janela de 15 minutos custou **0,29 s** para 1.296 processos.

## O passo é PONTO DE PARTIDA, porque a vazão varia 28x no mesmo dia

Linhas que a porta escreveu, por hora cheia, medidas em 24/08/2026:

    03h UTC   59.250      09h UTC  403.346
    04h UTC  268.218      10h UTC  385.846
    05h UTC  367.495      11h UTC  479.568
    06h UTC  385.359      12h UTC  798.824   <- pico
    07h UTC  360.631      13h UTC   36.464
    08h UTC  357.186      14h UTC   28.610   <- vale
                          média das 12 horas: 327.566

Os 27.468/h da primeira medição caíram num VALE (o `reabastecer_fila_datajud`
tinha acabado de secar). No pico, um passo de 15 minutos tem 200 mil linhas e
bate o teto de leitura SEMPRE — e um gate que PARA no teto ficaria congelado no
mesmo instante para sempre, que é o oposto do que ele existe para impedir. Por
isso `_conferir_passo` **divide a janela ao meio** quando o teto aparece, do
mesmo jeito que `search/jobs.py::_enviar_bulk` divide o `_bulk` no 413: o erro
não é de dado, é de TAMANHO. Só o recorte MÍNIMO que ainda não cabe vira ERRO
registrado e dívida visível.

A conta do teto de 100 rpm da APIKey compartilhada fecha com isso: 5.628
processos numa hora medida x 74 movimentos por processo = 416.186 linhas, que
é exatamente o que o gate contou naquela hora.

## As duas regras que este módulo não negocia

  · **ES mudo devolve "não sei", nunca 0** (regra nº 6). Zero faria a régua
    gritar que a janela inteira está fora do índice e o reparo re-enfileirar
    dezenas de milhares de linhas por nada.
  · **Teto é ALERTA, nunca corte mudo** (regra nº 2). Bater o teto de leitura
    não avança o watermark além do que foi conferido: a dívida fica visível e
    a próxima passada continua de onde parou.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import OperationalError, connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from search import gate
from tribunals.models import Movimentacao, Process

logger = logging.getLogger('voyager.datajud.indice')

#: Prefixo do `external_id` desta porta (`datajud/parser.py::build_external_id`).
#: Filtrado com LIKE, NUNCA com `__gte/__lt`: a collation `en_US.UTF-8` ignora
#: pontuação, então `external_id < 'datajud;'` compara como se fosse `datajud`
#: e a faixa devolve 0 linhas. Já custou um alarme falso.
PREFIXO = 'datajud:'

#: Carência antes de conferir uma janela. A entrega ao índice é assíncrona
#: (fila `es_index`) e o dreno leva minutos; conferir na hora só mediria a
#: profundidade da fila e re-enfileiraria tudo de novo. 20 min é o mesmo valor
#: do gate do diário e cobre com folga o tick de 10 min do poller.
CARENCIA_MIN = 20

#: Passo do recorte — o número medido acima. Cada passo é uma pergunta ao
#: Postgres e 1-3 ao Elasticsearch. É um ponto de PARTIDA: se o recorte não
#: couber no teto de leitura, `_conferir_passo` divide ao meio até caber.
PASSO_MIN = 15
#: Recorte mínimo da divisão. Abaixo disso, teto atingido vira ERRO registrado
#: e dívida visível. Com o teto de 200.000 ids, 1 minuto só não caberia a uma
#: vazão de 12 milhões de linhas/hora — 15x o pico já medido (798.824/h).
PASSO_MIN_MINIMO = 1

#: Quanto de janela uma passada do cron cobre, no máximo. Com o cron de 15 min
#: e 60 min por passada, o gate recupera 45 min de atraso a cada passada.
JANELA_MAX_MIN = 60

#: Teto de ids lidos do Postgres por passo. Existe por MEMÓRIA: 200 mil ids em
#: lista Python são ~8 MB, e o `worker_default` roda com `mem_limit 512m`.
#: Atingi-lo é ERRO registrado e o watermark NÃO passa do passo (regra nº 2).
TETO_IDS_MOVS = 200_000
#: Idem para o lado dos processos (medido: 1.296 numa janela de 15 min).
TETO_IDS_PROCS = 50_000

#: Teto de espera do Postgres. Medido: o passo de 15 min custa 2,33 s frio.
#: 120 s é folga de 50x. O banco de produção NÃO tem `statement_timeout`
#: global (medido: `SHOW statement_timeout` = 0), então sem isto uma consulta
#: pendurada segura conexão do pgbouncer indefinidamente.
PG_TIMEOUT = '120s'

#: Watermark: o instante até onde a janela de escrita já foi conferida.
WM = 'datajud:gate:wm'
#: Telemetria da última passada (lida pela dashboard/Command Center).
ULTIMO = 'datajud:gate:ultimo'

#: Se o watermark sumir do cache, re-ancora ESTE tanto para trás — e loga ERRO.
#: Re-ancorar no TOPO é o defeito que custou 27.619 linhas ao diário (o poller
#: faz exatamente isso); re-ancorar 6 h atrás perde no máximo 6 h e GRITA, e o
#: que ficou fora se recupera por `manage.py datajud_conferir_indice --desde`.
RE_ANCORA_HORAS = 6

#: Atraso do gate acima do qual a passada vira ERRO: ele deixou de ser um gate
#: e virou um backlog.
ALERTA_ATRASO_HORAS = 4


def gate_ativo() -> bool:
    """`DATAJUD_GATE_INDICE_ENABLED` — desligável sem deploy, ligado por padrão."""
    return bool(getattr(settings, 'DATAJUD_GATE_INDICE_ENABLED', True))


def _leitura_com_teto(qs, teto: int):
    """Lê até `teto + 1` valores com `statement_timeout` próprio.

    Devolve `(valores, teto_atingido)` ou `(None, False)` quando o Postgres não
    respondeu dentro do teto — e aí é ABSTENÇÃO, não lista vazia.
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
            valores = list(qs[:teto + 1])
    except OperationalError:
        logger.error('gate datajud: leitura no PG estourou %s — ABSTENDO '
                     '(o watermark não anda).', PG_TIMEOUT)
        return None, False
    if len(valores) > teto:
        return valores[:teto], True
    return valores, False


# ─────────────────────────────────────────────────────────────────────────────
# Lado das MOVIMENTAÇÕES
# ─────────────────────────────────────────────────────────────────────────────
def conferir_movs(ini: dt.datetime, fim: dt.datetime, reparar: bool = True) -> dict:
    """Mede os dois lados das movimentações escritas pela porta em [ini, fim).

    Os dois lados usam o MESMO critério porque o lado do ES é perguntado pelos
    MESMOS ids que o Postgres devolveu (`_mget` por `_id`) — não há janela de
    fuso para errar aqui, que é o erro que já produziu 1.029 linhas de
    diferença falsa num dia do TJSP no gate do diário.

    `faltando=None` significa NÃO SEI (Postgres ou ES mudos). Nunca 0.
    """
    qs = (Movimentacao.objects
          .filter(inserido_em__gte=ini, inserido_em__lt=fim,
                  external_id__startswith=PREFIXO)
          .values_list('id', flat=True))
    ids, teto = _leitura_com_teto(qs, TETO_IDS_MOVS)
    if ids is None:
        return {'pg': None, 'faltando': None, 'enfileiradas': 0,
                'teto_atingido': False, 'abstido': True}
    if teto:
        # Volta na hora, SEM medir e SEM reparar: o recorte é grande demais e
        # os números desta leitura truncada não valem nada (são "os primeiros
        # 200.000 que o índice devolveu"). Quem chama divide a janela ao meio
        # e mede as metades — ver `conferir_janela`.
        logger.warning(
            'gate datajud: TETO de %d ids de movimentação em [%s, %s) — janela '
            'grande demais, vai ser dividida.',
            TETO_IDS_MOVS, ini.isoformat(), fim.isoformat(),
        )
        return {'pg': len(ids), 'faltando': None, 'enfileiradas': 0,
                'teto_atingido': True, 'abstido': False}
    if not ids:
        return {'pg': 0, 'faltando': 0, 'enfileiradas': 0,
                'teto_atingido': teto, 'abstido': False}

    faltando: list[int] = []
    try:
        for i in range(0, len(ids), gate.BLOCO_TERMS):
            faltando.extend(gate.ausentes_no_bloco(ids[i:i + gate.BLOCO_TERMS]))
    except Exception:      # ES fora não pode virar "faltam todas"
        logger.warning('gate datajud: ES mudo nas movimentações de [%s, %s) — '
                       'abstendo', ini.isoformat(), fim.isoformat(), exc_info=True)
        return {'pg': len(ids), 'faltando': None, 'enfileiradas': 0,
                'teto_atingido': teto, 'abstido': True}

    enfileiradas = 0
    if faltando and reparar:
        # Propaga: reparo que não enfileirou não é reparo, e carimbar a janela
        # como conferida aqui seria pôr selo de qualidade sobre o buraco.
        enfileiradas = gate.enfileirar_movs(faltando)
    if faltando:
        logger.error(
            'gate datajud: %d de %d movimentações escritas em [%s, %s) estavam '
            'FORA do índice (%.2f%%) — %s.',
            len(faltando), len(ids), ini.isoformat(), fim.isoformat(),
            100.0 * len(faltando) / len(ids),
            f'{enfileiradas} re-enfileiradas' if reparar else 'reparo DESLIGADO',
        )
    return {'pg': len(ids), 'faltando': len(faltando), 'enfileiradas': enfileiradas,
            'teto_atingido': teto, 'abstido': False}


# ─────────────────────────────────────────────────────────────────────────────
# Lado dos PROCESSOS
# ─────────────────────────────────────────────────────────────────────────────
def _atrasados_no_bloco(pares: list[tuple[int, dt.datetime]]) -> list[int]:
    """Quais destes processos têm doc DESATUALIZADO em relação a esta porta.

    O critério é de UMA linha e é exato: o doc do processo carrega
    `enriquecido_em = max(data_enriquecimento_datajud, ..._tribunal, ..._djen,
    enriquecido_em)` (`search/documents.py::processo_to_doc`). Se o doc tivesse
    sido indexado DEPOIS desta sincronização, o `enriquecido_em` dele seria
    ≥ o `data_enriquecimento_datajud` que está no Postgres. Menor (ou ausente,
    ou doc inexistente) ⇒ o doc é anterior à escrita desta porta.

    Comparar `codigo_classe`/`total_movimentacoes` também funcionaria, mas
    esses campos têm outros donos (o enricher escreve classe; o trigger SQL da
    ingestão DJEN escreve `total_movimentacoes`) e a régua ficaria acusando
    dívida de outra porta.
    """
    if not pares:
        return []
    es = gate._es()
    idx = gate.indice_processos()
    atrasados: list[int] = []
    for i in range(0, len(pares), gate.BLOCO_MGET):
        bloco = pares[i:i + gate.BLOCO_MGET]
        r = es.mget(index=idx, ids=[str(p) for p, _ in bloco],
                    source=['enriquecido_em'], request_timeout=gate.ES_TIMEOUT)
        por_id = {int(d['_id']): d for d in r['docs']}
        for pk, escrito_em in bloco:
            d = por_id.get(pk)
            if d is None or not d.get('found'):
                atrasados.append(pk)
                continue
            # `parse_datetime` do Django, não `dateutil`: o valor no `_source`
            # é o `isoformat()` que nós mesmos gravamos, e uma dependência a
            # menos no caminho do gate é uma a menos para faltar na imagem.
            # Data ilegível conta como ATRASADA — abster para o lado de
            # reindexar é barato; o contrário é dar por buscável o que não é.
            bruto = (d.get('_source') or {}).get('enriquecido_em')
            quando = parse_datetime(bruto) if bruto else None
            if quando is None or quando < escrito_em:
                atrasados.append(pk)
    return atrasados


def conferir_processos(ini: dt.datetime, fim: dt.datetime, reparar: bool = True) -> dict:
    """Mede os dois lados dos processos que a porta tocou em [ini, fim)."""
    qs = (Process.objects
          .filter(data_enriquecimento_datajud__gte=ini,
                  data_enriquecimento_datajud__lt=fim)
          .values_list('id', 'data_enriquecimento_datajud'))
    pares, teto = _leitura_com_teto(qs, TETO_IDS_PROCS)
    if pares is None:
        return {'pg': None, 'atrasados': None, 'enfileirados': 0,
                'teto_atingido': False, 'abstido': True}
    if teto:
        # Mesma regra do lado das movimentações: leitura truncada não vira
        # medição. Volta e deixa `conferir_janela` dividir a janela.
        logger.warning(
            'gate datajud: TETO de %d processos em [%s, %s) — janela grande '
            'demais, vai ser dividida.',
            TETO_IDS_PROCS, ini.isoformat(), fim.isoformat(),
        )
        return {'pg': len(pares), 'atrasados': None, 'enfileirados': 0,
                'teto_atingido': True, 'abstido': False}
    if not pares:
        return {'pg': 0, 'atrasados': 0, 'enfileirados': 0,
                'teto_atingido': teto, 'abstido': False}

    try:
        atrasados = _atrasados_no_bloco(list(pares))
    except Exception:
        logger.warning('gate datajud: ES mudo nos processos de [%s, %s) — abstendo',
                       ini.isoformat(), fim.isoformat(), exc_info=True)
        return {'pg': len(pares), 'atrasados': None, 'enfileirados': 0,
                'teto_atingido': teto, 'abstido': True}

    enfileirados = 0
    if atrasados and reparar:
        enfileirados = gate.enfileirar_processos(atrasados)
    if atrasados:
        logger.error(
            'gate datajud: %d de %d processos tocados em [%s, %s) tinham doc '
            'ANTERIOR à escrita desta porta (%.2f%%) — %s.',
            len(atrasados), len(pares), ini.isoformat(), fim.isoformat(),
            100.0 * len(atrasados) / len(pares),
            f'{enfileirados} re-enfileirados' if reparar else 'reparo DESLIGADO',
        )
    return {'pg': len(pares), 'atrasados': len(atrasados), 'enfileirados': enfileirados,
            'teto_atingido': teto, 'abstido': False}


# ─────────────────────────────────────────────────────────────────────────────
# A passada
# ─────────────────────────────────────────────────────────────────────────────
def _conferir_passo(ini: dt.datetime, fim: dt.datetime, reparar: bool,
                    total: dict) -> bool:
    """Confere UM recorte. Devolve True se ele FECHOU (medido dos dois lados).

    Bateu o teto de leitura ⇒ **divide ao meio e mede as metades**, em vez de
    parar. Isso não é conveniência: a vazão desta porta varia 28x ao longo do
    dia (medido em 24/08/2026, linhas escritas por hora — 28.610 às 14h UTC
    contra **798.824** às 12h UTC, média de 327.566 nas 12 horas medidas).
    Com um passo fixo de 15 min, a hora de pico dá 200 mil linhas por passo e
    bate o teto SEMPRE — e um gate que para no teto ficaria congelado no mesmo
    instante para sempre, que é o oposto do que ele existe para impedir.

    É o mesmo remédio que `search/jobs.py::_enviar_bulk` já usa para o 413 do
    Elasticsearch: o erro não é de dado, é de TAMANHO, e a mesma faixa dividida
    passa. Só quando nem o recorte mínimo cabe é que vira ERRO registrado e a
    janela fica em dívida.
    """
    m = conferir_movs(ini, fim, reparar=reparar)
    p = conferir_processos(ini, fim, reparar=reparar)
    total['passos'] += 1

    if m['teto_atingido'] or p['teto_atingido']:
        # A leitura foi truncada: estes números são "os primeiros N que o
        # índice devolveu", não a janela. NÃO entram no total — quem mede são
        # as metades.
        if fim - ini <= dt.timedelta(minutes=PASSO_MIN_MINIMO):
            total['teto'] = True
            logger.error(
                'gate datajud: TETO atingido no recorte MÍNIMO de %d min em '
                '[%s, %s) — a janela NÃO fechou, o watermark não passa dela e '
                'a próxima passada tem que recomeçar daqui.',
                PASSO_MIN_MINIMO, ini.isoformat(), fim.isoformat(),
            )
            return False
        meio = ini + (fim - ini) / 2
        return (_conferir_passo(ini, meio, reparar, total)
                and _conferir_passo(meio, fim, reparar, total))

    for chave, valor in (('movs_pg', m['pg']), ('movs_fora', m['faltando']),
                         ('movs_enfileiradas', m['enfileiradas']),
                         ('procs_pg', p['pg']), ('procs_atrasados', p['atrasados']),
                         ('procs_enfileirados', p['enfileirados'])):
        if valor:
            total[chave] += valor
    if m['abstido'] or p['abstido']:
        total['abstidos'] += 1
        return False
    return True


def conferir_janela(ini: dt.datetime, fim: dt.datetime, reparar: bool = True,
                    passo_min: int = PASSO_MIN) -> dict:
    """Percorre [ini, fim) em passos de `passo_min` e confere os dois lados.

    Devolve, além dos totais, `ate` — o instante até onde a conferência
    REALMENTE fechou. Quem chama usa isso como watermark: um passo que absteve
    (ou que nem dividido coube no teto) para o avanço ali mesmo, e a próxima
    passada recomeça dele. É a diferença entre "não sei" e "está tudo certo".
    """
    total = {'passos': 0, 'movs_pg': 0, 'movs_fora': 0, 'movs_enfileiradas': 0,
             'procs_pg': 0, 'procs_atrasados': 0, 'procs_enfileirados': 0,
             'abstidos': 0, 'teto': False, 'ate': ini.isoformat()}
    passo = dt.timedelta(minutes=passo_min)
    cursor = ini
    while cursor < fim:
        prox = min(cursor + passo, fim)
        if not _conferir_passo(cursor, prox, reparar, total):
            break
        cursor = prox
        total['ate'] = cursor.isoformat()
    return total


def tick(reparar: bool = True, agora: dt.datetime | None = None) -> dict:
    """Uma passada do gate: do watermark até `agora - CARENCIA_MIN`.

    Idempotente e retomável. O watermark é o instante de escrita já conferido;
    ele só avança até onde a conferência FECHOU (ver `conferir_janela`).
    """
    if not gate_ativo():
        return {'skip': 'gate desligado (DATAJUD_GATE_INDICE_ENABLED=0)'}
    agora = agora or timezone.now()
    limite = agora - dt.timedelta(minutes=CARENCIA_MIN)

    wm = cache.get(WM)
    reancorou = False
    if wm is None:
        wm = limite - dt.timedelta(hours=RE_ANCORA_HORAS)
        reancorou = True
        logger.error(
            'gate datajud: watermark AUSENTE do cache — re-ancorando %d h atrás '
            '(%s). O que a porta escreveu antes disso NÃO será conferido por '
            'este cron; recupere com `manage.py datajud_conferir_indice '
            '--desde ... --ate ...`.', RE_ANCORA_HORAS, wm.isoformat(),
        )
    if wm >= limite:
        return {'skip': 'nada fora da carência', 'wm': wm.isoformat()}

    atraso_h = (limite - wm).total_seconds() / 3600.0
    if atraso_h > ALERTA_ATRASO_HORAS:
        logger.error(
            'gate datajud: o gate está %.1f h atrás da escrita (watermark=%s). '
            'Ele deixou de ser gate e virou backlog — cada passada cobre no '
            'máximo %d min.', atraso_h, wm.isoformat(), JANELA_MAX_MIN,
        )

    fim = min(limite, wm + dt.timedelta(minutes=JANELA_MAX_MIN))
    saida = conferir_janela(wm, fim, reparar=reparar)
    saida.update({'de': wm.isoformat(), 'ate_pedido': fim.isoformat(),
                  'reancorou': reancorou, 'atraso_h': round(atraso_h, 2)})

    # O watermark só anda até onde a conferência fechou. `timeout=None` porque
    # perder esta chave é perder cobertura (ver RE_ANCORA_HORAS).
    novo = dt.datetime.fromisoformat(saida['ate'])
    if novo > wm:
        cache.set(WM, novo, None)
    cache.set(ULTIMO, {**saida, 'em': agora.isoformat()}, 24 * 3600)
    if saida['movs_pg'] or saida['procs_pg']:
        logger.info('gate datajud: %s', saida)
    return saida
