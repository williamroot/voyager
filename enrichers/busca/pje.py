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

#: User-Agent do enricher: honesto, diz o que somos.
UA_AGENTE = 'voyager-ops/0.1 (+pje-consulta-publica)'

#: User-Agent de navegador. Não é disfarce: é o que faz o TRF5 servir o
#: formulário ATUAL — ver `UA_POR_TRIBUNAL`.
UA_NAVEGADOR = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

#: Qual User-Agent cada instalação quer — e eles são INVERSOS entre si.
#: Medido em 04/09/2026, três tentativas de cada:
#:
#: | tribunal | UA `voyager-ops`              | UA de navegador          |
#: |----------|-------------------------------|--------------------------|
#: | TRF3     | **HTTP 200 em 0,1 s**         | ReadTimeout 40 s (3/3)   |
#: | TRF5     | consulta pública ANTIGA,      | **formulário `fPP`**     |
#: |          | com captcha e sem `fPP` (3/3) | (3/3)                    |
#:
#: São dois comportamentos diferentes, e nenhum é "anti-bot" no sentido usual:
#: o TRF3 (Akamai) recusa a INCOERÊNCIA — UA de navegador sobre handshake de
#: Python, ou UA de ferramenta conhecida (`python-requests` também leva
#: timeout) —, e o TRF5 faz negociação de conteúdo, servindo a versão legada
#: para quem não se apresenta como navegador.
#:
#: Trocar de cliente HTTP não resolve nem um nem outro: com `curl_cffi`
#: imitando Chrome o TRF5 se comporta igual, porque quem ele lê é o UA. O que
#: resolve é mandar, para cada fonte, o cabeçalho que ela espera.
UA_POR_TRIBUNAL = {'TRF5': UA_NAVEGADOR}

CABECALHOS_BASE = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
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
        # Instância dedicada à busca: mexer no timeout e nos cabeçalhos aqui
        # não toca o enriquecimento em massa.
        self.enricher.timeout = TIMEOUT_BUSCA
        self.enricher.session.headers.update(dict(
            CABECALHOS_BASE,
            **{'User-Agent': UA_POR_TRIBUNAL.get(self.TRIBUNAL, UA_AGENTE)}))
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

    def _abrir_formulario(self) -> BeautifulSoup:
        """Carrega o `fPP` — e, se não vier, tenta com o OUTRO User-Agent.

        O mapa `UA_POR_TRIBUNAL` cobre o que já foi medido; este fallback cobre
        o que ainda não foi. Sem ele, um tribunal que passe a negociar conteúdo
        por UA (como o TRF5 faz) vira "formulário sem ViewState" — um erro de
        layout que manda o próximo a olhar isto caçar parser, quando o problema
        é cabeçalho.
        """
        resp = self.enricher._get(self.list_url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        if soup.find('form', {'id': 'fPP'}):
            return soup

        atual = self.enricher.session.headers.get('User-Agent')
        outro = UA_NAVEGADOR if atual == UA_AGENTE else UA_AGENTE
        logger.info('formulário fPP ausente; repetindo com o outro User-Agent',
                    extra={'tribunal': self.TRIBUNAL, 'de': atual, 'para': outro})
        self.enricher.session.headers['User-Agent'] = outro
        return BeautifulSoup(self.enricher._get(self.list_url).text, 'html.parser')

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

        soup = self._abrir_formulario()
        payload = self._montar_payload(soup, criterio, valor)

        time.sleep(PAUSA_ANTES_DO_POST_S)
        resp = self.enricher._post(self.list_url, payload)

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
            soup = BeautifulSoup(self.enricher._get(self.list_url).text, 'html.parser')
            payload = self._montar_payload(soup, criterio, valor, com_uf=True)
            time.sleep(PAUSA_ANTES_DO_POST_S)
            resp = self.enricher._post(self.list_url, payload)

        if not tem_tabela(resp.text):
            # Não é "não achou": é outra página. O TRF5 já serviu, na mesma
            # URL, uma consulta pública antiga com captcha de imagem — e uma
            # resposta AJAX que só atualiza a div de mensagens é o sintoma de
            # ter submetido pelo botão errado.
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: a resposta da pesquisa não tem a tabela de '
                f'resultados ({len(resp.text)} bytes)')

        yield parse_lista(resp.text, self.TRIBUNAL, self.base_url, self.detalhe_path)
