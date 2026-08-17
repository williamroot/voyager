"""Testes do coletor DEJT (`diarios/fontes/dejt/`).

Tudo aqui é material REAL capturado da fonte em 16/08/2026: os HTMLs são as
respostas do JBoss do CSJT, e os trechos de texto são verbatim dos cadernos de
10/07/2024 (TRT22 e TRT3). Os testes que dependem dos PDFs inteiros (2,9 MB e
62,7 MB, não commitados) marcam skip quando o arquivo não está presente — mas o
comportamento que eles medem também é exercitado pelos trechos inline, que
rodam sempre.

O princípio que organiza este arquivo: **nenhum teste assere status code**. O
DEJT devolve 200 para tudo — para a tela de sempre quando a view expira, para o
formulário em branco quando a conversa Seam aninha, e para o HTML de erro no
lugar do PDF. Só conteúdo prova conteúdo.
"""

import gzip
import os
import re
from datetime import date, datetime

import pytest

from diarios.base import RespostaInvalida, UnidadeColeta, UnidadeInexistente
from diarios.fontes.dejt import catalogo, segmentador
from diarios.fontes.dejt.coletor import ColetorDEJT

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios', 'dejt')


def fx(nome: str) -> str:
    return os.path.join(FIXTURES, nome)


def ler(nome: str) -> str:
    with open(fx(nome), encoding='utf-8', errors='replace') as fh:
        return fh.read()


def tem(nome: str) -> bool:
    return os.path.exists(fx(nome))


# ═════════════════════════════════════════════════════════════════════════════
# 1. CATÁLOGO — ler a tabela de cadernos sem depender do framework
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_catalogo_le_a_linha_real_do_trt3():
    """A resposta real da busca TRT3/10-07-2024 tem UMA linha, e ela tem que
    virar edição 4011/2024 do TRT3 com o link de download localizado."""
    linhas = catalogo.linhas_de_cadernos(ler('busca_trt3_2024-07-10_J.html'))
    assert len(linhas) == 1
    linha = linhas[0]
    assert linha.data == date(2024, 7, 10)
    assert linha.sigla == 'TRT3'
    assert (linha.edicao, linha.ano_edicao) == ('4011', '2024')
    assert linha.titulo == 'Edição 4011/2024 - Caderno do TRT da 3ª Região - Judiciário'
    assert linha.source, 'sem o source do postback não há como baixar o PDF'


def test_legenda_da_tela_nao_e_contador():
    """A legenda `... por Tribunal (16)` sai 16 em TODA consulta — inclusive na
    de 1 linha. Quem usar como contador constrói métrica falsa; contamos <tr>."""
    html_ = ler('busca_trt3_2024-07-10_J.html') if tem('busca_trt3_2024-07-10_J.html') else ''
    if not html_:
        pytest.skip('fixture da sonda ausente')
    assert 'por Tribunal (16)' in html_
    assert len(catalogo.linhas_de_cadernos(html_)) == 1


@pytest.mark.parametrize(('titulo', 'esperado'), [
    ('Edição 4011/2024 - Caderno do TRT da 3ª Região - Judiciário', 'TRT3'),
    # Até ~2012 o título vinha em CAIXA ALTA e com sufixo 'Jurídico'. São 13.656
    # das 95.679 linhas do inventário: casar sem IGNORECASE perderia 14% do
    # acervo em silêncio.
    ('Edição 1011/2012 - Caderno do TRT da 3ª REGIÃO - Jurídico', 'TRT3'),
    ('Edição 4535/2026 - Caderno do Tribunal Superior do Trabalho - Judiciário', 'TST'),
    ('Edição 1341/2013 - Caderno Tribunal Superior do Trabalho - Judiciário', 'TST'),
    ('Edição 1341/2013 - Caderno do Conselho Superior da Justiça do Trabalho', 'CSJT'),
    ('Edição 1339/2013 - Escola Nacional de Formação e Aperfeiçoamento de '
     'Magistrados do Trabalho', 'ENAMAT'),
    ('Edição 9999/2030 - Caderno de coisa nenhuma', None),
])
def test_sigla_do_titulo_cobre_a_deriva_de_16_anos(titulo, esperado):
    assert catalogo.sigla_do_titulo(titulo) == esperado


def test_indice_do_tribunal_bate_com_o_select_da_fonte():
    assert catalogo.indice_do_tribunal('TST') == 0
    assert catalogo.indice_do_tribunal('TRT1') == 1
    assert catalogo.indice_do_tribunal('TRT24') == 24
    assert catalogo.indice_do_tribunal('CSJT') == 25
    with pytest.raises(ValueError):
        catalogo.indice_do_tribunal('TJSP')


@pytest.mark.skipif(not tem('inventario_J_2008_2026.html.gz'),
                    reason='inventário de 18 anos (765 KB gz) não commitado')
def test_inventario_de_18_anos_sai_em_uma_resposta():
    """O catálogo inteiro do acervo cabe numa requisição — é o que permite
    MEDIR a jazida antes de decidir baixar 765 GB."""
    with gzip.open(fx('inventario_J_2008_2026.html.gz'), 'rt',
                   encoding='utf-8', errors='replace') as fh:
        linhas = catalogo.linhas_de_cadernos(fh.read())
    assert len(linhas) >= 95_000, f'inventário devolveu só {len(linhas)} linhas'
    # a jazida é o que está ANTES da migração para o DJEN
    pre_djen = [x for x in linhas if x.data <= date(2024, 7, 31)]
    assert len(pre_djen) >= 86_000
    assert min(x.data for x in linhas) == date(2008, 6, 9)
    # e o mapeamento de sigla não pode deixar linha órfã
    assert sum(1 for x in linhas if x.sigla is None) == 0


# ═════════════════════════════════════════════════════════════════════════════
# 2. A CASCA — "HTTP 200 que não é dado", que nesta fonte tem três formas
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not tem('diariocon_J.html'), reason='fixture da sonda ausente')
def test_eco_rejeita_a_tela_inicial_disfarcada_de_resultado():
    """O GET inicial de `diariocon` JÁ vem com 16 linhas — as edições de HOJE.

    Se o POST expirar a view, o JBoss devolve 200 com essa tela, e um coletor
    ingênuo gravaria as edições de 2026 achando que coletou 2015. O eco do
    `dataIni` é o que pega isso.
    """
    tela_inicial = ler('diariocon_J.html')
    assert len(catalogo.linhas_de_cadernos(tela_inicial)) == 16, 'a casca TEM linhas'
    with pytest.raises(RespostaInvalida, match='dataIni'):
        catalogo.conferir_eco(tela_inicial, data_ini='10/07/2024', tribunal_idx=3,
                              contexto='teste')


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_eco_aceita_a_busca_de_verdade():
    html_ = ler('busca_trt3_2024-07-10_J.html')
    assert catalogo.conferir_eco(html_, data_ini='10/07/2024', tribunal_idx=3) is html_


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_eco_rejeita_quando_o_tribunal_nao_voltou_selecionado():
    html_ = ler('busca_trt3_2024-07-10_J.html')
    with pytest.raises(RespostaInvalida, match='selected'):
        catalogo.conferir_eco(html_, data_ini='10/07/2024', tribunal_idx=15)


def test_eco_rejeita_html_que_nao_e_a_tela():
    with pytest.raises(RespostaInvalida, match='dataIni'):
        catalogo.conferir_eco('<html><body>Erro 599</body></html>',
                              data_ini='10/07/2024', tribunal_idx=3)


# ═════════════════════════════════════════════════════════════════════════════
# 3. j_id — a fragilidade número 1 desta fonte
# ═════════════════════════════════════════════════════════════════════════════
def test_nenhum_jid_literal_no_codigo_da_fonte():
    """`j_id132` é gerado pelo JSF na compilação do XHTML e muda a cada deploy
    do DEJT. Hardcodá-lo é escrever um coletor com data de validade."""
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        'diarios', 'fontes', 'dejt')
    for arquivo in os.listdir(base):
        if not arquivo.endswith('.py'):
            continue
        with open(os.path.join(base, arquivo), encoding='utf-8') as fh:
            codigo = fh.read()
        # Comentários explicando o problema podem citar; código, não.
        sem_comentario = re.sub(r'#.*', '', codigo)
        sem_docstring = re.sub(r'(?s)""".*?"""', '', sem_comentario)
        assert not re.search(r'j_id\d', sem_docstring), (
            f'{arquivo} tem j_id literal fora de comentário'
        )


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_link_e_achado_mesmo_com_jid_trocado_pelo_deploy():
    """Simula o deploy do DEJT que renumera os `j_id`: o coletor tem que achar
    a mesma linha, porque procura por TÍTULO + classe CSS, não por id."""
    html_ = ler('busca_trt3_2024-07-10_J.html')
    titulo = 'Edição 4011/2024 - Caderno do TRT da 3ª Região - Judiciário'
    antes = catalogo.achar_source_por_titulo(html_, titulo)
    depois_do_deploy = re.sub(r'j_id\d+', 'j_id777', html_)
    depois = catalogo.achar_source_por_titulo(depois_do_deploy, titulo)
    assert antes and depois and depois.endswith('j_id777')
    assert antes != depois, 'o teste só vale se o id realmente mudou'


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_linha_sem_classe_link_download_nao_conta():
    """Se o DEJT trocar a estrutura da tabela, a linha deixa de casar e o
    coletor levanta drift — em vez de gravar meia edição."""
    html_ = ler('busca_trt3_2024-07-10_J.html').replace('link-download', 'outra-coisa')
    assert catalogo.linhas_de_cadernos(html_) == []


# ═════════════════════════════════════════════════════════════════════════════
# 4. GABARITO — a fonte declara quantas matérias existem
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not tem('materia_dia_TRT3_dia_pre-DJEN.html'), reason='fixture da sonda ausente')
def test_gabarito_do_trt3_e_16717():
    assert catalogo.total_de_materias(ler('materia_dia_TRT3_dia_pre-DJEN.html')) == 16_717


@pytest.mark.skipif(not tem('materia_dia_TRT22_2024-07-10.html'), reason='fixture da sonda ausente')
def test_gabarito_do_trt22_e_885():
    assert catalogo.total_de_materias(ler('materia_dia_TRT22_2024-07-10.html')) == 885


@pytest.mark.skipif(not tem('materia_dia_TRT3_dia_pos-DJEN.html'), reason='fixture da sonda ausente')
def test_gabarito_mostra_o_corte_do_djen():
    """13/08/2026: 18 matérias no DEJT contra ≥10 mil no DJEN, no mesmo TRT3 e
    no mesmo dia. É este número que diz que a jazida do DEJT é histórica."""
    assert catalogo.total_de_materias(ler('materia_dia_TRT3_dia_pos-DJEN.html')) == 18


def test_gabarito_ausente_devolve_none_em_vez_de_zero():
    """Abster > chutar: sem o rodapé, devolver 0 reprovaria uma coleta boa."""
    assert catalogo.total_de_materias('<html>sem rodapé</html>') is None


# ═════════════════════════════════════════════════════════════════════════════
# 5. SEGMENTADOR — trechos VERBATIM do caderno do TRT22 de 10/07/2024
# ═════════════════════════════════════════════════════════════════════════════
# Bloco real (pág. 14 do caderno do TRT22). Escolhido de propósito: o CORPO cita
# um processo DIFERENTE do cabeçalho ('RT 0001243-07.2012.5.22.0103'), que é
# exatamente a armadilha de atribuição que a casa proíbe.
BLOCO_PRECATORIO = (
    'Processo Nº Precat-0088986-87.2023.5.22.0000\n'
    'Relator MARCO AURÉLIO LUSTOSA\n'
    'CAMINHA\n'
    'REQUERENTE MARIA IRANILDA RODRIGUES LEAL\n'
    'RAMOS\n'
    'ADVOGADO MARCOS VINICIUS ARAUJO\n'
    'VELOSO(OAB: 8526/PI)\n'
    'REQUERIDO ESTADO DO PIAUI\n'
    ' \n'
    'Intimado(s)/Citado(s): \n'
    '  - MARIA IRANILDA RODRIGUES LEAL RAMOS\n'
    ' \n'
    '            PODER JUDICIÁRIO\n'
    '            JUSTIÇA DO\n'
    'INTIMAÇÃO\n'
    'Fica V. Sa. intimado para tomar ciência do Despacho ID 77e51f1\n'
    'proferido nos autos.\n'
    'PROCESSO: 0088986-87.2023.5.22.0000 (Precatório)\n'
    'Código para aferir autenticidade deste caderno: 216342\n'
    '4011/2024 Tribunal Regional do Trabalho da 22ª Região 14\n'
    'Data da Disponibilização: Quarta-feira, 10 de Julho de 2024\n'
    ' \n'
    'DESPACHO\n'
    'Analisando o documento (Id. d6c87ef) presente nos autos da\n'
    'reclamação trabalhista (RT 0001243-07.2012.5.22.0103), que deu\n'
    'origem ao presente precatório, observa-se o atendimento ao\n'
    'requisito legal do art. 100, § 2º, da CF/88.\n'
)

# Bloco real do TRT3 (pág. 2 do caderno de 10/07/2024), seção Distribuição: o
# OUTRO formato de OAB ('(OAB/MG 110695)') e o outro separador de papel (' - ').
BLOCO_DISTRIBUICAO_TRT3 = (
    'ATOrd 0010177-81.2023.5.03.0010\n'
    '3ª Vara do Trabalho de Betim\n'
    'AUTOR - CARLOS FERREIRA DA SILVA\n'
    'ADVOGADO - FELIPE DOURADO LAGES (OAB/MG 110695)\n'
    'ADVOGADO - RODRIGO DOURADO DUARTE (OAB/MG 120494)\n'
    'RÉU - H MIX TECNOLOGIA E CONSULTORIA EM CONCRETO\n'
    'LTDA\n'
    'ADVOGADO - BRUNA SILVA ANDRADE\n'
    '(OAB/MG 146611)\n'
)


def _um_bloco(texto: str, tipo: str = 'Notificação'):
    achados = list(segmentador.blocos([texto], [(0, 'Secretaria Judiciaria', tipo)]))
    assert len(achados) == 1, f'esperava 1 bloco, vieram {len(achados)}'
    return achados[0]


def test_cnj_vem_do_cabecalho_e_nao_do_corpo():
    """O corpo cita `RT 0001243-07.2012.5.22.0103`; o ato é do precatório
    `0088986-87.2023.5.22.0000`. Emprestar o CNJ citado ao ato é a atribuição
    errada que a casa proíbe (`abster > chutar`)."""
    bloco = _um_bloco(BLOCO_PRECATORIO)
    assert bloco.cnj == '0088986-87.2023.5.22.0000'
    assert '0001243-07.2012.5.22.0103' in bloco.texto, 'o citado continua no verbatim'


def test_partes_e_advogado_do_bloco_real():
    bloco = _um_bloco(BLOCO_PRECATORIO)
    assert bloco.sigla_classe == 'Precat'
    nomes = {p['nome']: p for p in bloco.partes}
    assert 'MARIA IRANILDA RODRIGUES LEAL RAMOS' in nomes, 'nome quebrado em 2 linhas'
    assert nomes['MARIA IRANILDA RODRIGUES LEAL RAMOS']['polo'] == 'A'
    assert nomes['MARIA IRANILDA RODRIGUES LEAL RAMOS']['intimado'] is True
    assert nomes['ESTADO DO PIAUI']['polo'] == 'P'
    assert nomes['ESTADO DO PIAUI']['intimado'] is False
    assert bloco.advogados == [{
        'advogado': {'nome': 'MARCOS VINICIUS ARAUJO VELOSO',
                     'numero_oab': '8526', 'uf_oab': 'PI'},
        'papel': 'ADVOGADO',
        'parte': 'MARIA IRANILDA RODRIGUES LEAL RAMOS',
    }]


# Bloco REAL do TRT22 (caderno de 10/07/2024, precatório 0086840-73.2023...):
# o cabeçalho de partes tem uma linha em branco NO MEIO — resíduo de layout do
# PDF, não fim de tabela. É a fixture da regressão do achado de 16/08/2026.
BLOCO_CABECALHO_COM_LINHA_EM_BRANCO = (
    'Processo Nº Precat-0086840-73.2023.5.22.0000\n'
    'REQUERENTE ROSA MARIA AGUIAR LANDIM\n'
    ' \n'
    'ADVOGADO CLAUDI PINHEIRO DE ARAUJO(OAB: 264/PI)\n'
    'REQUERIDO MUNICIPIO DE SIMPLICIO MENDES\n'
    'ADVOGADO NAIRA DE SOUSA RIBEIRO(OAB: 12345/PI)\n'
    ' \n'
    'Intimado(s)/Citado(s): \n'
    '  - ROSA MARIA AGUIAR LANDIM\n'
    ' \n'
    'DESPACHO\n'
    'Vistos. Intime-se o ente devedor para manifestação em 10 dias.\n'
)


def test_linha_em_branco_no_meio_do_cabecalho_nao_engole_o_polo_passivo():
    """REGRESSÃO DO BLOQUEIO ACHADO EM 16/08/2026.

    A regra antiga era "o cabeçalho termina na PRIMEIRA linha em branco". Medida
    no caderno real do TRT22 de 10/07/2024, ela descartava 379 entradas em 86 dos
    999 blocos (8,6%) — e em 25 blocos (2,5%) o POLO PASSIVO INTEIRO, que num
    precatório é o ENTE DEVEDOR: exatamente o dado pelo qual esta fonte existe.

    Não era abstenção (que a casa aceita): era fato PARCIAL vendido como
    completo, sem log e sem contador. Aqui a lista tem que sair inteira.
    """
    bloco = _um_bloco(BLOCO_CABECALHO_COM_LINHA_EM_BRANCO)
    por_nome = {p['nome']: p for p in bloco.partes}
    assert 'ROSA MARIA AGUIAR LANDIM' in por_nome
    assert 'MUNICIPIO DE SIMPLICIO MENDES' in por_nome, 'o ente devedor sumia aqui'
    assert por_nome['MUNICIPIO DE SIMPLICIO MENDES']['polo'] == 'P'
    assert {a['advogado']['nome'] for a in bloco.advogados} == {
        'CLAUDI PINHEIRO DE ARAUJO', 'NAIRA DE SOUSA RIBEIRO'}
    # E a proteção original continua de pé: o corpo do ato NÃO vira parte.
    assert 'Vistos. Intime-se o ente devedor para manifestação em 10 dias.' not in por_nome
    assert 'DESPACHO' not in por_nome


def test_linha_em_branco_seguida_de_corpo_ainda_fecha_o_cabecalho():
    """O outro lado da mesma moeda: atravessar linha em branco só vale quando o
    que vem depois é papel do vocabulário fechado. Texto de despacho depois da
    linha em branco tem que continuar encerrando a tabela — senão a correção
    acima viraria a porta de entrada para nome de parte inventado."""
    bloco = _um_bloco(
        # DV calculado (mod 97): desde 2026-08-16 `achar_cnjs` recusa DV inválido
        # e o bloco seria descartado antes de o teste medir o que quer medir.
        'Processo Nº ATOrd-0000123-55.2023.5.22.0001\n'
        'RECLAMANTE JOAO DA SILVA\n'
        ' \n'
        'SENTENCA PROFERIDA NOS AUTOS\n'
        'RECLAMADO EMPRESA QUE VEIO DEPOIS DO CORPO\n'
    )
    nomes = {p['nome'] for p in bloco.partes}
    assert nomes == {'JOAO DA SILVA'}


def test_relator_nao_vira_parte():
    """`Relator`/`Revisor`/`Complemento` são metadado do cabeçalho. Se virassem
    parte, a ficha da parte encheria de nome de desembargador."""
    bloco = _um_bloco(BLOCO_PRECATORIO)
    assert 'MARCO AURÉLIO LUSTOSA CAMINHA' not in {p['nome'] for p in bloco.partes}


def test_corpo_do_ato_nao_vira_parte():
    """`PODER JUDICIÁRIO`, `INTIMAÇÃO` e `DESPACHO` são caixa alta como os nomes
    de parte. O vocabulário fechado de papéis é o que impede a invenção."""
    bloco = _um_bloco(BLOCO_PRECATORIO)
    nomes = {p['nome'] for p in bloco.partes}
    assert not (nomes & {'PODER JUDICIÁRIO', 'INTIMAÇÃO', 'DESPACHO', 'JUSTIÇA DO'})
    assert len(bloco.partes) == 2


def test_mobilia_de_pagina_sai_do_verbatim():
    """Um ato que atravessa página engoliria cabeçalho e rodapé no meio da
    frase. Isso é mobília do caderno, não texto do ato."""
    limpo = segmentador.limpar_mobilia(BLOCO_PRECATORIO)
    assert 'Código para aferir autenticidade' not in limpo
    assert 'Data da Disponibilização' not in limpo
    assert '4011/2024 Tribunal Regional do Trabalho da 22ª Região 14' not in limpo
    # e o conteúdo do ato continua intacto
    assert 'presente precatório' in limpo


def test_os_dois_formatos_de_oab_convivem():
    """`(OAB: 8526/PI)` e `(OAB/MG 110695)` convivem no mesmo acervo."""
    bloco = _um_bloco(BLOCO_DISTRIBUICAO_TRT3, tipo='Distribuição')
    oabs = {(a['advogado']['nome'], a['advogado']['numero_oab'], a['advogado']['uf_oab'])
            for a in bloco.advogados}
    assert ('FELIPE DOURADO LAGES', '110695', 'MG') in oabs
    assert ('BRUNA SILVA ANDRADE', '146611', 'MG') in oabs, 'OAB na linha seguinte'


def test_advogado_fica_com_a_parte_que_a_fonte_listou_acima():
    """O vínculo é a ADJACÊNCIA publicada pela fonte, não inferência nossa."""
    bloco = _um_bloco(BLOCO_DISTRIBUICAO_TRT3, tipo='Distribuição')
    por_adv = {a['advogado']['nome']: a['parte'] for a in bloco.advogados}
    assert por_adv['FELIPE DOURADO LAGES'] == 'CARLOS FERREIRA DA SILVA'
    assert por_adv['BRUNA SILVA ANDRADE'] == 'H MIX TECNOLOGIA E CONSULTORIA EM CONCRETO LTDA'


def test_polo_de_papel_recursal_fica_vazio():
    """Num recurso, o RECORRENTE tanto pode ser o autor quanto o réu. A fonte
    não diz, e nós não adivinhamos — o `papel` vai verbatim, o `polo` fica vazio."""
    bloco = _um_bloco(
        'Processo Nº ROT-0000597-26.2023.5.22.0001\n'
        'RECORRENTE SINDICATO NACIONAL DOS\n'
        'AEROVIARIOS\n'
        'RECORRIDO GOL LINHAS AEREAS S.A.\n'
        ' \n'
        'Intimado(s)/Citado(s): \n'
        '  - GOL LINHAS AEREAS S.A.\n'
    )
    por_nome = {p['nome']: p for p in bloco.partes}
    assert por_nome['SINDICATO NACIONAL DOS AEROVIARIOS']['polo'] == ''
    assert por_nome['SINDICATO NACIONAL DOS AEROVIARIOS']['papel'] == 'RECORRENTE'
    # o ponto final de 'S.A.' é parte do nome publicado — não pode ser podado,
    # senão o nome deixa de casar com a lista de intimados
    assert por_nome['GOL LINHAS AEREAS S.A.']['intimado'] is True


def test_ancora_de_distribuicao_so_vale_dentro_da_secao_de_distribuicao():
    """`ATOrd 0010177-81...` em linha isolada só é ato na seção Distribuição.
    Fora dela seria uma citação solta virando movimentação fantasma."""
    fora = list(segmentador.blocos([BLOCO_DISTRIBUICAO_TRT3],
                                   [(0, 'Secretaria Judiciaria', 'Notificação')]))
    assert fora == []
    dentro = list(segmentador.blocos([BLOCO_DISTRIBUICAO_TRT3],
                                     [(0, 'Presidência.', 'Distribuição')]))
    assert len(dentro) == 1


def test_bloco_sem_cnj_no_cabecalho_e_descartado():
    sem_cnj = ('Processo Nº\n'
               'AUTOR - FULANO DE TAL\n'
               ' \n'
               'texto do edital sem número de processo\n')
    assert list(segmentador.blocos([sem_cnj], [(0, 'Vara', 'Edital')])) == []


def test_secao_do_outline_vira_orgao_e_tipo():
    bloco = _um_bloco(BLOCO_PRECATORIO, tipo='Notificação')
    assert bloco.unidade == 'Secretaria Judiciaria'
    assert bloco.tipo == 'Notificação'


# ── capa: validação de CONTEÚDO do download ─────────────────────────────────
CAPA_REAL = (
    ' \nCaderno Judiciário do Tribunal Regional do Trabalho da 22ª Região\n'
    'DIÁRIO ELETRÔNICO DA JUSTIÇA DO TRABALHO\nPODER JUDICIÁRIO\n'
    'REPÚBLICA FEDERATIVA DO BRASIL\nNº4011/2024\n'
    'Data da disponibilização: Quarta-feira, 10 de Julho de 2024.\n'
)


def test_capa_de_caderno_de_verdade_passa():
    segmentador.conferir_capa([CAPA_REAL], edicao='4011')


def test_capa_de_outro_pdf_qualquer_e_recusada():
    with pytest.raises(RespostaInvalida, match='DIÁRIO ELETRÔNICO'):
        segmentador.conferir_capa(['Contrato de prestação de serviços'], edicao=None)


def test_capa_de_edicao_diferente_e_recusada():
    """Prova que o postback trouxe a LINHA pedida, e não a primeira da tabela."""
    with pytest.raises(RespostaInvalida, match='edição'):
        segmentador.conferir_capa([CAPA_REAL], edicao='4026')


# ── gates de volume contra o gabarito da própria fonte ──────────────────────
def _segmentar_pdf(nome: str):
    with open(fx(nome), 'rb') as fh:
        paginas, secoes = segmentador.ler_caderno(fh.read())
    return list(segmentador.blocos(paginas, secoes))


@pytest.mark.skipif(not tem('trt22_2024-07-10_pag1a6.txt'), reason='fixture de texto ausente')
def test_seis_paginas_reais_seguidas_viram_materias_inteiras():
    """Texto REAL das páginas 1-6 do caderno do TRT22 (extraído do PDF, com a
    mobília ainda dentro). Exercita o que os trechos inline não pegam: ato que
    atravessa página, e seção que troca no meio da página."""
    with open(fx('trt22_2024-07-10_pag1a6.txt'), encoding='utf-8') as fh:
        paginas = [segmentador.limpar_mobilia(p) for p in fh.read().split('\f')]
    assert len(paginas) == 6
    secoes = [(0, 'Secretaria do Tribunal Pleno', 'Pauta'),
              (2, 'Secretaria da 2ª Turma', 'Pauta'),
              (3, 'Secretaria Judiciaria', 'Notificação')]
    achados = list(segmentador.blocos(paginas, secoes))
    assert len(achados) >= 12
    assert all(a.cnj and a.texto for a in achados)
    # a troca de seção no meio da página 4 separa Pauta de Notificação
    assert {a.tipo for a in achados} == {'Pauta', 'Notificação'}
    # e nenhum bloco carrega a mobília de página no verbatim
    assert not any('aferir autenticidade' in a.texto for a in achados)


@pytest.mark.skipif(not tem('trt22_2024-07-10_jud.pdf'), reason='PDF de 2,9 MB não commitado')
def test_gate_trt22_acha_as_885_materias_declaradas():
    achados = _segmentar_pdf('trt22_2024-07-10_jud.pdf')
    assert len(achados) >= 885 * 0.95, f'segmentou só {len(achados)} de 885'
    assert all(a.cnj for a in achados)
    assert sum(1 for a in achados if a.advogados) / len(achados) > 0.8


@pytest.mark.skipif(not tem('trt3_2024-07-10_jud.pdf'),
                    reason='PDF de 62,7 MB não commitado (13.853 páginas, ~4 min)')
def test_gate_trt3_acha_as_16717_materias_declaradas():
    """O gate da missão: o site declara 16.717 matérias para o TRT3 em
    10/07/2024, e o segmentador tem que achar ≥95% delas."""
    achados = _segmentar_pdf('trt3_2024-07-10_jud.pdf')
    assert len(achados) >= 16_717 * 0.95, f'segmentou só {len(achados)} de 16.717'


# ═════════════════════════════════════════════════════════════════════════════
# 6. COLETOR — o contrato, com o transporte fingido e o dado real
# ═════════════════════════════════════════════════════════════════════════════
class SessaoJSFFalsa:
    """Devolve as respostas REAIS capturadas da fonte, sem tocar no CSJT."""

    def __init__(self, http, html_busca='', paginas=None):
        self.html_busca = html_busca
        self.paginas = paginas or []
        self.baixou = []

    def buscar(self, data_ini, data_fim, tribunal_idx='', caderno='J'):
        return self.html_busca

    def baixar_caderno(self, source, **kw):
        self.baixou.append(source)
        return b'%PDF-1.4' + b'\0' * 40_000


def _coletor_com(monkeypatch, html_busca, paginas, secoes):
    coletor = ColetorDEJT()
    falsa = SessaoJSFFalsa(None, html_busca)
    monkeypatch.setattr('diarios.fontes.dejt.coletor.SessaoJSF', lambda http: falsa)
    monkeypatch.setattr('diarios.fontes.dejt.coletor.ler_caderno',
                        lambda corpo: (paginas, secoes))
    return coletor, falsa


UNIDADE_TRT3 = UnidadeColeta(
    chave='J-TRT3-2024-07-10-4011', data=date(2024, 7, 10), tribunal_sigla='TRT3',
    rotulo='Edição 4011/2024 - Caderno do TRT da 3ª Região - Judiciário',
    meta={'titulo': 'Edição 4011/2024 - Caderno do TRT da 3ª Região - Judiciário',
          'edicao': '4011', 'ano_edicao': '2024', 'caderno': 'J',
          'tribunal_idx': 3, 'data_br': '10/07/2024'},
)


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_coletar_devolve_item_com_os_campos_que_o_recon_prometeu(monkeypatch):
    paginas = [CAPA_REAL, BLOCO_PRECATORIO]
    secoes = [(1, 'Secretaria Judiciaria', 'Notificação')]
    coletor, _ = _coletor_com(monkeypatch, ler('busca_trt3_2024-07-10_J.html'),
                              paginas, secoes)
    itens = list(coletor.coletar(UNIDADE_TRT3))
    assert len(itens) == 1
    item = itens[0]
    assert item.cnj == '0088986-87.2023.5.22.0000'
    assert item.external_id.startswith('dejt:'), 'namespace é o que separa do DJEN'
    assert len(item.external_id) <= 64
    assert item.data_disponibilizacao.date() == date(2024, 7, 10)
    assert item.nome_orgao == 'Secretaria Judiciaria'
    assert item.tipo_comunicacao == 'Notificação'
    assert item.numero_comunicacao == '4011/2024'
    assert item.meio == 'D'
    assert 'DEJT' in item.meio_completo
    assert item.hash and len(item.hash) == 40
    assert item.destinatarios and item.destinatario_advogados
    assert 'presente precatório' in item.texto, 'texto verbatim do ato'


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_coletar_abstem_nos_campos_que_a_fonte_nao_da(monkeypatch):
    """Campo vazio honesto > campo chutado. O DEJT não dá o código da classe do
    CNJ (só a sigla, que fica no texto) nem URL estável do ato."""
    coletor, _ = _coletor_com(monkeypatch, ler('busca_trt3_2024-07-10_J.html'),
                              [CAPA_REAL, BLOCO_PRECATORIO],
                              [(1, 'Secretaria Judiciaria', 'Notificação')])
    item = next(iter(coletor.coletar(UNIDADE_TRT3)))
    assert item.codigo_classe == ''
    assert item.nome_classe == ''
    assert item.link == ''
    assert item.data_envio is None
    assert item.id_orgao is None
    assert 'Precat' in item.texto, 'a sigla da classe fica verbatim no texto'


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_external_id_e_deterministico_entre_coletas(monkeypatch):
    """Backfill de 86 mil cadernos tem retry. Re-coletar a mesma edição tem que
    produzir os MESMOS ids, senão a re-ingestão duplica em vez de deduplicar."""
    args = (ler('busca_trt3_2024-07-10_J.html'), [CAPA_REAL, BLOCO_PRECATORIO],
            [(1, 'Secretaria Judiciaria', 'Notificação')])
    coletor, _ = _coletor_com(monkeypatch, *args)
    primeira = [i.external_id for i in coletor.coletar(UNIDADE_TRT3)]
    coletor2, _ = _coletor_com(monkeypatch, *args)
    segunda = [i.external_id for i in coletor2.coletar(UNIDADE_TRT3)]
    assert primeira == segunda != []


@pytest.mark.skipif(not tem('busca_trt3_2023-08-14_J.html'), reason='fixture da sonda ausente')
def test_feriado_forense_e_inexistente_e_nao_falha(monkeypatch):
    """14/08/2023 devolve ZERO linhas com o eco correto — é feriado forense, com
    os dias vizinhos cheios (edições 3781..3785). Tratar como lacuna faria o
    backfill retentar o mesmo dia para sempre: é o bug que o `_dia_coberto` do
    djen/jobs.py já pagou.

    A unidade aqui NÃO tem meta de catálogo: é o caminho de sonda / `--chave` de
    um dia que ninguém catalogou. É só nesse caminho que "zero linhas" pode ser
    ausência — ver o teste irmão logo abaixo.
    """
    html_ = ler('busca_trt3_2023-08-14_J.html')
    assert catalogo.linhas_de_cadernos(html_) == []
    coletor, _ = _coletor_com(monkeypatch, html_, [], [])
    unidade = UnidadeColeta(
        chave='J-TRT3-2023-08-14-0', data=date(2023, 8, 14), tribunal_sigla='TRT3',
        meta={'caderno': 'J', 'tribunal_idx': 3, 'data_br': '14/08/2023'},
    )
    with pytest.raises(UnidadeInexistente):
        list(coletor.coletar(unidade))


@pytest.mark.skipif(not tem('busca_trt3_2023-08-14_J.html'), reason='fixture da sonda ausente')
def test_unidade_catalogada_que_some_da_tabela_e_drift_e_nao_feriado(monkeypatch):
    """REGRESSÃO DO BLOQUEIO ACHADO EM 16/08/2026.

    Se a unidade veio do CATÁLOGO (tem título e número de edição no meta), o
    DEJT já a listou uma vez. Ela sumir agora não é feriado forense: é o layout
    do DEJT mudado (basta o CSJT renomear uma classe CSS) ou a edição removida.

    Antes desta correção o caminho era: zero linhas → `UnidadeInexistente` →
    `EdicaoDiario.inexistente` (TERMINAL, o tick não reenfileira) +
    `IngestionRun.status='success'` + ZERO `SchemaDriftAlert`. Ou seja, um deploy
    do CSJT no meio do backfill marcava as ~47 mil edições restantes como "não
    existe caderno nesse dia", gravava esse fato FALSO no watermark e reportava
    sucesso — a lacuna invisível que esta porta inteira existe para não repetir.
    """
    html_ = ler('busca_trt3_2023-08-14_J.html')
    coletor, _ = _coletor_com(monkeypatch, html_, [], [])
    alertou = []
    monkeypatch.setattr(coletor, '_alertar_drift', lambda *a, **k: alertou.append(a))
    unidade = UnidadeColeta(
        chave='J-TRT3-2023-08-14-3782', data=date(2023, 8, 14), tribunal_sigla='TRT3',
        meta={'titulo': 'Edição 3782/2023', 'edicao': '3782', 'ano_edicao': '2023',
              'caderno': 'J', 'tribunal_idx': 3, 'data_br': '14/08/2023'},
    )
    with pytest.raises(RespostaInvalida, match='não é feriado forense'):
        list(coletor.coletar(unidade))
    assert alertou, 'sumir da tabela tem que virar SchemaDriftAlert, não silêncio'


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_edicao_que_sumiu_da_tabela_nao_vira_coleta_silenciosa(monkeypatch):
    """A tabela respondeu, mas com outra edição. Isso é drift de layout ou
    edição removida — nunca "coletou 0 itens com sucesso"."""
    coletor, _ = _coletor_com(monkeypatch, ler('busca_trt3_2024-07-10_J.html'), [], [])
    monkeypatch.setattr(coletor, '_alertar_drift', lambda *a, **k: None)
    unidade = UnidadeColeta(
        chave='J-TRT3-2024-07-10-9999', data=date(2024, 7, 10), tribunal_sigla='TRT3',
        meta={'titulo': 'Edição 9999/2024 - Caderno que não existe',
              'edicao': '9999', 'ano_edicao': '2024', 'caderno': 'J',
              'tribunal_idx': 3, 'data_br': '10/07/2024'},
    )
    with pytest.raises(RespostaInvalida, match='nenhuma casa'):
        list(coletor.coletar(unidade))


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_catalogo_do_coletor_monta_a_unidade_de_coleta(monkeypatch):
    coletor, _ = _coletor_com(monkeypatch, ler('busca_trt3_2024-07-10_J.html'), [], [])
    unidades = list(coletor.catalogar(date(2024, 7, 10), date(2024, 7, 10)))
    assert len(unidades) == 1
    u = unidades[0]
    assert u.chave == 'J-TRT3-2024-07-10-4011'
    assert u.tribunal_sigla == 'TRT3'
    assert u.meta['tribunal_idx'] == 3
    # o `source` do postback NÃO pode ser persistido: ele expira no próximo
    # deploy do DEJT e o coletor tem que reencontrá-lo pelo título
    assert 'source' not in u.meta
    assert u.meta['titulo'] == u.rotulo


@pytest.mark.skipif(not tem('busca_trt3_2024-07-10_J.html'), reason='fixture da sonda ausente')
def test_bloco_repetido_na_edicao_nao_conta_duas_vezes(monkeypatch):
    """O caderno às vezes imprime o mesmo ato duas vezes na mesma página. O
    banco ignora o conflito, mas a CONTAGEM mentiria: a segunda coleta da mesma
    edição reportaria `novas=1` para sempre — e o critério de aceite é
    `novas=0`. Medido no TRT22 de 10/07/2024: 999 blocos, 998 ids distintos."""
    paginas = [CAPA_REAL, BLOCO_PRECATORIO + BLOCO_PRECATORIO]
    secoes = [(1, 'Secretaria Judiciaria', 'Notificação')]
    coletor, _ = _coletor_com(monkeypatch, ler('busca_trt3_2024-07-10_J.html'),
                              paginas, secoes)
    itens = list(coletor.coletar(UNIDADE_TRT3))
    assert len(itens) == 1, 'o mesmo ato na mesma página é UM item'
    # ...e o mesmo ato em PÁGINAS diferentes continua sendo dois: são duas
    # publicações distintas, e a coordenada física está no id.
    coletor2, _ = _coletor_com(monkeypatch, ler('busca_trt3_2024-07-10_J.html'),
                               [CAPA_REAL, BLOCO_PRECATORIO, BLOCO_PRECATORIO],
                               [(1, 'Secretaria Judiciaria', 'Notificação'),
                                (2, 'Secretaria Judiciaria', 'Notificação')])
    assert len(list(coletor2.coletar(UNIDADE_TRT3))) == 2


# ═════════════════════════════════════════════════════════════════════════════
# 7. DERIVA DE FORMATO EM 16 ANOS — o que o coletor NÃO faz, e diz que não faz
# ═════════════════════════════════════════════════════════════════════════════
# Os 68 bytes abaixo são VERBATIM do começo do caderno do TRT22 de 10/03/2010
# baixado em 16/08/2026: um envelope PKCS#7 (`signedData`, OID
# 1.2.840.113549.1.7.2) servido com Content-Type `application/pdf` e
# Content-Disposition `Diario_436__10_3_2010.pdf`. HTTP 200, header de PDF,
# bytes que não são PDF.
ENVELOPE_CMS_REAL = (
    b'0\x83\x0eA\xe4\x06\t*\x86H\x86\xf7\r\x01\x07\x02\xa0\x83\x0eA\xd40\x83\x0eA'
    b'\xcf\x02\x01\x011\x0b0\t\x06\x05+\x0e\x03\x02\x1a\x05\x000\x83\x0e:z\x06\t*'
    b'\x86H\x86\xf7\r\x01\x07\x01\xa0\x83\x0e:j\x04\x83\x0e:e'
)


def test_caderno_assinado_e_desembrulhado():
    from diarios.fontes.dejt.sessao_jsf import desembrulhar_assinatura

    assinado = ENVELOPE_CMS_REAL + b'%PDF-1.4\nmiolo do caderno\n%%EOF\n' + b'\xa0\x82\x05\xaa'
    limpo = desembrulhar_assinatura(assinado)
    assert limpo.startswith(b'%PDF-1.4')
    assert limpo.endswith(b'%%EOF'), 'a cauda da assinatura atrapalha o startxref'


def test_pdf_cru_passa_intacto_pelo_desembrulho():
    from diarios.fontes.dejt.sessao_jsf import desembrulhar_assinatura

    cru = b'%PDF-1.4\nconteudo\n%%EOF\n'
    assert desembrulhar_assinatura(cru) is cru


def test_html_de_erro_nao_vira_pdf_pelo_desembrulho():
    from diarios.fontes.dejt.sessao_jsf import desembrulhar_assinatura

    html_ = b'<html><body>Erro 599</body></html>'
    assert desembrulhar_assinatura(html_) == html_


@pytest.mark.skipif(not tem('trt22_2010-03-10_jud.p7s'), reason='caderno de 2010 não commitado')
def test_caderno_de_2010_de_verdade_vira_pdf_legivel():
    from diarios.fontes.dejt.sessao_jsf import desembrulhar_assinatura

    with open(fx('trt22_2010-03-10_jud.p7s'), 'rb') as fh:
        bruto = fh.read()
    assert not bruto.startswith(b'%PDF'), 'a fixture tem que ser o arquivo assinado'
    paginas, _ = segmentador.ler_caderno(desembrulhar_assinatura(bruto))
    assert len(paginas) == 160
    segmentador.conferir_capa(paginas, edicao='436')


def test_era_pre_pje_e_recusada_antes_de_baixar():
    """Medido contra o gabarito do próprio DEJT no TRT22: 0% de cobertura em
    2013, 22% em 2014, 72% em 2016, ≥95% de 2018 em diante. A matéria antiga é
    prosa corrida ('32. PROCESSO TRT-22ª/2ª TURMA/RO/0017700-10...') e exige
    outro parser. Até ele existir, o coletor ABSTÉM — e abstém antes do
    download, para não queimar banda do CSJT nem gravar meia edição."""
    from diarios.base import ColetorError

    coletor = ColetorDEJT()
    assert coletor.segmentavel_desde == date(2018, 1, 1)
    antiga = UnidadeColeta(chave='J-TRT22-2013-03-11-1182', data=date(2013, 3, 11),
                           tribunal_sigla='TRT22', meta={})
    with pytest.raises(ColetorError, match='pré-PJe'):
        list(coletor.coletar(antiga))


# ═════════════════════════════════════════════════════════════════════════════
# 8. JANELA DE EXCLUSIVIDADE — medida, não chutada
# ═════════════════════════════════════════════════════════════════════════════
def test_janela_termina_no_dia_em_que_o_djen_assumiu():
    """31/07/2024: caderno do TRT3 com 18,46 MB (ed. 4026). 01/08/2024: 1,48 MB
    (ed. 4027). No mesmo dia as matérias nacionais caem de 183.567 para 211."""
    coletor = ColetorDEJT()
    assert coletor.janela_inicio == date(2008, 6, 9)
    assert coletor.janela_fim == date(2024, 7, 31)
    assert coletor.dentro_da_janela(date(2024, 7, 31))
    assert not coletor.dentro_da_janela(date(2024, 8, 1))
    assert not coletor.dentro_da_janela(date(2008, 6, 8))


def test_registro_e_conduta_de_rede():
    from diarios.base import listar, obter

    assert 'dejt' in listar()
    coletor = obter('dejt')
    assert isinstance(coletor, ColetorDEJT)
    # teto auto-imposto: a fonte não tem rate limit nem WAF nem robots.txt,
    # então quem limita somos nós.
    assert coletor.sessao.rps <= 2.0
    assert coletor.janela_horaria == (20, 6)


def test_data_disponibilizacao_e_datetime_com_fuso():
    """`Movimentacao.data_disponibilizacao` é DateTimeField; datetime naive
    quebraria em USE_TZ."""
    from django.utils import timezone

    coletor = ColetorDEJT()
    assert coletor.slug == 'dejt'
    agora = timezone.now()
    assert isinstance(agora, datetime) and timezone.is_aware(agora)
