"""Tribunal → UF, pro mapa comercial (agregação por estado no ES).

Estaduais (TJ) mapeiam pra sua UF; federais/superiores (TRF*, TST, STJ, STF,
TRT*, TSE) caem em 'FED' (camada federal separada — processo federal não pertence
a uma UF única). Ver .ia/ENRICHMENT.md / módulo comercial.
"""

UF_DO_TRIBUNAL = {
    'TJAC': 'AC', 'TJAL': 'AL', 'TJAP': 'AP', 'TJAM': 'AM', 'TJBA': 'BA',
    'TJCE': 'CE', 'TJDFT': 'DF', 'TJES': 'ES', 'TJGO': 'GO', 'TJMA': 'MA',
    'TJMT': 'MT', 'TJMS': 'MS', 'TJMG': 'MG', 'TJPA': 'PA', 'TJPB': 'PB',
    'TJPR': 'PR', 'TJPE': 'PE', 'TJPI': 'PI', 'TJRJ': 'RJ', 'TJRN': 'RN',
    'TJRS': 'RS', 'TJRO': 'RO', 'TJRR': 'RR', 'TJSC': 'SC', 'TJSP': 'SP',
    'TJSE': 'SE', 'TJTO': 'TO',
}

#: rótulo pros tribunais sem UF única (federal/superior/trabalhista).
UF_FEDERAL = 'FED'


def uf_do_tribunal(sigla: str) -> str:
    s = (sigla or '').upper()
    if s in UF_DO_TRIBUNAL:
        return UF_DO_TRIBUNAL[s]
    if s.startswith('TJ') and len(s) >= 4:   # fallback p/ TJxx não listado
        return s[2:4]
    return UF_FEDERAL
