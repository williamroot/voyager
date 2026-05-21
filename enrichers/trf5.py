"""Enricher do TRF5 via PJe consulta pública (sem login).

Endpoint: https://pje1g.trf5.jus.br/pjeconsulta/ConsultaPublica/...

Abrangência: AL, CE, PB, PE, RN, SE.

O path prefix difere do TRF1 (/consultapublica/) e do TRF3 (/pje/):
no TRF5 é /pjeconsulta/. O form e o parsing do detalhe são idênticos
por serem PJe padrão CNJ — usa a mesma BasePjeEnricher.

⚠️ TRF5 fica atrás de Akamai Bot Manager. O UA identificador
`voyager-ops/0.1` retorna challenge-only sem o form. UA Firefox
vanilla passa pelo gate (Akamai injeta JS de telemetria mas serve
o conteúdo real). Por isso sobrescrevemos USER_AGENT só aqui.
"""
from .pje import BasePjeEnricher


class Trf5Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g.trf5.jus.br'
    LIST_URL = f'{BASE_URL}/pjeconsulta/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pjeconsulta/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF5'
    LOG_NAME = 'voyager.enrichers.trf5'
    USER_AGENT = 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0'
