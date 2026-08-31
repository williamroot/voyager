"""Jobs RQ pra ingestão Datajud."""
import logging

import django_rq
from django_rq import job
from rq import Retry

from tribunals.models import Process

from .client import DatajudClient
from .ingestion import sync_processo

logger = logging.getLogger('voyager.datajud.jobs')

# Auto-retry imediato de falhas transientes (Redis/PG drop) — idempotente
# (bulk_create ignore_conflicts). Sem interval: não exige --with-scheduler.
DATAJUD_RETRY = Retry(max=3)

# Análogo ao reabastecer_filas_enriquecimento (PJe). Drena backlog histórico
# de Process com data_enriquecimento_datajud IS NULL.
DATAJUD_REFILL_BATCH = 10_000
DATAJUD_REFILL_HIGH_WATER = 100_000
# PRIORIDADE DE VALOR (Fase 1 do plano de cobertura): tribunais datajud que o
# Juriscope lê (Juriscope ∩ sem-enricher) + TJPR (add do usuário). O refill gasta
# ~90% do batch neles (o teto de 100 rpm da APIKey pública é permanente → foco no
# que vira lead); os 10% restantes espalham pros demais pra não secarem.
# Ver .ia/ENRICHMENT.md "Plano de cobertura por valor".
DATAJUD_PRIORIDADE = ('TJPR', 'TRF4', 'TRF6', 'TRF2')
DATAJUD_PRIORIDADE_FRACAO = 0.9
# Teto de ALERTA do watcher: o refill E o auto-enqueue capam em HIGH_WATER (100k);
# se a fila passar disso com folga, algo enfileirou SEM bound (foi o vetor do
# incidente 02/07: 63M jobs). datajud_queue_watch loga 🔴 e registra em cache.
DATAJUD_QUEUE_CEILING = 300_000


def _fila_datajud_cheia(queue=None) -> tuple[bool, int]:
    """(cheia?, profundidade) — bound compartilhado por refill e auto-enqueue.
    Fonte única do high-water pra fila nunca virar o monstro do 02/07."""
    q = queue or django_rq.get_queue('datajud')
    depth = len(q)
    return depth >= DATAJUD_REFILL_HIGH_WATER, depth


@job('manual', timeout=300)
def datajud_sincronizar_processo(process_id: int, prefer_cortex: bool = True) -> dict:
    """Sincroniza um processo via Datajud — usado pelo botão UI.

    `prefer_cortex=True` por default: cliente vai pra Cortex primeiro,
    porque é click manual com user esperando. Backfill em massa que
    chamar isso direto pode passar False.
    """
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    logger.info('datajud_sincronizar_processo %s %s', p.tribunal.sigla, p.numero_cnj)
    client = DatajudClient(prefer_cortex=prefer_cortex)
    return sync_processo(p, client=client)


@job('datajud', timeout=600)
def datajud_sync_bulk(process_id: int) -> dict:
    """Versão bulk auto-enfileirada quando processos novos aparecem na
    ingestão DJEN. Roda na fila `datajud` (workers dedicados) pra não
    disputar com `enrich_trf*` (PJe scraping) nem `djen_backfill`
    (data-based)."""
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    logger.info('datajud_sync_bulk %s %s', p.tribunal.sigla, p.numero_cnj)
    client = DatajudClient(prefer_cortex=False)
    return sync_processo(p, client=client)


@job('default', timeout=300)
def reabastecer_fila_datajud() -> dict:
    """Cron: enfileira até DATAJUD_REFILL_BATCH Process pendentes de Datajud,
    desde que a fila não esteja cheia (DATAJUD_REFILL_HIGH_WATER).

    Análogo ao reabastecer_filas_enriquecimento (PJe). Sem este job, o
    backlog histórico de Process com data_enriquecimento_datajud=NULL
    nunca é drenado — só processos tocados na ingestão diária pegam
    Datajud, deixando processos antigos sem features Datajud na
    classificação.
    """
    from django.conf import settings
    if not getattr(settings, 'DATAJUD_ENQUEUE_ENABLED', True):
        logger.info('reabastecer_fila_datajud: desativado (DATAJUD_ENQUEUE_ENABLED=False)')
        return {'skipped': 'disabled'}
    queue = django_rq.get_queue('datajud')
    cheia, depth = _fila_datajud_cheia(queue)
    if cheia:
        msg = f'skip (fila com {depth:,} jobs ≥ {DATAJUD_REFILL_HIGH_WATER:,})'
        logger.info('reabastecer_fila_datajud: %s', msg)
        return {'skipped': msg}

    capacidade = DATAJUD_REFILL_HIGH_WATER - depth
    a_enfileirar = min(capacidade, DATAJUD_REFILL_BATCH)
    # PRIORIZAÇÃO por SPREAD entre tribunais (não FIFO/heap).
    # O drama do 02/07 foi enfileirar em heap order → clusterou por tribunal e
    # deixou os TJs gigantes no fim da fila (33d de defasagem enquanto TRTs = 0d).
    # Aqui damos a CADA tribunal elegível uma COTA do batch, sempre os mais NOVOS
    # (`-inserido_em`) — assim TODO tribunal drena em paralelo (o max(data_enriq)
    # de cada um salta pra recente → lag cai junto) e um tribunal grande (TJSP)
    # não engole o lote inteiro. Escopo: só tribunais SEM enricher (onde há
    # enricher, classe/assunto vem dele → Datajud redundante), pra não reafogar
    # a API pública compartilhada do CNJ.
    from djen.ingestion import TRIBUNAIS_COM_ENRICHER
    from tribunals.models import Tribunal
    # Tribunal tem `sigla` como PK → Process.tribunal_id == sigla.
    elig = set(
        Tribunal.objects.filter(ativo=True)
        .exclude(sigla__in=TRIBUNAIS_COM_ENRICHER)
        .values_list('sigla', flat=True)
    )
    base = Process.objects.filter(data_enriquecimento_datajud__isnull=True)

    pids: list[int] = []
    _vistos: set[int] = set()

    def _coletar(siglas, teto, qs):
        """Espalha `teto` vagas entre `siglas` (cota igual, sem duplicar). SEM
        order_by: o `WHERE tribunal_id=X AND datajud IS NULL ORDER BY inserido_em`
        não tem índice que sirva e estoura o timeout de 300s (TJPR 5,8M). Pra
        backfill, QUALQUER pendente serve — LIMIT sem sort é indexado/instantâneo."""
        siglas = [s for s in siglas if s in elig]
        if not siglas or len(pids) >= teto:
            return
        cota = max(1, (teto - len(pids)) // len(siglas))
        for sigla in siglas:
            if len(pids) >= teto:
                break
            adicionados = 0
            # busca cota + folga p/ compensar dedup dos já-vistos
            for pid in qs.filter(tribunal_id=sigla).values_list(
                    'pk', flat=True)[:cota + len(_vistos)]:
                if adicionados >= cota or len(pids) >= teto:
                    break
                if pid in _vistos:
                    continue
                _vistos.add(pid)
                pids.append(pid)
                adicionados += 1

    # Fase 0: dentro da prioridade, os COM sinal de precatório vão PRIMEIRO
    # (só ~5,5% do TJPR tem sinal → a quota de 100 rpm cai no que vira lead).
    # Inócuo enquanto o backfill não rodou (tem_sinal_precatorio IS NULL → 0 aqui,
    # cai no passe seguinte = comportamento atual por-tribunal).
    base_sinal = base.filter(tem_sinal_precatorio=True)
    alvo = int(a_enfileirar * DATAJUD_PRIORIDADE_FRACAO)
    _coletar(DATAJUD_PRIORIDADE, alvo, base_sinal)       # 1) prioridade COM sinal
    _coletar(DATAJUD_PRIORIDADE, alvo, base)             # 2) prioridade (resto)
    outros = sorted(elig - set(DATAJUD_PRIORIDADE))
    _coletar(outros, a_enfileirar, base)                 # 3) piso pro resto
    _coletar(DATAJUD_PRIORIDADE, a_enfileirar, base)     # 4) completa prioridade
    # Enqueue explícito na queue 'datajud' (não usa .delay()) — mesmo padrão
    # do reabastecer_filas_enriquecimento, defensivo contra alguém mudar o
    # decorator do datajud_sync_bulk no futuro.
    enfileirados = 0
    for pid in pids:
        try:
            queue.enqueue(datajud_sync_bulk, pid, job_timeout=600, retry=DATAJUD_RETRY)
            enfileirados += 1
        except Exception as exc:
            logger.warning('falha ao enfileirar datajud bulk pid=%d: %s', pid, exc)
    logger.info('reabastecer_fila_datajud: %d enfileirados', enfileirados)
    return {'enfileirados': enfileirados}


@job('default', timeout=1800)
def conferir_indice_datajud(reparar: bool = True) -> dict:
    """Gate de completude do ÍNDICE da porta do Datajud (uma passada).

    O buraco que este job existe para fechar, medido em produção em
    24/08/2026: **100%** das movimentações escritas por esta porta nos últimos
    5 minutos estavam fora do Elasticsearch, 42,27% das escritas entre 5 e 15
    minutos, e **0 de 500** processos tocados nas últimas 2 horas tinham o
    documento em dia com a escrita. A porta gravava e não entregava; o único
    caminho até o índice era um poller de 10 minutos — e, do lado do
    `Process`, nem isso (`.update()` não mexe em `atualizado_em`, que é a
    chave do keyset daquele poller).

    Mede os DOIS lados (regra nº 5), abstém em vez de chutar 0 (regra nº 6) e
    trata teto como ERRO registrado (regra nº 2). Ver `datajud/indice.py`.
    """
    from .indice import tick
    return tick(reparar=reparar)


def agendar_conferencia_indice_datajud() -> dict:
    """Enfileira UMA conferência, com `job_id` fixo. Chamado inline pelo cron.

    `job_id` determinístico é o que impede empilhamento: se a passada anterior
    ainda não rodou, o RQ substitui o job em vez de acrescentar mais um. Mesmo
    truque do gate do diário, pelo mesmo motivo.

    Fila `default`, nunca `datajud`: a fila da porta tem até 100 mil jobs de
    sincronização à frente (o `DATAJUD_REFILL_HIGH_WATER`), e um gate atrás
    disso demoraria horas para rodar. Gate que roda tarde é gate que não roda.
    """
    from .indice import gate_ativo

    if not gate_ativo():
        return {'skip': 'gate desligado'}
    fila = django_rq.get_queue('default')
    fila.enqueue(conferir_indice_datajud, job_id='datajud:conferir_indice',
                 job_timeout=1800)
    return {'enfileirado': True}


@job('default', timeout=60)
def datajud_api_healthcheck() -> dict:
    """Sonda a API pública do Datajud (_count direto, sem proxy/rate-limit) e
    registra o estado em cache (`datajud:api_health`). Loga WARNING na transição
    down→up pra sinalizar que dá pra religar (DATAJUD_ENQUEUE_ENABLED=true).

    1 req a cada 15min é desprezível pra quota — é só um probe.
    """
    import time

    import requests
    from django.core.cache import cache
    from django.utils import timezone

    from .client import DEFAULT_API_KEY, index_for

    url = f'https://api-publica.datajud.cnj.jus.br/{index_for("TJSP")}/_count'
    hdr = {'Authorization': DEFAULT_API_KEY, 'Content-Type': 'application/json'}
    prev = cache.get('datajud:api_health') or {}
    t0 = time.monotonic()
    try:
        r = requests.post(url, json={'query': {'match_all': {}}}, headers=hdr, timeout=15)
        ok = r.status_code == 200
        status = r.status_code
    except Exception as exc:  # noqa: BLE001
        ok = False
        status = type(exc).__name__
    latency = round(time.monotonic() - t0, 2)
    state = {'ok': ok, 'status': status, 'latency': latency,
             'checked_at': timezone.now().isoformat()}
    cache.set('datajud:api_health', state, 3600)
    if ok and not prev.get('ok'):
        logger.warning(
            '🟢 Datajud API RECUPEROU (_count HTTP %s em %ss). '
            'Dá pra religar: DATAJUD_ENQUEUE_ENABLED=true + force-recreate.',
            status, latency,
        )
    elif not ok:
        logger.info('Datajud API ainda fora (%s, %ss)', status, latency)
    return state


@job('default', timeout=60)
def datajud_queue_watch() -> dict:
    """Watcher da profundidade da fila `datajud` — impede o 'monstro' do 02/07.

    Registra a profundidade em cache (`datajud:queue_depth`, lida pelo Command
    Center) e loga 🔴 se passar do teto de alerta. O bound de verdade vive no
    refill e no auto-enqueue (`_fila_datajud_cheia`); este job é o OLHO — se a
    fila estourar mesmo com os bounds, é sinal de que algo enfileirou sem passar
    pelo guard (regressão), e queremos saber no dia 1, não em 33 dias.
    """
    from django.core.cache import cache
    from django.utils import timezone

    q = django_rq.get_queue('datajud')
    depth = len(q)
    estado = {
        'depth': depth,
        'high_water': DATAJUD_REFILL_HIGH_WATER,
        'ceiling': DATAJUD_QUEUE_CEILING,
        'alerta': depth >= DATAJUD_QUEUE_CEILING,
        'checked_at': timezone.now().isoformat(),
    }
    cache.set('datajud:queue_depth', estado, 3600)
    if depth >= DATAJUD_QUEUE_CEILING:
        logger.warning(
            '🔴 fila datajud=%s ≥ teto de alerta %s — algo enfileirou SEM bound '
            '(refill/auto-enqueue capam em %s). Investigar antes de virar backlog.',
            f'{depth:,}', f'{DATAJUD_QUEUE_CEILING:,}', f'{DATAJUD_REFILL_HIGH_WATER:,}',
        )
    return estado


# --------------------------------------------------------------------------- #
# Varredura do acervo declarado ao CNJ (F1-F5 do plano de completude)
# --------------------------------------------------------------------------- #
# Contexto: a ingestão sempre foi DJEN-only, e o DJEN é veículo de COMUNICAÇÃO —
# processo sem publicação nunca entrou. Medido em 14/08/2026: temos ~13% do
# acervo declarado ao CNJ (343M docs). Ver datajud/varredura.py.

#: kill switch por tribunal (mesma ideia do `enrich_pausados`): põe a sigla aqui
#: e a varredura vira no-op sem precisar de deploy. Existe porque a APIKey do
#: Datajud é COMPARTILHADA — se o CNJ começar a estrangular, tem que dar pra
#: parar em segundos, não em minutos.
VARREDURA_PAUSADOS_KEY = 'varredura:pausados'

#: PARADA GLOBAL: uma chave que desliga a puxada inteira, sem enumerar sigla.
#: O switch por tribunal serve para "o TJSP está pesado"; este serve para "o CNJ
#: está estrangulando a chave compartilhada, PARE TUDO agora". Enumerar 59
#: siglas na hora do aperto é justamente o que não se consegue fazer.
VARREDURA_PARADA_KEY = 'varredura:parada'


def varredura_pausados() -> set[str]:
    from django.core.cache import cache
    return set(cache.get(VARREDURA_PAUSADOS_KEY) or [])


def set_varredura_pausados(siglas: set[str]) -> None:
    from django.core.cache import cache
    cache.set(VARREDURA_PAUSADOS_KEY, sorted(siglas), timeout=None)


def varredura_parada() -> bool:
    from django.core.cache import cache
    return bool(cache.get(VARREDURA_PARADA_KEY))


def set_varredura_parada(parada: bool = True) -> None:
    """Liga/desliga a parada global. Sem deploy, vale em segundos.

    A varredura em curso confere isto A CADA PÁGINA (~10 s) e sai salvando o
    cursor — parar no meio e retomar de onde parou são a MESMA feature: um
    `stop` que perde o progresso não é kill switch, é sabotagem.
    """
    from django.core.cache import cache
    if parada:
        cache.set(VARREDURA_PARADA_KEY, True, timeout=None)
    else:
        cache.delete(VARREDURA_PARADA_KEY)


@job('varredura', timeout=86400)
def varrer_acervo(sigla: str, max_paginas: int | None = None,
                  do_zero: bool = False) -> dict:
    """Varre um tribunal inteiro. Retomável: o watermark fica no `Tribunal`."""
    if varredura_parada() or sigla.upper() in varredura_pausados():
        logger.info('varredura %s pausada — no-op', sigla)
        return {'tribunal': sigla.upper(), 'estado': 'pausado'}
    from .varredura import varrer_tribunal
    return varrer_tribunal(sigla, retomar=not do_zero, max_paginas=max_paginas)


@job('varredura', timeout=3600)
def tick_varredura_incremental() -> dict:
    """Passada incremental: só o que nasceu ou mudou desde o último cursor.

    É isto que mantém a completude VIVA — sem ele, a puxada de 343M vira uma
    foto de um dia. Barato porque o `@timestamp` do Datajud é a data da última
    atualização: `gte watermark` devolve poucos milhares por tribunal/dia.

    Só entra tribunal que já teve a varredura COMPLETA (tem cursor). Rodar o
    incremental antes da varredura completa faria o cursor pular pro presente e
    o histórico nunca ser varrido — perda silenciosa, o pior tipo. O cursor é o
    critério inteiro: `exclude(cursor=None)` já é "quem foi varrido".

    ⚠️ Este laço filtrava também por `ativo=True`, e isso era CONFUSÃO DE
    CAMPO. `ativo` governa a ingestão DJEN (o cron diário e o tick de backfill
    em `djen.scheduler`) — é decisão de volume de diário, não de varredura do
    Datajud, que são portas diferentes e com custo diferente. O efeito prático
    era que um tribunal varrido mas com o DJEN desligado teria o esqueleto
    congelado no dia da varredura, sem ninguém acusando: exatamente o padrão
    "run verde, log limpo" que esta casa persegue. Achado em 31/08/2026 junto
    com o STM (#107), que é o primeiro tribunal nessa situação — varrido por
    sigla explícita e `ativo=False`.
    """
    from tribunals.models import Tribunal
    if varredura_parada():
        logger.info('tick_varredura_incremental: parada global — no-op')
        return {'enfileirados': [], 'total': 0, 'estado': 'parado'}
    pausados = varredura_pausados()
    fila = django_rq.get_queue('varredura')
    enfileirados = []
    for sigla in (Tribunal.objects
                  .exclude(datajud_varredura_cursor=None)
                  .order_by('sigla').values_list('sigla', flat=True)):
        if sigla in pausados:
            continue
        # job_id determinístico: se a passada anterior ainda roda, não duplica
        fila.enqueue(varrer_acervo, sigla, job_id=f'varr:{sigla}',
                     retry=DATAJUD_RETRY)
        enfileirados.append(sigla)
    return {'enfileirados': enfileirados, 'total': len(enfileirados)}


@job('manual', timeout=600)
def hidratar_processo(cnj: str) -> dict:
    """Esqueleto → processo de verdade (fila MANUAL: tem gente esperando)."""
    from .hidratacao import hidratar_cnj
    return hidratar_cnj(cnj)


# Fila `default`, NÃO `varredura`: um vigia enfileirado atrás de quem ele vigia
# nunca roda. Os 8 slots da varredura ficam ocupados por jobs de HORAS, então o
# watchdog empilhou 20 execuções sem executar nenhuma (visto em 15/08/2026) —
# ele existia no papel e não no relógio.
@job('default', timeout=300)
def tick_varredura_watchdog(heartbeat_max: int = 120) -> dict:
    """Devolve pra fila a varredura que ficou órfã quando um worker morreu.

    O RQ só considera um job abandonado quando ele estoura o PRÓPRIO timeout —
    e o nosso é de 24h, porque varrer o TJSP é trabalho de horas. Resultado sem
    este tick: um `docker compose up -d` no meio da varredura deixa o maior
    tribunal do país parado por um dia inteiro, e ninguém percebe, porque na
    fila ele aparece como "rodando".

    Foi exatamente o que aconteceu em 14/08/2026 ao subir a frota de 4 pra 8
    réplicas: 8 tribunais (TJSP à frente) viraram fantasma no StartedJobRegistry.

    Órfão = job no StartedJobRegistry cujo worker não bate o coração há
    `heartbeat_max` segundos.
    """
    from django.utils import timezone as djtz
    from rq import Worker
    from rq.registry import StartedJobRegistry

    fila = django_rq.get_queue('varredura')
    registro = StartedJobRegistry(queue=fila)
    agora = djtz.now()

    vivos = set()
    for w in Worker.all(connection=fila.connection):
        bat = w.last_heartbeat
        if not bat:
            continue
        idade = (agora - bat.replace(tzinfo=agora.tzinfo)).total_seconds()
        if idade <= heartbeat_max:
            jid = w.get_current_job_id()
            if jid:
                vivos.add(jid)

    orfaos = [j for j in registro.get_job_ids() if j not in vivos]
    pausados = varredura_pausados()
    parada = varredura_parada()
    devolvidos = []
    for jid in orfaos:
        registro.remove(jid)
        sigla = jid.split(':')[-1]
        if parada or sigla in pausados:
            # devolver pra fila quem o operador acabou de parar transformaria o
            # kill switch num loop: para, o watchdog reenfileira, para de novo
            logger.info('varredura %s órfã mas PAUSADA — não reenfileiro', sigla)
            continue
        devolvidos.append(jid)
        # re-enfileira do watermark: com o checkpoint a cada 20 páginas, o
        # retrabalho é de minutos, não da varredura inteira
        fila.enqueue(varrer_acervo, sigla, job_id=jid, retry=DATAJUD_RETRY)
    if devolvidos:
        logger.warning('varredura: %d órfãos devolvidos pra fila: %s',
                       len(devolvidos), [j.split(':')[-1] for j in devolvidos])
    return {'orfaos': [j.split(':')[-1] for j in devolvidos],
            'total': len(devolvidos),
            'nao_devolvidos_por_pausa': len(orfaos) - len(devolvidos)}
