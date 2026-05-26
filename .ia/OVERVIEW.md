# Voyager — Overview

## Problema

A DJEN (Diário de Justiça Eletrônico Nacional) publica diariamente milhares de comunicações processuais (intimações, citações, decisões) por tribunal. Acompanhar isso à mão é inviável; e cada tribunal tem o próprio sistema de consulta pública com formato diferente. Operações jurídicas precisam de:

1. **Histórico completo e atualizado** de movimentações por processo, organizado por tribunal
2. **Dados ricos** das partes (autores, réus, advogados com OAB) — não só o texto da movimentação
3. **Busca textual** rápida em volumes grandes (~milhões de movimentações)
4. **Visualização** que mostre agregados, anomalias e estado de saúde da ingestão

## Escopo

### Dentro
- Ingestão completa do DJEN por tribunal — full backfill + diário com sobreposição
- Storage normalizado: `Tribunal → Process → Movimentacao` + `Process ↔ Parte` (N:N)
- Enriquecimento via consulta pública dos tribunais (TRF1 implementado, demais arquitetados)
- Dashboard interno e API REST (read-only) com auth por API key
- Detecção automática de mudança de schema na DJEN (`SchemaDriftAlert`)

### Fora (não-objetivos)
- **Login em PJe pra baixar autos** — não precisamos dos PDFs, só metadata
- **Multi-tenancy / múltiplas organizações** — single-tenant
- **Filtragem por termos na ingestão** — armazenamos tudo, filtragem é responsabilidade do consumidor
- **Frontend SPA externo** — dashboard server-rendered com HTMX

## Tribunais cobertos

| Sigla | Nome | Status |
|---|---|---|
| TRF1 | Tribunal Regional Federal da 1ª Região | **Ativo** (DJEN+Datajud+PJe) |
| TRF3 | Tribunal Regional Federal da 3ª Região | **Ativo** (DJEN+Datajud+PJe) |
| TRF5 | Tribunal Regional Federal da 5ª Região | **Ativo** (DJEN+Datajud+PJe) |
| TJMG | Tribunal de Justiça de MG | **Ativo** (DJEN+Datajud+PJe) |
| TRF2 | Tribunal Regional Federal da 2ª Região | **Ativo** (DJEN+Datajud, sem enricher — E-PROC requer login/2FA) |
| TRF4 | Tribunal Regional Federal da 4ª Região | **Ativo** (DJEN+Datajud, sem enricher — E-PROC) |
| TRF6 | Tribunal Regional Federal da 6ª Região | **Ativo** (DJEN+Datajud, sem enricher — E-PROC) |
| TJSP | Tribunal de Justiça de SP | **Ativo** (DJEN+Datajud+enricher e-SAJ desde 2026-05-24, floor=2023-08-14, ~5x volume dos TRFs) |
| TJMA | Tribunal de Justiça do MA | **Enricher pronto** (PJe `pje.tjma.jus.br/pje/...`, 2026-05-26) — falta `djen_descobrir_inicio` e flip `ativo=True` |

## Terminologia

| Termo | Significado |
|---|---|
| **DJEN** | Diário de Justiça Eletrônico Nacional (CNJ Resolução 455/2022). API pública: `comunicaapi.pje.jus.br/api/v1/comunicacao` |
| **Movimentação / comunicação** | Item da DJEN. Em `Movimentacao` no banco. Tem `external_id`, `data_disponibilizacao`, `texto`, e tipo |
| **Processo (CNJ)** | Identificador padrão `NNNNNNN-DD.AAAA.J.TR.OOOO`. Em `Process`, único por (tribunal, numero_cnj) |
| **Backfill** | Ingestão histórica em chunks de 30 dias, do `data_inicio_disponivel` até hoje |
| **Janela / chunk** | 30 dias de DJEN, 1 `IngestionRun` |
| **Parte** | Pessoa física, jurídica ou advogado. Entidade compartilhada entre processos |
| **ProcessoParte** | Relação N:N. `polo` (ativo/passivo/outros) + `papel` (autor/réu/advogado/...) + `representa` (advogado→representado) |
| **Drift alert** | DJEN retornou campos novos/faltando vs nosso mapeamento. `SchemaDriftAlert` registra |
| **Probe / probe scrap** | Outra forma de "tribunal" no jargão Voyager (mission control) — cada tribunal é uma probe transmitindo telemetria |

## Conceito visual

Inspirado no programa espacial **Voyager** (1977 → presente). O dashboard é uma estação de controle:

- Cada tribunal é uma "probe" enviando telemetria
- Indicadores telemetry-style: SOL counter, UPLINK ACTIVE, ACQUIRING SIGNAL
- Cores: NASA orange + Pulsar green + Pale Blue Dot + Golden Record yellow
- Tipografia: Major Mono Display (wordmark), JetBrains Mono (data), Manrope (body)
- Páginas de erro: SIGNAL LOST (404), CRITICAL ANOMALY (500), RESTRICTED SECTOR (403)

Detalhes em [`DASHBOARD.md`](DASHBOARD.md).
