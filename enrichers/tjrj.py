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

    # --- O TJRJ roda TRÊS sistemas, e o PJe só sabe de um -------------------
    #
    # Medido em 29/08/2026, amostra de 3,21 M publicações por página aleatória.
    # Dos `link` do TJRJ com host (60,2% das publicações):
    #   tjrj.pje.jus.br 65,4%  ·  www3/www4.tjrj.jus.br 22,1%  ·  eproc1g/2g 12,5%
    #
    # Cruzando host x prefixo x ano: **prefixo 3 é eproc em 100%** das
    # publicações de 2025 e 2026 (n=1.242) — e o prefixo 3 NÃO EXISTE no TJRJ
    # antes de 2024 (amostra de 10,36 M processos).
    #
    # Estado no banco: prefixo 3 + ano >= 2024 = **13,2% do TJRJ ≈ 809 mil
    # processos**, com **0,00% de `ok`** (0 de 80.943) contra 31-40% do
    # prefixo 0. Metade ainda está `pendente`: é requisição futura sendo
    # queimada, não só passado.
    #
    # Sonda ao vivo no próprio PJe consulta pública:
    #   prefixo 3, 2025-2026 -> **16 de 16 "não existe"**
    #   prefixo 3, 2024      -> **16 de 16 "não existe"**  (fronteira medida)
    #   CONTROLE prefixo 0, 2025-2026 -> **13 de 16 achou**
    FORA_DA_FONTE_FAIXAS = (('3', 2024, 'eproc'),)
