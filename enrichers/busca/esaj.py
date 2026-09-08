"""Busca por parte no e-SAJ (TJSP, TJAL) — o cliente HTTP.

Composição, não herança: recebe o enricher do tribunal que JÁ existe
(`TjspEnricher`, `TjalEnricher`) e usa a sessão, o pool de proxies e a política
de rotação dele. O que este módulo acrescenta é só o que muda na busca por
parte: outro `cbPesquisa`, a paginação com pausa, e os dois desfechos novos.

O fluxo é o mesmo do `_fetch_processo`: `open.do` estabelece o JSESSIONID e o
`search.do` sai pelo MESMO IP, porque o e-SAJ atrela a sessão ao endereço. A
paginação também: trocar de IP no meio derruba a conversa.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

import requests

from .base import (
    ADVOGADO,
    DOCUMENTO,
    NOME,
    OAB,
    BuscaPorParte,
    FonteIndisponivel,
    ItemEncontrado,
    PaginaResultado,
    RefinarBusca,
)
from .esaj_parser import (
    DESFECHO_AMBIGUO,
    DESFECHO_DETALHE,
    DESFECHO_MUITOS,
    DESFECHO_SIMULTANEAS,
    DESFECHO_VAZIO,
    PAUSA_ENTRE_PAGINAS_S,
    TETO_ESAJ,
    classificar,
    mensagem_da_fonte,
    parse_lista,
)

logger = logging.getLogger('voyager.busca.esaj')

#: `cbPesquisa` de cada critério, lido do `<select id="cbPesquisa">` real. O
#: formulário tem oito opções; as outras três (PRECATORIA, DOCDELEG, NUMCDA)
#: não têm campo correspondente no acervo — ver `FORA_DE_ESCOPO` da tela do
#: JURISCOPE, que chegou à mesma conclusão pelo outro lado.
CB_PESQUISA = {
    DOCUMENTO: 'DOCPARTE',
    NOME: 'NMPARTE',
    OAB: 'NUMOAB',
    ADVOGADO: 'NMADVOGADO',
}

#: Quantas vezes re-tentar o "multiplas consultas simultâneas". É espera, não
#: erro: a fonte está dizendo que a sessão ainda está ocupada com a página
#: anterior.
RETENTATIVAS_SIMULTANEAS = 3

#: `(conectar, ler)` da BUSCA — deliberadamente maior que os `(10, 60)` do
#: enricher, e o motivo é que são operações diferentes: o enricher pede UM
#: processo pelo número; a busca por parte manda o tribunal varrer a base dele.
#:
#: Medido: o CNPJ do Bradesco no e-SAJ levou **71 s** só na primeira página, e
#: uma busca pelo CNPJ do INSS no PJe do TRF3 (no navegador, 04/09/2026) passou
#: de um minuto sem responder. Com 60 s de leitura, a busca por ente público
#: grande morre em `FonteIndisponivel` — justamente onde estão os precatórios.
TIMEOUT_BUSCA = (10, 180)


class BuscaEsaj(BuscaPorParte):
    CRITERIOS_SUPORTADOS = frozenset({DOCUMENTO, NOME, OAB, ADVOGADO})
    TETO_DA_FONTE = TETO_ESAJ
    POR_PAGINA = 25

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        # `getattr` pelo mesmo motivo do motor PJe: o motor não deve assumir
        # atributo do enricher. Aqui o e-SAJ tem `PREFER_CORTEX`, mas ler
        # direto é o padrão que já quebrou os cinco PJe em produção.
        self.enricher = enricher_cls(
            prefer_cortex=(getattr(enricher_cls, 'PREFER_CORTEX', False)
                           if prefer_cortex is None else prefer_cortex))
        self.TRIBUNAL = enricher_cls.TRIBUNAL_SIGLA
        self.base_url = enricher_cls.BASE_URL
        # O enricher desta instância é exclusivo da busca, então trocar o
        # timeout dele não afeta o enriquecimento em massa.
        self.enricher.timeout = TIMEOUT_BUSCA
        self._proxies: dict = {}
        self._tentados: set = set()

    # ── rede ─────────────────────────────────────────────────────────────────

    @property
    def session(self) -> requests.Session:
        return self.enricher.session

    def _abrir_com_rotacao(self, criterio: str, valor: str,
                           so_requisitorios: bool) -> tuple[str, str]:
        """Abre a conversa e faz a primeira busca, trocando de IP até conseguir.

        Isto NÃO é zelo excessivo: o pool do TJAL responde por volta de 37% dos
        IPs (ADR-021), então uma tentativa só falha na maioria das vezes — foi
        exatamente o que aconteceu na primeira busca real em produção
        (`transporte — HTTPSConnectionPool(host='www2.tjal.jus.br')`). O
        enricher sempre rotacionou; o motor de busca não, e a diferença ficou
        invisível enquanto rodei tudo fora do container, sem proxy.

        A rotação vale só para a ABERTURA. Depois que a conversa está de pé, o
        JSESSIONID está atado àquele IP e trocar no meio derruba a paginação —
        por isso uma queda depois da primeira página vira resultado PARCIAL,
        não erro (ver `buscar_no_tribunal`).
        """
        tentativas = getattr(self.enricher, 'MAX_PROXY_ROTATIONS', 8)
        ultimo = None
        for tentativa in range(1, tentativas + 1):
            try:
                self._abrir_sessao()
                resp = self._get(f'{self.base_url}/cpopg/search.do',
                                 params=self._params(criterio, valor, so_requisitorios))
                return resp.text, resp.url
            except FonteIndisponivel as exc:
                ultimo = str(exc)
                logger.info('e-SAJ não respondeu por este IP; rotacionando',
                            extra={'tribunal': self.TRIBUNAL, 'tentativa': tentativa,
                                   'de': tentativas, 'motivo': ultimo[:80]})
        raise FonteIndisponivel(
            f'{self.TRIBUNAL}: {tentativas} IPs tentados sem sucesso'
            + (f' (último: {ultimo})' if ultimo else ''))

    def _abrir_sessao(self) -> None:
        """Escolhe um IP e abre a conversa (`open.do`) por ele.

        Sessão limpa por IP: o JSESSIONID nasce atado ao proxy desta tentativa,
        exatamente como no enricher. Sem isto, a segunda página sai por outro
        endereço e o e-SAJ não reconhece a consulta.
        """
        proxy = self.enricher._next_proxy(self._tentados)
        if not proxy:
            # Sem pool CONFIGURADO (`pool is None`) a saída é direta — é o modo
            # do `scripts/validar_busca_parte.py` fora do container. Com pool
            # configurado e vazio, o certo continua sendo falhar: sair pelo IP
            # do worker queimaria justamente o endereço que não se pode perder.
            if self.enricher.pool is not None:
                raise FonteIndisponivel(f'{self.TRIBUNAL}: pool sem proxy disponível')
            self._proxies = {}
            self.session.cookies.clear()
            self._get(f'{self.base_url}/cpopg/open.do')
            return
        from djen.proxies import cortex_proxy_url
        if proxy != cortex_proxy_url(self.enricher.pool):
            self._tentados.add(proxy)
        self._proxies = {'http': proxy, 'https': proxy}
        self.session.cookies.clear()
        self._get(f'{self.base_url}/cpopg/open.do')

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET pelo proxy da sessão, traduzindo falha de transporte/muro.

        Tudo que não é resposta útil vira `FonteIndisponivel` — e portanto
        re-tentável. Nada aqui pode virar "não achei": um 403 do WAF e uma
        lista vazia são fatos opostos.
        """
        try:
            resp = self.session.get(url, params=params, proxies=self._proxies,
                                    timeout=self.enricher.timeout,
                                    allow_redirects=True)
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            self._queimar_proxy()
            raise FonteIndisponivel(f'{self.TRIBUNAL}: transporte — {str(exc)[:120]}') from exc
        if resp.status_code in (403, 429):
            self._queimar_proxy()
            raise FonteIndisponivel(f'{self.TRIBUNAL}: bloqueado {resp.status_code}')
        if resp.status_code >= 500:
            # Culpa do servidor, não do IP: não queima o proxy.
            raise FonteIndisponivel(f'{self.TRIBUNAL}: e-SAJ {resp.status_code}')
        return resp

    def _queimar_proxy(self) -> None:
        atual = (self._proxies or {}).get('https')
        if not atual or self.enricher.pool is None:
            return
        from djen.proxies import cortex_proxy_url
        if atual != cortex_proxy_url(self.enricher.pool):
            self.enricher.pool.mark_bad(atual)

    # ── busca ────────────────────────────────────────────────────────────────

    def _params(self, criterio: str, valor: str, so_requisitorios: bool) -> dict:
        params = {
            'conversationId': '',
            'cbPesquisa': CB_PESQUISA[criterio],
            'dadosConsulta.valorConsulta': valor,
            'cdForo': '-1',
        }
        if so_requisitorios:
            # Recorte de precatório/RPV do próprio e-SAJ. É o caminho que o
            # JURISCOPE já usa em produção para achar requisitório por CPF, e
            # aqui serve de escape para o teto de 1.000: filtra antes de a
            # fonte truncar.
            params['consultaDeRequisitorios'] = 'true'
        return params

    def paginar(self, criterio: str, valor: str, teto_paginas: int = 40,
                so_requisitorios: bool = False) -> Iterator[PaginaResultado]:
        self.exigir_suporte(criterio)
        html, url_final = self._abrir_com_rotacao(criterio, valor, so_requisitorios)

        pagina = 1
        while True:
            desfecho = classificar(html, url_final)

            if desfecho == DESFECHO_VAZIO:
                # Terminal e honesto: a fonte olhou e não tem.
                yield PaginaResultado(itens=[], pagina=pagina, total_declarado=0)
                return

            if desfecho == DESFECHO_MUITOS:
                raise RefinarBusca(mensagem_da_fonte(html)
                                   or f'{self.TRIBUNAL}: busca ampla demais para a fonte')

            if desfecho == DESFECHO_SIMULTANEAS:
                # A sessão ainda está ocupada com a página anterior. Esperar é
                # a correção — trocar de IP recomeçaria a conversa do zero.
                html = self._reagir_a_simultaneas(url_final)
                if html is None:
                    raise FonteIndisponivel(
                        f'{self.TRIBUNAL}: a fonte seguiu acusando consultas '
                        f'simultâneas após {RETENTATIVAS_SIMULTANEAS} esperas')
                continue

            if desfecho == DESFECHO_DETALHE:
                yield PaginaResultado(itens=self._item_unico(html, url_final),
                                      pagina=pagina, total_declarado=1)
                return

            if desfecho == DESFECHO_AMBIGUO:
                raise FonteIndisponivel(
                    f'{self.TRIBUNAL}: resposta 200 que não é lista, detalhe, '
                    f'vazio nem recusa conhecida')

            resultado = parse_lista(html, self.TRIBUNAL, self.base_url, pagina)
            yield resultado

            if not resultado.tem_proxima or pagina >= teto_paginas:
                return

            html, url_final = self._proxima(html)
            pagina += 1

    def _reagir_a_simultaneas(self, url_final: str) -> str | None:
        """Espera crescente e repete a MESMA URL. `None` se não adiantou."""
        for tentativa in range(1, RETENTATIVAS_SIMULTANEAS + 1):
            espera = PAUSA_ENTRE_PAGINAS_S * tentativa
            logger.info('e-SAJ acusou consultas simultâneas; esperando',
                        extra={'tribunal': self.TRIBUNAL, 'espera_s': espera,
                               'tentativa': tentativa})
            time.sleep(espera)
            resp = self._get(url_final)
            if classificar(resp.text, resp.url) != DESFECHO_SIMULTANEAS:
                return resp.text
        return None

    def _proxima(self, html: str) -> tuple[str, str]:
        """Segue o link de próxima página, respeitando o pacing da fonte.

        A pausa não é educação: sem ela o e-SAJ responde "multiplas consultas
        simultâneas" e a lista parece ter acabado na página 1 (medido, 3 de 3).
        """
        from .esaj_parser import proxima_pagina

        href = proxima_pagina(html)
        if not href:
            raise FonteIndisponivel(f'{self.TRIBUNAL}: página seguinte sumiu do HTML')
        time.sleep(PAUSA_ENTRE_PAGINAS_S)
        url = href if href.startswith('http') else f'{self.base_url}{href}'
        resp = self._get(url)
        return resp.text, resp.url

    def _item_unico(self, html: str, url_final: str) -> list[ItemEncontrado]:
        """O caso de 1 resultado: o e-SAJ pula a lista e abre o processo.

        O número sai do `#numeroProcesso` da página; a URL é o fallback, porque
        nem toda instalação carrega o `processo.numero` na querystring.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or '', 'html.parser')
        el = soup.select_one('#numeroProcesso')
        numero = re.sub(r'\s+', '', el.get_text(strip=True)) if el else ''
        if not numero:
            m = re.search(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}', url_final or '')
            numero = m.group(0) if m else ''
        if not numero:
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: detalhe sem número de processo legível')
        return [ItemEncontrado(
            numero_cnj=numero, tribunal=self.TRIBUNAL,
            classe=(soup.select_one('#classeProcesso').get_text(strip=True)
                    if soup.select_one('#classeProcesso') else ''),
            assunto=(soup.select_one('#assuntoProcesso').get_text(strip=True)
                     if soup.select_one('#assuntoProcesso') else ''),
            url_fonte=url_final,
        )]
