"""Scheduler APScheduler para crons do Voyager.

Usa BlockingScheduler (sem persistência em banco — jobs são leves e
idempotentes, então re-registrar a cada restart é OK).

Padrão: jobs leves e de enfileiramento usam .delay() (RQ). Jobs de warm
do dashboard rodam INLINE no thread pool do scheduler — evita acúmulo
na fila `warm` e elimina dependência de workers externos para o dashboard.
Cada função de warm tem _with_lock próprio; max_instances=1 no APScheduler
é a segunda camada de proteção contra execuções sobrepostas.
"""
import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.db import close_old_connections

from tribunals.models import Tribunal

from dashboard.tasks import (
    refresh_ingestion_rate_hora,
    refresh_materialized_views,
    warm_charts_leves,
    warm_charts_pesados,
    warm_estatisticas_tribunal,
    warm_filtros_movimentacoes,
    warm_ingestao_por_hora,
    warm_kpis,
    warm_leads_charts,
    warm_partes,
    warm_pipeline_diario,
    warm_tribunal_status,
    warm_workers_cache_inline,
)

from .jobs import (
    refresh_proxy_pool,
    run_daily_ingestion,
    tick_backfill_retroativo,
    watchdog_ingestao,
)

logger = logging.getLogger('voyager.djen.scheduler')


def _close_db(event):
    close_old_connections()


def create_scheduler() -> BlockingScheduler:
    """Cria e configura o BlockingScheduler com todos os crons do Voyager."""
    ativos = list(Tribunal.objects.filter(ativo=True).order_by('sigla'))

    # 20 threads: jobs de warm podem rodar horas em paralelo (kpis, charts_pesados,
    # estatisticas, filtros) sem bloquear os ticks curtos (watchdog, backfill).
    scheduler = BlockingScheduler(
        timezone='America/Sao_Paulo',
        executors={'default': ThreadPoolExecutor(20)},
    )
    scheduler.add_listener(
        _close_db,
        EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )

    # Ingestão diária — espalhada na madrugada (00:00–05:59) por índice.
    # Robusto a QUALQUER número de tribunais: usa módulo numa janela de 6h
    # (antes era `hour = 4 + idx//2`, que estourava a hora 23 com >40 ativos —
    # quebrou o scheduler ao ativar os 25 TRTs em 2026-07-04).
    EARLY = {'TRF1': (2, 0), 'TRF3': (2, 30)}
    _WINDOW_MIN = 6 * 60   # 00:00–05:59
    _STEP_MIN = 7          # espaçamento entre tribunais
    for idx, t in enumerate(ativos):
        if t.sigla in EARLY:
            hour, minute = EARLY[t.sigla]
        else:
            off = (idx * _STEP_MIN) % _WINDOW_MIN
            hour, minute = off // 60, off % 60
        scheduler.add_job(
            run_daily_ingestion.delay,
            'cron',
            args=[t.sigla],
            hour=hour,
            minute=minute,
            id=f'daily_ingestion_{t.sigla}',
            replace_existing=True,
        )
        logger.info('agendado daily_ingestion %s %02d:%02d', t.sigla, hour, minute)

    # Tick de backfill retroativo: a cada 10 min por tribunal
    for t in ativos:
        scheduler.add_job(
            tick_backfill_retroativo.delay,
            'interval',
            args=[t.sigla],
            minutes=10,
            id=f'tick_backfill_{t.sigla}',
            replace_existing=True,
        )
        logger.info('agendado tick_backfill %s (cada 10min)', t.sigla)

    # Refresh do pool de proxies: a cada 15 min
    scheduler.add_job(
        refresh_proxy_pool.delay,
        'interval',
        minutes=15,
        id='refresh_proxies',
        replace_existing=True,
    )

    # Watchdog de ingestão: a cada 5 min
    scheduler.add_job(
        watchdog_ingestao.delay,
        'interval',
        minutes=5,
        id='watchdog_ingestao',
        replace_existing=True,
    )

    # Reabastece filas de enriquecimento: a cada 2 min
    from enrichers.jobs import reabastecer_filas_enriquecimento
    scheduler.add_job(
        reabastecer_filas_enriquecimento.delay,
        'interval',
        minutes=2,
        id='refill_enrichers',
        replace_existing=True,
    )

    # Recupera falsos-negativos e-SAJ (nao_encontrado legado pré-fix 2026-07-06):
    # devolve a 'pendente' de forma auto-limitante (só quando pendente baixo).
    # Cauda longa (~3,25M TJSP); a cada 5 min alimenta o reabastecer acima.
    from enrichers.jobs import tick_reenrich_esaj_legacy
    scheduler.add_job(
        tick_reenrich_esaj_legacy.delay,
        'interval',
        minutes=5,
        id='reenrich_esaj_legacy',
        replace_existing=True,
    )

    # Re-treino semanal do modelo de sobrevivência DC→precatório (freshness):
    # KM numpy sobre dados frescos → reescreve surv_strata.json (serving recarrega
    # por mtime). Domingo 03:17 (off-hours), fila default.
    from djen.jobs import retreinar_jurimetria_job
    scheduler.add_job(
        retreinar_jurimetria_job.delay,
        'cron',
        day_of_week='sun',
        hour=3,
        minute=17,
        id='retreinar_jurimetria',
        replace_existing=True,
    )

    # Reabastece fila Datajud: análogo ao PJe — drena backlog histórico
    # de Process com data_enriquecimento_datajud=NULL. Sem este job,
    # processos antigos ficavam pra sempre sem Datajud (observado 96%
    # TRF3 e 38% TRF1 backlog).
    from datajud.jobs import reabastecer_fila_datajud
    scheduler.add_job(
        reabastecer_fila_datajud.delay,
        'interval',
        minutes=2,
        id='refill_datajud',
        replace_existing=True,
    )

    # Health-check da API pública do Datajud (incidente 2026-07-02: chave
    # throttled, _search pendura). Registra estado em cache datajud:api_health
    # e loga WARNING na transição down→up pra sinalizar que dá pra religar.
    from datajud.jobs import datajud_api_healthcheck
    scheduler.add_job(
        datajud_api_healthcheck.delay,
        'interval',
        minutes=15,
        id='datajud_api_healthcheck',
        replace_existing=True,
    )

    # Aquecimento do dashboard — inline no thread pool do scheduler.
    # Sem fila RQ: sem acúmulo de duplicatas, sem dependência de workers externos.
    # _with_lock em cada função é a proteção primária contra sobreposição;
    # max_instances=1 + coalesce=True no APScheduler é a segunda camada.
    for warm_fn, job_id, interval_kwargs in (
        (warm_kpis,                  'warm_kpis',                  {'minutes': 30}),
        (warm_charts_leves,          'warm_charts_leves',          {'minutes': 30}),
        (warm_ingestao_por_hora,     'warm_ingestao_por_hora',     {'minutes': 15}),
        (refresh_ingestion_rate_hora,'refresh_ingestion_rate_hora',{'minutes': 30}),
        (warm_pipeline_diario,       'warm_pipeline_diario',       {'hours': 1}),
        (warm_partes,                'warm_partes',                {'minutes': 30}),
        (warm_estatisticas_tribunal, 'warm_estatisticas_tribunal', {'minutes': 30}),
        (warm_filtros_movimentacoes, 'warm_filtros_movimentacoes', {'minutes': 30}),
        (warm_charts_pesados,        'warm_charts_pesados',        {'minutes': 30}),
        (warm_leads_charts,          'warm_leads_charts',          {'minutes': 30}),
        (warm_tribunal_status,       'warm_tribunal_status',       {'minutes': 15}),
    ):
        scheduler.add_job(
            warm_fn,
            'interval',
            **interval_kwargs,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # REFRESH MV CONCURRENTLY — diário às 3h, inline.
    # lock_timeout=5s na query aborta se outro REFRESH segura lock.
    scheduler.add_job(
        refresh_materialized_views,
        'cron',
        hour=3,
        minute=0,
        id='refresh_materialized_views',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Workers snapshot — INLINE no thread do scheduler (sem RQ). É leve
    # (só lê Redis) e enfileirar fazia pile-up na fila default quando
    # workers ficavam ocupados no warm_dashboard_all pesado.
    scheduler.add_job(
        warm_workers_cache_inline,
        'interval',
        seconds=30,
        id='warm_workers_cache',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Classificação por prioridade — único cron, substitui todos os fluxos inline.
    # Grupo 1: desatualizados (classificacao_em < ultima_movimentacao_em), mais recentes primeiro.
    # Grupo 2 (fallback): classificados há mais tempo (classificacao_em ASC).
    # Idle quando tudo está atualizado — zero enqueue desnecessário.
    # max_instances=1 + coalesce=True: job coordenador é rápido (só enfileira),
    # mas os batches têm timeout 10min; sem proteção acumulariam na fila.
    from tribunals.jobs import reclassificar_por_prioridade
    scheduler.add_job(
        reclassificar_por_prioridade.delay,
        'interval',
        minutes=20,
        id='reclassificar_por_prioridade',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Comparação shadow (T19) — cron diário 04:00. Compara Process.classificacao
    # (versão ativa) contra ClassificacaoShadowLog (versão shadow) das últimas
    # 24h e grava .ia/SHADOW_COMPARISON_YYYYMMDD.md.
    from tribunals.jobs import comparar_shadow_wrapper
    scheduler.add_job(
        comparar_shadow_wrapper,
        'cron',
        hour=4,
        minute=0,
        id='comparar_shadow_daily',
        replace_existing=True,
        misfire_grace_time=3600,
        max_instances=1,
        coalesce=True,
    )

    # Pipeline semanal de lotes de validação humana (T21).
    # Domingo 02:00 — minera FN candidates por tribunal ativo e cria
    # AmostraValidacao(estrategia='fn_candidatos'). Notifica validadores.
    if getattr(settings, 'VALIDACAO_LOTES_SEMANAIS_ENABLED', True):
        from tribunals.jobs import gerar_lotes_semanais_fn
        scheduler.add_job(
            gerar_lotes_semanais_fn.delay,
            'cron',
            day_of_week='sun',
            hour=2,
            minute=0,
            id='gerar_lotes_semanais_fn',
            replace_existing=True,
            misfire_grace_time=7200,
            max_instances=1,
            coalesce=True,
        )
        logger.info('agendado gerar_lotes_semanais_fn (dom 02:00)')

    return scheduler
