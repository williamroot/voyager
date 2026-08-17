"""PDF do caderno → linhas COM O TAMANHO DA FONTE.

Por que não `page.extract_text()` e pronto: o caderno é um fluxo contínuo de
texto de 567 a 2.001 páginas, e a única coisa que separa "título de seção"
(o foro, a vara) de "corpo do ato" e de "mobília da página" (cabeçalho,
rodapé, número da página) é o TAMANHO DA FONTE. `extract_text()` joga essa
informação fora e devolve tudo achatado — aí a segmentação vira adivinhação de
regex sobre linha solta, que é exatamente o erro que cola o texto de um
processo no do vizinho.

Medido nos cadernos de 2009, 2012, 2015 e 2025 (quatro gerações do gerador de
PDF do e-SAJ, versões 1.4 e 1.5 do formato), a escala é a MESMA:

    16.0  agrupamento  ('Fóruns Centrais', 'PRIMEIRA INSTÂNCIA')
    12.0  foro         ('Fórum João Mendes Júnior', 'Varas Cíveis Centrais')
    11.0  vara/unidade ('1ª Vara Cível', 'Distribuidor Cível')
     9.0  capa do caderno (só na página 1: 'caderno 3', 'JUDICIAL - 1ª INSTÂNCIA')
     8.0  CORPO — é aqui que estão as publicações
     7.0  rodapé lateral ('Publicação Oficial do Tribunal de Justiça...')
     5.5  cabeçalho de página ('Disponibilização: ... Edição 4246')
     1.0  número da página

Mesmo assim o tamanho do corpo NÃO é constante hardcoded: é medido por caderno
(a moda dos tamanhos), porque 18 anos de arquivo é tempo demais para apostar
num número. `> corpo` é título; `< corpo` é mobília e vai fora.

O QUE ESTE MÓDULO **NÃO** CONSERTA
----------------------------------
O espaço espúrio no meio da palavra ('São Paul o', 'Banco Rodoben s S/A'). Ele
vem do próprio PDF: o texto é justificado com ajuste de kerning por caractere e
o deslocamento vai no array do operador `TJ`; qualquer extrator que respeite
posição vai inserir espaço ali. Conferido: `space_width` não muda nada (o
limiar real vem da largura do espaço da fonte, não do parâmetro) e
`extraction_mode='layout'` é 2,6x mais lento e não resolve — no caderno 12 de
21/07/2025, 4.727 CNJs pela regex estrita contra 5.243 pela tolerante nos DOIS
modos. Consertar isso "juntando" as palavras seria chutar em cima de texto
oficial; o `texto` fica verbatim e a defesa é a `CNJ_TOLERANTE` do
`diarios/base.py`.
"""

import io
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
    """Abre o PDF já validado por `exigir_pdf`. Import tardio do pypdf de
    propósito: se a imagem estiver sem a dependência, o erro tem que aparecer na
    COLETA (com mensagem clara), não na subida do Django — lá o
    `diarios/apps.py` engoliria a exceção e a fonte sumiria do registro em
    silêncio."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover — ambiente sem a dependência
        raise RuntimeError(
            'pypdf ausente: o coletor do DJE/TJSP precisa dele para ler o caderno '
            '(já declarado em requirements.txt como pypdf==5.1.0)'
        ) from exc
    return PdfReader(io.BytesIO(corpo))


def _linhas_da_pagina(pagina_pdf, numero: int) -> list[Linha]:
    """Reagrupa os pedaços de texto do PDF em linhas, pela coordenada Y.

    O visitor do pypdf é chamado por trecho desenhado, não por linha. Como toda
    linha do caderno é desenhada num único Y, agrupar por Y (arredondado a uma
    casa) reconstrói a linha; a ordem de leitura é de cima para baixo, que é a
    ordem do fluxo do diário.
    """
    baldes: dict[float, list[tuple[float, str]]] = defaultdict(list)

    def visitar(texto, _cm, tm, _fonte, tamanho_fonte):
        if not texto or not texto.strip():
            return
        # tm[0] é a escala horizontal do texto — na prática o corpo de fonte
        # efetivo. `Tf` no e-SAJ é sempre 1, então `tamanho_fonte` sozinho não
        # discrimina nada (vem 1.0 em toda linha).
        baldes[round(tm[5], 1)].append((round(abs(tm[0]) or tamanho_fonte, 1), texto))

    pagina_pdf.extract_text(visitor_text=visitar)

    linhas = []
    for y in sorted(baldes, reverse=True):
        pedacos = baldes[y]
        texto = ''.join(p[1] for p in pedacos).rstrip()
        if not texto.strip():
            continue
        linhas.append(Linha(pagina=numero, tamanho=max(p[0] for p in pedacos), texto=texto))
    return linhas


def tamanho_do_corpo(leitor, amostra: int = PAGINAS_AMOSTRA_TAMANHO) -> float:
    """Moda do tamanho de fonte = tamanho do corpo do caderno.

    Medir em vez de fixar 8.0 é o que faz o mesmo código valer para 2007 e para
    2025 sem `if ano <`.
    """
    contagem: Counter = Counter()
    for i, pagina_pdf in enumerate(leitor.pages):
        if i >= amostra:
            break
        for linha in _linhas_da_pagina(pagina_pdf, i + 1):
            contagem[linha.tamanho] += len(linha.texto)
    if not contagem:
        raise RespostaInvalida('caderno sem camada de texto extraível nas primeiras páginas')
    return contagem.most_common(1)[0][0]


def paginas(corpo: bytes) -> Iterator[Pagina]:
    """Itera as páginas do caderno já em linhas. Streaming: um caderno de
    2.001 páginas dá ~16 MB de texto e não cabe confortavelmente numa lista."""
    leitor = abrir(corpo)
    for i, pagina_pdf in enumerate(leitor.pages):
        yield Pagina(numero=i + 1, linhas=_linhas_da_pagina(pagina_pdf, i + 1))


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
