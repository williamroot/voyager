# Módulo Comercial — Mapa de Precatórios do Brasil

**Objetivo.** Dar ao comercial/executivo um **mapa do Brasil** (choropleth) que mostra,
com base no que já processamos, **onde estão os precatórios** — por estado e por tribunal —
com filtros dinâmicos, pra decidir **quais estados atacar primeiro**. Não é um relatório
estático: é uma ferramenta de prospecção.

**Princípio de honestidade.** O mapa reflete **o que processamos**, não a verdade do Brasil.
A cobertura varia por estado. Por isso o mapa mostra SEMPRE 3 números juntos: **potencial**
(sinal DJEN, toda a base), **confirmado** (classificação ML, limitado pela cobertura) e
**% validado** (quanto já enriquecemos). O "não sabemos ainda" é explícito, nunca mascarado.

## Arquitetura (decidida)
```
Postgres (fonte da verdade)  ──write-through (signals)──▶  Elasticsearch (VM .128 dedicada)
                                                                     │
                                          leituras/agregações do MAPA (terms por uf/tribunal,
                                          filtros, sub-aggs de valor) — NUNCA o Postgres contido
```
- **Toda leitura do mapa = ES.** Zero query pesada no Postgres de produção (é o gargalo; já
  causou 502 — ver OPS.md).
- Campos no doc ES `voyager-processos` (71M): `tribunal, uf, classificacao,
  tem_sinal_precatorio, valor_causa, ano_cnj, codigo_classe` — todos keyword/numérico
  agregáveis. `uf` e `tem_sinal_precatorio` adicionados na Fase A (commit f413ef2).

## Definições de métrica
- **Potencial de precatório** = `tem_sinal_precatorio=true` (sinal do texto DJEN; amplo, toda
  a base; tem falso-positivo tipo "carta precatória" → é POTENCIAL, não confirmado).
- **Precatório confirmado** = `classificacao='PRECATORIO'` (ML; preciso, mas só onde enriquecemos).
- **% validado** (cobertura) = enriquecidos ÷ total do tribunal (datajud/enricher done).
- **Densidade** = potencial ÷ total do tribunal.
- **Score de foco** = `densidade × valor_relativo × (1 − cobertura)` → prioriza estado
  precatório-rico, com dinheiro, que ainda mal tocamos. É o ranking-rei do exec.
- **Federal (TRF*, TST, STJ…)** = camada `uf='FED'` separada (processo federal é multi-UF).
- Toggle do mapa: **Volume ↔ R$** (constrói os dois).

---

## O GENERAL (orquestrador — responsável por levar até o fim)
Papel: **eu (loop principal) atuo como general.** Não delego a responsabilidade de terminar.

Responsabilidades:
1. **Sequenciar** as fases respeitando dependências (B→C→D em paralelo onde der; E no fim).
2. **Despachar** cada agente especialista com escopo fechado + contrato de I/O + o gate dele.
3. **Portão (gate) por agente**: só marca a fase como concluída quando as validações passam.
   Se o entregável falha o gate, **re-despacha o MESMO agente com o feedback do que faltou**
   (não segue em frente com dívida).
4. **Integrar**: juntar backend+frontend, rodar o teste end-to-end, resolver conflitos.
5. **Ledger de status** (mantido neste doc, seção "Status de execução"): fase, agente,
   estado (pendente/rodando/gate-falhou/concluído), o que falta.
6. **Guardrails invioláveis**: (a) nenhuma query pesada no Postgres de prod; (b) toda migration
   `atomic=False` conferida com `showmigrations [X]`; (c) checar o site (302) após todo
   force-recreate web; (d) números do mapa reconciliados com o Postgres antes de "pronto".
7. **Critério de PRONTO** (não para antes): todos os gates verdes + aceite end-to-end + doc
   atualizada + deployado + validado em prod + o QA adversarial não achou o mapa mentindo.

Loop do general (por fase): `despacha → coleta → roda gate → (falhou? re-despacha com feedback)
→ integra → próxima`. Entre fases, atualiza o ledger e reporta ao usuário nos marcos.

---

## AGENTES ESPECIALISTAS

### Agente 1 — ES-DATA (fundação de dados)  [Fase B]
**Missão:** deixar o índice `voyager-processos` pronto pra agregação do mapa.
**Entregáveis:**
- `PUT _mapping` nos índices adicionando `uf` (keyword) e `tem_sinal_precatorio` (boolean).
- Popular `uf` em todos os 71M docs via **`_update_by_query` com script painless** que deriva
  do `tribunal` JÁ presente no ES (o mapa `search/geo.py::UF_DO_TRIBUNAL`) — **sem tocar o Postgres**.
- Sincronizar `tem_sinal_precatorio` do Postgres pro ES (batched, DEPOIS do backfill Fase 0
  fechar; usa o write-through/reindex existente, throttlado).
- Garantir que o write-through (`search/signals.py`) passa a incluir os 2 campos p/ docs novos.
**Validações / GATE:**
- `_cat/indices` ok; `_mapping` mostra os 2 campos.
- Agregação `terms by uf` retorna 27 UFs + FED, soma dos counts == total de docs (±0).
- `count(uf=SP)` no ES bate com `count(tribunal LIKE 'TJSP')` no Postgres (±0,5%).
- Nenhum pico no Postgres durante o update_by_query (é ES-only) — confirmar via SHOW POOLS.
**Deps:** Fase A (feito). Bloqueia Agentes 2 e 4.

### Agente 2 — BACKEND (agregação + score)  [Fase C]
**Missão:** serviço + endpoints que servem o mapa a partir do ES.
**Entregáveis:**
- `search/agg_comercial.py`: funções de agregação ES parametrizadas por filtros
  (uf, tribunal, natureza?, faixa de valor, ano, classificacao, tem_sinal).
- Endpoints REST (`dashboard/` ou `search/`): `/api/comercial/mapa` (agregado por UF),
  `/api/comercial/tribunais?uf=` (drill-down), `/api/comercial/top?metric=` (ranking),
  todos com querystring de filtros. Cache warm (5min) pros agregados globais.
- Cálculo do **score de foco** (densidade × valor × (1−cobertura)) por UF/tribunal.
- Contrato JSON versionado (documentado no topo do arquivo) — o Agente 3 consome isso.
**Validações / GATE:**
- Cada endpoint responde <300ms (ES é rápido; medir).
- Números reconciliam com o Agente 1 (mesma agregação) e com um spot-check no Postgres.
- Filtros combinados funcionam (ex.: uf=SP & classificacao=PRECATORIO & ano>=2020).
- Testes unitários do agg + score (fixtures ES mock ou índice de teste).
- Auth: só usuário logado (é dado sensível de negócio).
**Deps:** Agente 1. Bloqueia Agente 3 (contrato).

### Agente 3 — FRONTEND (o mapa)  [Fase D]
**Missão:** a página do mapa, navegável e bonita, consumindo o backend.
**Entregáveis:**
- Página `/dashboard/comercial/mapa` (segue o tema/base do dashboard).
- **Choropleth do Brasil** (topojson/SVG inline — sem CDN externo por CSP), cor por métrica
  com **toggle Volume ↔ R$** e **toggle Potencial ↔ Confirmado**.
- **Drill-down**: clica no estado → painel com ranking de tribunais → top processos.
- **Filtros dinâmicos** (natureza, faixa de valor, estágio, ano) → re-query no backend.
- **Ranking Top-N** lateral + o **score de foco** destacado ("ataque primeiro").
- Legenda honesta: mostra % validado por estado (hachura/opacidade pra "pouco coberto").
**Validações / GATE:**
- Renderiza sem erro de JS; mapa interativo; hover/click funcionam.
- Números na tela == resposta do backend (spot-check).
- Sem horizontal scroll; responsivo; foco de teclado; sem quebra de CSP (tudo inline).
- Rótulos honestos (Potencial vs Confirmado nunca confundidos).
**Deps:** Agente 2 (contrato/endpoints).

### Agente 4 — MÉTRICAS / QA ADVERSARIAL  [Fase E]
**Missão:** provar que o mapa NÃO mente antes de liberar.
**Entregáveis:**
- Reconciliação: pra 5 UFs + FED, comparar potencial/confirmado/valor do ES vs Postgres vs
  Falcon (onde houver) e reportar divergências.
- Auditoria de honestidade: estados de baixa cobertura estão sinalizados? federal separado?
  falso-positivo do sinal comunicado? valor R$ vem de fonte confiável (Falcon/enriquecido)?
- Testa filtros extremos (UF sem dado, valor negativo, ano futuro) — degrada limpo.
- Veredito PASS/BLOCK com lista do que corrigir.
**Validações / GATE:** o próprio agente É o gate final; BLOCK volta pro general re-despachar.
**Deps:** Agentes 2 e 3 integrados.

---

## Sequência + dependências
```
A(feito) → B(ES-DATA) → ┬→ C(BACKEND) → D(FRONTEND) →┐
                        └→ (uf pronto libera aggs)     ├→ E(QA) → PRONTO
                          (sync do sinal: pós-backfill)┘
```
Paralelismo real: assim que o CONTRATO do Agente 2 está definido, o Agente 3 já pode começar
a casca do mapa (topojson, layout) com dados mock; integra quando os endpoints sobem.

## Critérios de ACEITE do módulo (o general só declara PRONTO com tudo isto)
1. `/dashboard/comercial/mapa` no ar, logado, choropleth do Brasil interativo.
2. Toggle Volume↔R$ e Potencial↔Confirmado funcionam; drill-down estado→tribunal→processos.
3. Filtros dinâmicos servidos pelo ES, <300ms.
4. Números reconciliados com o Postgres (±0,5%) e QA adversarial = PASS.
5. Honestidade: % validado visível; federal separado; potencial≠confirmado.
6. Zero carga pesada no Postgres de prod (tudo ES). Site segue 302.
7. Doc `.ia/COMERCIAL_MAPA.md` + `DASHBOARD.md`/`API.md` atualizados; tudo na main.

## Riscos + guardrails
- **Postgres contido**: leituras do mapa NUNCA no PG; o `update_by_query` do `uf` é ES-only;
  o sync do sinal é batched/throttlado e SÓ após o backfill Fase 0 fechar.
- **Honestidade dos números**: a cobertura baixa não pode fazer estado parecer "vazio" — o
  Agente 4 gateia isso.
- **CSP**: mapa 100% inline (sem CDN de topojson/JS).
- **Lição atomic=False** (incidente 10/08): qualquer migration nova conferir `showmigrations [X]`.

## Status de execução (o general mantém aqui)
- **Fase A** ✅ concluída (uf + tem_sinal_precatorio no doc/mapping; commit f413ef2).
- **Fase B** ⏳ próxima (ES-DATA).
- **Fases C/D/E** ⏳ pendentes.
- **Dep externa**: backfill Fase 0 rodando (alimenta `tem_sinal_precatorio`); sync do sinal
  pro ES espera ele fechar (~horas). O mapa já pode subir com `uf` + `classificacao` +
  `valor_causa` e ligar o `potencial` quando o sinal sincronizar.
