"""Enricher do TJRJ via PJe consulta pública (sem login).

Host: tjrj.pje.jus.br (instância PJe nacional), path `/pje/`. PJe clássico.
"""
from .pje import BasePjeEnricher


class TjrjEnricher(BasePjeEnricher):
    BASE_URL = 'https://tjrj.pje.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJRJ'
    LOG_NAME = 'voyager.enrichers.tjrj'
