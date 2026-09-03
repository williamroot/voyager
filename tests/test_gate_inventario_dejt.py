"""SEGUNDO EIXO do gate armado para o `dejt` (`diarios/inventario.py`).

Até 03/09/2026 o DEJT era a fonte MAIS desprotegida das cinco, e não por
descuido de uma régua só — por ausência das três:

  · eixo de PROPORÇÃO (CNJ impresso × CNJ dentro de bloco): **não existia**.
    O coletor segmentava, logava `N páginas → M matérias` e devolvia. Nada
    comparava os dois lados.
  · eixo de INVENTÁRIO (§18): a fonte não declarava marcador, então
    `Inventario.mede` era falso e o eixo ABSTINHA — e abstenção não é aprovação.
  · gabarito da fonte (`esperado()`, a pesquisa avançada que declara "1 até 20
    de 16.717"): devolve `None` em qualquer erro **por desenho**, e com o DEJT
    fora do ar desde 18/08/2026 devolve `None` SEMPRE.

Três réguas, três silêncios, e o mesmo sintoma de fora: run verde.

Os números abaixo não são inventados para o teste: saíram de 6 cadernos REAIS
(TRT16 e TRT22, edições de 2018, 2020, 2022 e 2024), medidos em 03/09/2026 pelos
três caminhos que têm que concordar — regex no texto colado, contagem linha a
linha, e blocos produzidos. O achado que fecha o argumento é o TRT22 de
15/03/2018: 92,6% de cobertura de CNJ **e** 45 CNJs órfãos numa forma de linha
que o segmentador não conhece (`Processo   : 0000817-80.2012.5.22.0107`, sem o
`Nº`) — enquanto o gabarito da própria fonte, na mesma era, dava **140%** e
absolvia (a tabela de `segmentavel_desde` no coletor).
"""

from collections import Counter
from datetime import date

import pytest

from diarios.base import ColetorError, UnidadeColeta, UnidadeSemDadoAproveitavel, achar_cnjs
from diarios.fontes.dejt import segmentador
from diarios.fontes.dejt.coletor import (
    MARCADORES_DEJT,
    NOME_MARCADOR_DISTRIBUICAO,
    ColetorDEJT,
    _paginas_de_distribuicao,
    _ver_linha,
)
from diarios.inventario import Inventario

# ─────────────────────────────────────────────────────────────────────────────
# Trechos VERBATIM (mesma procedência dos de `test_diario_dejt.py`)
# ─────────────────────────────────────────────────────────────────────────────
MATERIA = (
    'Processo Nº ATOrd-{cnj}\n'
    'RECLAMANTE FULANO DE TAL\n'
    'ADVOGADO SICRANO(OAB: 123/PI)\n'
    ' \n'
    'Intimado(s)/Citado(s): \n'
    '  - FULANO DE TAL\n'
    ' \n'
    'INTIMAÇÃO\n'
    'Fica V. Sa. intimado para tomar ciência do Despacho.\n'
)
LINHA_DISTRIBUICAO = (
    'ATOrd {cnj}\n'
    '3ª Vara do Trabalho de Betim\n'
    'AUTOR - CARLOS FERREIRA DA SILVA\n'
    'ADVOGADO - FELIPE DOURADO LAGES (OAB/MG 110695)\n'
)
#: numeração trabalhista PRÉ-CNJ, verbatim do TRT16 de 15/03/2018
MATERIA_PRE_CNJ = (
    'Processo Nº ROS-02029/2006-002-16-00.5\n'
    ' \n'
    'Complemento Ordem: 4\n'
    'Relator Desembargador(a) AMÉRICO BEDÊ FREIRE\n'
    'RECORRENTES RECOFARMA INDÚSTRIA DO AMAZONAS LTDA\n'
)
#: o TERCEIRO formato, achado nos órfãos do TRT22 de 15/03/2018 (45 de 50).
#: `Processo` seguido de espaços e dois-pontos — não casa com `Processo Nº`.
LINHA_TERCEIRO_FORMATO = 'Processo   : {cnj}'


def _cnj(n: int) -> str:
    """CNJ com DV VÁLIDO (Res. CNJ 65/2008, módulo 97).

    Não é preciosismo de teste: `achar_cnjs` confere o dígito verificador, e um
    número inventado sem DV é simplesmente ignorado pelo coletor. Com CNJ falso
    o teste mediria o silêncio do validador, não o gate.
    """
    seq, ano, j, tr, orgao = f'{n:07d}', '2012', '5', '22', '0107'
    resto = int(f'{seq}{ano}{j}{tr}{orgao}00') % 97
    return f'{seq}-{98 - resto:02d}.{ano}.{j}.{tr}.{orgao}'


def _rodar_gate(paginas, secoes):
    """Roda o caminho de gate do coletor sobre páginas já extraídas.

    É o MESMO código de `ColetorDEJT.coletar`, sem rede e sem banco:
    `object.__new__` pula o `__init__` (que abriria sessão HTTP).
    """
    col = object.__new__(ColetorDEJT)
    unidade = UnidadeColeta(chave='J-TRT22-2018-03-15-2435', data=date(2018, 3, 15),
                            tribunal_sigla='TRT22', rotulo='teste', meta={})
    inv = Inventario(marcadores=tuple(col.MARCADORES_DE_REGISTRO))
    cnjs_texto: set[str] = set()
    cnjs_bloco: set[str] = set()
    distrib = _paginas_de_distribuicao(len(paginas), secoes)
    for numero, pagina in enumerate(paginas):
        for linha in pagina.split('\n'):
            limpa = linha.strip()
            achados = achar_cnjs(limpa)
            cnjs_texto.update(achados)
            _ver_linha(inv, limpa, achados, em_distribuicao=numero in distrib)
    descartes: Counter = Counter()
    vistos = 0
    for bloco in segmentador.blocos(paginas, secoes, descartes):
        inv.ver_bloco(bloco.formato)
        cnjs_bloco.update(achar_cnjs(bloco.texto))
        vistos += 1
    col._aferir_cobertura(unidade, cnjs_texto, cnjs_bloco,
                          inventario=inv, descartes=descartes, vistos=vistos)
    return inv, descartes, vistos


# ─────────────────────────────────────────────────────────────────────────────
# 1. o balde tem que ser EXCLUSIVO — é o que faz a perna A morder
# ─────────────────────────────────────────────────────────────────────────────
def test_cada_ancora_tem_seu_proprio_balde():
    """DIARIOS.md §18.5, decisão 1: com as duas âncoras no MESMO formato, as
    ~900 matérias `Processo Nº` cobririam sozinhas a conta da Distribuição
    inteira e a perna A ficaria muda. Aqui elas são separadas na origem."""
    materias = [MATERIA.format(cnj=_cnj(i)) for i in range(1, 4)]
    distrib = [LINHA_DISTRIBUICAO.format(cnj=_cnj(i)) for i in range(10, 13)]
    # o 1º item da seção é o índice da PÁGINA (0-based), não offset de char
    blocos = list(segmentador.blocos(
        ['\n'.join(materias), '\n'.join(distrib)],
        [(0, 'Vara', 'Notificação'), (1, 'Presidência', 'Distribuição')]))
    formatos = Counter(b.formato for b in blocos)
    assert formatos == {segmentador.FORMATO_PROCESSO: 3,
                        segmentador.FORMATO_DISTRIBUICAO: 3}
    # …e os marcadores declarados apontam para esses mesmos dois baldes, um cada
    assert {m.formato for m in MARCADORES_DEJT} == {
        segmentador.FORMATO_PROCESSO, segmentador.FORMATO_DISTRIBUICAO}
    assert len({m.formato for m in MARCADORES_DEJT}) == len(MARCADORES_DEJT)


def test_perna_A_acusa_a_distribuicao_perdida_sem_olhar_para_o_CNJ():
    """O caso que motiva o eixo: a seção de Distribuição inteira sai do
    resultado (aqui, porque o outline não a declarou como Distribuição) e a
    perna A cobra com os DOIS números — mesmo que a cobertura de CNJ passasse.
    """
    inv = Inventario(marcadores=MARCADORES_DEJT)
    for i in range(40):
        inv.ver_linha(f'ATOrd {_cnj(i)}', [])          # a fonte imprimiu 40
    for _ in range(900):
        inv.ver_bloco(segmentador.FORMATO_PROCESSO)     # e só saíram matérias
    achados = inv.conferir()
    assert len(achados) == 1
    assert achados[0].tipo == 'marcador'
    assert (achados[0].impresso, achados[0].segmentado) == (40, 0)
    assert 'Distribuição' in achados[0].detalhe


def test_marcador_de_distribuicao_nao_casa_com_a_materia_comum():
    """Falso positivo em gate é pior que gate ausente: ele ensina a ignorar.
    Medido em 6 cadernos: a âncora de Distribuição nunca apareceu fora da seção
    de Distribuição."""
    inv = Inventario(marcadores=MARCADORES_DEJT)
    inv.ver_linha('Processo Nº ATOrd-0000817-80.2012.5.22.0107', [])
    inv.ver_linha('PROCESSO: 0088986-87.2023.5.22.0000 (Precatório)', [])
    inv.ver_linha('ATOrd 0000817-80.2012.5.22.0107', [])
    assert dict(inv.impresso) == {'matéria (Processo Nº)': 1, 'linha de Distribuição': 1}


# ─────────────────────────────────────────────────────────────────────────────
# 2. a diferença entre impresso e segmentado tem que ter NOME
# ─────────────────────────────────────────────────────────────────────────────
def test_descarte_de_numeracao_pre_cnj_e_nomeado_e_nao_reprova():
    """68 de 68 descartes medidos em 6 cadernos eram numeração pré-CNJ — dívida
    CONHECIDA (falta o de-para com `Process.numero_cnj`), não parser quebrado.
    Reprovar a edição por isso pararia a fonte inteira; calar sobre isso é a
    perda silenciosa. O meio-termo honesto é: conta, nomeia e segue."""
    paginas = [MATERIA.format(cnj=_cnj(i)) for i in range(1, 61)] + [MATERIA_PRE_CNJ]
    inv, descartes, vistos = _rodar_gate(paginas, [(0, 'Vara', 'Notificação')])
    assert descartes['pre_cnj'] == 1
    assert 'desconhecido' not in descartes
    assert vistos == 60
    # a perna A VÊ a diferença (61 impressos × 60 blocos) e ela fica no log
    assert inv.impresso['matéria (Processo Nº)'] == 61
    assert inv.segmentado[segmentador.FORMATO_PROCESSO] == 60


def test_descarte_sem_numeracao_reconhecivel_REPROVA():
    """O outro lado da moeda: descarte que ninguém explicou é cabeçalho mudado
    ou formato novo, e aí a unidade não pode fechar como coletada."""
    sem_numero = ('Processo Nº\n'
                  'AUTOR - FULANO DE TAL\n'
                  ' \n'
                  'edital sem número de processo nenhum\n')
    paginas = [MATERIA.format(cnj=_cnj(i)) for i in range(1, 61)] + [sem_numero]
    with pytest.raises(ColetorError) as exc:
        _rodar_gate(paginas, [(0, 'Vara', 'Notificação')])
    assert 'inventário divergente' in str(exc.value)
    assert 'SEM numeração reconhecível' in str(exc.value)


def test_edicao_inteiramente_pre_cnj_fecha_sem_aproveit_e_nao_vazia():
    """A lição do caderno 12 do TJSP de 15/06/2009 (DIARIOS.md §4), agora
    travada também no DEJT: havia publicação, nada é aproveitável, e isso NÃO é
    edição vazia — é acervo que existe e que ainda não sabemos ler."""
    paginas = [MATERIA_PRE_CNJ] * 60
    with pytest.raises(UnidadeSemDadoAproveitavel) as exc:
        _rodar_gate(paginas, [(0, 'Vara', 'Notificação')])
    assert 'ZERO virou matéria aproveitável' in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# 3. perna B — o formato que ninguém declarou, achado no caderno real
# ─────────────────────────────────────────────────────────────────────────────
def test_perna_B_nomeia_o_terceiro_formato_do_dejt():
    """`Processo   : <CNJ>` (sem o `Nº`) é o formato que o segmentador do DEJT
    não lê. Medido no TRT22 de 15/03/2018: 45 dos 50 CNJs órfãos estão nessa
    forma. Nenhum marcador o declara — quem tem que denunciá-lo é a perna B,
    pela REPETIÇÃO da forma da linha."""
    inv = Inventario(marcadores=MARCADORES_DEJT)
    orfaos = []
    for i in range(45):
        cnj = _cnj(i)
        orfaos.append(cnj)
        inv.ver_linha(LINHA_TERCEIRO_FORMATO.format(cnj=cnj), [cnj])
    achados = inv.conferir(orfaos)
    assert len(achados) == 1
    assert achados[0].tipo == 'assinatura'
    assert achados[0].impresso == 45
    assert achados[0].detalhe.startswith('Processo : #')
    assert 'formato provavelmente desconhecido' in str(achados[0])


def test_perna_B_reprova_mesmo_com_a_cobertura_acima_do_piso():
    """É o ponto inteiro do §18: proporção alta e formato perdido convivem.
    Aqui 45 órfãos contra 900 matérias = 95,3% de cobertura — PASSA no eixo 1 —
    e o eixo 2 reprova, dizendo na mensagem que a cobertura estava acima do
    piso, para ninguém procurar o erro no lugar errado."""
    paginas = [MATERIA.format(cnj=_cnj(i)) for i in range(1, 901)]
    paginas += [LINHA_TERCEIRO_FORMATO.format(cnj=_cnj(2000 + i)) for i in range(45)]
    # a seção nova corta o último bloco: sem essa fronteira o bloco anterior
    # ENGOLE as 45 linhas e elas nem chegam a ser órfãs — que é justamente o
    # resíduo que a §18.6 declara descoberto pelos dois eixos.
    with pytest.raises(ColetorError) as exc:
        _rodar_gate(paginas, [(0, 'Vara', 'Notificação'), (900, 'Presidência', 'Ata')])
    msg = str(exc.value)
    assert 'ACIMA do piso' in msg
    assert 'MESMA forma de linha' in msg


# ─────────────────────────────────────────────────────────────────────────────
# 4. o caso limpo — nenhum falso positivo
# ─────────────────────────────────────────────────────────────────────────────
def test_caderno_limpo_passa_pelos_dois_eixos():
    """TRT16 de 10/07/2024, medido: 1.102 impressos × 1.102 blocos, 154 × 154,
    cobertura 100,0%, zero descarte. O gate tem que ficar CALADO nesse caso —
    senão ele vira ruído e some do radar de quem lê o log."""
    materias = [MATERIA.format(cnj=_cnj(i)) for i in range(1, 61)]
    distrib = [LINHA_DISTRIBUICAO.format(cnj=_cnj(100 + i)) for i in range(20)]
    inv, descartes, vistos = _rodar_gate(
        ['\n'.join(materias), '\n'.join(distrib)],
        [(0, 'Vara', 'Notificação'), (1, 'Presidência', 'Distribuição')])
    assert descartes == {}
    assert vistos == 80
    assert inv.conferir(orfaos=()) == []
    assert inv.impresso['matéria (Processo Nº)'] == inv.segmentado[segmentador.FORMATO_PROCESSO]
    assert inv.impresso['linha de Distribuição'] == inv.segmentado[segmentador.FORMATO_DISTRIBUICAO]


def test_o_eixo_nao_le_o_texto_do_bloco():
    """A condição que faz o eixo valer (DIARIOS.md §18.3): `ver_bloco` recebe só
    o NOME do formato. No dia em que receber texto, alguém conta marcador lá
    dentro e o eixo passa a provar a si mesmo."""
    import inspect
    params = list(inspect.signature(Inventario.ver_bloco).parameters)
    assert params == ['self', 'formato'], (
        'ver_bloco não pode receber o texto do bloco — ver DIARIOS.md §18.3')


def test_linha_de_distribuicao_fora_da_secao_nao_e_registro():
    """O falso positivo que só o 7º caderno revelou.

    No TRT3 de 10/07/2024, **2.870** linhas têm a forma `SIGLA CNJ` e só
    **1.828** estão na seção que o outline declara Distribuição; as outras
    1.042 são citação dentro de uma `Notificação` (`AIRR 0004300-04.2002...`
    aparece 4 vezes seguidas). Contar as 1.042 como registro faria a perna A
    acusar `2.870 x 1.828` e reprovar a edição de REFERÊNCIA do DEJT por perda
    que não existe. Nos 6 cadernos menores as contagens batiam exatamente, e o
    erro teria passado.
    """
    inv = Inventario(marcadores=MARCADORES_DEJT)
    linha = f'AIRR {_cnj(7)}'
    _ver_linha(inv, linha, [_cnj(7)], em_distribuicao=False)
    assert inv.impresso[NOME_MARCADOR_DISTRIBUICAO] == 0, 'citação não é registro'
    _ver_linha(inv, linha, [_cnj(7)], em_distribuicao=True)
    assert inv.impresso[NOME_MARCADOR_DISTRIBUICAO] == 1
    # e a perna B continua enxergando a linha nos DOIS casos: suprimir o
    # marcador não pode cegar a assinatura do órfão
    assert inv.assinaturas_dos_orfaos([_cnj(7)])


def test_paginas_de_distribuicao_vem_do_outline_da_fonte():
    """A seção sai do índice que o PRÓPRIO PDF carrega — não do segmentador.
    É o que mantém a perna A independente (DIARIOS.md §18.3)."""
    secoes = [(0, 'Vara', 'Notificação'), (3, 'Presidência', 'Distribuição'),
              (6, 'Vara', 'Edital')]
    # a página 6 entra: seção troca no MEIO da página, e a Distribuição imprime
    # até o cabeçalho do Edital. Cortar em 6 (exclusivo) subcontava o impresso —
    # medido: TRT22 caía de 109 para 107 e TRT16 de 154 para 144.
    assert _paginas_de_distribuicao(9, secoes) == {3, 4, 5, 6}
    assert _paginas_de_distribuicao(9, []) == set()
    # a última seção vai até a última página do caderno, e não além
    assert _paginas_de_distribuicao(5, [(2, 'Presidência', 'Distribuição')]) == {2, 3, 4}
