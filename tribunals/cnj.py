"""Número CNJ → tribunal, de forma determinística (Resolução CNJ 65/2008).

O CNJ não é um identificador opaco: ele CARREGA o tribunal.

    NNNNNNN-DD.AAAA.J.TR.OOOO
    │       │  │    │ │  └─ unidade de origem (vara/comarca)
    │       │  │    │ └──── TR: tribunal dentro do segmento
    │       │  │    └────── J:  segmento do Judiciário
    │       │  └─────────── ano do ajuizamento
    │       └────────────── dígito verificador (módulo 97, ISO 7064)
    └────────────────────── número sequencial no ano/origem

Ou seja: pedir pro usuário escolher o tribunal num select, tendo o CNJ na mão,
é pedir uma informação que já está no que ele digitou — e é uma chance a mais
de ele errar (escolher TRF1 para um processo do TJSP devolve "não encontrado"
sem dizer por quê).

Fonte dos códigos: Resolução 65/2008 do CNJ, art. 1º §§ 1º-4º. Os estaduais
seguem a ordem ALFABÉTICA das UFs (AC=01 … TO=27), o que é fácil de conferir e
difícil de errar.
"""
import re

#: segmentos do Judiciário (o dígito `J`)
SEGMENTOS = {
    '1': 'STF', '2': 'CNJ', '3': 'STJ', '4': 'Justiça Federal',
    '5': 'Justiça do Trabalho', '6': 'Justiça Eleitoral',
    '7': 'Justiça Militar da União', '8': 'Justiça Estadual',
    '9': 'Justiça Militar Estadual',
}

#: J=8 — estaduais, em ordem alfabética de UF (art. 1º § 4º)
_UF_POR_CODIGO = {
    '01': 'AC', '02': 'AL', '03': 'AP', '04': 'AM', '05': 'BA', '06': 'CE',
    '07': 'DF', '08': 'ES', '09': 'GO', '10': 'MA', '11': 'MT', '12': 'MS',
    '13': 'MG', '14': 'PA', '15': 'PB', '16': 'PR', '17': 'PE', '18': 'PI',
    '19': 'RJ', '20': 'RN', '21': 'RS', '22': 'RO', '23': 'RR', '24': 'SC',
    '25': 'SE', '26': 'SP', '27': 'TO',
}

#: tribunais superiores: o TR não identifica tribunal (é 00), o segmento basta
_SEGMENTO_UNICO = {'1': 'STF', '2': 'CNJ', '3': 'STJ', '7': 'STM'}

_RE_NAO_DIGITO = re.compile(r'\D')


def so_digitos(cnj: str) -> str:
    return _RE_NAO_DIGITO.sub('', cnj or '')


def partes(cnj: str) -> dict | None:
    """Quebra o CNJ nos seus 5 campos. `None` se não tiver 20 dígitos."""
    d = so_digitos(cnj)
    if len(d) != 20:
        return None
    return {
        'sequencial': d[0:7], 'dv': d[7:9], 'ano': d[9:13],
        'segmento': d[13], 'tribunal': d[14:16], 'origem': d[16:20],
    }


def dv_valido(cnj: str) -> bool:
    """Confere o dígito verificador (módulo 97 base 10, ISO 7064).

    A conta é: pega o número SEM o DV, na ordem
    sequencial+ano+segmento+tribunal+origem+"00", e o resto por 97 tem que dar
    (98 - DV). Vale pra dizer "esse número está errado" em vez de sair
    consultando e devolver "não encontrado", que é outra coisa.
    """
    p = partes(cnj)
    if not p:
        return False
    base = (p['sequencial'] + p['ano'] + p['segmento'] + p['tribunal']
            + p['origem'] + '00')
    try:
        return int(base) % 97 == 98 - int(p['dv'])
    except ValueError:
        return False


def sigla_do_cnj(cnj: str) -> str | None:
    """Sigla do tribunal (TJSP, TRF3, TRT2, …) derivada do próprio número.

    `None` quando o número não tem 20 dígitos ou o segmento/código não é
    conhecido — abster é melhor que chutar um tribunal e mandar o usuário
    consultar no lugar errado.
    """
    p = partes(cnj)
    if not p:
        return None
    seg, tr = p['segmento'], p['tribunal']

    if seg in _SEGMENTO_UNICO:
        return _SEGMENTO_UNICO[seg]
    if seg == '4':                      # Justiça Federal → TRF1..TRF6
        n = int(tr)
        # ⚠️ O TRF6 (criado em 2022, desmembrado do TRF1 pra cobrir MG) herdou
        # processos cujo CNJ guarda o código ANTIGO (01 = TRF1). Medido em
        # 1.080 processos reais do índice: 6 divergências, TODAS deste caso.
        # A derivação está certa PELO NÚMERO — quem mudou foi o tribunal. Por
        # isso o índice, que observa o tribunal de fato, tem precedência sobre
        # esta função quando o processo já está na nossa base.
        return f'TRF{n}' if 1 <= n <= 6 else None
    if seg == '5':                      # Trabalho → TRT1..TRT24
        n = int(tr)
        return f'TRT{n}' if 1 <= n <= 24 else None
    if seg == '6':                      # Eleitoral → TRE-UF
        uf = _UF_POR_CODIGO.get(tr)
        return f'TRE{uf}' if uf else None
    if seg in ('8', '9'):               # Estadual e Militar Estadual
        uf = _UF_POR_CODIGO.get(tr)
        if not uf:
            return None
        # DF é TJDFT na nossa base (o CNJ usa 07 = "DF e Territórios")
        return 'TJDFT' if uf == 'DF' else f'TJ{uf}'
    return None


def descrever(cnj: str) -> dict:
    """O que dá pra dizer sobre um CNJ sem consultar nada.

    Devolve sempre um dict (nunca levanta), pra tela poder explicar o que
    entendeu — incluindo o caso "número inválido", que é diferente de "não
    encontrado".
    """
    d = so_digitos(cnj)
    p = partes(cnj)
    if not p:
        return {'valido': False, 'motivo': 'digitos',
                'digitos': len(d), 'sigla': None, 'segmento': None}
    return {
        'valido': dv_valido(cnj),
        'motivo': None if dv_valido(cnj) else 'dv',
        'digitos': 20,
        'sigla': sigla_do_cnj(cnj),
        'segmento': SEGMENTOS.get(p['segmento']),
        'ano': p['ano'],
        'formatado': f"{p['sequencial']}-{p['dv']}.{p['ano']}."
                     f"{p['segmento']}.{p['tribunal']}.{p['origem']}",
    }
