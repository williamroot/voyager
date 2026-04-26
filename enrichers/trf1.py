"""Enricher do TRF1 via PJe consulta pública (sem login).

Endpoint: https://pje1g-consultapublica.trf1.jus.br/consultapublica/...
"""
from .pje import BasePjeEnricher


class Trf1Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g-consultapublica.trf1.jus.br'
    LIST_URL = f'{BASE_URL}/consultapublica/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF1'
    LOG_NAME = 'voyager.enrichers.trf1'
