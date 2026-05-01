"""Jobs RQ pro app tribunals — classificação de leads em batch."""
import logging
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone
from django_rq import job

from .classificador import VERSAO, classificar_e_persistir
from .models import Movimentacao, Process

logger = logging.getLogger('voyager.tribunals.jobs')


@job('default', timeout=14400)
def reclassificar_recentes(dias: int = 7, batch_size: int = 1000) -> dict:
    """Re-classifica processos com mov inserida nos últimos N dias.

    Idempotente — atualiza Process.classificacao_em a cada run.
    Re-classifica também processos NUNCA classificados (classificacao_em IS NULL)
    pra cobrir backlog inicial.
    """
    cutoff = timezone.now() - timedelta(days=dias)

    # Process com mov recente OU nunca classificado
    pids_qs = (
        Movimentacao.objects.filter(inserido_em__gte=cutoff)
        .values_list('processo_id', flat=True)
        .distinct()
    )
    pids_recentes = set(pids_qs)
    nunca_classificados = set(
        Process.objects.filter(classificacao_em__isnull=True)
        .values_list('id', flat=True)[:50000]  # cap pra não estourar
    )
    pids_alvo = list(pids_recentes | nunca_classificados)
    logger.info(
        'reclassificar_recentes: alvo=%d (recentes=%d novos=%d)',
        len(pids_alvo), len(pids_recentes), len(nunca_classificados),
    )

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
