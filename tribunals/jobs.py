"""Jobs RQ pro app tribunals — classificação de leads em batch."""
import logging
from datetime import timedelta

from django.db.models import F, Max, Q
from django.utils import timezone
from django_rq import job

from .classificador import VERSAO, classificar_e_persistir
from .models import Movimentacao, Process

logger = logging.getLogger('voyager.tribunals.jobs')


@job('classificacao', timeout=14400)
def reclassificar_recentes(dias: int = 7, batch_size: int = 1000,
                           cap_nunca_classificados: int = 5_000_000,
                           paralelizar: bool = True) -> dict:
    """Re-classifica processos com mov inserida nos últimos N dias.

    Idempotente — atualiza Process.classificacao_em a cada run.
    Re-classifica também processos NUNCA classificados (classificacao_em IS NULL)
    pra cobrir backlog inicial. Cap default 500k é suficiente pra rodar
    de hora em hora e drenar ~2.4M backlog em alguns dias.

    Quando `paralelizar=True` (default), splitta o trabalho em N jobs
    `reclassificar_batch` enfileirados na fila `classificacao` — workers
    paralelos drenam. Quando False, processa sequencialmente neste job
    (útil pra runs pequenos / debug).
    """
    cutoff = timezone.now() - timedelta(days=dias)

    pids_qs = (
        Movimentacao.objects.filter(inserido_em__gte=cutoff)
        .values_list('processo_id', flat=True)
        .distinct()
    )
    pids_recentes = set(pids_qs)
    nunca_classificados = set(
        Process.objects.filter(classificacao_em__isnull=True)
        .values_list('id', flat=True)[:cap_nunca_classificados]
    )
    pids_alvo = list(pids_recentes | nunca_classificados)
    logger.info(
        'reclassificar_recentes: alvo=%d (recentes=%d novos=%d) paralelizar=%s',
        len(pids_alvo), len(pids_recentes), len(nunca_classificados), paralelizar,
    )

    if paralelizar and len(pids_alvo) > batch_size:
        # Enfileira N batches na fila classificacao — workers drenam paralelo
        n_jobs = 0
        for i in range(0, len(pids_alvo), batch_size):
            batch = pids_alvo[i:i+batch_size]
            reclassificar_batch.delay(batch)
            n_jobs += 1
        logger.info('reclassificar_recentes split em %d batches de %d', n_jobs, batch_size)
        return {'enfileirados': n_jobs, 'pids_total': len(pids_alvo), 'versao': VERSAO}

    n_done = 0; n_fail = 0
    for i in range(0, len(pids_alvo), batch_size):
        batch_pids = pids_alvo[i:i+batch_size]
        for p in Process.objects.filter(pk__in=batch_pids).iterator(chunk_size=200):
            try:
                classificar_e_persistir(p, registrar_log=True)
                n_done += 1
            except Exception as exc:
                logger.warning('classif fail pid=%d: %s', p.pk, exc)
                n_fail += 1
        if (i // batch_size) % 10 == 0:
            logger.info('  progresso %d/%d', n_done, len(pids_alvo))

    logger.info('reclassificar_recentes done: ok=%d fail=%d versao=%s',
                n_done, n_fail, VERSAO)
    return {'classificados': n_done, 'falhas': n_fail, 'versao': VERSAO}


@job('classificacao', timeout=600)
def reclassificar_batch(process_ids: list[int]) -> dict:
    """Reclassifica um lote de processos passados por ID."""
    n_done = 0; n_fail = 0
    for p in Process.objects.filter(pk__in=process_ids).iterator(chunk_size=200):
        try:
            classificar_e_persistir(p, registrar_log=True)
            n_done += 1
        except Exception as exc:
            logger.warning('reclassificar_batch fail pid=%d: %s', p.pk, exc)
            n_fail += 1
    return {'classificados': n_done, 'falhas': n_fail}


_CLASSIF_BATCH_SIZE = 500
_CLASSIF_CAP_POR_RUN = 50_000


@job('classificacao', timeout=14400)
def reclassificar_por_prioridade(
    cap: int = _CLASSIF_CAP_POR_RUN,
    batch_size: int = _CLASSIF_BATCH_SIZE,
) -> dict:
    """Cron único de classificação por prioridade.

    Grupo 1 (prioridade): processos com classificacao_em < ultima_movimentacao_em
    ou nunca classificados — ordenados por ultima_movimentacao_em DESC (mais recentes primeiro).
    Grupo 2 (fallback): processos já classificados, ordenados por classificacao_em ASC
    (reclassifica os mais antigos quando o grupo 1 esgota).
    Idle quando todos os processos estão com classificação atualizada.
    """
    pids = list(
        Process.objects
        .filter(ultima_movimentacao_em__isnull=False)
        .filter(
            Q(classificacao_em__isnull=True) |
            Q(classificacao_em__lt=F('ultima_movimentacao_em'))
        )
        .order_by('-ultima_movimentacao_em')
        .values_list('id', flat=True)[:cap]
    )
    grupo = 'desatualizados'

    if not pids:
        pids = list(
            Process.objects
            .filter(classificacao_em__isnull=False)
            .order_by('classificacao_em')
            .values_list('id', flat=True)[:cap]
        )
        grupo = 'mais_antigos'

    if not pids:
        logger.info('reclassificar_por_prioridade: idle — todos processos atualizados')
        return {'status': 'idle', 'enfileirados': 0, 'versao': VERSAO}

    n_jobs = 0
    for i in range(0, len(pids), batch_size):
        reclassificar_batch.delay(pids[i:i + batch_size])
        n_jobs += 1

    logger.info(
        'reclassificar_por_prioridade: grupo=%s pids=%d batches=%d versao=%s',
        grupo, len(pids), n_jobs, VERSAO,
    )
    return {'grupo': grupo, 'pids_total': len(pids), 'enfileirados': n_jobs, 'versao': VERSAO}
