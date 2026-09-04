"""Leitura do resultado de busca do PJe (form `fPP`). Puro: HTML entra, dado sai.

A resposta é um fragmento AJAX do RichFaces contendo a `fPP:processosTable`.
Cada linha tem três células:

    td0  o link "Ver detalhes" (`openPopUp(... listView.seam?ca=<hash>)`)
    td1  "[CÍVEL] CLASSE POR EXTENSO" + "<sigla> <CNJ> - <assunto>" +
         "POLO ATIVO X POLO PASSIVO"
    td2  última movimentação, com a data entre parênteses

E o `<tfoot>` diz "N resultados encontrados" — **N nunca passa de 30**, que é o
teto da fonte, não o total (medido: 12 quando há 12; 30 em três buscas de
volumes muito diferentes). Não existe scroller nem link de próxima página:
acima de 30, o dado não é alcançável por este critério.

Zero resultados tem uma cara própria e traiçoeira: o rodapé fica
"resultados encontrados", SEM número.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ItemEncontrado, PaginaResultado

#: Teto medido da consulta pública do PJe.
TETO_PJE = 30

_RE_CNJ = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')
_RE_CA = re.compile(r"listView\.seam\?ca=([0-9a-fA-F]+)")
_RE_TOTAL = re.compile(r'(\d[\d.]*)\s+resultados?\s+encontrados?', re.I)


def _texto(el) -> str:
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip() if el else ''


def tem_tabela(html: str) -> bool:
    """A resposta é mesmo o resultado da busca?

    Falso aqui significa "não é a página que eu conheço" — WAF, sessão perdida,
    instalação servindo outro formulário (o TRF5 já serviu uma consulta pública
    antiga, com captcha de imagem, na mesma URL). Quem chama transforma isso em
    fonte indisponível, jamais em "não achou".
    """
    return bool(BeautifulSoup(html or '', 'html.parser')
                .select_one('table[id$=processosTable]'))


def total_declarado(html: str) -> int | None:
    """N do rodapé. `None` quando não há rodapé; `0` quando o rodapé não traz
    número (que é como o PJe escreve "nenhum resultado")."""
    soup = BeautifulSoup(html or '', 'html.parser')
    tabela = soup.select_one('table[id$=processosTable]')
    if not tabela:
        return None
    rodape = _texto(tabela.select_one('tfoot'))
    if not rodape:
        return None
    m = _RE_TOTAL.search(rodape)
    if m:
        return int(m.group(1).replace('.', ''))
    return 0 if 'resultado' in rodape.lower() else None


def _parse_linha(tr, tribunal: str, base_url: str, detalhe_path: str) -> ItemEncontrado | None:
    celulas = tr.find_all('td')
    if len(celulas) < 2:
        return None
    corpo = celulas[1]
    texto = _texto(corpo)
    m = _RE_CNJ.search(texto)
    if not m:
        return None
    numero = m.group(0)

    # A classe é o que vem ANTES do bloco em negrito com o número; o assunto é
    # o que vem depois do "<CNJ> - ". Fatiar pelo próprio número evita depender
    # da ordem dos nós, que muda entre instalações.
    negrito = _texto(corpo.find('b'))
    classe = texto.split(negrito)[0].strip() if negrito and negrito in texto else ''
    classe = re.sub(r'^\[[^\]]*\]\s*', '', classe).strip()
    assunto = ''
    if negrito:
        depois = negrito.split(numero, 1)[-1]
        assunto = depois.lstrip(' -').strip()

    # "AUTOR X RÉU": é o que a lista mostra e serve para a tela. NÃO vira parte
    # no banco — quem cria parte é o enricher, com a ficha completa.
    partes: tuple[str, ...] = ()
    resto = texto.split(negrito, 1)[-1] if negrito and negrito in texto else ''
    if ' X ' in resto:
        partes = tuple(p.strip() for p in resto.split(' X ', 1) if p.strip())

    ca = _RE_CA.search(str(corpo)) or _RE_CA.search(str(celulas[0]))
    url = (f'{base_url}{detalhe_path}/listView.seam?ca={ca.group(1)}'
           if ca and base_url else '')

    return ItemEncontrado(
        numero_cnj=numero,
        tribunal=tribunal,
        classe=classe,
        assunto=assunto,
        distribuicao=_texto(celulas[2]) if len(celulas) > 2 else '',
        url_fonte=url,
        partes_na_lista=partes,
    )


def parse_lista(html: str, tribunal: str, base_url: str = '',
                detalhe_path: str = '') -> PaginaResultado:
    """A única página que o PJe dá. `tem_proxima` é sempre falso — de fato.

    `total_e_teto` liga quando o rodapé mostra exatamente o teto: é o sinal de
    que existe mais do que a fonte está disposta a mostrar, e a resposta da API
    precisa dizer isso em vez de entregar 30 como se fosse tudo.
    """
    soup = BeautifulSoup(html or '', 'html.parser')
    tabela = soup.select_one('table[id$=processosTable]')
    itens = []
    if tabela:
        for tr in tabela.select('tbody tr'):
            item = _parse_linha(tr, tribunal, base_url, detalhe_path)
            if item:
                itens.append(item)

    return PaginaResultado(
        itens=itens,
        pagina=1,
        **_conciliar_total(total_declarado(html), len(itens)),
        tem_proxima=False,
    )


def _conciliar_total(rodape: int | None, lidos: int) -> dict:
    """Concilia o que o rodapé diz com o que a tabela mostrou.

    O rodapé NÃO é contagem em toda instalação. Medido em 04/09/2026 no TRF5,
    buscando pelo CNPJ do INSS: rodapé "30 resultados encontrados", tabela com
    **uma** linha. Ali o 30 é o tamanho da página, e repassá-lo como total faria
    a API publicar um número que a própria fonte contradiz na mesma resposta.

    Regra: o total só é aceito quando bate com o que veio. Divergindo, a
    resposta diz "não sei" (`None`) e registra a anomalia — abster > chutar.

    E o TETO se mede pelas LINHAS, nunca pelo rodapé: 30 linhas é a fonte
    truncando, 30 no rodapé com 1 linha não é.
    """
    if rodape is not None and lidos and rodape != lidos:
        return {
            'total_declarado': None,
            'total_e_teto': lidos >= TETO_PJE,
            'aviso_fonte': (f'a fonte anuncia {rodape} resultados mas devolveu '
                            f'{lidos} na mesma resposta — o número publicado por '
                            f'este tribunal não é a contagem'),
        }
    return {
        'total_declarado': rodape,
        'total_e_teto': lidos >= TETO_PJE,
        'aviso_fonte': '',
    }
