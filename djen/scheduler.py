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

from search.sync_incremental import tick_sync_es_incremental

from dashboard.tasks import (
    refresh_ingestion_rate_hora,
    refresh_materialized_views,
    warm_charts_leves,
    warm_cobertura_enriquecimento,
    warm_charts_pesados,
    warm_enriquecimento,
    warm_estatisticas_tribunal,
    warm_filtros_movimentacoes,
    warm_ingestao_por_hora,
    warm_command_center,
    warm_kpis,
    warm_leads_charts,
    warm_leads_visibilidade_charts,
    warm_partes,
    warm_pipeline_diario,
    warm_tribunal_status,
    warm_vetorizacao_fleet,
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

    # Fase 2 (cobertura por valor): devolve status='erro' → 'pendente' nos
    # tribunais Juriscope∩enricher (o reabastecer só pega 'pendente'; erros
    # transitórios ficavam presos). Bounded pelo pendente floor. Ver ENRICHMENT.md.
    from enrichers.jobs import tick_requeue_erros_enricher
    scheduler.add_job(
        tick_requeue_erros_enricher.delay,
        'interval',
        minutes=10,
        id='requeue_erros_enricher',
        replace_existing=True,
    )

    # Completude do acervo (F5): passada incremental na varredura do Datajud.
    # Diária e de madrugada porque é a hora em que a APIKey compartilhada do CNJ
    # está mais folgada — e porque o incremental é pequeno (só o que mudou desde
    # o watermark), diferente da varredura completa, que é evento único por
    # tribunal. Só toca tribunal que já tem cursor. Ver datajud/varredura.py.
    from datajud.jobs import tick_varredura_incremental
    scheduler.add_job(
        tick_varredura_incremental.delay,
        'cron',
        hour=2,
        minute=20,
        id='varredura_incremental',
        replace_existing=True,
    )

    # Watchdog da varredura: devolve pra fila o tribunal que ficou órfão quando
    # um worker morreu. Precisa existir porque o RQ só declara abandono quando o
    # job estoura o próprio timeout — e o nosso é de 24h. Sem isto, um deploy no
    # meio da varredura para o TJSP por um dia sem ninguém ver.
    from datajud.jobs import tick_varredura_watchdog
    scheduler.add_job(
        tick_varredura_watchdog.delay,
        'interval',
        minutes=5,
        id='varredura_watchdog',
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

    # Watcher da fila datajud: registra profundidade em cache + loga 🔴 se passar
    # do teto de alerta (o refill/auto-enqueue capam em 100k; estouro = regressão).
    # É o olho que faltava no incidente 02/07 (fila foi a 63M sem ninguém ver).
    from datajud.jobs import datajud_queue_watch
    scheduler.add_job(
        datajud_queue_watch.delay,
        'interval',
        minutes=5,
        id='datajud_queue_watch',
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
        (refresh_ingestion_rate_hora,'refresh_ingestion_rate_hora',{'hours': 6}),
        (warm_pipeline_diario,       'warm_pipeline_diario',       {'hours': 6}),
        (warm_partes,                'warm_partes',                {'minutes': 30}),
        (warm_estatisticas_tribunal, 'warm_estatisticas_tribunal', {'minutes': 30}),
        (warm_filtros_movimentacoes, 'warm_filtros_movimentacoes', {'minutes': 30}),
        (warm_charts_pesados,        'warm_charts_pesados',        {'minutes': 30}),
        (warm_enriquecimento,        'warm_enriquecimento',        {'minutes': 30}),
        (warm_leads_charts,          'warm_leads_charts',          {'minutes': 30}),
        (warm_leads_visibilidade_charts, 'warm_leads_visibilidade_charts', {'minutes': 15}),
        (warm_tribunal_status,       'warm_tribunal_status',       {'minutes': 15}),
        (warm_cobertura_enriquecimento, 'warm_cobertura_enriquecimento', {'hours': 6}),
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

    # Telemetria da frota de vetorização (Zordon) — INLINE, a cada 10 min.
    # Só faz 1 GET HTTP ao endpoint /api/vetorizacao/fleet do Zordon e grava
    # no cache (chave vetor:fleet:v1). A página /dashboard/vetorizacao/ lê do
    # cache (fast path). Nunca propaga exceção — degrade gracioso.
    # Cadência 10min casa com o snapshot de taxa (últimos 10min) do endpoint.
    scheduler.add_job(
        warm_vetorizacao_fleet,
        'interval',
        minutes=10,
        id='warm_vetorizacao_fleet',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Sync incremental Postgres → ES — INLINE, a cada 10 min.
    # Fecha os 3 fluxos que escrevem em LOTE e NÃO disparam signal (logo, nunca
    # chegavam ao ES sozinhos): ingestão DJEN (bulk_create de Process/Movimentacao),
    # reclassificação em lote (.update()) e o tem_sinal_precatorio de processo
    # novo. Keyset/watermark no cache, teto por tick, kill-switch `sync_es:off`.
    # Sem isto o ES envelhece e o mapa comercial volta a mentir por omissão.
    scheduler.add_job(
        tick_sync_es_incremental,
        'interval',
        minutes=10,
        id='sync_es_incremental',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Command Center — custo QuickPod + ponto no ring de histórico 24h.
    # INLINE, a cada 60s. Leve (1-2 GETs à API QuickPod + read-modify-write do
    # ring no cache). Nunca propaga exceção. max_instances=1 garante que o ring
    # de snapshots (read-modify-write) não tenha corrida. A cadência de 60s dá
    # ~600 pontos ao longo de 24h (dentro do teto _COMMAND_HIST_MAX).
    scheduler.add_job(
        warm_command_center,
        'interval',
        seconds=60,
        id='warm_command_center',
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

    # Completude do acervo — mede os DOIS lados (nosso número vs o que a fonte
    # declara) fora do caminho da requisição. É caro (conta 1,39 bilhão de docs
    # no ES + agrega IngestionRun), por isso 30 min e nunca no hot path.
    from dashboard.completude_warm import warm_completude
    scheduler.add_job(
        warm_completude.delay,
        'interval',
        minutes=30,
        id='warm_completude',
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

    # Monitoramento (Fase F) — varredura diária 05:00 inline.
    # Idempotente (constraint unique em Detection), cria detections e
    # enfileira entregas webhook na fila 'monitoring'.
    from monitoring.scheduler import register_monitoring_jobs
    register_monitoring_jobs(scheduler)

    # ── DIÁRIOS PRÓPRIOS (terceira porta: DJE/TJSP, DEJT, STF, DOEs) ─────────
    # DESLIGADO POR PADRÃO (`DIARIOS_SCHEDULER_ENABLED=False`), e isso é
    # decisão, não esquecimento. Um único caderno-dia do DJE/TJSP rendeu 29.136
    # movimentações; o backfill completo é da ordem de centenas de milhões de
    # linhas contra um Postgres que a própria documentação classifica como
    # disk-I/O-bound (ver DB_CONTENCAO em OPS.md). Ligar isso em série é decisão
    # de arquitetura — particionamento/recorte —, não efeito colateral de deploy.
    #
    # Quando ligar: `DIARIOS_SCHEDULER_ENABLED=1` +
    # `DIARIOS_FONTES_AGENDADAS=stf` (uma fonte por vez). Para parar sem deploy:
    # `manage.py diarios_pausar --tudo`. Runbook em .ia/DIARIOS.md.
    if getattr(settings, 'DIARIOS_SCHEDULER_ENABLED', False):
        from diarios.jobs import catalogar_fronteira, tick_todas

        # Tick: alimenta a fila `diarios` com unidades pendentes. Teto por fonte
        # (WATERMARK_POR_FONTE=200) já vem do job — o cron só bate na porta.
        # 10 min é a mesma cadência do `tick_backfill_retroativo` do DJEN, e por
        # isso mesmo: a unidade aqui é um caderno de minutos, não uma página.
        scheduler.add_job(
            tick_todas.delay,
            'interval',
            minutes=10,
            id='diarios_tick_todas',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Fronteira: recataloga os últimos 7 dias das fontes cuja janela alcança
        # hoje (STF é fluxo corrente; DEJT ainda publica pauta/ata; os DOEs são
        # diários). A folga de 7 dias é o que torna o corte do dia AINDA ABERTO
        # (ver `ColetorSTF.catalogar`) seguro: o dia entra amanhã, sem buraco.
        # 05:40 é depois do refresh das MVs (03:00) e antes do pico de uso.
        scheduler.add_job(
            catalogar_fronteira.delay,
            'cron',
            hour=5,
            minute=40,
            args=[7],
            id='diarios_catalogar_fronteira',
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
        )
        logger.info('agendado diarios: tick_todas (10min) + catalogar_fronteira (05:40) — '
                    'fontes=%s', getattr(settings, 'DIARIOS_FONTES_AGENDADAS', []) or 'todas')
    else:
        logger.info('diarios: agendamento DESLIGADO (DIARIOS_SCHEDULER_ENABLED=0) — '
                    'coleta só por `manage.py diarios_coletar`')

    # Gate de completude do ÍNDICE — FORA do `if DIARIOS_SCHEDULER_ENABLED`, e
    # isso é a lição inteira deste job. A coleta de diário hoje acontece à MÃO
    # (`manage.py diarios_coletar`) com o agendamento desligado; um gate que só
    # rodasse junto com o agendamento não teria pego o caso que o criou:
    # 27.619 das 220.544 linhas do DJE/TJSP de 12/03/2025 fora do índice, com
    # as 8 edições marcadas `ok` (medido em 21/08/2026).
    #
    # Custo em repouso: UMA query barata (edição `ok` sem carimbo de
    # conferência). Sem edição coletada, não faz nada. Vai para a fila
    # `default` com `job_id` fixo — nunca para a `diarios`, que num backfill
    # tem 200 cadernos à frente, e nunca para a fila da ingestão.
    if getattr(settings, 'DIARIOS_GATE_INDICE_ENABLED', True):
        from diarios.jobs import agendar_conferencia_indice

        scheduler.add_job(
            agendar_conferencia_indice,
            'interval',
            minutes=15,
            id='diarios_conferir_indice',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info('agendado diarios_conferir_indice (15min) — gate de índice')
    else:
        logger.warning('diarios: GATE DE ÍNDICE DESLIGADO — edição coletada pode '
                       'ficar fora da busca sem ninguém saber')

    # Gate de completude do ÍNDICE da porta do DATAJUD. Job separado do gate do
    # diário porque o recorte é outro: lá é `(tribunal, dia)`, aqui é a JANELA
    # DE ESCRITA (`inserido_em`) — o Datajud entrega o histórico inteiro de um
    # processo numa requisição, então uma sincronização espalha linhas por
    # décadas de `data_disponibilizacao` e o gate do diário nunca alcançaria.
    #
    # O que ele fecha, medido em 24/08/2026: 100% das movimentações escritas
    # por esta porta nos últimos 5 min fora do índice, 42,27% das de 5-15 min,
    # e 0 de 500 processos tocados nas últimas 2 h com o doc em dia.
    #
    # Custo por passada, medido com EXPLAIN em produção: 2,33 s (frio) por
    # passo de 15 min do lado das movimentações e 0,29 s do lado dos processos.
    # A vazão da porta varia 28x no dia (28.610 linhas/h no vale, 798.824/h
    # no pico), então o passo de 15 min é PONTO DE PARTIDA: quando não cabe
    # no teto de leitura, o gate divide a janela ao meio em vez de travar.
    if getattr(settings, 'DATAJUD_GATE_INDICE_ENABLED', True):
        from datajud.jobs import agendar_conferencia_indice_datajud

        scheduler.add_job(
            agendar_conferencia_indice_datajud,
            'interval',
            minutes=15,
            id='datajud_conferir_indice',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info('agendado datajud_conferir_indice (15min) — gate de índice')
    else:
        logger.warning('datajud: GATE DE ÍNDICE DESLIGADO — o que a porta grava '
                       'pode ficar fora da busca sem ninguém saber')

    # SENTINELA do índice de PROCESSOS. Não é um gate de porta: os dois gates
    # acima cobrem a JANELA DE ESCRITA de uma porta cada um. Esta aqui olha o
    # acervo INTEIRO por amostra aleatória, porque o buraco que ela existe para
    # ver não veio de uma porta só — veio de todo caminho que escreve em lote
    # (`bulk_create`, `.update()`) e não dispara `post_save`.
    #
    # Medido em 24/08/2026, amostra de 4.000 pks por faixa (seed 20260824) com
    # `_mget` por id: 13,99% dos processos do Postgres estavam FORA do índice
    # (4.380 de 31.301), concentrados nas faixas de pk mais novas — 0,00% nos
    # cinco primeiros oitavos, 45,99% / 9,44% / 61,44% nos três últimos.
    #
    # Nada acusava. A fila `es_index` marcava zero e o `_cat/indices` mostrava
    # 104.594.795 documentos contra 87.709.209 do `_count` (`participacoes` é
    # `nested`: cada objeto aninhado é um doc Lucene), ou seja, a contagem mais
    # à mão dizia que o índice tinha MAIS processos do que o banco tem linhas.
    #
    # DIÁRIA, e não de 15 min: a amostra pequena custa ~3 s mas faz 8 leituras
    # aleatórias de 1.000 pks no Postgres, que é disk-I/O-bound. É sentinela,
    # não gate — quem tem de pegar a escrita fresca são os dois gates acima.
    if getattr(settings, 'SEARCH_SENTINELA_PROCESSOS_ENABLED', True):
        from search.jobs import agendar_conferencia_indice_processos

        scheduler.add_job(
            agendar_conferencia_indice_processos,
            'cron',
            hour=6, minute=40,
            id='search_conferir_indice_processos',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info('agendado search_conferir_indice_processos (06:40) — '
                    'sentinela do índice de processos')
    else:
        logger.warning('search: SENTINELA DO ÍNDICE DE PROCESSOS DESLIGADA — o '
                       'passivo de 24/08/2026 pode reabrir sem ninguém saber')

    return scheduler
