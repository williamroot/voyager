"""Extração de entidades do TEXTO das publicações — OAB, CPF/CNPJ, CNJ, valores.

POR QUE ISSO EXISTE
-------------------
A busca por OAB alcançava **0,26%** da base (183.531 de 71.441.064 processos),
porque a OAB só existia onde o enricher passou — e enricher é o gargalo caro
(112.820 processos/dia, 16 dos 60 tribunais). Só que a OAB **já estava na nossa
base o tempo todo**, escrita no corpo das publicações que ingerimos do DJEN:

    ADVOGADO(A): RENATA PASSOS BERFORD GUARANA (OAB RJ112211)

Medido em 15/08/2026, sobre amostras aleatórias do índice de 1,16 bilhão de
publicações:

    22,0% dos processos têm alguma publicação que cita OAB   (vs 0,26% hoje)
    42,2% têm publicação que cita advogado
    11,0% têm publicação que cita CPF

Ou seja: extrair do texto multiplica a cobertura de OAB por ~85 sem depender do
enricher, sem consultar nenhum tribunal, usando dado que já pagamos pra ter.

POR QUE NÃO BASTA BUSCAR O TEXTO CRU
------------------------------------
`match_phrase: "OAB RJ112211"` funciona e acha — mas só pega UMA das formas.
Classificando 1.200 ocorrências reais:

    53,1%  OAB: UF123456      ← "(OAB: DF09930)"
    45,8%  OAB 123456/UF      ← "OAB 123456/SP"
     0,9%  OAB/UF 123456
     0,2%  OAB nº 123456      (sem UF)

Quem digita a OAB de um jeito perde metade do acervo se a busca for textual.
Normalizar na INGESTÃO resolve de uma vez: uma forma canônica (`UF123456`, a
mesma de `normalizar_oab`) num campo `keyword`, que é exato, agregável e rápido.

PRINCÍPIO: EXTRAIR SÓ O QUE SE PODE PROVAR
------------------------------------------
CPF, CNPJ e CNJ têm dígito verificador — todos são conferidos antes de virar
entidade. Sem isso, qualquer sequência de 11 dígitos num texto de intimação (nº
de guia, protocolo, conta bancária) viraria "documento da parte" e envenenaria a
busca. Abster > chutar.
"""
from __future__ import annotations

import re

from search.busca_api import _dv_cnpj_ok, _dv_cpf_ok, normalizar_oab
from tribunals.cnj import dv_valido as _cnj_dv_ok

# --------------------------------------------------------------------------- #
# OAB
# --------------------------------------------------------------------------- #
# Uma regex por FORMA MEDIDA, na ordem de frequência. Tentar uma regex única
# "esperta" aqui é o caminho pro falso positivo: `OAB` seguido de qualquer
# número casaria "OAB do Estado de SP, protocolo 123456".
_RE_OAB = (
    # 53,1% — "(OAB: DF09930)" / "OAB DF09930"
    re.compile(r'OAB[\s:/-]{0,3}([A-Z]{2})\s?(\d{2,6}[A-Z]?)\b', re.I),
    # 45,8% — "OAB 123456/SP" / "OAB nº 123.456 - SP"
    re.compile(r'OAB[\s:n°ºNo.]{0,6}(\d{1,3}[.\s]?\d{3}[A-Z]?)[\s/-]{1,3}([A-Z]{2})\b', re.I),
    # 0,9% — "OAB/SP 123456"
    re.compile(r'OAB[\s]?/[\s]?([A-Z]{2})[\s:.-]{1,3}(\d{1,3}[.\s]?\d{3}[A-Z]?)\b', re.I),
)

#: UFs válidas — sem isto, "OAB nº 12345 - fl" viraria a OAB do estado "FL"
_UFS = frozenset({
    'AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS',
    'MG', 'PA', 'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC',
    'SP', 'SE', 'TO',
})

_RE_TAG = re.compile(r'<[^>]{1,80}>')
_RE_CPF = re.compile(r'\b(\d{3}\.\d{3}\.\d{3}-\d{2})\b')
_RE_CNPJ = re.compile(r'\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\b')
_RE_CNJ = re.compile(r'\b(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})\b')
_RE_VALOR = re.compile(r'R\$\s?((?:\d{1,3}\.)*\d{1,3},\d{2})')
_RE_NAO_DIGITO = re.compile(r'\D')

#: teto por campo. Publicação de pauta lista centenas de advogados; guardar
#: todos infla o doc e não ajuda ninguém a achar nada.
MAX_POR_CAMPO = 40


def limpar(texto: str) -> str:
    """Tira marcação HTML antes de casar regex.

    As publicações do DJEN vêm com tabela HTML (`<td>ADVOGADO(A)</td><td>: ...`).
    Sem limpar, `<td>` no meio do padrão quebra o casamento.
    """
    if not texto:
        return ''
    return _RE_TAG.sub(' ', texto)


def oabs(texto: str) -> list[str]:
    """OABs citadas, na forma canônica `UF123456` (mesma de `normalizar_oab`)."""
    achadas = []
    for regex in _RE_OAB:
        for m in regex.finditer(texto):
            a, b = m.group(1), m.group(2)
            # a UF pode vir antes ou depois do número, conforme a forma
            uf, num = (a, b) if a.upper() in _UFS else (b, a)
            if uf.upper() not in _UFS:
                continue
            norm = normalizar_oab(f'{uf}{num}')
            if norm and norm not in achadas:
                achadas.append(norm)
                if len(achadas) >= MAX_POR_CAMPO:
                    return achadas
    return achadas


def documentos(texto: str) -> list[str]:
    """CPF/CNPJ citados, mascarados, **com DV conferido**.

    O índice guarda documento COM máscara (medido: zero documentos com dígitos
    puros), então devolvemos a forma mascarada — casa com o que a busca já faz.
    """
    achados = []
    for regex, ok in ((_RE_CPF, _dv_cpf_ok), (_RE_CNPJ, _dv_cnpj_ok)):
        for m in regex.finditer(texto):
            bruto = m.group(1)
            if not ok(_RE_NAO_DIGITO.sub('', bruto)):
                continue                      # número parecido não é documento
            if bruto not in achados:
                achados.append(bruto)
                if len(achados) >= MAX_POR_CAMPO:
                    return achados
    return achados


def cnjs(texto: str) -> list[str]:
    """Outros processos CITADOS na publicação, com DV conferido.

    Vale mais do que parece: é assim que se acha o incidente vinculado (o
    cumprimento que virou precatório, o agravo, o apenso) sem depender de o
    tribunal declarar a relação.
    """
    achados = []
    for m in _RE_CNJ.finditer(texto):
        cnj = m.group(1)
        if _cnj_dv_ok(cnj) and cnj not in achados:
            achados.append(cnj)
            if len(achados) >= MAX_POR_CAMPO:
                return achados
    return achados


def valores(texto: str) -> list[float]:
    """Valores em R$ citados. Ordenados do maior pro menor."""
    vistos = set()
    for m in _RE_VALOR.finditer(texto):
        try:
            v = float(m.group(1).replace('.', '').replace(',', '.'))
        except ValueError:
            continue
        if v > 0:
            vistos.add(v)
    return sorted(vistos, reverse=True)[:MAX_POR_CAMPO]


def extrair(texto: str) -> dict:
    """Todas as entidades de um texto. Chaves ausentes quando não achou nada.

    Ausência é deliberada: campo vazio em 1,16 bilhão de documentos custa disco
    e faz `exists` mentir (foi exatamente o bug de cobertura de 15/08/2026).
    """
    limpo = limpar(texto)
    if not limpo.strip():
        return {}
    saida = {
        'oabs': oabs(limpo),
        'documentos': documentos(limpo),
        'cnjs_citados': cnjs(limpo),
        'valores_citados': valores(limpo),
    }
    return {k: v for k, v in saida.items() if v}
