"""Busca por parte no PJe consulta pública (form JSF `fPP`) — o cliente HTTP.

Composição sobre o enricher do tribunal (`Trf1Enricher`, `TjmgEnricher`, …):
`_get`, `_post` e `_request_with_rotation` já resolvem proxy, rotação, WAF e
5xx. O que muda na busca por parte é só o campo preenchido no formulário.

Três coisas medidas em 04/09/2026 que este cliente respeita, e que são a
diferença entre trazer o dado e trazer zero em silêncio
(`.ia/ENRICHMENT.md` §"Busca POR PARTE"):

1. o `name` dos campos tem id gerado pelo JSF e MUDA por instalação
   (`nomeAdv` é j_id186/184/180 em TJMA/TRF1/TRF5) — casamos por sufixo;
2. o botão que submete é o do `A4J.AJAX.Submit`, não o `fPP:searchProcessos`
   visível, que é `type=button`;
3. na busca por OAB, a UF fica SEM SELEÇÃO. Com a UF preenchida a resposta é
   zero; sem ela, a mesma OAB devolve resultado.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

from bs4 import BeautifulSoup

from .base import (
    ADVOGADO,
    DOCUMENTO,
    NOME,
    OAB,
    BuscaPorParte,
    FonteIndisponivel,
    PaginaResultado,
)
from .pje_parser import TETO_PJE, parse_lista, tem_tabela

logger = logging.getLogger('voyager.busca.pje')

#: Sufixo do componente que recebe cada critério. Sufixo, e não o `name`
#: inteiro, porque o prefixo `fPP:j_id###` é gerado e varia por instalação.
SUFIXO_CAMPO = {
    DOCUMENTO: ':documentoParte',
    NOME: ':nomeParte',
    OAB: ':numeroOAB',
    ADVOGADO: ':nomeAdv',
}

PAUSA_ANTES_DO_POST_S = 0.4

#: `(conectar, ler)` da BUSCA — ver o comentário gêmeo em `esaj.py`. O enricher
#: usa `(10, 60)`, que é o certo para pedir UM processo pelo número; a busca por
#: parte manda o tribunal varrer a base dele, e uma consulta pelo CNPJ do INSS
#: no TRF3 passou de um minuto sem responder (medido no navegador, 04/09/2026).
TIMEOUT_BUSCA = (10, 180)

#: Perfil de navegador que o `curl_cffi` imita no TLS/JA3 e no HTTP/2.
#:
#: **É isto que faz o TRF3 responder.** Ele está atrás do Akamai Bot Manager
#: (os cookies `ak_bmsc`/`bm_sv` do HAR provam), e o Akamai não olha o IP: olha
#: o handshake. Com `requests`, o servidor aceita o TCP, completa o TLS e depois
#: fica 45 s sem devolver um byte — de qualquer IP, residencial ou não. Com o
#: fingerprint de Chrome, o MESMO endereço e o MESMO código recebem HTTP 200 em
#: 0,2 s (medido em 04/09/2026, sem proxy nenhum).
#:
#: Não é evasão inventada aqui: é o transporte que o JURISCOPE já usa em
#: produção no cliente autenticado do TRF3 (`datamodel/processors/trf3.py`,
#: `impersonate='chrome131'`).
NAVEGADOR_IMITADO = 'chrome131'

#: Cabeçalhos de navegador. O fingerprint sozinho não basta: o Akamai também lê
#: o conjunto de headers, e uma requisição com JA3 de Chrome e `Accept: */*` de
#: script é incoerente.
HEADERS_NAVEGADOR = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
              'image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
}


class BuscaPje(BuscaPorParte):
    CRITERIOS_SUPORTADOS = frozenset({DOCUMENTO, NOME, OAB, ADVOGADO})
    TETO_DA_FONTE = TETO_PJE
    POR_PAGINA = TETO_PJE

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        self.enricher = enricher_cls(
            prefer_cortex=(enricher_cls.PREFER_CORTEX if prefer_cortex is None
                           else prefer_cortex))
        self.TRIBUNAL = enricher_cls.TRIBUNAL_SIGLA
        self.base_url = enricher_cls.BASE_URL
        self.list_url = enricher_cls.LIST_URL
        self.detalhe_path = enricher_cls.DETALHE_PATH
        # Instância dedicada à busca: mexer no timeout aqui não toca o
        # enriquecimento em massa.
        self.enricher.timeout = TIMEOUT_BUSCA
        self._sessao = None
        self._proxies: dict = {}
        self._tentados: set = set()

    # ── transporte ───────────────────────────────────────────────────────────
    #
    # A busca NÃO usa o `_get`/`_post` do enricher, e a diferença é o cliente:
    # ali é `requests`, aqui é `curl_cffi` imitando Chrome. Ver
    # NAVEGADOR_IMITADO — sem isso o TRF3 não responde a ninguém.

    def _abrir_sessao(self):
        # Imports tardios: `curl_cffi` é do transporte e `djen.proxies` puxa o
        # settings do Django. Deixá-los aqui é o que permite rodar o motor (e o
        # `scripts/validar_busca_parte.py`) de fora do container.
        from curl_cffi import requests as cr

        proxy = self.enricher._next_proxy(self._tentados)
        if proxy:
            from djen.proxies import cortex_proxy_url
            if proxy != cortex_proxy_url(self.enricher.pool):
                self._tentados.add(proxy)
        self._proxies = {'http': proxy, 'https': proxy} if proxy else {}
        self._sessao = cr.Session(impersonate=NAVEGADOR_IMITADO)
        return self._sessao

    def _pedir(self, metodo: str, url: str, **kwargs):
        """GET/POST pela sessão-navegador, com uma rotação de IP em bloqueio.

        Menos rotações que o enricher de propósito: aqui o muro típico não é o
        IP (o Akamai olha o handshake), e insistir em endereços novos só gasta
        pool. Erro de transporte vira `FonteIndisponivel`, nunca "não achei".
        """
        from curl_cffi import requests as cr

        if self._sessao is None:
            self._abrir_sessao()
        cabecalhos = dict(HEADERS_NAVEGADOR, **kwargs.pop('headers', {}))
        ultimo = None
        for tentativa in (1, 2):
            try:
                resp = self._sessao.request(
                    metodo, url, headers=cabecalhos, proxies=self._proxies or None,
                    timeout=TIMEOUT_BUSCA[1], **kwargs)
            except cr.exceptions.RequestsError as exc:
                ultimo = f'transporte: {str(exc)[:120]}'
                self._abrir_sessao()
                continue
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                ultimo = f'HTTP {resp.status_code}'
                if tentativa == 1:
                    # Uma segunda chance, com IP e sessão novos: o desafio do
                    # Akamai é por sessão, e um 403 pode ser sensor expirado.
                    self._abrir_sessao()
                    continue
                break
            return resp
        raise FonteIndisponivel(f'{self.TRIBUNAL}: {ultimo or "sem resposta"}')

    def _get(self, url: str):
        return self._pedir('GET', url)

    def _post(self, url: str, dados: dict):
        return self._pedir('POST', url, data=dados, headers={
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': self.base_url,
            'Referer': self.list_url,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
        })

    # ── formulário ───────────────────────────────────────────────────────────

    @staticmethod
    def _campo_por_sufixo(soup: BeautifulSoup, sufixo: str) -> str | None:
        form = soup.find('form', {'id': 'fPP'}) or soup
        for el in form.find_all(['input', 'select', 'textarea']):
            nome = el.get('name') or ''
            if nome.endswith(sufixo):
                return nome
        return None

    def _id_do_botao(self, soup: BeautifulSoup) -> str | None:
        """Componente que o `executarPesquisa` manda executar.

        Primeiro a heurística do enricher, que é a mesma usada há meses para a
        busca por CNJ; depois o `parameters` do `A4J.AJAX.Submit`, que é onde o
        id realmente está escrito.
        """
        achado = self.enricher._find_search_script_id(soup)
        if achado:
            return achado
        for script in soup.find_all('script'):
            conteudo = script.string or script.get_text() or ''
            if 'executarPesquisa' not in conteudo or 'A4J.AJAX.Submit' not in conteudo:
                continue
            m = re.search(r"'parameters':\s*\{'(fPP:[^']+)'", conteudo)
            if m:
                return m.group(1)
        return None

    def _montar_payload(self, soup: BeautifulSoup, criterio: str, valor: str,
                        com_uf: bool = False) -> dict:
        vs = soup.find('input', {'name': 'javax.faces.ViewState'})
        if not vs or not vs.get('value'):
            # Erro de LAYOUT é reservado para layout: WAF e sessão perdida já
            # foram tratados antes, no `_get` do enricher.
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: formulário sem javax.faces.ViewState')

        campo = self._campo_por_sufixo(soup, SUFIXO_CAMPO[criterio])
        if not campo:
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: o formulário não expõe o campo '
                f'{SUFIXO_CAMPO[criterio]} nesta resposta')

        payload = dict(self.enricher._extract_form_fields(soup))
        if criterio == OAB:
            payload[campo] = re.sub(r'[^0-9]', '', valor)
            combo = self._campo_por_sufixo(soup, ':estadoComboOAB')
            if combo:
                payload[combo] = (self._valor_da_uf(soup, combo, valor) if com_uf
                                  else self._valor_sem_selecao(soup, combo))
        else:
            payload[campo] = valor

        botao = self._id_do_botao(soup)
        if not botao:
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: não achei o botão de pesquisa no formulário')

        payload.update({
            'fPP': 'fPP',
            'AJAXREQUEST': '_viewRoot',
            'javax.faces.ViewState': vs['value'],
            'AJAX:EVENTS_COUNT': '1',
            botao: botao,
        })
        return payload

    @staticmethod
    def _valor_da_uf(soup: BeautifulSoup, nome_do_select: str, valor: str) -> str:
        """Índice do `<option>` da UF (o combo guarda posição, não sigla)."""
        uf = (re.sub(r'[^A-Za-z]', '', valor or '') or '').upper()[:2]
        select = soup.find('select', {'name': nome_do_select})
        if not select or not uf:
            return ''
        for opcao in select.find_all('option'):
            if opcao.get_text(strip=True).upper() == uf:
                return opcao.get('value', '')
        return ''

    @staticmethod
    def _valor_sem_selecao(soup: BeautifulSoup, nome_do_select: str) -> str:
        """O `value` da opção "UF" em branco — no Seam é um sentinela longo
        (`org.jboss.seam.ui.NoSelectionConverter.noSelectionValue`), não ''."""
        select = soup.find('select', {'name': nome_do_select})
        if not select:
            return ''
        primeira = select.find('option')
        return primeira.get('value', '') if primeira else ''

    # ── busca ────────────────────────────────────────────────────────────────

    def paginar(self, criterio: str, valor: str,
                teto_paginas: int = 1) -> Iterator[PaginaResultado]:
        """Uma página só, e isso não é limitação nossa.

        A consulta pública do PJe devolve no máximo 30 resultados e não oferece
        página seguinte — procuramos scroller, "próxima" e `paginaConsulta` no
        HTML: nada. `teto_paginas` existe para o contrato bater com os outros
        motores; aqui ele não muda nada.
        """
        self.exigir_suporte(criterio)

        resp = self._get(self.list_url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        payload = self._montar_payload(soup, criterio, valor)

        time.sleep(PAUSA_ANTES_DO_POST_S)
        resp = self._post(self.list_url, payload)

        # A UF da OAB é INVERSA entre instalações, e não há como saber qual sem
        # tentar. Medido em 04/09/2026, mesma busca, mesmo código:
        #
        #     TJMG   UF em branco -> 6 resultados   |  UF preenchida -> 0
        #     TRF3   UF em branco -> 0              |  UF preenchida -> 30
        #
        # Fixar qualquer um dos dois lados cega metade dos tribunais — e cega em
        # silêncio, porque o outro lado responde "0 resultados" com HTTP 200.
        # Então: tenta em branco, e só se vier zero paga uma requisição a mais
        # com a UF. O custo é uma requisição, e só quando não achou nada.
        if criterio == OAB and tem_tabela(resp.text) and not parse_lista(resp.text, self.TRIBUNAL).itens:
            logger.info('busca por OAB vazia sem UF; repetindo com a UF',
                        extra={'tribunal': self.TRIBUNAL})
            soup = BeautifulSoup(self._get(self.list_url).text, 'html.parser')
            payload = self._montar_payload(soup, criterio, valor, com_uf=True)
            time.sleep(PAUSA_ANTES_DO_POST_S)
            resp = self._post(self.list_url, payload)

        if not tem_tabela(resp.text):
            # Não é "não achou": é outra página. O TRF5 já serviu, na mesma
            # URL, uma consulta pública antiga com captcha de imagem — e uma
            # resposta AJAX que só atualiza a div de mensagens é o sintoma de
            # ter submetido pelo botão errado.
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: a resposta da pesquisa não tem a tabela de '
                f'resultados ({len(resp.text)} bytes)')

        yield parse_lista(resp.text, self.TRIBUNAL, self.base_url, self.detalhe_path)
