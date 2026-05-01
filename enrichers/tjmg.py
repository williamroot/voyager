"""Enricher do TJMG via PJe consulta pública (sem login).

Endpoint: https://pje-consulta-publica.tjmg.jus.br/pje/ConsultaPublica/...

Mesmo template do TRF3 (path `/pje/...`), só muda o domínio. Form e
parsing do detalhe são idênticos por ser PJe padrão CNJ.
"""
from .pje import BasePjeEnricher


class TjmgEnricher(BasePjeEnricher):
    BASE_URL = 'https://pje-consulta-publica.tjmg.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJMG'
    LOG_NAME = 'voyager.enrichers.tjmg'
