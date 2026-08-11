"""Polo do e-SAJ: o feminino trocava a vogal final e caía em 'outros'.

Medido no banco de produção em 2026-08-10: 33.338 partes de TJAL, TJSP e TJAC
classificadas como 'outros' só por causa do gênero do papel —

    RÉ 19.852 · AGRAVADA 4.178 · EXECUTADA 2.821 · REQUERIDA 2.654
    RECLAMADA 345 · EMBARGADA 228 · IMPETRADA 152 · RECORRIDA 15 · APELADA 1

O e-SAJ é o ÚNICO enricher que infere polo do TEXTO do papel; PJe, TJPA, TJDFT
e TJMT leem campo estruturado e não têm o problema.
"""
import pytest

from enrichers.esaj import BaseEsajEnricher


@pytest.fixture
def polo():
    return BaseEsajEnricher._polo_para_tipo.__get__(
        object.__new__(BaseEsajEnricher))


PASSIVO_FEMININO = ['RÉ', 'Requerida', 'EXECUTADA', 'Agravada', 'RECLAMADA',
                    'Embargada', 'IMPETRADA', 'Recorrida', 'APELADA']
PASSIVO_MASCULINO = ['Réu', 'REU', 'Requerido', 'Executado', 'Agravado',
                     'Reclamado', 'Embargado', 'Impetrado', 'Recorrido',
                     'Apelado', 'Reqdo', 'Exectdo', 'Apdo', 'Agvdo']
ATIVO = ['Autor', 'Autora', 'Requerente', 'Exequente', 'Apelante', 'Agravante',
         'Reclamante', 'Recorrente', 'Embargante', 'Impetrante', 'Reqte',
         'Exeqte', 'Recte']


@pytest.mark.parametrize('papel', PASSIVO_FEMININO)
def test_feminino_do_passivo(polo, papel):
    assert polo(papel) == 'passivo', papel


@pytest.mark.parametrize('papel', PASSIVO_MASCULINO)
def test_masculino_do_passivo_continua(polo, papel):
    assert polo(papel) == 'passivo', papel


@pytest.mark.parametrize('papel', ATIVO)
def test_ativo_nao_foi_contaminado(polo, papel):
    """A armadilha: 'ré'/'re' como PREFIXO engoliria 'requerente',
    'reclamante' e 'recorrente' — o polo ativo inteiro viraria passivo."""
    assert polo(papel) == 'ativo', papel


@pytest.mark.parametrize('papel', ['Advogado', 'Advogada', 'Perito',
                                   'Fiscal da Lei', '', None])
def test_quem_nao_e_polo_fica_em_outros(polo, papel):
    assert polo(papel) == 'outros'
