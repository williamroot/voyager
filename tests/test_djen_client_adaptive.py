"""iter_pages adaptativo: reduz page size quando a DJEN devolve 5xx em página
pesada e retoma do mesmo offset, sem pular nem (efetivamente) duplicar itens.

Desde 24/08/2026 a 1ª página do dia é a SONDA (`PAGE_SIZE_SONDA`, 100 itens):
ela mede quanto pesa uma publicação daquele tribunal antes de a paginação
comprometer memória. Não é redução por 5xx e não é teto de coleta — a página
volta ao tamanho cheio na leva seguinte quando o item é leve. Ver
`DJENClient.iter_pages` e tests/test_djen_memoria_em_voo.py.
"""
from djen.client import DJENClient, DjenServerError


def _client():
    c = DJENClient.__new__(DJENClient)  # sem __init__ (não precisa settings/pool)
    c.PAGE_SIZE = 1000
    c.page_sleep = 0
    c.max_retries = 5
    # janela 1 = paginação serial. Estes testes contam REQUISIÇÃO por requisição
    # pra provar o downshift, e uma janela paralela pediria páginas além do fim
    # (baratas, mas ruído aqui). O paralelismo tem suíte própria em
    # tests/test_djen_paginacao_paralela.py.
    c.paginas_paralelas = 1
    return c


def test_iter_pages_reduz_em_5xx_e_cobre_tudo():
    DATA = [{'id': i} for i in range(250)]
    calls = []

    def fake_fetch(sigla, ini, fim, pagina, itens_por_pagina=1000, extra_params=None, max_5xx=None):
        calls.append((pagina, itens_por_pagina))
        # Simula o bug real: página pesada (size grande) 500a; size pequeno responde.
        if itens_por_pagina > DJENClient.MIN_PAGE_SIZE:
            raise DjenServerError('500 simulado em page grande')
        start = (pagina - 1) * itens_por_pagina
        return {'items': DATA[start:start + itens_por_pagina]}

    c = _client()
    c._fetch = fake_fetch
    out = [x for page in c.iter_pages('TJX', None, None) for x in page]

    # cobertura completa, em ordem, sem buraco nem duplicata
    assert [x['id'] for x in out] == list(range(250))
    # A maior página tentada é a SONDA — desde 24/08/2026 o dia não começa mais
    # na página cheia, ele começa medindo. O que este teste guarda é o
    # downshift: tentou o tamanho maior antes de descer até o piso.
    assert (1, DJENClient.PAGE_SIZE_SONDA) in calls
    assert max(sz for _, sz in calls) == DJENClient.PAGE_SIZE_SONDA
    assert any(sz == DJENClient.MIN_PAGE_SIZE for _, sz in calls)


def test_iter_pages_sem_5xx_usa_page_size_cheio():
    DATA = [{'id': i} for i in range(1500)]
    calls = []

    def fake_fetch(sigla, ini, fim, pagina, itens_por_pagina=1000, extra_params=None, max_5xx=None):
        calls.append((pagina, itens_por_pagina))
        start = (pagina - 1) * itens_por_pagina
        return {'items': DATA[start:start + itens_por_pagina]}

    c = _client()
    c._fetch = fake_fetch
    out = [x for page in c.iter_pages('TJX', None, None) for x in page]

    assert [x['id'] for x in out] == list(range(1500))
    # A sonda é a 1ª e só ela é pequena; sem 5xx, nada mais reduz.
    assert calls[0] == (1, DJENClient.PAGE_SIZE_SONDA)
    assert all(sz == 1000 for _, sz in calls[1:])
