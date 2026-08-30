import logging

import django_rq
from django_rq import job
from rq import Retry

from tribunals.models import Process

from .esaj import TjacEnricher, TjalEnricher, TjspEnricher
from .tjap import TjapEnricher
from .tjce import TjceEnricher
from .tjdft import TjdftEnricher
from .tjma import TjmaEnricher
from .tjmg import TjmgEnricher
from .tjmt import TjmtEnricher
from .tjpa import TjpaEnricher
from .tjpe import TjpeEnricher
from .tjrj import TjrjEnricher
from .tjro import TjroEnricher
from .trf1 import Trf1Enricher
from .trf3 import Trf3Enricher
from .trf5 import Trf5Enricher

logger = logging.getLogger('voyager.enrichers.jobs')


_ENRICHERS = {
    'TRF1': Trf1Enricher,
    'TRF3': Trf3Enricher,
    'TRF5': Trf5Enricher,
    'TJMG': TjmgEnricher,
    'TJMA': TjmaEnricher,
    'TJSP': TjspEnricher,
    'TJAL': TjalEnricher,
    'TJDFT': TjdftEnricher,
    # Viáveis (recon 2026-06-29): PJe clássico sem captcha + e-SAJ.
    'TJCE': TjceEnricher,
    'TJAP': TjapEnricher,
    'TJPE': TjpeEnricher,
    'TJRJ': TjrjEnricher,
    'TJRO': TjroEnricher,
    'TJAC': TjacEnricher,
    'TJPA': TjpaEnricher,
    'TJMT': TjmtEnricher,
}

ENRICH_TIMEOUT = 300

# Auto-retry de falhas TRANSIENTES (Redis/PG connection drop, timeout). Sem
# interval = retry imediato (não exige rqworker --with-scheduler). Jobs são
# idempotentes (drainer dedupe por scraped_at; upsert ignore_conflicts), então
# re-tentar é seguro. nao_encontrado/erro de scrape NÃO são exceção → não
# disparam retry; só os erros de infra (que viravam failed à toa) re-tentam.
ENRICH_RETRY = Retry(max=3)


def queue_for(tribunal_sigla: str) -> str:
    """Mapeia sigla → nome da fila por tribunal (enrich_trf1, enrich_trf3...)."""
    return f'enrich_{tribunal_sigla.lower()}'


# --- Kill-switch de enrichment por tribunal ---------------------------------
# Quando um tribunal bloqueia o método de acesso (ex.: TJRO/TJAP devolvendo 403
# pra todos os proxies em 2026-07), continuar tentando só queima proxy/CPU e
# enche o Process.enriquecimento_erro. A pausa vive num set no cache Redis
# (sem TTL — sobrevive a restart) e é honrada em TRÊS pontos: refill, enqueue
# da ingestão e o próprio job (drena fila residual sem tocar a fonte).
_PAUSA_KEY = 'enrich:pausados'


# Tribunais que bloqueiam datacenter POR COMPLETO (403 em todo IP do pool —
# ex.: TJRO/TJAP em 2026-07). Pra esses, tentar datacenter é desperdício: vão
# DIRETO e SÓ pro Cortex (residencial). Granular por tribunal, em cache Redis.
_CORTEX_ONLY_KEY = 'enrich:cortex_only'


def enrich_cortex_only() -> set[str]:
    from django.core.cache import cache
    return set(cache.get(_CORTEX_ONLY_KEY) or [])


def is_cortex_only(sigla: str) -> bool:
    return (sigla or '').upper() in enrich_cortex_only()


def set_cortex_only(siglas: set[str]) -> None:
    from django.core.cache import cache
    cache.set(_CORTEX_ONLY_KEY, sorted(s.upper() for s in siglas), timeout=None)


# --- Faixa que a fonte não tem: recusa CONTADA, nunca corte mudo ------------
# Um enricher pode saber, sem gastar requisição, que a fonte dele não tem
# aquele processo — o caso medido é o TJSP, cuja faixa `eproc` (sequencial
# começando em 4, ano >= 2025, ≈ 2,94 M processos) devolveu "Não existem
# informações" em 16 de 16 sondas ao vivo. Recusar economiza ~10 mil
# requisições/h e até 8 IPs do pool COMPARTILHADO por job.
#
# Regra nº 2 do CLAUDE.md: teto é alerta, nunca `return` discreto. Cada recusa
# incrementa um hash no Redis (`sigla|motivo` -> contagem) e o refill imprime
# o censo em ERROR a cada passada em que ele cresceu. Sem isso, 2,94 M de
# processos sairiam de `pendente` sem ninguém nunca ver por quê.
_FORA_DO_ESAJ_KEY = 'enrich:fora_da_fonte'


def registrar_fora_do_esaj(sigla: str, motivo: str, quantos: int = 1) -> None:
    """Conta recusas por faixa. Best-effort: contador não pode derrubar job."""
    try:
        conn = django_rq.get_connection('default')
        conn.hincrby(_FORA_DO_ESAJ_KEY, f'{(sigla or "?").upper()}|{motivo}', quantos)
    except Exception:
        logger.warning('falha ao contar recusa fora-da-fonte',
                       extra={'sigla': sigla, 'motivo': motivo})


def _hook_fora_da_fonte(sigla: str):
    """Predicado `cnj -> motivo|None` do enricher deste tribunal, ou None.

    O hook nasceu no e-SAJ (`fora_do_esaj`) e virou genérico quando a medição de
    29/08/2026 achou a mesma fatia sem porta em tribunais de PJe. `fora_da_fonte`
    é o nome canônico; `fora_do_esaj` continua resolvendo por compatibilidade
    com enrichers/runbooks antigos.
    """
    cls = _ENRICHERS.get(sigla)
    if not cls:
        return None
    return getattr(cls, 'fora_da_fonte', None) or getattr(cls, 'fora_do_esaj', None)


def censo_fora_do_esaj() -> dict[str, int]:
    """`{'TJSP|eproc': 123456}` — quantos processos foram recusados por faixa."""
    try:
        conn = django_rq.get_connection('default')
        cru = conn.hgetall(_FORA_DO_ESAJ_KEY) or {}
    except Exception:
        return {}
    out: dict[str, int] = {}
    for k, v in cru.items():
        chave = k.decode() if isinstance(k, bytes) else k
        try:
            out[chave] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def enrich_pausados() -> set[str]:
    from django.core.cache import cache
    return set(cache.get(_PAUSA_KEY) or [])


def enrich_pausado(sigla: str) -> bool:
    return (sigla or '').upper() in enrich_pausados()


def set_enrich_pausados(siglas: set[str]) -> None:
    from django.core.cache import cache
    cache.set(_PAUSA_KEY, sorted(s.upper() for s in siglas), timeout=None)


# @job('default') é mantido pra trabalhar como fallback se algo enfileirar
# direto via .delay() sem passar pela queue per-tribunal.
@job('default', timeout=ENRICH_TIMEOUT)
def enriquecer_processo(process_id: int, prefer_cortex: bool | None = None,
                         direct_apply: bool = False, seguir_incidentes: bool = False) -> dict:
    # prefer_cortex=None → resolve do setting (default True: Cortex-first pra
    # passar o WAF; datacenter fica de fallback). Cobre TODOS os paths de enqueue.
    if prefer_cortex is None:
        from django.conf import settings
        prefer_cortex = getattr(settings, 'ENRICH_PREFER_CORTEX', True)
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    # Pausado: no-op SEM tocar no status (continua pendente) — a fila residual
    # drena rápido sem queimar proxy; ao despausar, o refill repõe e processa.
    if enrich_pausado(p.tribunal_id):
        return {'skip': 'pausado', 'tribunal': p.tribunal_id}
    cls = _ENRICHERS.get(p.tribunal_id)
    if not cls:
        raise ValueError(f'Sem enricher cadastrado para tribunal {p.tribunal_id}')
    # A faixa fora-da-fonte já é conferida no ENFILEIRAMENTO (`_separar_fora_da_fonte`
    # e o refill do legado), mas o job que ENTROU na fila antes de a faixa ser
    # medida passa direto por aquele filtro. Medido em 29/08/2026, logo após o
    # deploy das faixas: 99,8% dos 11.392 jobs na fila do TJMG e 6,8% dos 11.171
    # do TJRJ já eram da faixa morta.
    #
    # O custo não é só a requisição gasta num sistema que não tem o processo: o
    # job gravaria `nao_encontrado`, que é uma afirmação SOBRE A FONTE. Dizer
    # "a fonte não tem" quando perguntamos ao sistema errado é dado coletado
    # pela metade — vale menos que zero, porque produz confiança falsa (regra
    # nº 6: abster > chutar). Aqui a recusa é CONTADA no mesmo censo do
    # enfileiramento, nunca corte mudo (regra nº 2).
    fora = _hook_fora_da_fonte(p.tribunal_id)
    motivo = fora(p.numero_cnj) if fora else None
    if motivo:
        registrar_fora_do_esaj(p.tribunal_id, motivo)
        logger.info('enriquecer_processo fora-da-fonte %s %s motivo=%s',
                    p.tribunal_id, p.numero_cnj, motivo)
        return {'skip': 'fora_da_fonte', 'motivo': motivo,
                'tribunal': p.tribunal_id, 'cnj': p.numero_cnj}
    # e-SAJ (TJSP/TJAL/TJAC) funciona com o pool ProxyScrape; o Cortex tem resetado
    # conexão e faz o enrich falhar (dossiê vazio). Força pool-first pro e-SAJ,
    # independente do que o enqueue passou (garante o fix mesmo se o web ainda não
    # atualizou). Cortex vira fallback no _next_proxy. (2026-07-06)
    from enrichers.esaj import BaseEsajEnricher
    if issubclass(cls, BaseEsajEnricher):
        prefer_cortex = False
    logger.info('enriquecer_processo inicio %s %s', p.tribunal_id, p.numero_cnj)
    enricher = cls(prefer_cortex=prefer_cortex)
    # seguir_incidentes só faz sentido no e-SAJ (cada parte tem um incidente).
    if seguir_incidentes and isinstance(enricher, BaseEsajEnricher):
        result = enricher.enriquecer(p, direct_apply=direct_apply, seguir_incidentes=True)
    else:
        result = enricher.enriquecer(p, direct_apply=direct_apply)
    logger.info('enriquecer_processo fim %s %s status=%s', p.tribunal_id, p.numero_cnj, result.get('status', 'ok'))
    return result


def enqueue_enriquecimento(process_id: int, tribunal_sigla: str):
    """Enfileira na queue do tribunal — paraleliza coletas sem misturar pools.

    prefer_cortex do setting (default True): residencial passa o WAF dos tribunais;
    o pool datacenter fica só como fallback. Evita rotacionar 40 IPs queimados.
    """
    if enrich_pausado(tribunal_sigla):
        return None
    # Terceira porta de enfileiramento. O refill e o tick do legado já filtram a
    # faixa fora-da-fonte, mas ESTA é chamada pela ingestão — e o eproc publica
    # no DJEN todo dia, então ela repõe faixa morta continuamente. Medido em
    # 29/08/2026: com o gate só na execução, a fatia morta da fila do TJRJ
    # SUBIU de 6,8% para 21,8% em uma hora. O gate da execução garante a
    # correção (nenhuma requisição, nenhum status escrito); este aqui evita
    # ocupar a fila com job que já nasce para ser recusado.
    fora = _hook_fora_da_fonte(tribunal_sigla)
    if fora:
        cnj = (Process.objects.filter(pk=process_id)
               .values_list('numero_cnj', flat=True).first())
        motivo = fora(cnj) if cnj else None
        if motivo:
            registrar_fora_do_esaj(tribunal_sigla, motivo)
            return None
    queue = django_rq.get_queue(queue_for(tribunal_sigla))
    return queue.enqueue(enriquecer_processo, process_id, job_timeout=ENRICH_TIMEOUT,
                         retry=ENRICH_RETRY)


def enqueue_enriquecimento_manual(process_id: int):
    """Enfileira na queue 'manual' — prioritária pra cliques na UI.

    Bypassa as filas per-tribunal (que podem ter centenas de milhares de
    jobs). `prefer_cortex=False`: pool-first (ProxyScrape). O e-SAJ (TJSP/TJAL/
    TJAC) funciona bem com o pool datacenter, e o Cortex tem resetado conexão
    (Connection reset) — Cortex-first fazia o enrich manual FALHAR e o dossiê
    vir vazio. Com pool-first, o Cortex vira fallback no _next_proxy. (2026-07-06)
    """
    from django.conf import settings
    queue = django_rq.get_queue('manual')
    return queue.enqueue(
        enriquecer_processo,
        args=(process_id,),
        kwargs={'prefer_cortex': False, 'direct_apply': True,
                'seguir_incidentes': getattr(settings, 'ESAJ_SEGUIR_INCIDENTES', False)},
        job_timeout=ENRICH_TIMEOUT,
        retry=ENRICH_RETRY,
    )


# Buffer pra workers nunca esperarem: com 600+ workers (300 .30 + 300 .177)
# por tribunal a ~5-10s/job, queima ~60-120/s = ~7-14k/min. 100k high-water
# garante 7-14min de folga entre refills do scheduler (que roda a cada 2min).
ENQUEUE_BATCH_SIZE = 10_000
QUEUE_HIGH_WATER = 100_000  # TETO ABSOLUTO; o teto EFETIVO é medido (ver abaixo)

# --- Por que o teto de 100k virou o gargalo (censo de 25/08/2026) -------------
# A frota tem 146 workers de enrich, não os 600 da conta acima. A 100k jobs por
# fila, o tempo de espera na fila (enqueued_at do job na frente) foi medido em
# 20 a 74 HORAS. E um Process só sai de `pendente` quando o drainer aplica o
# resultado — ou seja, ele fica elegível pro refill durante essas 20-74h. Como
# `_reabastecer_impl` re-seleciona a MESMA cabeça do índice a cada 2 min, a fila
# se enche de cópias do mesmo processo. Censo COMPLETO (não amostra) das 16
# filas em 25/08/2026 00:20 UTC — `ids` é o comprimento da lista, `distintos`
# são os process_id únicos:
#
#   fila            ids    distintos  cópias   max  desperdício
#   enrich_tjmt   99.818       1.418    70,4   136       98,6%
#   enrich_tjac   99.976       1.929    51,8   607       98,1%
#   enrich_tjap   99.968       3.426    29,2   515       96,6%
#   enrich_tjal   99.993       6.433    15,5   849       93,6%
#   enrich_tjro   99.979       8.486    11,8 1.926       91,5%
#   enrich_tjpa   99.952      10.130     9,9   832       89,9%
#   enrich_tjma   99.927      10.711     9,3   564       89,3%
#   enrich_tjce   99.954      12.008     8,3   336       88,0%
#   enrich_tjpe   99.942      12.895     7,8   499       87,1%
#   enrich_trf5   99.948      31.546     3,2   535       68,4%
#   enrich_trf3   99.939      35.265     2,8   672       64,7%
#   enrich_trf1   99.935      35.675     2,8   484       64,3%
#   enrich_tjdft 100.767      54.277     1,9    32       46,1%
#   enrich_tjmg   99.947      74.931     1,3   192       25,0%
#   enrich_tjrj  251.534     251.534     1,0     1        0,0%   ← refill PULA
#   enrich_tjsp  227.678     227.503     1,0     2        0,1%   ← refill PULA
#
# 1.400.007 jobs nas 14 filas que o refill administra para 321.130 processos
# distintos: **77,1% das raspagens repetem trabalho já enfileirado**. As duas
# filas limpas são justamente as que estão ACIMA do teto — o refill não as
# toca, e a duplicação some. O teto redondo não era o total: era a fábrica.
#
# Duas correções, ambas aqui:
#  (a) TETO MEDIDO — o alvo de profundidade é ENRICH_QUEUE_ALVO_S segundos de
#      trabalho, calculado com a vazão real da fila (o FinishedJobRegistry do RQ
#      guarda 500 s de jobs terminados; len(reg)/500 = jobs/s). Fila rasa = o
#      processo é raspado antes de o refill ter chance de re-selecioná-lo.
#  (b) TRAVA DE VOO — ZSET por tribunal com os process_id já enfileirados. Um
#      pid só volta pra fila depois de ENRICH_INFLIGHT_TTL_S. Sem isso, tribunal
#      com backlog quase esgotado (TJMT tinha 1.418 pendentes) re-enfileira o
#      resto inteiro a cada 2 min, para sempre.
# 30 min de trabalho na fila. O refill roda a cada 2 min, então isto é folga
# de 15 passadas — a fila não seca se o `worker_default` estiver ocupado com um
# tick longo. E é sempre menor que ENRICH_INFLIGHT_TTL_S, senão um pid sairia da
# trava de voo antes de ser raspado e voltaria pra fila (a duplicação de volta).
ENRICH_QUEUE_ALVO_S = 1_800
ENRICH_QUEUE_MIN = 2_000         # piso: fila lenta/nova não pode secar
ENRICH_INFLIGHT_TTL_S = 3_600    # 1 h — maior que a espera-alvo, com folga
_INFLIGHT_KEY = 'enr:inflight:%s'


def _profundidade_alvo(conn, nome_fila: str) -> int:
    """Teto EFETIVO da fila: ~ENRICH_QUEUE_ALVO_S segundos de trabalho.

    A régua é o próprio RQ: `FinishedJobRegistry` guarda os jobs terminados por
    `result_ttl` (500 s em prod), então `len(registry)/500` é a vazão medida da
    fila, sem instrumentação nova. Registry vazio (fila parada ou recém-criada)
    cai no piso — nunca em zero, senão o worker fica sem trabalho.
    """
    from rq.registry import FinishedJobRegistry
    try:
        terminados = len(FinishedJobRegistry(nome_fila, connection=conn))
    except Exception:
        terminados = 0
    por_segundo = terminados / 500.0
    return max(ENRICH_QUEUE_MIN,
               min(QUEUE_HIGH_WATER, int(por_segundo * ENRICH_QUEUE_ALVO_S)))


def _filtrar_em_voo(conn, sigla: str, ids: list[int], quantos: int) -> list[int]:
    """Devolve até `quantos` pids que NÃO estão em voo, e os marca em voo.

    ZSET por tribunal (`enr:inflight:<sigla>`), score = epoch do enqueue. A poda
    é por score no começo de cada passada, então a chave não cresce sem fim:
    ela guarda no máximo o que foi enfileirado na última ENRICH_INFLIGHT_TTL_S.
    Duas viagens ao Redis (ler scores, depois marcar só os escolhidos) — marcar
    antes de escolher marcaria como em-voo pid que não foi enfileirado.
    """
    import time as _time
    chave = _INFLIGHT_KEY % sigla.lower()
    agora = _time.time()
    conn.zremrangebyscore(chave, '-inf', agora - ENRICH_INFLIGHT_TTL_S)
    if not ids:
        return []
    try:
        scores = conn.zmscore(chave, [str(i) for i in ids])
    except Exception:                      # Redis < 6.2 / redis-py antigo
        pipe = conn.pipeline()
        for i in ids:
            pipe.zscore(chave, str(i))
        scores = pipe.execute()
    livres = [pid for pid, sc in zip(ids, scores, strict=False) if sc is None][:quantos]
    if livres:
        conn.zadd(chave, {str(p): agora for p in livres})
    return livres

# --- Recuperação de falsos-negativos e-SAJ (nao_encontrado legado) -----------
# Os enrichers e-SAJ marcavam nao_encontrado TERMINAL em falha transitória (200
# ambíguo) — corrigido em 2026-07-06 (esaj 8bfd9f7). Os ~3,25M presos são legado.
# Este tick os devolve a 'pendente' pra re-enriquecer com a lógica nova, de forma
# AUTO-LIMITANTE (só quando a pipeline tem folga) e SEM loop: reseta apenas
# nao_encontrado com enriquecido_em ANTES do fix; os re-enriquecidos pós-fix
# (genuínos) ganham timestamp recente e nunca mais são resetados.
import datetime as _dt  # noqa: E402
from django.utils import timezone as _tz  # noqa: E402

REENRICH_ESAJ_TRIBUNAIS = ('TJSP', 'TJAL', 'TJAC')
REENRICH_LEGACY_CUTOFF = _tz.make_aware(_dt.datetime(2026, 7, 6))
REENRICH_RESET_BATCH = 50_000      # teto DURO por tribunal/tick
REENRICH_MAX_TENTATIVAS = 12       # teto de raspagens por processo no requeue de erro

# --- Por que o floor de pendente foi trocado por um teto por VAZÃO ----------
# O gate original era `só reseta se pendente < REENRICH_PENDENTE_FLOOR`, com
# FLOOR = 20.000. O TJSP tem **12.101.245** processos `pendente`. `pend >=
# FLOOR` é verdadeiro sempre — ou seja, o recuperador escrito em 2026-07-06
# especificamente para devolver os ~3,25 M do TJSP **nunca rodou uma vez**,
# e não falhava: só não fazia nada. Mesma família do `for pagina in range(1,
# 11)` — um limite plausível que impede o mecanismo e nunca acende luz.
#
# O que a sonda ao vivo de 25/08/2026 provou sobre esse estoque (62
# requisições, 3 s entre elas, amostra de semente 20260825): dos processos de
# prefixo 0/1 hoje marcados `nao_encontrado`, **30 de 32 devolveram cadastro
# completo ou lista AGORA** — Procedimento Comum Cível, Execução de Título
# Extrajudicial, Embargos à Execução, Usucapião, Mandado de Segurança. E
# **83,6%** deles foram queimados ANTES do fix (mês do `enriquecido_em`:
# 2026-05 → 457, 2026-06 → 1.781, 2026-07 → 214, 2026-08 → 226 de 2.678).
# São falsos-negativos, não ausências.
#
# O floor não foi APAGADO: virou teto POR LOTE, senão trocaríamos uma
# armadilha por outra invertida (2,59 M voltando de uma vez é incidente). O
# teto efetivo por tick é o MENOR entre três:
#   1. `REENRICH_LEGACY_POR_TICK` — o passo da fase (conservador por default);
#   2. o que a fila do tribunal REALMENTE drena no intervalo do tick, medido
#      pelo `FinishedJobRegistry` (a mesma régua de `_profundidade_alvo`);
#   3. `REENRICH_RESET_BATCH`, teto duro.
# Encostar no teto é ERRO registrado, com o número acumulado por tribunal.
#
# Sem loop, por construção: quem volta é re-raspado e ganha `enriquecido_em`
# recente, saindo do alvo `enriquecido_em < REENRICH_LEGACY_CUTOFF` para
# sempre. O conjunto só encolhe.
REENRICH_TICK_S = 300              # intervalo do job no djen/scheduler.py (5 min)
REENRICH_LEGACY_POR_TICK = 2_000   # PASSO 1. Subir só depois de medir vazão + pool
REENRICH_LEGACY_MIN = 200          # piso: fila fria/nova não pode travar a recuperação
_REENRICH_TOTAL_KEY = 'enrich:reenrich_legado_total'


def _teto_reenrich(conn, sigla: str) -> int:
    """Quantos `nao_encontrado` legados cabem neste tick, para este tribunal."""
    from django.conf import settings
    passo = getattr(settings, 'REENRICH_LEGACY_POR_TICK', REENRICH_LEGACY_POR_TICK)
    from rq.registry import FinishedJobRegistry
    try:
        terminados = len(FinishedJobRegistry(queue_for(sigla), connection=conn))
    except Exception:
        terminados = 0
    # result_ttl = 500 s em prod: len(registry)/500 é a vazão medida da fila.
    vazao_no_tick = int((terminados / 500.0) * REENRICH_TICK_S)
    return max(REENRICH_LEGACY_MIN,
               min(passo, vazao_no_tick or passo, REENRICH_RESET_BATCH))


@job('default', timeout=600)
def tick_reenrich_esaj_legacy() -> dict:
    """Devolve `nao_encontrado` LEGADO (pré-fix 2026-07-06) a `pendente`.

    Faseado e bounded pela vazão real da fila do tribunal. Pula a faixa que a
    fonte comprovadamente não tem (`fora_do_esaj`) — devolvê-la a `pendente`
    só a faria dar a volta inteira para ser recusada de novo.
    """
    conn = django_rq.get_connection('default')
    relatorio: dict = {}
    pausados = enrich_pausados()
    for sig in REENRICH_ESAJ_TRIBUNAIS:
        if sig in pausados:
            relatorio[sig] = 'pausado'
            continue
        teto = _teto_reenrich(conn, sig)
        fora = _hook_fora_da_fonte(sig)
        # Lê com folga: parte do lote vai ser descartada pela faixa fora-da-fonte
        # (no TJSP ela é 38,7% do estoque `nao_encontrado`), e sem folga o tick
        # devolveria menos que o teto a cada passada.
        limite_scan = min(teto * 3, REENRICH_RESET_BATCH)
        linhas = list(Process.objects.filter(
            tribunal_id=sig,
            enriquecimento_status=Process.ENRIQ_NAO_ENCONTRADO,
            enriquecido_em__lt=REENRICH_LEGACY_CUTOFF,
        ).values_list('pk', 'numero_cnj')[:limite_scan])
        if not linhas:
            relatorio[sig] = 'sem legado restante'
            continue
        elegiveis = ([pk for pk, cnj in linhas if not fora(cnj)] if fora
                     else [pk for pk, _ in linhas])
        recusados = len(linhas) - len(elegiveis)
        ids = elegiveis[:teto]
        if not ids:
            relatorio[sig] = f'{recusados:,} no lote, todos fora da fonte'
            continue
        n = Process.objects.filter(pk__in=ids).update(
            enriquecimento_status=Process.ENRIQ_PENDENTE,
            # CAMPAINHA: `enriquecimento_status` é campo do doc do ES e
            # `.update()` não roda o `auto_now` de `atualizado_em` — sem isto o
            # `sync_processos_atualizados` (keyset por `atualizado_em`) nunca vê.
            atualizado_em=_tz.now())
        try:
            acumulado = conn.hincrby(_REENRICH_TOTAL_KEY, sig, n)
        except Exception:
            acumulado = -1
        relatorio[sig] = (f'reset {n:,} (teto {teto:,}, lote {len(linhas):,}, '
                          f'fora da fonte {recusados:,}, acumulado {acumulado:,})')
        logger.info('tick_reenrich_esaj_legacy %s: reset %d (teto %d)', sig, n, teto)
        # Teto é ALERTA, nunca corte mudo (regra nº 2 do CLAUDE.md).
        if len(elegiveis) > teto:
            logger.error(
                'reenrich_legado: %s bateu o teto do tick (%s de %s elegiveis no lote) '
                '— ainda ha legado preso; acumulado devolvido ate agora: %s',
                sig, f'{teto:,}', f'{len(elegiveis):,}', f'{acumulado:,}')
    return relatorio


@job('default', timeout=600)
def tick_requeue_erros_enricher() -> dict:
    """Devolve status='erro' → 'pendente' nos tribunais de VALOR (Juriscope ∩
    enricher), bounded pelo pendente floor. Fase 2 do plano de cobertura: o
    `reabastecer_filas_enriquecimento` só re-enfileira 'pendente' → sem isto os
    ~1,1M em 'erro' (transitórios: proxy/WAF, comentário do ENRIQ_ERRO) ficavam
    presos pra sempre. Gêmeo do tick_reenrich_esaj_legacy. Ver .ia/ENRICHMENT.md.
    """
    from djen.ingestion import TRIBUNAIS_JURISCOPE
    alvo = [s for s in _ENRICHERS if s in TRIBUNAIS_JURISCOPE]
    relatorio: dict = {}
    pausados = enrich_pausados()
    for sig in alvo:
        if sig in pausados:
            relatorio[sig] = 'pausado'
            continue
        # SEM floor de pendente: os 'erro' estão PERMANENTEMENTE presos (o
        # reabastecer só pega 'pendente'); resetá-los é o ganho mesmo com pendente
        # já alto — a FILA é bounded pela profundidade-alvo, não o status no DB. O
        # batch por tick é o único limitador de ritmo (churn bounded).
        #
        # TETO DE TENTATIVAS (25/08/2026): sem ele o tick é uma esteira infinita.
        # Medido em prod: o processo 24179591 (TJAL) estava em
        # `enriquecimento_tentativas=360` — 360 raspagens, todas terminando em
        # erro, porque a cada 10 min este tick devolvia o registro a `pendente` e
        # o refill o enfileirava de novo. Cada tentativa dessas gasta até 8 IPs
        # do pool (que é compartilhado com TODOS os tribunais). Acima do teto o
        # processo fica ESTACIONADO em `erro` — e o teto é ALERTA, não corte
        # mudo: sai no log em nível ERROR com a contagem por tribunal.
        linhas = list(Process.objects.filter(
            tribunal_id=sig, enriquecimento_status=Process.ENRIQ_ERRO,
        ).values_list('pk', 'enriquecimento_tentativas')[:REENRICH_RESET_BATCH])
        ids = [pk for pk, t in linhas if (t or 0) < REENRICH_MAX_TENTATIVAS]
        estacionados = len(linhas) - len(ids)
        if estacionados:
            logger.error(
                'requeue_erros: %d de %d processos %s ESTACIONADOS em erro '
                '(≥ %d tentativas) — a fonte não responde pra eles, requeue não resolve',
                estacionados, len(linhas), sig, REENRICH_MAX_TENTATIVAS)
        if not ids:
            relatorio[sig] = ('sem erro' if not linhas
                              else f'{estacionados:,} estacionados (≥{REENRICH_MAX_TENTATIVAS} tent.)')
            continue
        n = Process.objects.filter(pk__in=ids).update(
            enriquecimento_status=Process.ENRIQ_PENDENTE,
            atualizado_em=_tz.now())        # CAMPAINHA — ver acima
        relatorio[sig] = f'reset {n:,}' + (f' / {estacionados:,} estacionados' if estacionados else '')
        logger.info('tick_requeue_erros_enricher %s: reset %d erro->pendente', sig, n)
    return relatorio


@job('default', timeout=600)
def reabastecer_filas_enriquecimento() -> dict:
    """Cron: enfileira Process pendentes por tribunal, sem duplicar in-flight.

    Idempotente e seguro contra os dois modos de falha que derrubaram o DB em
    2026-07-01 (enrich_tjmt chegou a 387k jobs p/ high-water 100k):
    - **concorrência**: lock Redis. O scan de pendentes é lento em tribunal com
      milhões pendentes; sem lock, runs do scheduler (2min) sobrepunham e cada um
      passava o teste `len(queue) < high_water` → estouro.
    - **re-enfileiramento**: o `enriquecimento_status` só vira OK quando o drainer
      (async) aplica; filtrar `status=pendente` sem mais nada re-selecionava os
      MESMOS processos a cada ciclo. O remédio prometido aqui era um cursor por
      pk (`enr:cursor:<sigla>`) que NUNCA existiu no código — e o censo de
      25/08/2026 mediu o preço: 77,1% dos jobos das 14 filas administradas por
      este refill eram cópia. Quem resolve hoje são as duas travas descritas
      junto de `ENRICH_QUEUE_ALVO_S`: profundidade-alvo medida pela vazão real
      da fila e ZSET de processos em voo.
    """
    from django.core.cache import cache
    lock = 'lock:reabastecer_enriquecimento'
    if not cache.add(lock, '1', timeout=600):
        logger.info('reabastecer: skip (lock held)')
        return {'skip': 'lock held'}
    try:
        return _reabastecer_impl()
    finally:
        cache.delete(lock)


def _reabastecer_impl() -> dict:
    from tribunals.models import Process

    relatorio = {}
    pausados = enrich_pausados()
    for sigla in _ENRICHERS.keys():
        if sigla in pausados:
            relatorio[sigla] = 'pausado'
            continue
        queue = django_rq.get_queue(queue_for(sigla))
        conn = queue.connection
        qlen = len(queue)
        alvo = _profundidade_alvo(conn, queue.name)
        if qlen >= alvo:
            relatorio[sigla] = f'skip (fila {qlen:,} ≥ alvo {alvo:,})'
            continue
        capacidade = min(alvo - qlen, ENQUEUE_BATCH_SIZE)
        # Query simples e sargável (usa índice de tribunal_id / status); NÃO
        # ordena por pk (ORDER BY pk varre o espaço GLOBAL de pk filtrando
        # tribunal_id → scan de minutos, incidente 2026-07-01).
        #
        # A re-seleção de PENDENTE já-em-fila NÃO é mais tolerada: era ela que
        # produzia 77,1% de cópias (censo acima). Quem barra é `_filtrar_em_voo`.
        # O out-of-order guard do drainer só descartava o efeito NO BANCO — a
        # requisição ao tribunal e o IP do pool já tinham sido gastos.
        # Lê MAIS candidatos que a capacidade: a cabeça do índice pode estar
        # inteira em voo (é o caso normal — foi ela que a passada anterior
        # enfileirou). Sem essa folga a fila secava em vez de avançar. O teto
        # de 60k mantém o scan barato (values_list de int no índice composto
        # proc_trib_enriq_idx; o incidente de 2026-07-01 foi por ORDER BY pk,
        # que continua fora).
        em_voo = conn.zcard(_INFLIGHT_KEY % sigla.lower())
        limite_scan = min(capacidade + em_voo + 1_000, 60_000)
        linhas = list(
            Process.objects.filter(
                tribunal_id=sigla,
                enriquecimento_status=Process.ENRIQ_PENDENTE,
            ).values_list('pk', 'numero_cnj')[:limite_scan]
        )
        candidatos, recusados = _separar_fora_da_fonte(sigla, linhas)
        ids = _filtrar_em_voo(conn, sigla, candidatos, capacidade)
        for pid in ids:
            try:
                queue.enqueue(enriquecer_processo, pid, job_timeout=ENRICH_TIMEOUT,
                              retry=ENRICH_RETRY)
            except Exception as exc:
                logger.warning('falha ao enfileirar', extra={'pid': pid, 'erro': str(exc)})
        relatorio[sigla] = (f'+{len(ids)} (fila {qlen}->{qlen+len(ids)}, alvo {alvo:,}, '
                            f'em voo {em_voo:,}, candidatos {len(candidatos):,}'
                            + (f', fora da fonte {recusados:,}' if recusados else '') + ')')
    logger.info('reabastecer_filas_enriquecimento', extra={'r': relatorio})
    _alertar_fora_da_fonte()
    return relatorio


# Quantos processos da faixa fora-da-fonte o refill fecha por passada, por
# tribunal. O refill roda a cada 2 min ⇒ 10k = 300 mil/h, e os ≈ 2,94 M do
# eproc do TJSP levam ~10 h para sair de `pendente`. É de propósito: um UPDATE
# de 60k linhas a cada 2 min numa tabela de 102 M sob escrita constante é como
# se derruba o banco, e este time já derrubou o site com ACCESS EXCLUSIVE preso
# atrás de consulta longa (incidente de 25/08/2026).
RECUSA_FORA_DA_FONTE_BATCH = 10_000


def _separar_fora_da_fonte(sigla: str, linhas: list) -> tuple[list, int]:
    """Tira da fila o que a fonte comprovadamente não tem, EM LOTE.

    Por que aqui e não só no job: se cada processo da faixa virasse um job, o
    worker o resolveria sem rede — e 40 réplicas sem rede publicam milhares de
    events por SEGUNDO no stream, que tem `MAXLEN` e é **compartilhado com
    todos os tribunais**. O TJSP expulsaria os events de TRF1/TJMG antes de o
    drainer aplicá-los. Fechar em lote no refill gasta um UPDATE por passada e
    zero event.

    O guard equivalente em `BaseEsajEnricher.enriquecer` continua existindo —
    ele cobre o clique manual e os jobs que já estavam na fila.
    """
    fora = _hook_fora_da_fonte(sigla)
    if not fora:
        return [pk for pk, _ in linhas], 0
    aptos: list[int] = []
    recusa: dict[str, list[int]] = {}
    for pk, cnj in linhas:
        motivo = fora(cnj)
        if motivo:
            recusa.setdefault(motivo, []).append(pk)
        else:
            aptos.append(pk)
    total = 0
    for motivo, pks in recusa.items():
        lote = pks[:RECUSA_FORA_DA_FONTE_BATCH]
        try:
            agora = _tz.now()
            n = Process.objects.filter(pk__in=lote).update(
                enriquecimento_status=Process.ENRIQ_NAO_ENCONTRADO,
                enriquecido_em=agora,
                # CAMPAINHA: `enriquecido_em` e `enriquecimento_status` são
                # campos do doc. Sem isto o processo fica `nao_encontrado` no
                # banco e o doc guarda o estado anterior para sempre.
                atualizado_em=agora,
            )
        except Exception as exc:
            logger.warning('falha ao fechar faixa fora-da-fonte',
                           extra={'sigla': sigla, 'motivo': motivo, 'erro': str(exc)[:200]})
            continue
        registrar_fora_do_esaj(sigla, motivo, n)
        total += n
    return aptos, total


# Última contagem impressa, por chave — só alerta quando CRESCEU, pra não
# transformar o ERROR num carimbo de 30 linhas/h depois que a faixa drenar.
_ultimo_censo_fora: dict[str, int] = {}


def _alertar_fora_da_fonte() -> None:
    censo = censo_fora_do_esaj()
    for chave, total in sorted(censo.items()):
        if total <= _ultimo_censo_fora.get(chave, 0):
            continue
        sigla, _, motivo = chave.partition('|')
        logger.error(
            'FORA DA FONTE: %s processos %s nao foram consultados — a faixa `%s` '
            'nao existe na consulta publica deste tribunal (+%s desde o ultimo aviso)',
            f'{total:,}', sigla, motivo,
            f'{total - _ultimo_censo_fora.get(chave, 0):,}')
        _ultimo_censo_fora[chave] = total


# ---------------------------------------------------------------------------
# Vigia das fontes: pausa quem está fora, e DESPAUSA quando volta.
# ---------------------------------------------------------------------------
#: Siglas pausadas pelo vigia. Existe separado de `_PAUSA_KEY` por um motivo
#: só: o vigia NUNCA despausa o que um humano pausou. Pausa de gente tem
#: motivo que o vigia não conhece (contrato, ordem judicial, incidente aberto).
_AUTO_PAUSA_KEY = 'enrich:pausa_automatica'

#: Quantas sondas por tribunal, e quantas precisam concordar. Uma sonda só
#: mede o proxy, não a fonte: um `ProxyError` isolado viraria "tribunal fora"
#: e pausaria um tribunal saudável.
VIGIA_SONDAS = 3
VIGIA_MAIORIA = 2
#: Teto de espera de CADA sonda. Nada no caminho de um tick periódico sem teto
#: (regra nº 7): sem isto o vigia herdaria o timeout do socket e travaria o
#: worker de monitoring.
VIGIA_TIMEOUT = 20
#: Abaixo disto um 2xx não é a página de consulta — é casca de erro. A do TJMG
#: tem 912 bytes; a do TJPA, 848.
VIGIA_BYTES_MINIMOS = 4000


def _auto_pausados() -> set[str]:
    from django.core.cache import cache
    return set(cache.get(_AUTO_PAUSA_KEY) or [])


def _set_auto_pausados(siglas: set[str]) -> None:
    from django.core.cache import cache
    cache.set(_AUTO_PAUSA_KEY, sorted(s.upper() for s in siglas), timeout=None)


def sondar_fonte(sigla: str) -> tuple[bool, str]:
    """`(fonte_viva, motivo)` — decidido por MAIORIA de sondas independentes.

    Só o PJe/e-SAJ sabem se a própria resposta é conteúdo. Reaproveitamos o
    detector que o enricher já usa (`_detect_pje_server_error`), então um
    tribunal que passe a servir página de erro nova é reconhecido assim que o
    marcador entrar na lista — num lugar só.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from enrichers.pje import _detect_pje_server_error
    from djen.proxies import ProxyScrapePool

    cls = _ENRICHERS.get(sigla)
    url = getattr(cls, 'LIST_URL', '') or getattr(cls, 'BASE_URL', '')
    if not url:
        return True, 'sem URL de sonda'          # abster > chutar: não pausa

    pool = ProxyScrapePool()

    def _sonda(_) -> tuple[str, str]:
        proxy = pool.get()
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        try:
            r = requests.get(url, proxies=proxies, timeout=VIGIA_TIMEOUT,
                             headers={'User-Agent': 'Mozilla/5.0'})
        except requests.RequestException as exc:
            return 'rede', type(exc).__name__     # NÃO conta como fonte morta
        marcador = _detect_pje_server_error(r.text)
        detalhe = f'HTTP {r.status_code}, {len(r.content)} bytes'
        if marcador:
            return 'morta', f'pagina de erro ({marcador})'
        if r.ok and len(r.content) > VIGIA_BYTES_MINIMOS:
            return 'viva', detalhe
        if r.ok or r.status_code >= 500:
            # 2xx pequeno demais para ser conteúdo, ou 5xx: a fonte falou e
            # falhou. TJPA (30/08) devolvia 200 com 848 bytes.
            return 'morta', detalhe
        # 4xx num GET seco NÃO prova que a fonte caiu: pode ser a nossa sonda
        # pedindo a URL errada, ou um fluxo que exige sessão/POST antes. Medido
        # em 30/08: TJDFT devolve 404 e TJMT 401 na `LIST_URL` — e não dá pra
        # separar "tribunal fora" de "sonda errada" só com isso. Abster >
        # chutar (regra nº 6): inconclusivo não pausa, mas é dito.
        return 'duvida', detalhe

    # As sondas vão JUNTAS. Sequencial, o vigia inteiro levava mais que o
    # próprio intervalo de 10 min (16 tribunais x 3 sondas x 25 s no pior
    # caso) — um tick que não cabe no intervalo se atropela e nunca fecha.
    with ThreadPoolExecutor(max_workers=VIGIA_SONDAS) as ex:
        resultados = list(ex.map(_sonda, range(VIGIA_SONDAS)))
    vivas = sum(1 for k, _ in resultados if k == 'viva')
    mortas = sum(1 for k, _ in resultados if k == 'morta')

    def _porque(veredito: str) -> str:
        """O motivo tem que vir de quem DECIDIU, não da última sonda a responder.

        Com 2 sondas vendo página de erro e 1 falhando por rede, reportar a
        última dizia "fonte fora: ProxyError" — culpando o proxy por uma
        decisão que foi da fonte. Motivo errado manda a investigação pro lado
        errado, que é pior que motivo nenhum.
        """
        for k, detalhe in resultados:
            if k == veredito:
                return detalhe
        return ''
    if mortas >= VIGIA_MAIORIA:
        return False, f'{mortas} de {len(resultados)} sondas: {_porque("morta")}'
    if vivas >= VIGIA_MAIORIA:
        return True, f'{vivas} de {len(resultados)} sondas: {_porque("viva")}'
    duvidas = [d for k, d in resultados if k == 'duvida']
    redes = [d for k, d in resultados if k == 'rede']
    detalhe = (duvidas or redes or [_porque('morta')])[0]
    return True, (f'inconclusivo ({vivas} viva/{mortas} morta/'
                  f'{len(duvidas)} duvida/{len(redes)} rede): {detalhe}')


@job('monitoring', timeout=600)
def tick_vigia_fontes() -> dict:
    """Pausa tribunal cuja fonte está comprovadamente fora; despausa quando volta.

    Sem isto, um tribunal fora do ar queima o pool COMPARTILHADO de IPs até
    alguém reparar: o TJMG, em 30/08/2026, fez 1.184 erro/h por réplica (~7.100
    req/h em 6) contra uma página de erro estática — e nada avisou.

    A recuperação é a metade que costuma faltar. Pausa manual é esquecida: foi
    assim que dois workers velhos deixaram um watchdog parado por 3.007 dias.
    Por isso o vigia só despausa o que ELE mesmo pausou, e diz em ERROR o que
    fez — teto é alerta, nunca corte mudo (regra nº 2).
    """
    from concurrent.futures import ThreadPoolExecutor

    pausados = enrich_pausados()
    automaticos = _auto_pausados()
    relatorio: dict = {}

    a_sondar = [s for s in sorted(_ENRICHERS)
                if not (s in pausados and s not in automaticos)]
    for sigla in sorted(_ENRICHERS):
        if sigla not in a_sondar:
            relatorio[sigla] = 'pausado por humano — vigia não mexe'

    # Um tribunal fora não pode atrasar a sonda dos outros.
    with ThreadPoolExecutor(max_workers=8) as ex:
        sondados = dict(zip(a_sondar, ex.map(sondar_fonte, a_sondar)))

    for sigla in a_sondar:
        auto = sigla in automaticos
        viva, motivo = sondados[sigla]

        if not viva and sigla not in pausados:
            set_enrich_pausados(pausados | {sigla})
            _set_auto_pausados(automaticos | {sigla})
            pausados = pausados | {sigla}
            automaticos = automaticos | {sigla}
            logger.error('vigia PAUSOU %s — fonte fora: %s', sigla, motivo)
            relatorio[sigla] = f'pausado: {motivo}'
        elif viva and auto:
            set_enrich_pausados(pausados - {sigla})
            _set_auto_pausados(automaticos - {sigla})
            pausados = pausados - {sigla}
            automaticos = automaticos - {sigla}
            logger.error('vigia DESPAUSOU %s — fonte voltou: %s', sigla, motivo)
            relatorio[sigla] = f'despausado: {motivo}'
        else:
            relatorio[sigla] = ('fora (já pausado)' if not viva else 'ok')

    return relatorio
