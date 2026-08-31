"""Forma canônica da inscrição na OAB — uma só, para escrita e para dedup.

O campo `Parte.oab` é `UF` + número (+ letra de sufixo): `SP123456`, `CE5864A`.
O zero à esquerda do número **não é significativo**: `PE00475` e `PE475` são a
MESMA inscrição. Só que as duas formas convivem no banco porque as duas portas
de escrita divergiam:

  · `enrichers.parsers.parse_oab` lê de TEXTO ("OAB PA 015237") e PRESERVAVA o
    zero — ele veio no cabeçalho da publicação;
  · `tribunals.services.partes_djen.formatar_oab` lê do JSON do DJEN e REMOVE o
    zero desde o commit `55264d3` (o piloto do TJRS mediu 21,5% de advogado
    duplicado sem essa remoção).

`Parte.oab` tem unique parcial: escrever com um zero a mais é criar uma
entidade nova para um advogado que já existe. Medido em 31/08/2026 sobre
`tribunals_parte` (943.510 linhas com OAB): **19.493 linhas** são a mesma
inscrição gravada nas duas formas — e **todos** os grupos em colisão têm ao
menos uma forma zero-padded, ou seja o zero responde por 100% da colisão.

A canonização NÃO mexe na UF nem na letra de sufixo:
  · UF diferente é advogado DIFERENTE (`SP475` ≠ `PE475`) — fundir por número
    seria inventar identidade;
  · a letra de sufixo (`AL10715A`) é categoria de inscrição, não ruído.
"""
import re

#: `UF` + dígitos + (opcional) UMA letra de sufixo. Qualquer outra forma faz a
#: função ABSTER — inclusive o lixo `MT10079GO` do enricher do TJMT (1.424
#: linhas em 31/08/2026), onde o prefixo é o TRIBUNAL e a UF real está no fim.
_FORMA = re.compile(r'^([A-Z]{2})([0-9]+)([A-Z]?)$')


def canonizar_oab(oab: str | None) -> str:
    """`'AC003600'` → `'AC3600'`. Devolve `''` quando a forma não é reconhecida.

    Idempotente: `canonizar_oab(canonizar_oab(x)) == canonizar_oab(x)`.
    """
    m = _FORMA.match((oab or '').strip().upper())
    if not m:
        return ''
    uf, digitos, sufixo = m.groups()
    # `SP000` fica `SP000`, não `SP0`: inscrição só de zeros é lixo, e lixo se
    # ABSTÉM em vez de virar uma chave que funde com outro lixo.
    return f'{uf}{digitos.lstrip("0") or digitos}{sufixo}'
