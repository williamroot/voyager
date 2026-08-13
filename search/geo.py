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


# --------------------------------------------------------------------------- #
# Quem PUBLICA valor da causa (probe 13/08/2026 — ver .ia/ENRICHMENT.md)
# --------------------------------------------------------------------------- #
# `valor_causa` só se preenche onde a FONTE manda o campo. Não é falha de
# parser: `BasePjeEnricher._extrair_dados` já tem o ramo que lê valor, e ele
# nunca dispara porque o PJe consulta pública **não expõe o campo em tribunal
# nenhum** — verificado no HTML cru de 27 processos reais (TJMG, TRF3, TJRJ,
# TJMA, TJPE, TJCE, TJAP): a string "valor" não aparece uma vez sequer.
#
# Isto existe pra tela poder dizer a VERDADE. Sem esta lista, "valor não
# informado" lê como "o tribunal não publicou" — quando em metade do país a
# frase honesta é "esta fonte não publica valor, e não há o que buscar".
TRIBUNAIS_COM_VALOR = frozenset({
    'TJSP', 'TJAL', 'TJAC',   # e-SAJ (#valorAcaoProcesso)
    'TJPA',                   # portal REST próprio (valorCausaFormatado)
    'TJMT',                   # SPA + REST (valorCausa)
})

#: UFs cujo tribunal publica valor. Derivado, pra não duplicar a verdade.
UFS_COM_VALOR = frozenset(
    UF_DO_TRIBUNAL[t] for t in TRIBUNAIS_COM_VALOR if t in UF_DO_TRIBUNAL
)


def fonte_publica_valor(sigla: str) -> bool:
    """A FONTE deste tribunal expõe valor da causa?

    `False` não significa "ainda não buscamos" — significa que não adianta
    buscar: o dado não existe na consulta pública. Um TJ novo (fora da lista)
    cai em False, que é o conservador: a tela diz "esta fonte não publica" em
    vez de prometer um número que nunca virá.
    """
    return (sigla or '').upper() in TRIBUNAIS_COM_VALOR


def uf_tem_fonte_de_valor(uf: str) -> bool:
    """A UF tem ALGUM tribunal que publica valor.

    FED é False: TRF1/3/5 são PJe e não publicam (medido 0/2000).
    """
    return (uf or '').upper() in UFS_COM_VALOR
