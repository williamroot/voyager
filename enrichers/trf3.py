"""Enricher do TRF3 via PJe consulta pública (sem login).

Endpoint: https://pje1g.trf3.jus.br/pje/ConsultaPublica/...

A diferença pro TRF1 é só o subdomínio (sem -consultapublica) e o path
prefix (/pje/ vs /consultapublica/). O form e o parsing do detalhe são
idênticos por serem PJe padrão CNJ.
"""
from .pje import BasePjeEnricher


class Trf3Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g.trf3.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF3'
    LOG_NAME = 'voyager.enrichers.trf3'
