"""Trava a troca de extrator de PDF do DJE/TJSP: pypdf → PyMuPDF (ADR-031).

CONTEXTO — os 15,1%, e a honestidade sobre o que eles são
--------------------------------------------------------
Medido em 20/08/2026 (não estimado) no caderno 3 do DJE/TJSP — Judicial 1ª
Instância, Capital Parte I, 12/03/2025, 36 MB, 4.229 páginas — contando CNJ
DISTINTO com a regex ESTRITA (`NNNNNNN-DD.AAAA.J.TR.OOOO`, sem espaço):

    PyMuPDF (MuPDF/C)   16,3 s   32.366 CNJs
    pdftotext (poppler) 17,3 s   32.366 CNJs
    pypdf 5.1.0        184,0 s   27.483 CNJs   ← 15,1% a menos

A causa está medida, e não é do PDF: o pypdf insere um espaço espúrio DENTRO do
próprio número ('1127986- 08.2023.8.26.0100'), e número quebrado não casa com a
regex. É o mesmo defeito que produzia 'Estado de S ão Paulo' e 'Banco Rodoben s
S/A' no `texto` verbatim (4.283 ocorrências de 'Paul o' neste caderno, contra 0
com o MuPDF).

O que estes testes NÃO afirmam — e não podem passar a afirmar
------------------------------------------------------------
Que a troca "recupera 15% de processos". Ela não recupera: dentro deste
pipeline os 15,1% NÃO se materializam, porque a `CNJ_TOLERANTE` de
`diarios/base.py` aceita espaço entre quaisquer dígitos e já absorvia o defeito.
Reconferido nesta máquina no mesmo caderno, com os dois extratores: **32.575
CNJs tolerantes pelos dois**, 0 de diferença nos dois sentidos.

Os 15,1% são o tamanho da armadilha para quem lê o `texto` gravado com regex
PRÓPRIA — extrator de autos, busca, exportação, o cliente — e é aí que o dado
sujo vira processo perdido sem ninguém saber. O ganho direto e medido é outro:
6,4x de CPU e 3,2x de RSS (184,1 s / 425 MB → 44,8 s / 185 MB nesta mesma
máquina, caderno inteiro), o que decide um backfill de ~37 mil cadernos.

O que este arquivo trava
------------------------
1. **fluxo, não acúmulo** — 4.229 páginas (35,3 MB de texto) não podem caber
   numa lista; foi tirando um teto de páginas que a casa trocou perda silenciosa
   por OOM no mesmo dia;
2. **CNJ inteiro** — o número não pode sair partido ao meio pelo EXTRATOR
   (quebra de linha impressa é outra coisa: é verdade do caderno);
3. **falta da lib é ERRO ALTO e explícito** — nunca fonte sumindo do registro em
   silêncio, nem `RespostaInvalida` (que o runner retentaria 5x).
"""

import gc
import os
import re
import sys
import tracemalloc
from itertools import islice

import pytest

from diarios.base import CNJ_TOLERANTE, RespostaInvalida
from diarios.fontes.tjsp_dje import pdf

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios', 'tjsp_esaj')

#: 95 páginas, ~1 MB — VERSIONADA. O que roda em clone limpo.
FIXTURE_PARTE2 = 'caderno20_capital_parteII_20250721.pdf'
#: o caderno dos 15,1%: 36 MB, 4.229 páginas. Pesada, fora do git (ver
#: tests/fixtures/diarios/README.md §2) — sem ela o gate PULA, de propósito.
FIXTURE_GRANDE = 'caderno3_capital_parteI_20250312.pdf'

#: a regex de quem lê o `texto` gravado sem saber do espaço espúrio — é ela que
#: mede o estrago, não a `CNJ_TOLERANTE` (que existe justamente para escondê-lo).
CNJ_ESTRITA = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')

#: piso do canário do caderno grande. O medido foi 32.366; o pypdf dava 27.483.
#: A folga de 366 é para tolerar variação de extrator, não meia edição.
PISO_CNJ_CADERNO_GRANDE = 32_000


def fx(nome: str) -> str:
    return os.path.join(FIXTURES, nome)


def tem(nome: str) -> bool:
    return os.path.exists(fx(nome))


def ler_bytes(nome: str) -> bytes:
    with open(fx(nome), 'rb') as fh:
        return fh.read()


def pico_de_heap(consumir) -> int:
    """Pico do heap do PYTHON durante `consumir()`, em bytes.

    O `tracemalloc` só vê alocação de Python — a memória nativa do MuPDF fica
    fora, e quem responde por ela é `pdf.fechar()`. Aqui o que se mede é
    exatamente o que interessa a este teste: se as `Pagina`/`Linha` do caderno
    inteiro estão sendo empilhadas.
    """
    gc.collect()
    tracemalloc.start()
    try:
        consumir()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 1. A extração é em FLUXO — não acumula o caderno em memória
# ═════════════════════════════════════════════════════════════════════════════

class _PaginaFalsa:
    """Página que sabe dizer se alguém pediu o texto dela."""

    def __init__(self, numero: int):
        self.numero = numero
        self.lida = False


class _DocumentoFalso:
    """Dublê do `Document` do MuPDF com o tamanho do caderno REAL (4.229
    páginas). Existe para provar preguiça sem gastar 45 s de CPU."""

    def __init__(self, page_count: int = 4229):
        self.page_count = page_count
        self.paginas = [_PaginaFalsa(i + 1) for i in range(page_count)]
        self.fechado = False

    def __getitem__(self, i):
        return self.paginas[i]

    def close(self):
        self.fechado = True

    @property
    def lidas(self) -> int:
        return sum(1 for p in self.paginas if p.lida)


@pytest.fixture
def caderno_falso(monkeypatch):
    doc = _DocumentoFalso()
    monkeypatch.setattr(pdf, 'abrir', lambda corpo: doc)

    def _linhas(pagina_pdf, numero):
        pagina_pdf.lida = True
        return [pdf.Linha(pagina=numero, tamanho=8.0, texto=f'pagina {numero}')]

    monkeypatch.setattr(pdf, '_linhas_da_pagina', _linhas)
    return doc


def test_paginas_so_extrai_a_pagina_que_o_consumidor_pediu(caderno_falso):
    """`paginas()` é generator de verdade: nada é extraído antes do primeiro
    `next()`, e uma página consumida é UMA página extraída.

    Não é preciosismo de API. O consumidor real (`coletor.coletar`) empurra as
    páginas para o segmentador e para o gate de cobertura; se a extração fosse
    ansiosa, o caderno de 4.229 páginas (35,3 MB de texto, 320 mil `Linha`)
    estaria inteiro no heap antes do primeiro bloco sair — e a casa já trocou
    uma perda silenciosa por um OOM exatamente assim.
    """
    gerador = pdf.paginas(b'%PDF-falso')
    assert caderno_falso.lidas == 0, 'extraiu antes de alguém pedir'

    primeiras = list(islice(gerador, 3))

    assert [p.numero for p in primeiras] == [1, 2, 3]
    assert caderno_falso.lidas == 3, 'extraiu páginas que ninguém pediu'
    gerador.close()


def test_paginas_devolve_a_memoria_nativa_quando_o_consumidor_desiste(caderno_falso):
    """Abandonar o generator no meio é o caminho NORMAL, não a exceção: o gate
    de cobertura levanta `ColetorError` no meio da iteração. Sem o `finally`, o
    `Document` (memória fora do heap do Python) ficaria pendurado até o GC — 37
    mil vezes, num worker que também roda extração e vetorização."""
    gerador = pdf.paginas(b'%PDF-falso')
    next(gerador)
    assert caderno_falso.fechado is False

    gerador.close()

    assert caderno_falso.fechado is True


@pytest.mark.skipif(not tem(FIXTURE_PARTE2), reason='fixture da parte II ausente')
def test_streaming_nao_acumula_as_paginas_do_caderno_no_heap():
    """No caderno REAL: iterar em fluxo tem que custar uma fração do que custa
    juntar as páginas numa lista — e o custo do fluxo NÃO pode crescer com o
    tamanho do caderno.

    Medido nesta fixture (95 páginas, 245.697 chars) no container de dev:
    fluxo 0,22 MB de pico contra 1,48 MB da lista, 6,7x. O teste exige só 3x
    para não virar teste de alocador; o que ele está travando é a FORMA
    (generator), que é o que some numa refatoração distraída.
    """
    corpo = ler_bytes(FIXTURE_PARTE2)

    def fluxo():
        return sum(len(pagina.texto) for pagina in pdf.paginas(corpo))

    def lista():
        todas = list(pdf.paginas(corpo))
        return sum(len(pagina.texto) for pagina in todas)

    fluxo()  # aquece: a 1ª chamada paga o cache interno do MuPDF, não o caderno

    pico_fluxo = pico_de_heap(fluxo)
    pico_lista = pico_de_heap(lista)

    assert pico_fluxo * 3 < pico_lista, (
        f'fluxo={pico_fluxo / 1e6:.2f} MB, lista={pico_lista / 1e6:.2f} MB — '
        'a extração voltou a acumular o caderno'
    )


@pytest.mark.skipif(not tem(FIXTURE_PARTE2), reason='fixture da parte II ausente')
def test_o_pico_do_fluxo_nao_cresce_com_o_numero_de_paginas():
    """A prova de que o consumo é O(1) por página e não O(páginas): consumir 10
    páginas e consumir as 95 têm que custar a mesma ordem de grandeza. Um
    acumulador escondido (um `list.append` de debug, um cache por página)
    aparece aqui como crescimento linear."""
    corpo = ler_bytes(FIXTURE_PARTE2)

    def consumir(quantas=None):
        def _rodar():
            fonte = pdf.paginas(corpo)
            paginas = islice(fonte, quantas) if quantas else fonte
            total = sum(len(p.texto) for p in paginas)
            fonte.close()
            return total
        return _rodar

    consumir(5)()  # aquecimento

    pico_10 = pico_de_heap(consumir(10))
    pico_tudo = pico_de_heap(consumir())

    assert pico_tudo < pico_10 * 3, (
        f'10 páginas={pico_10 / 1e6:.2f} MB, 95 páginas={pico_tudo / 1e6:.2f} MB — '
        'o pico está acompanhando o número de páginas'
    )


# ═════════════════════════════════════════════════════════════════════════════
# 2. O CNJ não sai partido ao meio pelo extrator
# ═════════════════════════════════════════════════════════════════════════════

def _cnjs_partidos_por_linha(corpo: bytes) -> list[str]:
    """Linhas em que a `CNJ_TOLERANTE` acha mais números que a ESTRITA — ou
    seja, em que o número só é legível porque alguém tolerou espaço no meio
    dele. Comparação DENTRO da linha de propósito: quebra de linha impressa
    ('0002419-\\n32.2019...') é verdade do caderno, não defeito do extrator."""
    partidas = []
    for pagina in pdf.paginas(corpo):
        for linha in pagina.linhas:
            tolerantes = CNJ_TOLERANTE.findall(linha.texto)
            estritos = CNJ_ESTRITA.findall(linha.texto)
            if len(tolerantes) > len(estritos):
                partidas.append(linha.texto)
    return partidas


@pytest.mark.skipif(not tem(FIXTURE_PARTE2), reason='fixture da parte II ausente')
def test_cnj_com_hifen_nao_vem_quebrado_pelo_extrator():
    """O número do processo sai inteiro do extrator — hífen e pontos colados nos
    dígitos, do jeito que foi impresso. É o defeito que fazia o pypdf devolver
    '1127986- 08.2023.8.26.0100'.

    Honestidade sobre o alcance deste teste: nesta fixture pequena o pypdf
    TAMBÉM entrega 0 partidos (medido: 961 CNJs na linha, 0 partidos pelos
    dois). Ele é guarda de regressão, não o teste que discrimina — quem
    discrimina é `test_o_caderno_de_4229_paginas_...`, onde a diferença medida é
    23 linhas contra 5.866.
    """
    corpo = ler_bytes(FIXTURE_PARTE2)

    partidas = _cnjs_partidos_por_linha(corpo)
    total = sum(len(CNJ_ESTRITA.findall(p.texto)) for p in pdf.paginas(corpo))

    assert total > 900, f'só {total} CNJs na fixture — a fixture mudou, não o extrator'
    assert partidas == [], f'{len(partidas)} linhas com CNJ partido: {partidas[:3]}'


@pytest.mark.skipif(not tem(FIXTURE_PARTE2), reason='fixture da parte II ausente')
def test_o_espaco_esporio_do_kerning_nao_aparece_no_texto_verbatim():
    """'Estado de S ão Paulo' era o mesmo defeito do CNJ partido, visível a olho
    nu — e o `texto` daqui é gravado verbatim em `Movimentacao`, lido pelo
    extrator de autos e exportado ao cliente. Medido nesta fixture: pypdf
    escrevia 'Paul o' 95 vezes; o MuPDF, nenhuma."""
    corpo = ler_bytes(FIXTURE_PARTE2)

    texto = '\n'.join(pagina.texto for pagina in pdf.paginas(corpo))

    assert 'Paulo' in texto, 'a fixture deixou de conter "Paulo" — teste sem lastro'
    assert texto.count('Paul o') == 0
    assert texto.count('S ão') == 0


@pytest.mark.skipif(not tem(FIXTURE_GRANDE), reason='caderno de 36 MB não commitado')
def test_o_caderno_de_4229_paginas_entrega_os_32_mil_cnjs():
    """CANÁRIO da troca, no caderno que a motivou (36 MB, 4.229 páginas).

    Gate: ≥ 32.000 CNJs distintos pela regex ESTRITA. Medido no container de
    dev em 20/08/2026, com os dois extratores, no mesmo arquivo:

        PyMuPDF  32.366 estritos · 32.575 tolerantes · 44,8 s · RSS 185 MB
        pypdf    27.483 estritos · 32.575 tolerantes · 184,1 s · RSS 425 MB

    Ver 27.483 aqui não é "o gate ficou apertado": é a troca ter sido desfeita
    (ou a imagem ter subido sem `pymupdf` e alguém ter feito o fallback voltar).
    A igualdade dos tolerantes é o que impede este teste de virar a mentira
    "recuperamos 15% de processos".

    A segunda asserção é a mesma coisa vista por dentro da LINHA, que é onde o
    defeito mora: linhas em que só a `CNJ_TOLERANTE` enxerga o número. Medido
    neste caderno: **23** linhas com o MuPDF (0,06% — e são espaço realmente
    impresso, do tipo '1010872-51.2023. 8.26.0002') contra **5.866** com o pypdf
    (15,5% — '1127986- 08.2023.8.26.0100'). O teto de 100 separa as duas
    ordens de grandeza sem virar teste de dígito.
    """
    corpo = ler_bytes(FIXTURE_GRANDE)

    distintos: set[str] = set()
    linhas_partidas = 0
    paginas = 0
    for pagina in pdf.paginas(corpo):
        paginas += 1
        for linha in pagina.linhas:
            estritos = CNJ_ESTRITA.findall(linha.texto)
            distintos.update(estritos)
            if len(CNJ_TOLERANTE.findall(linha.texto)) > len(estritos):
                linhas_partidas += 1

    assert paginas == 4229, f'o caderno tem {paginas} páginas — fixture trocada'
    assert len(distintos) >= PISO_CNJ_CADERNO_GRANDE, (
        f'{len(distintos)} CNJs distintos (piso {PISO_CNJ_CADERNO_GRANDE}). '
        '~27.483 significa extrator antigo de volta, não caderno diferente.'
    )
    assert linhas_partidas <= 100, (
        f'{linhas_partidas} linhas com CNJ partido ao meio (teto 100; medido 23). '
        '~5.866 é a assinatura do espaço espúrio do pypdf.'
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3. Sem a lib, erro ALTO e explícito — não fonte sumindo em silêncio
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sem_pymupdf(monkeypatch):
    """Simula a imagem que subiu sem a dependência — o cenário REAL de hoje:
    conferido em 20/08/2026, o `voyager-web-1` de prod tem pypdf 5.1.0 e NÃO tem
    pymupdf. `None` em `sys.modules` é como o Python sinaliza import bloqueado."""
    monkeypatch.setitem(sys.modules, 'pymupdf', None)


def test_sem_a_lib_abrir_levanta_erro_explicito_dizendo_o_que_fazer(sem_pymupdf):
    """A mensagem tem que nomear o pacote, a versão e o REBUILD.

    'ModuleNotFoundError: No module named pymupdf' no meio de um traceback de
    worker manda o plantonista ler código. Dizer que a imagem precisa de rebuild
    (git pull não basta, o requirements.txt já tem a linha) resolve o incidente
    sem ele abrir o repositório.
    """
    with pytest.raises(RuntimeError) as erro:
        pdf.abrir(b'%PDF-1.4 qualquer coisa')

    mensagem = str(erro.value)
    assert 'pymupdf' in mensagem
    assert 'requirements' in mensagem
    assert 'rebuild' in mensagem.lower()


def test_a_falta_da_lib_nao_e_falha_retentavel(sem_pymupdf):
    """Dependência ausente NÃO pode virar `RespostaInvalida`.

    A hierarquia de erro do `diarios/base.py` decide o que o runner faz: erro de
    resposta é recuperável e a unidade é retentada até `MAX_TENTATIVAS=5`.
    Retentar 5x (baixando 36 MB a cada vez) uma imagem sem dependência é castigar
    a fonte por um problema nosso, e ainda esconde a causa atrás de um contador
    de tentativas.
    """
    with pytest.raises(RuntimeError) as erro:
        pdf.abrir(b'%PDF-1.4 qualquer coisa')

    assert not isinstance(erro.value, RespostaInvalida)


def test_o_import_do_pymupdf_e_tardio_para_a_fonte_nao_sumir_do_registro(sem_pymupdf):
    """O módulo continua importável sem a lib — e isso é deliberado.

    `diarios/apps.py` auto-descobre as fontes importando `coletor.py` na subida
    do Django e engole exceção de import: uma dependência faltando no topo do
    arquivo faria o `tjsp-dje` DESAPARECER do registro em silêncio — sem coletor,
    sem erro, sem run. Com o import dentro de `abrir()`, a fonte continua
    listada e o problema aparece na coleta, com nome e sobrenome.
    """
    import importlib

    modulo = importlib.import_module('diarios.fontes.tjsp_dje.coletor')

    assert modulo.pdf is pdf, 'o coletor deixou de usar este módulo de PDF'
    assert 'pymupdf' not in vars(pdf), 'o pymupdf subiu para o topo do módulo'


@pytest.mark.skipif(not tem(FIXTURE_PARTE2), reason='fixture da parte II ausente')
def test_a_troca_pegou_o_leitor_e_do_mupdf():
    """Guarda de sanidade contra o pior cenário do canário: tudo verde, número
    redondo e o pypdf de volta no caminho sem ninguém notar. Aqui o teste olha
    QUEM abriu o PDF, não o resultado."""
    leitor = pdf.abrir(ler_bytes(FIXTURE_PARTE2))
    try:
        assert type(leitor).__module__.split('.')[0] in {'pymupdf', 'fitz'}
        assert leitor.page_count > 0
    finally:
        pdf.fechar(leitor)
