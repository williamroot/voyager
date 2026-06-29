"""Enricher do TJCE via PJe consulta pública (sem login).

Host: pje-consulta.tjce.jus.br (pje.tjce.jus.br e /pje1grau redirecionam pra cá).
PJe clássico JSF/RichFaces; reCaptcha desativado no JS (`if(false)`). Path do
1º grau é `/pje1grau/`. Mesmo template do TRF3, só muda domínio/path.
"""
from .pje import BasePjeEnricher


class TjceEnricher(BasePjeEnricher):
    BASE_URL = 'https://pje-consulta.tjce.jus.br'
    LIST_URL = f'{BASE_URL}/pje1grau/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje1grau/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJCE'
    LOG_NAME = 'voyager.enrichers.tjce'
