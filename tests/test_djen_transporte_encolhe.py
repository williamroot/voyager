"""Página que o TRANSPORTE não entrega encolhe — não mata o dia (24/08/2026).

CONTEXTO MEDIDO. Com o OOM e o deadlock fora do caminho, os dias do TJDFT que
ainda deviam morriam todos com a mesma assinatura no `IngestionRun`:

    24/08 15:13 failed  16 min  pgs=1  n=0 d=100
                erro: "erro de transporte após 8 tentativas: HTTPSConnectionPool
                       (host='comunicaapi.pje.jus.br', port=443): Max retries..."
    23/08 21:34 failed  62 min  pgs=3  n=0 d=3000   ← watchdog: "worker crashou"
    23/08 11:26 failed  61 min  pgs=3  n=0 d=3000
    21/08 22:54 failed  62 min  pgs=3  n=0 d=3000
    ... 8 tentativas seguidas, dia nenhum coletado

Três páginas em uma hora não é worker crashado: é o corpo não chegando. A
publicação do TJDFT pesa 56 KB em média e chegou a 766,9 KB numa leva; uma
página de 250 itens são dezenas de MB que o proxy residencial não entrega
dentro do `read timeout` de 60 s. As 8 tentativas queimam ~10 min **no mesmo
offset**, e aí o dia inteiro morre.

O `Content-Length` (teto de bytes) só protege contra quem DECLARA o tamanho.
Quem cai no meio do download não declara nada — e o remédio é o mesmo:
encolher a página e reler o MESMO offset. Custa requisição, não item.

Estes testes guardam as duas metades da lição:
  1. o dia sai INTEIRO, em ordem, quando o transporte recusa a página grande;
  2. quando nem o piso passa, a exceção SOBE (não inventamos dia coletado).
"""
import pytest

from djen.client import DJENClient, DjenTransporteError

KB = 1024


def _cliente(api, janela=4):
    c = DJENClient.__new__(DJENClient)   # sem __init__: não precisa settings/pool
    c.page_sleep = 0
    c.paginas_paralelas = janela
    c._fetch = api
    return c


class ApiQueNaoEntregaPaginaGrande:
    """Devolve dado só quando a página é pequena; acima disso, timeout."""

    def __init__(self, total: int, teto_que_passa: int):
        self.total = total
        self.teto = teto_que_passa
        self.pedidas: list[int] = []

    def __call__(self, sigla, ini, fim, pagina, itens_por_pagina=1000, **kw):
        self.pedidas.append(itens_por_pagina)
        if itens_por_pagina > self.teto:
            raise DjenTransporteError(
                f'erro de transporte após 8 tentativas '
                f'(itensPorPagina={itens_por_pagina}): Read timed out')
        inicio = (pagina - 1) * itens_por_pagina
        n = max(0, min(itens_por_pagina, self.total - inicio))
        return {'items': [{'id': f'i-{inicio + k}', 'texto': 'x' * 4096}
                          for k in range(n)]}


def test_o_dia_sai_inteiro_quando_o_transporte_recusa_a_pagina():
    TOTAL = 900
    api = ApiQueNaoEntregaPaginaGrande(TOTAL, teto_que_passa=60)
    c = _cliente(api)

    ids = [it['id'] for pag in c.iter_pages('TJDFT', None, None) for it in pag]

    assert ids == [f'i-{i}' for i in range(TOTAL)], \
        'timeout de transporte virou corte de acervo'
    # o caminho é 250 → 125 → 62 → 31: as tentativas grandes acontecem UMA vez
    # cada (é assim que se descobre o tamanho que passa), e a partir daí a
    # página fica onde chega.
    assert api.pedidas[-1] <= 60, 'terminou pedindo página que não chega'
    assert api.pedidas.count(125) == 1 and api.pedidas.count(62) == 1, \
        'insistiu no mesmo tamanho recusado em vez de encolher'


def test_a_recusa_de_transporte_vira_alerta_com_o_numero():
    """Regra nº 2: encolher é decisão do coletor e tem que ficar registrada."""
    api = ApiQueNaoEntregaPaginaGrande(300, teto_que_passa=50)
    c = _cliente(api)

    list(c.iter_pages('TJDFT', None, None))

    aviso = next(a for a in c.alertas
                 if a['erro'] == 'transporte_nao_entregou_a_pagina')
    assert aviso['novo_itens_por_pagina'] < aviso['itens_por_pagina']
    assert aviso['tribunal'] == 'TJDFT'


def test_no_piso_a_excecao_sobe_em_vez_de_fingir_dia_vazio():
    """Se nem 25 itens chegam, o dia NÃO foi coletado — e tem que doer."""
    api = ApiQueNaoEntregaPaginaGrande(300, teto_que_passa=0)
    c = _cliente(api)

    with pytest.raises(DjenTransporteError):
        list(c.iter_pages('TJDFT', None, None))

    assert min(api.pedidas) <= DJENClient.PISO_ITENS, \
        'desistiu antes de tentar o piso de itens'


def test_o_encolhimento_do_transporte_e_herdado_pelo_resto_do_dia():
    """Sem herdar, a página cresce de novo e cada recusa custa ~10 min."""
    api = ApiQueNaoEntregaPaginaGrande(4_000, teto_que_passa=60)
    c = _cliente(api)

    list(c.iter_pages('TJDFT', None, None))

    assert all(b <= a for a, b in zip(api.pedidas, api.pedidas[1:], strict=False)), \
        f'a calibração reinflou a página recusada — ping-pong: {api.pedidas[:12]}'
