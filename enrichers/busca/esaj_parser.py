"""Leitura do resultado de busca do e-SAJ. Puro: HTML entra, dado sai.

Sem rede e sem Django de propósito — é aqui que mora a decisão de o que a
resposta SIGNIFICA, e essa decisão precisa de teste barato sobre fixture real
(`tests/fixtures/tjsp/busca_*.html`, `tests/fixtures/tjal/busca_*.html`).

O ponto sensível é o desfecho. A busca por número, que os enrichers já fazem,
tem quatro (`enrichers/esaj.py::classificar_resposta`); a busca por parte tem
SEIS, e três deles chegam como uma página sem nenhum resultado:

    lista        achou — tem `a.linkProcesso`
    vazio        "Não existem informações disponíveis..."  (terminal)
    muitos       "Foram encontrados muitos processos ... refine"  (peça refino)
    simultaneas  "Foram identificadas multiplas consultas simultâneas"
                 (TRANSITÓRIO — você pediu rápido demais; ver PAUSA_ENTRE_PAGINAS)
    detalhe      1 resultado só: o e-SAJ pula a lista e devolve o processo
    ambiguo      não é nada disso — trata como fonte indisponível, nunca como
                 "não achou"

Ler `muitos` ou `simultaneas` como lista vazia é dizer "esta pessoa não tem
processo" quando a fonte disse outra coisa. Daí os desfechos serem nomeados.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .base import ItemEncontrado, PaginaResultado

DESFECHO_LISTA = 'lista'
DESFECHO_VAZIO = 'vazio'
DESFECHO_MUITOS = 'muitos'
DESFECHO_SIMULTANEAS = 'simultaneas'
DESFECHO_DETALHE = 'detalhe'
DESFECHO_AMBIGUO = 'ambiguo'

#: Teto do e-SAJ, medido: o CNPJ do Bradesco devolve exatamente
#: "1000 Processos encontrados". Número redondo é piso disfarçado.
TETO_ESAJ = 1000

#: Pausa entre páginas da MESMA sessão. Sem ela, o `trocarPagina.do` devolve
#: "multiplas consultas simultâneas" — 3 vezes em 3. Com 1,5 s a página vem
#: completa: primeiro 3 de 3, e depois **32 transições consecutivas sem um
#: único erro**, esgotando uma busca de 823 processos em 51 s. É esse segundo
#: teste que fixa o número: 1,5 s não é o mínimo que funcionou uma vez, é o que
#: sustentou a paginação inteira.
#:
#: A requisição em si custa 0,1 a 0,3 s — a espera é praticamente o gasto todo,
#: e é por isso que ela não sobe "por margem": cada 0,5 s a mais custa 16 s numa
#: busca de 33 páginas.
PAUSA_ENTRE_PAGINAS_S = 1.5

_MARCADORES = (
    (DESFECHO_VAZIO, 'não existem informações'),
    (DESFECHO_VAZIO, 'nao existem informacoes'),
    (DESFECHO_MUITOS, 'muitos processos'),
    (DESFECHO_SIMULTANEAS, 'consultas simultâneas'),
    (DESFECHO_SIMULTANEAS, 'consultas simultaneas'),
)


def _texto(el) -> str:
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip() if el else ''


def classificar(html: str, url_final: str = '') -> str:
    """Desfecho da resposta. `url_final` distingue o caso de 1 resultado só.

    A ordem importa: a página de LISTA também contém o texto do rodapé e dos
    avisos do e-SAJ, então "tem `a.linkProcesso`" é testado ANTES das
    mensagens. Um resultado real nunca é recusa.
    """
    if 'show.do' in (url_final or ''):
        return DESFECHO_DETALHE
    soup = BeautifulSoup(html or '', 'html.parser')
    if soup.select_one('a.linkProcesso'):
        return DESFECHO_LISTA
    aviso = _texto(soup.select_one('#mensagemRetorno')).lower()
    for desfecho, marcador in _MARCADORES:
        if marcador in aviso:
            return desfecho
    # Fora do `#mensagemRetorno` também: instalações antigas põem o aviso na
    # tabela de mensagens (`#spwTabelaMensagem`), e o TJAL às vezes só no corpo.
    corpo = (html or '').lower()
    for desfecho, marcador in _MARCADORES:
        if marcador in corpo:
            return desfecho
    return DESFECHO_AMBIGUO


def mensagem_da_fonte(html: str) -> str:
    """O aviso que o e-SAJ escreveu, palavra por palavra.

    Vai para a resposta da API: quando a fonte se recusa a responder, quem
    perguntou merece ler o motivo dela, não uma paráfrase nossa.
    """
    return _texto(BeautifulSoup(html or '', 'html.parser').select_one('#mensagemRetorno'))


def total_declarado(html: str) -> int | None:
    """O "N Processos encontrados" do `#contadorDeProcessos`. `None` se não há.

    Cuidado de leitura: este número é o total quando é menor que
    `TETO_ESAJ`, e é o TETO quando igual — quem chama decide o que dizer.
    """
    texto = _texto(BeautifulSoup(html or '', 'html.parser').select_one('#contadorDeProcessos'))
    m = re.search(r'([\d.]+)', texto)
    return int(m.group(1).replace('.', '')) if m else None


def _cnj_do_link(a) -> str:
    return _texto(a)


def _codigo_e_foro(href: str) -> tuple[str, str]:
    qs = parse_qs(urlparse(href or '').query)
    return (qs.get('processo.codigo') or [''])[0], (qs.get('processo.foro') or [''])[0]


def parse_lista(html: str, tribunal: str, base_url: str = '',
                pagina: int = 1) -> PaginaResultado:
    """Itens da página de lista, na ordem em que a fonte os deu.

    O foro NÃO está na linha do processo: o e-SAJ agrupa a listagem em
    `<h2 class="foroDosProcessos">` seguidos de um `<ul>` cada. Ler o `h2` mais
    próximo acima é o que preserva a comarca — cair no `select` global pegaria
    sempre o primeiro foro e carimbaria a lista inteira com ele (a busca por
    OAB da fixture tem três foros distintos em 25 linhas).
    """
    soup = BeautifulSoup(html or '', 'html.parser')
    itens: list[ItemEncontrado] = []

    listagem = soup.select_one('#listagemDeProcessos')
    blocos = listagem.find_all(recursive=False) if listagem else []
    foro_atual = ''
    for bloco in blocos:
        if bloco.name == 'h2':
            foro_atual = _texto(bloco)
            continue
        for div in bloco.select('div[id^=divProcesso]'):
            link = div.select_one('a.linkProcesso')
            if not link:
                continue
            _codigo, _foro_id = _codigo_e_foro(link.get('href', ''))
            href = link.get('href', '')
            itens.append(ItemEncontrado(
                numero_cnj=_cnj_do_link(link),
                tribunal=tribunal,
                classe=_texto(div.select_one('.classeProcesso')),
                assunto=_texto(div.select_one('.assuntoPrincipalProcesso')),
                orgao=foro_atual,
                distribuicao=_texto(div.select_one('.dataLocalDistribuicaoProcesso')),
                url_fonte=f'{base_url}{href}' if href.startswith('/') else href,
            ))

    # Sem `#listagemDeProcessos` (layout antigo), cai no varrimento simples:
    # perde o foro, mas não perde processo.
    if not itens:
        for link in soup.select('a.linkProcesso'):
            href = link.get('href', '')
            itens.append(ItemEncontrado(
                numero_cnj=_cnj_do_link(link), tribunal=tribunal,
                url_fonte=f'{base_url}{href}' if href.startswith('/') else href,
            ))

    total = total_declarado(html)
    return PaginaResultado(
        itens=itens,
        pagina=pagina,
        total_declarado=total,
        total_e_teto=bool(total and total >= TETO_ESAJ),
        tem_proxima=proxima_pagina(html) is not None,
    )


def proxima_pagina(html: str) -> str | None:
    """`href` da próxima página, como a fonte o escreveu — ou `None`.

    Seguir o link da própria página, em vez de remontar a querystring, é o que
    mantém o `conversationId` e os parâmetros exatamente como o e-SAJ os quer.
    """
    soup = BeautifulSoup(html or '', 'html.parser')
    prox = soup.select_one('a.unj-pagination__next[href]')
    if prox and 'javascript' not in (prox.get('href') or ''):
        return prox['href']
    # Fallback: o maior `paginaConsulta` citado é sempre a próxima quando o
    # e-SAJ desenha só "1 2 3 …".
    atual = soup.select_one('a.paginaAtual')
    n_atual = int(_texto(atual) or 1) if atual else 1
    for a in soup.select('a.paginacao[href]'):
        m = re.search(r'paginaConsulta=(\d+)', a.get('href') or '')
        if m and int(m.group(1)) == n_atual + 1:
            return a['href']
    return None
