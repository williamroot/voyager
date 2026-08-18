"""`iter_pages` busca N páginas do mesmo dia em paralelo — sem mudar o resultado.

CONTEXTO. Serial, o canário do TJSP (2026-08-13, 261.076 publicações) levou
**163 minutos** — 262 requisições em fila indiana, 1,61 página/min. A fase de
maior valor da recuperação nacional são 3.688 dias-tribunal, o que daria 52 dias
de fila com os 8 workers de ingestão.

A página da DJEN é um offset puro: buscar 8 de cada vez não muda o que volta, só
o relógio. O teto de memória continua sendo a janela em voo, não o dia (a mesma
razão pela qual a coleta por UF usa fila limitada — acumular o dia inteiro matou
os workers com OOM em 17/08).

O que estes testes protegem:
  1. a saída é IDÊNTICA à serial, item a item e em ordem;
  2. a janela é respeitada (não vira fan-out ilimitado sobre a API do CNJ);
  3. o downshift de 5xx continua funcionando e não pula item;
  4. "página incompleta seguida de página com dado" vira ERRO — a versão serial
     não conseguia nem enxergar esse caso, e é a assinatura exata do corte mudo
     que já custou 43,6% do TJSP.
"""
import threading
import time

import pytest

from djen.client import DJENClient, DjenClientError, DjenServerError


class ApiFalsa:
    """DJEN de mentira com `total` itens. Registra concorrência observada."""

    def __init__(self, total, quebrar_em=None, mentir_fim_em=None):
        self.total = total
        self.quebrar_em = quebrar_em or {}      # {pagina: n_vezes_5xx}
        self.mentir_fim_em = mentir_fim_em      # página que devolve menos sem ser o fim
        self.pedidas = []
        self.pico = 0
        self._vivos = 0
        self._lock = threading.Lock()

    def __call__(self, sigla, ini, fim, pagina, itens_por_pagina=1000, **kw):
        with self._lock:
            self._vivos += 1
            self.pico = max(self.pico, self._vivos)
            self.pedidas.append((pagina, itens_por_pagina))
        try:
            time.sleep(0.02)   # a DJEN real leva segundos; sem isso as threads
                               # nem chegam a se cruzar e a concorrência some
            if self.quebrar_em.get(pagina, 0) > 0:
                self.quebrar_em[pagina] -= 1
                raise DjenServerError('sistema muito ocupado')
            if pagina == self.mentir_fim_em:
                return {'items': [{'id': 'x'}] * (itens_por_pagina // 2)}
            desde = (pagina - 1) * itens_por_pagina
            n = max(0, min(itens_por_pagina, self.total - desde))
            return {'items': [{'id': f'i-{desde + k}'} for k in range(n)]}
        finally:
            with self._lock:
                self._vivos -= 1


def _cliente(api, janela=8, monkeypatch=None):
    c = DJENClient.__new__(DJENClient)
    c.page_sleep = 0
    c.paginas_paralelas = janela
    c._fetch = api
    return c


def _colher(c):
    from datetime import date
    return [it['id'] for pag in c.iter_pages('TJSP', date(2026, 8, 13), date(2026, 8, 13))
            for it in pag]


def test_resultado_identico_ao_serial():
    """A prova que importa: paralelizar não pode mudar UM item."""
    api_par = ApiFalsa(total=25_500)
    api_ser = ApiFalsa(total=25_500)
    assert _colher(_cliente(api_par, janela=8)) == _colher(_cliente(api_ser, janela=1))


def test_traz_o_dia_inteiro_em_ordem():
    api = ApiFalsa(total=25_500)
    ids = _colher(_cliente(api, janela=8))
    assert len(ids) == 25_500
    assert ids == [f'i-{k}' for k in range(25_500)], 'entregou fora de ordem ou pulou item'


def test_realmente_paralelo_mas_dentro_da_janela():
    api = ApiFalsa(total=25_500)
    _colher(_cliente(api, janela=8))
    assert api.pico > 1, 'não paralelizou nada'
    assert api.pico <= 8, f'estourou a janela ({api.pico}) — fan-out sobre a API do CNJ'


def test_dia_pequeno_nao_fica_mais_caro_em_requisicao_util():
    """300 itens: a primeira página já é o fim. A janela pede as outras 7 (custo
    fixo e barato), mas não pode pedir uma SEGUNDA rodada."""
    api = ApiFalsa(total=300)
    assert len(_colher(_cliente(api, janela=8))) == 300
    assert max(p for p, _ in api.pedidas) <= 8, 'pediu rodada além do fim'


def test_downshift_de_5xx_continua_funcionando():
    """A DJEN 500a em página pesada; o cliente reduz o page size e retoma do
    mesmo offset de itens. Nenhum item pode se perder nisso."""
    # falha UMA vez e depois responde — comportamento real da DJEN em página
    # pesada. Página quebrada pra sempre é falha de verdade e deve propagar:
    # a escada de downshift é 1000 → 200 → 100, e no piso o erro sobe.
    api = ApiFalsa(total=3_500, quebrar_em={2: 1})
    ids = _colher(_cliente(api, janela=4))
    assert len(set(ids)) == 3_500, 'perdeu item no downshift'
    assert any(ipp < 1000 for _, ipp in api.pedidas), 'não chegou a reduzir o page size'


def test_fim_mentiroso_vira_erro():
    """Página incompleta seguida de página COM dado = a paginação mentiu.

    A versão serial parava ali e gravava `success` — que é exatamente a
    assinatura do corte mudo que custou 43,6% do TJSP. Agora grita.
    """
    api = ApiFalsa(total=25_500, mentir_fim_em=3)
    with pytest.raises(DjenClientError, match='paginação inconsistente'):
        _colher(_cliente(api, janela=8))
