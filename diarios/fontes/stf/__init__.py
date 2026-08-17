"""DJe do STF — a única porta do Supremo, porque ele nunca entrou no DJEN.

Medido em 16/08/2026: `siglaTribunal=STF` no DJEN devolve o mesmo HTTP 500
("O sistema está muito ocupado") que uma sigla inventada (`ZZZ`), enquanto
TRF1/STJ/TST devolvem 200 — ou seja, o 500 aqui não é sobrecarga, é
"tribunal não participante". O DJe em PDF do portal legado, por sua vez,
morreu entre dez/2022 e jul/2023 (a URL responde 200 com 142 bytes de
`<SCRIPT>alert("DJ Eletrônico solicitado inválido ou não disponível.")`).

Sobra a API JSON de `digital.stf.jus.br`, que é o que este pacote coleta.

O STJ **não** mora aqui: aderiu ao DJEN em 29/11/2024 e o payload dele bate
100% com `djen/parser.py`. Ligar o STJ é migration + comando, não coletor —
ver `tribunals/migrations/0049_stj_horizonte_djen.py` e
`djen/management/commands/djen_ligar_stj.py`.
"""
