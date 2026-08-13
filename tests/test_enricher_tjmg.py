"""Enricher TJMG — config + a limitação documentada do `valor_causa`.

Contexto (probe 2026-08-13). Pergunta: "por que MG aparece como 'valor não
informado' no mapa?". Resposta medida: **o PJe consulta pública do TJMG não
publica o valor da causa** — não é parser faltando.

`BasePjeEnricher._extrair_dados` JÁ tem o ramo `'valor' in chave and 'causa'
in chave` (pje.py). Ele nunca dispara no TJMG porque a página de detalhe não
tem o campo: a string "valor" não aparece **nenhuma vez** no HTML (nem em
label, nem em valor, nem em JS). Idem TRF3 — é limitação do PJe consulta
pública, não do TJMG.

Estes testes travam a evidência em fixture pra ninguém "consertar o parser"
de novo. Se o TJMG passar a publicar o campo, capture uma fixture nova e
troque o teste — a base já extrai sozinha, sem código novo.

Fixtures capturadas contra pje-consulta-publica.tjmg.jus.br em 2026-08-13:
  · execução fiscal (a classe que MAIS teria valor da causa)
  · procedimento comum cível
"""
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

from enrichers.parsers import parse_valor_brl
from enrichers.pje import BasePjeEnricher
from enrichers.tjmg import TjmgEnricher

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjmg'
DETALHES = (
    'detalhe_exec_fiscal_5219007-28.2022.8.13.0024.html',
    'detalhe_comum_5009689-84.2025.8.13.0481.html',
)


def _make_enricher() -> TjmgEnricher:
    return TjmgEnricher(pool=MagicMock())


def _soup(nome: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / nome).read_text(encoding='utf-8'), 'html.parser')


# --------------------------- Config / wiring ---------------------------

def test_config_endpoints_e_sigla():
    e = _make_enricher()
    assert e.TRIBUNAL_SIGLA == 'TJMG'
    assert e.BASE_URL == 'https://pje-consulta-publica.tjmg.jus.br'
    assert e.LIST_URL == 'https://pje-consulta-publica.tjmg.jus.br/pje/ConsultaPublica/listView.seam'
    assert e.DETALHE_PATH == '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'


def test_e_subclasse_de_base_pje():
    assert issubclass(TjmgEnricher, BasePjeEnricher)


def test_registry_e_fila():
    from enrichers.jobs import _ENRICHERS, queue_for
    assert _ENRICHERS['TJMG'] is TjmgEnricher
    assert queue_for('TJMG') == 'enrich_tjmg'


# ------------------- valor_causa: indisponível na fonte -------------------

@pytest.mark.parametrize('nome', DETALHES)
def test_detalhe_nao_contem_valor_da_causa(nome):
    """A fonte não tem o dado — a palavra 'valor' não existe no HTML.

    Este é o teste que responde "é bug nosso ou é o tribunal?": se um dia
    falhar, o TJMG passou a publicar algo com 'valor' e vale reinvestigar.
    """
    html = (FIXTURES / nome).read_text(encoding='utf-8')
    assert 'valor' not in html.lower()


@pytest.mark.parametrize('nome', DETALHES)
def test_extrair_dados_pega_o_resto_mas_nao_o_valor(nome):
    """O parser da base funciona no TJMG (classe/data/assunto saem) — o que
    falta é só o valor, porque a fonte não manda."""
    dados = _make_enricher()._extrair_dados(_soup(nome))
    assert dados.get('classe')
    assert dados.get('data_autuacao')
    assert 'valor_causa' not in dados


@pytest.mark.parametrize('nome', DETALHES)
def test_sem_valor_nao_vira_zero_no_drainer(nome):
    """Ausência de valor ⇒ campo ausente no update ⇒ `valor_causa` fica NULL.
    Nunca 0 — 0 mentiria "causa de R$ 0,00" na ficha e no mapa."""
    from enrichers.drainer import normalize_dados
    out = normalize_dados(_make_enricher()._extrair_dados(_soup(nome)))
    assert 'valor_causa' not in out


# ----------- parse_valor_brl: formato BR (milhar × decimal) -----------
# Parser compartilhado por TODOS os enrichers que extraem valor (e-SAJ,
# TJMT, TJPA). Confundir '.' de milhar com decimal erra por 1000×.

@pytest.mark.parametrize('texto,esperado', [
    ('R$ 1.234,56',        Decimal('1234.56')),    # milhar + centavos
    ('R$ 1234,56',         Decimal('1234.56')),    # sem separador de milhar
    ('R$ 1.000,00',        Decimal('1000.00')),    # milhar redondo
    ('R$ 1.234.567,89',    Decimal('1234567.89')), # acima de 1 milhão
    ('R$ 12.345.678,90',   Decimal('12345678.90')),
    ('R$ 0,99',            Decimal('0.99')),       # só centavos
    ('Valor da ação R$ 198.543,57', Decimal('198543.57')),  # embutido em texto
    ('Cr$ 1.500,00',       Decimal('1500.00')),    # cruzeiro (processo antigo)
])
def test_parse_valor_brl_formato_br(texto, esperado):
    assert parse_valor_brl(texto) == esperado


def test_parse_valor_brl_milhar_nao_vira_decimal():
    """Guarda explícita contra o erro de 1000×."""
    assert parse_valor_brl('R$ 1.234,56') != Decimal('1.23456')
    assert parse_valor_brl('R$ 1.000,00') == Decimal('1000.00') != Decimal('1.00')


@pytest.mark.parametrize('texto', ['', None, 'sem valor', 'R$', '--'])
def test_parse_valor_brl_ausente_vira_none(texto):
    """Sem valor ⇒ None (nunca Decimal('0'))."""
    assert parse_valor_brl(texto) is None


def test_parse_valor_brl_sem_centavos_nao_e_reconhecido():
    """Limitação conhecida do VALOR_RE: exige ',dd'. Fontes atuais (e-SAJ
    `#valorAcaoProcesso`, TJPA `valorCausaFormatado`, TJMT via
    `_valor_para_br`) sempre emitem centavos, então não morde hoje — mas
    fonte nova sem centavos precisa normalizar antes."""
    assert parse_valor_brl('R$ 1.000') is None
