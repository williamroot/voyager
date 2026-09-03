"""Do PDF do caderno para as matérias — a parte cara do DEJT.

O download é barato (25 cadernos = 519 MB em 34 s). O trabalho é aqui: virar
13.853 páginas em 16.717 matérias sem atribuir advogado errado a processo.

O QUE A SONDA MEDIU E QUE TORNA ISTO VIÁVEL
-------------------------------------------
· Camada de texto NATIVA: 0 de 13.853 páginas sem texto no TRT3 de 10/07/2024
  (Producer 'iText 2.0.7'). Nunca foi scan, não precisa de OCR.
· O PDF traz o PRÓPRIO índice de segmentação no outline (479 nós no TRT3):
  Unidade Publicadora → Tipo de Matéria → página. É de onde saem `nome_orgao`
  e `tipo_comunicacao` — dado declarado pela fonte, não inferido por nós.
· A armadilha do layout multi-coluna (que estraga o e-SAJ) NÃO se confirmou
  aqui: o iText escreve coluna a coluna, e o `extract_text` do pypdf devolve os
  blocos íntegros. Conferido verbatim no TRT22 (`ADVOGADO ... (OAB: 3596/PI)`
  logo abaixo do seu `REQUERENTE`), e é o que autoriza casar parte↔advogado por
  adjacência: a adjacência é da FONTE, não uma inferência nossa.

AS DUAS ÂNCORAS, E POR QUE SÃO DUAS
-----------------------------------
(1) `Processo Nº <sigla>-<CNJ>` — a matéria padrão (notificação, despacho,
    acórdão, edital). Contadas: 16.956 no TRT3 de 10/07/2024 contra as 16.717
    que a pesquisa avançada do próprio DEJT declara (101,4%), e 890 contra 885
    no TRT22 do mesmo dia (100,6%). O gabarito é da fonte e é mecânico.
(2) `<sigla> <CNJ>` em linha isolada — o formato da seção **Distribuição**, que
    é UMA matéria contendo milhares de processos recém-distribuídos, cada um com
    vara, autor, réu e advogados. Medido no TRT3: 2.566 CNJs do caderno NÃO
    aparecem no cabeçalho de nenhum bloco `Processo Nº`, e 1.412 deles estão
    nessa lista. Ignorá-la seria jogar fora justamente os processos NOVOS, que
    são o motivo desta missão existir.
    Esta âncora só é aplicada DENTRO de seção cujo tipo é 'Distribuição'
    (declarado no outline) — fora dela ela não é ligada, para não confundir uma
    citação solta com um ato.

ONDE ESTE ARQUIVO SE ABSTÉM (de propósito)
------------------------------------------
· `codigo_classe`/`nome_classe`: o caderno traz a SIGLA colada no CNJ ('ATOrd',
  'ROT', 'AIRR'), não o código da tabela de classes do CNJ. Escrever 'ROT' em
  `nome_classe` contaminaria `Process.classe_nome` (o
  `preencher_classe_via_djen` propaga esse campo) com um acrônimo. A sigla fica
  VERBATIM no `texto` e em `Bloco.sigla_classe`, e o mapeamento fica para quem
  tiver a tabela do SGT.
· `polo` de papéis recursais (RECORRENTE/AGRAVANTE/...): num recurso, quem
  recorre tanto pode ser o autor quanto o réu. Chutar 'A' aqui é exatamente o
  erro do Falcon que a casa proíbe. O `papel` vai verbatim; o `polo` fica vazio.
· Blocos sem CNJ no cabeçalho: não recebem CNJ "achado no corpo". O corpo cita
  outros processos, e emprestar um deles ao ato é atribuição errada. Bloco sem
  CNJ é contado e descartado.
"""

import bisect
import io
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from diarios.base import RespostaInvalida, achar_cnjs

logger = logging.getLogger('voyager.diarios.dejt')

# ─────────────────────────────────────────────────────────────────────────────
# Mobília de página — some do texto, mas não do verbatim do ato
# ─────────────────────────────────────────────────────────────────────────────
# Toda página repete cabeçalho (edição + tribunal + nº da página, e a linha
# 'Data da Disponibilização') e rodapé ('Código para aferir autenticidade...').
# Um ato que atravessa 3 páginas engoliria essas 9 linhas no meio da frase.
# Removê-las é limpeza de MOBÍLIA, não edição de conteúdo — e o cabeçalho só é
# removido quando as DUAS linhas aparecem juntas, que é a assinatura dele.
RE_CABECALHO_1 = re.compile(r'^\s*\d{1,6}/\d{4}\s+.{3,120}?\s+\d{1,6}\s*$')
RE_CABECALHO_2 = re.compile(r'^\s*Data da Disponibiliza[çc][ãa]o\s*:', re.IGNORECASE)
RE_RODAPE = re.compile(r'^\s*C[óo]digo para aferir autenticidade deste caderno\s*:\s*\d+\s*$',
                       re.IGNORECASE)

#: A capa tem que dizer que é DEJT. É a validação de CONTEÚDO do download —
#: status 200 não prova nada (ver `diarios/base.py::RespostaInvalida`).
RE_MARCA_DEJT = re.compile(r'DIARIO ELETRONICO DA JUSTICA DO TRABALHO', re.IGNORECASE)
RE_EDICAO_CAPA = re.compile(r'N[º°o]\s*(\d{1,6})\s*/\s*(\d{4})')

# ─────────────────────────────────────────────────────────────────────────────
# Âncoras de bloco
# ─────────────────────────────────────────────────────────────────────────────
RE_ANCORA_PROCESSO = re.compile(r'(?m)^[ \t]*Processo\s+N[º°o]\b')
_CNJ_TRABALHISTA = r'\d{7}-\d{2}\.\d{4}\.5\.\d{2}\.\d{4}'
RE_ANCORA_DISTRIBUICAO = re.compile(r'(?m)^[ \t]*[A-Za-zÇç]{2,10}[ \t]+' + _CNJ_TRABALHISTA + r'[ \t]*$')
RE_SIGLA_CLASSE = re.compile(r'(?m)^[ \t]*(?:Processo\s+N[º°o]\s*)?([A-Za-zÇç]{2,10})\s*[- ]\s*\d{7}-')
RE_INTIMADOS = re.compile(r'Intimado\(s\)\s*/\s*Citado\(s\)\s*:', re.IGNORECASE)
TIPO_DISTRIBUICAO = 'distribuicao'

# ─────────────────────────────────────────────────────────────────────────────
# Formatos de bloco — existem por causa do GATE, não do parsing
# ─────────────────────────────────────────────────────────────────────────────
#: Cada âncora do caderno tem um balde EXCLUSIVO, e é essa exclusividade que faz
#: a perna A do segundo eixo (`diarios/inventario.py`) morder: se as duas
#: âncoras caíssem no mesmo balde, `blocos_do_formato >= registros_do_marcador`
#: passaria com a Distribuição inteira perdida, porque as ~900 matérias
#: `Processo Nº` cobririam a conta sozinhas. É exatamente a armadilha que o
#: `FORMATO_PAUTA` do TJSP documenta (DIARIOS.md §18.5, decisão 1).
FORMATO_PROCESSO = 'processo'
FORMATO_DISTRIBUICAO = 'distribuicao'

#: Onde o CNJ do ato pode estar: o CABEÇALHO do bloco, nunca o corpo (o corpo
#: cita outros processos). Mesmo limite usado por `_montar_bloco` e pela
#: classificação do descarte — se um dia divergirem, um bloco seria dado como
#: "sem CNJ" e classificado por um trecho que o outro nem olhou.
LIMITE_CABECALHO = 300

#: Numeração trabalhista PRÉ-CNJ, como o caderno a imprime:
#: 'ROS-02029/2006-002-16-00.5', 'ED/ROS-01011/2012-002-16-00.4'. Não é
#: `Process.numero_cnj` e não há de-para — por isso o bloco é descartado. O que
#: esta regex faz é dar NOME ao descarte, para o gate distinguir "acervo de era
#: que ainda não sabemos ler" de "o parser quebrou".
RE_NUMERO_PRE_CNJ = re.compile(r'\b\d{4,6}[-/]\d{4}[-.]\d{3}[-.]\d{2}[-.]\d{2}\b')

# ─────────────────────────────────────────────────────────────────────────────
# Papéis — vocabulário fechado, verbatim do caderno
# ─────────────────────────────────────────────────────────────────────────────
# Vocabulário FECHADO de propósito: um `^[A-Z ]+` genérico casaria com qualquer
# linha em caixa alta do corpo do ato ('PODER JUDICIÁRIO', 'INTIMAÇÃO') e
# inventaria partes. Aqui, o que não está na lista não vira parte.
POLO_ATIVO = 'A'
POLO_PASSIVO = 'P'
PAPEIS_ATIVOS = {
    'RECLAMANTE', 'AUTOR', 'AUTORA', 'EXEQUENTE', 'REQUERENTE', 'IMPETRANTE',
    'SUSCITANTE', 'EMBARGANTE', 'DEPRECANTE', 'DENUNCIANTE', 'ARREMATANTE',
}
PAPEIS_PASSIVOS = {
    'RECLAMADO', 'RECLAMADA', 'RÉU', 'REU', 'RÉ', 'EXECUTADO', 'EXECUTADA',
    'REQUERIDO', 'REQUERIDA', 'IMPETRADO', 'IMPETRADA', 'SUSCITADO',
    'EMBARGADO', 'EMBARGADA', 'DEPRECADO', 'DENUNCIADO',
}
#: papéis de RECURSO: a fonte não diz de que lado o recorrente está, e nós não
#: vamos adivinhar (ver docstring do módulo).
PAPEIS_SEM_POLO = {
    'RECORRENTE', 'RECORRIDO', 'RECORRIDA', 'AGRAVANTE', 'AGRAVADO', 'AGRAVADA',
    'INTERESSADO', 'INTERESSADA', 'TERCEIRO INTERESSADO', 'LITISCONSORTE',
    'CUSTOS LEGIS', 'PERITO', 'PERITA', 'TESTEMUNHA', 'PACIENTE',
    'AUTORIDADE COATORA', 'ADMINISTRADOR JUDICIAL', 'TERCEIRO',
    'MINISTERIO PUBLICO DO TRABALHO', 'MINISTÉRIO PÚBLICO DO TRABALHO',
}
PAPEIS_ADVOGADO = {'ADVOGADO', 'ADVOGADA', 'PROCURADOR', 'PROCURADORA', 'DEFENSOR', 'DEFENSORA'}
#: linhas de metadado do cabeçalho — não são parte nem advogado.
PAPEIS_METADADO = {'RELATOR', 'RELATORA', 'REVISOR', 'REVISORA', 'COMPLEMENTO',
                   'ÓRGÃO JULGADOR', 'ORGAO JULGADOR'}

_TODOS_PAPEIS = (PAPEIS_ATIVOS | PAPEIS_PASSIVOS | PAPEIS_SEM_POLO
                 | PAPEIS_ADVOGADO | PAPEIS_METADADO)
# Mais longos primeiro: 'TERCEIRO INTERESSADO' tem que ganhar de 'TERCEIRO'.
RE_PAPEL = re.compile(
    r'^[ \t]*(' + '|'.join(re.escape(p) for p in sorted(_TODOS_PAPEIS, key=len, reverse=True))
    + r')(?:\s*[-\u2013]\s*|\s+)(\S.*)$'  # '-' ou en-dash: os dois aparecem no caderno
)

# Os dois formatos de OAB que convivem no mesmo acervo (medidos):
#   'ADVOGADO JESSICA LOURENCO SILVA(OAB: 165467/MG)'
#   'ADVOGADO - FELIPE DOURADO LAGES (OAB/MG 110695)'
RE_OAB_DOIS_PONTOS = re.compile(r'\(\s*OAB\s*:\s*([\w.\-]+)\s*/\s*([A-Za-z]{2})\s*\)')
RE_OAB_BARRA_UF = re.compile(r'\(\s*OAB\s*/\s*([A-Za-z]{2})\s*[:\s]\s*([\w.\-]+)\s*\)')


@dataclass
class Bloco:
    """Uma matéria recortada do caderno."""
    cnj: str
    texto: str                       # VERBATIM (só sem a mobília de página)
    pagina: int                      # 1-based, como o caderno numera
    unidade: str = ''                # unidade publicadora → nome_orgao
    tipo: str = ''                   # tipo de matéria → tipo_comunicacao
    subtipo: str = ''                # → tipo_documento
    sigla_classe: str = ''           # 'ATOrd', 'ROT'... NÃO é classe CNJ
    partes: list[dict] = field(default_factory=list)
    advogados: list[dict] = field(default_factory=list)
    #: qual âncora produziu este bloco (`FORMATO_PROCESSO`/`FORMATO_DISTRIBUICAO`).
    #: NÃO é dado da publicação: é o que o segundo eixo do gate confere contra o
    #: que a fonte imprimiu. Ver `diarios/inventario.py`.
    formato: str = FORMATO_PROCESSO


# ─────────────────────────────────────────────────────────────────────────────
# 1. PDF → páginas
# ─────────────────────────────────────────────────────────────────────────────
def _sem_acento(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '')
                   if unicodedata.category(c) != 'Mn')


def limpar_mobilia(texto_pagina: str) -> str:
    linhas = (texto_pagina or '').split('\n')
    saida = []
    i = 0
    while i < len(linhas):
        atual = linhas[i]
        proxima = linhas[i + 1] if i + 1 < len(linhas) else ''
        if RE_CABECALHO_1.match(atual) and RE_CABECALHO_2.match(proxima):
            i += 2
            continue
        if RE_RODAPE.match(atual):
            i += 1
            continue
        saida.append(atual)
        i += 1
    return '\n'.join(saida)


def ler_caderno(corpo: bytes) -> tuple[list[str], list[tuple[int, str, str]]]:
    """PDF → (texto por página sem mobília, seções do outline).

    Seções vêm como (índice da página, unidade publicadora, tipo de matéria),
    na ordem do documento. Caderno sem outline devolve lista vazia — e aí
    `nome_orgao`/`tipo_comunicacao` ficam em branco, que é a abstenção correta.
    """
    # Import tardio e embrulhado, igual ao `tjsp_dje/pdf.py`: se a imagem
    # estiver sem a dependência, o erro tem que aparecer na COLETA com mensagem
    # clara, e não como `ModuleNotFoundError` cru dentro de um worker. (A imagem
    # `voyagerdev-web` de 16/08/2026 NÃO tinha pypdf, embora o requirements.txt
    # já declare pypdf==5.1.0 — é rebuild pendente, não código.)
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover — ambiente sem a dependência
        raise RuntimeError(
            'pypdf ausente: o coletor do DEJT precisa dele para ler o caderno '
            '(já declarado em requirements.txt como pypdf==5.1.0 — falta rebuild da imagem)'
        ) from exc

    leitor = pypdf.PdfReader(io.BytesIO(corpo))
    paginas = [limpar_mobilia(p.extract_text() or '') for p in leitor.pages]
    return paginas, _secoes_do_outline(leitor)


def _secoes_do_outline(leitor) -> list[tuple[int, str, str]]:
    # `get_destination_page_number` varre a lista de páginas a cada chamada; num
    # caderno de 13.853 páginas com 479 nós isso é O(n·m). O mapa idnum→índice
    # resolve em uma passada.
    try:
        por_id = {p.indirect_reference.idnum: i for i, p in enumerate(leitor.pages)}
        raiz = leitor.outline
    except Exception:
        logger.warning('caderno sem outline legível — nome_orgao/tipo ficarão vazios')
        return []

    secoes: list[tuple[int, str, str]] = []
    unidade_corrente = ['']

    def andar(itens, nivel=0):
        for item in itens:
            if isinstance(item, list):
                andar(item, nivel + 1)
                continue
            try:
                pagina = por_id.get(item.page.idnum)
            except Exception:
                pagina = None
            titulo = (item.title or '').strip()
            # nível 0 = tribunal (e o destino dele é a página do índice, no fim
            # do caderno — inútil). nível 1 = unidade publicadora. nível 2 = tipo.
            if nivel == 1:
                unidade_corrente[0] = titulo
            elif nivel >= 2 and pagina is not None:
                secoes.append((pagina, unidade_corrente[0], titulo))

    try:
        andar(raiz)
    except Exception:
        logger.exception('outline ilegível — seguindo sem seções')
        return []
    return secoes


def conferir_capa(paginas: list[str], edicao: str | None = None) -> None:
    """A capa tem que dizer 'DIÁRIO ELETRÔNICO DA JUSTIÇA DO TRABALHO'.

    Sem isto, um PDF qualquer (ou o caderno do dia errado) passa por caderno.
    Quando o catálogo sabe o número da edição, ele também é conferido: é a
    prova de que o postback baixou a LINHA que pedimos, e não a primeira da
    tabela.
    """
    capa = _sem_acento(paginas[0] if paginas else '')
    if not RE_MARCA_DEJT.search(capa):
        raise RespostaInvalida(
            'a capa do PDF não traz "DIÁRIO ELETRÔNICO DA JUSTIÇA DO TRABALHO" '
            f'(primeiros 120 chars: {capa[:120]!r})'
        )
    if edicao:
        achado = RE_EDICAO_CAPA.search(capa)
        if achado and achado.group(1) != str(edicao):
            raise RespostaInvalida(
                f'capa diz edição {achado.group(1)}, catálogo pediu {edicao} — '
                'o postback trouxe outra linha da tabela'
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. páginas → blocos
# ─────────────────────────────────────────────────────────────────────────────
def _colar_paginas(paginas: list[str]) -> tuple[str, list[int]]:
    """Texto contínuo + offset inicial de cada página.

    Colar é obrigatório: um ato atravessa páginas, e recortar por página
    partiria a matéria no meio (e o `texto` deixaria de ser o ato inteiro).
    """
    partes = []
    offsets = []
    pos = 0
    for p in paginas:
        offsets.append(pos)
        partes.append(p)
        pos += len(p) + 1
    return '\n'.join(partes), offsets


def _marcadores(paginas: list[str], offsets: list[int],
                secoes: list[tuple[int, str, str]]) -> list[tuple[int, str, str, str]]:
    """Seções do outline → offsets no texto colado.

    O outline dá a PÁGINA em que a seção começa, não a posição: e uma página
    pode conter o fim de uma seção e o começo de outra (visto no TRT22, pág. 4).
    Como o cabeçalho da seção aparece literalmente no texto ('Secretaria
    Judiciaria' / 'Notificação'), procuramos o rótulo dentro da página, com um
    cursor que só anda para a frente — assim duas seções na mesma página não se
    embaralham. Não achou o rótulo: cai no começo da página (a ordem se mantém,
    o recorte fica grosseiro naquele ponto).
    """
    marcas: list[tuple[int, str, str, str]] = []
    cursor_por_pagina: dict[int, int] = {}
    for pagina, unidade, tipo in secoes:
        if pagina is None or pagina >= len(paginas):
            continue
        texto = paginas[pagina]
        inicio = cursor_por_pagina.get(pagina, 0)
        pos = -1
        if unidade:
            pos_unid = texto.find(unidade, inicio)
            if pos_unid >= 0:
                pos = texto.find(tipo, pos_unid)
        if pos < 0 and tipo:
            pos = texto.find(tipo, inicio)
        if pos < 0:
            pos = inicio
        cursor_por_pagina[pagina] = pos + max(len(tipo), 1)
        marcas.append((offsets[pagina] + pos, unidade, tipo,
                       _subtipo(texto[pos:pos + 400], tipo)))
    marcas.sort(key=lambda m: m[0])
    return marcas


def _secao_em(marcas: list[tuple[int, str, str, str]], pos: int) -> tuple[str, str, str]:
    unidade = tipo = subtipo = ''
    for offset, u, t, s in marcas:
        if offset > pos:
            break
        unidade, tipo, subtipo = u, t, s
    return unidade, tipo, subtipo


def _e_distribuicao(tipo: str) -> bool:
    return TIPO_DISTRIBUICAO in _sem_acento(tipo).lower()


def _pagina_de(offsets: list[int], pos: int) -> int:
    """Página 1-based em que o bloco começa (busca binária)."""
    return max(1, bisect.bisect_right(offsets, pos))


def blocos(paginas: list[str], secoes: list[tuple[int, str, str]],
           descartes: Counter | None = None) -> Iterator[Bloco]:
    """Recorta o caderno em matérias. É a função que o gate mede.

    `descartes` (opcional) recebe a CONTABILIDADE do que foi jogado fora, por
    motivo. Ela existe porque o segundo eixo do gate compara marcadores
    impressos com blocos produzidos, e a diferença precisa ter NOME: sem isso,
    68 matérias de numeração pré-CNJ medidas em 6 cadernos (§ abaixo) e um
    formato novo que o parser não conhece produziriam o mesmo número, e o gate
    não saberia qual dos dois está olhando.

    Baldes:
      · `pre_cnj` — o cabeçalho traz a numeração trabalhista ANTIGA
        ('ROS-02029/2006-002-16-00.5'). É acervo real que existe e que ainda
        não sabemos casar com `Process.numero_cnj`: dívida CONHECIDA, medida em
        100% (68 de 68) dos descartes de TRT16/TRT22 em 2018/2020/2022/2024.
      · `desconhecido` — descartado sem numeração reconhecível de nenhuma era.
        É o que o gate tem que reprovar: ou o cabeçalho mudou, ou há formato
        novo. Medido: **0** nas mesmas 6 edições.
      · `vazio` — âncora que não delimitou corpo nenhum.
    """
    texto, offsets = _colar_paginas(paginas)
    marcas = _marcadores(paginas, offsets, secoes)

    # (offset, formato): o formato viaja com a âncora até o bloco, porque é a
    # âncora — e só ela — que sabe qual registro da fonte deu origem a ele.
    ancoras: list[tuple[int, str]] = []
    for m in RE_ANCORA_PROCESSO.finditer(texto):
        if not _e_distribuicao(_secao_em(marcas, m.start())[1]):
            ancoras.append((m.start(), FORMATO_PROCESSO))
    for m in RE_ANCORA_DISTRIBUICAO.finditer(texto):
        if _e_distribuicao(_secao_em(marcas, m.start())[1]):
            ancoras.append((m.start(), FORMATO_DISTRIBUICAO))
    ancoras.sort()
    if not ancoras:
        return

    # Fronteiras de seção também cortam: sem isso o último bloco de uma seção
    # engoliria o cabeçalho (e o começo) da seção seguinte.
    fronteiras = sorted({m[0] for m in marcas} | {len(texto)})

    sem_cnj = 0
    for i, (inicio, formato) in enumerate(ancoras):
        proxima_ancora = ancoras[i + 1][0] if i + 1 < len(ancoras) else len(texto)
        j = bisect.bisect_right(fronteiras, inicio)
        proxima_fronteira = fronteiras[j] if j < len(fronteiras) else len(texto)
        fim = min(proxima_ancora, proxima_fronteira)
        corpo = texto[inicio:fim].strip('\n')
        if not corpo:
            if descartes is not None:
                descartes['vazio'] += 1
            continue
        unidade, tipo, subtipo = _secao_em(marcas, inicio)
        bloco = _montar_bloco(corpo, _pagina_de(offsets, inicio), unidade, tipo, subtipo,
                              formato=formato)
        if bloco is None:
            # Bloco sem CNJ no cabeçalho é DESCARTADO, não remendado com um CNJ
            # citado no corpo. Fica contado no log para virar dívida visível.
            sem_cnj += 1
            if descartes is not None:
                descartes['pre_cnj' if RE_NUMERO_PRE_CNJ.search(corpo[:LIMITE_CABECALHO])
                          else 'desconhecido'] += 1
            continue
        yield bloco
    if sem_cnj:
        logger.info('dejt: %d de %d blocos descartados por não ter CNJ no cabeçalho',
                    sem_cnj, len(ancoras))


def _montar_bloco(corpo: str, pagina: int, unidade: str, tipo: str,
                  subtipo: str, *, formato: str = FORMATO_PROCESSO) -> Bloco | None:
    # O CNJ vem do CABEÇALHO do bloco (primeiros 300 chars), nunca do corpo:
    # o corpo cita outros processos, e emprestar um deles ao ato é atribuição
    # errada — o erro que a casa proíbe.
    achados = achar_cnjs(corpo[:LIMITE_CABECALHO])
    if not achados:
        return None
    sigla = RE_SIGLA_CLASSE.match(corpo)
    partes, advogados = ler_cabecalho_de_partes(corpo)
    return Bloco(
        cnj=achados[0], texto=corpo, pagina=pagina, unidade=unidade, tipo=tipo,
        subtipo=subtipo,
        sigla_classe=(sigla.group(1) if sigla else ''),
        partes=partes, advogados=advogados, formato=formato,
    )


def _subtipo(trecho: str, tipo: str) -> str:
    """Subtipo ('Pauta de Julgamento' sob 'Pauta', 'Ata de Sessão' sob 'Ata').

    Vive no CABEÇALHO DA SEÇÃO, logo abaixo do tipo — não dentro de cada bloco.
    Só é aceito quando a linha começa com a palavra do tipo: é regra ancorada no
    texto, não o palpite de que "a linha seguinte deve ser o subtipo". Quando
    não bate, fica vazio (o DEJT tem tipo sem subtipo, ex.: 'Notificação').
    """
    if not tipo:
        return ''
    primeira_palavra = tipo.strip().split()[0]
    for linha in trecho.split('\n')[1:4]:
        limpa = linha.strip()
        if not limpa:
            continue
        if limpa.startswith(primeira_palavra) and limpa != tipo.strip() and len(limpa) <= 80:
            return limpa
        break
    return ''


# ─────────────────────────────────────────────────────────────────────────────
# 3. cabeçalho de partes
# ─────────────────────────────────────────────────────────────────────────────
def _tem_minuscula(linha: str) -> bool:
    return any(c.islower() for c in _sem_acento(linha))


def _limpar_nome(bruto: str) -> str:
    # O ponto NÃO entra no strip: 'GOL LINHAS AEREAS S.A.' viraria
    # 'GOL LINHAS AEREAS S.A' e deixaria de casar com a lista de
    # 'Intimado(s)/Citado(s)' — além de deixar de ser o nome que a fonte
    # publicou. Verbatim é regra da casa.
    sem_oab = RE_OAB_DOIS_PONTOS.sub('', RE_OAB_BARRA_UF.sub('', bruto))
    return re.sub(r'\s+', ' ', sem_oab).strip(' ,;-')


def _chave_de_nome(nome: str) -> str:
    """Forma de COMPARAÇÃO (nunca de gravação) entre nome de parte e a lista de
    intimados: só tira acento, pontuação de borda e caixa."""
    return _sem_acento(re.sub(r'\s+', ' ', nome or '')).strip(' .,;-').upper()


def _oab(bruto: str) -> tuple[str, str]:
    m = RE_OAB_DOIS_PONTOS.search(bruto)
    if m:
        return m.group(1), m.group(2).upper()
    m = RE_OAB_BARRA_UF.search(bruto)
    if m:
        return m.group(2), m.group(1).upper()
    return '', ''


def ler_cabecalho_de_partes(corpo: str) -> tuple[list[dict], list[dict]]:
    """Lê a tabela de partes/advogados do topo do bloco.

    O cabeçalho termina em 'Intimado(s)/Citado(s):' ou numa linha em branco que
    NÃO é seguida por outra linha de papel. Parar aí é o que impede o corpo do
    despacho de virar nome de parte.

    POR QUE NÃO "PARA NA PRIMEIRA LINHA EM BRANCO" (correção de 2026-08-16)
    ----------------------------------------------------------------------
    Era essa a regra até a verificação adversarial medi-la no caderno REAL do
    TRT22 de 10/07/2024: 86 dos 999 blocos (8,6%) têm linha em branco DENTRO da
    tabela de partes — resíduo de layout do PDF, não fim de tabela. O custo eram
    379 entradas descartadas em silêncio (226 ADVOGADO, 57 RÉU, 40 RECORRIDO,
    15 AUTOR...) e, em 25 blocos (2,5%), o POLO PASSIVO INTEIRO. Verbatim do
    pior caso, precatório 0086840-73.2023.5.22.0000:

        REQUERENTE ROSA MARIA AGUIAR LANDIM
        ⟨linha com um espaço⟩
        ADVOGADO CLAUDI PINHEIRO DE ARAUJO(OAB: 264/PI)
        REQUERIDO MUNICIPIO DE SIMPLICIO MENDES        ← o ENTE DEVEDOR

    Gravar só a requerente e chamar isso de ficha da parte é fato PARCIAL
    vendido como completo — pior que abstenção, porque ninguém vê. E num
    backfill de 86.587 cadernos é caro de desfazer (re-parse + rewrite).

    A regra nova mantém a proteção original: a linha em branco só é atravessada
    quando a PRÓXIMA linha não-vazia é ela mesma uma linha de papel do
    vocabulário fechado (`RE_PAPEL`). Corpo de despacho depois de linha em
    branco continua encerrando o cabeçalho, que é o caso normal.

    Nome quebrado em duas linhas é a regra, não a exceção ('RECORRENTE
    SINDICATO NACIONAL DOS\\nAEROVIARIOS'). A continuação só é aceita se a linha
    não tiver minúscula e for curta — o corpo do ato tem minúscula.

    O vínculo advogado→parte vem da ADJACÊNCIA no cabeçalho, que é a estrutura
    que a própria fonte publica (o advogado vem listado sob a sua parte); não é
    inferência semântica nossa.
    """
    partes: list[dict] = []
    advogados: list[dict] = []
    parte_corrente = ''
    entrada: list[str] | None = None
    papel_corrente = ''

    def fechar():
        nonlocal entrada, papel_corrente, parte_corrente
        if entrada is None:
            return
        bruto = ' '.join(entrada)
        papel = papel_corrente.upper()
        if papel in PAPEIS_METADADO:
            entrada = None
            return
        if papel in PAPEIS_ADVOGADO:
            numero, uf = _oab(bruto)
            nome = _limpar_nome(bruto)
            if nome:
                advogados.append({
                    'advogado': {'nome': nome, 'numero_oab': numero, 'uf_oab': uf},
                    'papel': papel_corrente,
                    'parte': parte_corrente,
                })
        else:
            nome = _limpar_nome(bruto)
            if nome:
                polo = ''
                if papel in PAPEIS_ATIVOS:
                    polo = POLO_ATIVO
                elif papel in PAPEIS_PASSIVOS:
                    polo = POLO_PASSIVO
                partes.append({'nome': nome, 'papel': papel_corrente, 'polo': polo})
                parte_corrente = nome
        entrada = None

    linhas = corpo.split('\n')[1:]
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        if not linha.strip():
            # Linha em branco: só atravessa se o cabeçalho CONTINUA (próxima
            # linha não-vazia é papel do vocabulário fechado). Ver docstring.
            j = i + 1
            while j < len(linhas) and not linhas[j].strip():
                j += 1
            if (j < len(linhas) and not RE_INTIMADOS.search(linhas[j])
                    and RE_PAPEL.match(linhas[j])):
                fechar()          # a entrada em curso termina na linha em branco
                i = j
                continue
            break
        if RE_INTIMADOS.search(linha):
            break
        casou = RE_PAPEL.match(linha)
        if casou:
            fechar()
            papel_corrente = casou.group(1)
            entrada = [casou.group(2).strip()]
            i += 1
            continue
        if entrada is not None and not _tem_minuscula(linha) and len(linha.strip()) <= 90:
            entrada.append(linha.strip())
            i += 1
            continue
        fechar()
        i += 1
    fechar()

    # 'Intimado(s)/Citado(s):' é a lista de quem foi efetivamente intimado.
    # Marcar quem está nela é conferência VERBATIM (comparação de string), não
    # opinião — e é o sinal mais próximo do `destinatarios` do DJEN.
    intimados = _intimados(corpo)
    if intimados:
        for p in partes:
            p['intimado'] = _chave_de_nome(p['nome']) in intimados
    return partes, advogados


def _intimados(corpo: str) -> set[str]:
    m = RE_INTIMADOS.search(corpo)
    if not m:
        return set()
    nomes: set[str] = set()
    atual: list[str] = []
    restante = corpo[m.end():].split('\n')
    for pos, linha in enumerate(restante):
        if not linha.strip():
            # Mesmo defeito que a tabela de partes tinha (ver
            # `ler_cabecalho_de_partes`): a linha em branco é resíduo de layout
            # do PDF, não fim de lista. Só encerra se o próximo bullet não vier.
            proximo = next((x for x in restante[pos + 1:] if x.strip()), '')
            if atual and not proximo.lstrip().startswith('-'):
                break
            if atual:
                nomes.add(_chave_de_nome(' '.join(atual)))
                atual = []
            continue
        if linha.lstrip().startswith('-'):
            if atual:
                nomes.add(_chave_de_nome(' '.join(atual)))
            atual = [linha.lstrip()[1:].strip()]
        elif atual and not _tem_minuscula(linha):
            atual.append(linha.strip())
        else:
            break
    if atual:
        nomes.add(_chave_de_nome(' '.join(atual)))
    return {n for n in nomes if n}
