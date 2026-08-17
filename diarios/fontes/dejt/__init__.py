"""DEJT — Diário Eletrônico da Justiça do Trabalho (CSJT).

Cobre TST + 24 TRTs. O que a sonda de 16/08/2026 mediu e que define tudo o que
está neste pacote:

  · A jazida é HISTÓRICA, não corrente. Em 01/08/2024 a Justiça do Trabalho
    migrou os cadernos judiciários para o DJEN (Res. CNJ 455/2022 + Ato Conjunto
    TST.CSJT.GP 77/2023). O caderno do TRT3 caiu de 69 MB para 1,5 MB de um dia
    para o outro, e as matérias do país inteiro, de 183.567/dia para 211/dia.
    Um coletor "diário" do DEJT resolveria ~0,1% do problema; o acervo
    2008-06-09 → 2024-07-31 (86.587 cadernos) é que é a fonte nova.
  · Não há API, não há GET direto para o PDF: é postback JSF 1.2 sobre
    JBoss 4.3.0.GA com conversa Seam. Ver `sessao_jsf.py`.
  · O caderno é PDF com camada de texto NATIVA (iText; 0 de 13.853 páginas sem
    texto no TRT3 de 10/07/2024) e traz o próprio índice de segmentação no
    outline. Não precisa de OCR. Ver `segmentador.py`.

Divisão dos arquivos (cada um responde por uma pergunta):
  · `sessao_jsf.py`  — como falar com um JSF de 2010 sem quebrar a conversa
  · `catalogo.py`    — que edições existem (1 requisição devolve 18 anos)
  · `segmentador.py` — como virar 13.853 páginas em 16.717 matérias
  · `coletor.py`     — a costura, no contrato de `diarios/base.py`
"""
