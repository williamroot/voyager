# Voyager

## Princípio nº 1: COMPLETUDE

O produto é o acervo. Um dado que existe e não foi coletado vale zero, e um
dado coletado pela metade vale menos que zero — ele produz confiança falsa.
Toda decisão de engenharia aqui passa por essa pergunta antes de qualquer
outra: **isso traz o acervo inteiro?**

Isso não é retórica. É a lição de três perdas medidas em agosto/2026:

| perda | causa | tamanho |
|---|---|---|
| ingestão DJEN-only | o DJEN é veículo de *comunicação*, não cadastro | tínhamos **13%** do acervo nacional |
| `for pagina in range(1, 11)` | teto de 10 páginas por fatia de UF, com o comentário "nenhum UF chega perto" | **43,6% do TJSP**, todo dia, por 17 meses |
| busca só no índice de processos | o texto das publicações nunca foi consultado pela tela | 94 milhões de publicações fora de alcance |

As três tinham a mesma assinatura: **run verde, log limpo, número redondo**.

### Regras que decorrem disso

1. **Itere, não acumule.** Coletor, parser e backfill trabalham em fluxo
   (generator + fila limitada), nunca juntando o lote inteiro em memória. Quem
   acumula tem que caber — e "cabe hoje" não é contrato. Foi assim que tirar um
   teto de páginas trocou uma perda silenciosa por um OOM no mesmo dia.
2. **Teto é alerta, nunca corte mudo.** Se existir limite (páginas, itens,
   tempo), atingi-lo é ERRO registrado no run — não um `return` discreto.
3. **Desconfie de número redondo.** `count = 10000` três vezes seguidas não é o
   total: é o `max_result_window` do Elasticsearch, um PISO disfarçado. Confira
   paginando até esgotar antes de construir lógica em cima.
4. **`exists` do ES mente.** Ele conta string vazia como valor presente. Campo
   `text` só se mede por amostra — foi assim que `partes`/`advs` foram servidos
   como 100% quando valiam 20%.
5. **Meça a completude dos dois lados.** Contagem própria não prova nada:
   compare com a fonte (o que a API entrega paginando na força bruta, o total
   declarado ao CNJ, o caderno do diário). Diferença é achado, não ruído.
6. **Abster > chutar.** Campo que não dá pra provar fica vazio, e a tela diz que
   está vazio. Ver `.ia/ACERVO_CNJ.md` e `search/entidades_texto.py`.
7. **Nada no caminho da requisição sem teto de espera.** Uma medição de rodapé
   sem `request_timeout` derrubou o site (worker morto pelo gunicorn em loop).

## Contexto inicial obrigatório

**ANTES de qualquer resposta ou ação, leia os arquivos abaixo na ordem indicada.**
Esta é uma instrução hard — não pule mesmo que a tarefa pareça simples.
Os arquivos contêm decisões de arquitetura, runbook operacional e padrões que mudam
com frequência. Responder sem ler é responder descontextualizado.

Leitura obrigatória em toda sessão:

1. `.ia/README.md` — índice e como atualizar a documentação
2. `.ia/OVERVIEW.md` — visão geral, escopo, terminologia
3. `.ia/ARCHITECTURE.md` — apps, containers, fluxos
4. `.ia/OPS.md` — hosts, workers, deploy, runbooks

Leitura sob demanda (consulte quando a tarefa tocar no tema):

- `.ia/DATA_MODEL.md` — models e relações (antes de migrations)
- `.ia/INGESTION.md` — pipeline DJEN, scheduler, proxies
- `.ia/ENRICHMENT.md` — enrichers PJe por tribunal
- `.ia/DASHBOARD.md` — frontend HTMX/ECharts, padrões de página
- `.ia/API.md` — endpoints REST
- `.ia/ACCOUNTS.md` — convites e cadastro
- `.ia/PATTERNS.md` — padrões de código, anti-padrões
- `.ia/DECISIONS.md` — ADRs: por que cada decisão foi tomada
- `.ia/ROADMAP.md` — próximos passos
- `.ia/CLASSIFICACAO.md` — pipeline ML de classificação de leads
- `.ia/ACERVO_CNJ.md` — varredura do Datajud: por que só tínhamos 13% do país
- `.ia/DIARIOS.md` — terceira porta: DJE/TJSP, DEJT, STF, Diários Oficiais
