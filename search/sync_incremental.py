"""Sync incremental Postgres → Elasticsearch (o ES nunca mais envelhece).

O write-through por signal cobre `.save()` unitário, mas TRÊS fluxos de volume
escrevem em lote e NÃO disparam signal — logo, nunca chegavam ao ES sozinhos:

  1. INGESTÃO DJEN — `Process.objects.bulk_create(...)` / `Movimentacao...`
     → processo/movimentação novos ficavam fora do ES até um reindex manual.
  2. RECLASSIFICAÇÃO em lote — `.update(classificacao=...)`
     → o QA mediu ES 35k vs PG 47,6k confirmados (26% de defasagem).
  3. `tem_sinal_precatorio` de processo NOVO — nada computa na ingestão; só o
     comando de backfill. Sem isto, a fatia "sinal não processado" volta a
     crescer todo dia depois do backfill (o mapa comercial volta a mentir).

Este módulo fecha os três, por KEYSET/WATERMARK (barato, só índice), com
watermarks no cache, teto por tick e kill-switch. Roda no scheduler
(`tick_sync_es_incremental`, ver djen/scheduler.py) — INLINE e curto.

    from search.sync_incremental import tick_sync_es_incremental
    tick_sync_es_incremental()          # 1 tick (idempotente)

Kill-switch: `cache.set('sync_es:off', True)` desliga sem deploy.
"""
import logging

from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from tribunals.models import Movimentacao, Process

logger = logging.getLogger('voyager.search.sync')

#: teto por tick — mantém o tick curto e o DB fora de pileup. A ingestão real
#: fica MUITO abaixo disso por ciclo de 10min; a folga é pra recuperar atraso.
LIMITE_PROC_NOVOS = 20_000
LIMITE_PROC_ATUALIZADOS = 10_000
#: 50.000 por tick de 10 min = 300 mil/h, e a ingestão sob recuperação escreve
#: MUITO mais. O teto virou corte mudo: 179.490.613 publicações no banco e fora
#: do índice, com a fila `es_index` marcando zero. Agora o teto é alto e quem
#: segura o ritmo é a fila (FILA_ES_ALTA) — atingi-lo é ERRO registrado.
LIMITE_MOVS_NOVAS = 400_000

#: acima disso o tick não empurra mais: o gargalo é o dreno, e engordar a fila
#: não aumenta vazão nenhuma — só esconde o atraso num número maior.
FILA_ES_ALTA = 150_000

#: tamanho do bulk enfileirado (o job `indexar_processos_bulk` faz 1 _bulk).
CHUNK = 500

#: chaves de watermark (cache; sobrevivem a restart do scheduler)
_WM_PROC_ID = 'sync_es:wm:proc_id'
_WM_PROC_TS = 'sync_es:wm:proc_ts'
_WM_MOV_ID = 'sync_es:wm:mov_id'
_OFF = 'sync_es:off'

#: mesmo padrão do backfill Fase 0 (tribunals/management/commands/
#: backfill_sinal_precatorio.py) — sinal de precatório no texto das movs.
PADRAO_SINAL = r'precat[óo]rio|of[íi]cio requisit[óo]rio|\mrpv\M|requisi[çc][ãa]o de pagamento'


def _enfileirar_processos(pks: list[int]) -> int:
    """Enfileira indexação bulk na fila es_index. Best-effort (nunca propaga)."""
    if not pks:
        return 0
    try:
        import django_rq
        q = django_rq.get_queue('es_index')
        for i in range(0, len(pks), CHUNK):
            q.enqueue('search.jobs.indexar_processos_bulk', pks[i:i + CHUNK])
    except Exception:  # noqa: BLE001 — fila fora não pode derrubar o tick
        logger.warning('enqueue es_index falhou (%d pks)', len(pks), exc_info=True)
        return 0
    return len(pks)


def _enfileirar_movs(pks: list[int]) -> int:
    """Enfileira em LOTE, igual aos processos.

    Enfileirava um job por publicação. Com 50.000 movs de teto por tick, isso
    são 50.000 jobs a cada 10 minutos (300 mil/h) contra um dreno medido de
    ~200 mil/h — a fila só podia crescer, e cresceu: 1,68 milhão em 18/08/2026,
    com o índice ~178 milhões de docs atrás do Postgres. Em lote de 500 o mesmo
    tick vira 100 jobs.
    """
    if not pks:
        return 0
    try:
        import django_rq
        q = django_rq.get_queue('es_index')
        for i in range(0, len(pks), CHUNK):
            q.enqueue('search.jobs.indexar_movimentacoes_bulk', pks[i:i + CHUNK])
    except Exception:  # noqa: BLE001
        logger.warning('enqueue movs falhou (%d pks)', len(pks), exc_info=True)
        return 0
    return len(pks)


def computar_sinal(pks: list[int]) -> int:
    """Calcula `tem_sinal_precatorio` dos processos novos (os que estão NULL).

    Mesmo UPDATE do backfill Fase 0, mas restrito a uma lista pequena de pks
    (os que acabaram de entrar) — usa o índice de `processo_id` da movimentação
    via EXISTS, ~7ms por linha. Sem isto o mapa comercial volta a ter "sinal não
    processado" crescendo todo dia.
    """
    if not pks:
        return 0
    sql = ("UPDATE tribunals_process p SET tem_sinal_precatorio = EXISTS ("
           "SELECT 1 FROM tribunals_movimentacao m "
           "WHERE m.processo_id = p.id AND m.texto ~* %s) "
           "WHERE p.id = ANY(%s) AND p.tem_sinal_precatorio IS NULL")
    with connection.cursor() as c:
        c.execute(sql, [PADRAO_SINAL, pks])
        return c.rowcount


def sync_processos_novos() -> dict:
    """Processos criados desde o último tick (keyset por pk crescente)."""
    wm = cache.get(_WM_PROC_ID)
    if wm is None:
        # 1º tick: ancora no topo (não re-processa a base inteira — pra isso
        # existe o reindexar_processos). Daqui pra frente segue o keyset.
        wm = Process.objects.order_by('-id').values_list('id', flat=True).first() or 0
        cache.set(_WM_PROC_ID, wm, None)
        logger.info('sync_es: watermark de processo ancorado em id=%s', wm)
        return {'novos': 0, 'sinal': 0, 'wm': wm, 'ancorou': True}

    pks = list(Process.objects.filter(id__gt=wm).order_by('id')
               .values_list('id', flat=True)[:LIMITE_PROC_NOVOS])
    if not pks:
        return {'novos': 0, 'sinal': 0, 'wm': wm}

    # ordem importa: computa o sinal ANTES de indexar, senão o doc vai pro ES
    # com tem_sinal_precatorio=NULL e só corrige no próximo toque do processo.
    n_sinal = computar_sinal(pks)
    n = _enfileirar_processos(pks)
    cache.set(_WM_PROC_ID, pks[-1], None)
    return {'novos': n, 'sinal': n_sinal, 'wm': pks[-1]}


def sync_processos_atualizados() -> dict:
    """Processos alterados em lote (reclassificação/enriquecimento sem signal).

    Watermark por `atualizado_em` (auto_now) com sobreposição de 1 tick pra não
    perder escrita concorrente — reindex é idempotente por `_id`.
    """
    agora = timezone.now()
    wm = cache.get(_WM_PROC_TS)
    if wm is None:
        cache.set(_WM_PROC_TS, agora, None)
        return {'atualizados': 0, 'wm': agora.isoformat(), 'ancorou': True}

    pks = list(Process.objects.filter(atualizado_em__gt=wm).order_by('atualizado_em')
               .values_list('id', flat=True)[:LIMITE_PROC_ATUALIZADOS])
    n = _enfileirar_processos(pks)
    # avança só até onde leu: se bateu o teto, o próximo tick continua daqui.
    if len(pks) < LIMITE_PROC_ATUALIZADOS:
        cache.set(_WM_PROC_TS, agora, None)
    elif pks:
        ultimo = (Process.objects.filter(id=pks[-1])
                  .values_list('atualizado_em', flat=True).first())
        if ultimo:
            cache.set(_WM_PROC_TS, ultimo, None)
    return {'atualizados': n, 'wm': str(cache.get(_WM_PROC_TS))}


def _fila_es() -> int:
    """Tamanho da fila `es_index`. -1 se não der pra ler (nunca derruba o tick)."""
    try:
        import django_rq
        return django_rq.get_queue('es_index').count
    except Exception:  # noqa: BLE001
        return -1


def sync_movimentacoes_novas() -> dict:
    """Movimentações novas da ingestão (bulk_create não dispara signal).

    O TETO DAQUI JÁ FOI UM CORTE MUDO. Com `LIMITE_MOVS_NOVAS = 50.000` e tick
    de 10 minutos, o máximo que este sync consegue indexar são 300 mil/h. Durante
    a recuperação do acervo a ingestão escreve muito mais que isso, e o watermark
    simplesmente não acompanha: o resto fica ESPERANDO, não perdido — mas fora do
    alcance da busca, que dá no mesmo pra quem procura.

    Medido em 18/08/2026 por amostragem dos dois lados: abaixo do watermark, 9 de
    9 janelas de id conferem EXATAMENTE com o Postgres; acima dele havia
    **179.490.613 publicações fora do índice**. E o sintoma era uma fila `es_index`
    zerada — "run verde, log limpo, número redondo", os três da tabela do
    CLAUDE.md ao mesmo tempo.

    Agora: teto alto (a indexação em lote aguenta), auto-freio pela fila em vez
    de por um número fixo, e o atraso vira ERRO registrado em vez de silêncio.
    """
    wm = cache.get(_WM_MOV_ID)
    if wm is None:
        wm = Movimentacao.objects.order_by('-id').values_list('id', flat=True).first() or 0
        cache.set(_WM_MOV_ID, wm, None)
        logger.info('sync_es: watermark de movimentação ancorado em id=%s', wm)
        return {'movs': 0, 'wm': wm, 'ancorou': True}

    # Auto-freio: quem manda no ritmo é o dreno, não um número fixo. Se a fila
    # já está cheia, empurrar mais só aumenta o tamanho da fila — não a vazão.
    fila = _fila_es()
    if fila > FILA_ES_ALTA:
        logger.info('sync_es: fila em %d (>%d), pulando o tick', fila, FILA_ES_ALTA)
        return {'movs': 0, 'wm': wm, 'fila': fila, 'pulou': True}

    pks = list(Movimentacao.objects.filter(id__gt=wm).order_by('id')
               .values_list('id', flat=True)[:LIMITE_MOVS_NOVAS])
    if not pks:
        return {'movs': 0, 'wm': wm}
    n = _enfileirar_movs(pks)
    cache.set(_WM_MOV_ID, pks[-1], None)

    saida = {'movs': n, 'wm': pks[-1], 'fila': fila}
    # Bateu o teto: existe mais coisa esperando do que coube neste tick. Isso é
    # ALERTA, nunca um `return` discreto — é assim que 179 milhões ficaram fora
    # do índice sem ninguém ver.
    if len(pks) >= LIMITE_MOVS_NOVAS:
        topo = Movimentacao.objects.order_by('-id').values_list('id', flat=True).first() or 0
        atraso = topo - pks[-1]
        saida['atraso_ids'] = atraso
        logger.error(
            'sync_es: tick ATINGIU o teto de %d — ainda faltam ~%d ids pra alcançar '
            'o banco (watermark=%d, topo=%d). A busca NÃO enxerga esse resto.',
            LIMITE_MOVS_NOVAS, atraso, pks[-1], topo,
        )
    return saida


def tick_sync_es_incremental() -> dict:
    """1 tick do sync incremental. INLINE no scheduler, nunca propaga exceção."""
    if cache.get(_OFF):
        return {'off': True}
    out: dict = {}
    for nome, fn in (('proc_novos', sync_processos_novos),
                     ('proc_atualizados', sync_processos_atualizados),
                     ('movs_novas', sync_movimentacoes_novas)):
        try:
            out[nome] = fn()
        except Exception:  # noqa: BLE001 — um bloco falho não mata o tick
            logger.warning('sync_es: %s falhou', nome, exc_info=True)
            out[nome] = {'erro': True}
    if any(v.get('novos') or v.get('atualizados') or v.get('movs') for v in out.values()
           if isinstance(v, dict)):
        logger.info('sync_es tick: %s', out)
    return out
