"""Extração de OAB/CPF/CNPJ/CNJ do texto das publicações.

Os casos abaixo são TRECHOS REAIS do índice (colhidos em 15/08/2026), não
exemplos inventados — é a diferença entre um extrator que passa no teste e um
que funciona em 1,16 bilhão de publicações.

O que estes testes protegem:
  - as TRÊS formas de OAB medidas no corpus (53,1% / 45,8% / 0,9%);
  - o dígito verificador como porteiro: número parecido com CPF NÃO é CPF;
  - ausência de chave quando não há entidade (campo vazio em 1,16B docs custa
    disco e faz `exists` mentir).
"""
from search import entidades_texto as E

# trechos reais do índice
PJE = ('<td>ADVOGADO(A)</td><td>: PALOMA MACIEL PANIZZI (OAB PR116436)</td>'
       '</tr><tr><td>ADVOGADO(A)</td><td>: JAILSON NERES FERREIRA (OAB TO011228)')
ESAJ = 'ADV: FULANO DE TAL (OAB 123456/SP), BELTRANO DA SILVA (OAB 98765/RJ)'
BARRA = 'Intimação do advogado SICRANO (OAB/MG 45678) para manifestar-se'
COLON = 'RENATA GUARANA (OAB: DF09930) e RICARDO ULLMANN (OAB: RS63214)'


def test_pega_as_tres_formas_de_oab():
    assert E.oabs(E.limpar(PJE)) == ['PR116436', 'TO011228']
    assert E.oabs(ESAJ) == ['SP123456', 'RJ98765']
    assert E.oabs(BARRA) == ['MG45678']
    assert E.oabs(COLON) == ['DF09930', 'RS63214']


def test_normaliza_pra_forma_unica():
    """O ponto todo: quem digita de um jeito tem que achar quem foi publicado
    de outro. As quatro grafias abaixo são a MESMA inscrição."""
    formas = ['(OAB SP123456)', 'OAB 123456/SP', 'OAB/SP 123456', 'OAB: SP123456']
    assert {E.oabs(f)[0] for f in formas} == {'SP123456'}


def test_uf_inventada_nao_vira_oab():
    """'OAB nº 12345 - fl. 3' não pode virar a OAB do estado 'FL'."""
    assert E.oabs('OAB nº 12345 - fl. 3') == []
    assert E.oabs('OAB do Estado de SP, protocolo 998877') == []


def test_html_no_meio_do_padrao_quebraria_o_casamento():
    """As publicações do DJEN vêm em tabela HTML. Quando a tag cai DENTRO do
    padrão, a regex não casa — por isso `extrair` limpa antes.

    (No trecho `PJE` a tag fica fora do padrão e casaria de qualquer jeito;
    este teste usa o caso que realmente exercita a limpeza.)
    """
    com_tag = 'ADV: <span>FULANO</span> (OAB <i>SP123456</i>)'
    assert E.oabs(com_tag) == []                   # cru: não acha
    assert E.extrair(com_tag)['oabs'] == ['SP123456']
    assert E.extrair(PJE)['oabs'] == ['PR116436', 'TO011228']


def test_cpf_e_cnpj_so_com_dv_valido():
    """Num texto de intimação há guia, protocolo, conta — sequências de 11 e 14
    dígitos que NÃO são documento. O DV é o porteiro."""
    bom = 'CPF 038.499.054-11 e CNPJ 29.979.036/0001-40'
    ruim = 'guia 038.499.054-99 e protocolo 29.979.036/0001-99'
    assert E.documentos(bom) == ['038.499.054-11', '29.979.036/0001-40']
    assert E.documentos(ruim) == []


def test_cnj_citado_so_com_dv_valido():
    texto = ('nos autos 5229078-89.2022.8.13.0024, em apenso ao '
             '1234567-00.2020.8.26.0100 (número quebrado)')
    achados = E.cnjs(texto)
    assert '5229078-89.2022.8.13.0024' in achados
    assert '1234567-00.2020.8.26.0100' not in achados


def test_valores_do_maior_pro_menor():
    assert E.valores('R$ 1.234,56 e R$ 98.765,43 e R$ 10,00') == [98765.43, 1234.56, 10.0]


def test_sem_entidade_devolve_dict_vazio():
    """Chave ausente ≠ chave vazia: em 1,16B docs a diferença é disco e é a
    honestidade do `exists`."""
    assert E.extrair('Intimação para audiência de conciliação.') == {}
    assert E.extrair('') == {}
    assert E.extrair(None) == {}


def test_deduplica_e_limita():
    """Pauta de audiência repete o mesmo advogado dezenas de vezes."""
    texto = ' '.join(['(OAB SP123456)'] * 50)
    assert E.oabs(texto) == ['SP123456']
    muitas = ' '.join(f'(OAB SP{100000+i})' for i in range(60))
    assert len(E.oabs(muitas)) == E.MAX_POR_CAMPO


def test_doc_builder_nao_cita_o_proprio_processo():
    """Toda publicação cita o número dela mesma. Se ele entrasse em
    `cnjs_citados`, o campo "processos citados" viraria "este processo" — e o
    uso que justifica o campo é achar o INCIDENTE VINCULADO (o cumprimento que
    virou precatório, o agravo, o apenso).

    Pego na validação contra dados reais: 13,5% das publicações traziam só o
    próprio CNJ.
    """
    from search.documents import _entidades_do_texto
    proprio = '5229078-89.2022.8.13.0024'
    outro = '1070146-16.2025.8.26.0053'
    ent = _entidades_do_texto(f'Nos autos {proprio}, em apenso a {outro}.', proprio)
    assert ent['cnjs_citados'] == [outro]

    so_proprio = _entidades_do_texto(f'Intimação nos autos {proprio}.', proprio)
    assert 'cnjs_citados' not in so_proprio


def test_nome_do_advogado_sai_junto_com_a_oab():
    """Busca por NOME de advogado tem 18,6% de cobertura (vem do enricher); o
    texto da publicação alcança 42,2% dos processos. O nome vem colado na OAB.
    """
    assert E.advogados(E.limpar(PJE)) == ['PALOMA MACIEL PANIZZI',
                                          'JAILSON NERES FERREIRA']
    assert E.advogados(ESAJ) == ['FULANO DE TAL', 'BELTRANO DA SILVA']
    assert E.advogados(COLON) == ['RENATA GUARANA', 'RICARDO ULLMANN']


def test_nome_nao_engole_a_frase_em_volta():
    """Sem delimitador antes do nome, o recorte tem que andar de trás pra frente
    aceitando só peça de nome — senão vem 'Intimação do advogado SICRANO'.
    """
    assert E.advogados('Intimação do advogado SICRANO PEREIRA '
                       '(OAB/MG 45678) para manifestar-se') == ['SICRANO PEREIRA']
    # `BARRA` tem só um nome — e nome sem sobrenome é recusado de propósito
    assert E.advogados(BARRA) == []
    assert E.advogados('fica intimada a parte por seu patrono '
                       'Joao Alves de Souza (OAB SP111222)') == ['Joao Alves de Souza']


def test_nome_precisa_de_sobrenome():
    """Um token só quase sempre é rótulo mal recortado, não advogado."""
    assert E.advogados('DR (OAB SP123456)') == []
    assert E.advogados('ADVOGADO (OAB SP123456)') == []


def test_sem_oab_nao_ha_advogado():
    """A OAB é a âncora. Sem ela, qualquer nome em caixa alta da publicação
    (parte, juiz, vara) viraria advogado."""
    assert E.advogados('REQUERENTE: MARIA DA SILVA SANTOS') == []
