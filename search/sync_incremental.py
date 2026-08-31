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

## ⚠️ WATERMARK EM DIA NÃO PROVA ÍNDICE COMPLETO (medido em 31/08/2026)

Medido no fim do backfill do sinal do TJSP: o Postgres fechou com **0** processos
sem `tem_sinal_precatorio`, e o índice ainda tinha **524.945** docs do TJSP com o
campo em `null` — 34,7% do lote. Ao mesmo tempo:

    watermark `sync_es:wm:proc_ts` .... 1 min 23 s de atraso  (em dia)
    fila `es_index` .................. 0                     (drenada)
    FailedJobRegistry(es_index) ...... 0                     (nada falhou)

Os três instrumentos de saúde deste módulo diziam "tudo certo" enquanto faltava
meio milhão de documentos. O doc builder não era o culpado: chamar
`indexar_processos_bulk` à mão nos mesmos pks gravou o valor em 5 ms.

**Consequência prática: não use idade de watermark como gate de completude.** O
gate honesto é contagem dos DOIS lados (`_count` no ES contra `count(*)` no
Postgres, no mesmo recorte) — foi ela que achou o buraco, e é ela que provou o
conserto (reenfileirar os pks levou o ES a zero).

Duas hipóteses ficaram sem prova, e a segunda é a que dá medo:

1. os jobs rodaram na janela em que `models.py` já tinha as colunas da migration
   0054 e o banco ainda não, e morreram sem deixar rastro no registry;
2. a chave `sync_es:wm:proc_ts` sumiu do Redis e o tique **re-ancorou em
   `agora`** — veja `sync_processos_atualizados`: `if wm is None: cache.set(agora)`
   e devolve `ancorou: True`. Isso é perda **silenciosa e definitiva** por
   desenho: tudo que foi escrito antes da re-ancoragem nunca mais é revisitado,
   porque o keyset só anda para frente. Vale para os três lados deste módulo.

Se alguém for endurecer isto, a re-ancoragem é o alvo: ancorar no topo é certo no
PRIMEIRO tique da vida do sistema e errado em toda perda de chave posterior.
"""
import logging
import os

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

from tribunals.models import Movimentacao, Process

logger = logging.getLogger('voyager.search.sync')

#: teto por tick. AJUSTÁVEL POR ENV — sem deploy, porque foi um número fixo que
#: transformou os dois lados de processo em vazamento (ver abaixo).
#:
#: MEDIDO em 26/08/2026, e é a mesma lição que o lado das movimentações já tinha
#: aprendido: teto fixo baixo não é freio, é vazamento.
#:   · `proc_novos` batia o teto em 6 de 6 ticks, 7,72 M de pks atrás do banco,
#:     COM A FILA `es_index` EM ZERO e 4 jobs rodando — os workers ociosos
#:     esperando o tique liberar. Amostra dos dois lados: 60 pks ACIMA do
#:     watermark → 57 AUSENTES do índice; 60 pks ABAIXO (controle) → 60
#:     presentes. A fronteira do índice ERA o watermark.
#:   · `proc_atualizados` era pior: DIVERGIA. A watermark andava ~45 s de relógio
#:     por tick de 600 s (idade 127,92 → 128,08 → 128,26 h em três ticks). Um
#:     teto que perde 13:1 para o relógio não é teto.
#:
#: `proc_novos` é limitado pelo `computar_sinal` INLINE (~7 ms/processo medido),
#: não pelo enqueue: 60.000 ≈ 7 min de tick. Subir muito além disso faz o tique
#: passar da janela e atrasar os outros dois syncs, que rodam depois dele.
#: `proc_atualizados` não computa nada — só consulta e enfileira —, então o teto
#: dele pode ser alto de verdade.
LIMITE_PROC_NOVOS = int(os.environ.get('SYNC_ES_LIMITE_PROC_NOVOS', 60_000))
LIMITE_PROC_ATUALIZADOS = int(os.environ.get('SYNC_ES_LIMITE_PROC_ATUALIZADOS', 200_000))
#: 50.000 por tick de 10 min = 300 mil/h, e a ingestão sob recuperação escreve
#: MUITO mais. O teto virou corte mudo: 179.490.613 publicações no banco e fora
#: do índice, com a fila `es_index` marcando zero. Agora o teto é alto e quem
#: segura o ritmo é a fila (FILA_ES_ALTA) — atingi-lo é ERRO registrado.
LIMITE_MOVS_NOVAS = int(os.environ.get('SYNC_ES_LIMITE_MOVS', 400_000))

#: acima disso o tick não empurra mais: o gargalo é o dreno, e engordar a fila
#: não aumenta vazão nenhuma — só esconde o atraso num número maior.
FILA_ES_ALTA = int(os.environ.get('SYNC_ES_FILA_ALTA', 150_000))

#: `computar_sinal` roda o MESMO `EXISTS` com regex sobre `movimentacao.texto`
#: que, com lote de 20.000 no TJSP, rodou **12,7 horas** numa transação só e
#: travou três jobs (incidente de 27/08/2026, ver `.ia/OPS.md`). O custo é por
#: MOVIMENTAÇÃO, não por processo, e varia ~100× entre tribunais — então o teto
#: aqui é de TEMPO, por bloco pequeno, e não um número de linhas.
BLOCO_SINAL = int(os.environ.get('SYNC_ES_BLOCO_SINAL', 2_000))
TIMEOUT_SINAL = os.environ.get('SYNC_ES_TIMEOUT_SINAL', '60s')

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
    """Enfileira indexação bulk na fila es_index. PROPAGA erro de propósito."""
    if not pks:
        return 0
    # PROPAGA — ver o comentário em `_enfileirar_movs`. Quem chama é que decide
    # o que fazer com o watermark, e a decisão certa é NÃO avançar.
    import django_rq
    q = django_rq.get_queue('es_index')
    for i in range(0, len(pks), CHUNK):
        q.enqueue('search.jobs.indexar_processos_bulk', pks[i:i + CHUNK])
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
    # PROPAGA. A versão anterior engolia a exceção e devolvia 0 — e quem chama
    # avançava o watermark logo em seguida, incondicionalmente. Ou seja: um
    # soluço do Redis no meio de um tick apagava do índice, PARA SEMPRE, todas
    # as publicações daquela leva: o keyset só anda pra frente e ninguém
    # revisita. Perda silenciosa com log de WARNING, que é o pior formato.
    import django_rq
    q = django_rq.get_queue('es_index')
    for i in range(0, len(pks), CHUNK):
        q.enqueue('search.jobs.indexar_movimentacoes_bulk', pks[i:i + CHUNK])
    return len(pks)


def computar_sinal(pks: list[int]) -> int:
    """Calcula `tem_sinal_precatorio` dos processos novos (os que estão NULL).

    Mesmo UPDATE do backfill Fase 0, mas restrito a uma lista pequena de pks
    (os que acabaram de entrar) — usa o índice de `processo_id` da movimentação
    via EXISTS, ~7ms por linha. Sem isto o mapa comercial volta a ter "sinal não
    processado" crescendo todo dia.
    """
    if not pks:
        return 0, []
    sql = ("UPDATE tribunals_process p SET tem_sinal_precatorio = EXISTS ("
           "SELECT 1 FROM tribunals_movimentacao m "
           "WHERE m.processo_id = p.id AND m.texto ~* %s) "
           "WHERE p.id = ANY(%s) AND p.tem_sinal_precatorio IS NULL")
    feitos, cobertos = 0, []
    for i in range(0, len(pks), BLOCO_SINAL):
        bloco = pks[i:i + BLOCO_SINAL]
        try:
            with transaction.atomic(), connection.cursor() as c:
                c.execute('SET LOCAL statement_timeout = %s', [TIMEOUT_SINAL])
                c.execute('SET LOCAL lock_timeout = %s', ['5s'])
                c.execute(sql, [PADRAO_SINAL, bloco])
                feitos += c.rowcount
            cobertos.extend(bloco)
        except Exception:
            # Bloco que estourou o teto NÃO entra em `cobertos`: quem chama
            # avança o watermark só até onde o sinal foi de fato computado.
            # Assim nenhum processo passa da fronteira sem sinal — que é a
            # invariante deste sync desde que ele existe.
            logger.error(
                'sync_es: computar_sinal estourou o teto de %s num bloco de %d '
                '(pks %s..%s). O tick segue com os %d que deram certo; o resto '
                'volta no próximo — a watermark NÃO passa por cima.',
                TIMEOUT_SINAL, len(bloco), bloco[0], bloco[-1], len(cobertos),
                exc_info=True)
            break
    return feitos, cobertos


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

    # Auto-freio pela fila, igual ao lado das movimentações: quem manda no ritmo
    # é o DRENO. Sem isto, subir o teto só troca "atraso no watermark" por
    # "atraso na fila" — o mesmo dado invisível, num número maior.
    fila = _fila_es()
    if fila > FILA_ES_ALTA:
        logger.info('sync_es: proc_novos — fila em %d (>%d), pulando', fila, FILA_ES_ALTA)
        return {'novos': 0, 'sinal': 0, 'wm': wm, 'fila': fila, 'pulou': True}

    pks = list(Process.objects.filter(id__gt=wm).order_by('id')
               .values_list('id', flat=True)[:LIMITE_PROC_NOVOS])
    if not pks:
        return {'novos': 0, 'sinal': 0, 'wm': wm}

    # ordem importa: computa o sinal ANTES de indexar, senão o doc vai pro ES
    # com tem_sinal_precatorio=NULL e só corrige no próximo toque do processo.
    n_sinal, pks = computar_sinal(pks)
    if not pks:
        # nem o primeiro bloco fechou: não enfileira e não anda. O ERROR já
        # saiu de dentro do `computar_sinal` com o tamanho do bloco.
        return {'novos': 0, 'sinal': 0, 'wm': wm, 'sinal_travou': True}
    try:
        n = _enfileirar_processos(pks)
    except Exception:      # mesma regra das movimentações:
        # a fila cair não derruba o tick, MAS o watermark não anda.
        logger.error('sync_es: enqueue de %d processos FALHOU — watermark '
                     'permanece em %s.', len(pks), wm, exc_info=True)
        return {'novos': 0, 'sinal': n_sinal, 'wm': wm, 'erro_enqueue': True}
    cache.set(_WM_PROC_ID, pks[-1], None)
    saida = {'novos': n, 'sinal': n_sinal, 'wm': pks[-1]}
    if len(pks) >= LIMITE_PROC_NOVOS:
        saida['atraso_ids'] = _alertar_teto_processos(
            'proc_novos', LIMITE_PROC_NOVOS, pks[-1])
    return saida


def _wm_par(wm):
    """Watermark como `(atualizado_em, id)`, aceitando o formato antigo.

    Até 27/08/2026 o watermark era só o instante. Quem estiver com o valor
    velho no cache entra aqui como `(instante, 0)` — não perde nada e passa a
    andar com desempate a partir do próximo tique.
    """
    if isinstance(wm, (tuple, list)) and len(wm) == 2:
        return wm[0], int(wm[1] or 0)
    return wm, 0


def sync_processos_atualizados() -> dict:
    """Processos alterados em lote (reclassificação/enriquecimento sem signal).

    Watermark por `atualizado_em` (auto_now) com sobreposição de 1 tick pra não
    perder escrita concorrente — reindex é idempotente por `_id`.
    """
    agora = timezone.now()
    wm = cache.get(_WM_PROC_TS)
    if wm is None:
        cache.set(_WM_PROC_TS, (agora, 0), None)
        return {'atualizados': 0, 'wm': agora.isoformat(), 'ancorou': True}

    fila = _fila_es()
    if fila > FILA_ES_ALTA:
        logger.info('sync_es: proc_atualizados — fila em %d (>%d), pulando', fila, FILA_ES_ALTA)
        return {'atualizados': 0, 'wm': str(wm), 'fila': fila, 'pulou': True}

    # KEYSET POR `(atualizado_em, id)`, e o `id` não é enfeite. Com `> wm` puro e
    # `ORDER BY atualizado_em` sem desempate, quando o teto cai no MEIO de um
    # grupo de linhas que compartilham o mesmo instante — e escrita em lote grava
    # milhares com o mesmo `now()` — o watermark vira aquele instante e o `>` do
    # tique seguinte exclui TODAS elas, inclusive as que nunca foram lidas.
    # Some sem erro: a consulta é válida, o log é limpo, o número é redondo.
    wm_ts, wm_id = _wm_par(wm)
    pks = list(Process.objects.raw(
        'SELECT id FROM tribunals_process '
        'WHERE (atualizado_em, id) > (%s, %s) '
        'ORDER BY atualizado_em, id LIMIT %s',
        [wm_ts, wm_id, LIMITE_PROC_ATUALIZADOS]))
    pks = [p.id for p in pks]
    try:
        n = _enfileirar_processos(pks)
    except Exception:      # ver acima: watermark NÃO anda no erro.
        logger.error('sync_es: enqueue de %d processos atualizados FALHOU — '
                     'watermark permanece em %s.', len(pks), wm, exc_info=True)
        return {'atualizados': 0, 'wm': str(wm), 'erro_enqueue': True}
    # avança só até onde leu: se bateu o teto, o próximo tick continua daqui.
    if len(pks) < LIMITE_PROC_ATUALIZADOS:
        cache.set(_WM_PROC_TS, (agora, 0), None)
    elif pks:
        ultimo = (Process.objects.filter(id=pks[-1])
                  .values_list('atualizado_em', flat=True).first())
        if ultimo:
            # guarda o PAR: sem o id, o próximo tique pularia o resto do grupo
            # que divide este mesmo instante.
            cache.set(_WM_PROC_TS, (ultimo, pks[-1]), None)
    saida = {'atualizados': n, 'wm': str(cache.get(_WM_PROC_TS))}
    if len(pks) >= LIMITE_PROC_ATUALIZADOS:
        # Aqui a watermark é um INSTANTE, então o número que importa não é
        # quantos ids faltam — é quanto TEMPO de escrita ainda não chegou à
        # busca. Medido em 25/08/2026: watermark parada em 19/08 (6 dias) e
        # avançando ~30-40 s de relógio por tick de 10 min, ou seja perdendo
        # ~17:1. Um teto que não converge não é teto, é vazamento.
        atual, _ = _wm_par(cache.get(_WM_PROC_TS))
        idade_h = None
        if atual is not None:
            try:
                idade_h = round((timezone.now() - atual).total_seconds() / 3600.0, 2)
            except TypeError:      # watermark serializada de outro jeito
                idade_h = None
        saida['idade_wm_h'] = idade_h
        logger.error(
            'sync_es: proc_atualizados ATINGIU o teto de %d por tick — a '
            'watermark está em %s (%s h atrás) e anda menos que o relógio. '
            'Toda escrita em lote posterior a isso está FORA da busca.',
            LIMITE_PROC_ATUALIZADOS, atual,
            'idade não medida' if idade_h is None else idade_h)
    return saida


def _topo_process() -> int | None:
    """`max(id)` de `tribunals_process`, ou `None` se não der pra medir.

    Existe só para o ALERTA de teto saber dizer o número REAL de quanto ficou
    para trás. Por isso ele **nunca** pode derrubar o tick nem segurar escrita
    (regra nº 7): tem teto de espera próprio, e falha em ABSTENÇÃO — `None` é
    "não consegui medir", que a mensagem diz com todas as letras, em vez de um
    zero que leria como "não falta nada" (regra nº 6).
    """
    from django.db import transaction
    try:
        with transaction.atomic(), connection.cursor() as c:
            c.execute('SET LOCAL statement_timeout = %s', ['5s'])
            c.execute('SELECT max(id) FROM tribunals_process')
            return c.fetchone()[0] or 0
    except Exception:
        logger.warning('sync_es: não consegui ler max(id) para medir o atraso.')
        return None


def _alertar_teto_processos(rotulo: str, limite: int, ultimo_pk: int) -> int | None:
    """Teto atingido é ERRO REGISTRADO com o número real, nunca `return` discreto.

    Regra nº 2 do CLAUDE.md. O lado das movimentações já fazia isto desde que o
    teto de 50 mil virou corte mudo e deixou **179.490.613 publicações fora do
    índice com a fila `es_index` marcando zero** — "run verde, log limpo, número
    redondo", os três ao mesmo tempo. Os dois lados de PROCESSO ficaram de fora
    da lição até 25/08/2026, quando a medição mostrou o teto sendo batido em
    **6 de 6 ticks seguidos** e a watermark 7,84 milhões de pks atrás do banco.

    Devolve o atraso em ids, ou `None` se não deu para medir.
    """
    topo = _topo_process()
    if topo is None:
        logger.error(
            'sync_es: %s ATINGIU o teto de %d por tick (watermark=%d) e não deu '
            'pra medir o quanto falta. O resto NÃO está na busca.',
            rotulo, limite, ultimo_pk)
        return None
    atraso = topo - ultimo_pk
    logger.error(
        'sync_es: %s ATINGIU o teto de %d por tick — faltam ~%d ids pra alcançar '
        'o banco (watermark=%d, topo=%d). Esse resto NÃO está na busca.',
        rotulo, limite, atraso, ultimo_pk, topo)
    return atraso


def _fila_es() -> int:
    """Tamanho da fila `es_index`. -1 se não der pra ler (nunca derruba o tick)."""
    try:
        import django_rq
        return django_rq.get_queue('es_index').count
    except Exception:
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
    try:
        n = _enfileirar_movs(pks)
    except Exception:      # fila fora não derruba o tick...
        # ...mas o WATERMARK NÃO ANDA. Avançar aqui seria dar por indexadas
        # publicações que ninguém enfileirou, e o keyset nunca volta atrás.
        logger.error('sync_es: enqueue de %d movimentações FALHOU — watermark '
                     'permanece em %s; o próximo tick refaz esta leva.',
                     len(pks), wm, exc_info=True)
        return {'movs': 0, 'wm': wm, 'erro_enqueue': True}
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
        except Exception:
            # ERROR, não WARNING. Bloco que falha é watermark que NÃO ANDA, e
            # keyset só anda pra frente: o que ficou para trás só volta se
            # alguém for buscar. "Perda silenciosa com log de WARNING é o pior
            # formato" — é o que este módulo já aprendeu em `_enfileirar_movs`,
            # e o mesmo vale para o bloco inteiro. Em 25/08/2026 o
            # `proc_atualizados` morreu com `LockNotAvailable` e saiu como
            # WARNING no meio de um tick que, no resto, parecia saudável.
            logger.error('sync_es: %s FALHOU — a watermark dele NÃO andou neste '
                         'tick.', nome, exc_info=True)
            out[nome] = {'erro': True}
    if any(v.get('novos') or v.get('atualizados') or v.get('movs') for v in out.values()
           if isinstance(v, dict)):
        logger.info('sync_es tick: %s', out)
    return out
