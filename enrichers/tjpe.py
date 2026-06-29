"""Enricher do TJPE via PJe consulta pública (sem login).

Host: pje.cloud.tjpe.jus.br, path do 1º grau `/1g/`. PJe clássico, sem captcha.
"""
from .pje import BasePjeEnricher


class TjpeEnricher(BasePjeEnricher):
    BASE_URL = 'https://pje.cloud.tjpe.jus.br'
    LIST_URL = f'{BASE_URL}/1g/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/1g/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJPE'
    LOG_NAME = 'voyager.enrichers.tjpe'
