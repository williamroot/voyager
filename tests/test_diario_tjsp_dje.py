"""Testes do coletor DJE/TJSP (`diarios/fontes/tjsp_dje/`).

Todo material aqui é REAL, capturado do `dje.tjsp.jus.br` em 16/08/2026: os
HTMLs são as respostas do e-SAJ (inclusive as três variantes de "HTTP 200 que
não é dado"), e os trechos inline são VERBATIM do texto extraído dos cadernos
12 de 21/07/2025, 12 e 13 de 15/07/2015 — com o espaço espúrio do PDF onde ele
existe de verdade ('Estado de S ão Paulo'), porque é justamente isso que o
coletor tem que aguentar.

Dois princípios organizam o arquivo:

1. **Nenhum teste assere status code.** O e-SAJ responde 200 para o caderno
   inexistente (851 bytes de HTML), para a casca do visualizador (1.207 bytes
   de `<frameset>`) e para página além da última (corpo vazio). Só conteúdo
   prova conteúdo.
2. **A segmentação é medida contra o próprio caderno.** O gate não é "parece
   certo": é a fração dos CNJs impressos no caderno que caiu dentro de algum
   bloco (≥95%). Os testes que precisam do PDF inteiro pulam quando a fixture
   não está presente — mas o comportamento que eles medem também é exercitado
   pelos trechos inline, que rodam sempre.
"""

import os
from datetime import date

import pytest

from diarios.base import (
    CNJ_TOLERANTE,
    MAX_EXTERNAL_ID,
    ColetorError,
    RespostaInvalida,
    UnidadeColeta,
    UnidadeInexistente,
    achar_cnjs,
    exigir_pdf,
)
from diarios.fontes.tjsp_dje import catalogo, pdf, segmentador
from diarios.fontes.tjsp_dje.coletor import ColetorDjeTjsp

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios', 'tjsp_esaj')


def fx(nome: str) -> str:
    return os.path.join(FIXTURES, nome)


def tem(nome: str) -> bool:
    return os.path.exists(fx(nome))


def ler(nome: str) -> str:
    with open(fx(nome), encoding='utf-8', errors='replace') as fh:
        return fh.read()


def ler_bytes(nome: str) -> bytes:
    with open(fx(nome), 'rb') as fh:
        return fh.read()


def paginas_de(texto: str, *, titulos: dict[str, float] | None = None,
               numero: int = 1, corpo: float = 8.0):
    """Monta uma `Pagina` a partir de texto verbatim.

    O tamanho de fonte de cada linha é o do corpo, salvo as declaradas em
    `titulos` — que é exatamente o que o `pdf.py` entrega ao segmentador a
    partir do PDF real. Permite testar a máquina de estados com trecho
    verbatim, sem depender de fixture de 4 MB.
    """
    titulos = titulos or {}
    linhas = [
        pdf.Linha(pagina=numero, tamanho=titulos.get(linha.strip(), corpo), texto=linha)
        for linha in texto.split('\n') if linha.strip()
    ]
    return [pdf.Pagina(numero=numero, linhas=linhas)]


# ═════════════════════════════════════════════════════════════════════════════
# 1. CATÁLOGO — 4.162 edições numa requisição, e a recusa da casca
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not tem('cabecalho_4246_c19.html'), reason='fixture da sonda ausente')
def test_catalogo_traz_as_4162_edicoes_de_uma_vez():
    """O gate da fonte: uma requisição de 268 KB devolve o arquivo inteiro,
    de 2007-10-01 (nuDiario 86) a 2025-07-22 (nuDiario 4247)."""
    edicoes = catalogo.parse_indice(ler('cabecalho_4246_c19.html'))

    assert len(edicoes) == 4162
    assert (edicoes[0].data, edicoes[0].nu_diario) == (date(2025, 7, 22), 4247)
    assert (edicoes[-1].data, edicoes[-1].nu_diario) == (date(2007, 10, 1), 86)
    # o cdVolume muda ao longo dos anos e é o que monta o permalink
    assert edicoes[0].cd_volume == 19 and edicoes[-1].cd_volume == 1


@pytest.mark.skipif(not tem('consultaSimples_p480.html'), reason='fixture da sonda ausente')
def test_catalogo_recusa_a_casca_do_visualizador():
    """`consultaSimples.do` devolve 200 com 1.207 bytes de `<frameset>` vazio.
    É a armadilha clássica: parece resposta, não tem dado."""
    casca = ler('consultaSimples_p480.html')
    assert len(casca) < 2000 and 'frameset' in casca
    with pytest.raises(RespostaInvalida):
        catalogo.parse_indice(casca)


@pytest.mark.skipif(not tem('caderno_inexistente_erro.html'), reason='fixture da sonda ausente')
def test_html_de_erro_de_851_bytes_nao_passa_por_pdf():
    """Caderno que não existe naquela data: HTTP 200, text/html, 851 bytes.
    Num backfill de 4.162 edições isso vira lacuna INVISÍVEL se o coletor
    olhar só o status."""
    corpo = ler_bytes('caderno_inexistente_erro.html')
    assert catalogo.MARCA_CADERNO_INEXISTENTE in corpo.decode('utf-8', 'replace')
    with pytest.raises(RespostaInvalida):
        exigir_pdf(corpo, contexto='teste')


@pytest.mark.skipif(not tem('cabecalho_4246_c19.html'), reason='fixture da sonda ausente')
def test_datas_sem_diario_sao_o_gabarito_do_feriado():
    """A fonte declara as datas sem edição. Usar isso economiza 9 downloads de
    851 bytes por feriado — e evita marcar como falha o que é ausência."""
    fora = catalogo.parse_datas_sem_diario(ler('cabecalho_4246_c19.html'))

    assert len(fora) == 390
    # 22/07/2025 é a última edição publicada; de 23/07 em diante não há mais
    # nenhuma — o arquivo do e-SAJ está FECHADO (o TJSP migrou para o DJEN).
    assert date(2025, 7, 23) in fora
    assert date(2025, 7, 22) not in fora


@pytest.mark.skipif(not tem('index.html'), reason='fixture da sonda ausente')
def test_cadernos_vem_da_home_sem_o_pesquisar_em_todos():
    cadernos = catalogo.parse_cadernos(ler('index.html'))

    assert set(cadernos) == set(catalogo.CADERNOS_PADRAO), 'a tabela medida tem que bater com a home'
    assert cadernos[12] == 'caderno 3 - Judicial - 1ª Instância - Capital - Parte I'
    assert -11 not in cadernos, 'o -11 é "pesquisar em todos", não é caderno'


def test_urls_sao_as_coordenadas_da_fonte():
    assert catalogo.url_download_caderno(date(2015, 7, 15), 13) == (
        'https://dje.tjsp.jus.br/cdje/downloadCaderno.do'
        '?dtDiario=15/07/2015&cdCaderno=13&tpDownload=D')
    assert catalogo.url_pagina_humana(19, 4246, 12, 97).endswith(
        'cdVolume=19&nuDiario=4246&cdCaderno=12&nuSeqpagina=97')
    assert catalogo.chave_unidade(1924, 13) == '1924-13'


# ═════════════════════════════════════════════════════════════════════════════
# 2. PDF — o tamanho da fonte é o que separa título, corpo e mobília
# ═════════════════════════════════════════════════════════════════════════════
FIXTURE_2015 = 'caderno12_20150715_p96-125.pdf'   # 30 páginas reais do caderno 12
FIXTURE_2025 = 'caderno12_20250721.pdf'           # caderno inteiro, 567 páginas


@pytest.mark.skipif(not tem(FIXTURE_2015), reason='fixture do caderno de 2015 ausente')
def test_linhas_trazem_o_tamanho_e_a_hierarquia_de_secao():
    """No caderno real: corpo 8.0, cabeçalho de página 5.5, rodapé 7.0,
    número da página 1.0 e título de vara 11.0. É essa escala que permite
    fechar bloco na virada de vara."""
    corpo = ler_bytes(FIXTURE_2015)
    leitor = pdf.abrir(corpo)
    assert pdf.tamanho_do_corpo(leitor) == 8.0

    pagina = list(pdf.paginas(corpo))[1]
    tamanhos = {linha.tamanho for linha in pagina.linhas}
    assert {1.0, 5.5, 7.0, 8.0, 11.0} <= tamanhos
    titulos = [linha.texto for linha in pagina.linhas if linha.tamanho == 11.0]
    assert titulos == ['1ª Vara Cível']


@pytest.mark.skipif(not tem(FIXTURE_2015), reason='fixture do caderno de 2015 ausente')
def test_conferir_data_pega_caderno_do_dia_errado():
    """O download é um GET sem estado; a única prova de que veio o dia certo é
    o 'Disponibilização:' impresso em toda página. Dado com data errada é pior
    que dado ausente."""
    pagina = next(iter(pdf.paginas(ler_bytes(FIXTURE_2015))))
    assert pdf.data_impressa(pagina) == date(2015, 7, 15)

    pdf.conferir_data(pagina, date(2015, 7, 15))          # não levanta
    with pytest.raises(RespostaInvalida):
        pdf.conferir_data(pagina, date(2015, 7, 16))


# ═════════════════════════════════════════════════════════════════════════════
# 3. SEGMENTADOR — os quatro formatos, em texto verbatim do caderno
# ═════════════════════════════════════════════════════════════════════════════
# Verbatim do caderno 12 de 21/07/2025, página 1 (relação de distribuídos).
BLOCO_DISTRIBUICAO = """RELAÇÃO DOS FEITOS CÍVEIS DISTRIBUÍDOS ÀS VARAS DO FORO CENTRAL  CÍVEL EM 17/07/2025
PROCESSO :1099645-98.2025.8.26.0100
CLASSE :EMBARGOS À EXECUÇÃO
EMBARGTE : Laura Cristina Lopes Cardoso
ADVOGADO : 293286/SP - Luiz Fernando Vian Espeiorin
EMBARGDO : Marcelo Guimarães Sarti
ADVOGADO : 181477/SP - Maristela Canata Bourached Gardonio
VARA :15ª VARA CÍVEL
PROCESSO :1099646-83.2025.8.26.0100
CLASSE :PROCEDIMENTO COMUM CÍVEL
REQTE : K.G.S.G.
ADVOGADO : 426141/SP - Tom Henrique Santis
REQDO : F.S.O.B.
VARA :19ª VARA CÍVEL"""


def test_distribuicao_extrai_parte_papel_e_oab():
    blocos = list(segmentador.segmentar(paginas_de(BLOCO_DISTRIBUICAO), 8.0))
    assert len(blocos) == 2, 'cada PROCESSO : abre um bloco'

    pub = segmentador.interpretar(blocos[0])
    assert pub.cnj == '1099645-98.2025.8.26.0100'
    assert pub.nome_classe == 'EMBARGOS À EXECUÇÃO'
    assert pub.nome_orgao == '15ª VARA CÍVEL', 'a VARA do bloco vale mais que a seção'
    assert pub.tipo_comunicacao == 'Distribuição', 'o cabeçalho da relação declara isso'
    assert pub.destinatarios == [
        {'nome': 'Laura Cristina Lopes Cardoso', 'polo': 'A', 'papel': 'EMBARGTE'},
        {'nome': 'Marcelo Guimarães Sarti', 'polo': 'P', 'papel': 'EMBARGDO'},
    ]
    assert pub.destinatario_advogados[0] == {'advogado': {
        'nome': 'Luiz Fernando Vian Espeiorin', 'numero_oab': '293286', 'uf_oab': 'SP'}}
    # texto VERBATIM: nada de "consertar" o que veio do PDF
    assert pub.texto.startswith('PROCESSO :1099645-98.2025.8.26.0100\nCLASSE :EMBARGOS')
    assert pub.texto.endswith('VARA :15ª VARA CÍVEL')


# Verbatim do caderno 12 de 15/07/2015 (distribuição criminal, DIPO).
BLOCO_CRIMINAL = """PROCESSO :0054948-14.2015.8.26.0050
CLASSE :INQUÉRITO POLICIAL
IP : 613/2015 - São Paulo
AUTOR : J.P.
DECLARANTE : P.P.V.
VARA :DIPO 4 - SEÇÃO 4.1.2"""


def test_codigo_de_peca_criminal_nao_vira_parte():
    """'IP : 613/2015 - São Paulo' é o número do inquérito, não gente. Sem
    esta guarda o acervo ganha 1.593 "partes" chamadas 613/2015 num caderno só."""
    pub = segmentador.interpretar(
        next(iter(segmentador.segmentar(paginas_de(BLOCO_CRIMINAL), 8.0))))

    nomes = [p['nome'] for p in pub.destinatarios]
    assert '613/2015 - São Paulo' not in nomes
    assert nomes == ['J.P.', 'P.P.V.']
    # DECLARANTE não mapeia para polo nenhum: fica vazio em vez de chutado
    assert [p['polo'] for p in pub.destinatarios] == ['A', '']


# Verbatim do caderno 13 de 15/07/2015 (1ª instância interior, Ibitinga).
BLOCO_ATO = """Processo 0000003-43.2011.8.26.0236 (236.01.2011.000003) - Reintegração / Manutenção de Posse - Posse - Bv Leasing
Arrendamento Mercantil Sa - Decurso de prazo. Manifeste o requerente. - ADV: CRISTINA ELIANE FERREIRA DA MOTA (OAB
192562/SP), ANA PAULA Z. TOLEDO BARBOSA DA SILVA FERNANDES (OAB 268862/SP)"""


def test_ato_extrai_classe_e_advogados_mas_abstem_de_partes():
    """No formato 2 não há marcador entre a última parte e o começo do
    despacho. Extrair parte aí seria chute — 'Decurso de prazo' viraria nome.
    A casa manda abster: o nome fica no texto verbatim."""
    pub = segmentador.interpretar(
        next(iter(segmentador.segmentar(paginas_de(BLOCO_ATO), 8.0))))

    assert pub.cnj == '0000003-43.2011.8.26.0236'
    assert pub.nome_classe == 'Reintegração / Manutenção de Posse'
    assert pub.destinatarios == []
    assert [a['advogado']['nome'] for a in pub.destinatario_advogados] == [
        'CRISTINA ELIANE FERREIRA DA MOTA', 'ANA PAULA Z. TOLEDO BARBOSA DA SILVA FERNANDES']
    assert pub.destinatario_advogados[0]['advogado']['numero_oab'] == '192562'


def test_numero_antigo_entre_parenteses_e_guardado_e_nao_vira_classe():
    """O caderno do interior imprime o de-para CNJ ↔ numeração pré-2010. Ele
    não existe em nenhum outro lugar do sistema — e, se não for reconhecido,
    vira a 'classe' de 31 mil movimentações."""
    pub = segmentador.interpretar(
        next(iter(segmentador.segmentar(paginas_de(BLOCO_ATO), 8.0))))

    assert pub.numero_impresso == '0000003-43.2011.8.26.0236 (236.01.2011.000003)'
    assert '(' not in pub.nome_classe


# Verbatim do caderno 13 de 15/07/2015. O aposto entre parênteses NÃO é
# puramente numérico — tem letra —, e era por isso que ele escapava do
# `_RE_SO_NUMERO` e virava a classe.
BLOCO_APENSADO = (
    'Processo 0001079-63.2015.8.26.0236 (processo principal 0000359-67.2013.8.26) - '
    'Impugnação de Assistência Judiciária - Assistência Judiciária Gratuita - '
    'Banco Bradesco S/A - Vistos. Manifeste-se o impugnado em 5 dias. - '
    'ADV: JOSE ROBERTO SILVA (OAB 123456/SP)'
)


def test_aposto_com_letra_entre_parenteses_nao_vira_classe():
    """REGRESSÃO 16/08/2026: 963 de 31.408 itens (3,1%) do caderno 13 gravavam
    `nome_classe='(processo principal 0000359-67.2013.8.26)'` ou
    `'(apensado ao processo ...)'`, PERDENDO a classe verdadeira e inflando o
    vocabulário de classes de 267 para 1.090 valores.

    O teste acima cobria só o parêntese puramente NUMÉRICO (o de-para pré-2010);
    o `_RE_SO_NUMERO` exige ausência de letra, e 'processo principal' tem. A
    regra passou a ser posicional: segmento que ABRE com '(' nunca é classe —
    nenhuma classe da TPU começa com parêntese."""
    pub = segmentador.interpretar(
        next(iter(segmentador.segmentar(paginas_de(BLOCO_APENSADO), 8.0))))
    assert pub.cnj == '0001079-63.2015.8.26.0236'
    assert pub.nome_classe == 'Impugnação de Assistência Judiciária'


# Verbatim do caderno 12 de 21/07/2025 (Colégio Recursal). O 'S ão' e o
# 'nã o' são o espaço espúrio do PDF, e estão aqui de propósito.
BLOCO_2INST = """Nº 0000566-73.2020.8.26.0025 - Processo Digital  - Recurso Inominado Cível - Angatuba - Recorrente: Estado de S ão
Paulo - Recorrida: Kelly Cristina Gasques - Vistos. Trata-se de  recurso extraordinário interposto contra acórdão proferido pel a
Turma Recursal. É o breve relatório. Decido. O apelo extremo nã o merece prosperar. - Advs: Simone Mass ilon Bezerra Barbosa
(OAB: 301497/SP) - Jaqueline Beatriz Ferreira Domingues (OAB: 259428/SP)"""


def test_segunda_instancia_extrai_partes_rotuladas():
    pub = segmentador.interpretar(
        next(iter(segmentador.segmentar(paginas_de(BLOCO_2INST), 8.0))))

    assert pub.cnj == '0000566-73.2020.8.26.0025'
    assert pub.nome_classe == 'Recurso Inominado Cível'
    assert pub.nome_orgao == 'Angatuba', 'a comarca vem no cabeçalho do bloco'
    assert [(p['papel'], p['polo']) for p in pub.destinatarios] == [
        ('Recorrente', 'A'), ('Recorrida', 'P')]
    assert [a['advogado']['numero_oab'] for a in pub.destinatario_advogados] == [
        '301497', '259428']


# Verbatim: pauta de julgamento virtual (pág. 430) e, logo abaixo, uma CITAÇÃO
# de jurisprudência (pág. 103) que o PDF quebra de linha bem no CNJ.
BLOCO_PAUTA = """0110390-43.2025.8.26.9061; Processo Digital; Agravo de Instrume nto; 5ª Turma Recursal de Fazenda Pública; FLÁVIO PINELLA
HELAEHIL - COLÉGIO RECURSAL; Fórum de Suzano; Vara do Juizado Especial Cível e Criminal; Procedimento do Juizado Especial
Cível; 1006878-75.2025.8.26.0606; Perdas e Danos; Agravante: Ga briel Candido e Silva; Advogado: Caio Alexandro Barreto (OAB:
417281/SP); Agravado: Estado de São Paulo; Ficam as partes intimadas para manifestarem-se."""

CITACAO_JURISPRUDENCIA = """RECURSO NÃO CONHECIDO. (TJSP; Agravo de Instrumento 0105590-69. 2025.8.26.9061; Relator (a): Vera Lúcia Calvio de
0106620-42.2025.8.26.9061; Relator (a):Mônica Soares Machado; Ó rgão Julgador: 3ª Turma Recursal Cível; Foro de São Carlos
-Vara do Juizado Especial Civel; Data do Julgamento: 07/05/2025 ) Posto isso, indefiro a inicial."""


def test_pauta_abre_bloco_e_atribui_o_ato_ao_processo_do_recurso():
    blocos = list(segmentador.segmentar(paginas_de(BLOCO_PAUTA), 8.0))
    assert len(blocos) == 1

    pub = segmentador.interpretar(blocos[0])
    # o ato é do agravo, não do processo de origem citado na mesma corrida
    assert pub.cnj == '0110390-43.2025.8.26.9061'
    # 'Instrume nto' com espaço no meio é o que o PDF entrega (kerning por
    # caractere). O campo sai VERBATIM: "consertar" classe por adivinhação é o
    # tipo de conserto que inventa dado. Quem quiser vocabulário normalizado
    # casa contra a TPU depois, com dicionário.
    assert pub.nome_classe == 'Agravo de Instrume nto'
    assert ('Agravante', 'A') in [(p['papel'], p['polo']) for p in pub.destinatarios]
    # 'Relator (a)' e 'Órgão Julgador' casam o mesmo padrão "<rótulo>: <valor>"
    # e NÃO podem virar parte
    assert all('Turma' not in p['nome'] for p in pub.destinatarios)


def test_citacao_de_jurisprudencia_nao_abre_bloco_de_pauta():
    """Sem exigir 'Processo Digital' logo depois do número, a âncora dispara
    dentro de acórdão citado — e parte um ato ao meio, atribuindo a segunda
    metade ao processo CITADO. Erro pior que não achar: parece dado bom."""
    blocos = list(segmentador.segmentar(paginas_de(CITACAO_JURISPRUDENCIA), 8.0))
    assert blocos == [], 'nenhuma dessas linhas é início de publicação'


def test_titulo_de_secao_e_linha_estrutural_fecham_o_bloco():
    """Sem isso, o cabeçalho da relação seguinte ('JUÍZO DE DIREITO DA 1ª VARA
    CÍVEL', 'RELAÇÃO Nº 0186/2015') seria colado no fim do despacho anterior —
    e o órgão da relação nova viraria texto do processo velho."""
    texto = (BLOCO_ATO + '\n1ª Vara Cível\n'
             'JUÍZO DE DIREITO DA 1ª VARA CÍVEL\n'
             'RELAÇÃO Nº 0186/2015\n' + BLOCO_ATO.replace('0000003-43', '0000024-19'))
    paginas = paginas_de(texto, titulos={'1ª Vara Cível': 11.0})
    blocos = list(segmentador.segmentar(paginas, 8.0))

    assert len(blocos) == 2
    assert 'JUÍZO DE DIREITO' not in blocos[0].texto
    assert 'RELAÇÃO Nº' not in blocos[0].texto
    assert segmentador.interpretar(blocos[1]).nome_orgao == '1ª Vara Cível'


# Verbatim do caderno 11 de 15/07/2015 (2ª instância, 'Subseção III -
# Processos Distribuídos'): tabela de duas colunas, sem texto de ato.
GRADE_DISTRIBUICAO = """Câmara Especial
Ao Des. Eros Piceli (Vice Presidente)
Apelação
0000274-71.2015.8.26.0540 0001637-06.2015.8.26.0472
0002227-96.2013.8.26.0360"""


def test_grade_de_distribuicao_vira_uma_publicacao_por_numero():
    """21% dos CNJs do caderno 11 estão SÓ na grade. Cada número vira uma
    publicação com o cabeçalho reconstruído — e o `texto` deixa claro que é
    entrada de grade, não despacho."""
    blocos = list(segmentador.segmentar(paginas_de(GRADE_DISTRIBUICAO), 8.0))
    assert len(blocos) == 3, 'dois números na primeira linha, um na segunda'

    pub = segmentador.interpretar(blocos[0])
    assert pub.cnj == '0000274-71.2015.8.26.0540'
    assert pub.formato == segmentador.FORMATO_LISTA
    assert pub.nome_classe == 'Apelação'
    assert pub.tipo_comunicacao == 'Distribuição'
    assert pub.texto == ('Ao Des. Eros Piceli (Vice Presidente)\n'
                         'Apelação\n0000274-71.2015.8.26.0540')
    # e o segundo número da MESMA linha vira publicação própria, com id próprio
    assert segmentador.interpretar(blocos[1]).cnj == '0001637-06.2015.8.26.0472'


def test_numero_sozinho_dentro_de_despacho_aberto_nao_vira_grade():
    """A quebra de linha do PDF deixa número sozinho no meio de despacho. Se
    isso virasse entrada de grade, o ato perderia o pedaço e nasceria uma
    publicação fantasma."""
    texto = ('Processo 1005255-88.2015.8.26.0100 - Procedimento Ordinário - Vistos. Cite-se nos autos\n'
             '2113576-63.2025.8.26.0000\ne intime-se. - ADV: FULANO DE TAL (OAB 123456/SP)')
    blocos = list(segmentador.segmentar(paginas_de(texto), 8.0))

    assert len(blocos) == 1
    assert '2113576-63.2025.8.26.0000' in blocos[0].texto
    assert blocos[0].cnj == '1005255-88.2015.8.26.0100'


# Verbatim do caderno 11 de 15/07/2015 (resultado de julgamento): abre no
# número, sem 'Nº' na frente, separador ' - '.
RESULTADO_JULGAMENTO = """0004389-73.2012.8.26.0045 - Processo Físico  - Apelação - Santa Isabel - Relator: Des.: Roberto Midolla, Revisor: Des.:
Otávio Henrique - Apelante: Rinaldo Francisco da Silva - Apelado: Ministério Público do Estado de São Paulo - Negaram
provimento ao recurso. V. U. - Advogado: Carlos Roberto Vissechi (OAB: 99588/SP)"""


def test_resultado_de_julgamento_abre_bloco_no_numero():
    """Sem esta âncora, 1.534 dos 16.339 CNJs do caderno 11 (9%) ficavam fora
    de qualquer bloco — e o gate de cobertura reprovava a unidade inteira."""
    blocos = list(segmentador.segmentar(paginas_de(RESULTADO_JULGAMENTO), 8.0))
    assert len(blocos) == 1

    pub = segmentador.interpretar(blocos[0])
    assert pub.cnj == '0004389-73.2012.8.26.0045'
    assert pub.nome_classe == 'Apelação'
    assert [(p['papel'], p['polo']) for p in pub.destinatarios] == [
        ('Apelante', 'A'), ('Apelado', 'P')]
    assert pub.destinatario_advogados == [{'advogado': {
        'nome': 'Carlos Roberto Vissechi', 'numero_oab': '99588', 'uf_oab': 'SP'}}]


def test_cnj_do_bloco_ignora_processo_citado_no_corpo():
    """O despacho cita outros processos. O CNJ do bloco é o do CABEÇALHO —
    pegar o primeiro do bloco inteiro atribuiria o ato ao processo errado."""
    texto = ('Processo 1005255-88.2015.8.26.0100 - Procedimento Ordinário - Vistos. '
             'Cite-se o precedente do Agravo 2113576-63.2025.8.26.0000 e intime-se.')
    bloco = next(iter(segmentador.segmentar(paginas_de(texto), 8.0)))

    assert bloco.cnj == '1005255-88.2015.8.26.0100'
    assert '2113576-63.2025.8.26.0000' in achar_cnjs(bloco.texto_corrido)


# ── o gate mecânico: cobertura dos CNJs impressos ───────────────────────────
def _cobertura(corpo: bytes) -> tuple[float, int, int]:
    leitor = pdf.abrir(corpo)
    tamanho = pdf.tamanho_do_corpo(leitor)
    no_texto: set[str] = set()
    em_bloco: set[str] = set()
    blocos = 0

    def _paginas():
        for pagina in pdf.paginas(corpo):
            no_texto.update(achar_cnjs(pagina.texto))
            yield pagina

    for bloco in segmentador.segmentar(_paginas(), tamanho):
        blocos += 1
        em_bloco.update(achar_cnjs(bloco.texto_corrido))
    return len(no_texto & em_bloco) / len(no_texto), len(no_texto), blocos


@pytest.mark.skipif(not tem(FIXTURE_2025), reason='fixture de 4 MB não commitada')
def test_cobertura_de_cnjs_no_caderno_12_de_2025():
    """GATE da fonte: ≥95% dos CNJs TOLERANTES impressos no caderno têm que
    cair dentro de um bloco. Medido: 4.101 CNJs distintos, 99,1% cobertos.

    A regex tolerante é obrigatória aqui — a estrita acha 4.727 ocorrências no
    mesmo caderno contra 5.243 da tolerante, e os 516 de diferença sumiriam em
    silêncio."""
    cobertura, total, blocos = _cobertura(ler_bytes(FIXTURE_2025))

    assert total > 4000, f'o caderno tem milhares de CNJs, achei {total}'
    assert blocos > 3000
    assert cobertura >= 0.95, f'cobertura {cobertura:.1%} de {total} CNJs'


@pytest.mark.skipif(not tem(FIXTURE_2015), reason='fixture do caderno de 2015 ausente')
def test_cobertura_no_caderno_historico_de_2015():
    """A janela desta fonte é histórica: o layout de 2015 (que é o que vai ser
    coletado de verdade) tem que passar no mesmo gate."""
    cobertura, total, blocos = _cobertura(ler_bytes(FIXTURE_2015))

    assert total > 100 and blocos > 100
    assert cobertura >= 0.95, f'cobertura {cobertura:.1%} de {total} CNJs'


# ═════════════════════════════════════════════════════════════════════════════
# 4. COLETOR — janela, ausência, idempotência e o que NÃO é gravado
# ═════════════════════════════════════════════════════════════════════════════
class SessaoFalsa:
    """Substitui a `SessaoDiario` para não tocar a rede no teste. Devolve
    bytes REAIS capturados da fonte — o que se está testando é o coletor, não
    o `requests`."""

    def __init__(self, corpo: bytes):
        self.corpo = corpo
        self.pedidos: list[str] = []

    def get(self, url, **kw):
        self.pedidos.append(url)

        class _Resp:
            content = self.corpo
            text = self.corpo.decode('utf-8', 'replace')
        return _Resp()


def unidade_2015(caderno: int = 12) -> UnidadeColeta:
    return UnidadeColeta(
        chave=catalogo.chave_unidade(1924, caderno), data=date(2015, 7, 15),
        tribunal_sigla='TJSP', rotulo=f'Edição 1924 — caderno {caderno}',
        meta={'nu_diario': 1924, 'cd_volume': 9, 'cd_caderno': caderno,
              'caderno': 'caderno 3 - Judicial - 1ª Instância - Capital - Parte I'},
    )


def test_janela_de_exclusividade_e_a_medicao_do_djen():
    """2025-03-14 é o primeiro dia em que o DJEN devolve TJSP (count=0 em toda
    data anterior). Daí em diante esta porta é redundante e o runner recusa."""
    coletor = ColetorDjeTjsp()

    assert coletor.janela_inicio == date(2007, 10, 1)
    assert coletor.dentro_da_janela(date(2015, 7, 15))
    assert coletor.dentro_da_janela(date(2025, 3, 13))
    assert not coletor.dentro_da_janela(date(2025, 3, 14))


def test_caderno_de_editais_fica_fora_do_catalogo():
    """Caderno 5 (Editais e Leilões) é texto corrido, não relação: o
    segmentador cobre 4,7% dos CNJs dele. Coletar assim seria gravar lacuna e
    chamar de acervo."""
    coletor = ColetorDjeTjsp()
    coletor._cadernos_cache = None
    coletor.sessao = SessaoFalsa(b'<html>sem select</html>')  # cai no fallback medido

    cadernos = coletor._cadernos()
    assert 14 not in cadernos
    assert 12 in cadernos and len(cadernos) == 8


@pytest.mark.skipif(not tem('caderno_inexistente_erro.html'), reason='fixture da sonda ausente')
def test_caderno_que_nao_existe_e_ausencia_e_nao_falha():
    """Em 2015 os cadernos 19 e 20 ainda não existiam. Tratar isso como falha
    faria o backfill retentar o mesmo dia para sempre — a lição do
    `_dia_coberto` do DJEN."""
    coletor = ColetorDjeTjsp()
    coletor.sessao = SessaoFalsa(ler_bytes('caderno_inexistente_erro.html'))

    with pytest.raises(UnidadeInexistente):
        list(coletor.coletar(unidade_2015(20)))


def test_resposta_vazia_nao_e_ausencia_e_sim_falha():
    """Corpo vazio (o que o e-SAJ devolve para página além da última) NÃO é
    'não existe': é resposta quebrada, e tem que ser retentada."""
    coletor = ColetorDjeTjsp()
    coletor.sessao = SessaoFalsa(b'')

    with pytest.raises(RespostaInvalida):
        list(coletor.coletar(unidade_2015(12)))


@pytest.mark.skipif(not tem(FIXTURE_2015), reason='fixture do caderno de 2015 ausente')
def test_coleta_produz_item_completo_e_idempotente():
    """Ponta a ponta com PDF real: os itens saem com os campos que o diário
    rotula, e coletar duas vezes devolve exatamente os mesmos `external_id` —
    que é o que faz `ignore_conflicts` deduplicar em vez de duplicar."""
    coletor = ColetorDjeTjsp()
    coletor.sessao = SessaoFalsa(ler_bytes(FIXTURE_2015))

    itens = list(coletor.coletar(unidade_2015(12)))
    assert len(itens) > 100

    item = itens[0]
    assert item.cnj.endswith('.8.26.0100') or '.8.26.' in item.cnj
    assert item.external_id.startswith('tjsp-dje:1924-12-')
    assert len(item.external_id) <= MAX_EXTERNAL_ID
    assert item.data_disponibilizacao.date() == date(2015, 7, 15)
    assert item.meio == 'D'
    assert item.meio_completo.startswith('DJE/TJSP (e-SAJ) —')
    assert item.link.startswith('https://dje.tjsp.jus.br/cdje/consultaSimples.do?cdVolume=9')
    assert item.hash and len(item.hash) == 40, 'fingerprint_ato do ato, não hash opaco'
    assert item.texto.strip()
    assert item.codigo_classe == '', 'o diário não traz código TPU — campo vazio honesto'
    assert item.tipo_documento == ''

    de_novo = list(coletor.coletar(unidade_2015(12)))
    assert [i.external_id for i in de_novo] == [i.external_id for i in itens]


def test_cnj_de_outro_tribunal_nao_vira_processo_do_tjsp(monkeypatch):
    """O caderno cita recurso ao STJ e precedente do TRF. Gravar isso como
    processo do TJSP contaminaria o acervo — o número existe, mas não é daqui."""
    coletor = ColetorDjeTjsp()
    texto = ('Processo 0001234-56.2015.4.03.6100 - Apelação - Vistos. Nada a prover. '
             '- ADV: FULANO DE TAL (OAB 123456/SP)')
    monkeypatch.setattr(coletor, '_baixar', lambda unidade: b'%PDF-falso')
    monkeypatch.setattr(pdf, 'abrir', lambda corpo: None)
    monkeypatch.setattr(pdf, 'tamanho_do_corpo', lambda leitor, **kw: 8.0)
    monkeypatch.setattr(pdf, 'paginas', lambda corpo: iter(paginas_de(texto)))
    monkeypatch.setattr(pdf, 'conferir_data', lambda pagina, esperada: None)

    assert list(coletor.coletar(unidade_2015(12))) == []


def test_cobertura_abaixo_do_piso_derruba_a_unidade(monkeypatch):
    """Se a segmentação perder metade do caderno, a unidade NÃO pode ser dada
    como coletada: fica pendente e é retentada. Meia edição gravada em
    silêncio é o pior desfecho possível num backfill de 4.162 edições."""
    coletor = ColetorDjeTjsp()
    # 300 CNJs impressos, nenhum dentro de bloco (nenhuma linha tem âncora).
    # O dígito verificador é CALCULADO (mod 97, Res. CNJ 65): desde 2026-08-16
    # o `achar_cnjs` descarta DV inválido, então uma fixture com número
    # fabricado no chute não exercitaria o gate — ela sumiria antes dele.
    def _com_dv(sequencial: int) -> str:
        corpo = f'{sequencial:07d}'
        dv = 98 - (int(corpo + '202582 60100'.replace(' ', '')) * 100) % 97
        return f'{corpo}-{dv:02d}.2025.8.26.0100'

    texto = '\n'.join(f'consta o processo {_com_dv(1000000 + i)} no relatório'
                      for i in range(300))
    monkeypatch.setattr(coletor, '_baixar', lambda unidade: b'%PDF-falso')
    monkeypatch.setattr(pdf, 'abrir', lambda corpo: None)
    monkeypatch.setattr(pdf, 'tamanho_do_corpo', lambda leitor, **kw: 8.0)
    monkeypatch.setattr(pdf, 'paginas', lambda corpo: iter(paginas_de(texto)))
    monkeypatch.setattr(pdf, 'conferir_data', lambda pagina, esperada: None)

    with pytest.raises(ColetorError, match='segmentação suspeita'):
        list(coletor.coletar(unidade_2015(12)))


def test_esperado_e_none_porque_a_fonte_nao_declara():
    """O e-SAJ não diz quantas publicações tem no caderno (o DEJT diz). Fingir
    um gabarito seria pior que não ter: o gate desta fonte é a cobertura de
    CNJ, medida no próprio texto."""
    assert ColetorDjeTjsp().esperado(unidade_2015(12)) is None


def test_regex_tolerante_e_a_usada_no_gate():
    """Prova de que o gate não é auto-indulgente: o número quebrado pelo PDF
    ('1 002997-71...') conta como CNJ impresso e portanto TEM que ser coberto."""
    trecho = 'Recurso nº: 1 002997-71.2023.8.26.0344 - recurso'
    assert CNJ_TOLERANTE.search(trecho)
    assert achar_cnjs(trecho) == ['1002997-71.2023.8.26.0344']
