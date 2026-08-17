"""DJE do TJSP (e-SAJ) — https://dje.tjsp.jus.br/cdje/

A porta dos 17,5 anos que o DJEN não tem. Medido em 16/08/2026: o DJEN devolve
`count=0` para o TJSP em TODA data até 2025-03-13 e `count>0` a partir de
2025-03-14. São 4.162 edições no catálogo do e-SAJ, das quais 4.077 sem nenhuma
cobertura do DJEN — para o maior tribunal do país, do qual temos ~6%.

Módulos (um assunto cada, para o teste conseguir mirar):

  catalogo.py    o índice das edições (`var diarios`) e a tabela de cadernos —
                 tudo o que dá para saber SEM baixar PDF
  pdf.py         PDF → linhas com TAMANHO DE FONTE (é o tamanho que separa
                 título de seção, corpo e mobília de página)
  segmentador.py linhas → blocos de publicação (os três formatos do caderno)
  coletor.py     o `ColetorDiario`: junta os três e vira `Movimentacao`

Achado operacional que muda o planejamento (medido 16/08/2026, confirmado ao
vivo): o catálogo do e-SAJ **fechou**. A última edição é a 4247, de
2025-07-22, e `var datasSemDiario` lista TODOS os 390 dias seguintes como
"sem diário" — coerente com o TJSP ter migrado a publicação para o DJEN em
14/03/2025. Ou seja: este coletor é um backfill FINITO de 4.162 edições, não
uma ingestão corrente. Não há fronteira diária para acompanhar.
"""
