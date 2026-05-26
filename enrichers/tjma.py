"""Enricher do TJMA via PJe consulta pública (sem login).

Endpoint: https://pje.tjma.jus.br/pje/ConsultaPublica/...

Mesmo template do TJMG (path `/pje/...`), só muda o domínio. Form fPP,
script de pesquisa (capturado via fallback `A4J.AJAX.Submit` — TJMA usa
`executarPesquisa` em vez de `executarPesquisaReCaptcha`) e parsing do
detalhe são idênticos por ser PJe padrão CNJ.

CPF/CNPJ vêm sem máscara (igual TRF1/TJMG, diferente do TRF3). Sem WAF
identificado — UA default `voyager-ops/0.1` passa. Sem 2º grau público
separado: domínio `pje-2g.tjma.jus.br` não resolve, igual ao TJMG.
"""
from .pje import BasePjeEnricher


class TjmaEnricher(BasePjeEnricher):
    BASE_URL = 'https://pje.tjma.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJMA'
    LOG_NAME = 'voyager.enrichers.tjma'
