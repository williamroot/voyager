import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Iterator

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import IngestionRun, Movimentacao, Process, Tribunal, ano_cnj_from_numero

from .client import DjenBusyError, DJENClient, circuit_is_open
from .parser import parse_item

logger = logging.getLogger('voyager.djen.ingestion')

BATCH_SIZE = 500

UF_OABS = [
    'AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS', 'MT',
    'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN', 'RO', 'RR', 'RS', 'SC', 'SE', 'SP', 'TO',
]

# Tribunais com enricher implementado. Process novo nesses tribunais é
# auto-enfileirado pra enriquecimento via consulta pública.
TRIBUNAIS_COM_ENRICHER = {'TRF1', 'TRF3', 'TRF5', 'TJMG', 'TJMA', 'TJSP', 'TJAL', 'TJDFT',
                          # recon 2026-06-29: consulta pública aberta (sem captcha/login)
                          'TJCE', 'TJAP', 'TJPE', 'TJRJ', 'TJRO', 'TJAC', 'TJMT', 'TJPA'}

# Tribunais que o JURISCOPE/Falcon de fato lê (precatório vira lead lá) — o ALVO DE
# VALOR do enriquecimento. Medido no Falcon (datamodel_process, 2026-08-06): 2,43M
# precatórios conhecidos. Enriquecer POR VALOR (não por contagem) é obrigatório: o
# Datajud tem 1 APIKey pública compartilhada (~100 rpm, teto permanente). Ver
# .ia/ENRICHMENT.md "Plano de cobertura por valor".
TRIBUNAIS_JURISCOPE = {'TJSP', 'TRF1', 'TRF3', 'TRF4', 'TJMG', 'TRF6', 'TRF5',
                       'TJAL', 'TRF2', 'TJMA'}


def ingest_processo(processo, client: DJENClient | None = None) -> dict:
    """Sincroniza movimentações de UM processo via DJEN.

    Não cria IngestionRun — esses são reservados pro backfill janela-de-dia
    via `ingest_window`. Auditoria por-processo fica em
    `Process.ultima_sinc_djen_em` + `Movimentacao.inserido_em` (do bulk insert).

    Reusa `_process_page` com run=None: bulk_create(ignore_conflicts=True)
    continua idempotente, só não atualiza contadores de run.
    """
    client = client or DJENClient()
    tribunal = processo.tribunal
    cnjs_tocados: set[str] = set()
    novas = 0
    duplicadas = 0
    paginas = 0
    for items in client.iter_pages_processo(tribunal.sigla_djen, processo.numero_cnj):
        n_novas, n_dup = _process_page(items, tribunal, None, cnjs_tocados)
        novas += n_novas
        duplicadas += n_dup
        paginas += 1
    if cnjs_tocados:
        _atualizar_resumo_processos(tribunal, cnjs_tocados)
    now_ts = timezone.now()
    Process.objects.filter(pk=processo.pk).update(
        data_enriquecimento_djen=now_ts,
        ultima_sinc_djen_em=now_ts,
    )

    return {
        'cnj': processo.numero_cnj,
        'novas': novas,
        'duplicadas': duplicadas,
        'paginas': paginas,
    }


def chunk_dates(start: date, end: date, days: int = 30) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


# O `count` da DJEN satura em 10.000 — mas isso é o `max_result_window` do
# Elasticsearch por baixo, ou seja um PISO ("tem pelo menos 10 mil"), NÃO um
# teto de paginação. Continua servindo de gatilho de "dia grande"; não serve
# mais de justificativa pra fatiar.
DJEN_HARD_CAP = 10_000

#: Fatiar o dia por `ufOab` (27 requisições) foi a resposta à crença de que a
#: API capava em 10.000. Medição de 18/08/2026 nos 59 tribunais derrubou a
#: crença — `iter_pages` pagina até esgotar (TJSP: 262 páginas, 261.076 itens,
#: zero duplicata) — e mostrou que o fatiamento tem defeito PRÓPRIO, que o
#: conserto do teto de páginas não fecha:
#:
#:   1. é CEGO a publicação sem advogado com OAB. Provado na unidade em cinco
#:      tribunais: TJPE 2025-08-13 tem 2.853 itens sem OAB e são exatamente as
#:      2.853 que faltavam; TJMA 2026-08-13, 911 = 911; TJRN, STJ e TJPB idem.
#:      São 2% a 10% de TODO dia acima de 10.000;
#:   2. custa 27× mais requisição à API do CNJ (que tem rate-limit de 20/s);
#:   3. um erro de fatia vira `success` (limiar de 14/27) e o dia fica marcado
#:      como coberto pra sempre — TJDFT gravou 4.005 de 12.479 assim.
#:
#: Fica no código como escotilha (`DJEN_ESTRATEGIA_UF=1`) porque é caminho
#: testado em produção, mas o padrão é a paginação flat.
ESTRATEGIA_UF = getattr(settings, 'DJEN_ESTRATEGIA_UF', False)

#: Teto de SANIDADE por fatia de UF — não é o limite da API, é proteção contra
#: laço infinito se a API entrar em loop. Alto de propósito: o teto anterior era
#: 10 páginas e cortava o TJSP pela metade em silêncio. Atingi-lo é ERRO logado,
#: não caminho normal. Medido: `ufOab=SP` no TJSP passa de 10 páginas todo dia.
MAX_PAGINAS_UF = 500

#: quantas páginas ficam em voo entre os fetchers e quem grava, na estratégia
#: por UF. O teto de memória de verdade é o de BYTES (`DJEN_BYTES_EM_VOO`,
#: aplicado em `DJENClient.iter_pages`): "8 páginas ≈ 30 MB" era uma conta que
#: assumia publicação de ~3 KB, e o TJDFT publica de 56 KB (medido 24/08/2026).
PAGINAS_EM_VOO = 8

#: De quantos em quantos CNJs a ingestão FECHA o lote (resumo dos processos +
#: auto-enqueue) e esquece o que já tratou.
#:
#: Antes, `cnjs_tocados` acumulava o dia inteiro e só era usado no fim — um
#: `set` O(dia) vivo do começo ao fim, e um `numero_cnj__in=<todos>` de 260 mil
#: parâmetros no TJSP. Ambos são acumulação, que é o que a regra nº 1 do
#: CLAUDE.md proíbe, e ambos vivem no processo que o OOM killer matou 342 vezes
#: em agosto/2026.
#:
#: 5.000 CNJs ≈ 600 KB e é a mesma ordem de grandeza do `IN` que um dia pequeno
#: já fazia — nenhuma query nova fica maior do que já era. O preço é que um
#: processo que apareça em DOIS lotes do mesmo dia é reenfileirado duas vezes;
#: enriquecimento é idempotente e o custo é um job repetido, não um dado errado.
CNJS_POR_LOTE = 5_000

_TAM_PAGINA_SO = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096


def _rss_mb() -> float:
    """RSS do processo, em MB, lido do /proc — sem dependência e sem syscall cara.

    Devolve 0.0 onde /proc não existe (macOS de dev): a vigilância de memória é
    diagnóstico, não pode ser motivo de falha da ingestão.
    """
    try:
        with open('/proc/self/statm') as fh:
            return int(fh.read().split()[1]) * _TAM_PAGINA_SO / 1048576
    except (OSError, IndexError, ValueError):
        return 0.0


def _vigiar_memoria(run: IngestionRun | None) -> None:
    """Regra nº 2 do CLAUDE.md aplicada à memória: passar do teto é ERRO
    registrado COM O NÚMERO REAL, e não o SIGKILL calado do OOM killer.

    Foi assim que 342 dias morreram até 24/08/2026: o work-horse sumia com
    `waitpid returned 9`, o `IngestionRun` ficava `running` para sempre e só
    uma hora depois o watchdog escrevia "worker crashou" — sem UM número que
    dissesse quanta memória, em que página, com que tribunal. O alerta é
    gravado na hora, porque um alerta que espera o fim do run não sobrevive à
    morte do processo que ele descreve.
    """
    teto = int(getattr(settings, 'DJEN_RSS_ALERTA_MB', 700))
    if run is None or teto <= 0:
        return
    rss = _rss_mb()
    if rss < teto:
        return
    for e in run.erros:
        if e.get('erro') == 'memoria_acima_do_alerta':
            e['rss_mb'] = max(e['rss_mb'], round(rss, 1))
            e['paginas_lidas'] = run.paginas_lidas
            return
    run.erros.append({'erro': 'memoria_acima_do_alerta', 'rss_mb': round(rss, 1),
                      'teto_mb': teto, 'paginas_lidas': run.paginas_lidas})
    logger.error(
        'ingest_window run_id=%s: RSS %.0f MB acima do alerta de %d MB na página %d '
        '— o processo está a caminho do OOM killer (mem_limit do worker: 1 GiB)',
        run.pk, rss, teto, run.paginas_lidas,
    )
    try:
        run.save(update_fields=['erros'])
    except Exception:   # o alerta não pode derrubar a coleta
        logger.warning('falha ao gravar alerta de memória no run %s', run.pk)


def _drenar_alertas(client: DJENClient, run: IngestionRun | None) -> None:
    """Passa pro run os avisos que o coletor não tem como registrar sozinho —
    ele não conhece o `IngestionRun`. Ver `DJENClient.alertas`.

    Grava NA HORA, e é chamado a cada página, não só no fim. O aviso que mais
    importa (`orcamento_memoria_no_piso`) é justamente o que diz que o pico vai
    passar do orçamento — esperar o fim do run pra gravá-lo é perdê-lo
    exatamente no caso em que ele seria lido.
    """
    if run is None or not getattr(client, 'alertas', None):
        return
    novos = 0
    for aviso in client.alertas:
        if aviso not in run.erros:
            run.erros.append(aviso)
            novos += 1
    client.alertas.clear()
    if novos and run.pk:
        try:
            run.save(update_fields=['erros'])
        except Exception:   # o alerta não pode derrubar a coleta
            logger.warning('falha ao gravar alerta do coletor no run %s', run.pk)


def _fechar_lote(tribunal: Tribunal, cnjs: set[str],
                 com_novidade: set[str] | None = None,
                 run: IngestionRun | None = None) -> None:
    """Fecha um lote de CNJs: resumo dos processos + auto-enqueue. Chamado a
    cada `CNJS_POR_LOTE` em vez de uma vez no fim do dia — ver a constante.

    O auto-enqueue continua vendo TODOS os CNJs tocados (ele tem corte próprio
    de 24 h); só a ESCRITA do resumo é restrita a quem mudou."""
    if not cnjs:
        return
    _atualizar_resumo_processos(tribunal, cnjs, com_novidade, run)
    _enfileirar_todos_enrichments(tribunal, cnjs)


def ingest_window(tribunal: Tribunal, data_inicio: date, data_fim: date,
                  client: DJENClient | None = None,
                  forcar_uf_em_1d: bool = False) -> IngestionRun:
    """Ingere uma janela contínua de dias para um tribunal. 1 IngestionRun por chamada.

    Estratégia adaptativa ao CAP de 10k:
    - Janela > 1 dia que bate o CAP: divide em 2 metades e re-processa recursivamente,
      propagando `forcar_uf_em_1d=True` pros filhos.
    - Janela de 1 dia que bate o CAP: proba count antes e usa ufOab (27 UFs) como filtro,
      garantindo cobertura completa. Enfileira job de auditoria por órgão.

    `forcar_uf_em_1d`: quando True e a janela for de 1 dia, pula o probe
    `count_only` e vai direto pra `_ingest_day_por_uf`. Usado pelos filhos
    do split adaptativo — um count baixo nesse contexto é quase certo
    falso negativo (WAF/proxy ruim retornando payload truncado), e ignorá-lo
    foi causa documentada de perda de dados (~10k/dia/tribunal).
    """
    client = client or DJENClient()

    # Probe antecipado: dia único com CAP ia pra estratégia UF. Ver ESTRATEGIA_UF.
    if data_inicio == data_fim and ESTRATEGIA_UF:
        if forcar_uf_em_1d:
            logger.info('djen single-day forçando UF strategy (split de janela capada)', extra={
                'tribunal': tribunal.sigla, 'dia': str(data_inicio),
            })
            return _ingest_day_por_uf(tribunal, data_inicio, client)
        count = client.count_only(tribunal.sigla_djen, data_inicio, data_fim)
        if count >= DJEN_HARD_CAP:
            logger.warning('djen single-day cap detected via probe, using UF strategy', extra={
                'tribunal': tribunal.sigla, 'dia': str(data_inicio), 'count': count,
            })
            return _ingest_day_por_uf(tribunal, data_inicio, client)

    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=data_inicio, janela_fim=data_fim,
    )
    cnjs_tocados: set[str] = set()
    cnjs_com_novidade: set[str] = set()
    t0 = time.monotonic()
    logger.info('ingest_window inicio %s %s→%s run_id=%d', tribunal.sigla, data_inicio, data_fim, run.pk)
    try:
        # Circuito aberto não é tentativa: é "nem chegamos a tentar". Sem isto o
        # run fica `failed` e polui a métrica de saúde — em 19/08 foram 4.153
        # runs marcados como falha que na verdade eram jobs ADIADOS, e isso
        # esconde as falhas de verdade no meio do ruído. O run é apagado porque
        # nada foi lido: não há o que auditar nele.
        if circuit_is_open():
            run.delete()
            raise DjenBusyError('DJEN circuito aberto — dia adiado, nada coletado')
        # FLUXO, não acumulação (regra nº 1). Cada página é gravada e esquecida;
        # os CNJs tocados saem em lotes de CNJS_POR_LOTE em vez de esperar o dia
        # inteiro na memória. Nada aqui pode crescer com o tamanho do dia.
        for items in client.iter_pages(tribunal.sigla_djen, data_inicio, data_fim):
            _process_page(items, tribunal, run, cnjs_tocados, cnjs_com_novidade)
            del items
            _vigiar_memoria(run)
            _drenar_alertas(client, run)
            if len(cnjs_tocados) >= CNJS_POR_LOTE:
                _fechar_lote(tribunal, cnjs_tocados, cnjs_com_novidade, run)
                cnjs_tocados = set()
                cnjs_com_novidade = set()
        _fechar_lote(tribunal, cnjs_tocados, cnjs_com_novidade, run)
        _drenar_alertas(client, run)
        run.status = IngestionRun.STATUS_SUCCESS
    except DjenBusyError:
        raise                       # já apagou o run acima; o job adia e volta
    except Exception as exc:
        run.status = IngestionRun.STATUS_FAILED
        _drenar_alertas(client, run)
        run.erros.append({'erro': 'execucao', 'detalhe': str(exc)[:500]})
        logger.exception('ingestion_run failed', extra={'run_id': run.pk, 'tribunal': tribunal.sigla})
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'erros', 'finished_at'])
        raise
    finally:
        duracao = int(time.monotonic() - t0)
        if run.status == IngestionRun.STATUS_SUCCESS:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])
            logger.info(
                'ingest_window fim %s %s→%s → novas=%d dup=%d pgs=%d %ds run_id=%d',
                tribunal.sigla, data_inicio, data_fim,
                run.movimentacoes_novas, run.movimentacoes_duplicadas,
                run.paginas_lidas, duracao, run.pk,
            )

    # Split adaptativo: se bateu o cap em janela > 1 dia, divide em 2 metades.
    # Filhos sempre forçam UF strategy quando chegarem em 1 dia — count_only
    # pode mentir (WAF) e o caminho normal de paginação re-cap aria.
    if (run.movimentacoes_novas + run.movimentacoes_duplicadas) >= DJEN_HARD_CAP \
            and run.paginas_lidas >= 10 \
            and (data_fim - data_inicio).days >= 1:
        meio = data_inicio + (data_fim - data_inicio) // 2
        logger.warning('djen window hit cap, splitting', extra={
            'tribunal': tribunal.sigla, 'inicio': str(data_inicio), 'fim': str(data_fim),
            'novas': run.movimentacoes_novas, 'duplicadas': run.movimentacoes_duplicadas,
        })
        ingest_window(tribunal, data_inicio, meio, client=client, forcar_uf_em_1d=True)
        ingest_window(tribunal, meio + timedelta(days=1), data_fim, client=client,
                      forcar_uf_em_1d=True)

    return run


def _ingest_day_por_uf(tribunal: Tribunal, dia: date, client: DJENClient) -> IngestionRun:
    """Ingere um dia com >10k movs subdividindo por ufOab (27 UFs em paralelo).

    Nenhum UF isolado atinge o CAP, então a soma garante cobertura completa.
    Itens são deduplicados pelo campo 'id' antes do INSERT.
    Após ingestão, enfileira job de auditoria por órgão na fila djen_audit.
    """
    run = IngestionRun.objects.create(
        tribunal=tribunal, status=IngestionRun.STATUS_RUNNING,
        janela_inicio=dia, janela_fim=dia,
    )
    cnjs_tocados: set[str] = set()
    cnjs_com_novidade: set[str] = set()

    def _iter_paginas_uf(uf: str):
        """Pagina uma fatia de UF ATÉ ESGOTAR.

        ⚠️ Aqui morava a maior perda de acervo do sistema. A linha era
        `for pagina in range(1, 11)` — teto de 10 páginas × 1000 = **10.000
        itens por UF**, com o comentário "nenhum UF chega perto". A premissa era
        falsa: no TJSP, `ufOab=SP` passa MUITO disso, e a fatia era decapitada
        em silêncio (run `success`, zero alerta).

        Medido em 17/08/2026, TJSP no dia 2025-07-21:
            Postgres (o que coletamos) ......... 117.215 publicações
            API paginando até esgotar .......... 208.000+ (piso, a sonda parou)
        Ou seja: **43,6% do dia perdido**, todo dia, desde que o TJSP entrou no
        DJEN. Conferido fatia a fatia: `ufOab=SP` e `ufOab=MG` devolvem dado na
        página 11 — exatamente a que o teto cortava.

        O laço agora termina onde tem que terminar: quando a página volta
        incompleta. O teto de sanidade continua existindo (uma API em loop não
        pode nos prender), mas é ALTO e, se for atingido, vira alerta — nunca
        mais um corte mudo.
        """
        pagina = 1
        vistos = 0
        # ⚠️ ESTA ESCOTILHA NÃO TEM O ORÇAMENTO DE BYTES do caminho flat (ver
        # `DJENClient.iter_pages`). Aqui a página continua fixa em 1000 itens,
        # e são 27 fatias em voo ao mesmo tempo: num tribunal que publica 56 KB
        # por item (TJDFT, medido 24/08/2026) isso é da ordem de 1,5 GB. O
        # caminho flat é o padrão desde 18/08 e foi ele que recebeu o conserto;
        # retrofitar a calibração aqui reescreveria os quatro testes de
        # regressão que guardam a lição das 10 páginas, num caminho DESLIGADO.
        # Se alguém ligar `DJEN_ESTRATEGIA_UF` num tribunal pesado, é aqui que
        # o OOM volta — o alerta de RSS abaixo grita antes, mas não impede.
        from .client import _peso_por_item, itens_por_pagina
        peso_item = 0
        while pagina <= MAX_PAGINAS_UF:
            payload = client._fetch(
                tribunal.sigla_djen, dia, dia,
                pagina=pagina, itens_por_pagina=1000,
                extra_params={'ufOab': uf},
            )
            page = payload.get('items') or []
            del payload
            if page:
                novo_peso = _peso_por_item(page)
                if novo_peso > peso_item:
                    peso_item = novo_peso
                    # Não muda a paginação — só DIZ, com o número medido, que a
                    # escotilha está pedindo mais memória do que o orçamento.
                    itens_por_pagina(tribunal.sigla_djen, peso_item, len(UF_OABS),
                                     1000, getattr(client, 'alertas', None))
                yield page                 # entrega e ESQUECE: quem grava é o consumidor
            vistos += len(page)
            if len(page) < 1000:
                return
            pagina += 1
        # Chegou no teto de sanidade: a fatia PODE estar truncada. Isso é
        # exceção e tem que gritar — foi o silêncio que custou 43,6% do TJSP.
        logger.error(
            'djen UF %s/%s dia=%s ATINGIU o teto de %d páginas (%d itens) — '
            'a fatia pode estar truncada',
            tribunal.sigla, uf, dia, MAX_PAGINAS_UF, vistos,
        )
        run.erros.append({'erro': 'uf_teto_paginas', 'uf': uf,
                          'paginas': MAX_PAGINAS_UF, 'itens': vistos})

    try:
        # STREAMING, não acumulação. A versão anterior juntava TODOS os itens
        # das 27 UFs numa lista e só então gravava. Com o teto de 10 páginas
        # isso cabia (~117k itens); sem o teto, o TJSP traz 208k+ publicações
        # com o TEXTO inteiro dentro — os workers morreram com signal 9 (OOM),
        # e o watchdog registrou "worker crashou" em 8 dos 30 dias do backfill.
        #
        # Agora as páginas viajam por uma fila LIMITADA: os fetchers produzem,
        # a thread principal grava e descarta. O pico de memória passa a ser o
        # tamanho da fila, não o tamanho do dia — e o dedupe continua garantido
        # pelo uniq (tribunal, external_id) do banco, que é onde ele sempre
        # esteve de verdade.
        import queue as _queue

        paginas = _queue.Queue(maxsize=PAGINAS_EM_VOO)
        uf_erros: list[str] = []
        total_itens = 0
        FIM = object()

        def _produz(uf: str):
            try:
                for page in _iter_paginas_uf(uf):
                    paginas.put(page)
            except Exception as exc:
                uf_erros.append(uf)
                run.erros.append({'erro': 'uf_fetch', 'uf': uf, 'detalhe': str(exc)[:200]})
                logger.warning('falha ao coletar uf=%s: %s', uf, str(exc)[:120])

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_produz, uf) for uf in UF_OABS]
            pool.submit(lambda: ([f.result() for f in futs], paginas.put(FIM)))
            while True:
                page = paginas.get()
                if page is FIM:
                    break
                total_itens += len(page)
                for i in range(0, len(page), BATCH_SIZE):
                    _process_page(page[i:i + BATCH_SIZE], tribunal, run,
                                  cnjs_tocados, cnjs_com_novidade)
                del page
                _vigiar_memoria(run)
                if len(cnjs_tocados) >= CNJS_POR_LOTE:
                    _fechar_lote(tribunal, cnjs_tocados, cnjs_com_novidade, run)
                    cnjs_tocados = set()
                    cnjs_com_novidade = set()

        if uf_erros:
            logger.warning('djen UF strategy: %d UFs falharam: %s', len(uf_erros), uf_erros)

        logger.info(
            'djen UF strategy %s %s → %d itens em fluxo (%d UFs, %d erros)',
            tribunal.sigla, dia, total_itens, len(UF_OABS), len(uf_erros),
        )

        # QUALQUER fatia perdida falha o run. O limiar anterior era 14 de 27, e
        # contar fatias trata a fatia da capital igual à do estado que responde
        # 40 itens — mas a distribuição é tudo menos uniforme: no TJDFT a fatia
        # DF é 77% do dia, no TRT5 a BA é 92% do tribunal. Os dois gravaram
        # `success` com o grosso do dia faltando (TJDFT: 4.005 de 12.479), e
        # `_dia_coberto` pula dia com run success — ou seja, o dia truncado
        # ficava marcado como coberto PARA SEMPRE. Perder 1 de 27 é perder o
        # dia até prova em contrário, e a prova custa uma re-coleta barata.
        if uf_erros:
            raise RuntimeError(
                f'UF strategy: {len(uf_erros)}/{len(UF_OABS)} fatias falharam '
                f'({", ".join(sorted(uf_erros)[:8])}) — dia NÃO pode contar como coberto'
            )

        _fechar_lote(tribunal, cnjs_tocados, cnjs_com_novidade, run)
        _drenar_alertas(client, run)
        run.status = IngestionRun.STATUS_SUCCESS
    except Exception as exc:
        run.status = IngestionRun.STATUS_FAILED
        _drenar_alertas(client, run)
        run.erros.append({'erro': 'execucao_uf', 'detalhe': str(exc)[:500]})
        logger.exception('_ingest_day_por_uf failed', extra={'run_id': run.pk, 'tribunal': tribunal.sigla})
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'erros', 'finished_at'])
        raise
    finally:
        if run.status == IngestionRun.STATUS_SUCCESS:
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])

    # Enfileira auditoria de cobertura por órgão de forma assíncrona.
    try:
        from .jobs import audit_cobertura_dia
        audit_cobertura_dia.delay(tribunal.sigla, str(dia))
    except Exception as exc:
        logger.warning('falha ao enfileirar audit_cobertura_dia: %s', exc)

    return run


def _process_page(items: list[dict], tribunal: Tribunal, run: IngestionRun | None,
                  cnjs_tocados: set[str],
                  cnjs_com_novidade: set[str] | None = None) -> tuple[int, int]:
    """Processa uma página da DJEN. Retorna (novas, duplicadas) pra caller
    agregar quando rodando sem IngestionRun (ingest_processo).

    Quando `run` é não-None (caminho ingest_window/backfill_dia), atualiza
    os contadores no run direto. Atomicidade garante consistência da métrica.

    `cnjs_com_novidade` (opcional) recebe só os CNJs que ganharam movimentação
    NOVA nesta página. É o que separa "o processo apareceu de novo no diário"
    de "o processo mudou": em 24 h de produção (24/08/2026) 13.215.471 das
    18.809.848 publicações processadas eram DUPLICADAS (70,3%), e 158 dos 444
    runs com dado (35,6%) fecharam com ZERO publicação nova. Recalcular o
    resumo desses processos reescreve a linha pra gravar exatamente o mesmo
    valor — é a escrita que abre a janela de contenção do deadlock.
    """
    parsed = []
    for item in items:
        p = parse_item(item, tribunal, run)
        if p is not None:
            parsed.append(p)

    if not parsed:
        if run is not None:
            run.paginas_lidas += 1
            run.save(update_fields=['paginas_lidas', 'erros'])
        return (0, 0)

    cnjs_pagina = {p.cnj for p in parsed}
    ext_ids_pagina = [p.external_id for p in parsed]

    with transaction.atomic():
        existentes_cnj = dict(
            Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina)
            .values_list('numero_cnj', 'pk')
        )
        # ORDENADO por CNJ de propósito. `cnjs_pagina` é set, então a ordem de
        # inserção variava por processo — e dois workers inserindo CNJs
        # sobrepostos em ordens diferentes travam um no outro no índice único.
        # Medido em 18/08/2026, com 8 workers de backfill no mesmo tribunal:
        #   deadlock detected ... while inserting index tuple in "tribunals_process"
        # O run falha (certo, não é perda silenciosa), mas joga fora a coleta do
        # dia inteiro. Ordem total igual em todo mundo = sem ciclo de espera.
        novos_processos = [
            Process(tribunal=tribunal, numero_cnj=c, ano_cnj=ano_cnj_from_numero(c))
            for c in sorted(cnjs_pagina - existentes_cnj.keys())
        ]
        if novos_processos:
            Process.objects.bulk_create(novos_processos, ignore_conflicts=True, batch_size=BATCH_SIZE)
            existentes_cnj = dict(
                Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs_pagina)
                .values_list('numero_cnj', 'pk')
            )

        ja_existem_extids = set(
            Movimentacao.objects.filter(tribunal=tribunal, external_id__in=ext_ids_pagina)
            .values_list('external_id', flat=True)
        )

        # Catálogo de classes — upsert batch dos pares (codigo, nome) da página.
        # Usa nome do DJEN só se a classe ainda não existe (Process já populou
        # nomes melhores via PJe consulta pública).
        from tribunals.models import ClasseJudicial
        classes_pagina = {
            (p.codigo_classe, p.nome_classe)
            for p in parsed if p.codigo_classe and p.nome_classe
        }
        if classes_pagina:
            ClasseJudicial.objects.bulk_create(
                [ClasseJudicial(codigo=c, nome=n) for c, n in classes_pagina],
                ignore_conflicts=True,
                batch_size=BATCH_SIZE,
            )

        movs = []
        for p in parsed:
            kwargs = p.to_movimentacao_kwargs()
            if p.codigo_classe:
                kwargs['classe_id'] = p.codigo_classe
            movs.append(Movimentacao(
                processo_id=existentes_cnj[p.cnj],
                tribunal=tribunal,
                **kwargs,
            ))
        # mesma razão do sort acima: o único (tribunal, external_id) também
        # deadlocka se dois workers inserirem em ordens diferentes
        movs.sort(key=lambda m: m.external_id)
        Movimentacao.objects.bulk_create(movs, ignore_conflicts=True, batch_size=BATCH_SIZE)

        # Métrica aproximada: TOCTOU possível entre SELECT e bulk_create. Documentado:
        # workers concorrentes podem dupli-contar como "novos" o mesmo external_id;
        # `ignore_conflicts` garante que dados não sejam duplicados, só a métrica.
        novos_count = len(ext_ids_pagina) - len(ja_existem_extids)
        if cnjs_com_novidade is not None:
            # Novidade é por external_id, não por processo: o mesmo processo
            # pode vir com 3 publicações das quais 2 já estavam no banco.
            cnjs_com_novidade.update(
                p.cnj for p in parsed if p.external_id not in ja_existem_extids
            )
        if run is not None:
            run.movimentacoes_novas += novos_count
            run.movimentacoes_duplicadas += len(ja_existem_extids)
            run.processos_novos += len(novos_processos)
            run.paginas_lidas += 1
            run.save(update_fields=[
                'movimentacoes_novas', 'movimentacoes_duplicadas',
                'processos_novos', 'paginas_lidas', 'erros',
            ])
        cnjs_tocados.update(cnjs_pagina)
        return (novos_count, len(ja_existem_extids))


#: Campos do resumo escritos por `_flush_resumo`. Fixos de propósito: a lista
#: de campos entra na ordem das colunas do UPDATE e não pode variar por lote.
CAMPOS_RESUMO = ['primeira_movimentacao_em', 'ultima_movimentacao_em',
                 'total_movimentacoes', 'data_enriquecimento_djen']

#: Quantas linhas de `tribunals_process` cada UPDATE trava por vez. Cada lote é
#: uma transação PRÓPRIA — o `bulk_update` do Django envolve TODOS os batches
#: num único `atomic()`, e é essa transação longa que fecha o ciclo de espera.
LOTE_UPDATE_PROCESS = 500

#: Deadlock é erro TRANSITÓRIO: a transação que perdeu não fez nada de errado,
#: só chegou depois. Retentar é correto — mas com teto e registrado no run
#: (regra nº 2 do CLAUDE.md), nunca um `except: pass` que esconde regressão.
DEADLOCK_TENTATIVAS = 5
DEADLOCK_BACKOFF_S = 0.25


def _e_deadlock(exc: Exception) -> bool:
    """SQLSTATE 40P01. Lê o `sqlstate` do erro original do psycopg (o texto é
    fallback pra driver/versão que não o exponha)."""
    causa = getattr(exc, '__cause__', None)
    if getattr(causa, 'sqlstate', None) == '40P01':
        return True
    return 'deadlock detected' in str(exc).lower()


def _registrar_deadlock(run: IngestionRun | None, tribunal: Tribunal,
                        tentativas: int, linhas: int, venceu: bool) -> None:
    """Grava NO RUN quantos deadlocks aconteceram e quantas tentativas custaram.

    Um retry que ninguém conta é um defeito que volta invisível: se a ordem
    determinística sumir num refactor, o sistema segue "verde" pagando latência
    e ninguém descobre. O número real fica no `erros` do run, na hora — o
    work-horse pode morrer antes do fim.
    """
    logger.warning(
        'deadlock em tribunals_process (%s): %d tentativa(s) para %d linhas, %s',
        tribunal.sigla, tentativas, linhas,
        'venceu' if venceu else 'ESGOTOU o teto',
    )
    if run is None:
        return
    for e in run.erros:
        if e.get('erro') == 'deadlock_em_tribunals_process':
            e['ocorrencias'] += 1
            e['tentativas_max'] = max(e['tentativas_max'], tentativas)
            e['linhas_max'] = max(e['linhas_max'], linhas)
            if not venceu:
                e['esgotou_tentativas'] = True
            break
    else:
        run.erros.append({
            'erro': 'deadlock_em_tribunals_process', 'ocorrencias': 1,
            'tentativas_max': tentativas, 'linhas_max': linhas,
            'teto_tentativas': DEADLOCK_TENTATIVAS,
            'esgotou_tentativas': not venceu, 'tribunal': tribunal.sigla,
        })
    try:
        run.save(update_fields=['erros'])
    except Exception:   # o registro do alerta não pode derrubar a coleta
        logger.warning('falha ao gravar alerta de deadlock no run %s', run.pk)


def _gravar_lote_resumo(lote: list[Process], tribunal: Tribunal,
                        run: IngestionRun | None) -> None:
    """Escreve UM lote de resumos travando as linhas em ordem CRESCENTE de pk.

    O deadlock medido em 24/08/2026 (203 de 703 falhas da `djen_backfill`,
    28,9%) tinha esta assinatura, sempre no `bulk_update`:

        django.db.utils.OperationalError: deadlock detected
        DETAIL: Process A waits for ShareLock on transaction X; blocked by B.
                Process B waits for ShareLock on transaction Y; blocked by A.
        CONTEXT: while locking tuple (1126731,22) in relation "tribunals_process"

    Ordenar o queryset NÃO bastava, e ordenar só a lista quase não basta:

      * o `bulk_update` do Django envolve TODOS os batches num único
        `transaction.atomic(savepoint=False)`. Com a lista fora de ordem, o
        batch 1 de um worker pega pks {1, 5} e o batch 2 pega {3, 7}, enquanto
        outro worker faz o contrário — cada um segura o que o outro quer;
      * dentro de UM `UPDATE ... WHERE id IN (...)` a ordem de travamento é a
        do PLANO. Medido com EXPLAIN em produção (86 M linhas), o plano é
        `Index Scan using tribunals_process_pkey`, que sobe ordenado — mas
        depender do plano é depender de estatística.

    Então: cada lote é uma transação própria, e a primeira coisa que ela faz é
    `SELECT ... ORDER BY id FOR NO KEY UPDATE`. O EXPLAIN desse SELECT em
    produção é `LockRows -> Index Scan using tribunals_process_pkey`: as linhas
    são travadas em ordem crescente de pk, sempre, independente do plano do
    UPDATE que vem depois. Ordem total igual em todo mundo = não existe ciclo.

    `FOR NO KEY UPDATE` (e não `FOR UPDATE`) porque nenhuma coluna de chave
    muda aqui — não precisa bloquear quem referencia o processo.
    """
    pks = [p.pk for p in lote]          # já vem ascendente do chamador
    # Deadlock aborta a transação INTEIRA no Postgres. Se alguém acima já abriu
    # uma (o `atomic()` daqui viraria savepoint), retentar dentro dela só
    # produziria "current transaction is aborted" — então o erro sobe pra quem
    # é dono da transação decidir. Na ingestão de verdade o worker roda em
    # autocommit e este caminho é o normal.
    tentativas = 1 if connection.in_atomic_block else DEADLOCK_TENTATIVAS
    for tentativa in range(1, tentativas + 1):
        try:
            with transaction.atomic():
                list(
                    Process.objects.filter(pk__in=pks).order_by('pk')
                    .select_for_update(no_key=True).values_list('pk', flat=True)
                )
                Process.objects.bulk_update(
                    lote, fields=CAMPOS_RESUMO, batch_size=len(lote),
                )
            if tentativa > 1:
                _registrar_deadlock(run, tribunal, tentativa, len(lote), venceu=True)
            return
        except OperationalError as exc:
            if not _e_deadlock(exc):
                raise
            if tentativa >= tentativas:
                # Teto atingido é ERRO com o número real, não corte mudo.
                _registrar_deadlock(run, tribunal, tentativa, len(lote), venceu=False)
                raise
            # backoff exponencial com jitter — dois perdedores que voltam
            # juntos deadlockam de novo.
            time.sleep(min(2.0, DEADLOCK_BACKOFF_S * 2 ** (tentativa - 1))
                       * (0.5 + random.random()))


def _atualizar_resumo_processos(tribunal: Tribunal, cnjs: set[str],
                                com_novidade: set[str] | None = None,
                                run: IngestionRun | None = None) -> None:
    """Recalcula primeira/ultima_movimentacao_em e total_movimentacoes em batch.

    `com_novidade=None` significa "não sei quem mudou" (caminho
    `ingest_processo`, de um processo só) e recalcula todos. O caminho de dia
    (`ingest_window`) sempre informa.
    """
    chunk = []
    for cnj in cnjs:
        chunk.append(cnj)
        if len(chunk) >= 1000:
            _flush_resumo(tribunal, chunk, com_novidade, run)
            chunk = []
    if chunk:
        _flush_resumo(tribunal, chunk, com_novidade, run)


def _flush_resumo(tribunal: Tribunal, cnjs: list[str],
                  com_novidade: set[str] | None = None,
                  run: IngestionRun | None = None) -> None:
    """Reescreve o resumo SÓ de quem mudou, travando na ordem do pk.

    Amplificação medida em 24/08/2026, simulando a lógica de lote sobre a ordem
    real de inserção de um dia inteiro (`CNJS_POR_LOTE=5.000`):

        TRF6 2026-08-20   10.407 movs   10.140 processos   1,01 escrita/proc
        TRF2 2026-08-20   14.792 movs   14.146 processos   1,03
        TJPR 2026-08-20   85.660 movs   62.053 processos   1,03
        TJSP 2026-08-20  278.911 movs  257.899 processos   1,04

    Ou seja: DENTRO de um dia o mesmo processo quase não é reescrito duas vezes
    — a hipótese de "muitas escritas por página" não se confirma desde que o
    lote fecha a cada 5.000 CNJs. A redundância está no EIXO DO TEMPO: o mesmo
    dia é recoletado (overlap diário, recuperação, retry) e cada passada
    reescrevia todos os processos daquele dia com valores idênticos. Em 24 h de
    produção, 70,3% das publicações processadas eram duplicadas e 35,6% dos
    runs não trouxeram uma publicação nova sequer.

    Agora só entra na escrita quem tem movimentação nova — mais o reparo de
    quem está com `total_movimentacoes=0` apesar de aparecer no diário (linha
    que nunca foi somada; o trigger `mov_update_process_agg` da migration 0004
    NÃO existe no banco de produção — conferido em `pg_trigger` em 24/08/2026,
    só `process_set_ano_cnj` sobrou. Este código é o ÚNICO que mantém o
    resumo).
    """
    procs = list(
        Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
        .only('pk', 'numero_cnj', 'total_movimentacoes',
              'primeira_movimentacao_em', 'ultima_movimentacao_em',
              'data_enriquecimento_djen')
    )
    if com_novidade is None:
        alvo = procs
    else:
        alvo = [p for p in procs
                if p.numero_cnj in com_novidade or p.total_movimentacoes == 0]
    if not alvo:
        return

    now_ts = timezone.now()
    # `processo_id__in=<pks>` e não `processo__in=<queryset>`: o subquery
    # reexecutava a busca por (tribunal, numero_cnj), e o filtro por
    # `tribunal` impedia o Index Only Scan em `mov_processo_data_disp_idx`
    # (processo_id já determina o tribunal — Movimentacao.processo é do
    # tribunal do processo, sempre).
    aggregates = (
        Movimentacao.objects.filter(processo_id__in=[p.pk for p in alvo])
        .values('processo_id')
        .annotate(
            primeira=Min('data_disponibilizacao'),
            ultima=Max('data_disponibilizacao'),
            total=Count('id'),
        )
    )
    by_proc = {a['processo_id']: a for a in aggregates}
    to_update = []
    for p in alvo:
        agg = by_proc.get(p.pk)
        if not agg:
            continue
        p.primeira_movimentacao_em = agg['primeira']
        p.ultima_movimentacao_em = agg['ultima']
        p.total_movimentacoes = agg['total']
        # `data_enriquecimento_djen` passa a significar "última vez que o DJEN
        # trouxe movimentação NOVA para este processo" — antes era "última vez
        # que o DJEN passou por aqui", e renová-la era o motivo de reescrever
        # 70% das linhas à toa. Quem lê o campo (ficha do processo, doc do ES)
        # quer saber de dado, não de varredura.
        p.data_enriquecimento_djen = now_ts
        to_update.append(p)
    if not to_update:
        return

    # ORDEM TOTAL por pk: os lotes viram faixas crescentes e disjuntas, iguais
    # pra todo worker. Sem isso, dois workers com conjuntos sobrepostos travam
    # em ordens opostas — ver `_gravar_lote_resumo`.
    to_update.sort(key=lambda p: p.pk)
    for i in range(0, len(to_update), LOTE_UPDATE_PROCESS):
        _gravar_lote_resumo(to_update[i:i + LOTE_UPDATE_PROCESS], tribunal, run)


def _enfileirar_todos_enrichments(tribunal: Tribunal, cnjs: set[str]) -> None:
    """Para todo processo NOVO descoberto na ingestão DJEN, enfileira:
      1. Enriquecimento PJe (consulta pública) — partes/advogados — fila enrich_trf{N}
      2. Sincronização Datajud — movs+metadados — fila datajud

    Filtra por enriquecido_em < 24h pra evitar re-enfileirar processos
    recém-tocados. Cada fila tem workers dedicados, sem competição.

    Histórico DJEN per-processo (`sync_movimentacoes_bulk`) NÃO é mais
    enfileirado aqui: o `backfill_dia` cobre todo histórico do tribunal
    naturalmente, e Datajud já traz o histórico completo do processo em
    1 request. DJEN per-processo só sob demanda (futuro).
    """
    if not cnjs:
        return

    cutoff = timezone.now() - timedelta(hours=24)
    procs = list(
        Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
        .values('pk', 'enriquecido_em', 'ultima_sinc_djen_em')
    )
    if not procs:
        return

    enriq_eligiveis = []
    datajud_eligiveis = []
    for p in procs:
        if p['enriquecido_em'] is None or p['enriquecido_em'] < cutoff:
            enriq_eligiveis.append(p['pk'])
        if p['ultima_sinc_djen_em'] is None or p['ultima_sinc_djen_em'] < cutoff:
            datajud_eligiveis.append(p['pk'])

    # PJe enricher só pra tribunais com scraper implementado. Honra o watermark:
    # sob backfill, este auto-enqueue (sem teto) inflava filas a MILHÕES (TJRO
    # 3,6M / TJRJ 2,6M vistos em 2026-07-11) — acima do teto, deixa pro refill
    # (o processo já fica 'pendente'; nada se perde). LLEN é O(1), e ainda
    # cacheamos a decisão por 30s pra não consultar o Redis a cada processo.
    if enriq_eligiveis and tribunal.sigla in TRIBUNAIS_COM_ENRICHER:
        import django_rq as _drq
        from django.core.cache import cache as _cache

        from enrichers.jobs import QUEUE_HIGH_WATER, enqueue_enriquecimento, queue_for
        gate_key = f'enrich:gate:{tribunal.sigla}'
        gate = _cache.get(gate_key)
        if gate is None:
            try:
                gate = len(_drq.get_queue(queue_for(tribunal.sigla))) < QUEUE_HIGH_WATER
            except Exception:  # noqa: BLE001 — Redis indisponível: não bloqueia ingestão
                gate = True
            _cache.set(gate_key, gate, timeout=30)
        if gate:
            for pid in enriq_eligiveis:
                try:
                    enqueue_enriquecimento(pid, tribunal.sigla)
                except Exception as exc:
                    logger.warning('falha enfileirar enrichment', extra={'pid': pid, 'erro': str(exc)})
        else:
            logger.info('auto-enqueue enrichment skip: fila %s ≥ watermark', tribunal.sigla)

    # Datajud SÓ pra tribunais SEM enricher: onde há enricher (PJe/e-SAJ), a
    # classe/assunto já vem dele → Datajud é redundante. Escopo evita afogar a
    # API pública compartilhada do CNJ (incidente 2026-07-02: 46M+ jobs).
    # `at_front=True`: fluxo diário fura o backlog histórico.
    from django.conf import settings as _settings
    datajud_on = (getattr(_settings, 'DATAJUD_ENQUEUE_ENABLED', True)
                  and tribunal.sigla not in TRIBUNAIS_COM_ENRICHER)
    datajud_enq = 0
    if datajud_eligiveis and datajud_on:
        import django_rq

        from datajud.jobs import DATAJUD_RETRY, _fila_datajud_cheia, datajud_sync_bulk
        queue = django_rq.get_queue('datajud')
        # BOUND (faltava): sem isto o auto-enqueue diário empurrava sem teto e a
        # fila virava o monstro do 02/07 (63M jobs). Mesmo high-water do refill.
        cheia, depth = _fila_datajud_cheia(queue)
        if cheia:
            logger.info('auto-enqueue datajud skip %s: fila %d ≥ high-water',
                        tribunal.sigla, depth)
        else:
            for pid in datajud_eligiveis:
                try:
                    queue.enqueue(datajud_sync_bulk, pid, job_timeout=600, at_front=True,
                                  retry=DATAJUD_RETRY)
                    datajud_enq += 1
                except Exception as exc:
                    logger.warning('falha enfileirar datajud', extra={'pid': pid, 'erro': str(exc)})

    logger.info(
        'auto-enqueue %s → pje=%d datajud=%d (de %d tocados)',
        tribunal.sigla, len(enriq_eligiveis) if tribunal.sigla in TRIBUNAIS_COM_ENRICHER else 0,
        datajud_enq, len(procs),
    )
