"""Faixas que a consulta pública do tribunal comprovadamente NÃO tem.

Um tribunal pode rodar **mais de um sistema** ao mesmo tempo. Quando a fatia
nova (quase sempre `eproc`) não tem consulta pública aberta, o enricher pergunta
ao sistema errado e ouve "não existe" — gastando requisição e IP do pool
COMPARTILHADO para nada, e escondendo o processo atrás de um `nao_encontrado`
que parece resposta da fonte.

Este módulo concentra as faixas MEDIDAS. Cada linha exige três provas, e a
terceira é a que manda:

1. **Sistema** — o host do `link` da publicação DJEN denuncia o sistema
   (`eproc1g.<trib>.jus.br` contra `pje…`/`esaj…`/`www.dje…`).
2. **Estado** — `enriquecimento_status` dentro da faixa contra fora dela, em
   amostra por página aleatória do heap (`TABLESAMPLE SYSTEM REPEATABLE`), nunca
   bloco contíguo.
3. **Sonda ao vivo** — 16 CNJ da faixa perguntados à fonte real, MAIS um
   controle negativo na mesma janela. Sem o controle, "não existe" pode ser a
   fonte fora do ar; com ele, é a fonte dizendo que não tem.

⚠️ O corte é sempre `prefixo` **E** `ano >= N`, nunca o prefixo sozinho:
medido no TJSP, CNJ de prefixo 4 e ano 2013 ESTÃO no e-SAJ (33 de 33); medido no
TJMG, prefixo 1 de 2015-2021 está no PJe (7 de 16, contra 0 de 16 em 2025-2026).
Generalizar o prefixo apagaria processo bom.

⚠️ Recusa é **contada**, nunca corte mudo (regra nº 2 do CLAUDE.md) — quem conta
é `enrichers.jobs.registrar_fora_do_esaj` e o censo sai em ERROR no refill.

Medições de 29/08/2026 (amostra: 3,21 M publicações + 10,36 M processos, por
página aleatória; sondas ao vivo com controle negativo). Ver
`.ia/ENRICHMENT.md` §"Um tribunal, mais de um sistema".
"""
from typing import Iterable, Optional, Tuple

#: `(prefixo do sequencial do CNJ, ano mínimo, motivo)`.
Faixa = Tuple[str, int, str]


def so_digitos(valor: str) -> str:
    return ''.join(ch for ch in (valor or '') if ch.isdigit())


def faixa_fora_da_fonte(numero_cnj: str, faixas: Iterable[Faixa]) -> Optional[str]:
    """Motivo pelo qual este CNJ não está na fonte deste tribunal — ou `None`.

    CNJ malformado devolve `None`: lixo não vira recusa (abster > chutar).
    """
    digitos = so_digitos(numero_cnj)
    if len(digitos) != 20:
        return None
    try:
        ano = int(digitos[9:13])
    except ValueError:          # pragma: no cover — 4 dígitos sempre convertem
        return None
    for prefixo, ano_minimo, motivo in faixas:
        if digitos[0] == prefixo and ano >= ano_minimo:
            return motivo
    return None
