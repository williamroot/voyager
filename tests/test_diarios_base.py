"""Testes do contrato compartilhado dos coletores de diário (`diarios/base.py`).

Testam COMPORTAMENTO com material real capturado nas sondas de 16/08/2026 —
inclusive os três "HTTP 200 que não é dado" que o e-SAJ devolve. Os trechos de
texto são verbatim das fixtures em `tests/fixtures/diarios/`; os testes que
precisam do arquivo inteiro (10 MB, não commitado) marcam skip quando ele não
está presente, em vez de falhar em máquina limpa.
"""

import os
from datetime import UTC, date, datetime

import pytest

from diarios.base import (
    MAX_EXTERNAL_ID,
    RespostaInvalida,
    achar_cnjs,
    exigir_ancora,
    exigir_pdf,
    external_id_de,
    fingerprint_ato,
    id_bloco_impresso,
    validar_slug,
)

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios')


# ── CNJ tolerante: o ruído de PDF que come ~8% dos processos ────────────────
# Trecho VERBATIM do caderno 12 do DJE/TJSP de 21/07/2025 (pág. do Colégio
# Recursal). Repare no 'Recurso nº: 1 002997-71...': o extrator injeta espaço
# no meio do número por causa do kerning. A regex estrita não vê este processo.
TRECHO_REAL_TJSP = (
    'Colégio Recursal dos Juizados Especiais - Recurso nº: 1 002997-71.2023.8.26.0344 '
    '- recurso: Luís Gustavo da Silva \nPires). (g) Portanto, verificada a incompetência '
    'deste eg. Colé gio Recursal, deixo de conhecer do presente recurso'
)


def test_cnj_tolerante_recupera_numero_quebrado_por_espaco():
    achados = achar_cnjs(TRECHO_REAL_TJSP)
    assert '1002997-71.2023.8.26.0344' in achados, (
        'a regex tolerante tem que remontar o CNJ quebrado pelo extrator de PDF'
    )


def test_cnj_tolerante_nao_inventa_numero():
    assert achar_cnjs('processo 123 de 2024, fls. 45/2019') == []


def test_cnj_decapitado_por_kerning_nao_vira_processo_de_outra_pessoa():
    """O preço da tolerância a espaço, medido em 16/08/2026: com dois dígitos
    colados pelo kerning ('991000001-11...'), a regex casava a partir do
    terceiro dígito e devolvia '1000001-11.2015.8.26.0100' — que é o CNJ de
    OUTRO processo, existente. Grudar no processo errado é pior que perder, e é
    o que a casa proíbe. Adjacência de dígito ⇒ abstém."""
    # o pedaço decapitado ('1000001-37...') tem DV VÁLIDO — é processo de
    # verdade. Quem salva aqui é a adjacência, não o dígito verificador.
    assert achar_cnjs('Processo 991000001-37.2015.8.26.0100') == []
    # e o número íntegro, com pontuação normal em volta, continua sendo achado
    assert achar_cnjs('Processo 1000001-37.2015.8.26.0100.') == ['1000001-37.2015.8.26.0100']


def test_cnj_com_digito_verificador_errado_e_abstencao():
    """Res. CNJ 65/2008: o DV é módulo 97. Um número com DV que não fecha não é
    processo — é erro de impressão ou recorte errado nosso. Medido: 2 em 35.289
    itens do TJSP, e os 2 tinham virado `Process` no banco de dev. Numa missão
    cujo objetivo é MEDIR acervo, processo fantasma é o pior defeito possível."""
    from diarios.base import dv_cnj_valido

    assert dv_cnj_valido('1099663-22.2025.8.26.0100')
    assert not dv_cnj_valido('1099663-23.2025.8.26.0100')
    assert not dv_cnj_valido('0000000-00.2026.8.02.9003')
    assert achar_cnjs('consta o processo 1099663-23.2025.8.26.0100 nos autos') == []


def test_cnj_de_tabela_com_horario_ao_lado_continua_sendo_achado():
    """A guarda de adjacência é ESTRITA (não pula espaço) por causa disto: a
    tabela de convocação de Maceió imprime o CNJ e o horário separados por um
    espaço só. Pular o branco reprovaria dado bom — foi medido na fixture real
    do Querido Diário."""
    assert achar_cnjs('Maicon dos Santos Freitas 0501276-27.2026.8.02.9003 09:00 1') == [
        '0501276-27.2026.8.02.9003']


def test_cnj_tolerante_preserva_ordem_e_desduplica():
    texto = ('Nº 2217577-02.2025.8.26.0000 ... Nº 1099663-22.2025.8.26.0100 ... '
             'de novo 2217577-02.2025.8.26.0000')
    assert achar_cnjs(texto) == ['2217577-02.2025.8.26.0000', '1099663-22.2025.8.26.0100']


@pytest.mark.skipif(
    not os.path.exists(os.path.join(FIXTURES, 'tjsp_esaj', 'caderno12_20250721.txt')),
    reason='fixture de 3,7 MB não está presente (não é commitada)',
)
def test_tolerante_acha_mais_que_estrita_no_caderno_inteiro():
    """O ganho da regex tolerante não é teórico: no caderno 12 de 21/07/2025 ela
    acha centenas de processos a mais que a estrita — que sumiriam em silêncio."""
    import re

    from diarios.base import CNJ_TOLERANTE
    caminho = os.path.join(FIXTURES, 'tjsp_esaj', 'caderno12_20250721.txt')
    with open(caminho, encoding='utf-8', errors='replace') as fh:
        texto = fh.read()
    # Ocorrências, não distintos: é a métrica do recon (4.722 estrita vs
    # 5.136 tolerante no mesmo caderno = 8,1% de processos perdidos).
    estrita = len(re.findall(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}', texto))
    tolerante = sum(1 for _ in CNJ_TOLERANTE.finditer(texto))
    assert tolerante > estrita * 1.05, f'estrita={estrita} tolerante={tolerante}'


# ── external_id: namespace por fonte é o que faz a dedupe funcionar ─────────
def test_external_id_prefixado_pela_fonte():
    assert external_id_de('tjsp-dje', 4246, 19, 480).startswith('tjsp-dje:')


def test_slug_djen_e_reservado():
    with pytest.raises(ValueError):
        validar_slug('djen')  # namespace legado: 65M linhas sem prefixo


def test_external_id_estoura_limite_em_vez_de_truncar():
    """Truncar cola dois atos no mesmo id e o segundo some no ignore_conflicts.
    Melhor explodir na cara do implementador."""
    with pytest.raises(ValueError):
        external_id_de('dejt', 'x' * MAX_EXTERNAL_ID)


def test_id_de_bloco_nao_depende_do_ordinal():
    """O id do bloco é conteúdo + coordenada. Mudar o segmentador (e vai mudar:
    o layout do caderno variou em 16 anos) NÃO pode reescrever os ids e fazer a
    re-ingestão duplicar a edição inteira."""
    texto = 'Nº 2217577-02.2025.8.26.0000 - Agravo de Instrumento - Agravante: Banco X'
    a = id_bloco_impresso('tjsp-dje', 4246, 19, 480, texto=texto)
    b = id_bloco_impresso('tjsp-dje', 4246, 19, 480, texto='  ' + texto.upper() + '\n')
    assert a == b


def test_id_de_bloco_distingue_pagina():
    texto = 'Intimação. Ciência às partes.'
    assert (id_bloco_impresso('dejt', 4011, 'TRT3', 81, texto=texto)
            != id_bloco_impresso('dejt', 4011, 'TRT3', 82, texto=texto))


# ── fingerprint: parear o mesmo ato vindo por portas diferentes ─────────────
def test_fingerprint_igual_para_o_mesmo_ato_com_espacamento_diferente():
    cnj = '1099663-22.2025.8.26.0100'
    dia = date(2025, 7, 21)
    a = fingerprint_ato(cnj, dia, 'Vistas às partes.  Prazo de 15 dias.')
    b = fingerprint_ato(cnj, datetime(2025, 7, 21, 3, 0, tzinfo=UTC),
                        'Vistas  às partes.\nPrazo de 15 dias.')
    assert a == b


def test_fingerprint_diferente_para_processos_diferentes():
    assert (fingerprint_ato('1099663-22.2025.8.26.0100', date(2025, 7, 21), 'Vista.')
            != fingerprint_ato('2217577-02.2025.8.26.0000', date(2025, 7, 21), 'Vista.'))


# ── o "200 que não é dado": as três armadilhas medidas no e-SAJ ─────────────
def test_exigir_pdf_rejeita_html_de_caderno_inexistente():
    """`downloadCaderno.do` de data sem edição devolve HTTP 200 + 851 bytes de
    'Erro ao acessar o caderno selecionado'. Num backfill de 4.000 edições isso
    vira lacuna invisível."""
    caminho = os.path.join(FIXTURES, 'tjsp_esaj', 'caderno_inexistente_erro.html')
    if not os.path.exists(caminho):
        pytest.skip('fixture não presente')
    with open(caminho, 'rb') as fh:
        corpo = fh.read()
    with pytest.raises(RespostaInvalida):
        exigir_pdf(corpo, contexto='caderno inexistente')


def test_exigir_pdf_rejeita_corpo_vazio():
    """Página acima da última devolve 200 com 0 bytes."""
    with pytest.raises(RespostaInvalida):
        exigir_pdf(b'', contexto='pagina 2002')


def test_exigir_pdf_aceita_pdf_de_verdade():
    caminho = os.path.join(FIXTURES, 'tjsp_esaj', 'pagina_4246_c19_p480.pdf')
    if not os.path.exists(caminho):
        pytest.skip('fixture não presente')
    with open(caminho, 'rb') as fh:
        corpo = fh.read()
    assert exigir_pdf(corpo, contexto='pagina 480') is corpo


def test_exigir_ancora_rejeita_casca_do_visualizador():
    """`consultaSimples.do` devolve 200 com 1.207 bytes de <frameset> — a casca,
    sem dado nenhum."""
    caminho = os.path.join(FIXTURES, 'tjsp_esaj', 'consultaSimples_p480.html')
    if not os.path.exists(caminho):
        pytest.skip('fixture não presente')
    with open(caminho, encoding='latin-1') as fh:
        html = fh.read()
    with pytest.raises(RespostaInvalida):
        exigir_ancora(html, 'var diarios', contexto='consultaSimples')


# ── persistência: idempotência e coexistência entre portas ──────────────────
@pytest.mark.django_db
def test_persistir_e_idempotente_e_portas_coexistem():
    """Duas garantias de uma vez:

    1. re-coletar a mesma edição não duplica (ignore_conflicts sobre
       uniq(tribunal, external_id));
    2. o MESMO ato vindo pelo DJEN e pelo diário próprio coexiste, porque o
       external_id é namespaceado — e é a leitura, pelo `hash`, que pareia os
       dois. Sobrescrever um com o outro destruiria o verbatim de um veículo.
    """
    from diarios.base import espelhadas_no_lote, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Tribunal

    # get_or_create: as migrations já semeiam tribunais no banco de teste.
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    cnj = '1099663-22.2025.8.26.0100'
    quando = datetime(2025, 7, 21, 0, 0, tzinfo=UTC)
    texto = 'REQTE: Gisela Aparecida Paulino. Vista às partes.'

    do_diario = ParsedItem(
        cnj=cnj, external_id=id_bloco_impresso('tjsp-dje', 4246, 12, 3, texto=texto),
        data_disponibilizacao=quando, texto=texto,
        hash=fingerprint_ato(cnj, quando, texto),
        meio='D', meio_completo='DJE/TJSP (e-SAJ)',
    )
    novas, dup = persistir_movimentacoes([do_diario], t, None)
    assert (novas, dup) == (1, 0)

    # re-coleta da mesma edição: idempotente
    novas, dup = persistir_movimentacoes([do_diario], t, None)
    assert (novas, dup) == (0, 1)
    assert Movimentacao.objects.filter(tribunal=t).count() == 1

    # O mesmo ato pelo DJEN (external_id nu, sem prefixo) entra ao lado.
    #
    # ATENÇÃO ao `hash` daqui: 30 caracteres opacos, que é o que
    # `djen/parser.py:243` põe de verdade (`str(item.get('hash'))`), NÃO um
    # `fingerprint_ato`. Este teste construía o item com o fingerprint e por
    # isso afirmava, verde, que a métrica de sobreposição funcionava — quando em
    # produção ela não podia funcionar: sha1 tem 40 chars e nunca é igual a 30.
    # Se alguém trocar isto por `fingerprint_ato` de novo, o teste volta a
    # provar uma ficção.
    do_djen = ParsedItem(
        cnj=cnj, external_id='695042804', data_disponibilizacao=quando, texto=texto,
        hash='7e9MjpmEYnBUkdVulTlPJE8Yqr',
        meio='D', meio_completo='Diário de Justiça Eletrônico Nacional',
    )
    # A sobreposição é revelada pelo par (processo, data) — o único que os dois
    # veículos compartilham de fato. O ato do diário próprio já está gravado.
    assert espelhadas_no_lote([do_djen], t) == 1, 'a sobreposição entre portas tem que aparecer'
    novas, dup = persistir_movimentacoes([do_djen], t, None)
    assert (novas, dup) == (1, 0)
    assert Movimentacao.objects.filter(tribunal=t).count() == 2
    # ...e a leitura NÃO consegue deduplicar por `hash`: os dois veículos
    # guardam coisas diferentes ali (fingerprint sha1 vs hash opaco da API).
    # Deixado explícito porque a versão anterior deste teste afirmava o oposto.
    assert Movimentacao.objects.filter(tribunal=t).values('hash').distinct().count() == 2


@pytest.mark.django_db
def test_run_de_outra_fonte_nao_conta_como_cobertura_do_djen():
    """A regressão que motiva `IngestionRun.fonte`: sem o campo, um run do DJE
    próprio na mesma janela faria o backfill do DJEN pular o dia como coberto —
    perda silenciosa de uma edição inteira."""
    from djen.jobs import _dia_coberto
    from tribunals.models import IngestionRun, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP', 'overlap_dias': 3})
    dia = date(2015, 7, 15)
    IngestionRun.objects.filter(tribunal=t, janela_inicio=dia).delete()
    IngestionRun.objects.create(
        tribunal=t, fonte='tjsp-dje', status=IngestionRun.STATUS_SUCCESS,
        janela_inicio=dia, janela_fim=dia, movimentacoes_novas=27484, paginas_lidas=1,
    )
    assert _dia_coberto(t, dia) is False

    IngestionRun.objects.create(
        tribunal=t, status=IngestionRun.STATUS_SUCCESS,   # default fonte='djen'
        janela_inicio=dia, janela_fim=dia, movimentacoes_novas=10, paginas_lidas=1,
    )
    assert _dia_coberto(t, dia) is True


@pytest.mark.django_db
def test_watermark_fecha_unidade_inexistente_para_sempre():
    """Feriado forense não é lacuna. O DEJT devolve zero linhas em 14/08/2023 e
    12/03/2022 (Carnaval/feriado) — tratar isso como falha faz o backfill
    retentar o mesmo dia para sempre (bug já pago no `_dia_coberto` do DJEN)."""
    from diarios.models import EdicaoDiario

    e = EdicaoDiario.objects.create(fonte='dejt', chave='4011-TRT3', data=date(2023, 8, 14))
    e.marcar(EdicaoDiario.INEXISTENTE, erro='feriado forense')
    e.refresh_from_db()
    assert e.status == EdicaoDiario.INEXISTENTE
    pendentes = EdicaoDiario.objects.filter(
        fonte='dejt', status__in=[EdicaoDiario.PENDENTE, EdicaoDiario.FALHA])
    assert pendentes.count() == 0


# ── o contrato inteiro, ponta a ponta, sem rede ────────────────────────────
# Este teste é também o EXEMPLO DE REFERÊNCIA para quem for implementar uma
# fonte: é o mínimo que um coletor precisa ter para o runner funcionar.
def _fake_coletor(itens=1, gabarito=None, data_unidade=date(2015, 7, 15)):
    from diarios.base import ColetorDiario, UnidadeColeta
    from djen.parser import ParsedItem

    class FakeDje(ColetorDiario):
        slug = 'fake-dje'
        nome = 'Diário de mentira (teste)'
        janela_inicio = date(2007, 10, 1)
        janela_fim = date(2025, 3, 13)   # a fronteira medida do TJSP no DJEN

        def catalogar(self, data_inicio, data_fim):
            yield UnidadeColeta(chave='4246-12', data=data_unidade, tribunal_sigla='TJSP',
                                rotulo='Caderno 12 · 1ª Inst. Capital',
                                meta={'cdCaderno': 12, 'nuDiario': 4246})

        def coletar(self, unidade):
            # CNJs com dígito verificador VÁLIDO (mod 97). Variar só o último
            # dígito do foro, como esta fixture fazia, produzia número inválido —
            # e desde 2026-08-16 `achar_cnjs` confere o DV e o descarta, que é
            # exatamente o comportamento desejado. Ver `dv_cnj_valido`.
            sequenciais = ['1099663-22', '1099664-07', '1099665-89', '1099666-74']
            for i in range(itens):
                texto = (f'VARA :35ª VARA CÍVEL PROCESSO :{sequenciais[i % 4]}.2025.8.26.0100'
                         f' REQTE : Fulano {i}')
                cnj = achar_cnjs(texto)[0]
                quando = datetime(2015, 7, 15, tzinfo=UTC)
                yield ParsedItem(
                    cnj=cnj,
                    external_id=id_bloco_impresso(self.slug, unidade.meta['nuDiario'],
                                                  unidade.meta['cdCaderno'], 3, texto=texto),
                    data_disponibilizacao=quando, texto=texto,
                    hash=fingerprint_ato(cnj, quando, texto),
                    meio='D', meio_completo='DJE/TJSP (e-SAJ)',
                    nome_orgao='35ª VARA CÍVEL',
                )

        def esperado(self, unidade):
            return gabarito

    return FakeDje()


@pytest.mark.django_db
def test_contrato_ponta_a_ponta_catalogar_coletar_persistir():
    from diarios.base import catalogar_fonte, coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import IngestionRun, Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    Movimentacao.objects.filter(tribunal=t, external_id__startswith='fake-dje:').delete()

    coletor = _fake_coletor(itens=2)
    assert catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))['novas'] == 1

    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')
    assert edicao.status == EdicaoDiario.PENDENTE

    r = coletar_unidade(coletor, edicao)
    assert r['novas'] == 2
    edicao.refresh_from_db()
    assert edicao.status == EdicaoDiario.OK
    run = IngestionRun.objects.get(pk=r['run_id'])
    assert (run.fonte, run.status) == ('fake-dje', IngestionRun.STATUS_SUCCESS)
    assert Movimentacao.objects.filter(tribunal=t, external_id__startswith='fake-dje:').count() == 2

    # re-coleta da mesma unidade: idempotente (é o que acontece em todo retry)
    r2 = coletar_unidade(coletor, edicao)
    assert (r2['novas'], r2['duplicadas']) == (0, 2)
    edicao.refresh_from_db()
    # `itens_gravados` é quantas linhas a unidade TEM no banco, não quantas
    # nasceram nesta execução: antes de 16/08/2026 a re-coleta zerava o número e
    # a dashboard mostrava edição de 31 mil linhas com `itens_gravados=0`.
    assert edicao.itens_gravados == 2


@pytest.mark.django_db
def test_ato_impresso_duas_vezes_na_mesma_unidade_nao_conta_como_novo():
    """O diário às vezes imprime o MESMO ato duas vezes (conferido na pg. 188 do
    caderno 13 do TJSP de 15/07/2015: 12 colisões em 31.408 blocos). Os dois
    blocos geram o mesmo `external_id` — o banco já ignorava o conflito, mas a
    CONTAGEM somava a lista e não o conjunto: a segunda coleta da mesma edição
    reportava `novas=11` para sempre, violando o critério de aceite da casa
    ('a segunda passada devolve novas=0')."""
    from diarios.base import catalogar_fonte, coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    Movimentacao.objects.filter(tribunal=t, external_id__startswith='fake-dje:').delete()

    coletor = _fake_coletor(itens=4)
    original = coletor.coletar

    def coletar_com_repeticao(unidade):
        # 4 blocos distintos + 2 reimpressos, exatamente como o caderno faz
        distintos = list(original(unidade))
        return iter(distintos + distintos[:2])

    coletor.coletar = coletar_com_repeticao
    catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))
    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')
    r = coletar_unidade(coletor, edicao)
    assert r['novas'] == 4, 'bloco repetido no MESMO lote não é linha nova'
    assert Movimentacao.objects.filter(
        tribunal=t, external_id__startswith='fake-dje:').count() == 4
    assert coletar_unidade(coletor, edicao)['novas'] == 0


@pytest.mark.django_db
def test_gabarito_da_fonte_reprova_segmentacao_incompleta():
    """Quando a fonte declara quantos itens existem (o DEJT declara), achar
    menos que o piso é FALHA — melhor que gravar meia edição em silêncio."""
    from diarios.base import ColetorError, catalogar_fonte, coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    coletor = _fake_coletor(itens=1, gabarito=100)
    catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))
    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')
    with pytest.raises(ColetorError):
        coletar_unidade(coletor, edicao)
    edicao.refresh_from_db()
    assert edicao.status == EdicaoDiario.FALHA
    assert edicao.tentativas == 1


@pytest.mark.django_db
def test_edicao_sem_nada_aproveitavel_nao_se_disfarca_de_edicao_vazia():
    """REGRESSÃO 16/08/2026: 'NÃO HAVIA' ≠ 'HAVIA E NÃO SERVE'.

    O caderno 12 do DJE/TJSP de 15/06/2009 tem 16.952 blocos de publicação REAL,
    todos com numeração pré-CNJ. Descartá-los é correto; fechar a unidade como
    `VAZIA` (cujo contrato é 'baixou e não havia publicação'), com
    `itens_gravados=0`, `ultimo_erro=''`, status terminal e o log dizendo
    'cobertura de CNJ 0/0 = 100.0%', não é — 16.952 publicações viravam lacuna
    invisível com o run VERDE.

    O status `sem_aproveit` é terminal como o `inexistente` (retentar 15 MB não
    muda o resultado), mas guarda o MOTIVO e é contável na dashboard.
    """
    from diarios.base import UnidadeSemDadoAproveitavel, catalogar_fonte, coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    coletor = _fake_coletor(itens=1)

    def sem_aproveitamento(unidade):
        raise UnidadeSemDadoAproveitavel('16952 blocos e ZERO com CNJ — era pré-CNJ')
        yield  # pragma: no cover — mantém a assinatura de gerador

    coletor.coletar = sem_aproveitamento
    catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))
    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')
    r = coletar_unidade(coletor, edicao)

    assert r['status'] == EdicaoDiario.SEM_APROVEITAMENTO
    edicao.refresh_from_db()
    assert edicao.status != EdicaoDiario.VAZIA, 'não pode se passar por dia sem publicação'
    assert '16952 blocos' in edicao.ultimo_erro, 'o motivo tem que ficar escrito'


@pytest.mark.django_db
def test_fora_da_janela_nao_coleta_sem_sobrepor():
    """A dedupe principal: no período em que o DJEN já cobre o TJSP
    (a partir de 14/03/2025), o diário próprio não ingere nada."""
    from diarios.base import catalogar_fonte, coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    coletor = _fake_coletor(itens=1, data_unidade=date(2025, 7, 21))
    # catálogo já descarta o que está fora da janela
    assert catalogar_fonte(coletor, date(2025, 7, 1), date(2025, 7, 31))['novas'] == 0
    # e mesmo forçando a unidade no banco, a coleta recusa
    edicao = EdicaoDiario.objects.create(fonte='fake-dje', chave='4246-12',
                                        data=date(2025, 7, 21), tribunal_id='TJSP')
    assert coletar_unidade(coletor, edicao)['status'] == EdicaoDiario.FORA_DA_JANELA


@pytest.mark.django_db
def test_ausencia_precisa_ser_confirmada_antes_de_virar_terminal():
    """REGRESSÃO 03/09/2026 — `inexistente` é TERMINAL, logo tem que ser PROVADO.

    As 5 unidades do `tjsp-dje` que estavam `inexistente` em produção foram
    reconferidas contra a fonte viva com GET real: **as 5 devolveram `%PDF`**.
    Os cadernos existem. O e-SAJ tinha servido, uma vez, a página de 851 bytes
    de "Erro ao acessar o caderno selecionado" (HTTP 200, `text/html`) para
    caderno que ele tem — e essa única observação fechava o watermark PARA
    SEMPRE, com `IngestionRun.status='success'` e o log limpo.

    A régua: uma observação deixa a unidade PENDENTE (e conta tentativa);
    `CONFIRMACOES_DE_AUSENCIA` observações a fecham. Nunca a primeira.
    """
    from diarios.base import (CONFIRMACOES_DE_AUSENCIA, UnidadeInexistente,
                              catalogar_fonte, coletar_unidade)
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    coletor = _fake_coletor(itens=1)

    def nao_existe(unidade):
        raise UnidadeInexistente('e-SAJ não tem o caderno 15 em 24/01/2025')
        yield  # pragma: no cover — mantém a assinatura de gerador

    coletor.coletar = nao_existe
    catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))
    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')

    for vista in range(1, CONFIRMACOES_DE_AUSENCIA):
        r = coletar_unidade(coletor, edicao)
        edicao.refresh_from_db()
        assert r['status'] == EdicaoDiario.PENDENTE, (
            f'{vista}ª observação NÃO pode fechar um status terminal')
        assert r['ausencia_nao_confirmada'] == vista
        assert edicao.status == EdicaoDiario.PENDENTE
        assert edicao.tentativas == 0, (
            'ausência não é falha: não pode gastar o orçamento de MAX_TENTATIVAS')
        assert f'({vista}/' in edicao.ultimo_erro, 'o número da observação tem que ficar escrito'

    r = coletar_unidade(coletor, edicao)
    edicao.refresh_from_db()
    assert r['status'] == EdicaoDiario.INEXISTENTE, 'confirmada, aí sim fecha'
    assert edicao.status == EdicaoDiario.INEXISTENTE
    assert 'confirmada em' in edicao.ultimo_erro


@pytest.mark.django_db
def test_confirmacao_de_ausencia_cabe_dentro_do_teto_de_tentativas():
    """Se `CONFIRMACOES_DE_AUSENCIA` alcançasse `MAX_TENTATIVAS`, a unidade
    pararia de ser selecionada pelo tick (`tentativas__lt=MAX_TENTATIVAS`) e
    ficaria `pendente` para sempre — dívida INVISÍVEL no lugar de um status
    terminal honesto. Trocar uma perda medida por uma não medida é o oposto
    do que este conserto faz."""
    from diarios.base import CONFIRMACOES_DE_AUSENCIA
    from diarios.jobs import MAX_TENTATIVAS

    assert CONFIRMACOES_DE_AUSENCIA < MAX_TENTATIVAS


@pytest.mark.django_db
def test_ausencia_conta_ausencia_seguida_e_nao_o_contador_de_falhas():
    """O DADO que corrigiu o conserto, e por isso ele tem teste próprio.

    As 5 unidades falsamente `inexistente` estavam com `tentativas` **4 e 5** —
    gastas em FALHAS de outra natureza (a `NotNullViolation` do §14 do
    `.ia/DIARIOS.md`), não em ausências. Se o contador de confirmação fosse
    `tentativas`, uma ÚNICA observação de "200 que não é dado" fecharia o
    watermark de qualquer unidade que já tivesse tropeçado antes — que é
    exatamente o caso medido em produção.

    A régua: falha anterior NÃO adianta o relógio da ausência.
    """
    from diarios.base import (CONFIRMACOES_DE_AUSENCIA, UnidadeInexistente,
                              catalogar_fonte, coletar_unidade)
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    coletor = _fake_coletor(itens=1)
    catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))
    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')

    # o estado real de produção: 4 tentativas queimadas em falha de INSERT
    edicao.marcar(EdicaoDiario.FALHA, erro='null value in column "classe_cnj_codigo"')
    EdicaoDiario.objects.filter(pk=edicao.pk).update(tentativas=4)
    edicao.refresh_from_db()

    def nao_existe(unidade):
        raise UnidadeInexistente('e-SAJ não tem o caderno 12 em 05/02/2025')
        yield  # pragma: no cover — mantém a assinatura de gerador

    coletor.coletar = nao_existe
    r = coletar_unidade(coletor, edicao)
    edicao.refresh_from_db()
    assert r['status'] == EdicaoDiario.PENDENTE, (
        'tentativas=4 de FALHA não pode valer como ausência confirmada')
    assert r['ausencia_nao_confirmada'] == 1, 'o relógio da ausência começa em 1'
    assert edicao.tentativas == 4, 'e não pode consumir mais tentativa'


@pytest.mark.django_db
def test_qualquer_outro_desfecho_zera_o_relogio_da_ausencia():
    """"Confirmada" quer dizer SEGUIDA. Uma coleta que deu certo (ou uma falha)
    no meio do caminho reescreve `ultimo_erro` e o contador recomeça — abster
    para o lado de perguntar de novo, nunca para o lado de fechar terminal."""
    from diarios.base import UnidadeInexistente, catalogar_fonte, coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.filter(fonte='fake-dje').delete()
    coletor = _fake_coletor(itens=1)
    catalogar_fonte(coletor, date(2015, 7, 1), date(2015, 7, 31))
    edicao = EdicaoDiario.objects.get(fonte='fake-dje', chave='4246-12')

    def nao_existe(unidade):
        raise UnidadeInexistente('e-SAJ não tem o caderno')
        yield  # pragma: no cover

    coletor.coletar = nao_existe
    assert coletar_unidade(coletor, edicao)['ausencia_nao_confirmada'] == 1
    edicao.refresh_from_db()
    edicao.marcar(EdicaoDiario.FALHA, erro='timeout no download')   # desfecho de outra natureza
    edicao.refresh_from_db()
    assert coletar_unidade(coletor, edicao)['ausencia_nao_confirmada'] == 1, (
        'o relógio tem que recomeçar, não continuar de onde parou'
    )
