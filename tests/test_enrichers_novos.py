"""Config + wiring dos enrichers adicionados no recon 2026-06-29.

Cobre os tribunais VIÁVEIS (consulta pública aberta, sem captcha/login):
PJe clássico (TJCE, TJAP, TJPE, TJRJ, TJRO), e-SAJ (TJAC) — subclasses de base —
e os REST próprios (TJMT, TJPA). Os testes de parsing live de cada REST ficam em
test_enricher_tjmt.py / test_enricher_tjpa.py.
"""
import pytest

from enrichers.pje import BasePjeEnricher
from enrichers.esaj import BaseEsajEnricher
from enrichers.tjce import TjceEnricher
from enrichers.tjap import TjapEnricher
from enrichers.tjpe import TjpeEnricher
from enrichers.tjrj import TjrjEnricher
from enrichers.tjro import TjroEnricher
from enrichers.esaj import TjacEnricher


PJE_SUBCLASSES = {
    'TJCE': (TjceEnricher, 'https://pje-consulta.tjce.jus.br',
             'https://pje-consulta.tjce.jus.br/pje1grau/ConsultaPublica/listView.seam',
             '/pje1grau/ConsultaPublica/DetalheProcessoConsultaPublica'),
    'TJAP': (TjapEnricher, 'https://pje.tjap.jus.br',
             'https://pje.tjap.jus.br/1g/ConsultaPublica/listView.seam',
             '/1g/ConsultaPublica/DetalheProcessoConsultaPublica'),
    'TJPE': (TjpeEnricher, 'https://pje.cloud.tjpe.jus.br',
             'https://pje.cloud.tjpe.jus.br/1g/ConsultaPublica/listView.seam',
             '/1g/ConsultaPublica/DetalheProcessoConsultaPublica'),
    'TJRJ': (TjrjEnricher, 'https://tjrj.pje.jus.br',
             'https://tjrj.pje.jus.br/pje/ConsultaPublica/listView.seam',
             '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'),
    'TJRO': (TjroEnricher, 'https://pjepg-consulta.tjro.jus.br',
             'https://pjepg-consulta.tjro.jus.br/consulta/ConsultaPublica/listView.seam',
             '/consulta/ConsultaPublica/DetalheProcessoConsultaPublica'),
}


@pytest.mark.parametrize('sigla,cfg', PJE_SUBCLASSES.items())
def test_config_pje_subclasses(sigla, cfg):
    cls, base, list_url, detalhe = cfg
    assert issubclass(cls, BasePjeEnricher)
    assert cls.TRIBUNAL_SIGLA == sigla
    assert cls.BASE_URL == base
    assert cls.LIST_URL == list_url
    assert cls.DETALHE_PATH == detalhe
    assert cls.LOG_NAME == f'voyager.enrichers.{sigla.lower()}'


def test_config_tjac_esaj():
    assert issubclass(TjacEnricher, BaseEsajEnricher)
    assert TjacEnricher.TRIBUNAL_SIGLA == 'TJAC'
    assert TjacEnricher.BASE_URL == 'https://esaj.tjac.jus.br'
    assert TjacEnricher.CPOSG_PATH == 'cposg5'
    assert TjacEnricher.LOG_NAME == 'voyager.enrichers.tjac'


# --------------------------- Wiring (registry/filas/auto-enqueue) ---------------------------

NOVOS = ['TJCE', 'TJAP', 'TJPE', 'TJRJ', 'TJRO', 'TJAC', 'TJMT', 'TJPA']


@pytest.mark.parametrize('sigla', NOVOS)
def test_registry(sigla):
    from enrichers.jobs import _ENRICHERS, queue_for
    assert sigla in _ENRICHERS
    assert queue_for(sigla) == f'enrich_{sigla.lower()}'


@pytest.mark.parametrize('sigla', NOVOS)
def test_fila_rq(sigla):
    from django.conf import settings
    assert f'enrich_{sigla.lower()}' in settings.RQ_QUEUES


@pytest.mark.parametrize('sigla', NOVOS)
def test_auto_enqueue(sigla):
    from djen.ingestion import TRIBUNAIS_COM_ENRICHER
    assert sigla in TRIBUNAIS_COM_ENRICHER
