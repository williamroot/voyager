# Documentação técnica do Voyager

Esta pasta concentra a documentação **destilada** do projeto — o que um engenheiro novo precisa saber pra ser produtivo em algumas horas, sem ler 100k linhas de código.

## Índice

| Documento | Quando ler |
|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | Sempre: visão de alto nível, motivação, escopo, terminologia |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Antes de modificar a estrutura geral, adicionar app, mexer em fluxos |
| [`DATA_MODEL.md`](DATA_MODEL.md) | Antes de criar/alterar models ou migrations |
| [`INGESTION.md`](INGESTION.md) | Antes de mexer em DJEN, jobs, scheduler, proxies |
| [`ENRICHMENT.md`](ENRICHMENT.md) | Antes de adicionar enricher de tribunal novo (TRF3, TRF5, etc.) |
| [`DASHBOARD.md`](DASHBOARD.md) | Antes de criar páginas, alterar tema, adicionar componentes |
| [`API.md`](API.md) | Antes de criar/alterar endpoints REST |
| [`PATTERNS.md`](PATTERNS.md) | Sempre: padrões de código, anti-padrões, decisões idiomáticas |
| [`OPS.md`](OPS.md) | Quando rodar/operar o sistema, runbooks, troubleshooting |
| [`DECISIONS.md`](DECISIONS.md) | ADRs — por que escolhemos cada caminho |
| [`ROADMAP.md`](ROADMAP.md) | O que está pendente, escopo futuro |

## Como atualizar

Estes arquivos são **fonte de verdade técnica**. Quando você muda código que afeta um deles, **atualize na mesma PR**. Não deixe pra depois.

Manter em pt-BR. Direto e curto. Exemplos > prosa. Diagramas em ASCII art.
