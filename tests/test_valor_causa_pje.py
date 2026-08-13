"""Valor da causa: o que o PJe consulta pública NÃO entrega, e o que o parser faz.

Contexto (triagem 2026-08-13). Auditoria apontou TJRJ/TJMA/TJPE/TJCE/TJAP com
0% de `valor_causa` e a hipótese era "falta parser nesses enrichers". É falso:
`BasePjeEnricher._extrair_dados` (enrichers/pje.py) JÁ tem o ramo
`'valor' in chave and 'causa' in chave` — os 5 enrichers herdam dele. O que
falta é o DADO: a consulta pública do PJe clássico **não publica** o valor da
causa. Nas fixtures reais abaixo a string "valor" não aparece UMA vez no HTML
inteiro (~70-140KB), e o `_source` do Datajud desses tribunais sequer traz a
chave `valorCausa`.

Consequência: escrever parser novo não preencheria nada. Estes testes travam a
constatação — se um tribunal passar a publicar o valor, o teste de "ausência"
quebra e avisa que dá pra ligar a extração (a lógica já está lá).

O que os testes garantem:
1. Nos 5 tribunais, o par (fixture real → `_extrair_dados` → `normalize_dados`)
   termina SEM `valor_causa` — ou seja, `Process.valor_causa` fica NULL.
   Nunca `Decimal('0')`: 0 é um valor de causa afirmado, NULL é "não informado".
2. `parse_valor_brl` respeita o formato BR (o separador de milhar é '.', o
   decimal é ','). Trocar um pelo outro erra por 1000×.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from enrichers.drainer import normalize_dados
from enrichers.parsers import parse_valor_brl
from enrichers.pje import BasePjeEnricher
from enrichers.tjap import TjapEnricher
from enrichers.tjce import TjceEnricher
from enrichers.tjma import TjmaEnricher
from enrichers.tjpe import TjpeEnricher
from enrichers.tjrj import TjrjEnricher

FIXTURES = Path(__file__).parent / 'fixtures'

# (sigla, enricher, fixture, CNJ de prova) — HTML de detalhe REAL, capturado da
# consulta pública do tribunal (TJPE/TJAP em 2026-08-13; demais em 2026-06/07).
DETALHES_REAIS = [
    ('TJRJ', TjrjEnricher, 'tjrj/detalhe_0937165-77.2025.8.19.0001.html',
     '0937165-77.2025.8.19.0001'),
    ('TJMA', TjmaEnricher, 'tjma/detalhe_ok.html', '0801341-50.2025.8.10.0114'),
    ('TJMA-2g', TjmaEnricher, 'tjma/detalhe_ok_2g.html', '0836521-81.2025.8.10.0000'),
    ('TJPE', TjpeEnricher, 'tjpe/detalhe_0144101-59.2024.8.17.2001.html',
     '0144101-59.2024.8.17.2001'),
    ('TJCE', TjceEnricher, 'tjce/pje_detalhe_3000739-94.2025.8.06.0100.html',
     '3000739-94.2025.8.06.0100'),
    ('TJAP', TjapEnricher, 'tjap/detalhe_6002434-35.2024.8.03.0008.html',
     '6002434-35.2024.8.03.0008'),
]

IDS = [c[0] for c in DETALHES_REAIS]


def _soup(rel: str) -> tuple[BeautifulSoup, str]:
    html = (FIXTURES / rel).read_text(encoding='utf-8', errors='replace')
    return BeautifulSoup(html, 'html.parser'), html


# --------------------------------------------------------------------------
# 1. A fonte não publica o valor (evidência), e o pipeline devolve None
# --------------------------------------------------------------------------

@pytest.mark.parametrize('sigla,cls,fixture,cnj', DETALHES_REAIS, ids=IDS)
def test_pje_publico_nao_traz_valor_da_causa(sigla, cls, fixture, cnj):
    """A palavra "valor" não existe no HTML de detalhe — não é falha de parser.

    Se este teste falhar, o tribunal passou a publicar o valor: cheque o rótulo
    e ligue a extração (o ramo já existe em `_extrair_dados`).
    """
    _, html = _soup(fixture)
    assert 'valor' not in html.lower(), (
        f'{sigla} ({cnj}) agora tem "valor" no HTML da consulta pública — '
        f'reavaliar: a extração de valor_causa pode ser ligada.'
    )


@pytest.mark.parametrize('sigla,cls,fixture,cnj', DETALHES_REAIS, ids=IDS)
def test_extrair_dados_nao_inventa_valor(sigla, cls, fixture, cnj):
    """`_extrair_dados` roda inteiro no HTML real e simplesmente não emite
    `valor_causa` (em vez de emitir '' ou 0)."""
    soup, _ = _soup(fixture)
    dados = BasePjeEnricher._extrair_dados(cls, soup)
    # O parser funciona — só o valor é que não está na fonte.
    assert dados.get('classe'), f'{sigla}: fixture deveria ter classe'
    assert 'valor_causa' not in dados, (
        f'{sigla}: _extrair_dados emitiu valor_causa={dados.get("valor_causa")!r}'
    )


@pytest.mark.parametrize('sigla,cls,fixture,cnj', DETALHES_REAIS, ids=IDS)
def test_processo_sem_valor_vira_none_nunca_zero(sigla, cls, fixture, cnj):
    """End-to-end até o drainer: sem valor na fonte, `Process.valor_causa`
    fica NULL. Zero seria pior que nada — 0 afirma "a causa vale R$ 0,00"."""
    soup, _ = _soup(fixture)
    dados = BasePjeEnricher._extrair_dados(cls, soup)
    out = normalize_dados(dados)
    assert 'valor_causa' not in out, (
        f'{sigla}: drainer gravaria valor_causa={out.get("valor_causa")!r}'
    )
    # Chave ausente ⇒ setattr não roda ⇒ campo permanece NULL no Process.
    assert out.get('valor_causa') is None
    assert out.get('valor_causa') != Decimal('0')


def test_valor_ausente_ou_lixo_nunca_vira_zero():
    """Contrato do parser: sem valor legível → None, jamais Decimal('0')."""
    for entrada in ['', None, 'Valor da causa', 'R$', 'não informado', '-', 'R$ ,']:
        assert parse_valor_brl(entrada) is None, f'{entrada!r} deveria ser None'
    # Um zero DE VERDADE publicado pela fonte continua sendo 0 (é um dado).
    assert parse_valor_brl('R$ 0,00') == Decimal('0.00')


# --------------------------------------------------------------------------
# 2. Formato BR: milhar '.' e decimal ',' (inverter erra por 1000x)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('texto,esperado', [
    # com centavos
    ('R$ 1.234,56', Decimal('1234.56')),
    ('R$ 999,99', Decimal('999.99')),
    # centavos zerados (o caso comum: fonte publica ",00")
    ('R$ 1.000,00', Decimal('1000.00')),
    ('R$ 15.000,00', Decimal('15000.00')),
    # acima de 1 milhão — dois separadores de milhar
    ('R$ 1.000.000,00', Decimal('1000000.00')),
    ('R$ 2.345.678,90', Decimal('2345678.90')),
    ('R$ 12.345.678.901,23', Decimal('12345678901.23')),
    # com ruído em volta (é assim que vem do HTML)
    ('Valor da causa: R$ 87.654,32 (atualizado)', Decimal('87654.32')),
    # moeda antiga (processos em Cruzeiro)
    ('Cr$ 4.500,00', Decimal('4500.00')),
    ('US$ 1.200,50', Decimal('1200.50')),
])
def test_parse_valor_brl_formato_br(texto, esperado):
    obtido = parse_valor_brl(texto)
    assert obtido == esperado
    # Guard explícito contra o erro de 1000x (milhar lido como decimal).
    assert obtido == esperado, f'{texto} → {obtido}, esperado {esperado}'


def test_milhar_nao_e_lido_como_decimal():
    """'R$ 1.234,56' vale mil e duzentos, não um e vinte e três."""
    assert parse_valor_brl('R$ 1.234,56') == Decimal('1234.56')
    assert parse_valor_brl('R$ 1.234,56') != Decimal('1.23456')
    assert parse_valor_brl('R$ 1.234,56') > Decimal('1000')
    # E um milhão é um milhão.
    assert parse_valor_brl('R$ 1.000.000,00') == Decimal('1000000')
    assert parse_valor_brl('R$ 1.000.000,00') > Decimal('999999')


def test_valor_sem_centavos_nao_e_chutado():
    """Sem os centavos o parser se abstém (None) em vez de adivinhar escala.

    Nenhuma fonte nossa publica valor sem centavos — e-SAJ traz 'R$ x,xx' no
    HTML, TJPA usa `valorCausaFormatado` e TJMT formata com `:,.2f`. Abster é o
    comportamento seguro: ler 'R$ 1.234' como 1234 ou como 1,234 é um chute com
    1000x de diferença.
    """
    assert parse_valor_brl('R$ 1.234') is None
    assert parse_valor_brl('R$ 1234') is None
    assert parse_valor_brl('R$ 1.234,5') is None  # 1 casa decimal: ambíguo
