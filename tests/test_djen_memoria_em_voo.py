"""O que fica em voo na coleta de um dia é BYTE, não página.

CONTEXTO (censo e medição de 24/08/2026).

O `FailedJobRegistry` da fila `djen_backfill` tinha **703 entradas**. Censo
COMPLETO (não amostra — `get_job_ids()` devolve por ordem de EXPIRAÇÃO, e ler
"os primeiros N" já apontou pro tribunal errado uma vez):

    342  (48,6%)  OOM — `Work-horse terminated unexpectedly; waitpid returned 9`
                  333 TJDFT · 9 TJAM
    203  (28,9%)  deadlock no Postgres (TRF2/TRF3/TRF6/TRF4/TJSP)
    131  (18,6%)  id órfão (hash expirado, não é falha nova)
     14   (2,0%)  transporte/HTTP · 4 timeout · 1 abandonado

A causa do OOM, medida no TJDFT 2026-08-21 sem gravar nada no banco:

    14.651 publicações no dia ........... 822,6 MB de texto
    ⇒ 56 KB por publicação

`itensPorPagina=1000` nesse tribunal são **55 MB de JSON por requisição**. Com
`DJEN_PAGINAS_PARALELAS=3` e a leva anterior ainda viva enquanto a próxima é
buscada, o pico de RSS medido foi **957 MB** — contra o `mem_limit: 1g` do
`worker_ingestion`. Com `DJEN_PAGINAS_PARALELAS=1`, o mesmo dia deu **640 MB**:
o pico acompanha a JANELA, exatamente como a conta prevê.

A conta que autorizava a janela paralela era "8 páginas de 1000 publicações
≈ 30 MB". Ela assumia publicação de ~3 KB. Para o TJDFT ela erra 27 vezes, e a
diferença entre a estimativa e o número medido é o `mem_limit` do worker.

O que estes testes protegem:
  1. o pico é o ORÇAMENTO, não o tamanho do dia — a marca de um acumulador
     voltando é o pico andar junto com o número de publicações;
  2. o `itensPorPagina` encolhe quando a publicação é pesada e fica cheio
     quando é leve — a variável de ajuste é o item, o teto é o byte;
  3. **encolher não corta**: o dia sai inteiro, em ordem e sem repetição,
     apesar da sonda e das re-âncoras. Reintroduzir teto de coleta é o pecado
     original deste projeto (43,6% do TJSP por `for pagina in range(1, 11)`);
  4. bater no piso de itens é ERRO registrado com o número real (regra nº 2),
     nunca um encolhimento silencioso;
  5. `IngestionRun.erros` não cresce com o dia — ela é re-serializada INTEIRA a
     cada página.
"""
import gc
import json
import tracemalloc

import pytest
from django.test import override_settings

from djen.client import DJENClient, DjenPaginaGrandeError, itens_por_pagina
from djen.parser import MAX_ERROS_NO_RUN, registrar_erro_no_run

KB = 1024
MB = 1024 * 1024


# ═════════════════════════════════════════════════════════════════════════════
# Dublês
# ═════════════════════════════════════════════════════════════════════════════

class ApiPesada:
    """DJEN de mentira com `total` publicações de `kb_por_item` cada.

    Cada item ganha uma string PRÓPRIA (o prefixo muda), senão o CPython
    devolveria o mesmo objeto e o teste mediria ponteiro em vez de memória.
    """

    def __init__(self, total: int, kb_por_item: int):
        self.total = total
        self.corpo = 'x' * (kb_por_item * KB)
        self.pedidas: list[tuple[int, int]] = []

    def __call__(self, sigla, ini, fim, pagina, itens_por_pagina=1000,
                 extra_params=None, max_5xx=None):
        self.pedidas.append((pagina, itens_por_pagina))
        inicio = (pagina - 1) * itens_por_pagina
        n = max(0, min(itens_por_pagina, self.total - inicio))
        return {'items': [{'id': f'i-{inicio + k}', 'texto': f'{k:09d}{self.corpo}'}
                          for k in range(n)]}

    @property
    def bytes_do_dia(self) -> int:
        return self.total * len(self.corpo)


def _cliente(api, janela=8):
    c = DJENClient.__new__(DJENClient)   # sem __init__: não precisa settings/pool
    c.page_sleep = 0
    c.paginas_paralelas = janela
    c._fetch = api
    return c


def _colher(c, api):
    return [it['id'] for pag in c.iter_pages('TJDFT', None, None) for it in pag]


def pico_de_heap(consumir) -> int:
    """Pico do heap do Python durante `consumir()`, em bytes. Mesmo molde do
    `tests/test_diarios_pymupdf.py::pico_de_heap` — aqui tudo que interessa é
    alocação de Python (str/dict do JSON), então o `tracemalloc` vê tudo."""
    gc.collect()
    tracemalloc.start()
    try:
        consumir()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 1. O pico é o orçamento, não o dia
# ═════════════════════════════════════════════════════════════════════════════

@override_settings(DJEN_BYTES_EM_VOO=2 * MB)
def test_o_pico_nao_cresce_com_o_tamanho_do_dia():
    """A assinatura do acumulador é o pico andar junto com o número de
    publicações. Aqui o dia grande tem 10 vezes as publicações do pequeno e o
    peso por item; se o pico acompanhar, alguém voltou a juntar o dia."""
    def consumir(total):
        def _rodar():
            api = ApiPesada(total=total, kb_por_item=8)
            assert len(_colher(_cliente(api, janela=4), api)) == total
        return _rodar

    consumir(500)()                      # aquece (import, thread pool, buffers)

    pico_pequeno = pico_de_heap(consumir(500))     # 4 MB de dia
    pico_grande = pico_de_heap(consumir(5_000))    # 40 MB de dia

    assert pico_grande < pico_pequeno * 2, (
        f'dia de 500 itens={pico_pequeno / 1e6:.1f} MB, '
        f'dia de 5.000={pico_grande / 1e6:.1f} MB — o pico voltou a acompanhar o dia'
    )


@override_settings(DJEN_BYTES_EM_VOO=2 * MB)
def test_o_pico_cabe_no_orcamento_e_nao_no_dia():
    """Número absoluto, não só proporção: um dia de 40 MB de texto tem que
    passar por um processo que nunca chega perto de 40 MB de heap. É este
    teste que falha se o `DJEN_BYTES_EM_VOO` deixar de ser respeitado."""
    api = ApiPesada(total=5_000, kb_por_item=8)

    def consumir():
        assert len(_colher(_cliente(api, janela=4), api)) == 5_000

    consumir()                            # aquece
    pico = pico_de_heap(consumir)

    assert pico < api.bytes_do_dia // 4, (
        f'dia={api.bytes_do_dia / 1e6:.1f} MB, pico={pico / 1e6:.1f} MB — '
        f'orçamento configurado: 2 MB em voo'
    )


# ═════════════════════════════════════════════════════════════════════════════
# 2. O item é a variável de ajuste; o byte é o teto
# ═════════════════════════════════════════════════════════════════════════════

@override_settings(DJEN_BYTES_EM_VOO=48 * MB)
def test_pagina_encolhe_quando_a_publicacao_e_pesada():
    """56 KB por publicação é o TJDFT medido. Com 48 MB de orçamento e 8
    páginas em voo, cabem 6 MB por página — 109 publicações, não 1000."""
    api = ApiPesada(total=3_000, kb_por_item=56)
    _colher(_cliente(api, janela=8), api)

    tamanhos = {sz for _, sz in api.pedidas}
    assert DJENClient.PAGE_SIZE_SONDA in tamanhos      # a sonda mediu primeiro
    depois_da_sonda = {sz for _, sz in api.pedidas[1:]}
    assert depois_da_sonda == {(48 * MB // 8) // (56 * KB)}, depois_da_sonda
    assert 1000 not in tamanhos, 'pediu página cheia num tribunal de 56 KB por item'


@override_settings(DJEN_BYTES_EM_VOO=48 * MB)
def test_pagina_fica_cheia_quando_a_publicacao_e_leve():
    """O orçamento não é uma redução geral: onde a publicação é leve (a maioria
    dos TRFs), a página continua no teto de 1000 da API do CNJ."""
    api = ApiPesada(total=3_000, kb_por_item=2)
    _colher(_cliente(api, janela=8), api)

    assert {sz for _, sz in api.pedidas[1:]} == {DJENClient.PAGE_SIZE}


# ═════════════════════════════════════════════════════════════════════════════
# 3. Encolher NÃO corta — o dia sai inteiro
# ═════════════════════════════════════════════════════════════════════════════

@override_settings(DJEN_BYTES_EM_VOO=48 * MB)
@pytest.mark.parametrize('kb_por_item', [2, 56])
def test_o_dia_sai_inteiro_em_ordem_e_sem_repetir(kb_por_item):
    """O teste que impede o remédio de virar a doença. A sonda e as re-âncoras
    re-pedem offsets já lidos; o recorte de sobreposição tem que devolver
    exatamente o dia — sem buraco (que é perda de acervo) e sem repetição (que
    é INSERT desperdiçado e métrica de duplicata inflada)."""
    api = ApiPesada(total=2_500, kb_por_item=kb_por_item)

    ids = _colher(_cliente(api, janela=8), api)

    assert ids == [f'i-{i}' for i in range(2_500)]


# ═════════════════════════════════════════════════════════════════════════════
# 4. Piso de itens é ERRO registrado, não encolhimento mudo (regra nº 2)
# ═════════════════════════════════════════════════════════════════════════════

@override_settings(DJEN_BYTES_EM_VOO=64 * KB)
def test_piso_de_itens_vira_alerta_com_o_numero_real():
    """Orçamento apertado de propósito: 64 KB ÷ 8 páginas = 8 KB por página, e
    a publicação também pesa 8 KB — cabe UMA, muito abaixo do piso de 25.

    A coleta segue no piso (a alternativa seria não coletar o dia, e dia não
    coletado é perda de acervo), mas o número tem que sair registrado: nesse
    regime o pico VAI passar do orçamento, e alguém precisa saber disso pelo
    log, não pelo SIGKILL."""
    api = ApiPesada(total=300, kb_por_item=8)
    c = _cliente(api, janela=8)

    ids = _colher(c, api)

    assert ids == [f'i-{i}' for i in range(300)], 'o piso não pode cortar o dia'
    avisos = [a for a in c.alertas if a['erro'] == 'orcamento_memoria_no_piso']
    assert avisos, c.alertas
    assert avisos[0]['peso_item_bytes'] >= 8 * KB
    assert avisos[0]['piso_itens'] == DJENClient.PISO_ITENS
    assert avisos[0]['cabem'] < DJENClient.PISO_ITENS


def test_orcamento_e_dividido_pela_janela_em_voo():
    """27 fatias de UF em voo dividem o mesmo orçamento que 8 páginas flat — é
    a SOMA que precisa caber no `mem_limit: 1g`, não cada uma."""
    with override_settings(DJEN_BYTES_EM_VOO=48 * MB):
        flat = itens_por_pagina('TJDFT', 56 * KB, janela=8, teto=1000)
        por_uf = itens_por_pagina('TJDFT', 56 * KB, janela=27, teto=1000)
    assert flat > por_uf
    assert flat * 8 == pytest.approx(por_uf * 27, rel=0.05)


# ═════════════════════════════════════════════════════════════════════════════
# 4b. TETO DURO de bytes por resposta — previsão erra, teto não
# ═════════════════════════════════════════════════════════════════════════════

class _RespostaFalsa:
    """Resposta HTTP de mentira que entrega o corpo em pedaços de 1 MB."""

    def __init__(self, corpo: bytes, declara_tamanho=True):
        self.corpo = corpo
        self.headers = {'Content-Length': str(len(corpo))} if declara_tamanho else {}
        self.fechada = False
        self.baixado = 0

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.corpo), chunk_size):
            pedaco = self.corpo[i:i + chunk_size]
            self.baixado += len(pedaco)
            yield pedaco

    def json(self):
        return json.loads(self.corpo)

    def close(self):
        self.fechada = True


@override_settings(DJEN_BYTES_MAX_RESPOSTA=1 * MB)
def test_resposta_declarada_gigante_nem_e_baixada():
    """`Content-Length` acima do teto: o coletor nem baixa. Baixar pra depois
    decidir é pagar a memória inteira antes de ter opinião sobre ela."""
    c = _cliente(None)
    resp = _RespostaFalsa(b'x' * (3 * MB))

    with pytest.raises(DjenPaginaGrandeError) as e:
        c._ler_com_teto(resp, itens_por_pagina=250)

    assert e.value.declarado and resp.baixado == 0 and resp.fechada


@override_settings(DJEN_BYTES_MAX_RESPOSTA=1 * MB)
def test_resposta_sem_content_length_aborta_no_meio():
    """Sem `Content-Length` (chunked), o corte é durante o download — e nada
    além do teto fica na memória."""
    c = _cliente(None)
    resp = _RespostaFalsa(b'x' * (5 * MB), declara_tamanho=False)

    with pytest.raises(DjenPaginaGrandeError) as e:
        c._ler_com_teto(resp, itens_por_pagina=250)

    assert not e.value.declarado
    assert resp.baixado <= 2 * MB, f'baixou {resp.baixado} — o teto não cortou'


@override_settings(DJEN_BYTES_MAX_RESPOSTA=1 * MB)
def test_no_piso_de_itens_o_teto_cede_e_o_dia_e_coletado():
    """No piso não há como pedir menos, e recusar seria deixar o dia sem
    coletar — o pecado que este projeto não comete. Lê mesmo assim; quem avisa
    é o DJEN_RSS_ALERTA_MB."""
    c = _cliente(None)
    corpo = json.dumps({'items': [{'id': 'x'}]}).encode()
    resp = _RespostaFalsa(corpo)

    assert c._ler_com_teto(resp, itens_por_pagina=DJENClient.PISO_ITENS) == {'items': [{'id': 'x'}]}


def test_teto_de_bytes_reduz_a_pagina_e_nao_perde_item():
    """O teste que separa "teto" de "corte": a resposta grande é RECUSADA, a
    página encolhe e o MESMO offset é relido. O dia tem que sair inteiro, em
    ordem — e o número real vira alerta registrado."""
    TOTAL = 900

    class ApiComPaginaGorda:
        def __init__(self):
            self.pedidas = []

        def __call__(self, sigla, ini, fim, pagina, itens_por_pagina=1000, **kw):
            self.pedidas.append(itens_por_pagina)
            # Acima de 60 itens a resposta "não cabe" — 400 KB por publicação.
            if itens_por_pagina > 60:
                raise DjenPaginaGrandeError(itens_por_pagina * 400 * KB,
                                            24 * MB, itens_por_pagina)
            inicio = (pagina - 1) * itens_por_pagina
            n = max(0, min(itens_por_pagina, TOTAL - inicio))
            return {'items': [{'id': f'i-{inicio + k}', 'texto': 'x' * 4096}
                              for k in range(n)]}

    api = ApiComPaginaGorda()
    c = _cliente(api, janela=4)

    ids = [it['id'] for pag in c.iter_pages('TJDFT', None, None) for it in pag]

    assert ids == [f'i-{i}' for i in range(TOTAL)], 'o teto virou corte de acervo'
    assert max(api.pedidas[1:]) <= 60, 'continuou pedindo página que não cabe'
    aviso = next(a for a in c.alertas if a['erro'] == 'resposta_acima_do_teto_de_bytes')
    assert aviso['peso_item_bytes'] == 400 * KB
    assert aviso['novo_itens_por_pagina'] < aviso['itens_por_pagina']


# ═════════════════════════════════════════════════════════════════════════════
# 5. `IngestionRun.erros` não cresce com o dia
# ═════════════════════════════════════════════════════════════════════════════

class _RunFalso:
    def __init__(self):
        self.erros = []
        self.paginas_lidas = 0


def test_erros_do_run_param_no_teto_e_o_resto_vira_contador():
    """`erros` é JSONField re-serializado INTEIRO a cada `run.save()`, e o save
    acontece uma vez por página. Uma lista que cresce com o número de itens
    ruins é memória O(dia) e escrita quadrática — dentro do mesmo processo que
    o OOM killer matou 342 vezes. O teto guarda o que se lê e conta o resto."""
    run = _RunFalso()

    for i in range(MAX_ERROS_NO_RUN + 5_000):
        registrar_erro_no_run(run, {'erro': 'cnj_indisponivel', 'external_id': str(i)})

    assert len(run.erros) == MAX_ERROS_NO_RUN + 1
    contador = run.erros[-1]
    assert contador['erro'] == 'itens_recusados_alem_do_teto'
    assert contador['total'] == 5_000, 'o número real tem que continuar registrado'


# ═════════════════════════════════════════════════════════════════════════════
# 6. Passar do teto de RSS é ERRO no run, na hora — não SIGKILL calado
# ═════════════════════════════════════════════════════════════════════════════

class _RunGravavel(_RunFalso):
    def __init__(self):
        super().__init__()
        self.pk = 1
        self.salvou = 0

    def save(self, update_fields=None):
        self.salvou += 1


@override_settings(DJEN_RSS_ALERTA_MB=700)
def test_rss_acima_do_teto_vira_erro_no_run_na_hora(monkeypatch):
    """O alerta é gravado NA HORA, não no fim do run. Um alerta que espera o
    fim não sobrevive à morte do processo que ele descreve — foi exatamente
    assim que 342 dias morreram sem deixar um número: o único registro era o
    watchdog escrevendo "worker crashou" uma hora depois."""
    from djen import ingestion as ing

    run = _RunGravavel()
    run.paginas_lidas = 7
    monkeypatch.setattr(ing, '_rss_mb', lambda: 812.4)

    ing._vigiar_memoria(run)

    alerta = next(e for e in run.erros if e['erro'] == 'memoria_acima_do_alerta')
    assert alerta['rss_mb'] == 812.4 and alerta['teto_mb'] == 700
    assert alerta['paginas_lidas'] == 7
    assert run.salvou == 1, 'gravou só no fim — o SIGKILL leva o alerta junto'

    # segunda passagem mais alta: atualiza o MÁXIMO, não duplica a entrada
    run.paginas_lidas = 9
    monkeypatch.setattr(ing, '_rss_mb', lambda: 940.0)
    ing._vigiar_memoria(run)
    assert len([e for e in run.erros if e['erro'] == 'memoria_acima_do_alerta']) == 1
    assert run.erros[-1]['rss_mb'] == 940.0 and run.erros[-1]['paginas_lidas'] == 9


def test_alerta_do_coletor_chega_ao_run_a_cada_pagina():
    """O coletor não conhece o `IngestionRun`, então ele junta os avisos e quem
    grava é a ingestão. Isso tem que acontecer A CADA PÁGINA, não no fim: o
    aviso que mais importa é o que diz que o pico vai passar do orçamento — ou
    seja, o aviso do run que pode não chegar ao fim."""
    from djen import ingestion as ing

    run = _RunGravavel()
    c = _cliente(None)
    c.alertas.append({'erro': 'resposta_acima_do_teto_de_bytes', 'bytes': 40 << 20})

    ing._drenar_alertas(c, run)

    assert run.erros == [{'erro': 'resposta_acima_do_teto_de_bytes', 'bytes': 40 << 20}]
    assert run.salvou == 1, 'guardou o aviso pro fim do run'
    assert c.alertas == [], 'não limpou — o mesmo aviso entraria de novo a cada página'

    ing._drenar_alertas(c, run)          # nada novo: não regrava
    assert run.salvou == 1


@override_settings(DJEN_RSS_ALERTA_MB=700)
def test_rss_abaixo_do_teto_nao_polui_o_run(monkeypatch):
    """O caminho comum não pode encher `erros` — ela é re-serializada inteira a
    cada página."""
    from djen import ingestion as ing

    run = _RunGravavel()
    monkeypatch.setattr(ing, '_rss_mb', lambda: 210.0)
    ing._vigiar_memoria(run)
    assert run.erros == [] and run.salvou == 0


def test_run_none_nao_explode():
    """`_process_page` roda com `run=None` no caminho por-processo
    (`ingest_processo`) — o registro de erro tem que ser no-op ali."""
    registrar_erro_no_run(None, {'erro': 'cnj_indisponivel'})
