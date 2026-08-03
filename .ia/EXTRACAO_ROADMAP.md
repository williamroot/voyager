# Roadmap da Extração — qualidade + throughput (ponto a ponto)

> Origem: teste do PDF de 1,5 GB / 9.815 páginas. Ficou claro onde ganhamos
> qualidade e onde ganhamos velocidade. Este doc é o plano faseado.
> Motor: `~/projetos/extrator-precatorio-sdk` (servido no pod QuickPod v21).

## Estado atual (o que JÁ existe — não reconstruir)

O pipeline **já** divide por documento e roda em paralelo. NÃO processa o autos
inteiro numa janela de contexto só.

```
PDF → páginas → classifica CADA página (classificador.prior_pagina/classify_pagina)
   → segmenta em DOCUMENTOS (segmentar.segmentar_paginas — detecta fronteira)
   → roteia por doc_classe (ofício→valor · procuração→partes · cessão→cedente…)
   → extrai CADA doc numa janela de MAX_CHARS_DOC=9000 chars (texto[:9000])
   → em PARALELO: asyncio.gather + Semaphore(N_PARALLEL=4) — pipeline.py:447
   → merger determinístico (resolver_entidades + montar_ficha) consolida
```

Prova: 1,5 GB → ~2.765 docs, 452 partes, **0 estouro de contexto**, ~13 min.
Logo: o "perde fragmento numa janela só" **não é o gargalo** — o autos já vira
milhares de janelas pequenas independentes.

## ★ NORTE — extração orientada a EVENTOS, com um "gestor" que roteia contexto

A visão-alvo: não extrair tudo de tudo, e sim montar a **sequência do crédito com
datas** e deixar um modelo-**gestor** decidir *o que interessa* e mandar só o
contexto certo pra extração focada. Resultado: extração **mais limpa, mais barata,
mais precisa**.

Duas camadas:

```
CAMADA 1 — universal e barata (roda em TODO doc, nada é descartado)
  classifica cada doc  +  data cada doc  →  LINHA DO TEMPO do crédito
  (DC → homologação → precatório → pagamento; ramos: impugnação, cessão, óbito)

CAMADA 2 — o GESTOR (orquestrador)
  lê o índice (classes + datas + 1ª linha de cada doc) e decide, por INTENÇÃO:
    • homologação de crédito → pega o despacho/decisão → data + valor homologado
    • impugnação            → flag + data + quem impugnou
    • pagamento             → comprovante/alvará → valor recebido + beneficiário
    • cessão                → instrumento → cedente → cessionário + %/valor
  e manda SÓ esse contexto pra extração profunda (janela do doc certo).
  → saída final = ledger limpo: timeline de eventos + ficha consolidada.
```

**Por que funciona (e o que já temos):** a Camada 1 é o que o pipeline **já faz**
(classe por doc + `estagio.marcos`/eventos com data). O gestor é uma evolução do
**roteamento por doc_classe** que já existe — hoje as regras roteiam por classe;
o gestor roteia por **intenção/evento** e é seletivo no contexto.

**Regra inegociável (abster > chutar):** o gestor **seleciona o que aprofundar,
nunca descarta silenciosamente**. A Camada 1 (classe + data) cobre 100% dos docs —
então mesmo o que o gestor não aprofunda VIRA um evento na timeline (só sem o
detalhe). Assim nunca "some" um documento; no máximo fica raso. Seleção conservadora
(na dúvida, inclui).

Este norte reorganiza as fases abaixo: P3 (fronteira) + P4 (join) alimentam a
timeline; o "gestor" é a camada nova que se apoia nelas. Ver §"Gestor" no fim.

## Onde está a perda REAL (diagnóstico honesto)

| Sintoma | Causa real | Alavanca |
|---|---|---|
| 452 partes, 0 com valor (1,5 GB) | JOIN entre docs: nome vem da procuração, valor do ofício/pagamento; merger só une por **nome exato** | P4 |
| doc denso truncado | `texto[:9000]` corta doc > 9k chars (planilha/decisão longa) | P2 |
| dois docs colados / doc cortado no meio | segmentador é heurístico (classe de página + prior) | P3 |
| lento pra volume | 1 pod, N_PARALLEL=4, --parallel 2 | P1 |

---

## P1 — Multi-GPU (round-robin de endpoints)  · throughput · esforço P

**Ganho:** velocidade ~linear no nº de GPUs. **Sem retrain, sem mudar modelo.**
Arquitetura já é embaraçosamente paralela (cada janela = chamada LLM independente).

- `extrator/llm.py`: `LlamaClient` aceita **lista** de `LLM_URL` (pool) e faz
  round-robin / least-in-flight por chamada. Env `LLM_URLS="url1,url2,..."`.
- `deploy/serve.sh`: subir 1 `llama-server` por GPU/pod; publicar as URLs.
- Balanceamento simples no cliente (contador atômico) — dispensa proxy externo.
- **Gate:** throughput/h sobe ~linear; precisão idêntica (mesmo modelo/janela).

## P2 — Sub-janelamento de docs longos (> MAX_CHARS)  · qualidade · esforço P/M

**Ganho:** tapa a ÚNICA perda por truncamento de verdade (dentro de 1 doc grande).

- `pipeline.py::_processar_doc`: se `len(texto) > max_chars`, gerar **janelas
  deslizantes com overlap** (ex.: 9000 / overlap 1500) em vez de `texto[:max_chars]`.
- Rodar a tarefa do doc em cada janela; **mesclar** os registros (dedup por span/valor).
- Aplicar só a classes onde o dado espalha: PLANILHA_CALCULO, DECISAO, SENTENCA,
  CUMPRIMENTO_SENTENCA (ofício costuma caber em 9k).
- **Gate:** em docs > 9k chars, recall de valor/partes sobe; docs curtos inalterados
  (mesma 1 janela). Custo: +chamadas só nos docs longos.

## P3 — Fronteira de documento melhor  · qualidade · esforço M

**Ganho:** menos mis-split (dois docs colados / doc partido) → cada tarefa vê o doc certo.

- `segmentar.py`: hoje decide por classe de página + `prior_pagina`. Adicionar sinal
  de **início de documento** (cabeçalho e-SAJ/PJe, "Autos nº", "Ofício nº", quebra de
  numeração de páginas, mudança de classe forte).
- Opção Tier-2 (atrás de flag): árbitro-modelo confirma fronteira em página ambígua
  (reusa `_arbitrar_classes`, já existe).
- **Gate:** κ de segmentação numa amostra rotulada; precisão de partes/valor não regride.

## P4 — JOIN valor↔parte + eventos (o buraco do 1,5 GB)  · qualidade · esforço G

**Ganho:** é AQUI que mora a qualidade que faltou. Não é split — é vínculo.

- **Ofício requisitório pareado:** no ofício, beneficiário e valor vêm juntos → o
  merger deve priorizar esse par (fonte forte) em vez de somar solto.
- **Atribuição por contexto de documento:** valor de um doc de pagamento → amarrar
  ao beneficiário do MESMO doc/vizinhança, não por nome global.
- **Fuzzy match de nome no merger** (normalização já existe em `_normNome` no front;
  portar pro `resolver_entidades`): acento/caixa/prefixo → mesma entidade.
- **OCR pro "já recebido":** alvará/comprovante costuma ser scan → sem OCR o pagamento
  não entra. Ligar OCR direcionado só nas páginas de classe de pagamento.
- Regra de ouro mantida: **abster > chutar** — no conflito, null + motivo.
- **Gate:** precisão@cobertura de valor por parte vs Juriscope (join numero_autos);
  G1 ≥ 99,5% sobre emitidos.

## P5 — OAB + advogado→lado no SDK  · qualidade · esforço P (parcial feito)

- OAB: já extraído por regex determinística (`pipeline.atribuir_oab`, deployado).
- Advogado→polo: hoje inferido no front por coocorrência de doc. **Portar pro SDK**
  (o merger tem os doc_ids) pra virar dado persistido, não só render.
- **Gate:** precisão de OAB e de lado numa amostra; abstém quando ambíguo.

## P6 — Passada de verificação (verbatim cross-check)  · qualidade · esforço M

Task #86. Depois de extrair, 2º passe confirma cada campo lendo o contexto; mecânica
verifica verbatim; abstém se não bate. Paga a latência em precisão (norma do usuário).

## P7 — O GESTOR (extração orientada a eventos)  · qualidade+custo · esforço G

O norte (ver topo). Só faz sentido DEPOIS de P3 (fronteira boa) e P4 (join), porque
o gestor decide em cima de uma timeline confiável.

- **Camada 1 (universal):** garantir que TODO doc vira `{classe, data, doc_id}` na
  timeline — barato, determinístico onde der (data por âncora/regex; classe já feita).
- **Gestor (Camada 2):** dado o índice (classes + datas + 1ª linha), seleciona por
  INTENÇÃO quais docs aprofundar e com qual tarefa. Implementável primeiro como
  **regras** (mapa intenção→classes→tarefa; já temos o roteamento), depois como
  **modelo-decisor** (evolução do árbitro Tier-2 `_arbitrar_classes`).
- **Saída:** ledger = timeline de eventos datados + ficha consolidada (só o que
  interessa, limpo). Menos chamadas (não aprofunda doc irrelevante) = mais barato.
- **Gate:** (a) nenhum evento relevante some vs baseline (recall de eventos ≥ hoje);
  (b) precisão dos campos aprofundados sobe; (c) custo/processo cai (menos janelas).
- **Guarda:** seleção conservadora — o gate de recall de eventos é HARD (abster >
  descartar). Timeline cobre 100% dos docs mesmo sem aprofundar.

---

## Ordem recomendada (custo-benefício)

1. **P1 multi-GPU** — destrava volume, barato, sem risco.
2. **P2 sub-janelamento** — tapa a perda real por truncamento, pequeno.
3. **P4 JOIN valor↔parte** — o maior ganho de qualidade (é o que faltou no 1,5 GB).
4. **P3 fronteira** + **P5 advogado→lado** + **P6 verificação** — refino contínuo.
5. **P7 GESTOR** — o norte; só depois de P3+P4 (timeline confiável) ele decide bem.

## Métricas / gates (sempre)

- **Qualidade:** precisão@cobertura por campo × tribunal vs Juriscope (régua já existe).
- **Throughput:** docs/h e processos/h; GPU sem OOM (bge-m3 + Qwen coexistindo).
- **Regressão:** nenhum gate baixa; toda mudança passa pelo harness antes de promover.

Ver também: [`EXTRACAO_DO_ACERVO.md`](EXTRACAO_DO_ACERVO.md) (rodar do banco vetorizado).
