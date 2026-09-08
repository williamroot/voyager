"""Busca POR PARTE na consulta pública dos tribunais.

Os enrichers (`enrichers/*.py`) respondem "o que tem NESTE processo", a partir
de um número CNJ. Este pacote responde a pergunta inversa — "QUAIS processos
esta pessoa tem" — que é outro formulário em cada tribunal e nunca existiu aqui.

Divisão de trabalho, e ela é o coração do desenho:

    a busca DESCOBRE números de processo, e para aí.

Ela não abre o processo, não parseia detalhe e não grava parte nenhuma. Quem
faz isso é o enricher que já existe, já roda em produção e já tem teste — a
busca só entrega CNJs para `datajud.hidratacao.hidratar_cnj`, que cria o
`Process` e enfileira o enricher do tribunal. Sem parser novo de detalhe, sem
caminho novo de escrita, sem risco no drainer.

Módulos:
    base.py        contrato (critérios, item, página, exceções) — sem I/O
    esaj_parser.py leitura do HTML do e-SAJ — puro, testável sem rede
    esaj.py        cliente e-SAJ (TJSP, TJAL) sobre o pool de proxies
    registry.py    sigla -> buscador, e o que cada fonte aceita

A matriz do que cada fonte responde, com fixture de cada desfecho, está em
`.ia/ENRICHMENT.md` §"Busca POR PARTE na consulta pública".
"""
