"""Catálogo do DJE/TJSP: quais edições existem, e de que cadernos.

Por que isto é um módulo separado do download: o catálogo INTEIRO — 4.162
edições, de 2007-10-01 a 2025-07-22 — sai numa única requisição de 268 KB, e o
download de um caderno custa até 62 MB. Catalogar primeiro é o gate mais barato
que existe: dá para medir o acervo, planejar o backfill e fechar o watermark
antes de puxar o primeiro byte de PDF.

O e-SAJ não tem API: o índice vive num `var diarios = [...]` dentro do HTML do
`cabecalho.do` (o frame de topo do visualizador). É JavaScript literal, não
JSON — as chaves não têm aspas. Por isso o parse é por regex ancorada nos três
campos, e não `json.loads`: qualquer campo novo que o TJSP acrescente ao objeto
é ignorado em vez de quebrar a coleta.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date

from diarios.base import RespostaInvalida, exigir_ancora

logger = logging.getLogger('voyager.diarios.tjsp_dje')

URL_BASE = 'https://dje.tjsp.jus.br/cdje'
#: os parâmetros podem ir todos vazios — conferido ao vivo em 16/08/2026, as
#: duas formas devolvem as mesmas 4.162 edições. Vazio é melhor: não amarra o
#: catálogo a uma edição específica que um dia sai do ar.
URL_INDICE = f'{URL_BASE}/cabecalho.do?cdVolume=&nuDiario=&cdCaderno=&nuSeqpagina=&dtDiario='
URL_HOME = f'{URL_BASE}/index.do'
URL_CADERNO = f'{URL_BASE}/downloadCaderno.do'
URL_VISUALIZADOR = f'{URL_BASE}/consultaSimples.do'

#: âncora que só existe quando a resposta é o cabeçalho de verdade. Sem ela,
#: qualquer página de erro do JBoss com HTTP 200 passaria por catálogo vazio —
#: e catálogo vazio num backfill é lacuna invisível, não erro.
ANCORA_INDICE = 'var diarios'

_RE_EDICAO = re.compile(
    r"dtPublicacao:\s*'(?P<data>\d{4}-\d{2}-\d{2})'\s*,\s*"
    r"cdVolume:\s*(?P<volume>\d+)\s*,\s*"
    r"nuDiario:\s*(?P<diario>\d+)"
)
_RE_SEM_DIARIO = re.compile(r'var\s+datasSemDiario\s*=\s*\[(?P<corpo>.*?)\]\s*;', re.S)
_RE_DATA_JS = re.compile(r"'\w{3}\s+(?P<mes>\w{3})\s+(?P<dia>\d{1,2})\s[\d:]+\s[^']*?(?P<ano>\d{4})'")
_MESES_JS = {m: i for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], start=1)}

_RE_SELECT_CADERNOS = re.compile(
    r'<select[^>]*name="cadernosCad"[^>]*>(?P<corpo>.*?)</select>', re.S | re.I)
_RE_OPTION = re.compile(r'<option[^>]*value="(?P<cd>-?\d+)"[^>]*>(?P<rotulo>.*?)</option>', re.S | re.I)

#: Tabela medida em 16/08/2026 no `<select name="cadernosCad">` do index.do.
#: Existe como FALLBACK, não como verdade: o coletor tenta ler a lista da
#: própria home a cada catálogo. O motivo de guardar uma cópia é que o
#: catálogo não pode parar de funcionar porque a home mudou de layout — e o
#: conjunto de cadernos é estável desde 2007 (o que muda é QUAIS existem em
#: cada data, e isso só o download responde).
CADERNOS_PADRAO: dict[int, str] = {
    10: 'caderno 1 - Administrativo',
    11: 'Caderno 2 - Judicial - 2ª Instância - Entrada e Distribuição - Parte I',
    19: 'Caderno 2 - Judicial - 2ª Instância - Processamento - Parte II',
    12: 'caderno 3 - Judicial - 1ª Instância - Capital - Parte I',
    20: 'caderno 3 - Judicial - 1ª Instância - Capital - Parte II',
    18: 'caderno 4 - Judicial - 1ª Instância - Interior - Parte I',
    13: 'caderno 4 - Judicial - 1ª Instância - Interior - Parte II',
    15: 'caderno 4 - Judicial - 1ª Instância - Interior - Parte III',
    14: 'caderno 5 - Editais e Leilões',
}

#: HTML de erro do e-SAJ que vem com HTTP 200, content-type text/html e 851
#: bytes. É o "caderno que não existe naquela data" — e num backfill de 4.162
#: edições x 9 cadernos ele é a resposta MAIS comum depois do PDF.
MARCA_CADERNO_INEXISTENTE = 'Erro ao acessar o caderno selecionado'


@dataclass(frozen=True)
class EdicaoIndice:
    """Uma linha do `var diarios`: a coordenada de uma edição no e-SAJ."""
    data: date
    cd_volume: int
    nu_diario: int


def parse_indice(html: str) -> list[EdicaoIndice]:
    """Extrai as edições do HTML do `cabecalho.do`, da mais nova para a mais antiga.

    Levanta `RespostaInvalida` se a âncora não estiver lá — o e-SAJ responde 200
    para praticamente tudo, então status code aqui não prova nada.
    """
    exigir_ancora(html, ANCORA_INDICE, contexto='cabecalho.do')
    edicoes = [
        EdicaoIndice(date.fromisoformat(m.group('data')),
                     int(m.group('volume')), int(m.group('diario')))
        for m in _RE_EDICAO.finditer(html)
    ]
    if not edicoes:
        raise RespostaInvalida('cabecalho.do: `var diarios` presente mas sem nenhuma edição')
    return edicoes


def parse_datas_sem_diario(html: str) -> set[date]:
    """Datas que a PRÓPRIA fonte declara sem edição (feriado forense, recesso).

    É gabarito de graça para o `UnidadeInexistente`: em vez de descobrir o
    feriado gastando 9 downloads que voltam 851 bytes, o catálogo já nem cria a
    unidade. As datas vêm no formato `toString()` do Date do Java
    ('Wed Jul 23 00:00:00 GMT-03:00 2025'), em inglês.
    """
    m = _RE_SEM_DIARIO.search(html or '')
    if not m:
        return set()
    fora = set()
    for d in _RE_DATA_JS.finditer(m.group('corpo')):
        mes = _MESES_JS.get(d.group('mes'))
        if mes:
            fora.add(date(int(d.group('ano')), mes, int(d.group('dia'))))
    return fora


def parse_cadernos(html: str) -> dict[int, str]:
    """Lê a tabela de cadernos do `<select name="cadernosCad">` da home.

    O `-11` ("Pesquisar em todos os cadernos") é da tela de busca e não é
    caderno — fica de fora. Se o select sumir (mudança de layout), devolve
    vazio e quem chama cai no `CADERNOS_PADRAO`.
    """
    sel = _RE_SELECT_CADERNOS.search(html or '')
    if not sel:
        return {}
    cadernos = {}
    for o in _RE_OPTION.finditer(sel.group('corpo')):
        cd = int(o.group('cd'))
        if cd < 0:
            continue
        cadernos[cd] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', o.group('rotulo'))).strip()
    return cadernos


def chave_unidade(nu_diario: int, cd_caderno: int) -> str:
    """`EdicaoDiario.chave` — determinística e reconstruível sem estado.

    Usa nuDiario (e não a data) porque é o identificador da edição NA fonte: a
    data pode aparecer em dois formatos e o nuDiario é o que o e-SAJ usa nas
    URLs do visualizador.
    """
    return f'{nu_diario}-{cd_caderno}'


def url_download_caderno(quando: date, cd_caderno: int) -> str:
    """`downloadCaderno.do` — 1 requisição = 1 caderno inteiro (até 2.001 páginas).

    A data vai em DD/MM/AAAA (o e-SAJ ignora o nuDiario aqui; a coordenada do
    download é data+caderno).
    """
    return (f'{URL_CADERNO}?dtDiario={quando.strftime("%d/%m/%Y")}'
            f'&cdCaderno={cd_caderno}&tpDownload=D')


def url_pagina_humana(cd_volume: int, nu_diario: int, cd_caderno: int, pagina: int) -> str:
    """Permalink da página no visualizador do e-SAJ — vai no `Movimentacao.link`.

    É a casca `<frameset>` (1.207 bytes) quando baixada por robô, mas é a URL
    certa para um humano conferir o verbatim no diário oficial, que é para isso
    que o campo serve.
    """
    return (f'{URL_VISUALIZADOR}?cdVolume={cd_volume}&nuDiario={nu_diario}'
            f'&cdCaderno={cd_caderno}&nuSeqpagina={pagina}')
