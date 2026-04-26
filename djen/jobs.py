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


@job('default', timeout=300)
def sincronizar_movimentacoes(process_id: int) -> dict:
    """Atualiza movimentações de um processo específico via DJEN (?numeroProcesso=...)."""
    from .ingestion import ingest_processo
    p = Process.objects.select_related('tribunal').get(pk=process_id)
    return ingest_processo(p)
