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

    # --- O TJMG roda TRÊS sistemas, e o PJe só sabe de um -------------------
    #
    # Medido em 29/08/2026, amostra de 3,21 M publicações por página aleatória.
    # Dos `link` do TJMG com host (11,3% das publicações):
    #   pje.tjmg.jus.br 48,8%  ·  www4.tjmg.jus.br 35,9%  ·  eproc1g/2g 12,5%
    #
    # O eproc não é o acervo velho, é o novo, e ele carrega prefixo próprio de
    # sequencial no CNJ. Cruzando host x prefixo x ano (mesma amostra):
    #   prefixo 1 + 2026 -> eproc 92% (resto www4)   |  prefixo 5 -> pje 83-95%
    #   prefixo 1 + 2025 -> eproc 76% (resto www4)
    # ou seja, prefixo 1 de 2025/2026 é 100% NÃO-PJe.
    #
    # Estado no banco (amostra de 10,36 M processos): prefixo 1 + ano >= 2025 =
    # **13,6% do TJMG ≈ 1,13 M processos**, com **0,00% de `ok`** (0 de 113.361)
    # contra 43-51% do prefixo 5.
    #
    # Sonda ao vivo no próprio PJe consulta pública:
    #   prefixo 1, 2025-2026  -> **16 de 16 "não existe"**
    #   CONTROLE prefixo 5, mesma janela -> **15 de 16 achou**
    #   CONTROLE prefixo 1, 2015-2021    -> **7 de 16 achou**
    # O terceiro é o que impede a generalização: cortar por prefixo sozinho
    # apagaria quase metade de uma faixa que o PJe SERVE.
    #
    # Fora do corte de propósito: prefixo 1 de 2022-2024 também dá 16 de 16
    # "não existe", mas ali a amostra é toda de foro `0000` (2º grau, que este
    # enricher não cobre — o TJMG tem `pjerecursal.tjmg.jus.br` sem
    # configuração aqui) e o estoque já está 99% consumido como
    # `nao_encontrado`: recusar não pouparia requisição. Fenômeno diferente,
    # tratamento diferente.
    FORA_DA_FONTE_FAIXAS = (('1', 2025, 'eproc'),)
