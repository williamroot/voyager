"""PDF do caderno → linhas COM O TAMANHO DA FONTE.

Por que não `page.get_text()` e pronto: o caderno é um fluxo contínuo de
texto de 567 a 4.229 páginas, e a única coisa que separa "título de seção"
(o foro, a vara) de "corpo do ato" e de "mobília da página" (cabeçalho,
rodapé, número da página) é o TAMANHO DA FONTE. O texto achatado joga essa
informação fora — aí a segmentação vira adivinhação de regex sobre linha
solta, que é exatamente o erro que cola o texto de um processo no do vizinho.

Medido nos cadernos de 2009, 2012, 2015 e 2025 (quatro gerações do gerador de
PDF do e-SAJ, versões 1.4 e 1.5 do formato), a escala é a MESMA:

    16.0  agrupamento  ('Fóruns Centrais', 'PRIMEIRA INSTÂNCIA')
    12.0  foro         ('Fórum João Mendes Júnior', 'Varas Cíveis Centrais')
    11.0  vara/unidade ('1ª Vara Cível', 'Distribuidor Cível')
     9.0  capa do caderno (só na página 1: 'caderno 3', 'JUDICIAL - 1ª INSTÂNCIA')
     8.0  CORPO — é aqui que estão as publicações
     7.0  rodapé lateral e número da página
     5.5  cabeçalho de página ('Disponibilização: ... Edição 4246')

Mesmo assim o tamanho do corpo NÃO é constante hardcoded: é medido por caderno
(a moda dos tamanhos), porque 18 anos de arquivo é tempo demais para apostar
num número. `> corpo` é título; `< corpo` é mobília e vai fora.

POR QUE MuPDF E NÃO pypdf (medido em 20/08/2026, não estimado)
--------------------------------------------------------------
O caderno 3 (Capital Parte I) de 12/03/2025 — 36 MB, 4.229 páginas — extraído
pelos três candidatos, contando CNJ DISTINTO com a regex ESTRITA:

    PyMuPDF (MuPDF/C)   16,3 s   32.366 CNJs
    pdftotext (poppler) 17,3 s   32.366 CNJs
    pypdf 5.1.0        184,0 s   27.483 CNJs   ← perde 15,1%

A causa dos 15,1% está MEDIDA: o pypdf insere um espaço espúrio no meio do
próprio número ('1127986- 08.2023.8.26.0100'), e o número quebrado não casa
com a regex. Nas MESMAS 600 páginas: 1.198 CNJs quebrados pelo pypdf contra
314 pelo MuPDF. O mesmo defeito é o que produzia 'Estado de S ão Paulo' e
'Banco Rodoben s S/A' no `texto` verbatim — 'S ão' 27 ocorrências → 0,
'Paul o' 579 → 0 no caderno de 21/07/2025. Ou seja: o espaço espúrio NÃO era
do PDF (como este módulo afirmava até aqui), era do extrator.

A honestidade que o commit exige: DENTRO deste pipeline os 15,1% NÃO se
materializam, porque a `CNJ_TOLERANTE` de `diarios/base.py` já absorve o
espaço espúrio. Rodando o SEGMENTADOR REAL sobre o mesmo caderno de 4.229
páginas, os dois extratores acham os mesmos 32.575 CNJs, fecham os mesmos
29.106 blocos e dão a mesma cobertura (99,751%) — 0 CNJ de diferença nos dois
sentidos. Vender esta troca como "recupera 15% de processos" seria mentira.

O ganho real, medido no mesmo caderno com o segmentador real, é outro e é
grande:

    tempo de segmentação   192,1 s → 30,1 s   (6,4×)
    pico de RSS            406 MB → 125 MB    (3,2×)
    CNJ com espaço no meio 516 (9,84%) → 215 (4,10%) — e os 215 que sobram
                           são quebra de LINHA real ('0002419-\\n32...'), não
                           kerning: são verdade impressa, não defeito

Os 6,4× decidem o backfill: o caderno baixa em 0,2-1,2 s e o `rps` é 1,0, então
o gargalo desta fonte é CPU, não rede. 4.162 edições × ~9 cadernos ≈ 37 mil
cadernos = ~2.000 h de CPU a 192 s/caderno contra ~310 h a 30 s.

Recusados, e por quê: `pdftotext` não devolve tamanho de fonte nenhum (sem ele
não há hierarquia de seção, que é a tese deste módulo); `pypdfium2` (BSD, o que
resolveria a licença AGPL do PyMuPDF) lê rápido mas `FPDFText_GetFontSize`
devolve 1.0 para TODOS os caracteres deste PDF — a mesma armadilha do `Tf=1`
do e-SAJ. Ver ADR-031.
"""

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

from diarios.base import RespostaInvalida

logger = logging.getLogger('voyager.diarios.tjsp_dje')

#: quantas páginas bastam para medir a moda do tamanho do corpo. 12 páginas
#: dão dezenas de milhares de caracteres e evitam a página 1 (capa), que é a
#: única com tipografia diferente.
PAGINAS_AMOSTRA_TAMANHO = 12


@dataclass(frozen=True)
class Linha:
    """Uma linha física do caderno, com o tamanho de fonte em que foi impressa."""
    pagina: int          # 1-based, igual ao `nuSeqpagina` do e-SAJ
    tamanho: float
    texto: str


@dataclass
class Pagina:
    numero: int
    linhas: list[Linha] = field(default_factory=list)

    @property
    def texto(self) -> str:
        return '\n'.join(linha.texto for linha in self.linhas)


def abrir(corpo: bytes):
    """Abre o PDF já validado por `exigir_pdf`. Import tardio do pymupdf de
    propósito: se a imagem estiver sem a dependência, o erro tem que aparecer na
    COLETA (com mensagem clara), não na subida do Django — lá o
    `diarios/apps.py` engoliria a exceção e a fonte sumiria do registro em
    silêncio.

    Quem abre é quem fecha: o `Document` do MuPDF segura memória FORA do heap
    do Python, então o chamador tem que passar por `fechar()` (ou usar
    `paginas()`, que já fecha no `finally`).
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover — ambiente sem a dependência
        raise RuntimeError(
            'pymupdf ausente: o coletor do DJE/TJSP precisa dele para ler o caderno '
            '(já declarado em requirements.txt como pymupdf==1.24.14). '
            'A imagem precisa de rebuild — não basta git pull.'
        ) from exc
    try:
        return pymupdf.open(stream=corpo, filetype='pdf')
    except pymupdf.FileDataError as exc:
        # PDF truncado/corrompido. Sem este mapeamento a mensagem do MuPDF
        # ('cannot recognize version marker') vira `falha` genérica e o runner
        # retenta 5× um caderno que nunca vai abrir.
        raise RespostaInvalida(f'caderno não abre como PDF: {exc}') from exc


def fechar(leitor) -> None:
    """Libera o `Document` do MuPDF, tolerando quem não tem `close` (os testes
    injetam dublês). Recurso nativo não é coletado pelo GC do Python na hora —
    num backfill de 37 mil cadernos isso é a diferença entre 125 MB e OOM."""
    fechar_ = getattr(leitor, 'close', None)
    if callable(fechar_):
        fechar_()


def _linhas_da_pagina(pagina_pdf, numero: int) -> list[Linha]:
    """Reagrupa os pedaços de texto do PDF em linhas, pela coordenada Y.

    Por que `get_text('dict')` e não `get_text('blocks')`: 'blocks' devolve o
    texto já colado, SEM o tamanho da fonte — que é a única informação que
    separa título de corpo aqui. 'dict' desce até o `span`, que traz
    `size` (o corpo de fonte efetivo, já com a escala do text matrix aplicada —
    é por isso que ele funciona onde o `Tf` do e-SAJ, sempre 1, não dizia nada)
    e `origin` (x, y da linha de base).

    O agrupamento por Y continua sendo NOSSO, e não o do MuPDF, porque a grade
    de distribuição vem em DUAS COLUNAS: só a fusão por linha de base mantém
    'PROCESSO :...' e o valor na mesma linha. Pelo mesmo motivo `sort=False` —
    `sort=True` reordena para ordem de leitura e desmonta o formato `lista`.

    ARMADILHA que custou a troca: o pypdf ordenava por `tm[5]` DECRESCENTE
    (espaço PDF, Y cresce para cima); no MuPDF `origin[1]` cresce para BAIXO,
    então a ordenação é CRESCENTE. Copiar o `reverse=True` leria o caderno de
    baixo para cima e colaria o despacho de um processo no cabeçalho do
    vizinho — erro silencioso e plausível, que só o gate de cobertura pegaria.
    """
    baldes: dict[float, list[tuple[float, float, str]]] = defaultdict(list)
    for bloco in pagina_pdf.get_text('dict', sort=False)['blocks']:
        if bloco.get('type') != 0:      # 1 = imagem; o caderno tem brasão e selo
            continue
        for linha in bloco['lines']:
            for span in linha['spans']:
                if not span['text'].strip():
                    continue
                x, y = span['origin']
                baldes[round(y, 1)].append((x, round(span['size'], 1), span['text']))

    linhas = []
    for y in sorted(baldes):
        # dentro da linha, a ordem é a horizontal — o MuPDF entrega o span na
        # ordem de desenho, que na grade de distribuição não é a de leitura.
        pedacos = sorted(baldes[y], key=lambda p: p[0])
        texto = ''.join(p[2] for p in pedacos).rstrip()
        if not texto.strip():
            continue
        linhas.append(Linha(pagina=numero, tamanho=max(p[1] for p in pedacos), texto=texto))
    return linhas


def tamanho_do_corpo(leitor, amostra: int = PAGINAS_AMOSTRA_TAMANHO) -> float:
    """Moda do tamanho de fonte = tamanho do corpo do caderno.

    Medir em vez de fixar 8.0 é o que faz o mesmo código valer para 2007 e para
    2025 sem `if ano <`.
    """
    contagem: Counter = Counter()
    for i in range(min(amostra, leitor.page_count)):
        for linha in _linhas_da_pagina(leitor[i], i + 1):
            contagem[linha.tamanho] += len(linha.texto)
    if not contagem:
        raise RespostaInvalida('caderno sem camada de texto extraível nas primeiras páginas')
    return contagem.most_common(1)[0][0]


def paginas(corpo: bytes) -> Iterator[Pagina]:
    """Itera as páginas do caderno já em linhas. Streaming: um caderno de
    4.229 páginas dá ~33 MB de texto e não cabe confortavelmente numa lista.

    O `finally` não é zelo: o gate de cobertura levanta `ColetorError` NO MEIO
    desta iteração, e o consumidor abandona o generator. Sem ele, o `Document`
    (memória nativa, fora do heap) ficaria pendurado até o GC — 37 mil vezes,
    num worker que também roda extração e vetorização.
    """
    doc = abrir(corpo)
    try:
        for i in range(doc.page_count):
            yield Pagina(numero=i + 1, linhas=_linhas_da_pagina(doc[i], i + 1))
    finally:
        fechar(doc)


# ─────────────────────────────────────────────────────────────────────────────
# Conferência da data — o download é por data, mas quem garante que veio a
# data certa é o cabeçalho impresso em toda página.
# ─────────────────────────────────────────────────────────────────────────────
_MESES = {'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3, 'abril': 4, 'maio': 5,
          'junho': 6, 'julho': 7, 'agosto': 8, 'setembro': 9, 'outubro': 10,
          'novembro': 11, 'dezembro': 12}
_RE_DISPONIBILIZACAO = re.compile(
    r'Disponibiliza[çc][ãa]o:\s*[\wÀ-ú-]+,?\s*(?P<dia>\d{1,2})\s+de\s+(?P<mes>[\wÀ-ú]+)\s+de\s+(?P<ano>\d{4})',
    re.I)


def data_impressa(pagina: Pagina) -> date | None:
    """Data que o caderno declara no cabeçalho ('Disponibilização: segunda-feira,
    21 de julho de 2025'). Devolve None se a página não tiver cabeçalho — a
    página 1 de algumas edições não tem."""
    for linha in pagina.linhas:
        m = _RE_DISPONIBILIZACAO.search(linha.texto)
        if not m:
            continue
        mes = _MESES.get(m.group('mes').lower())
        if mes:
            return date(int(m.group('ano')), mes, int(m.group('dia')))
    return None


def conferir_data(pagina: Pagina, esperada: date) -> None:
    """Falha alto quando o caderno baixado não é o do dia pedido.

    Parece paranoia até lembrar que o `downloadCaderno.do` é um GET sem estado
    contra um servidor que responde HTTP 200 para tudo: um redirect interno, um
    cache errado ou uma mudança silenciosa no significado de `dtDiario` gravaria
    milhares de movimentações com a data errada — e dado com data errada é pior
    que dado ausente, porque ninguém desconfia.
    """
    impressa = data_impressa(pagina)
    if impressa is not None and impressa != esperada:
        raise RespostaInvalida(
            f'caderno pedido para {esperada} veio com "Disponibilização: {impressa}" impressa'
        )
