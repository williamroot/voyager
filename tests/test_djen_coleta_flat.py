"""A coleta do dia é FLAT, e uma fatia perdida não pode virar `success`.

CONTEXTO (medição de 18/08/2026 nos 59 tribunais).

O fatiamento por `ufOab` nasceu de uma crença: "a API capa em 10.000, então
fatie o dia por UF". A crença caiu. `count=10000` é o `max_result_window` do
Elasticsearch por baixo — um PISO ("tem pelo menos 10 mil"), não um teto de
paginação. Provas independentes:

    TJSP  2026-05-13 flat até a página 258 → 257.593 itens, 257.593 ids
                     DISTINTOS, zero página incompleta no meio
    TJRJ  2026-05-13 dia inteiro flat = 53.480 = exatamente o que gravamos
    TJRR  2025-06-18 página 2000 (offset 200.000) devolve 100 itens distintos

E o fatiamento tem defeito PRÓPRIO, que o conserto do teto de páginas (17/08)
não fecha: ele só enxerga publicação que cita advogado com OAB. Provado na
unidade, em cinco tribunais —

    TJPE 2025-08-13: 2.853 itens sem OAB  ==  as 2.853 que faltavam
    TJMA 2026-08-13:   911 itens sem OAB  ==  as   911 que faltavam
    TJRN 2026-05-13: dia 11.116, união das 27 fatias 10.015, acervo 10.015
    STJ  e TJPB: idem, ao item

São 2% a 10% de todo dia acima de 10.000. Reingerir 12.934 dias pelo caminho
fatiado seria pagar o custo inteiro e continuar com o buraco — e pagar de novo.

O que estes testes protegem:
  1. dia grande sai pela paginação flat, sem fatiar (o padrão);
  2. a escotilha `DJEN_ESTRATEGIA_UF` ainda liga o caminho antigo;
  3. UMA fatia perdida falha o run — contar fatias tratava a do DF (77% do dia
     no TJDFT) igual à de um estado que devolve 40 itens.
"""
from datetime import date
from unittest.mock import patch

import pytest

from djen import ingestion as I


class DjenFalso:
    """DJEN de mentira. `por_uf=None` = ignora `ufOab` (comportamento flat)."""

    def __init__(self, total, por_uf=None):
        self.total = total
        self.por_uf = por_uf
        self.chamadas = []

    def count_only(self, sigla, ini, fim):
        self.chamadas.append(('count', None))
        return min(self.total, 10_000)

    def iter_pages(self, sigla, ini, fim):
        self.chamadas.append(('flat', None))
        lidos = 0
        while lidos < self.total:
            n = min(1000, self.total - lidos)
            yield [{'id': f'flat-{lidos + i}'} for i in range(n)]
            lidos += n

    def _fetch(self, sigla, ini, fim, pagina=1, itens_por_pagina=1000,
               extra_params=None, **kw):
        uf = (extra_params or {}).get('ufOab', '??')
        self.chamadas.append(('uf', uf))
        total = (self.por_uf or {}).get(uf, 0)
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
def test_dia_grande_sai_flat_sem_fatiar(tribunal):
    """Dia bem acima de 10.000: nem uma requisição com `ufOab`.

    12 mil e não 261 mil (o volume real do TJSP) porque o que se prova aqui é a
    DECISÃO de rota, e cada item falso custa um round-trip de alerta de drift ao
    banco — 261 mil deles levam a suíte a meia hora sem provar nada a mais.
    """
    cli = DjenFalso(total=12_000)
    with patch.object(I, 'ESTRATEGIA_UF', False):
        run = I.ingest_window(tribunal, date(2026, 8, 13), date(2026, 8, 13), client=cli)

    modos = {m for m, _ in cli.chamadas}
    assert 'flat' in modos
    assert 'uf' not in modos, 'ainda está fatiando por UF'
    assert 'count' not in modos, 'gastou requisição no probe de count à toa'
    assert run.status == run.STATUS_SUCCESS


@pytest.mark.django_db
def test_escotilha_religa_o_caminho_antigo(tribunal):
    """O caminho fatiado continua no código e testado — mas só sob a flag."""
    cli = DjenFalso(total=12_000, por_uf={'SP': 1_200})
    with patch.object(I, 'ESTRATEGIA_UF', True), patch.object(I, 'UF_OABS', ['SP']):
        I.ingest_window(tribunal, date(2026, 8, 13), date(2026, 8, 13), client=cli)
    assert ('uf', 'SP') in cli.chamadas


@pytest.mark.django_db
def test_uma_fatia_perdida_falha_o_run(tribunal):
    """No TJDFT a fatia DF é 77% do dia. Perdê-la e gravar `success` fez o dia
    ficar marcado como coberto pra sempre, com 4.005 de 12.479."""
    cli = DjenFalso(total=0, por_uf={'DF': 900, 'GO': 400})

    def explode(sigla, ini, fim, pagina=1, itens_por_pagina=1000, extra_params=None, **kw):
        if (extra_params or {}).get('ufOab') == 'DF':
            raise RuntimeError('DJEN 500 após 8 tentativas')
        return DjenFalso._fetch(cli, sigla, ini, fim, pagina, itens_por_pagina,
                                extra_params, **kw)

    cli._fetch = explode
    with patch.object(I, 'UF_OABS', ['DF', 'GO']), pytest.raises(RuntimeError):
        I._ingest_day_por_uf(tribunal, date(2025, 8, 13), cli)

    from tribunals.models import IngestionRun
    run = IngestionRun.objects.filter(tribunal=tribunal).order_by('-id').first()
    assert run.status == run.STATUS_FAILED, 'fatia dominante perdida virou success'
