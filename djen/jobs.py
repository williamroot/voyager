import collections
import json
import logging
import os
import sys
import time
from datetime import UTC, date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import F, Max
from django.db.models.expressions import RawSQL
from django.db.utils import OperationalError
from django.utils import timezone
from django_rq import job

from tribunals.models import IngestionRun, Process, Tribunal

from .client import DJENClient
from .ingestion import chunk_dates, ingest_window
from .proxies import ProxyScrapePool

logger = logging.getLogger('voyager.djen.jobs')

# Desde 08/2026 o mesmo tribunal pode ter IngestionRun de OUTRAS portas (DJE
# próprio do TJSP, DEJT, STF — ver `diarios/base.py`). Toda leitura de
# COBERTURA daqui tem que ser restrita ao DJEN: sem isto, um run do diário
# próprio na mesma janela faria o backfill do DJEN pular o dia como coberto.
FONTE = 'djen'


@job('djen_ingestion', timeout=300)
def run_daily_ingestion(tribunal_sigla: str) -> dict:
    """Fan-out: enfileira N backfill_dia (1 dia cada) na fila djen_backfill.

    Antes era 1 ingest_window cobrindo overlap_dias+1 dias num único request
    pageado. DJEN responde HTTP 500 ("sistema muito ocupado") em janelas
    grandes — observado TRF1/TRF3 falhando 3 dias seguidos no daily 4-dias
    enquanto backfill_dia (1 dia) sucede normalmente.

    backfill_dia é idempotente (skip via IngestionRun success já cobrindo
    o dia) — re-enfileirar overlap_dias todo dia é barato.
    """
    t = Tribunal.objects.filter(sigla=tribunal_sigla, ativo=True).first()
    if not t:
        logger.info('daily skip: tribunal inativo ou inexistente', extra={'tribunal': tribunal_sigla})
        return {'skipped': 'inativo'}
    if not t.backfill_concluido_em:
        logger.warning('daily skip: backfill ainda em andamento', extra={'tribunal': t.sigla})
        return {'skipped': 'backfill_pendente'}

    fim = date.today()
    inicio = fim - timedelta(days=t.overlap_dias)
    dias = []
    d = inicio
    # at_front: o daily NUNCA disputa FIFO com o backfill histórico (fila com
    # dezenas de milhares de jobs) — senão a edição do dia entra com dias de
    # atraso (visto 08-10/07: home presa em "indexado até 08/07").
    import django_rq
    q = django_rq.get_queue('djen_backfill')
    while d <= fim:
        q.enqueue(backfill_dia, tribunal_sigla, str(d), at_front=True,
                  job_timeout=86400)
        dias.append(str(d))
        d += timedelta(days=1)
    logger.info('daily fan-out %s %s→%s: %d backfill_dia enfileirados',
                t.sigla, inicio, fim, len(dias))
    return {'fanout_dias': len(dias), 'inicio': str(inicio), 'fim': str(fim)}


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
            tribunal=t, fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
            janela_inicio=chunk_inicio, janela_fim=chunk_fim,
        ).exists()
        if ja_ok:
            pulados += 1
            continue
        # Retenta chunks que falharam antes — apaga IngestionRun(status=failed) anteriores
        # da mesma janela pra começar limpo.
        deletados, _ = IngestionRun.objects.filter(
            tribunal=t, fonte=FONTE, status=IngestionRun.STATUS_FAILED,
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
            tribunal=t, fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
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

    Pra janelas 1-dia, força UF strategy (`forcar_uf_em_1d=True`) — quem
    chama reprocessar_janela quer cobertura completa, e o probe count_only
    pode mentir sob WAF (count truncado pra ~100). Sem isso, o reprocesso
    de dia 1-dia faz iter_pages normal e termina cedo com `pgs=1 dup=100`,
    deixando 10k+ movs perdidas mesmo após o "success".
    """
    from .client import DjenBusyError
    from .ingestion import ingest_window
    inicio_d = date.fromisoformat(inicio)
    fim_d = date.fromisoformat(fim)
    t = Tribunal.objects.get(sigla=tribunal_sigla)
    logger.info('reprocessar_janela inicio %s %s→%s', tribunal_sigla, inicio, fim)
    try:
        run = ingest_window(t, inicio_d, fim_d, forcar_uf_em_1d=(inicio_d == fim_d))
    except DjenBusyError:
        # CIRCUITO ABERTO NÃO É FALHA DO DIA — é "volte mais tarde".
        #
        # Medido em 19/08/2026, e foi o pior estrago do dia: com o circuito
        # aberto, cada job saía da fila, via o circuito e morria em
        # MILISSEGUNDOS. A fila inteira drenou — **9.724 dias viraram 10.325
        # falhas em 25 minutos**, sem uma única publicação coletada. Uma janela
        # de 5 minutos de proteção apagou dias de trabalho enfileirado.
        #
        # A própria exceção já dizia isso no docstring ("os jobs devem tratar
        # como 'adiar', não como erro fatal") — só que ninguém tratava.
        # Reenfileirar com atraso mantém o dia vivo e deixa o circuito cumprir o
        # papel dele, que é dar tempo pro CNJ respirar.
        _reenfileirar_adiado(tribunal_sigla, inicio, fim)
        return {'status': 'adiado', 'motivo': 'djen_circuito_aberto',
                'janela': f'{inicio}→{fim}'}
    return {
        'run_id': run.pk, 'novas': run.movimentacoes_novas,
        'pgs': run.paginas_lidas, 'janela': f'{inicio}→{fim}',
    }


#: espera antes de tentar de novo quando o circuito está aberto. Acima do
#: cooldown do breaker (`DJEN_CIRCUIT_COOLDOWN`, 300s), com folga aleatória pra
#: que os N jobs adiados não voltem todos no mesmo segundo — que é exatamente
#: como se reabre um circuito que acabou de fechar.
ESPERA_CIRCUITO = 330


def _reenfileirar_adiado(sigla: str, inicio: str, fim: str) -> None:
    import random
    from datetime import timedelta

    import django_rq
    espera = ESPERA_CIRCUITO + random.randint(0, 120)
    django_rq.get_queue('djen_backfill').enqueue_in(
        timedelta(seconds=espera), 'djen.jobs.reprocessar_janela', sigla, inicio, fim,
        job_id=f'adiado:{sigla}:{inicio}', job_timeout=21600,
    )
    logger.warning('djen circuito aberto — %s %s adiado em %ds (dia NÃO perdido)',
                   sigla, inicio, espera)


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

#: teto do run que AINDA TEM JOB VIVO no worker. "1h sem terminar = worker
#: crashou" é falso para o dia grande: MEDIDO em 24/08/2026, de 170 runs de 1
#: dia que fecharam `success` em 12 h, **11 (6,5%) levaram mais de 60 min** e o
#: pior (TJSP) levou **357 min**. Todos foram marcados "worker crashou"
#: enquanto o worker estava trabalhando, e só voltaram a `success` quando
#: terminaram — poluindo o censo de falhas (era parte dos 640 "worker crashou"
#: contados em 24/08) e, pior, APAGANDO os `erros` já gravados no run, que é
#: justamente a evidência que a regra nº 2 manda preservar.
#: O valor é o `job_timeout` do `reprocessar_janela` (21.600 s) + 1 h de folga:
#: passou disso, nem o RQ acredita mais no job, e o run é zumbi de verdade.
WATCHDOG_RUN_ZOMBIE_COM_JOB_S = 7 * 60 * 60

#: o que fica escrito no run marcado zumbi. Constante porque é lido de volta
#: nos censos de falha.
MSG_ZUMBI = 'watchdog: status=running sem finished_at por >1h — worker crashou'
WATCHDOG_DAILY_STALE_SECONDS = 60 * 60 * 26    # Tribunal sem run success em 26h

#: Orçamento INTERNO do tique, menor que o `timeout=120` do decorador.
#:
#: Quando o RQ mata o job por `JobTimeoutException` ele não deixa nada: os dias
#: já devolvidos não aparecem no log, o teto não vira ERRO, e a única marca é
#: uma linha no FailedJobRegistry — o lugar que, medido em 21/08/2026, tinha
#: 4.259 falhas que ninguém tinha aberto. Com orçamento interno o tique SEMPRE
#: termina falando: diz o que fez e o que não coube.
#:
#: Medido em 21/08/2026 com o banco sob carga de batch: as leituras do watchdog
#: somam 1,6s e os 200 `enqueue` custam 0,17s (0,8ms cada). 90s é ~50x a medida
#: — não é folga pra crescer, é margem pra contenção de disco.
WATCHDOG_ORCAMENTO_S = 90

#: Teto por leitura. Todo estouro histórico do watchdog (17/08/2026: 1 watchdog
#: + 10 `tick_backfill_retroativo` mortos em 15 minutos, todos dentro de
#: `_dias_cobertos`) foi contenção do banco, não custo de algoritmo — no mesmo
#: código, hoje, o pior tribunal leva 0,14s. 20s é 140x a pior medida: só
#: dispara com o banco travado, e aí falha ALTO e rápido em vez de segurar um
#: worker da fila `default` por 120s (foi assim que o watchdog morreu: os ticks
#: na frente dele seguraram a fila).
WATCHDOG_SQL_TIMEOUT_S = 20

#: A partir de quantas falhas no FailedJobRegistry da `default` o watchdog
#: grita. O registry é o cemitério que ninguém visita: em 21/08/2026 ele tinha
#: 4.259 falhas acumuladas em 4 dias (~1.390/dia) e nada no sistema apontava
#: pra isso. Agora aponta.
WATCHDOG_FALHAS_ALERTA = 200

#: Tolerância do detector de código velho (abaixo). Um container que sobe no
#: mesmo minuto de um `git pull` não é defeito.
CODIGO_VELHO_TOLERANCIA_S = 120

#: A partir de quantas horas de atraso a frota vira ERRO, e não só WARNING.
#:
#: Calibração medida em 21/08/2026, logo após o deploy: `Worker.all()` devolveu
#: **251 workers, 244 (97%) nascidos antes do .py mais novo do disco**. É
#: verdade — e é inútil como alarme, porque ninguém reinicia 250 containers a
#: cada commit; um alerta que fica sempre aceso é um alerta gasto. O que
#: distingue o incidente é a ESCALA do atraso: os `worker_default` estavam com
#: código de SETE DIAS. 72h passa longe de um deploy normal e pega o caso real
#: no primeiro dia útil.
FROTA_ATRASO_ALERTA_H = 72


def _dias_com_job_vivo() -> set[tuple[str, str]]:
    """`(sigla, dia)` dos jobs de 1 dia que estão AGORA nas mãos de um worker.

    Sai de graça do próprio RQ: o job_id é determinístico (`f2:<SIGLA>:<dia>`,
    `bfd:<SIGLA>:<dia>`, `adiado:<SIGLA>:<dia>`), então dá pra saber quem ainda
    tem alguém trabalhando nele sem tocar no banco. São dezenas de ids (14
    medidos em 24/08/2026, um por réplica), não milhares.

    Falha de leitura do registro devolve conjunto VAZIO de propósito: sem essa
    informação o watchdog volta ao comportamento antigo (mata o zumbi) — o
    diagnóstico não pode virar motivo pra deixar run travado pra sempre.
    """
    import django_rq
    from rq.registry import StartedJobRegistry

    vivos: set[tuple[str, str]] = set()
    for nome in ('djen_backfill', 'djen_ingestion'):
        try:
            fila = django_rq.get_queue(nome)
            for jid in StartedJobRegistry(queue=fila).get_job_ids():
                partes = jid.split(':')
                if len(partes) >= 3 and partes[0] in ('f2', 'bfd', 'adiado'):
                    vivos.add((partes[1], partes[2]))
        except Exception as exc:
            logger.warning('watchdog: não li os jobs vivos de `%s`: %s', nome, exc)
    return vivos


def _leitura_com_teto(fn, segundos: int = WATCHDOG_SQL_TIMEOUT_S):
    """Roda `fn()` sob `statement_timeout` de verdade.

    O pgbouncer roda em transaction-mode: um `SET statement_timeout` solto vai
    pra uma conexão e a query seguinte vai pra outra. `SET LOCAL` dentro de
    `transaction.atomic()` vale pra TODAS as queries da transação — é o padrão
    da casa (`dashboard/tasks.py::_with_timeout`).
    """
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [int(segundos * 1000)])
        return fn()


def _inicio_do_processo() -> float | None:
    """Epoch em que subiu o processo que tem estes módulos em memória.

    O RQ forka um work-horse por job, então `/proc/self` aqui nasceu segundos
    atrás e não serve de régua — quem carregou os módulos é o PAI (o worker).
    Lê os dois e fica com o mais antigo. Devolve None se o /proc não colaborar
    (o detector nunca pode derrubar o watchdog).
    """
    def _subiu_em(pid: int) -> float:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            # o comm vem entre parênteses e pode conter espaços: corta pelo
            # último ')'. Depois dele o campo [0] é o 3 do proc(5), então
            # starttime (campo 22) fica no índice 19.
            campos = f.read().rsplit(b')', 1)[1].split()
        ticks = float(campos[19])
        with open('/proc/stat') as f:
            btime = next(float(linha.split()[1]) for linha in f
                         if linha.startswith('btime '))
        return btime + ticks / os.sysconf('SC_CLK_TCK')

    import contextlib
    inicios = []
    for pid in (os.getpid(), os.getppid()):
        # /proc é diagnóstico, não contrato: se não der, o detector se cala.
        with contextlib.suppress(Exception):
            inicios.append(_subiu_em(pid))
    return min(inicios) if inicios else None


def _alerta_codigo_velho() -> dict:
    """O worker está executando o código que está no disco? MEÇA, não suponha.

    INCIDENTE 21/08/2026 — a falha mais cara do dia e a mais silenciosa.

    Os dois `worker_default` subiram em 14/08 17:40 e nunca mais reiniciaram. O
    bind-mount `.:/app` faz o `git pull` do host aparecer dentro do container,
    mas o processo Python já tem os módulos em memória: eles seguiram rodando o
    `djen/` de 14/08 por SETE DIAS, com run verde e log limpo.

    O que isso custou, medido por sonda dentro do próprio worker:

      * `ressuscitar_dias_de_recuperacao` (deploy de 19/08) NUNCA executou —
        `hasattr(djen.jobs, 'ressuscitar_dias_de_recuperacao') == False` no
        worker. **3.007 dias-tribunal** ficaram `failed` sem ninguém devolver;
      * `datajud/client.py` passou a importar `djen.proxies.sessao_rotativa`,
        que só existia no disco. O `import datajud.jobs` quebrava DENTRO do
        work-horse e o RQ traduzia isso pra `ValueError: Invalid attribute
        name` — **4.247 jobs do datajud mortos em ~30 ms cada**, o
        reabastecimento da fila datajud parado por 3 dias, e a única marca era
        o FailedJobRegistry.

    Um deploy que não recarrega é indistinguível de um deploy que funcionou.
    Agora não é: qualquer .py do projeto mais novo que o processo vira ERRO com
    nome e idade. Custa ~200 `os.stat` (medido <5ms).
    """
    inicio = _inicio_do_processo()
    if inicio is None:
        return {'checado': False}
    raiz = str(getattr(settings, 'BASE_DIR', '/app'))
    limite = inicio + CODIGO_VELHO_TOLERANCIA_S
    velhos = []
    mais_novo = 0.0
    for nome, mod in list(sys.modules.items()):
        arquivo = getattr(mod, '__file__', None)
        if not arquivo or not arquivo.startswith(raiz) or not arquivo.endswith('.py'):
            continue
        try:
            mtime = os.path.getmtime(arquivo)
        except OSError:
            continue
        mais_novo = max(mais_novo, mtime)
        if mtime > limite:
            velhos.append((nome, mtime))
    if not velhos:
        return {'checado': True, 'modulos_velhos': 0, 'mtime_mais_novo': mais_novo}
    velhos.sort(key=lambda x: -x[1])
    atraso_h = (max(m for _, m in velhos) - inicio) / 3600
    logger.error(
        'watchdog: WORKER RODANDO CÓDIGO VELHO — %d módulos do projeto mudaram '
        'no disco DEPOIS que este processo subiu (o mais novo há %.1fh): %s. '
        'O deploy não recarregou: `docker restart` no container deste worker. '
        'Foi assim que 3.007 dias de recuperação e 4.247 jobs do datajud '
        'morreram em silêncio em 08/2026.',
        len(velhos), atraso_h, ', '.join(n for n, _ in velhos[:8]),
    )
    return {'checado': True, 'modulos_velhos': len(velhos),
            'atraso_horas': round(atraso_h, 1),
            'mtime_mais_novo': mais_novo,
            'exemplos': [n for n, _ in velhos[:8]]}


def _epoca_utc(nascimento) -> float:
    """`birth_date` do RQ é UTC INGÊNUO — `.timestamp()` nele mente por 3h.

    MEDIDO em 24/08/2026, dentro do `voyager-web-1`:

        birth_date (repr)    : datetime.datetime(2026, 8, 24, 15, 13, 55, 806287)
        TZ do container      : ('-03', '-03')  offset -3.0
        birth_date.timestamp : 1787595235.8   -> 18:13:55 UTC
        como UTC .timestamp  : 1787584435.8   -> 15:13:55 UTC   (o certo)
        delta                : -10800 s

    O RQ grava `birth` com `utcformat(utcnow())` e devolve um datetime SEM
    tzinfo. `datetime.timestamp()` num ingênuo aplica o fuso LOCAL do processo,
    e o fuso do container é `America/Sao_Paulo` (UTC-3) — então todo worker era
    contado como nascido **3 horas no futuro**.

    O outro lado da comparação (`os.path.getmtime`) é época real. O resultado
    era um **ponto cego de 3h no alarme que existe pra pegar deploy sem
    restart**: qualquer frota com menos de 3h de atraso lia `velhos: 0`, e
    `atraso_horas` saía sempre 3h menor que a verdade. Foi o que aconteceu às
    15:33 de 24/08/2026, logo depois de reciclar 240 containers: `velhos: 0`
    com metade da frota ainda no código anterior ao `git pull` de 15:31.

    O erro é conservador na direção que menos ajuda: ele ESCONDE atraso, nunca
    inventa. Um alarme que só erra pra menos é pior que nenhum, porque dá
    confiança falsa — o defeito exato que o princípio nº 1 do projeto proíbe.
    """
    if nascimento.tzinfo is None:
        return nascimento.replace(tzinfo=UTC).timestamp()
    return nascimento.timestamp()


def _alerta_workers_velhos(mtime_mais_novo: float) -> dict:
    """O resto da FROTA também recarregou? Olha o `birth_date` de cada worker.

    `_alerta_codigo_velho` só enxerga o processo onde o watchdog está rodando —
    e o incidente de 21/08/2026 tinha DOIS grupos parados: os `worker_default`
    (que seguraram o watchdog e o datajud) e, medidos pela mesma sonda, os
    workers das filas `datajud` e `es_index`, que também estavam com o
    `djen.jobs` de antes de 19/08.

    O RQ já guarda o que falta: cada worker publica `birth_date` no Redis.
    Worker que nasceu ANTES do arquivo .py mais novo do projeto está, por
    construção, rodando código velho. Custa um SMEMBERS + um HGETALL por
    worker: medido em prod com 250 workers, o tique do watchdog subiu de 1,24s
    para 1,38-1,42s — ~0,15s, contra um orçamento de 90s.

    Ressalva honesta: o mtime é o do disco DESTE host. Hosts diferentes puxam o
    repo em momentos diferentes, então isto é indício forte, não prova — por
    isso o alerta manda conferir com `docker ps` antes de reiniciar nada.
    """
    try:
        import django_rq
        from rq import Worker
        conexao = django_rq.get_connection('default')
        frota = Worker.all(connection=conexao)
    except Exception:  # a frota é diagnóstico, não pode derrubar o tique
        return {'checado': False}
    if not mtime_mais_novo:
        return {'checado': False}
    limite = mtime_mais_novo - CODIGO_VELHO_TOLERANCIA_S
    velhos = [w for w in frota
              if w.birth_date and _epoca_utc(w.birth_date) < limite]
    if not velhos:
        return {'checado': True, 'total': len(frota), 'velhos': 0, 'atraso_horas': 0.0}

    idade_h = (mtime_mais_novo - min(_epoca_utc(w.birth_date) for w in velhos)) / 3600
    filas = collections.Counter(f for w in velhos for f in w.queue_names())
    detalhe = ', '.join(f'{f}={n}' for f, n in filas.most_common(8))
    if idade_h >= FROTA_ATRASO_ALERTA_H:
        logger.error(
            'watchdog: %d de %d workers da frota nasceram ANTES do código do '
            'disco, o mais antigo há %.0fh (teto de alerta %dh) — deploy sem '
            '`docker restart`. Filas: %s. Confira o `docker ps` antes de '
            'reiniciar: o mtime é o do disco DESTE host.',
            len(velhos), len(frota), idade_h, FROTA_ATRASO_ALERTA_H, detalhe,
        )
    else:
        logger.warning(
            'watchdog: %d de %d workers ainda não recarregaram (mais antigo há '
            '%.1fh). Filas: %s', len(velhos), len(frota), idade_h, detalhe,
        )
    return {'checado': True, 'total': len(frota), 'velhos': len(velhos),
            'atraso_horas': round(idade_h, 1)}


def _alerta_registry_de_falhas(fila) -> int:
    """Falha que ninguém olha não existe. Conta o cemitério e grita.

    Medido em 21/08/2026: 4.259 falhas na `default` acumuladas em 4 dias, 99,7%
    delas do mesmo defeito (código velho), e nada no sistema apontava. O ZCARD
    custa microssegundos — não há desculpa pra não olhar a cada 5 minutos.
    """
    try:
        from rq.registry import FailedJobRegistry
        n = len(FailedJobRegistry(queue=fila))
    except Exception:  # registro fora não derruba o tique
        return -1
    if n >= WATCHDOG_FALHAS_ALERTA:
        logger.error(
            'watchdog: FailedJobRegistry da fila `%s` com %d jobs (teto de '
            'alerta %d) — alguma coisa está morrendo em série e ninguém viu. '
            'Censo: `manage.py djen_censo_falhas %s`',
            fila.name, n, WATCHDOG_FALHAS_ALERTA, fila.name,
        )
    return n



def _matar_zumbis(agora) -> tuple[int, int]:
    """Marca `failed` o run travado — e SÓ ele. Devolve (matados, poupados).

    "status=running há mais de 1h ⇒ o worker crashou" é uma inferência, e ela é
    falsa justamente no dia que mais importa: o dia grande. MEDIDO em
    24/08/2026, entre os 170 runs de 1 dia que fecharam `success` em 12 h,
    **11 (6,5%) passaram de 60 min**, o pior deles um TJSP de **357 min**. Cada
    um foi marcado "worker crashou" com o worker trabalhando, e só voltou a
    `success` ao terminar. O estrago é duplo: o censo de falhas fica com
    fantasma (parte dos 640 "worker crashou" de 24/08 é isto) e o `erros` do
    run era SOBRESCRITO, apagando o teto de bytes e o drift que aquele dia
    tinha registrado — a evidência que a regra nº 2 manda guardar.

    O desempate não custa banco: o job_id é determinístico, então quem está com
    o dia na mão aparece no `StartedJobRegistry`. Com job vivo, o run ganha até
    `WATCHDOG_RUN_ZOMBIE_COM_JOB_S`; sem ele, nada muda.
    """
    zumbi_cutoff = agora - timedelta(seconds=WATCHDOG_RUN_ZOMBIE_SECONDS)
    vivos = _dias_com_job_vivo()
    candidatos = _leitura_com_teto(lambda: list(IngestionRun.objects.filter(
        status=IngestionRun.STATUS_RUNNING,
        finished_at__isnull=True,
        started_at__lt=zumbi_cutoff,
    ).values('pk', 'tribunal__sigla', 'janela_inicio', 'janela_fim', 'started_at')))
    teto_vivo = agora - timedelta(seconds=WATCHDOG_RUN_ZOMBIE_COM_JOB_S)
    alvos, poupados = [], 0
    for r in candidatos:
        trabalhando = (
            r['janela_inicio'] == r['janela_fim']
            and (r['tribunal__sigla'], r['janela_inicio'].isoformat()) in vivos
            and r['started_at'] > teto_vivo
        )
        if trabalhando:
            poupados += 1
            continue
        alvos.append(r['pk'])
    if not alvos:
        return 0, poupados
    # `erros` é APPEND, não substituição: o run pode já carregar o teto de
    # bytes ou o drift daquele dia, e sobrescrever apaga a evidência.
    matados = _leitura_com_teto(lambda: IngestionRun.objects.filter(
        pk__in=alvos,
    ).update(
        status=IngestionRun.STATUS_FAILED,
        finished_at=agora,
        erros=RawSQL("COALESCE(erros, '[]'::jsonb) || %s::jsonb",
                     [json.dumps([MSG_ZUMBI])]),
    ))
    return matados, poupados


@job('default', timeout=120)
def watchdog_ingestao() -> dict:
    """Garante que a ingestão/backfill estão progredindo. Roda em cron (5min).

    Heals:
      0. Grita se o worker está rodando código velho e se o cemitério de
         falhas (FailedJobRegistry) cresceu — ver `_alerta_codigo_velho`.
      1. Marca como `failed` IngestionRun com status=running e
         finished_at NULL há mais de 1h (worker crashou).
      2. Pra cada tribunal ativo com `backfill_concluido_em IS NULL`,
         re-enfileira `tick_backfill_retroativo` se não houver job dele em
         execução.
      3. Pra cada tribunal com backfill concluído mas sem IngestionRun
         success nas últimas 26h, re-enfileira `run_daily_ingestion`.
      4. Devolve pra fila os dias avulsos de recuperação que morreram órfãos.

    ORÇAMENTO (21/08/2026): o job tem `timeout=120` no RQ, e morrer assim
    apaga o relatório inteiro — foi o que aconteceu em 17/08, quando o banco
    entrou em contenção e o watchdog virou uma linha no registry. Agora cada
    etapa confere `WATCHDOG_ORCAMENTO_S` (90s) e, se o tempo acabar, o que
    ficou de fora sai como ERRO **com o número real**, nunca como `return`
    discreto (regra nº 2 do CLAUDE.md).
    """
    import django_rq

    fim_do_orcamento = time.monotonic() + WATCHDOG_ORCAMENTO_S
    agora = timezone.now()
    resultado: dict = {
        'zumbis_matados': 0, 're_backfill': [], 're_daily': [],
        'dias_recuperacao_reenfileirados': 0, 'etapas_puladas': [],
    }

    def _sobrou_tempo(etapa: str) -> bool:
        if time.monotonic() < fim_do_orcamento:
            return True
        resultado['etapas_puladas'].append(etapa)
        logger.error(
            'watchdog: orçamento de %ds estourado ANTES da etapa `%s` — o tique '
            'terminou pela metade. Etapas puladas: %s. Banco em contenção é a '
            'causa medida (17/08/2026); confira `pg_stat_activity`.',
            WATCHDOG_ORCAMENTO_S, etapa, resultado['etapas_puladas'],
        )
        return False

    default_q = django_rq.get_queue('default')

    # 0) O worker está rodando o código do disco? E o cemitério, cresceu?
    resultado['codigo'] = _alerta_codigo_velho()
    resultado['frota'] = _alerta_workers_velhos(
        resultado['codigo'].get('mtime_mais_novo', 0.0))
    resultado['falhas_no_registry'] = _alerta_registry_de_falhas(default_q)

    # 1) Zumbis — mas só os que são zumbi de verdade (ver `_matar_zumbis`).
    if _sobrou_tempo('zumbis'):
        n_zumbis, poupados = _matar_zumbis(agora)
        resultado['zumbis_matados'] = n_zumbis
        resultado['zumbis_poupados_com_job_vivo'] = poupados
        if n_zumbis or poupados:
            logger.warning('watchdog matou zumbis', extra={
                'n': n_zumbis, 'poupados_com_job_vivo': poupados})

    # 2 + 3) Re-enfileira backfill/daily
    if _sobrou_tempo('reenfileiramento'):
        backfill_q = django_rq.get_queue('djen_backfill')
        ingestion_q = django_rq.get_queue('djen_ingestion')
        backfill_jobs_args = _coletar_args(backfill_q)
        ingestion_jobs_args = _coletar_args(ingestion_q)
        default_jobs_args = _coletar_args(default_q)

        re_backfill = []
        re_daily = []
        daily_stale_cutoff = agora - timedelta(seconds=WATCHDOG_DAILY_STALE_SECONDS)
        hoje = date.today()

        for t in Tribunal.objects.filter(ativo=True):
            if t.backfill_concluido_em is None:
                # Usa tick_backfill_retroativo (1 dia por run) em vez de
                # run_backfill (30 dias por chunk). Ticks ficam na fila
                # `default` — verificamos lá pra evitar duplicar.
                if t.sigla not in default_jobs_args and t.sigla not in backfill_jobs_args:
                    tick_backfill_retroativo.delay(t.sigla)
                    re_backfill.append(t.sigla)
                continue
            # Backfill concluído — confere se daily rodou recentemente.
            # janela_fim__gte=hoje-1 distingue daily (cobre hoje) de backfill_dia
            # (dia histórico). Sem esse filtro, qualquer backfill_dia success
            # mascarava o daily quebrado — visto TRF1 sem ingestão por 3 dias
            # enquanto o tick_backfill rodava normalmente.
            ultima = _leitura_com_teto(lambda t=t: (
                IngestionRun.objects.filter(
                    tribunal=t, fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
                    janela_fim__gte=hoje - timedelta(days=1),
                ).order_by('-finished_at').first()
            ))
            if (ultima is None or
                ultima.finished_at is None or
                ultima.finished_at < daily_stale_cutoff):
                if t.sigla not in ingestion_jobs_args:
                    run_daily_ingestion.delay(t.sigla)
                    re_daily.append(t.sigla)
            if not _sobrou_tempo('reenfileiramento (parcial)'):
                break

        resultado['re_backfill'] = re_backfill
        resultado['re_daily'] = re_daily
        if re_backfill or re_daily:
            logger.warning('watchdog re-enfileirou jobs', extra={
                're_backfill': re_backfill, 're_daily': re_daily,
            })

    # 4) Dias avulsos de recuperação
    if _sobrou_tempo('recuperacao'):
        resumo: dict = {}
        resultado['dias_recuperacao_reenfileirados'] = (
            ressuscitar_dias_de_recuperacao(resumo=resumo))
        resultado['recuperacao'] = resumo

    # 5) O dia que fechou `success` fechou ÍNTEGRO? Ninguém perguntava isso
    #    automaticamente até 24/08/2026 — ver o bloco do gate de completude.
    if _sobrou_tempo('gate de completude'):
        conferencia: dict = {}
        resultado['gate_enfileirados'] = conferir_dias_fechados(resumo=conferencia)
        resultado['gate'] = conferencia

    return resultado


#: quantos dias o watchdog devolve por tique. Teto pra não inundar a fila num
#: incidente grande; atingi-lo é ERRO logado, nunca corte mudo.
RECUP_POR_TIQUE = 200

#: até onde olhar pra trás. Além disso é backfill histórico, não incidente.
RECUP_JANELA_DIAS = 7


def ressuscitar_dias_de_recuperacao(resumo: dict | None = None) -> int:
    """Devolve pra fila o dia de recuperação que falhou e ficou órfão.

    O watchdog antigo cuidava do fluxo diário e do backfill por tribunal, mas
    NÃO dos dias avulsos da recuperação do acervo (`reprocessar_janela`, os
    `f2:<SIGLA>:<dia>`). Um deles que falhasse por deadlock, 403 ou timeout
    simplesmente ficava `failed` para sempre — ninguém reenfileirava.

    Medido em 19/08/2026: 4 dias do TJSP parados assim, cada um valendo ~176 mil
    publicações. Não é perda de dado (o run registra o motivo), é perda de
    trabalho — e some no meio de milhares de runs.

    Não mexe em dia que já voltou sozinho: pula o que tem `success` mais recente
    que a falha, e o que já está na fila, agendado ou em execução.

    NÃO pula dia que já tem `success` antigo, e isso é deliberado — MEDIDO em
    21/08/2026: 2.929 dos 3.007 órfãos (97,4%) têm um `success` cobrindo o dia.
    Parece redundância e não é: a recuperação nacional existe exatamente pra
    refazer dia que fechou `success` batendo o cap de 10k
    (`djen_reprocessar_janelas_capped`: `paginas_lidas>=100` e
    `novas+duplicadas>=10000`). Filtrar por "já coberto" aqui apagaria 97% do
    trabalho de recuperação achando que estava economizando.

    Pelo mesmo motivo NÃO conta como ocupado um `bfd:<sigla>:<dia>` na fila
    (medido: 45 dos 3.007, 1,5%): `backfill_dia` faz skip em dia coberto, então
    ele NÃO refaz o dia capado — deixar o `f2:` entrar é o certo, e o `bfd:`
    duplicado custa um skip sem request.
    """
    import django_rq

    fila = django_rq.get_queue('djen_backfill')
    ocupados = set(fila.job_ids)
    try:
        from rq.registry import ScheduledJobRegistry, StartedJobRegistry
        ocupados |= set(ScheduledJobRegistry(queue=fila).get_job_ids())
        ocupados |= set(StartedJobRegistry(queue=fila).get_job_ids())
    except Exception:  # noqa: BLE001 — registro fora não pode derrubar o tique
        pass

    desde = timezone.now() - timedelta(days=RECUP_JANELA_DIAS)
    falhos = _leitura_com_teto(lambda: list(
        IngestionRun.objects
        .filter(fonte=FONTE, status=IngestionRun.STATUS_FAILED,
                started_at__gte=desde)
        .exclude(janela_inicio=None)
        .filter(janela_inicio=F('janela_fim'))
        .values('tribunal__sigla', 'janela_inicio')
        .annotate(ultima_falha=Max('started_at'))))

    # um SELECT só pra saber quem já foi refeito depois da falha
    sucessos = _leitura_com_teto(lambda: {
        (r['tribunal__sigla'], r['janela_inicio']): r['quando']
        for r in (IngestionRun.objects
                  .filter(fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
                          started_at__gte=desde)
                  .filter(janela_inicio=F('janela_fim'))
                  .values('tribunal__sigla', 'janela_inicio')
                  .annotate(quando=Max('started_at')))
    })

    candidatos = []
    for r in falhos:
        chave = (r['tribunal__sigla'], r['janela_inicio'])
        ok = sucessos.get(chave)
        if ok and ok > r['ultima_falha']:
            continue                                   # já foi refeito
        dia = r['janela_inicio'].isoformat()
        if f'f2:{chave[0]}:{dia}' in ocupados or f'adiado:{chave[0]}:{dia}' in ocupados:
            continue                                   # já está a caminho
        candidatos.append((chave[0], dia))

    # Teto de sanidade da fila. Não é corte mudo: se não cabe, o número real
    # sai como ERRO. A fila `djen_backfill` é buffer de dias inteiros do DJEN e
    # o que a API do CNJ enxerga é `réplicas x DJEN_PAGINAS_PARALELAS` (regra
    # dura: ≤ 64) — encher o buffer não acelera nada, só esconde o tamanho do
    # problema. Medido em 21/08/2026: fila com 592 jobs, teto 8.000.
    vagas = max(0, BACKFILL_WATERMARK - len(fila))
    cabe = min(RECUP_POR_TIQUE, vagas)

    devolvidos = 0
    for sigla, dia in candidatos[:cabe]:
        fila.enqueue('djen.jobs.reprocessar_janela', sigla, dia, dia,
                     job_id=f'f2:{sigla}:{dia}', job_timeout=21600)
        devolvidos += 1

    if resumo is not None:
        resumo.update({'orfaos': len(candidatos), 'devolvidos': devolvidos,
                       'sobraram': len(candidatos) - devolvidos,
                       'vagas_na_fila': vagas})

    if vagas < min(RECUP_POR_TIQUE, len(candidatos)):
        logger.error(
            'watchdog: fila djen_backfill sem vagas (%d jobs, teto %d) — %d dias '
            'de recuperação órfãos esperando e só %d couberam neste tique',
            len(fila), BACKFILL_WATERMARK, len(candidatos), devolvidos,
        )
    elif len(candidatos) > devolvidos:
        # O tempo abaixo é o de ENFILEIRAR, não o de coletar. Dizer só
        # "≈75 min pra drenar" num ERRO faz quem lê às 3h da manhã esperar 75
        # minutos, ver `recuperacao.orfaos` ainda alto e concluir que o
        # watchdog quebrou de novo. Os dois relógios têm que estar na MESMA
        # linha do alerta: nota de rodapé em outro arquivo não salva ninguém.
        logger.error(
            'watchdog: %d dias de recuperação órfãos, devolvi só %d neste tique — '
            'a esse ritmo são ~%.0f min só pra ENFILEIRAR os %d. A coleta é outro '
            'relógio: cada job é um dia inteiro de um tribunal, então `orfaos` no '
            'banco cai bem depois da fila encher. Esse número alto é sintoma',
            len(candidatos), devolvidos,
            (len(candidatos) / max(devolvidos, 1)) * 5, len(candidatos),
        )
    elif candidatos:
        logger.warning('watchdog devolveu %d dias de recuperação órfãos', len(candidatos))
    return devolvidos


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


BACKFILL_WATERMARK = 8000  # teto GLOBAL de sanidade da fila djen_backfill.
# 2026-07-05: 200 → 4000. Com 200 e a fila compartilhada por ~60 tribunais, o tick
# dos TRTs via SEMPRE fila>=200 e PULAVA — backfill trabalhista ficou starved.
# 2026-07-29: teto global sozinho não dá fairness — quem enche primeiro monopoliza
# a FIFO e os demais ficam com a fronteira recente congelada (TRTs com lag 20d).
# Agora o limite operacional é POR TRIBUNAL (abaixo); o global é só sanidade
# (27 tribunais × 200 = 5.400 < 8.000).
BACKFILL_WATERMARK_POR_TRIBUNAL = 200  # máx de jobs pendentes de UM tribunal
BACKFILL_BATCH = 100       # dias por tick
BACKFILL_JOB_PREFIX = 'bfd'  # job_id determinístico bfd:<sigla>:<dia> — permite
# contar pendentes por tribunal via get_job_ids() (LRANGE barato, sem fetch de
# payload) e deduplica re-enfileiramento entre ticks.


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
    from .client import DjenBusyError
    try:
        run = ingest_window(t, dia, dia)
    except DjenBusyError:
        # DJEN sobrecarregado (circuito aberto): ADIA sem falhar — não empilha no
        # FailedRegistry nem martela o servidor. O tick re-enfileira quando voltar.
        logger.warning('backfill_dia adiado %s %s (DJEN circuito aberto)', tribunal_sigla, dia_iso)
        return {'skip': 'circuito_aberto', 'dia': dia_iso}
    return {'run_id': run.pk, 'novas': run.movimentacoes_novas, 'dia': dia_iso}


@job('default', timeout=120)
def tick_backfill_retroativo(tribunal_sigla: str) -> dict:
    """Tick a cada 10min: loga progresso e alimenta djen_backfill com dias pendentes.

    Percorre de hoje para o passado. Para quando a fila já tem >= BACKFILL_WATERMARK
    jobs pendentes. Tanto TRF1 quanto TRF3 têm seu próprio tick independente.
    """
    import django_rq

    # DJEN sobrecarregado (circuito aberto) → NÃO alimenta a fila (para de martelar).
    from .client import circuit_is_open
    if circuit_is_open():
        return {'skip': 'djen circuito aberto (sobrecarregado)'}

    t = Tribunal.objects.filter(sigla=tribunal_sigla, ativo=True).first()
    if not t or not t.data_inicio_disponivel:
        return {'skip': 'tribunal inativo ou sem data_inicio_disponivel'}

    hoje = date.today()
    ini = t.data_inicio_disponivel
    total_dias = (hoje - ini).days + 1

    # `_dias_cobertos` foi onde 10 ticks morreram em 17/08/2026 — todos em 15
    # minutos, todos por `JobTimeoutException` de 120s, e como são 59
    # tribunais numa fila `default` com 2 workers, eles seguraram a FIFO e
    # levaram o `watchdog_ingestao` junto (head-of-line blocking). Não era
    # custo de algoritmo: medido em 21/08/2026 o pior tribunal leva 0,14s e os
    # 59 somam 1,40s. Era contenção do banco. Com teto de 20s a mesma
    # contenção devolve o worker 6x mais rápido, e ALTO.
    marcador = time.monotonic()
    try:
        cobertos = _leitura_com_teto(lambda: _dias_cobertos(t, ini, hoje))
    except OperationalError as e:
        logger.error(
            'backfill %s: leitura de cobertura estourou %ds (%.1fs de relógio) — '
            'banco em contenção, tique abortado sem enfileirar. %s',
            tribunal_sigla, WATCHDOG_SQL_TIMEOUT_S,
            time.monotonic() - marcador, e,
        )
        return {'skip': 'banco em contenção', 'segundos': round(time.monotonic() - marcador, 1)}
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
    job_ids = backfill_q.get_job_ids()
    q_len = len(job_ids)
    meu_prefixo = f'{BACKFILL_JOB_PREFIX}:{tribunal_sigla}:'
    meus = sum(1 for jid in job_ids if jid.startswith(meu_prefixo))
    if q_len >= BACKFILL_WATERMARK or meus >= BACKFILL_WATERMARK_POR_TRIBUNAL:
        logger.debug('backfill %s: fila global=%d meus=%d — aguardando',
                     tribunal_sigla, q_len, meus)
        return {'aguardando': True, 'fila': q_len, 'meus': meus,
                'pendentes': len(pendentes)}

    vagas = min(BACKFILL_WATERMARK - q_len,
                BACKFILL_WATERMARK_POR_TRIBUNAL - meus)
    a_enfileirar = pendentes[:min(BACKFILL_BATCH, vagas)]
    for dia in a_enfileirar:
        backfill_q.enqueue(backfill_dia, tribunal_sigla, str(dia),
                           job_id=f'{meu_prefixo}{dia}', job_timeout=3600)

    logger.info(
        'backfill %s: +%d dias enfileirados (fila era %d) · %.1f%% completo',
        tribunal_sigla, len(a_enfileirar), q_len, pct,
    )
    return {
        'enfileirados': len(a_enfileirar),
        'pendentes_restantes': len(pendentes) - len(a_enfileirar),
        'pct': round(pct, 1),
    }


def _run_tem_dados(v) -> bool:
    return bool(v['movimentacoes_novas'] or v['movimentacoes_duplicadas']
                or v['paginas_lidas'])


def _dia_coberto(tribunal: Tribunal, dia: date) -> bool:
    qs = IngestionRun.objects.filter(
        tribunal=tribunal, fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
        janela_inicio__lte=dia, janela_fim__gte=dia,
    ).values('movimentacoes_novas', 'movimentacoes_duplicadas', 'paginas_lidas')
    horizonte = date.today() - timedelta(days=tribunal.overlap_dias)
    if dia >= horizonte and dia.weekday() < 5:
        return any(_run_tem_dados(v) for v in qs)
    return qs.exists()


def _dias_cobertos(tribunal: Tribunal, ini: date, fim: date) -> set[date]:
    """Dias cobertos por IngestionRun success. Para DIAS ÚTEIS no horizonte
    recente (hoje - overlap_dias), exige run com dados; fim de semana e dias
    antigos: qualquer success (DJEN não publica sáb/dom)."""
    runs = list(IngestionRun.objects.filter(
        tribunal=tribunal, fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
        janela_inicio__lte=fim, janela_fim__gte=ini,
    ).values('janela_inicio', 'janela_fim', 'movimentacoes_novas',
             'movimentacoes_duplicadas', 'paginas_lidas'))
    horizonte = date.today() - timedelta(days=tribunal.overlap_dias)
    covered: set[date] = set()
    for run in runs:
        d = max(run['janela_inicio'], ini)
        end = min(run['janela_fim'], fim)
        com_dados = _run_tem_dados(run)
        while d <= end:
            if d < horizonte or d.weekday() >= 5 or com_dados:
                covered.add(d)
            d += timedelta(days=1)
    return covered


# ─────────────────────────── Gate de completude (o dia fechou ÍNTEGRO?) ──────
#
# `status='success'` nunca foi prova de completude. As três perdas do CLAUDE.md
# tinham run VERDE — o teto de 10 páginas serviu 43,6% do TJSP como sucesso por
# 17 meses. Até 24/08/2026 nada no sistema comparava, sozinho, o que o dia
# trouxe com o que a FONTE declara: a conferência existia só como comando
# manual (`djen_provar_dias`, `djen_auditar_cobertura`), e comando manual é
# coisa que ninguém roda.
#
# Este gate roda a cada tique do watchdog e é deliberadamente BARATO:
#
#   1. `count_window` (1 requisição, `itensPorPagina=1`, 0,66 s medido) diz
#      quantas publicações a DJEN declara no dia. Confrontado com
#      `novas + duplicadas` do run, ele pega corte mudo no MESMO dia em que
#      acontece;
#   2. o `count` tem TETO DE 10.000 (regra nº 3: número redondo é piso
#      disfarçado). Acima disso ele não decide nada, e dizer "10.000 = 10.000"
#      seria a mentira mais cara possível. Então o dia grande vai pro caminho
#      CARO — paginar até esgotar — em dose homeopática (`GATE_PAGINADOS_POR_TIQUE`),
#      porque cada um baixa o dia inteiro (o TJDFT 2026-08-21 são 822,6 MB).
#
# Divergência não vira exceção nem apaga nada: vira ERRO em `run.erros` (regra
# nº 2, teto/anomalia é alerta auditável) e linha de log com os dois números.
# Quem decide o que fazer é gente.

#: quanto tempo um dia conferido fica marcado (Redis). Maior que a janela de
#: recuperação de 7 dias: um dia refeito DEPOIS disso é run novo e merece
#: conferência nova.
GATE_TTL_S = 8 * 24 * 3600

#: dias conferidos pelo caminho barato por tique de watchdog (5 min). 20/tique
#: são 240 requisições/h contra as ~10 mil páginas/h que a frota já pede — 2,4%.
GATE_POR_TIQUE = 20

#: dias GRANDES (count no teto) conferidos por paginação por tique. É o caminho
#: caro: 1 dia do TJDFT = 59 requisições e 822,6 MB. 1 por tique = 12/h, teto de
#: 12 dias grandes auditados por hora.
GATE_PAGINADOS_POR_TIQUE = 1

#: teto interno do `count` da DJEN.
GATE_CAP = 10_000

#: tolerância do confronto barato. ZERO por decisão: o `count` e a paginação
#: leem a mesma janela na mesma API, então diferença é achado, não ruído
#: (regra nº 5). Um dia que a fonte reeditou depois da coleta aparece aqui — e
#: aparecer é o comportamento certo.
GATE_TOLERANCIA = 0


def _marca_gate(sigla: str, dia_iso: str) -> str:
    return f'djen:conferido:{sigla}:{dia_iso}'


@job('djen_audit', timeout=10800)
def conferir_dia_contra_fonte(tribunal_sigla: str, dia_iso: str,
                              paginar: bool = False) -> dict:
    """Confere UM dia fechado contra a fonte. Fila `djen_audit` de propósito.

    Não disputa worker com a ingestão (a fila `djen_audit` tem workers próprios,
    ociosos), e o que ela pede à API do CNJ é 1 requisição de 1 item — ou, no
    caminho `paginar`, o dia inteiro, que é caro e por isso vem racionado pelo
    watchdog.

    Devolve sempre um dict com os dois lados; nunca levanta por divergência.
    """
    from .client import DjenBusyError

    t = Tribunal.objects.get(sigla=tribunal_sigla)
    dia = date.fromisoformat(dia_iso)
    run = (IngestionRun.objects
           .filter(fonte=FONTE, tribunal=t, janela_inicio=dia, janela_fim=dia,
                   status=IngestionRun.STATUS_SUCCESS)
           .order_by('-finished_at').first())
    if run is None:
        # O dia deixou de estar fechado entre o enfileiramento e agora (refeito,
        # ou o watchdog o marcou zumbi). Não é erro: é conferência sem objeto.
        return {'skip': 'sem success de 1 dia', 'tribunal': tribunal_sigla, 'dia': dia_iso}

    lidas = run.movimentacoes_novas + run.movimentacoes_duplicadas
    client = DJENClient()
    try:
        declarado = client.count_window(t.sigla_djen, dia, dia)
    except DjenBusyError:
        # Circuito aberto é "volte mais tarde", não resultado. Sem marca no
        # Redis, o próximo tique reenfileira este dia.
        return {'skip': 'djen_circuito_aberto', 'tribunal': tribunal_sigla, 'dia': dia_iso}

    res = {'tribunal': tribunal_sigla, 'dia': dia_iso, 'run_id': run.pk,
           'run_lidas': lidas, 'fonte_count': declarado, 'paginado': False}

    if declarado >= GATE_CAP and paginar:
        # Caminho caro: paginação PRÓPRIA (a do `djen_provar_dias`), de fora do
        # `iter_pages`. Se a calibração de página do coletor voltar a cortar
        # mudo, a conferência não repete o erro junto — ela o denuncia.
        from djen.management.commands.djen_provar_dias import paginar_forca_bruta
        try:
            bruto = paginar_forca_bruta(client, t.sigla_djen, dia)
        except Exception as exc:
            logger.warning('gate: paginação de %s %s não concluiu: %s',
                           tribunal_sigla, dia_iso, str(exc)[:200])
            return res | {'skip': 'paginacao_falhou', 'detalhe': str(exc)[:200]}
        declarado = len(bruto['ids'])
        res |= {'paginado': True, 'fonte_paginada': declarado,
                'requisicoes': bruto['requisicoes']}
    elif declarado >= GATE_CAP:
        # 10.000 é PISO, não total: com este número não dá pra afirmar nem
        # negar integridade. Abster > chutar (regra nº 6).
        cache.set(_marca_gate(tribunal_sigla, dia_iso), 'cap', GATE_TTL_S)
        return res | {'veredito': 'indecidivel_no_teto_de_10k'}

    gap = declarado - lidas
    res['gap'] = gap
    cache.set(_marca_gate(tribunal_sigla, dia_iso), 'ok' if abs(gap) <= GATE_TOLERANCIA
              else 'gap', GATE_TTL_S)
    if abs(gap) <= GATE_TOLERANCIA:
        return res | {'veredito': 'integro'}

    # Achado. Vai pro run (auditável depois, sem depender de log rotacionado) e
    # pro log com os DOIS números na mesma linha.
    marca = {'erro': 'cobertura_divergente', 'fonte': declarado, 'run': lidas,
             'gap': gap, 'via': 'paginacao' if res['paginado'] else 'count',
             'quando': timezone.now().isoformat()}
    try:
        with transaction.atomic():
            atual = (IngestionRun.objects.select_for_update()
                     .filter(pk=run.pk).values_list('erros', flat=True).first())
            erros = list(atual or [])
            if not any(isinstance(e, dict) and e.get('erro') == 'cobertura_divergente'
                       for e in erros):
                erros.append(marca)
                IngestionRun.objects.filter(pk=run.pk).update(erros=erros)
    except OperationalError as exc:            # banco em contenção não pode
        logger.warning('gate: não gravei o achado no run %s: %s', run.pk, exc)
    logger.error(
        'GATE DE COMPLETUDE: %s %s fechou `success` com %d publicações mas a '
        'fonte declara %d (gap %+d, via %s, run=%d). Regra nº 5: diferença é '
        'achado, não ruído',
        tribunal_sigla, dia_iso, lidas, declarado, gap,
        'paginação' if res['paginado'] else 'count de 1 requisição', run.pk,
    )
    return res | {'veredito': 'divergente'}


#: até onde o gate olha pra trás por tique. Folga de 4x sobre o cron de 5 min:
#: um tique perdido (ou um watchdog que estourou o orçamento) não deixa dia
#: sem conferência.
GATE_JANELA_S = 20 * 60

#: teto de linhas lidas por tique. O `LIMIT` é da leitura, não da conferência:
#: quem passar disso volta no próximo tique com `finished_at` ainda na janela.
GATE_CANDIDATOS_MAX = 500

def conferir_dias_fechados(resumo: dict | None = None) -> int:
    """Enfileira a conferência dos dias que fecharam desde o último tique.

    Escolhe pela ponta certa: `finished_at` recente. Um dia já conferido carrega
    marca no Redis por 8 dias — perder a marca custa uma requisição repetida,
    não uma conferência perdida.
    """
    import django_rq

    desde = timezone.now() - timedelta(seconds=GATE_JANELA_S)
    fechados = _leitura_com_teto(lambda: list(
        IngestionRun.objects
        .filter(fonte=FONTE, status=IngestionRun.STATUS_SUCCESS,
                finished_at__gte=desde)
        .filter(janela_inicio=F('janela_fim'))
        .values('tribunal__sigla', 'janela_inicio',
                'movimentacoes_novas', 'movimentacoes_duplicadas')
        .order_by('-finished_at')[:GATE_CANDIDATOS_MAX]))

    fila = django_rq.get_queue('djen_audit')
    baratos = caros = 0
    for r in fechados:
        sigla, dia = r['tribunal__sigla'], r['janela_inicio'].isoformat()
        if cache.get(_marca_gate(sigla, dia)) is not None:
            continue
        grande = (r['movimentacoes_novas'] + r['movimentacoes_duplicadas']) >= GATE_CAP
        if grande:
            if caros >= GATE_PAGINADOS_POR_TIQUE:
                continue          # não é corte mudo: volta no próximo tique
            caros += 1
        elif baratos >= GATE_POR_TIQUE:
            continue
        else:
            baratos += 1
        fila.enqueue('djen.jobs.conferir_dia_contra_fonte', sigla, dia, grande,
                     job_id=f'gate:{sigla}:{dia}', job_timeout=10800)

    if resumo is not None:
        resumo.update({'candidatos': len(fechados), 'baratos': baratos, 'caros': caros})
    return baratos + caros


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


@job('default', timeout=5400)
def retreinar_jurimetria_job():
    """Re-treino semanal do modelo de sobrevivência DC→precatório (freshness).
    Roda o command retreinar_jurimetria (KM numpy, sem pandas/lifelines) → reescreve
    o artefato surv_strata.json, que o serving recarrega por mtime."""
    from django.core.management import call_command
    call_command('retreinar_jurimetria')
    return {'ok': True}
