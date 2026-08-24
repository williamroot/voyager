import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Iterator, Optional

import requests
from django.conf import settings

from .proxies import ProxyScrapePool, sessao_rotativa

logger = logging.getLogger('voyager.djen.client')


class DjenClientError(Exception):
    pass


class DjenServerError(DjenClientError):
    """Erro 5xx da DJEN após esgotar retries. Distinto de DjenClientError pra
    que a paginação possa reagir (reduzir page size) — a DJEN devolve 500
    ('sistema muito ocupado') em queries pesadas, sobretudo na 1ª página de
    janelas grandes a itensPorPagina=1000; o mesmo offset a page size menor
    responde 200."""
    pass


class DjenBusyError(DjenServerError):
    """Circuito ABERTO: o DJEN vinha respondendo 5xx em massa ('muito ocupado')
    e as buscas estão pausadas por um cooldown pra não martelar o servidor
    (evita o círculo vicioso). Fast-fail sem tocar no DJEN. Os jobs devem tratar
    como 'adiar', não como erro fatal."""
    pass


# ─────────────────────────── Circuit breaker (fleet-wide, via Redis/cache) ──────────
# Quando o DJEN responde 5xx repetidamente ('O sistema está muito ocupado'), abrir
# o circuito e pausar TODAS as buscas por um cooldown — em vez de 40k jobs × 8 retries
# martelarem o servidor e agravarem a sobrecarga (incidente 2026-07-10).
_CIRCUIT_KEY = 'djen:circuit_open'
_5XX_KEY = 'djen:5xx_recent'


def _cb_threshold() -> int:
    return int(getattr(settings, 'DJEN_CIRCUIT_5XX_THRESHOLD', 15))


def _cb_cooldown() -> int:
    return int(getattr(settings, 'DJEN_CIRCUIT_COOLDOWN', 300))


def _cb_window() -> int:
    return int(getattr(settings, 'DJEN_CIRCUIT_5XX_WINDOW', 120))


def circuit_is_open() -> bool:
    from django.core.cache import cache
    try:
        return bool(cache.get(_CIRCUIT_KEY))
    except Exception:  # noqa: BLE001 — cache indisponível → não bloqueia
        return False


def _record_5xx() -> None:
    """Conta um 5xx na janela; ao atingir o limiar, ABRE o circuito."""
    from django.core.cache import cache
    try:
        try:
            n = cache.incr(_5XX_KEY)
        except ValueError:
            cache.set(_5XX_KEY, 1, timeout=_cb_window())
            n = 1
        if n >= _cb_threshold() and not cache.get(_CIRCUIT_KEY):
            cache.set(_CIRCUIT_KEY, True, timeout=_cb_cooldown())
            logger.error(
                'DJEN circuit ABERTO — %d respostas 5xx na janela; pausando buscas por %ds',
                n, _cb_cooldown(),
            )
    except Exception:  # noqa: BLE001
        pass


def _record_success() -> None:
    """Sucesso → zera o contador de 5xx e fecha o circuito (DJEN recuperou)."""
    from django.core.cache import cache
    try:
        cache.delete(_5XX_KEY)
        cache.delete(_CIRCUIT_KEY)
    except Exception:  # noqa: BLE001
        pass


def _bytes_em_voo() -> int:
    """TETO DE MEMÓRIA da coleta de um dia: bytes de texto em voo entre a API e
    quem grava (ver `DJENClient.iter_pages`). Lido tarde, como os `_cb_*` acima,
    pra que `override_settings` funcione e pra que um cliente montado sem
    `__init__` (os dublês dos testes) continue tendo o teto."""
    return int(getattr(settings, 'DJEN_BYTES_EM_VOO', 24 * 1024 * 1024))


def _peso_por_item(items: list[dict]) -> int:
    """Bytes de texto por publicação nesta página.

    O `texto` é ~99% do peso de um item da DJEN (o resto são datas, siglas e
    dois nomes), então medi-lo é medir a página. `len()` de `str` é O(1) e o
    CPython guarda texto português em 1 byte/caractere na representação
    compacta — o número sai em bytes com erro pequeno e para baixo, que é o
    lado seguro pra um orçamento de memória.

    Piso de 1: página de teste sem `texto` (os dublês dos testes) não pode
    zerar o divisor e nem congelar a calibração.
    """
    if not items:
        return 0
    total = 0
    for it in items:
        texto = it.get('texto')
        if isinstance(texto, str):
            total += len(texto)
    return max(1, total // len(items))


#: Quanto a página pode CRESCER de uma recalibração pra outra.
#:
#: A sonda pesa as 100 primeiras publicações do dia, e 100 de 14 mil é amostra
#: pequena de uma distribuição que não é nada uniforme — medido no TJDFT
#: 2026-08-21: a sonda viu 20,5 KB por publicação e uma leva seguinte trouxe
#: 295,7 KB, **14 vezes mais pesada**. Sem freio, a página saltava de 100 pra 797
#: itens em cima da estimativa leve e a leva pesada chegava com 235 MB.
#:
#: Encolher continua imediato (o peso já foi MEDIDO, não é aposta); crescer é
#: aposta, e aposta anda devagar.
FATOR_CRESCIMENTO = 4


def itens_por_pagina(sigla_djen: str, peso_item: int, janela: int, teto: int,
                     alertas: list[dict] | None = None,
                     anterior: int | None = None) -> int:
    """Quantos itens cabem numa página dentro do orçamento de bytes em voo.

    `janela` é quantas páginas ficam em voo ao mesmo tempo — 8 no caminho flat
    (`DJEN_PAGINAS_PARALELAS`), 27 no caminho por UF (uma por fatia). O
    orçamento é dividido por elas, porque é a SOMA que precisa caber no
    `mem_limit: 1g` do worker.

    Regra nº 2 do CLAUDE.md: se o piso for atingido — publicação tão pesada que
    nem `PISO_ITENS` cabem no orçamento — isso é ERRO registrado com o número
    real, não um encolhimento silencioso.
    """
    teto_api = DJENClient.PAGE_SIZE
    piso = DJENClient.PISO_ITENS
    if peso_item <= 0:
        return min(teto, teto_api)
    orcamento = _bytes_em_voo()
    cabem = (orcamento // max(1, janela)) // peso_item
    if cabem < piso:
        aviso = {
            'erro': 'orcamento_memoria_no_piso', 'tribunal': sigla_djen,
            'peso_item_bytes': int(peso_item), 'cabem': int(cabem),
            'piso_itens': piso, 'bytes_em_voo': orcamento, 'janela': janela,
        }
        if alertas is not None and aviso not in alertas:
            alertas.append(aviso)
            logger.error(
                'DJEN %s: publicação de %.0f KB — no orçamento de %.0f MB ÷ %d '
                'páginas caberiam só %d itens/página, abaixo do piso de %d. '
                'A coleta segue no piso e o pico de memória VAI passar do '
                'orçamento: %.0f MB em voo',
                sigla_djen, peso_item / 1024, orcamento / 1048576,
                janela, cabem, piso, piso * peso_item * janela / 1048576,
            )
    novo = int(max(piso, min(teto, teto_api, cabem)))
    if anterior:                      # cresce devagar, encolhe na hora
        novo = min(novo, max(piso, anterior * FATOR_CRESCIMENTO))
    return novo


class DJENClient:
    """Cliente HTTP da DJEN com paginação, retry exponencial e rotação de proxies."""

    # Cap interno máximo da API DJEN — 1000 itens por página.
    # itensPorPagina>1000 retorna 1000 silenciosamente.
    PAGE_SIZE = 1000
    # Piso pra redução adaptativa de page size quando a DJEN 5xx em página
    # pesada (ver iter_pages). 100 responde 200 de forma confiável onde 1000
    # 500a (medido em TJDFT/TJ* de alto volume, 2026-06-27).
    MIN_PAGE_SIZE = 100
    #: Tamanho da 1ª página do dia — a SONDA. Ela não existe pra coletar (o que
    #: ela traz é re-entregue depois, já sem repetição): existe pra MEDIR quanto
    #: pesa uma publicação deste tribunal antes de comprometer memória.
    #:
    #: 250 e não 100 porque 100 de 14 mil provou ser amostra ruim: no TJDFT
    #: 2026-08-21 as 100 primeiras deram 20,5 KB por publicação e uma leva
    #: seguinte trouxe 295,7 KB. Sozinha (a sonda não é paralela), 250
    #: publicações são ~15 MB no caso comum e ~75 MB no pior caso conhecido —
    #: cabe com folga no `mem_limit: 1g`, e ainda dá o fator 4 até a página cheia,
    #: é o passo máximo de crescimento (`FATOR_CRESCIMENTO`).
    PAGE_SIZE_SONDA = 250
    #: Piso absoluto do tamanho de página quando o orçamento de bytes manda
    #: encolher. Abaixo disso a página deixa de compensar a requisição — e
    #: chegar aqui é ALERTA registrado (`self.alertas`), nunca corte mudo.
    PISO_ITENS = 25

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        self.base_url = settings.DJEN_BASE_URL
        self.page_sleep = settings.DJEN_PAGE_SLEEP_SECONDS
        self.max_retries = settings.DJEN_MAX_RETRIES
        self.max_proxy_rotations = getattr(settings, 'DJEN_MAX_PROXY_ROTATIONS', 50)
        self.timeout = (settings.DJEN_REQUEST_TIMEOUT_CONNECT, settings.DJEN_REQUEST_TIMEOUT_READ)
        self.user_agent = settings.DJEN_USER_AGENT
        self.pool = pool or ProxyScrapePool.singleton()
        #: quantas páginas do MESMO dia são buscadas ao mesmo tempo. A página é
        #: offset puro, então paralelizar não muda o resultado — só o relógio
        #: (medido: 262 páginas do TJSP em 163 min serial). Teto de memória é
        #: esta janela, não o dia.
        self.paginas_paralelas = getattr(settings, 'DJEN_PAGINAS_PARALELAS', 8)
        self.session = sessao_rotativa()   # cache de proxies limitado — ver AdaptadorProxyLimitado
        # Quando True (cliques manuais via fila `manual`), tenta Cortex
        # primeiro — proxy residencial premium, success rate muito maior
        # que pool ProxyScrape rotativo. Click do user retorna em ~3-10s
        # em vez de 30s+ rotacionando proxies queimados.
        self.prefer_cortex = prefer_cortex
        # Em modo normal (não-manual) intercala Cortex/Pool por request via
        # sorteio nesta proporção. Cada request sai com IP diferente —
        # diversifica de verdade quando o WAF bloqueia datacenter em onda.
        self.cortex_ratio = getattr(settings, 'DJEN_CORTEX_RATIO', 0.0)

    @property
    def alertas(self) -> list[dict]:
        """Avisos que o coletor não tem como registrar sozinho — ele não conhece
        o `IngestionRun`. `ingest_window` drena isto pra `run.erros`, porque
        teto atingido tem que virar ERRO auditável (regra nº 2 do CLAUDE.md).

        Preguiçoso de propósito: os testes montam o cliente com `__new__`, sem
        passar pelo `__init__`, e um alerta que só existe no caminho feliz não
        vale nada."""
        avisos = self.__dict__.get('_alertas')
        if avisos is None:
            avisos = self.__dict__['_alertas'] = []
        return avisos

    def count_window(self, sigla_djen: str, data_inicio: date, data_fim: date) -> int:
        """Estimativa do total de movimentações na janela.

        DJEN não retorna count real — `count` no payload é apenas
        `min(total, itensPorPagina)` (descoberta empírica). Usamos itens=1 que
        retorna count=10000 quando a janela tem >=10000 movs (cap interno),
        ou count<10000 se realmente houver menos. Útil pra heurística;
        para volume real, usar `iter_pages` que vai até items=[]."""
        payload = self._fetch(sigla_djen, data_inicio, data_fim, pagina=1, itens_por_pagina=1)
        return int(payload.get('count') or 0)

    def iter_pages(self, sigla_djen: str, data_inicio: date, data_fim: date) -> Iterator[list[dict]]:
        """Itera páginas até esgotar a janela, com o PESO em voo limitado.

        ── por que o tamanho da página é medido em BYTES, não em itens ──

        "8 páginas de 1000 publicações ≈ 30 MB" era a conta que justificava a
        janela paralela. A conta assume que publicação tem tamanho parecido em
        todo tribunal, e isso é falso. Medido em 24/08/2026, TJDFT 2026-08-21:

            14.651 publicações no dia .......... 822,6 MB de texto
            ⇒ 56 KB por publicação, não ~3 KB

        A `itensPorPagina=1000` isso são **55 MB de JSON por requisição**. Com
        `DJEN_PAGINAS_PARALELAS=3` e a leva anterior ainda viva enquanto a
        próxima é buscada, são 6 páginas ≈ 330 MB de JSON — que viram ~950 MB de
        heap em Python (str/dict pesam ~2,5 vezes o JSON cru). Medido no mesmo
        dia, sem gravar nada no banco: **pico de RSS 957 MB**, contra o
        `mem_limit: 1g` do `worker_ingestion`. O OOM killer levava o work-horse
        com SIGKILL e o dia inteiro ia junto — 342 das 703 falhas da fila
        `djen_backfill` (48,6%) eram exatamente isso, 333 delas no TJDFT.

        Então: o que fica constante aqui é o **orçamento de bytes em voo**
        (`DJEN_BYTES_EM_VOO`, 48 MB), repartido entre as páginas paralelas. O
        `itensPorPagina` é a variável de ajuste — para o TJDFT ele cai sozinho
        pra ~280, para um TRF de publicação curta fica no teto de 1000.

        **Isto não é teto de coleta.** A paginação continua indo até a página
        voltar incompleta; muda só o tamanho do balde. Reintroduzir teto de
        página é o pecado original deste projeto (43,6% do TJSP perdidos por
        `for pagina in range(1, 11)`), e não é o que está acontecendo aqui.

        ── a sonda ──

        A 1ª leva vai SOZINHA e com `PAGE_SIZE_SONDA` itens: é ela que mede o
        peso da publicação deste tribunal antes de comprometer memória. O que
        ela traz é re-lido na leva seguinte (a paginação re-ancora por offset de
        ITEM), então custa uma requisição pequena por dia e nunca pula item.

        ── o que já existia e continua ──

        * **downshift de 5xx**: a DJEN 500a ('sistema muito ocupado') em página
          pesada; reduzindo o page size, o mesmo offset responde 200. Ao
          reduzir, retoma de `floor(itens_lidos / novo_size)` — nunca pula item,
          no máximo re-entrega alguns, que o ingest deduplica por id;
        * **janela paralela** (`DJEN_PAGINAS_PARALELAS`): a página é offset
          puro, então buscar N de cada vez não muda o que volta, só o relógio
          (serial, um dia de TJSP são 163 min medidos);
        * **página incompleta seguida de página com dado = ERRO**, nunca um
          `return` discreto — é a assinatura exata do corte mudo.
        """
        pagina = 1
        janela_alvo = max(1, self.paginas_paralelas)
        # A 1ª leva é a sonda: 1 página pequena, sozinha, só pra pesar o item.
        janela = 1
        page_size = self.PAGE_SIZE_SONDA
        itens_lidos = 0
        peso_item = 0            # bytes de texto por publicação (média com meia-vida)
        peso_leva = 0            # o máximo medido na leva corrente
        # Teto herdado do downshift de 5xx: uma vez que a DJEN recusou página
        # grande neste offset, o orçamento de bytes não pode reinflá-la de volta
        # (senão os dois controles ficam em ping-pong eterno).
        teto_5xx = self.PAGE_SIZE
        with ThreadPoolExecutor(max_workers=janela_alvo) as pool:
            while True:
                try:
                    # Em page size grande, desiste cedo do 5xx (max_5xx=2) pra
                    # reduzir rápido em vez de insistir minutos no offset pesado.
                    # No piso, usa o budget normal de retries.
                    futuros = [
                        pool.submit(
                            self._fetch, sigla_djen, data_inicio, data_fim, n,
                            itens_por_pagina=page_size,
                            max_5xx=(2 if page_size > self.MIN_PAGE_SIZE else None),
                        )
                        for n in range(pagina, pagina + janela)
                    ]
                    payloads = [f.result() for f in futuros]
                except DjenServerError:
                    if page_size > self.MIN_PAGE_SIZE:
                        novo = max(self.MIN_PAGE_SIZE, page_size // 5)
                        logger.warning(
                            'DJEN 5xx em %s page_size=%d (offset~%d) → reduzindo p/ %d e retomando',
                            sigla_djen, page_size, itens_lidos, novo,
                        )
                        page_size = teto_5xx = novo
                        pagina = itens_lidos // page_size + 1
                        continue
                    raise

                acabou_em = None
                peso_leva = 0
                for i in range(len(payloads)):
                    # Tira a página do voo ANTES de entregá-la: enquanto o
                    # consumidor grava, a leva inteira não pode continuar viva.
                    payload, payloads[i] = payloads[i], None
                    items = (payload or {}).get('items') or []
                    del payload
                    n_itens = len(items)
                    if not n_itens:
                        if acabou_em is None:
                            acabou_em = i
                        continue
                    if acabou_em is not None:
                        raise DjenClientError(
                            f'{sigla_djen} {data_inicio}: página {pagina + acabou_em} '
                            f'sinalizou fim mas a {pagina + i} trouxe {n_itens} itens — '
                            f'paginação inconsistente, o dia NÃO pode contar como coberto'
                        )
                    peso_leva = max(peso_leva, _peso_por_item(items))
                    # Recorte da SOBREPOSIÇÃO. Quando o tamanho da página muda no
                    # meio do dia (a sonda, ou o downshift de 5xx), a paginação
                    # re-ancora por offset de ITEM e a 1ª página da nova leva
                    # repete o que já foi entregue. O offset é conhecido
                    # ((pagina-1) * page_size), então dá pra cortar aqui — e a
                    # saída volta a ser EXATAMENTE o dia, em ordem e sem repetir.
                    # Antes isso ficava por conta do dedupe do banco, o que
                    # custava INSERT e escondia o custo do re-fetch.
                    inicio_desta = (pagina + i - 1) * page_size
                    if inicio_desta < itens_lidos:
                        items = items[itens_lidos - inicio_desta:]
                    if items:
                        yield items
                    del items          # a página sai da memória aqui, não no fim do dia
                    itens_lidos = max(itens_lidos, inicio_desta + n_itens)
                    # Página menor que o page size: chegou ao fim da janela.
                    if n_itens < page_size:
                        acabou_em = i
                del payloads           # nada da leva anterior sobrevive à próxima
                if acabou_em is not None:
                    return

                pagina += janela
                # ── recalibra o balde pelo peso medido ────────────────────────
                # O peso da publicação varia MUITO dentro do mesmo dia: no TJDFT
                # 2026-08-21 as levas mediram 24,6 KB, 220,4 KB, 336,3 KB e
                # 578,8 KB por item. Guardar o MÁXIMO de todos os tempos deixa a
                # página presa no tamanho do pior trecho até o fim do dia (28
                # itens, 523 páginas) — seguro e lento demais. Guardar só a
                # última leva volta a se expor inteiro a cada oscilação. O meio
                # é MEMÓRIA COM MEIA-VIDA: o passado pesa metade a cada leva,
                # então a página recupera o tamanho em ~4 levas em vez de nunca,
                # e nunca de uma vez só.
                peso_item = max(peso_leva, peso_item // 2)
                novo = self._itens_por_pagina(sigla_djen, peso_item, janela_alvo,
                                              teto_5xx, anterior=page_size)
                if novo != page_size:
                    logger.info(
                        'DJEN %s: publicação pesa %.1f KB → itensPorPagina %d→%d '
                        '(orçamento %.0f MB em voo ÷ %d páginas paralelas)',
                        sigla_djen, peso_item / 1024, page_size, novo,
                        _bytes_em_voo() / 1048576, janela_alvo,
                    )
                    page_size = novo
                    pagina = itens_lidos // page_size + 1   # re-ancora por ITEM
                janela = janela_alvo
                time.sleep(self.page_sleep)

    def _itens_por_pagina(self, sigla_djen: str, peso_item: int, janela: int,
                          teto: int, anterior: int | None = None) -> int:
        return itens_por_pagina(sigla_djen, peso_item, janela, teto, self.alertas,
                                anterior=anterior)

    def _fetch(self, sigla_djen: str, data_inicio: date, data_fim: date, pagina: int,
               itens_por_pagina: int = 1000, extra_params: Optional[dict] = None,
               max_5xx: Optional[int] = None) -> dict:
        # max_5xx limita só os retries de 5xx (servidor). iter_pages passa um
        # valor baixo pra "desistir cedo" e reduzir o page size em vez de
        # insistir minutos no mesmo offset pesado. None = usa self.max_retries.
        limite_5xx = max_5xx if max_5xx is not None else self.max_retries
        params = {
            'pagina': pagina,
            'itensPorPagina': itens_por_pagina,
            'siglaTribunal': sigla_djen,
            'dataDisponibilizacaoInicio': data_inicio.isoformat(),
            'dataDisponibilizacaoFim': data_fim.isoformat(),
        }
        if extra_params:
            params.update(extra_params)
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}

        last_exc: Optional[Exception] = None
        last_failed_source: Optional[str] = None
        proxy_rotations = 0
        transport_retries = 0
        server_5xx_retries = 0

        while True:
            # Circuito aberto (DJEN sobrecarregado) → fast-fail sem tocar no servidor.
            if circuit_is_open():
                raise DjenBusyError(
                    'DJEN circuito aberto (sobrecarregado) — busca pausada pra não martelar'
                )
            proxy_url, using = self._pick_proxy(prefer_other_than=last_failed_source)
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

            t0 = time.monotonic()
            try:
                resp = self.session.get(self.base_url, params=params, headers=headers,
                                        proxies=proxies, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                proxy_label = using if proxy_url else 'direct'
                # 403/429: IP bloqueado → marca proxy ruim e troca.
                # Backoff progressivo quando muitas rotações falham seguidas:
                # WAF da DJEN tipicamente "abre" se pausarmos um momento.
                if resp.status_code in (403, 429):
                    if using == 'pool' and proxy_url:
                        self.pool.mark_bad(proxy_url)
                    elif using == 'cortex':
                        self.pool.mark_cortex_bad()  # usa CORTEX_BAD_TTL_SECONDS
                    last_failed_source = using
                    proxy_rotations += 1
                    if proxy_rotations > self.max_proxy_rotations:
                        raise DjenClientError(
                            f'DJEN {resp.status_code} após {proxy_rotations} rotações de proxy: {resp.text[:200]}'
                        )
                    logger.warning(
                        '🔄 %s bloqueado via %s → rotação %d/%d',
                        resp.status_code, proxy_label, proxy_rotations, self.max_proxy_rotations,
                    )
                    pause_after = getattr(settings, 'DJEN_ROTATION_PAUSE_AFTER', 10)
                    pause_step = getattr(settings, 'DJEN_ROTATION_PAUSE_STEP', 5.0)
                    pause_max = getattr(settings, 'DJEN_ROTATION_PAUSE_MAX', 30.0)
                    if proxy_rotations >= pause_after and proxy_rotations % pause_after == 0:
                        wait = min(pause_max, pause_step * (proxy_rotations // pause_after))
                        logger.warning(
                            'WAF wave: %d rotações falhando seguidas, pausando %ds',
                            proxy_rotations, wait,
                        )
                        time.sleep(wait)
                    continue
                # 5xx: erro do servidor → backoff longo, limite próprio de retries.
                if 500 <= resp.status_code < 600:
                    _record_5xx()  # alimenta o circuit-breaker (abre em massa)
                    server_5xx_retries += 1
                    if server_5xx_retries >= limite_5xx:
                        raise DjenServerError(
                            f'DJEN {resp.status_code} após {server_5xx_retries} tentativas: {resp.text[:200]}'
                        )
                    logger.warning(
                        '⏳ %s servidor via %s → retry #%d', resp.status_code, proxy_label, server_5xx_retries,
                    )
                    self._sleep_backoff(server_5xx_retries, factor=3.0, max_wait=180.0)
                    continue
                if 400 <= resp.status_code < 500:
                    raise DjenClientError(f'DJEN {resp.status_code}: {resp.text[:200]}')
                resp.raise_for_status()
                logger.debug(
                    '✅ %s pg=%d → %d via %s %dms [rot=%d retry=%d]',
                    sigla_djen, pagina, resp.status_code, proxy_label,
                    latency_ms, proxy_rotations, transport_retries,
                )
                _record_success()  # DJEN respondeu 200 → fecha o circuito
                if using == 'pool':
                    # Pool provou que funciona: zera o streak de falhas e tira
                    # a degradação. Sem este par do mark_bad, o sinal de taxa
                    # de falha só sobe e nunca se desarma.
                    self.pool.mark_ok()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                last_exc = exc
                if proxy_url and using == 'pool':
                    self.pool.mark_bad(proxy_url)
                last_failed_source = using
                transport_retries += 1
                logger.warning(
                    '🔌 transporte #%d via %s: %s',
                    transport_retries, using if proxy_url else 'direct', str(exc)[:120],
                )
                if transport_retries >= self.max_retries:
                    raise DjenClientError(
                        f'erro de transporte após {self.max_retries} tentativas: {exc}'
                    ) from exc
                self._sleep_backoff(transport_retries)
                continue

    def _pick_proxy(self, prefer_other_than: Optional[str] = None) -> tuple[Optional[str], str]:
        from .proxies import cortex_proxy_url

        cortex = cortex_proxy_url(self.pool)
        # Modo manual (cliques do user): Cortex sempre primeiro — latência
        # baixa importa mais que diversificar fontes.
        if self.prefer_cortex:
            if cortex and prefer_other_than != 'cortex':
                return cortex, 'cortex'
            proxy = self.pool.get()
            if proxy:
                return proxy, 'pool'
            return (cortex, 'cortex') if cortex else (None, 'direct')

        # Modo normal: sorteia entre Cortex e Pool em cada request. IP varia
        # entre fontes a cada chamada, distribuindo carga e contornando
        # ondas de WAF que bloqueiam só datacenter ou só residencial.
        # `prefer_other_than` (passado em retry) força a fonte alternativa.
        if prefer_other_than == 'cortex':
            quer_cortex = False
        elif prefer_other_than == 'pool':
            quer_cortex = True
        else:
            # Quando pool degradado, joga 90% via Cortex — datacenter queimado
            # não vale a aposta de 50/50. Ratio degradado configurável (default
            # 1.0 = 100% Cortex quando o datacenter está queimado).
            from django.conf import settings as _s
            ratio = (getattr(_s, 'DJEN_CORTEX_RATIO_DEGRADED', 1.0)
                     if self.pool.is_degraded() else self.cortex_ratio)
            quer_cortex = random.random() < ratio

        if quer_cortex and cortex:
            return cortex, 'cortex'
        proxy = self.pool.get()
        if proxy:
            return proxy, 'pool'
        # Fallback final: usa o que sobrou.
        if cortex:
            return cortex, 'cortex'
        return None, 'direct'


    def _sleep_backoff(self, attempt: int, factor: float = 1.0, max_wait: float = 60.0) -> None:
        wait = min(max_wait, 3.0 * factor * (2 ** attempt) + random.uniform(0, 2))
        time.sleep(wait)

    def count_only(self, sigla_djen: str, data_inicio: date, data_fim: date) -> int:
        """Estimativa via probe rápido — itens=1 retorna count saturado em
        10000 quando volume >= cap, ou count real quando menor."""
        payload = self._fetch(sigla_djen, data_inicio, data_fim, 1, itens_por_pagina=1)
        return int(payload.get('count') or 0)

    def iter_pages_processo(self, sigla_djen: str, numero_cnj: str) -> Iterator[list[dict]]:
        """Itera todas as movimentações de UM processo (sem filtro de data).

        DJEN aceita numeroProcesso=<CNJ formatado ou sem máscara> + siglaTribunal.
        Pagina até items=[]. PAGE_SIZE=1000 (cap DJEN).
        """
        pagina = 1
        page_size = self.PAGE_SIZE
        while True:
            payload = self._fetch_processo(sigla_djen, numero_cnj, pagina, page_size)
            items = payload.get('items') or []
            if not items:
                return
            yield items
            if len(items) < page_size:
                return
            pagina += 1
            time.sleep(self.page_sleep)

    def _fetch_processo(self, sigla_djen: str, numero_cnj: str, pagina: int,
                        itens_por_pagina: int = 1000) -> dict:
        # DJEN aceita ambas as formas; usamos sem máscara pra evitar problemas de URL encoding
        unmask = numero_cnj.replace('-', '').replace('.', '')
        params = {
            'pagina': pagina,
            'itensPorPagina': itens_por_pagina,
            'siglaTribunal': sigla_djen,
            'numeroProcesso': unmask,
        }
        # Reaproveita pipeline de retry/proxy chamando _fetch genérico via params custom.
        # Como _fetch hoje recebe data_inicio/data_fim, vamos chamar diretamente o session.get
        # com a mesma estratégia de proxy.
        return self._fetch_generic(params)

    def _fetch_generic(self, params: dict) -> dict:
        """Versão genérica do _fetch que aceita qualquer params dict.
        Usa a mesma estratégia de proxy + retry de _fetch.
        """
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
        last_exc: Optional[Exception] = None
        last_failed_source: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            proxy_url, using = self._pick_proxy(prefer_other_than=last_failed_source)
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
            t0 = time.monotonic()
            try:
                resp = self.session.get(self.base_url, params=params, headers=headers,
                                        proxies=proxies, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.info('djen request (processo)', extra={
                    'params': params, 'attempt': attempt,
                    'proxy': using if proxy_url else 'direct',
                    'status_code': resp.status_code, 'latency_ms': latency_ms,
                })
                if resp.status_code in (403, 429):
                    if proxy_url and using == 'pool':
                        self.pool.mark_bad(proxy_url)
                    last_failed_source = using
                    if attempt >= self.max_retries:
                        raise DjenClientError(f'DJEN {resp.status_code} após {self.max_retries} tentativas')
                    self._sleep_backoff(attempt)
                    continue
                if 500 <= resp.status_code < 600:
                    if attempt >= self.max_retries:
                        raise DjenClientError(f'DJEN {resp.status_code} após {self.max_retries} tentativas')
                    self._sleep_backoff(attempt, factor=3.0, max_wait=180.0)
                    continue
                if 400 <= resp.status_code < 500:
                    raise DjenClientError(f'DJEN {resp.status_code}: {resp.text[:200]}')
                resp.raise_for_status()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                last_exc = exc
                if proxy_url and using == 'pool':
                    self.pool.mark_bad(proxy_url)
                last_failed_source = using
                if attempt >= self.max_retries:
                    raise DjenClientError(f'erro de transporte: {exc}') from exc
                self._sleep_backoff(attempt)
                continue
        raise DjenClientError(f'esgotadas tentativas: {last_exc}')
