"""Enricher do TJMA via PJe consulta pública (sem login).

Endpoints:
  1º grau: https://pje.tjma.jus.br/pje/ConsultaPublica/...
  2º grau: https://pje2.tjma.jus.br/pje2g/ConsultaPublica/...

Mesmo template do TJMG (path `/pje/...`), só muda o domínio. Form fPP,
script de pesquisa (capturado via fallback `A4J.AJAX.Submit` — TJMA usa
`executarPesquisa` em vez de `executarPesquisaReCaptcha`) e parsing do
detalhe são idênticos por ser PJe padrão CNJ.

CPF/CNPJ vêm sem máscara (igual TRF1/TJMG, diferente do TRF3). Sem WAF
identificado — UA default `voyager-ops/0.1` passa.

2º grau: ao contrário do que a versão anterior deste arquivo afirmava, o
TJMA TEM consulta pública de 2º grau — ela vive em `pje2.tjma.jus.br/pje2g`
(não no inexistente `pje-2g.tjma.jus.br`). É PJe clássico (JSF/Seam), aberto
sem login, com o MESMO HTML do 1º grau — só muda host/path. A base roteia
por grau (foro de origem `OOOO == '0000'` ⇒ 2g), idêntico ao e-SAJ; aqui
basta declarar as URLs `*_2G`.
"""
from .pje import BasePjeEnricher


class TjmaEnricher(BasePjeEnricher):
    BASE_URL = 'https://pje.tjma.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    BASE_URL_2G = 'https://pje2.tjma.jus.br'
    LIST_URL_2G = f'{BASE_URL_2G}/pje2g/ConsultaPublica/listView.seam'
    DETALHE_PATH_2G = '/pje2g/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJMA'
    LOG_NAME = 'voyager.enrichers.tjma'
