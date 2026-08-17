"""Leitura do HTML da tela de cadernos do DEJT (`/dejt/f/n/diariocon`).

Este módulo é PURO: recebe string de HTML e devolve dado. Nenhuma requisição
mora aqui — é o que permite testar a leitura com a fixture capturada em
16/08/2026 sem tocar no servidor do CSJT.

As três armadilhas que este arquivo existe para não cair:

1. **`j_id` NÃO é identificador.** O link de download da linha é
   `source:'corpo:formulario:plcLogicaItens:0:j_id132'`. O sufixo `j_id132` é
   gerado pelo JSF na compilação do XHTML: muda a cada deploy do DEJT. Não é
   "se", é "quando". Por isso o `source` NUNCA é persistido como chave: ele é
   relido do HTML fresco na hora do download, localizado por
   `class="link-download"` + posição na linha (ver `linhas_de_cadernos`), e o
   pareamento linha↔edição é feito pelo TÍTULO, que é dado e não identificador
   de framework.

2. **A legenda `(N)` é lixo.** `Cadernos Judiciários e Administrativos por
   Tribunal (16)` sai 16 em TODA consulta — inclusive na de 1 linha (fixture
   `busca_trt3_2024-07-10_J.html`) e na de 22. Quem usar como contador constrói
   métrica falsa. Contamos `<tr class="linhapar|linhaimpar">`.

3. **HTTP 200 que não é dado.** O GET inicial de `diariocon` JÁ vem com 16
   linhas (as edições mais recentes). Se o POST expirar a view, o JBoss devolve
   200 com uma tela parecidíssima — e o coletor gravaria as edições de HOJE
   achando que coletou 2015. A defesa é o ECO: a resposta de uma busca de
   verdade traz `dataIni` com a data pedida e `<option value="N" selected>` no
   tribunal (o GET inicial não tem `selected`). Ver `conferir_eco`.
"""

import html as _html
import re
from dataclasses import dataclass
from datetime import date

from diarios.base import RespostaInvalida

# ─────────────────────────────────────────────────────────────────────────────
# Tribunais: índice do <select> ↔ sigla do Voyager
# ─────────────────────────────────────────────────────────────────────────────
# Verbatim do `<select id="corpo:formulario:tribunal">` (fixture diariocon_J.html):
# ''=[Selecione]/todos, 0=TST, 1..24=TRT1..24, 25=CSJT, 26=ENAMAT.
IDX_TST = 0
IDX_CSJT = 25
IDX_ENAMAT = 26

#: CSJT e ENAMAT publicam no DEJT mas NÃO existem como `Tribunal` no Voyager
#: (a tabela tem TST + TRT1..24). Catalogá-los criaria `EdicaoDiario` com FK
#: quebrada e depois falha eterna no runner. Ficam de fora do catálogo, e a
#: contagem do que foi ignorado sai no log — é dívida VISÍVEL, não silêncio.
#: Para ligá-los basta criar as duas linhas em `tribunals.Tribunal`.
SEM_TRIBUNAL_NO_VOYAGER = {IDX_CSJT: 'CSJT', IDX_ENAMAT: 'ENAMAT'}

CADERNO_JUDICIARIO = 'J'
CADERNO_ADMINISTRATIVO = 'A'
#: valor do rádio `tipoCaderno` no form: 1=Judiciário, 0=Administrativo.
TIPO_CADERNO = {CADERNO_JUDICIARIO: '1', CADERNO_ADMINISTRATIVO: '0'}


def indice_do_tribunal(sigla: str) -> int:
    """Sigla do Voyager → índice do `<select>` do DEJT."""
    s = (sigla or '').strip().upper()
    if s == 'TST':
        return IDX_TST
    if s == 'CSJT':
        return IDX_CSJT
    if s == 'ENAMAT':
        return IDX_ENAMAT
    m = re.fullmatch(r'TRT(\d{1,2})', s)
    if m and 1 <= int(m.group(1)) <= 24:
        return int(m.group(1))
    raise ValueError(f'tribunal sem índice no DEJT: {sigla!r}')


# O título é o único campo estável da linha, então é ele que carrega o tribunal.
# Os quatro formatos abaixo foram contados no inventário completo de 18 anos
# (95.679 linhas, fixture inventario_J_2008_2026.html.gz):
#   70.074  'Caderno do TRT da 3ª Região - Judiciário'
#   13.656  'Caderno do TRT da 3ª REGIÃO - Jurídico'      (até ~2012)
#    6.561  'Caderno do TRT da 3ª REGIÃO - Judiciário'
#    3.272  'Caderno (do) Tribunal Superior do Trabalho'
#    1.072  'Caderno do Conselho Superior da Justiça do Trabalho'
#       67  'Escola Nacional de Formação e Aperfeiçoamento de Magistrados...'
# Ou seja: a deriva de 16 anos é de MAIÚSCULA e do sufixo Jurídico→Judiciário.
# Casar sem `re.IGNORECASE` seria perder 13.656 edições de 2008-2012 em silêncio.
RE_EDICAO = re.compile(r'Edi[çc][ãa]o\s+(\d{1,6})\s*/\s*(\d{4})', re.IGNORECASE)
RE_TRT = re.compile(r'Caderno\s+do\s+TRT\s+da\s+(\d{1,2})\s*[ªa]\s*Regi[ãa]o', re.IGNORECASE)
RE_TST = re.compile(r'Tribunal\s+Superior\s+do\s+Trabalho', re.IGNORECASE)
RE_CSJT = re.compile(r'Conselho\s+Superior\s+da\s+Justi[çc]a\s+do\s+Trabalho', re.IGNORECASE)
RE_ENAMAT = re.compile(r'Escola\s+Nacional\s+de\s+Forma[çc][ãa]o', re.IGNORECASE)

RE_LINHA = re.compile(r'<tr class="linha(?:par|impar)">(.*?)</tr>', re.S)
RE_CAMPO = re.compile(r'<span class="af_outputLabel">(.*?)</span>', re.S)
RE_ANCORA = re.compile(r'<a\b[^>]*>', re.S)
RE_SOURCE = re.compile(r"source:'([^']+)'")
RE_DATA_BR = re.compile(r'(\d{2})/(\d{2})/(\d{4})')


@dataclass(frozen=True)
class LinhaCaderno:
    """Uma linha da tabela de cadernos = uma unidade de coleta em potencial."""
    data: date
    titulo: str          # verbatim, é o que pareia a linha na re-busca
    source: str          # id JSF do link — VOLÁTIL, só vale nesta resposta
    sigla: str | None    # None quando o título não mapeia p/ Tribunal do Voyager
    edicao: str          # '4011'
    ano_edicao: str      # '2024'


def _texto(bruto: str) -> str:
    return re.sub(r'\s+', ' ', _html.unescape(bruto or '')).strip()


def sigla_do_titulo(titulo: str) -> str | None:
    """Sigla do tribunal a partir do título da edição. None = fora do Voyager."""
    if RE_TRT.search(titulo):
        return f'TRT{int(RE_TRT.search(titulo).group(1))}'
    if RE_TST.search(titulo):
        return 'TST'
    if RE_CSJT.search(titulo):
        return 'CSJT'
    if RE_ENAMAT.search(titulo):
        return 'ENAMAT'
    return None


def conferir_eco(html_: str, *, data_ini: str, tribunal_idx: object, contexto: str = '') -> str:
    """Prova que ESTA resposta é a busca que pedimos, e não a tela de sempre.

    Sem isto o coletor tem um falso-positivo perfeito: HTTP 200, HTML válido,
    16 linhas de edições — só que são as edições de HOJE que o GET inicial já
    trazia, não as do dia que pedimos. Num backfill de 86 mil cadernos isso
    grava dado errado sem nenhum erro no log.

    O eco tem duas partes, ambas medidas nas fixtures: o `dataIni` volta com a
    data pedida e, quando pedimos um tribunal específico, a `<option>` dele
    volta `selected` (o GET inicial não tem `selected` nenhum).
    """
    m = re.search(r'<input[^>]*id="corpo:formulario:dataIni"[^>]*>', html_ or '')
    if not m:
        raise RespostaInvalida(f'{contexto}: sem o campo dataIni — não é a tela de cadernos '
                               f'({len(html_ or "")} bytes)')
    eco = re.search(r'value="([^"]*)"', m.group(0))
    if not eco or eco.group(1).strip() != data_ini:
        raise RespostaInvalida(
            f'{contexto}: dataIni ecoou {eco.group(1) if eco else None!r}, pedimos {data_ini!r} — '
            'view expirada ou POST não submetido'
        )
    if (tribunal_idx not in ('', None)
            and f'<option value="{tribunal_idx}" selected>' not in html_):
        raise RespostaInvalida(
            f'{contexto}: tribunal {tribunal_idx} não voltou selected — o form não submeteu'
        )
    return html_


def linhas_de_cadernos(html_: str) -> list[LinhaCaderno]:
    """Lê a tabela de resultados. Só linhas COMPLETAS entram.

    Cada `<tr>` tem 2 `af_outputLabel` (data, título) e uma `<a class="...
    link-download ...">` cujo `onclick` carrega o `source`. Exigir os três
    juntos é o que dá o alarme de drift: se o DEJT mudar a tabela, a contagem
    cai a zero num dia em que o inventário diz que há edição — e isso o
    `coletor.py` transforma em `SchemaDriftAlert` em vez de "dia vazio".
    """
    linhas: list[LinhaCaderno] = []
    for tr in RE_LINHA.findall(html_ or ''):
        campos = [_texto(c) for c in RE_CAMPO.findall(tr)]
        if len(campos) < 2:
            continue
        source = ''
        for tag in RE_ANCORA.findall(tr):
            # Posição na linha + classe CSS, NUNCA o j_id literal.
            if 'link-download' not in tag:
                continue
            achado = RE_SOURCE.search(tag)
            if achado:
                source = achado.group(1)
                break
        if not source:
            continue
        d = RE_DATA_BR.search(campos[0])
        ed = RE_EDICAO.search(campos[1])
        if not d or not ed:
            continue
        linhas.append(LinhaCaderno(
            data=date(int(d.group(3)), int(d.group(2)), int(d.group(1))),
            titulo=campos[1],
            source=source,
            sigla=sigla_do_titulo(campos[1]),
            edicao=ed.group(1),
            ano_edicao=ed.group(2),
        ))
    return linhas


def achar_source_por_titulo(html_: str, titulo: str) -> str | None:
    """Reencontra o link de download da edição pelo TÍTULO, no HTML fresco.

    É o coração da imunidade a `j_id`: o catálogo guarda o título (dado), não o
    `source` (id de framework). Na hora de baixar, refazemos a busca e pegamos o
    `source` que aquela resposta trouxer.
    """
    alvo = _texto(titulo)
    for linha in linhas_de_cadernos(html_):
        if linha.titulo == alvo:
            return linha.source
    return None


RE_TOTAL = re.compile(r'(\d[\d.]*)\s*at[ée]\s*(\d[\d.]*)\s*de\s*([\d.]+)')


def total_de_materias(html_: str) -> int | None:
    """O gabarito de graça: o rodapé da pesquisa avançada diz `1 até 20 de 16.717`.

    É a única fonte que declara quantas matérias existem num tribunal-dia, e é
    contra ela que o segmentador é medido (`ColetorDiario.esperado`). Devolve
    None quando o rodapé não aparece — abster é melhor que devolver 0 e reprovar
    uma coleta boa.
    """
    m = RE_TOTAL.search(_html.unescape(html_ or ''))
    if not m:
        return None
    return int(m.group(3).replace('.', ''))
