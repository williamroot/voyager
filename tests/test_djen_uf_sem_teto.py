"""A fatia por UF não pode ser decapitada em silêncio NEM estourar a memória.

CONTEXTO (medido em 17/08/2026, e é a maior perda de acervo já achada aqui):

    `_fetch_uf` tinha `for pagina in range(1, 11)` — teto de 10 páginas × 1000 =
    10.000 itens POR UF — com o comentário "nenhum UF chega perto".

    No TJSP, `ufOab=SP` chega e passa. Conferido no dia 2025-07-21:

        Postgres (o que coletamos) ........ 117.215 publicações
        API paginando até esgotar ......... 208.000+ (piso: a sonda parou antes)

    43,6% do dia perdido, todo dia, com o run marcado `success` e zero alerta.
    Conferido fatia a fatia: `ufOab=SP` e `ufOab=MG` devolvem 1.000 itens na
    página 11 — exatamente a que o teto cortava.

SEGUNDO ATO (mesmo dia): tirar o teto sem mudar o resto trocou a perda
silenciosa por um OOM. A função acumulava TODOS os itens das 27 UFs numa lista
antes de gravar — cabia com 117k, não cabe com 208k publicações contendo o texto
inteiro. Os workers morreram com signal 9 e o watchdog registrou "worker
crashou" em 8 dos 30 dias do backfill. Agora as páginas viajam por uma fila
LIMITADA (`PAGINAS_EM_VOO`): o pico de memória é o tamanho da fila, não o do dia.

O que estes testes protegem:
  1. uma fatia com mais de 10 páginas vem INTEIRA;
  2. se o teto de sanidade for atingido, isso vira ERRO registrado — nunca mais
     um corte mudo;
  3. a coleta NÃO acumula o dia em memória (o que causou o OOM).
"""
from datetime import date
from unittest.mock import patch

import pytest

from djen import ingestion as I


class ClienteFalso:
    """DJEN de mentira: devolve `total_por_uf` itens, 1000 por página."""

    def __init__(self, total_por_uf):
        self.total_por_uf = total_por_uf
        self.paginas_pedidas = []

    def _fetch(self, sigla, ini, fim, pagina=1, itens_por_pagina=1000,
               extra_params=None, **kw):
        uf = (extra_params or {}).get('ufOab', '??')
        self.paginas_pedidas.append((uf, pagina))
        total = self.total_por_uf.get(uf, 0)
        desde = (pagina - 1) * itens_por_pagina
        n = max(0, min(itens_por_pagina, total - desde))
        return {'items': [{'id': f'{uf}-{desde + i}'} for i in range(n)]}


@pytest.fixture
def tribunal(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJ São Paulo', 'sigla_djen': 'TJSP'})
    return t


@pytest.mark.django_db
def test_fatia_com_mais_de_dez_paginas_vem_inteira(tribunal):
    """23.500 itens em SP = 24 páginas. Com o teto antigo, viriam 10.000."""
    cli = ClienteFalso({'SP': 23_500, 'RJ': 1_200})
    with patch.object(I, 'UF_OABS', ['SP', 'RJ']):
        run = I._ingest_day_por_uf(tribunal, date(2025, 7, 21), cli)

    # A prova é a PAGINAÇÃO, não o que foi persistido: os itens falsos não têm
    # CNJ nem data, então o parser os recusa (corretamente) e nada é gravado.
    # O que este teste afirma é que o coletor foi até o fim da fatia.
    pedidas_sp = [p for uf, p in cli.paginas_pedidas if uf == 'SP']
    assert max(pedidas_sp) == 24, 'parou antes de esgotar a fatia de SP'
    assert len(pedidas_sp) == 24, 'pulou página no meio da fatia'
    assert max(p for uf, p in cli.paginas_pedidas if uf == 'RJ') == 2
    assert run is not None


@pytest.mark.django_db
def test_teto_de_sanidade_vira_erro_registrado(tribunal):
    """Se o teto for atingido, tem que GRITAR. O silêncio é que custou 43,6%."""
    with patch.object(I, 'MAX_PAGINAS_UF', 3), patch.object(I, 'UF_OABS', ['SP']):
        cli = ClienteFalso({'SP': 50_000})     # 50 páginas, teto em 3
        run = I._ingest_day_por_uf(tribunal, date(2025, 7, 21), cli)

    # O contrato durável é o registro NO RUN (sobrevive ao processo e aparece
    # no dashboard); o log é conveniência de plantão.
    erro = next((e for e in run.erros if e.get('erro') == 'uf_teto_paginas'), None)
    assert erro is not None, 'truncou sem registrar o erro no run'
    assert erro['uf'] == 'SP' and erro['paginas'] == 3
    assert erro['itens'] == 3_000


@pytest.mark.django_db
def test_fatia_pequena_para_na_primeira_pagina(tribunal):
    """O caso comum não pode ficar mais caro: 300 itens = 1 requisição."""
    cli = ClienteFalso({'AC': 300})
    with patch.object(I, 'UF_OABS', ['AC']):
        I._ingest_day_por_uf(tribunal, date(2025, 7, 21), cli)
    assert [p for uf, p in cli.paginas_pedidas if uf == 'AC'] == [1]


@pytest.mark.django_db
def test_nao_acumula_o_dia_inteiro_em_memoria(tribunal):
    """O consumidor tem que gravar ENQUANTO os fetchers produzem.

    Se a implementação voltar a juntar tudo antes de gravar, este teste falha:
    ele conta quantas páginas já foram entregues quando a primeira gravação
    acontece. Numa versão acumuladora, TODAS já teriam sido entregues.
    """
    entregues = {'n': 0}
    gravou_em = {'n': None}

    cli = ClienteFalso({'SP': 30_000})
    original = cli._fetch

    def conta(*a, **kw):
        r = original(*a, **kw)
        if r['items']:
            entregues['n'] += 1
        return r
    cli._fetch = conta

    def _grava(page, *a, **kw):
        if gravou_em['n'] is None:
            gravou_em['n'] = entregues['n']
    with patch.object(I, 'UF_OABS', ['SP']), patch.object(I, '_process_page', _grava):
        I._ingest_day_por_uf(tribunal, date(2025, 7, 21), cli)

    assert entregues['n'] == 30, 'não paginou a fatia inteira'
    # com fila de PAGINAS_EM_VOO, a 1ª gravação acontece MUITO antes do fim
    assert gravou_em['n'] <= I.PAGINAS_EM_VOO + 2, (
        f'gravou só depois de {gravou_em["n"]} páginas — voltou a acumular o dia')
