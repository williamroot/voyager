import logging
from datetime import date, timedelta

from django.utils import timezone
from django_rq import job

from tribunals.models import IngestionRun, Process, Tribunal

from .client import DJENClient
from .ingestion import chunk_dates, ingest_window
from .proxies import ProxyScrapePool

logger = logging.getLogger('voyager.djen.jobs')


@job('djen_ingestion', timeout=7200)
def run_daily_ingestion(tribunal_sigla: str) -> dict:
    t = Tribunal.objects.filter(sigla=tribunal_sigla, ativo=True).first()
    if not t:
        logger.info('daily skip: tribunal inativo ou inexistente', extra={'tribunal': tribunal_sigla})
        return {'skipped': 'inativo'}
    if not t.backfill_concluido_em:
        logger.warning('daily skip: backfill ainda em andamento', extra={'tribunal': t.sigla})
        return {'skipped': 'backfill_pendente'}

    fim = date.today()
    inicio = fim - timedelta(days=t.overlap_dias)
    logger.info('daily ingestion inicio %s %s→%s', t.sigla, inicio, fim)
    run = ingest_window(t, inicio, fim, client=DJENClient())
    return {'run_id': run.pk, 'novas': run.movimentacoes_novas, 'duplicadas': run.movimentacoes_duplicadas}


@job('djen_backfill', timeout=86400)
def run_backfill(tribunal_sigla: str, force_inicio: str | None = None) -> dict:
    t = Tribunal.objects.get(sigla=tribunal_sigla)
    if force_inicio:
        inicio = date.fromisoformat(force_inicio)
    else:
        inicio = t.data_inicio_disponivel
    if not inicio:
        raise ValueError(
            f'{t.sigla}: data_inicio_disponivel é NULL. '
            'Rode `djen_descobrir_inicio` primeiro ou passe --inicio YYYY-MM-DD.'
        )

    fim = date.today()
    chunks = list(chunk_dates(inicio, fim, days=30))
    logger.info('run_backfill inicio %s: %d chunks %s→%s', t.sigla, len(chunks), inicio, fim)
    completados = 0
    pulados = 0
    retentados = 0
    falhas = 0
    client = DJENClient()
    for chunk_inicio, chunk_fim in chunks:
        ja_ok = IngestionRun.objects.filter(
            tribunal=t, status=IngestionRun.STATUS_SUCCESS,
            janela_inicio=chunk_inicio, janela_fim=chunk_fim,
        ).exists()
        if ja_ok:
            pulados += 1
            continue
        # Retenta chunks que falharam antes — apaga IngestionRun(status=failed) anteriores
        # da mesma janela pra começar limpo.
        deletados, _ = IngestionRun.objects.filter(
            tribunal=t, status=IngestionRun.STATUS_FAILED,
            janela_inicio=chunk_inicio, janela_fim=chunk_fim,
        ).delete()
        if deletados:
            retentados += 1
        try:
            ingest_window(t, chunk_inicio, chunk_fim, client=client)
            completados += 1
        except Exception as exc:
            falhas += 1
            logger.warning('chunk falhou, seguindo com o próximo', extra={
                'tribunal': t.sigla, 'chunk_inicio': str(chunk_inicio),
                'chunk_fim': str(chunk_fim), 'erro': str(exc)[:200],
            })

    todos_ok = all(
        IngestionRun.objects.filter(
            tribunal=t, status=IngestionRun.STATUS_SUCCESS,
            janela_inicio=ci, janela_fim=cf,
        ).exists()
        for ci, cf in chunks
    )
    if todos_ok:
        Tribunal.objects.filter(pk=t.pk).update(backfill_concluido_em=timezone.now())
    else:
        logger.warning('backfill incompleto — backfill_concluido_em não setado', extra={'tribunal': t.sigla})

    return {
        'tribunal': t.sigla,
        'chunks_total': len(chunks),
        'chunks_completados_agora': completados,
        'chunks_pulados': pulados,
        'chunks_retentados': retentados,
        'chunks_falharam': falhas,
        'concluido': todos_ok,
    }


@job('default', timeout=120)
def refresh_proxy_pool() -> dict:
    count = ProxyScrapePool.singleton().refresh()
    return {'proxies_carregados': count}


@job('djen_backfill', timeout=14400)
def reprocessar_janela(tribunal_sigla: str, inicio: str, fim: str) -> dict:
    """Re-processa uma janela específica (idempotente). Usado pelo command
    djen_reprocessar_janelas_capped pra paralelizar via fila djen_backfill —
    cada janela vira 1 job consumido pelos workers, em vez de loop sequencial.
    """
    from .ingestion import ingest_window
    inicio_d = date.fromisoformat(inicio)
    fim_d = date.fromisoformat(fim)
    t = Tribunal.objects.get(sigla=tribunal_sigla)
    logger.info('reprocessar_janela inicio %s %s→%s', tribunal_sigla, inicio, fim)
    run = ingest_window(t, inicio_d, fim_d)
    return {
        'run_id': run.pk, 'novas': run.movimentacoes_novas,
        'pgs': run.paginas_lidas, 'janela': f'{inicio}→{fim}',
    }


@job('manual', timeout=300)
def sincronizar_movimentacoes(process_id: int) -> dict:
    """Atualiza movimentações de um processo específico via DJEN (?numeroProcesso=...).

    Vai na fila 'manual' (prioritária) porque é sempre disparado pelo botão
    no dashboard — usuário esperando feedback. Usa `prefer_cortex=True` no
    DJEN client pra retornar em ~3-10s (proxy residencial) em vez de 30s+
    rotacionando proxies queimados.
    """
    from .client import DJENClient
    from .ingestion import ingest_processo
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    logger.info('sincronizar_movimentacoes %s %s', p.tribunal_id, p.numero_cnj)
    return ingest_processo(p, client=DJENClient(prefer_cortex=True))


@job('djen_backfill', timeout=600)
def sync_movimentacoes_bulk(process_id: int) -> dict:
    """Sincroniza histórico DJEN de um processo — versão bulk para ingestão automática.

    Mesma lógica do botão 'Sincronizar movimentações' na UI, mas roda na fila
    djen_backfill para não disputar com cliques interativos do dashboard.
    Disparado automaticamente por _enfileirar_todos_enrichments após cada ingestão.
    """
    from .ingestion import ingest_processo
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    logger.info('sync_movimentacoes_bulk %s %s', p.tribunal_id, p.numero_cnj)
    return ingest_processo(p)


# Limites do watchdog (constantes em segundos pra deixar explícito)
WATCHDOG_RUN_ZOMBIE_SECONDS = 60 * 60          # IngestionRun travado >1h = zumbi
WATCHDOG_DAILY_STALE_SECONDS = 60 * 60 * 26    # Tribunal sem run success em 26h


@job('default', timeout=120)
def watchdog_ingestao() -> dict:
    """Garante que a ingestão/backfill estão progredindo. Roda em cron.

    Heals:
      1. Marca como `failed` IngestionRun com status=running e
         finished_at NULL há mais de 1h (worker crashou).
      2. Pra cada tribunal ativo com `backfill_concluido_em IS NULL`,
         re-enfileira `run_backfill` se não houver job dele em
         djen_backfill (pending ou started).
      3. Pra cada tribunal com backfill concluído mas sem IngestionRun
         success nas últimas 26h, re-enfileira `run_daily_ingestion`.
    """
    import django_rq

    agora = timezone.now()

    # 1) Zumbis
    zumbi_cutoff = agora - timedelta(seconds=WATCHDOG_RUN_ZOMBIE_SECONDS)
    zumbis = IngestionRun.objects.filter(
        status=IngestionRun.STATUS_RUNNING,
        finished_at__isnull=True,
        started_at__lt=zumbi_cutoff,
    )
    n_zumbis = zumbis.update(
        status=IngestionRun.STATUS_FAILED,
        finished_at=agora,
        erros=['watchdog: status=running sem finished_at por >1h — worker crashou'],
    )
    if n_zumbis:
        logger.warning('watchdog matou zumbis', extra={'n': n_zumbis})

    # 2 + 3) Re-enfileira backfill/daily
    backfill_q = django_rq.get_queue('djen_backfill')
    ingestion_q = django_rq.get_queue('djen_ingestion')
    backfill_jobs_args = _coletar_args(backfill_q)
    ingestion_jobs_args = _coletar_args(ingestion_q)

    re_backfill = []
    re_daily = []
    daily_stale_cutoff = agora - timedelta(seconds=WATCHDOG_DAILY_STALE_SECONDS)

    default_q = django_rq.get_queue('default')
    default_jobs_args = _coletar_args(default_q)

    for t in Tribunal.objects.filter(ativo=True):
        if t.backfill_concluido_em is None:
            # Usa tick_backfill_retroativo (1 dia por run) em vez de
            # run_backfill (30 dias por chunk). Ticks ficam na fila
            # `default` — verificamos lá pra evitar duplicar.
            if t.sigla not in default_jobs_args and t.sigla not in backfill_jobs_args:
                from .jobs import tick_backfill_retroativo
                tick_backfill_retroativo.delay(t.sigla)
                re_backfill.append(t.sigla)
            continue
        # Backfill concluído — confere se daily rodou recentemente.
        ultima = (
            IngestionRun.objects.filter(
                tribunal=t, status=IngestionRun.STATUS_SUCCESS,
            ).order_by('-finished_at').first()
        )
        if (ultima is None or
            ultima.finished_at is None or
            ultima.finished_at < daily_stale_cutoff):
            if t.sigla not in ingestion_jobs_args:
                from .jobs import run_daily_ingestion
                run_daily_ingestion.delay(t.sigla)
                re_daily.append(t.sigla)

    if re_backfill or re_daily:
        logger.warning('watchdog re-enfileirou jobs', extra={
            're_backfill': re_backfill, 're_daily': re_daily,
        })

    return {
        'zumbis_matados': n_zumbis,
        're_backfill': re_backfill,
        're_daily': re_daily,
    }


def _coletar_args(queue) -> set[str]:
    """Retorna o 1º arg dos jobs ATUALMENTE EM EXECUÇÃO na fila.

    Intencionalmente ignora jobs pendentes: com filas grandes (ex: 14k+
    backfill_dia) escanear pending é O(n) e estoura o timeout do watchdog.
    Jobs pendentes já garantem progresso por si só — o watchdog só precisa
    saber se algum worker já está rodando aquela sigla.
    run_backfill/run_daily_ingestion são idempotentes (verificam watermark),
    então enfileirar duplicata é inofensivo.
    """
    from rq.job import Job as RQJob
    started_ids = list(queue.started_job_registry.get_job_ids())
    if not started_ids:
        return set()
    jobs = RQJob.fetch_many(started_ids, connection=queue.connection)
    return {str(j.args[0]) for j in jobs if j and j.args}


BACKFILL_WATERMARK = 200   # não enfileira mais se djen_backfill já tem isso
BACKFILL_BATCH = 100       # dias por tick


@job('djen_backfill', timeout=3600)
def backfill_dia(tribunal_sigla: str, dia_iso: str) -> dict:
    """Ingere um único dia — unidade atômica do backfill retroativo.

    Skip rápido (sem request à API) se qualquer IngestionRun success já cobre o dia.
    """
    t = Tribunal.objects.get(sigla=tribunal_sigla)
    dia = date.fromisoformat(dia_iso)
    if _dia_coberto(t, dia):
        logger.debug('backfill_dia skip %s %s (já coberto)', tribunal_sigla, dia_iso)
        return {'skip': True, 'dia': dia_iso}
    logger.info('backfill_dia inicio %s %s', tribunal_sigla, dia_iso)
    run = ingest_window(t, dia, dia)
    return {'run_id': run.pk, 'novas': run.movimentacoes_novas, 'dia': dia_iso}


@job('default', timeout=120)
def tick_backfill_retroativo(tribunal_sigla: str) -> dict:
    """Tick a cada 10min: loga progresso e alimenta djen_backfill com dias pendentes.

    Percorre de hoje para o passado. Para quando a fila já tem >= BACKFILL_WATERMARK
    jobs pendentes. Tanto TRF1 quanto TRF3 têm seu próprio tick independente.
    """
    import django_rq

    t = Tribunal.objects.filter(sigla=tribunal_sigla, ativo=True).first()
    if not t or not t.data_inicio_disponivel:
        return {'skip': 'tribunal inativo ou sem data_inicio_disponivel'}

    hoje = date.today()
    ini = t.data_inicio_disponivel
    total_dias = (hoje - ini).days + 1

    cobertos = _dias_cobertos(t, ini, hoje)
    # Mais recente para o mais antigo — prioriza dados frescos
    pendentes = [
        ini + timedelta(days=i)
        for i in range(total_dias - 1, -1, -1)
        if (ini + timedelta(days=i)) not in cobertos
    ]

    pct = len(cobertos) / total_dias * 100 if total_dias else 100.0
    logger.info(
        'backfill %s: %d/%d dias (%.1f%%) · %d pendentes · próximo=%s',
        tribunal_sigla, len(cobertos), total_dias, pct,
        len(pendentes), pendentes[0] if pendentes else '-',
    )

    if not pendentes:
        logger.info('backfill %s: 100%% concluído!', tribunal_sigla)
        if t.backfill_concluido_em is None:
            Tribunal.objects.filter(pk=t.pk).update(backfill_concluido_em=timezone.now())
            logger.info('backfill %s: backfill_concluido_em setado pelo tick', tribunal_sigla)
        return {'completo': True, 'total': total_dias}

    backfill_q = django_rq.get_queue('djen_backfill')
    q_len = len(backfill_q)
    if q_len >= BACKFILL_WATERMARK:
        logger.debug('backfill %s: fila com %d jobs — aguardando', tribunal_sigla, q_len)
        return {'aguardando': True, 'fila': q_len, 'pendentes': len(pendentes)}

    vagas = BACKFILL_WATERMARK - q_len
    a_enfileirar = pendentes[:min(BACKFILL_BATCH, vagas)]
    for dia in a_enfileirar:
        backfill_dia.delay(tribunal_sigla, str(dia))

    logger.info(
        'backfill %s: +%d dias enfileirados (fila era %d) · %.1f%% completo',
        tribunal_sigla, len(a_enfileirar), q_len, pct,
    )
    return {
        'enfileirados': len(a_enfileirar),
        'pendentes_restantes': len(pendentes) - len(a_enfileirar),
        'pct': round(pct, 1),
    }


def _dia_coberto(tribunal: Tribunal, dia: date) -> bool:
    return IngestionRun.objects.filter(
        tribunal=tribunal,
        status=IngestionRun.STATUS_SUCCESS,
        janela_inicio__lte=dia,
        janela_fim__gte=dia,
    ).exists()


def _dias_cobertos(tribunal: Tribunal, ini: date, fim: date) -> set[date]:
    """Retorna set de dates cobertos por algum IngestionRun success na janela."""
    runs = list(
        IngestionRun.objects.filter(
            tribunal=tribunal,
            status=IngestionRun.STATUS_SUCCESS,
            janela_inicio__lte=fim,
            janela_fim__gte=ini,
        ).values('janela_inicio', 'janela_fim')
    )
    covered: set[date] = set()
    for run in runs:
        d = max(run['janela_inicio'], ini)
        end = min(run['janela_fim'], fim)
        while d <= end:
            covered.add(d)
            d += timedelta(days=1)
    return covered


@job('djen_audit', timeout=3600)
def audit_cobertura_dia(tribunal_sigla: str, dia_iso: str) -> dict:
    """Audita a cobertura de um dia que atingiu o CAP via estratégia por órgão.

    Descobrir órgãos a partir da primera passada (até 10k itens), conta itens
    por órgão e compara com o total já ingerido no banco. Roda na fila djen_audit
    para não disputar workers com a ingestão principal.
    """
    t = Tribunal.objects.get(sigla=tribunal_sigla)
    dia = date.fromisoformat(dia_iso)
    client = DJENClient()

    # Coleta primera passada (até 10k) para descobrir os órgãos presentes no dia.
    primeira_passada: list[dict] = []
    for pagina in range(1, 11):
        payload = client._fetch(t.sigla_djen, dia, dia, pagina=pagina, itens_por_pagina=1000)
        page = payload.get('items') or []
        primeira_passada.extend(page)
        if len(page) < 1000:
            break

    orgao_ids = sorted({it.get('idOrgao') for it in primeira_passada if it.get('idOrgao')})
    logger.info(
        'audit %s %s → %d órgãos descobertos via primera passada (%d itens)',
        tribunal_sigla, dia, len(orgao_ids), len(primeira_passada),
    )

    # Conta total via probe por órgão (1 request barato por órgão).
    total_por_orgao: dict[int, int] = {}
    for oid in orgao_ids:
        payload = client._fetch(
            t.sigla_djen, dia, dia,
            pagina=1, itens_por_pagina=1,
            extra_params={'orgaoId': oid},
        )
        total_por_orgao[oid] = int(payload.get('count') or 0)

    total_via_orgaos = sum(total_por_orgao.values())

    # Conta o que de fato está no banco para esse dia.
    from tribunals.models import Movimentacao
    ingerido = Movimentacao.objects.filter(
        tribunal=t,
        data_disponibilizacao=dia,
    ).count()

    gap = total_via_orgaos - ingerido
    cobertura_pct = ingerido / total_via_orgaos * 100 if total_via_orgaos else 100.0

    log_fn = logger.warning if gap > 100 else logger.info
    log_fn(
        'audit %s %s → orgaos=%d total_orgaos=%d ingerido=%d gap=%d cobertura=%.1f%%',
        tribunal_sigla, dia, len(orgao_ids), total_via_orgaos, ingerido, gap, cobertura_pct,
    )

    return {
        'dia': dia_iso,
        'tribunal': tribunal_sigla,
        'orgaos_descobertos': len(orgao_ids),
        'total_via_orgaos': total_via_orgaos,
        'ingerido': ingerido,
        'gap': gap,
        'cobertura_pct': round(cobertura_pct, 1),
    }
