"""Gate "há devedor público no polo passivo?" (`tribunals.estagio.RE_ENTE_PUBLICO`).

Nomes tirados da amostra de 0,2% de `tribunals_process` (16/10/2026): entes e
razões sociais aparecem com o nome real (são pessoas jurídicas, públicas no
cadastro); nenhuma pessoa física.
"""
import pytest

from search.management.commands.recalcular_ente_publico_es import flag_novo
from tribunals.estagio import RE_ENTE_PUBLICO, RE_ENTE_PUBLICO_FEATURE_V1


@pytest.mark.parametrize('nome', [
    # o defeito do "estado d": falso-negativo nos Estados
    'ESTADO DO RIO GRANDE DO SUL', 'Estado de São Paulo', 'ESTADO DE MATO GROSSO',
    'ESTADO DE MINAS GERAIS (REQUERIDO(A))', 'ESTADO DO MARANHAO', 'Estado do Pará',
    'ESTADO DO RS', 'PROCURADORIA GERAL DO ESTADO DO CEARÁ',
    'COMANDANTE DA POLÍCIA MILITAR DO ESTADO DO MARANHÃO',
    # previdência própria e autarquias estaduais
    'INSTITUTO DE PREVIDÊNCIA DO ESTADO DO RIO GRANDE DO SUL - IPE -PREV', 'IPERGS',
    'IPE SAÚDE', 'INSTITUTO DE PREVIDENCIA DOS SERVIDORES DO ESTADO DO MARANHAO - IPREV',
    'IPREVSANTOS', 'DETRAN/PA', 'DEPARTAMENTO AUTONOMO DE ESTRADAS DE RODAGEM - DAER',
    # o que já casava continua casando
    'MUNICÍPIO DE PORTO ALEGRE', 'MUNICIPIO DE SENADOR SA', 'UNIÃO FEDERAL',
    'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS', 'Fazenda Pública do Estado de São Paulo',
    'DISTRITO FEDERAL', 'PREFEITURA MUNICIPAL DE CANOAS',
    'EMPRESA PÚBLICA DE TRANSPORTES E CIRCULAÇÃO DO MUNICÍPIO DE PORTO ALEGRE - EPTC / RS',
])
def test_e_devedor_publico(nome):
    assert RE_ENTE_PUBLICO.search(nome), nome


@pytest.mark.parametrize('nome', [
    # "estado" que não é o Estado
    'ESTADO CIVIL', 'ESTADO DE ALERTA PRODUCOES', 'MARIA DO ESTADO',
    # razão social com "Estado"/"União"/"Fazenda" no nome
    'BANCO DO ESTADO DO RIO GRANDE DO SUL SA',
    'CIA DE SANEAMENTO BÁSICO DO ESTADO DE SÃO PAULO - SABESP',
    'ASSOCIACAO DOS SERVIDORES DO CORPO DE BOMBEIROS E POLICIA MILITAR DO ESTADO DE MINAS GERAIS',
    'SERVICO SOCIAL DO COMERCIO - SESC - ADMINISTRACAO REGIONAL NO ESTADO DE SAO PAULO',
    'EXPRESSO UNIAO LTDA', 'FAZENDA BODOQUENA SA', 'FAZENDA AMONTADA S A',
    'UNIAO SEGURADORA S.A. - VIDA E PREVIDENCIA', 'Banco do Brasil S/A - Agência União dos Palmares',
    'COOPERATIVA DE CREDITO DE LIVRE ADMISSAO UNIAO E NEGOCIOS - SICOOB INTEGRACAO',
    'AAPB-ASSOCIACAO DOS APOSENTADOS E PENSIONISTAS BRASILEIROS DO INSS E FUNDOS DE PENSAO',
    'DF TRANSPORTES E LOGISTICA EIRELI - ME', 'COMPANHIA DE SANEAMENTO AMBIENTAL DO DISTRITO FEDERAL',
    'IPE COUNTRY CLUBE',
    # órgão público que não é DEVEDOR: está no passivo como intimado
    'Ministério Público do Estado de São Paulo', 'DEFENSORIA PÚBLICA DA UNIÃO',
    'Defensoria Pública da União no Estado de São Paulo', 'MINISTERIO PUBLICO DA UNIAO',
    'PROCURADORIA GERAL DE JUSTICA DO ESTADO DE MATO GROSSO',
    # pessoa física
    'Daniel Gerber', '',
])
def test_nao_e_devedor_publico(nome):
    assert not RE_ENTE_PUBLICO.search(nome), nome


def test_feature_do_modelo_continua_congelada():
    """O Estágio do Crédito foi treinado com a regex antiga, defeito incluído."""
    assert not RE_ENTE_PUBLICO_FEATURE_V1.search('ESTADO DO RIO GRANDE DO SUL')
    assert RE_ENTE_PUBLICO_FEATURE_V1.search('EXPRESSO UNIAO LTDA')


def test_flag_do_doc_so_olha_o_passivo():
    assert flag_novo([{'nome': 'MARIA', 'polo': 'ativo'},
                      {'nome': 'ESTADO DO RIO GRANDE DO SUL', 'polo': 'passivo'}])
    assert not flag_novo([{'nome': 'ESTADO DO RIO GRANDE DO SUL', 'polo': 'ativo'}])
    assert not flag_novo(None)
