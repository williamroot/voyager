# Fixtures dos diários próprios — o que commitar e o que não

Tudo aqui é **resposta real** capturada das fontes em 16/08/2026. Nada foi
fabricado: é o que o `dje.tjsp.jus.br`, o `dejt.jt.jus.br`, o
`digital.stf.jus.br`, o `api.queridodiario.ok.org.br` e o
`do-api-web-search.doe.sp.gov.br` devolveram, incluindo as **cascas** — os
"HTTP 200 que não são dado", que são metade do valor deste diretório.

## O problema: 88 MB, e a maior parte não deveria entrar no git

```
dejt/       66 MB   (trt3_2024-07-10_jud.pdf sozinho tem 62,7 MB)
tjsp_esaj/  11 MB
stj_stf/    7,8 MB
doe_entes/  4,0 MB
```

### 1. OBRIGATÓRIAS — pequenas, e a suíte não é suíte sem elas

Sem elas o teste correspondente **falha** (não pula). É deliberado: em
16/08/2026 a verificação adversarial escondeu `stf_proc_ARE1617690.html` e a
suíte respondeu `18 passed, 8 skipped` — verde, com o núcleo do resolvedor de
CNJ desligado. Skip silencioso em fixture commitável é auto-amputação da suíte,
e `tests/test_diario_stf.py::FIXTURES_OBRIGATORIAS` passou a impedir isso.

| Diretório | Arquivos |
|---|---|
| `stj_stf/` | `stf_publicacoes_2026-08-13_p1_q5.json`, `stf_publicacoes_pagina_alem_do_fim.json`, `stf_publicacoes_422_quantidade.json`, `stf_proc_ARE1617690.html`, `stf_detalhe_sem_numero_unico.html`, `stf_detalhe_incidente_inexistente.html` |
| `dejt/` | `busca_trt3_2024-07-10_J.html`, `busca_trt3_2023-08-14_J.html`, `materia_dia_TRT22_2024-07-10.html`, `trt22_2024-07-10_pag1a6.txt`, `inventario_J_2008_2026.html.gz`, `trt22_2010-03-10_jud.p7s` |
| `tjsp_esaj/` | `cabecalho_4246_c19.html`, `caderno_inexistente_erro.html`, `index.html`, `caderno12_20150715_p96-125.pdf` (30 páginas, recorte) |
| `doe_entes/` | `qd_busca_dia_2026-04-30.json`, `qd_dia_vazio_2026-08-16.json`, `qd_gazeta_amostra.txt`, `qd_spa_casca.html`, `rs_spa_casca.html`, `doesp_*.json` |

### 2. PESADAS — gate de volume; o teste PULA sem elas, e isso é aceitável

São os cadernos inteiros, e é sobre eles que rodam os gates da missão (≥95% das
16.717 matérias do TRT3; cobertura de CNJ do caderno do TJSP). **Não commitar** —
juntas passam de 75 MB. Quem quiser rodar o gate baixa de novo com os scripts de
sonda deste diretório.

```
dejt/trt3_2024-07-10_jud.pdf            62,7 MB   gate das 16.717 matérias
dejt/trt22_2024-07-10_jud.pdf            2,9 MB   gate das 885 matérias
dejt/trt3_2026-08-13_jud.pdf             0,7 MB   caderno pós-DJEN (corte de 870x)
tjsp_esaj/caderno12_20250721.pdf         4,0 MB   gate de cobertura de CNJ (2025)
tjsp_esaj/caderno3_capital_parteI_*      36 MB    canário do extrator: ≥32.000 CNJs
tjsp_esaj/caderno12_20250721.txt         3,7 MB   texto extraído do anterior
tjsp_esaj/caderno20_capital_parteII_*    ~1 MB
stj_stf/djen_stj_amostra200_*.json       2,8 MB   prova de que o STJ é DJEN puro
stj_stf/stf_publicacoes_*_q200.json      1,5 MB
stj_stf/stf_dje_41_2022-03-03.pdf        2,2 MB   DJe em PDF do portal legado (morto)
doe_entes/mg_2026-08-14_executivo.pdf    2,6 MB   MG, fonte NÃO implementada
```

Sugestão para o `.gitignore` (a decisão é de quem revisa):

```gitignore
tests/fixtures/diarios/**/*.pdf
!tests/fixtures/diarios/tjsp_esaj/caderno12_20150715_p96-125.pdf
tests/fixtures/diarios/**/caderno12_20250721.txt
tests/fixtures/diarios/stj_stf/djen_stj_amostra200_*.json
tests/fixtures/diarios/stj_stf/stf_publicacoes_*_q200.json
```

### 3. PROVENIÊNCIA — capturadas no recon, hoje sem teste que as leia

Ficam porque documentam o que foi sondado e **por que foi descartado** (o DJe em
PDF do STF morto desde 2023, o MG que deu zero no dia inteiro medido, o RS que
rende 0,18 documento/dia). Apagá-las obrigaria a re-sondar para reabrir a
discussão. Se o revisor preferir enxugar, o critério é: some tudo que não está
nas listas 1 e 2 acima.

### 4. SCRIPTS DE SONDA (`dejt/*.py`)

`baixar.py`, `dejt_probe.py`, `analisa_pdf.py`, `materia.py`, `tamanho_dia.py`,
`tamanho_trib.py` — foram como as fixtures foram capturadas. **Não são testes** e
o pytest não os coleta (não começam com `test_`). Servem para recapturar as
fixtures pesadas da lista 2.
