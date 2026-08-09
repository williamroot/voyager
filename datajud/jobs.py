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

    def _coletar(siglas, teto, pids):
        """Espalha `teto` vagas entre `siglas` (cota igual, newest-first)."""
        siglas = [s for s in siglas if s in elig]
        if not siglas or len(pids) >= teto:
            return
        cota = max(1, (teto - len(pids)) // len(siglas))
        for sigla in siglas:
            if len(pids) >= teto:
                break
            pids.extend(
                base.filter(tribunal_id=sigla)
                .order_by('-inserido_em')
                .values_list('pk', flat=True)[:min(cota, teto - len(pids))]
            )

    # 1) ALVO DE VALOR primeiro (~90% do batch); 2) resto no piso restante.
    pids: list[int] = []
    alvo = int(a_enfileirar * DATAJUD_PRIORIDADE_FRACAO)
    _coletar(DATAJUD_PRIORIDADE, alvo, pids)              # prioridade
    outros = sorted(elig - set(DATAJUD_PRIORIDADE))
    _coletar(outros, a_enfileirar, pids)                 # piso pro resto
    # se a prioridade não encheu sua fatia (backlog acabou), completa com o resto
    _coletar(DATAJUD_PRIORIDADE, a_enfileirar, pids)
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
