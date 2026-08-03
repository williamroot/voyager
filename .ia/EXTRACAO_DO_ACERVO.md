# Rodar a extração a partir do ACERVO vetorizado (por CNJ, sem arquivo)

> Objetivo: localizar um processo já vetorizado (por `numero_cnj`) e rodar a
> extração **a partir do texto no banco**, sem re-upload, sem re-OCR, sem
> re-classificar. Reusa o mesmo motor (SDK) e a classificação já feita.

## Por que é fácil (os dois encaixes já existem)

1. **O SDK aceita TEXTO, não só PDF.** A entrada real do pipeline é
   `aextrair_textos(textos: list[TextoDoc], …)` (pipeline.py:447). PDF→texto é só
   pré-passo. `TextoDoc` já carrega `classe` — **o pipeline honra a classe
   pré-decidida** (não re-classifica se já vier).
2. **O acervo já tem tudo por CNJ** (Zordon `acervo/models.py`):
   ```
   Processo(numero_cnj unique)
     └─ Documento(doc_classe, doc_classe_conf, doc_classe_fonte)   ← já classificado!
          └─ Chunk(text [EncryptedText], ordinal, embedding, search_vector)
   ```
3. **Destino de persistência já modelado:** `MetadadoExtraido(numero_cnj,
   extrator_versao, …)` — uma linha por (cnj, versão).

Então: montar `TextoDoc[]` a partir dos chunks ordenados de cada `Documento`
(usando o `doc_classe` já gravado) e chamar `aextrair_textos`. **Pula** OCR e
reclassificação. É plumbing, não motor novo.

## Fluxo

```
numero_cnj
  → Processo → Documentos (doc_classe já setado)
      → por Documento: Chunk.objects.order_by('ordinal') → junta text  →  1 TextoDoc
          TextoDoc(arquivo=Documento.nome/id, texto=<chunks concatenados>,
                   classe=doc_classe, classe_conf=…, classe_fonte='acervo')
  → aextrair_textos(textos)  → roteia por classe → extrai (janela por doc) → merger
  → persiste em MetadadoExtraido (e/ou ShowcaseAnalise p/ a UI)
```

## Fases

### F1 — Adapter `textos_do_acervo(cnj)`  · esforço P
- No Zordon (ou numa camada fina), função que lê `Documento`+`Chunk` do CNJ e
  devolve `list[TextoDoc]` (ordinal preserva a ordem; junta chunks do mesmo doc).
- Honra `doc_classe` já gravado → `TextoDoc.classe` (fonte='acervo'); onde vier
  vazio, deixa o segmentador/`classificar` decidir (fallback já existe).
- Cuidado: `Chunk.text` é `EncryptedText` (decripta on-read) — custo baixo.

### F2 — Entry `extrair_do_cnj(cnj, versao)`  · esforço P
- Chama o adapter + `aextrair_textos` (mesma versão/modelo do pod).
- Persiste em `MetadadoExtraido` (idempotente por `numero_cnj+extrator_versao`,
  `update_or_create`) e devolve o payload (mesmo contrato da ficha da showcase).
- Sem `_talvez_limpar` (não há upload) e sem preservar arquivo (a fonte é o banco).

### F3 — UI: localizar processo → rodar do acervo  · esforço P/M
- Na showcase, campo "buscar por CNJ" (autocomplete nos `Processo` vetorizados).
- Botão **"Rodar do acervo"** → job na fila `manual` → mesma ficha + persistência
  em `ShowcaseAnalise` (compartilhável por UUID). Reusa polling/redirect já feitos.
- Diferença do upload: `arquivo_path` fica vazio; o "reprocessar" re-roda do CNJ.

### F4 — Batch (volume)  · esforço M
- Comando/stream que roda N CNJs do acervo (produtor-consumidor, como o
  `stream_backfill`), grava em `MetadadoExtraido`. Reprocessa em massa quando o
  modelo/SDK melhora — **sem tocar nos arquivos**.
- Casa com o multi-GPU (P1 do [`EXTRACAO_ROADMAP.md`](EXTRACAO_ROADMAP.md)).

## Ganhos

- **Sem re-upload / re-OCR / re-vetorização.** Usa o texto e a classificação já feitos.
- **Reprocessar em massa** quando a qualidade sobe (P2/P4 do roadmap) — o acervo é a
  fonte, um comando re-roda tudo.
- **Casa com jurimetria:** `MetadadoExtraido` já é o destino previsto (F1 do plano
  de metadados) — alimenta survival/estágio/classificador.

## Cuidados / itens em aberto

- **Cobertura de `doc_classe`:** o backfill de classificação nos 644k docs precisa
  estar razoável; onde faltar, o pipeline re-classifica (fallback), custa mais.
- **Fidelidade do chunking:** o chunker preserva o texto, mas pode ter perdido
  layout (colunas/tabelas). Pra valor em planilha densa, o PDF original ainda é
  melhor → oferecer as duas fontes (acervo rápido; PDF quando precisa de layout).
- **Qual versão/modelo:** rodar do acervo usa o mesmo pod v21; registrar
  `extrator_versao` pra rastrear.

Ver também: [`EXTRACAO_ROADMAP.md`](EXTRACAO_ROADMAP.md) · plano F1 de metadados em
[`../PLANS`] (MetadadoExtraido) · [`JURIMETRIA.md`](JURIMETRIA.md).
