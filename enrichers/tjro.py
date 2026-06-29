"""Enricher do TJRO via PJe consulta pública (sem login).

Host: pjepg-consulta.tjro.jus.br, path `/consulta/`. PJe clássico, sem captcha.
"""
from .pje import BasePjeEnricher


class TjroEnricher(BasePjeEnricher):
    BASE_URL = 'https://pjepg-consulta.tjro.jus.br'
    LIST_URL = f'{BASE_URL}/consulta/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/consulta/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJRO'
    LOG_NAME = 'voyager.enrichers.tjro'
