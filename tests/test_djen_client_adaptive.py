"""iter_pages adaptativo: reduz page size quando a DJEN devolve 5xx em página
pesada e retoma do mesmo offset, sem pular nem (efetivamente) duplicar itens."""
from djen.client import DJENClient, DjenServerError


def _client():
    c = DJENClient.__new__(DJENClient)  # sem __init__ (não precisa settings/pool)
    c.PAGE_SIZE = 1000
    c.page_sleep = 0
    c.max_retries = 5
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
    # tentou o tamanho grande antes de reduzir até o piso
    assert (1, 1000) in calls
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
    # nunca reduziu: todas as chamadas a 1000
    assert all(sz == 1000 for _, sz in calls)
