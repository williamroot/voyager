"""Enricher do TJAP via PJe consulta pública (sem login).

Host: pje.tjap.jus.br, path do 1º grau `/1g/`. PJe clássico, sem captcha.
"""
from .pje import BasePjeEnricher


class TjapEnricher(BasePjeEnricher):
    BASE_URL = 'https://pje.tjap.jus.br'
    LIST_URL = f'{BASE_URL}/1g/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/1g/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJAP'
    LOG_NAME = 'voyager.enrichers.tjap'
