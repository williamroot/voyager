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


def validar(criterio: str, valor: str) -> dict:
    """`{'criterio', 'valor', 'normalizado'}` — ou `EntradaInvalida`."""
    criterio = (criterio or '').strip().lower()
    if criterio not in ROTULOS:
        raise EntradaInvalida(
            'criterio_desconhecido',
            f'Critério "{criterio}" não existe. Use: {", ".join(sorted(ROTULOS))}.')

    if criterio == DOCUMENTO:
        return _validar_documento(valor)
    if criterio == OAB:
        return _validar_oab(valor)
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


def _validar_oab(valor: str) -> dict:
    from search.busca_api import normalizar_oab

    oab = normalizar_oab(valor)
    if not oab:
        raise EntradaInvalida(
            'oab_invalida',
            'Informe a OAB com o número e a UF (ex.: 123456/SP).')
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


__all__ = ['ADVOGADO', 'DOCUMENTO', 'NOME', 'OAB', 'EntradaInvalida', 'rotulo', 'validar']
