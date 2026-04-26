"""Parsers comuns aos enrichers de tribunais. Extração de partes, classes, valores."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

# PJe consulta pública mascara doc como "639.XXX.XXX-XX" / "***.123.456-**".
# Regex é amplo (aceita X/*) pra capturar nome+strip; parse_documento separa.
CPF_RE = re.compile(r'(\d{3}\.[\dX*]{3}\.[\dX*]{3}-[\dX*]{2})')
CNPJ_RE = re.compile(r'(\d{2}\.[\dX*]{3}\.[\dX*]{3}/[\dX*]{4}-[\dX*]{2})')
OAB_RE = re.compile(r'OAB[\s/-]*([A-Z]{2})\s*([\d\.]+(?:-?[A-Z])?)', re.IGNORECASE)
ROLE_RE = re.compile(r'\(([^)]+)\)\s*$')
VALOR_RE = re.compile(r'R\$\s*([\d\.]+,\d{2})')
DATE_BR_RE = re.compile(r'(\d{2})/(\d{2})/(\d{4})')


def parse_documento(text: str) -> tuple[str, str]:
    """Retorna (documento_formatado, tipo) — ('', '') se não achar.

    Mascarado ou não, o match volta intacto — preservamos os dígitos visíveis
    pra ajudar dedupe de homônimos (TRF3 mascara como '639.XXX.XXX-XX'; um
    'João' com '639.XXX' é parte distinta de outro 'João' com '438.XXX').
    """
    if not text:
        return '', ''
    m = CNPJ_RE.search(text)
    if m:
        return m.group(1), 'CNPJ'
    m = CPF_RE.search(text)
    if m:
        return m.group(1), 'CPF'
    return '', ''


def is_documento_mascarado(documento: str) -> bool:
    """True se o documento contém qualquer caractere de mascara (X/*)."""
    if not documento:
        return False
    return 'X' in documento.upper() or '*' in documento


def parse_oab(text: str) -> str:
    """Retorna OAB normalizada (ex: 'SP123456' ou 'SP123456-A') ou ''."""
    if not text:
        return ''
    m = OAB_RE.search(text)
    if not m:
        return ''
    uf, num = m.group(1).upper(), m.group(2).replace('.', '').replace('-', '')
    return f'{uf}{num}'


def parse_role(text: str) -> str:
    """Extrai papel entre parênteses no fim, ex: 'Fulano (ADVOGADO)' → 'ADVOGADO'."""
    if not text:
        return ''
    m = ROLE_RE.search(text.strip())
    return m.group(1).strip() if m else ''


def parse_valor_brl(text: str) -> Optional[Decimal]:
    """'R$ 1.234,56' → Decimal('1234.56'). None se inválido."""
    if not text:
        return None
    m = VALOR_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace('.', '').replace(',', '.')
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_data_br(text: str) -> Optional[datetime]:
    """'25/04/2026' → datetime. None se inválido."""
    if not text:
        return None
    m = DATE_BR_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f'{m.group(1)}/{m.group(2)}/{m.group(3)}', '%d/%m/%Y')
    except ValueError:
        return None


def limpar_nome(text: str) -> str:
    """Remove documento, role e OAB do texto da parte, deixando só o nome."""
    if not text:
        return ''
    s = text
    s = CPF_RE.sub('', s)
    s = CNPJ_RE.sub('', s)
    s = OAB_RE.sub('', s)
    s = ROLE_RE.sub('', s)
    # Limpa marcadores tipo " - CPF: ", " - CNPJ: ", " - OAB"
    s = re.sub(r'\s*-\s*(CPF|CNPJ|OAB)\s*:?\s*', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s)
    return s.strip(' -·.,')


def classificar_tipo_parte(documento: str, tipo_documento: str, oab: str, papel: str) -> str:
    """Retorna 'pf', 'pj', 'advogado' ou 'desconhecido'."""
    if oab or 'advogad' in (papel or '').lower():
        return 'advogado'
    if tipo_documento == 'CNPJ':
        return 'pj'
    if tipo_documento == 'CPF':
        return 'pf'
    return 'desconhecido'
