# Schema canônico do Elasticsearch

Fonte de verdade dos índices ES (VM dedicada `192.168.30.128:9200`, ES 8.19).
Postgres continua sendo a fonte de verdade dos DADOS; o ES é a camada de
leitura/busca/agregação (o Postgres de prod é contido — ver COMERCIAL_MAPA.md).

**REGRA DE OURO: não adicionar campo em doc builder sem (1) mapping em
`search/mappings.py`, (2) `PUT _mapping` aplicado em prod e (3) plano de
reindex/backfill.** O teste `tests/test_es_schema_partes.py::
test_mapping_*_cobre_todo_campo_do_doc` trava builder ⊆ mapping no CI.
Campo sem mapping vira dynamic mapping silencioso (tipo errado pra sempre).

## Índices

| Índice | Docs (2026-08-11) | Doc builder | Mapping |
|---|---|---|---|
| `voyager-processos` | 71,1M | `search/documents.py::processo_to_doc` | `PROC_MAPPING` |
| `voyager-movimentacoes` | 1,16B | `movimentacao_to_doc[_sem_partes]` | `MOV_MAPPING` |

`_id` = pk do Postgres (idempotente). Formato compat Jusbrasil/Digesto.

## voyager-processos (schema de negócio)

| Campo | Tipo | Fonte (Process.*) | Caso de uso |
|---|---|---|---|
| id, source | long, integer | pk, FonteDiario | join/compat |
| tribunal, uf | keyword | tribunal_id, geo.py | agregação, mapa comercial |
| proc | keyword | numero_cnj | busca exata por CNJ formatado |
| proc_digits | keyword | derivado (só dígitos) | busca "colável" de CNJ sem máscara |
| classe_nome, codigo_classe | keyword | classe_* | filtro/agg por classe |
| assunto, assunto_codigo | text, keyword | assunto_* | busca/filtro TPU |
| advs, partes | text | concat de ProcessoParte | full-text compat (legado) |
| **participacoes** | **nested** | ProcessoParte+Parte | **busca estruturada por parte** (abaixo) |
| orgao_julgador, juizo | keyword | orgao_julgador_nome, juizo | filtro/agg por vara |
| valor_causa | double | valor_causa | faixa de valor (federal=NULL: fato de dado) |
| ano_cnj, data_autuacao | integer, date | idem | idade do processo |
| primeira/ultima_movimentacao_em, total_movimentacoes | date, int | agregados | jurimetria de duração/atividade |
| inserido_em | date | idem | crescimento da base |
| segredo_justica | boolean | idem | filtro |
| classificacao, classificacao_score, classificacao_versao, classificacao_em | keyword, double, keyword, date | idem | leads (confirmado), freshness, A/B de modelo |
| enriquecido, enriquecido_em, enriquecimento_status | boolean, date, keyword | derivado das 4 datas; status | cobertura honesta (nao_encontrado ≠ pendente) |
| tem_sinal_precatorio | boolean | idem | potencial (Fase 0) |
| tem_ente_publico_passivo | boolean | derivado (RE_ENTE_PUBLICO no passivo) | coração do precatório |

### participacoes (nested)

```
{parte_id: long, nome: text(+.raw keyword), documento: keyword,
 oab: keyword, tipo: keyword(pf|pj|advogado|desconhecido),
 polo: keyword(ativo|passivo|outros), papel: keyword(AUTOR|EXECUTADO|…),
 eh_advogado: boolean}
```

Exemplo — "processos onde João é EXECUTADO com valor > 1M em SP":

```json
{"query": {"bool": {"filter": [
  {"term": {"uf": "SP"}},
  {"range": {"valor_causa": {"gt": 1000000}}},
  {"nested": {"path": "participacoes", "query": {"bool": {"filter": [
    {"match": {"participacoes.nome": "joão da silva"}},
    {"term": {"participacoes.papel": "EXECUTADO"}}]}}}]}}}
```

**Decisão (ADR local): nested no doc do processo, NÃO índice `voyager-partes`
separado.** Motivo: ES não tem join; o caso de uso rei (leads/API) filtra
PROCESSOS por atributos de parte combinados com atributos do processo — nested
resolve numa query. Índice separado exigiria 2 hops e estoura `terms` pra
partes de alta cardinalidade (ente público em milhões de processos).
Cardinalidade por doc é pequena (mediana <10 participações; limite ES
`nested_objects` = 10k/doc). A visão parte-cêntrica (`/dashboard/partes`,
"em quantos processos a parte X está") continua no Postgres via pontes
`ParteTribunal`/`PartePapel` — se um dia precisar sair do PG, aí sim nasce o
`voyager-partes` (parte → stats), sem conflito com o nested.

Limitações conhecidas do nested: `documento` pode vir MASCARADO
(`639.XXX.XXX-XX` — TRF3); busca por CPF exato não acha a versão mascarada
(dedupe disso é no Postgres). O vínculo advogado→representado
(`ProcessoParte.representa`) não é modelado no ES (consultar no PG pelo id).

## voyager-movimentacoes

| Campo | Tipo | Fonte | Caso de uso |
|---|---|---|---|
| id, recorte_id, processo_id, source | long/int | pks | join/compat |
| tribunal | keyword | tribunal_id | filtro |
| publish_date, available_at, detected_at | date | data_disponibilizacao, inserido_em | range de data |
| body | text (pt+ascii) | texto | **busca de decisão/teor** |
| proc, proc_digits | keyword | numero_cnj (+só dígitos) | CNJ com/sem máscara |
| advs, partes | text | ProcessoParte (vazio no backfill `--sem-partes`) | full-text |
| assunto, assunto_norm | text, nested | Process.assunto_nome, tipos_norm | classificação Jusbrasil |
| classe_nome, codigo_classe | keyword | mov ou processo | filtro |
| tipo_comunicacao | keyword | idem | Intimação/Citação/Edital… |
| tipo_documento | keyword | idem | teor: Sentença/Despacho/Acórdão… |
| nome_orgao, secao_diario, periodico_* | keyword | idem, FonteDiario | filtro por órgão/diário |
| ativo | boolean | idem | exclui canceladas |
| docurl, cached_docurl | keyword (não indexado) | link, pdf_storage | link do PDF |

Busca de decisão = `body` (match) + `tipo_documento`/`tipo_comunicacao`
(term) + `publish_date` (range) + `tribunal` (term). **UF em movimentações:
resolver no app** (`uf → [tribunais]` via `search/geo.py`) — não vale reindexar
1,16B docs pra denormalizar um campo derivável do `tribunal`.

## Write-through — quem indexa o quê (mapa honesto)

| Caminho de escrita | Dispara indexação ES? | Via |
|---|---|---|
| `Movimentacao.save()` / delete | ✅ | signal → fila `es_index` (worker na .102) |
| Ingestão DJEN (`bulk_create`) | ❌ signal não dispara | **tail do `reindexar_movimentacoes`** (keyset + checkpoint) |
| `Process.save()` (apply_event, classificação por save, admin) | ✅ | signal |
| Ingestão DJEN (Process novo via `bulk_create`) | ❌ | **reindex incremental `--desde-id`** (cron sugerido) |
| Drainer `apply_batch` (`bulk_update`/`bulk_create`) | ✅ | enqueue explícito `search.jobs.indexar_processos_bulk` no fim do batch (fix 2026-08) |
| ProcessoParte (create/delete individual) | — sem signal DE PROPÓSITO | o processo é reindexado pelo `processo.save()` que fecha todo enriquecimento; signal por-linha multiplicaria a fila ~2N por processo |
| Comandos de manutenção em massa (dedup_partes, recategorizar_tipo_partes, SQL cru) | ❌ | rodar `reindexar_processos` direcionado depois |

## Plano de reindex (backfill dos campos novos)

Pré-requisito: `PUT _mapping` aditivo nos 2 índices (o general aplica — os
campos novos de `search/mappings.py`). Sem isso o bulk cria dynamic mapping.

1. **Fase 1 — processos enriquecidos dos tribunais Juriscope** (têm partes/
   valor; é onde a busca estruturada acende):
   `reindexar_processos --tribunal X --somente-enriquecidos --sleep 0.5`
   na ordem TJSP, TRF1, TRF3, TRF4, TJMG, TRF6, TRF5, TJAL, TRF2, TJMA, TJPR.
2. **Fase 2 — resto dos tribunais Juriscope** (sem `--somente-enriquecidos`):
   popula proc_digits/inserido_em/etc. nos não-enriquecidos (~31,7M docs no
   total das fases 1+2).
3. **Fase 3 — demais tribunais** (~39,4M docs).
4. **Movimentações**: NÃO reindexar 1,16B de uma vez. `tipo_documento`/
   `proc_digits` valem pra docs novos via tail; retroativo sob demanda:
   `reindexar_movimentacoes --tribunal X --desde YYYY-MM-DD --sleep`.
5. **Contínuo**: cron diário `reindexar_processos --desde-id <max_id da última
   rodada>` cobre os Process novos da ingestão (bulk_create não tem signal).

Throttle: `--sleep 0.5` em horário comercial, `0.2` de madrugada — o DB é
I/O-sensível (regra da casa). Rodar no container web de prod com `docker exec
-d`, monitorar `SHOW POOLS` no pgbouncer. ETA a ~800–1.200 docs/s efetivos:
fases 1+2 ≈ 8–11h, total 71M ≈ 18–25h de execução líquida.

## Lacunas registradas (não resolver aqui)

- **valor real do precatório (Falcon)**: `valor_causa` é o valor DA CAUSA
  (esparso; federal NULL). O valor de ofício/requisitório vive no Falcon —
  integração futura (campo novo `valor_precatorio` quando houver pipeline).
- **`destinatarios`/`destinatario_advogados` (JSON da DJEN)**: partes cruas
  pré-enriquecimento; não indexadas (ruído/duplicação vs ProcessoParte).
- **`proc_alt`/`proc_apens`**: sempre NULL (compat de schema Jusbrasil) —
  candidatos a receber os CNJ vinculados (tema TJSP incidentes CNJ).
- **`data_envio`, `meio`, `numero_comunicacao`, `hash`**: operacionais,
  ficam só no PG.

## Freshness contínua — sync incremental (11/08)

O write-through por signal cobre `.save()` unitário. TRÊS fluxos de volume escrevem
em LOTE e não disparam signal — eram buracos por onde o ES envelhecia:

| Fluxo | Escrita | Cobertura |
|---|---|---|
| Ingestão DJEN (Process/Movimentacao) | `bulk_create` | ❌ era → ✅ `sync_es_incremental` |
| Reclassificação em lote | `.update()` | ❌ era (QA mediu −26% no confirmado) → ✅ idem |
| `tem_sinal_precatorio` de processo NOVO | nada computava | ❌ era → ✅ computado no tick |
| Enriquecimento (drainer `apply_batch`) | `bulk_update` | ✅ enfileira bulk (fix 11/08) |
| `.save()` unitário | ORM | ✅ signal |

**`search/sync_incremental.py`** → job `sync_es_incremental` no scheduler (10 min, INLINE):
- **processos novos**: keyset por `id > watermark` → computa o sinal **antes** de indexar
  (senão o doc entra com `tem_sinal_precatorio` NULL) → enfileira bulk na `es_index`;
- **processos atualizados**: watermark por `atualizado_em`; se bate o teto, avança só
  até o último lido (não pula o resto);
- **movimentações novas**: keyset por `id`.
- 1º tick **ancora** os watermarks no topo (não reprocessa a base — pra isso existe o
  `reindexar_processos`). Tetos por tick, kill-switch `cache.set('sync_es:off', True)`,
  bloco que falha não derruba o tick. 7 testes em `tests/test_sync_incremental.py`.
