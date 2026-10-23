"""Fila oficial de precatórios — a quarta porta do acervo.

O DJEN só mostra o precatório que teve ato publicado. O precatório antigo
parado na fila de pagamento não publica nada, e por isso nunca nasceu no
Voyager: medido em 23/10/2026 no TJAL, 75 dos 82 precatórios TJ-AL da lista
oficial de Delmiro Gouveia tinham ZERO publicação no DJEN (75 de 75 consultados
um a um), e numa amostra de 8.532 pendentes das listas oficiais, 2.387 (28%)
estavam fora do Voyager — 0 de 424 consultados tinham publicação.

A fila oficial do tribunal é a fonte primária desse acervo. Um coletor por
tribunal entrega `ItemFila` normalizados; a gravação, a descoberta do processo
e a classificação (`filas_oficiais/descoberta.py`) não sabem de onde vieram.

LGPD (Res. CNJ 303/2019, art. 12, §3º): o `ItemFila` NÃO tem campo para CPF
nem nome do credor, de propósito. A fonte publica os dois; o coletor os
descarta na leitura. Quem precisa do credor (o JURISCOPE) lê na consulta
pública do processo.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

_RE_CNJ = re.compile(r'(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})')

SITUACAO_PENDENTE = 'pendente'
SITUACAO_PAGO = 'pago'
SITUACAO_EXTINTO = 'extinto'
SITUACAO_OUTRO = 'outro'
#: Estava pendente e não apareceu numa coleta COMPLETA da sua entidade. Não é
#: prova de pagamento (a fonte não diz por que saiu), então o classificador se
#: abstém em vez de vetar.
SITUACAO_FORA_DA_FILA = 'fora_da_fila'


def normalizar_chave(numero: str) -> str:
    """CNJ formatado, sem sufixo ("0500366-61.2022.8.02.0043/01" → sem "/01").

    O SAPRE grava o número do incidente com "/NN" (46 de 122 linhas em
    Delmiro); o processo, aqui e no e-SAJ, é o CNJ sem ele. '' sem CNJ.
    """
    m = _RE_CNJ.search(re.sub(r'\s+', '', numero or ''))
    if not m:
        return ''
    a, b, c, d, e, f = m.groups()
    return f'{a}-{b}.{c}.{d}.{e}.{f}'


def segmento_tribunal(chave: str) -> str:
    """"J.TR" do CNJ ("8.02" = TJAL, "5.19" = TRT19, "4.05" = TRF5)."""
    partes = (chave or '').split('.')
    return f'{partes[2]}.{partes[3]}' if len(partes) == 5 else ''


def normalizar_entidade(nome: str) -> str:
    """Nome da entidade sem acento, em caixa alta e com espaços simples.

    A lista em PDF diz "Delmiro Gouveia" e o SAPRE "DELMIRO GOUVEIA": é a mesma
    fila, e a contagem por entidade não pode partir em duas.
    """
    s = unicodedata.normalize('NFKD', nome or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'^(MUNICIPIO|PREFEITURA)\s+(MUNICIPAL\s+)?D[EOA]S?\s+', '', s.upper().strip())
    return re.sub(r'\s+', ' ', s).strip()


@dataclass
class ItemFila:
    """Um precatório como a fonte o publica, já normalizado e sem dado pessoal.

    `chave` é o CNJ do precatório (sem "/NN"). `chave_e_origem`: a fonte deu
    como "número do precatório" o próprio processo de 1º grau (precatório
    antigo, sem autuação própria).
    """
    chave: str
    fonte: str
    entidade: str = ''
    entidade_devedora: str = ''
    situacao: str = SITUACAO_PENDENTE
    situacao_raw: str = ''
    chave_e_origem: bool = False
    id_externo: str = ''
    valor_original: Decimal | None = None
    valor_atualizado: Decimal | None = None
    ano_orcamento: int | None = None
    natureza: str = ''
    origem_cnj: str = ''
    ordem: int | None = None
    regime: str = ''
    n_itens: int = 1
    #: Só para deduplicar linhas repetidas da fonte, em memória: hash curto do
    #: documento + valor. Nunca é gravado.
    _dedup: tuple = field(default=(), repr=False, compare=False)


@dataclass
class ResultadoEntidade:
    """O que a coleta de UMA entidade trouxe.

    `completa`: todas as fontes que a entidade tem responderam inteiras. Só
    numa coleta completa o que sumiu da fila pode ser marcado `fora_da_fila` —
    uma fonte fora do ar não "paga" precatório nenhum.
    """
    entidade: str
    itens: list
    completa: bool
    info: dict = field(default_factory=dict)
    falhas: list = field(default_factory=list)
