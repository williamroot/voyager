"""Leitura do resultado de busca do PJe (form `fPP`). Puro: HTML entra, dado sai.

A resposta é um fragmento AJAX do RichFaces contendo a `fPP:processosTable`.
Cada linha tem três células:

    td0  o link "Ver detalhes" (`openPopUp(... listView.seam?ca=<hash>)`)
    td1  "[CÍVEL] CLASSE POR EXTENSO" + "<sigla> <CNJ> - <assunto>" +
         "POLO ATIVO X POLO PASSIVO"
    td2  última movimentação, com a data entre parênteses

E o `<tfoot>` diz "N resultados encontrados". Esse N é a CONTAGEM da fonte, e
ela para de crescer em **30**: não existe scroller nem link de próxima página,
então acima de 30 o dado não é alcançável por este critério.

Duas coisas que parecem "nenhum resultado" e não são:

- **rodapé sem número** ("resultados encontrados", seco) é como o PJe escreve
  zero;
- **rodapé maior que o número de linhas** é a fonte contando certo e mostrando
  menos. O TRF5 conta 16 e renderiza UMA linha — ver `_conciliar_total`.
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

    A primeira leitura desta função estava ERRADA, e a correção veio de medir
    mais. Com uma amostra só (TRF5: rodapé "30", uma linha), a conclusão foi
    "o rodapé é o tamanho da página". Com **seis** buscas no TRF5, em
    04/09/2026, o padrão apareceu:

        nome "MARIA JOSE DOS SANTOS"      1 linha · rodapé 30
        nome "JOAO BATISTA DE OLIVEIRA"   1 linha · rodapé 30
        OAB 18191                         1 linha · rodapé 16
        advogado "KLEBER TABOSA..."       1 linha · rodapé 13
        nome raro                         0 linhas · rodapé sem número
        CNPJ do INSS                      1 linha · rodapé 30

    O rodapé VARIA com a busca (16, 13, 30) — logo é contagem, não tamanho de
    página. Quem está truncada é a TABELA: a consulta pública do TRF5 conta
    certo e renderiza **uma** linha. Nos outros quatro PJe o rodapé bate com as
    linhas (12 para 12, 30 para 30).

    Então o total é aceito como total, e a diferença vira aviso: "a fonte
    contou N e devolveu M". Dizer `None` aqui, como a versão anterior fazia,
    jogava fora a única informação confiável da resposta.
    """
    faltando = (rodape - lidos) if (rodape is not None and rodape > lidos) else 0
    return {
        'total_declarado': rodape,
        # O teto do PJe aparece na CONTAGEM: 30 é onde ela para de crescer.
        'total_e_teto': bool(rodape and rodape >= TETO_PJE),
        'aviso_fonte': (
            f'a fonte contou {rodape} processos e devolveu {lidos} nesta '
            f'resposta — {faltando} não vieram, e ela não oferece página seguinte'
            if faltando else ''),
    }
