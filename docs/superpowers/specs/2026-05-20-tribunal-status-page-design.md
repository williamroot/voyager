# Página de Status / Linha do Tempo por Tribunal — Design

**Data:** 2026-05-20
**Status:** aprovado (brainstorming)

## Objetivo

Nova página de dashboard que mostra, para um tribunal selecionado, a **linha do
tempo da cobertura de dados**: desde quando há dados disponíveis, qual a primeira
e a última movimentação efetivamente mapeadas, o volume ao longo do tempo e a
distribuição dos processos por ano-safra do CNJ — além de KPIs de saúde do
pipeline (backfill, lag Datajud, lag classificação).

Responde à pergunta: *"Para o tribunal X, quanto do histórico eu já tenho, e
está em dia?"*

## Não-objetivos

- Não substitui nem altera `/dashboard/tribunais/` (cards) nem
  `/dashboard/tribunais/<sigla>/` (detalhe). É uma **terceira página**.
- Sem period picker — a linha do tempo é all-time por natureza.
- Sem comparação multi-tribunal lado a lado — um tribunal por vez.

## Relação com páginas existentes

| Página | Foco | Mantida |
|---|---|---|
| `/dashboard/tribunais/` | Cards: stats agregadas de todos os tribunais | sim, intacta |
| `/dashboard/tribunais/<sigla>/` | Detalhe: KPIs + charts de volume | sim, intacta |
| **`/dashboard/tribunais/status/`** | **Linha do tempo + cobertura + saúde** | **nova** |

## Estrutura da página

```
┌─────────────────────────────────────────────────────────────┐
│ ▸ STATUS POR TRIBUNAL                          [ TRF5  ▾ ]   │  ← page_header + dropdown
├─────────────────────────────────────────────────────────────┤
│ BLOCO 1 · KPI strip (6 cards)                                │
│ [backfill] [início disp.] [1ª mov] [última mov]              │
│            [lag datajud]  [lag classif.]                     │
├─────────────────────────────────────────────────────────────┤
│ BLOCO 2 · Cobertura temporal (faixa full-width)              │
│  início_disponivel ──gap── 1ª mov ████████ última mov · hoje │
├─────────────────────────────────────────────────────────────┤
│ BLOCO 3              │  BLOCO 4                               │
│ Volume por mês       │  Processos por ano do CNJ             │
│ (bar chart ECharts)  │  (bar chart ECharts)                  │
└──────────────────────┴───────────────────────────────────────┘
```

### Filtro de tribunal

- Dropdown no `page_header`, `?tribunal=<SIGLA>`.
- Lista só `Tribunal.objects.filter(ativo=True).order_by('sigla')`.
- Default (sem query param) = primeiro tribunal ativo alfabético.
- Trocar de tribunal recarrega a página inteira (GET) — não usa HTMX swap; a
  página toda é instantânea (lê só do warm cache).

### Bloco 1 — KPI strip (6 cards)

| Card | Fonte | Cor de alerta |
|---|---|---|
| `status_backfill` | `Tribunal.backfill_concluido_em` → "concluído" (data) / "em curso" | accent se concluído, warning se em curso |
| `inicio_disponivel` | `Tribunal.data_inicio_disponivel` | — |
| `primeira_mov` | `MIN(Movimentacao.data_disponibilizacao)` do tribunal | — |
| `ultima_mov` | `MAX(Movimentacao.data_disponibilizacao)` do tribunal | warning se > 3 dias atrás |
| `lag_datajud` | hoje − `MAX(Process.data_enriquecimento_datajud)` do tribunal | warning se > 3d, danger se > 7d |
| `lag_classificacao` | hoje − `MAX(Process.classificacao_em)` do tribunal | warning se > 3d, danger se > 7d |

### Bloco 2 — Cobertura temporal

Faixa horizontal full-width. Eixo = tempo, de `min(data_inicio_disponivel,
primeira_mov)` até `hoje`. Três marcos:

- **início disponível** (`Tribunal.data_inicio_disponivel`) — onde o histórico
  *deveria* começar.
- **1ª mov mapeada** (`MIN data_disponibilizacao`) — onde *de fato* começa.
- **última mov mapeada** (`MAX data_disponibilizacao`) → **hoje**.

A barra preenchida vai de "1ª mov" a "última mov". O segmento entre "início
disponível" e "1ª mov" é renderizado como **gap** (hachurado/apagado) — torna
explícito se o backfill ainda não alcançou o floor. O segmento entre "última
mov" e "hoje" idem (lag de ingestão recente).

Implementação: `<div>`s posicionados por porcentagem (mesma técnica do mockup
aprovado), sem ECharts. Datas computadas no warm; template só posiciona.

### Bloco 3 — Volume por mês

Bar chart ECharts (`buildVolumeChart` já existe em `base.html`). Série =
contagem de `Movimentacao` por mês (`TruncMonth(data_disponibilizacao)`),
all-time, do tribunal selecionado. Mostra picos, vales e interrupções de
ingestão ao longo de todo o histórico.

### Bloco 4 — Processos por ano do CNJ

Bar chart ECharts. Série = contagem de `Process` agrupada por **ano-safra** —
o ano embutido no número CNJ (`NNNNNNN-DD.AAAA.J.TR.OOOO`, o `AAAA`). Mostra se
o tribunal tem acervo antigo ou só processo recente.

Extração do ano: `split_part(numero_cnj, '.', 2)` no SQL do warm job (segundo
campo separado por ponto). Resultado agrupado e contado em uma query.

## Dados e cache

Segue **exatamente** o padrão de `dashboard/queries.py::estatisticas_por_tribunal`:
um cron computa **todos os tribunais de uma vez**, grava uma chave de cache; o
hot path só lê.

### `compute_tribunal_status()` — chamado APENAS pelo warm

Computa, com poucas queries `GROUP BY tribunal_id` cobrindo todos os tribunais
ativos:

1. `MIN`/`MAX` de `Movimentacao.data_disponibilizacao` por tribunal.
2. `MAX` de `Process.data_enriquecimento_datajud` por tribunal.
3. `MAX` de `Process.classificacao_em` por tribunal.
4. Volume mensal: `Movimentacao.annotate(mes=TruncMonth(...)).values('tribunal_id','mes').annotate(Count)`.
5. Ano CNJ: `Process.annotate(ano=split_part(numero_cnj,'.',2)).values('tribunal_id','ano').annotate(Count)`.

Monta um payload `dict[sigla] → {kpis, cobertura, volume_mensal, ano_cnj}` e
grava em **uma** chave de cache (`tribunal_status:v1`), TTL 2h.

Campos `Tribunal` (`data_inicio_disponivel`, `backfill_concluido_em`) entram no
payload como dados serializáveis; o objeto `Tribunal` é re-hidratado no read
(mesma técnica de `estatisticas_por_tribunal`).

### `tribunal_status_data(sigla)` — hot path

Lê a chave de cache, devolve o sub-dict do tribunal pedido. Cache miss →
placeholder com flag `pending=True` (página mostra "acquiring signal" até o
próximo ciclo de warm). Nunca computa no hot path.

### Warm job

Função `warm_tribunal_status` em `dashboard/tasks.py`, registrada no
`djen/scheduler.py` no **loop de warm inline** (ThreadPoolExecutor, sem fila RQ
— ADR-017), cadência **15 min**. `_with_lock` em volta (anti-sobreposição),
igual aos outros warm jobs.

### Por que sem lazy-load HTMX

`DASHBOARD.md` manda listagens grandes e charts caros usarem lazy-load. Aqui
**todos** os dados (KPIs + timeline + as duas séries de chart) vêm de uma única
`cache.get()` — custo de hot path desprezível. A página renderiza inteira
server-side, instantânea. Lazy-load só adicionaria round-trips sem ganho.

## Arquivos afetados

| Arquivo | Mudança |
|---|---|
| `dashboard/views.py` | + view `tribunal_status` (`@login_required @require_GET`) |
| `dashboard/urls.py` | + rota `tribunais/status/` → name `dashboard:tribunal-status` |
| `dashboard/queries.py` | + `compute_tribunal_status()` + `tribunal_status_data(sigla)` + constantes de cache key/TTL |
| `dashboard/tasks.py` | + `warm_tribunal_status` |
| `djen/scheduler.py` | registra `warm_tribunal_status` no loop de warm inline (15min) |
| `dashboard/templates/dashboard/tribunal_status.html` | novo template (4 blocos) |
| `dashboard/templates/dashboard/base.html` | + item na sidebar |
| `.ia/DASHBOARD.md` | documenta a página nova |
| `.ia/OPS.md` | documenta o novo warm job |

Sem migration — nenhuma coluna nova. Sem mudança de model.

## Riscos e mitigações

| Risco | Mitigação |
|---|---|
| `Process` não tem coluna de ano-safra | Extraído via `split_part(numero_cnj,'.',2)` no SQL — só no warm, fora do hot path |
| Volume mensal all-time = `GROUP BY TruncMonth` em ~30M+ `Movimentacao` | Roda só no warm (cron 15min), nunca no hot path; query única cobre todos os tribunais |
| CNJs malformados quebram `split_part` | `split_part` devolve string vazia em vez de erro; warm descarta anos não-numéricos / fora de [1998, ano_atual+1] |
| Cache miss deixa a página vazia | Placeholder `pending=True` + "acquiring signal", idêntico a `estatisticas_por_tribunal` |
| Tribunal sem nenhuma `Movimentacao` (recém-ativado) | KPIs de mov ficam `None`; template mostra "—"; timeline degrada pra "sem dados ainda" |

## Critério de pronto

1. `/dashboard/tribunais/status/?tribunal=TRF5` renderiza os 4 blocos com dados reais.
2. Trocar o dropdown recarrega com outro tribunal.
3. Página carrega instantânea (só `cache.get()` no hot path).
4. `warm_tribunal_status` registrado no scheduler, roda a cada 15min, popula o cache.
5. Cache miss → placeholders `pending`, sem erro 500.
6. `.ia/DASHBOARD.md` e `.ia/OPS.md` atualizados.
