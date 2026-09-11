"""Validação e normalização do que foi digitado. Antes de gastar rede.

Duas saídas por entrada: o `valor` que vai para a fonte (cada uma quer um
formato — o e-SAJ aceita CPF com máscara, os REST querem dígitos) e o
`normalizado`, que é a forma canônica pela qual o cache reconhece a MESMA
pergunta escrita de outro jeito.

CPF e OAB reusam `search.busca_api`, que já é o normalizador desta casa —
inclusive a conferência de dígito verificador. Um CPF com DV quebrado é 400 com
mensagem, nunca uma busca que sai queimando IP do pool para voltar vazia.
"""
from __future__ import annotations

import re
import unicodedata

from .base import ADVOGADO, DOCUMENTO, NOME, OAB, ROTULOS, BuscaError

#: Mínimo de letras num nome. Abaixo disso a fonte devolve "refine sua busca"
#: (e-SAJ) ou o teto de 30 (PJe) — gastar a requisição para ouvir isso não
#: ajuda ninguém.
MIN_NOME = 4

#: As 27 unidades da federação. Existe aqui, e não só no `<select>` da tela,
#: porque a régua da OAB é do BACKEND: a API v1 tem os mesmos clientes e não
#: pode aceitar o que a tela passou a recusar.
UFS = (
    'AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS',
    'MT', 'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN', 'RO', 'RR', 'RS', 'SC',
    'SE', 'SP', 'TO',
)


class EntradaInvalida(BuscaError):
    """O que foi digitado não dá para buscar. Vira 400 com mensagem humana."""

    def __init__(self, codigo: str, mensagem: str):
        self.codigo, self.mensagem = codigo, mensagem
        super().__init__(mensagem)


def _sem_acento(texto: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', texto)
                   if not unicodedata.combining(c))


def _limpar_nome(bruto: str) -> str:
    return re.sub(r'\s+', ' ', (bruto or '')).strip()


def validar(criterio: str, valor: str, uf: str = '') -> dict:
    """`{'criterio', 'valor', 'normalizado'}` — ou `EntradaInvalida`.

    `uf` só é lido no critério OAB, e existe porque a inscrição da OAB **não é
    única no país**: o 123.456 de São Paulo e o 123.456 do Pará são advogados
    diferentes. Ver `_validar_oab`.
    """
    criterio = (criterio or '').strip().lower()
    if criterio not in ROTULOS:
        raise EntradaInvalida(
            'criterio_desconhecido',
            f'Critério "{criterio}" não existe. Use: {", ".join(sorted(ROTULOS))}.')

    if criterio == DOCUMENTO:
        return _validar_documento(valor)
    if criterio == OAB:
        return _validar_oab(valor, uf)
    return _validar_nome(criterio, valor)


def _validar_documento(valor: str) -> dict:
    from search.busca_api import BuscaParamError, normalizar_documento

    try:
        doc = normalizar_documento(valor)
    except BuscaParamError as exc:
        codigo = str(exc)
        raise EntradaInvalida(codigo, {
            'documento_invalido': 'Informe um CPF (11 dígitos) ou CNPJ (14 dígitos).',
            'cpf_dv_invalido': 'CPF inválido — o dígito verificador não fecha.',
            'cnpj_dv_invalido': 'CNPJ inválido — o dígito verificador não fecha.',
        }.get(codigo, 'Documento inválido.')) from exc

    if doc['tipo'] == 'raiz_cnpj':
        # A raiz serve para varrer o ÍNDICE (matriz + filiais); nenhum
        # formulário de tribunal aceita 8 dígitos. Recusar é mais honesto do que
        # buscar por uma coisa e responder outra.
        raise EntradaInvalida(
            'documento_incompleto',
            'A busca no tribunal precisa do CNPJ inteiro (14 dígitos); '
            'a raiz só funciona na busca por índice.')

    # A forma MASCARADA vai para a fonte: é como o e-SAJ e o PJe escrevem o
    # documento nos seus formulários. Os clientes REST tiram os pontos.
    return {'criterio': DOCUMENTO, 'valor': doc['mascarado'],
            'normalizado': doc['digitos']}


#: OAB já normalizada: UF (2 letras) + número, com sufixo opcional de letra
#: (`CE5864A` existe no acervo). É a forma que `normalizar_oab` devolve QUANDO
#: a UF foi informada — e é justamente isso que precisa ser conferido.
_RE_OAB_COM_UF = re.compile(r'^[A-Z]{2}\d+[A-Z]?$')


def _validar_oab(valor: str, uf: str = '') -> dict:
    """A OAB SEM UF é recusada — e isto fecha um buraco, não só melhora a tela.

    Medido em 11/09/2026: `normalizar_oab('123456')` devolve `'123456'`. Sem
    UF nenhuma, e a função não reclama. Esse valor descia inteiro até a fonte,
    que procurava uma inscrição que não existe naquele formato e devolvia zero.
    A tela então dizia **"nenhum processo"** — indistinguível da resposta de uma
    busca bem-feita.

    É a assinatura das perdas do `CLAUDE.md`: pergunta errada, resposta vazia
    com cara de resposta certa. A inscrição da OAB **não é única no país** — o
    123.456/SP e o 123.456/PA são advogados diferentes —, então número sem UF
    não é uma busca ampla: é uma busca impossível.

    Aceita as duas formas, porque a API v1 tem clientes que já mandam a UF
    embutida: `('123456', 'SP')` e `('123456/SP', '')` chegam no mesmo lugar.
    """
    from search.busca_api import normalizar_oab

    uf = re.sub(r'[^A-Za-z]', '', (uf or '')).upper()
    if uf and uf not in UFS:
        raise EntradaInvalida(
            'uf_invalida', f'UF "{uf}" não existe. Escolha uma das 27.')

    bruto = (valor or '').strip()
    # a UF do seletor só entra se o número já não trouxer uma — quem digita
    # `123456/SP` com `PA` escolhido está se contradizendo, e adivinhar qual
    # vale seria escolher por ele
    oab = normalizar_oab(bruto)
    if oab and uf and not _RE_OAB_COM_UF.match(oab):
        oab = normalizar_oab(f'{uf}{oab}')
    if not oab or not _RE_OAB_COM_UF.match(oab):
        raise EntradaInvalida(
            'oab_sem_uf',
            'A OAB precisa da UF: a mesma inscrição existe em estados '
            'diferentes e pertence a advogados diferentes. '
            'Escolha a UF, ou digite no formato 123456/SP.')
    return {'criterio': OAB, 'valor': oab, 'normalizado': oab}


def _validar_nome(criterio: str, valor: str) -> dict:
    nome = _limpar_nome(valor)
    if len(re.sub(r'[^A-Za-zÀ-ÿ]', '', nome)) < MIN_NOME:
        raise EntradaInvalida(
            'nome_curto',
            f'Informe ao menos {MIN_NOME} letras do {ROTULOS[criterio]}.')
    return {'criterio': criterio, 'valor': nome,
            'normalizado': _sem_acento(nome).upper()}


def rotulo(criterio: str) -> str:
    return ROTULOS.get(criterio, criterio)


__all__ = ['ADVOGADO', 'DOCUMENTO', 'NOME', 'OAB', 'UFS', 'EntradaInvalida',
           'rotulo', 'validar']
