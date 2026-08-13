# Copy deck — Mapa de Precatórios (`/dashboard/overview/mapa/`)

**Autor:** UX Writer. **Escopo:** só microcopy. **Não editar o template a partir daqui sem ler a seção 6.**
**Arquivo alvo da implementação:** `dashboard/templates/dashboard/comercial_mapa.html` (+ 1 linha em `base.html`, nav).
**Público:** comercial e executivos, sem background jurídico nem técnico.

## 0. Como usar este deck

- Cada tabela é **ANTES → DEPOIS**. `RÓTULO` = o texto que aparece sempre. `EXPLICAÇÃO` = 1 frase ≤140 caracteres, para `title=`/tooltip/inline.
- Onde há `⚠️`, simplificar pode induzir **erro de leitura de negócio**. A alternativa já escolhida está no próprio item. Não "melhore" esses.
- Onde o deck manda **quebrar em 2 linhas** ou **trocar um número**, é mudança de layout/cálculo mínima — está isolada e justificada.
- Nada de jargão no texto visível. **Proibido:** `DJEN`, `ML`, `choropleth`, `enriquecimento`, `cobertura` (como rótulo), `sinal` solto, `score` solto, `bucket`, `agregação`, `UF` (como rótulo — use "estado"), `pot`, `conf`, `val`, `vol`.

---

## 1. Termos canônicos do produto (adotar em TODA a superfície)

Hoje o mesmo conceito aparece com 3 nomes. A partir daqui, **um conceito = um nome**.

| # | Termo canônico | O que é (dado) | Nunca mais dizer |
|---|---|---|---|
| 1 | **Processos na base** | `volume` — processos que ingerimos | "Volume", "vol", "acervo" |
| 2 | **Possível precatório** (plural: *possíveis*) | `potencial` / `tem_sinal_precatorio` | "Potencial", "pot", "sinal DJEN", "sinal", "indício" (como rótulo) |
| 3 | **Confirmado pela IA** (plural: *confirmados*) | `classificacao='PRECATORIO'` | "Confirmado (ML)", "conf", "classificação ML" |
| 4 | **Valor informado** | soma de `valor_causa` | "Valor (R$)" seco, "R$" sem qualificador |
| 5 | **Já analisado** (`N% analisado`) | `cobertura_pct` | "% validado", "val", "cobertura", "cobertura de enriquecimento" |
| 6 | **Prioridade** (0–100) | `score_foco` normalizado | "score de foco", "score", "0.0000" |
| 7 | **Ainda não analisado** | `potencial=null` / `sinal_processado=false` / `cobertura_pct=null` | "não processado", "sem dado", "—" sozinho |
| 8 | **Justiça Federal** | `uf='FED'` | "Camada Federal (multi-UF)", "FED" solto |

**Par de honestidade que nunca pode colapsar:** *possível* (indício, número grande) **≠** *confirmado pela IA* (certeiro, número menor).
**Par que nunca pode ser confundido:** *não informado* (o **tribunal** não publicou o valor) **≠** *ainda não analisado* (**nós** ainda não olhamos).

---

## 2. Regras de formatação numérica (pt-BR)

### 2.1 Dinheiro

| Faixa | Formato | Exemplo |
|---|---|---|
| ≥ 1 trilhão | `R$ N,N tri` | `R$ 3,1 tri` |
| ≥ 1 bilhão | `R$ N,N bi` | `R$ 742,1 bi` |
| ≥ 1 milhão | `R$ N,N mi` | `R$ 3,8 mi` |
| ≥ 1 mil | `R$ N,N mil` | `R$ 12,4 mil` |
| < 1 mil | `R$ N` | `R$ 850` |
| **= 0 / nulo** | **`não informado`** ⚠️ | ver 2.4 |

- Sempre **vírgula** decimal, **1 casa**, e **derruba o `,0`**: `R$ 3 tri`, não `R$ 3,0 tri`.
- 🐛 **BUG ATUAL (`fmtBRL`, linha ~54):** a escada para em `bi`, então 3,0658 trilhões viram **`R$ 3065,8 bi`**. É o número mais visível da página (KPI global) e está errado. Substituir por:

```js
const _ESCALA_BRL = [[1e12, 'tri'], [1e9, 'bi'], [1e6, 'mi'], [1e3, 'mil']];
// Devolve SEM o prefixo "R$" quando não há valor, pra caber em "Valor: não informado".
function fmtBRL(v) {
  if (v === null || v === undefined || v === '') return 'não informado';
  v = Number(v);
  if (!isFinite(v) || v === 0) return 'não informado';   // ⚠️ ver 2.4
  const a = Math.abs(v);
  for (const [k, suf] of _ESCALA_BRL) {
    if (a >= k) {
      const n = (v / k).toFixed(1).replace(/\.0$/, '').replace('.', ',');
      return 'R$ ' + n + ' ' + suf;
    }
  }
  return 'R$ ' + v.toLocaleString('pt-BR', {maximumFractionDigits: 0});
}
```

### 2.2 Contagens

- **Sempre por extenso, com ponto de milhar:** `119.586`, `2.675.121`. Usar o `fmt()` global (`toLocaleString('pt-BR')`). Nunca abreviar contagem em KPI ou linha de ranking.
- **Só na escala de cor do mapa** cabe compacto — e em pt-BR, não em inglês:

```js
// Local desta página. NÃO alterar o fmtCompact() global do base.html (outras páginas usam "k/M").
function fmtQtdCompacta(n) {
  const v = Number(n) || 0, a = Math.abs(v);
  if (a >= 1e6) return (v/1e6).toFixed(1).replace(/\.0$/, '').replace('.', ',') + ' mi';
  if (a >= 1e3) return (v/1e3).toFixed(1).replace(/\.0$/, '').replace('.', ',') + ' mil';
  return String(Math.round(v));
}
```
→ `120k` vira `120 mil`; `1.2M` vira `1,2 mi`.

### 2.3 Porcentagem

| Caso | Formato |
|---|---|
| exatamente 0 | `0% analisado` |
| 0 < n < 0,1 | `menos de 0,1% analisado` |
| ≥ 0,1 | 1 casa, vírgula, sem `,0`: `4% analisado`, `14,8% analisado` |
| nulo | `não sabemos ainda` |

🐛 **BUG ATUAL:** `Number(x).toFixed(1)` produz `0.0` com **ponto** → a tela mostra `val 0.0%`. Separador decimal errado para pt-BR em `cobLabel()` (linha ~383) e no tooltip do mapa (linha ~219). Trocar por `.replace('.', ',')` **e** derrubar o `,0`.

### 2.4 Zeros e vazios — a regra mais importante

| Situação no dado | Texto | Nunca |
|---|---|---|
| `valor = 0` | **`não informado`** + tooltip "O tribunal ainda não publicou o valor destes processos." ⚠️ | `R$ 0` |
| `potencial = null` ou `sinal_processado = false` | **`ainda não analisado`** (curto: `não analisado`) | `0`, `—` sem legenda |
| `cobertura_pct = null` | **`não sabemos ainda`** | `0%`, `desconhecida` |
| estado ausente da resposta | **`nenhum processo deste estado na base`** ⚠️ | `sem dado` |
| `confirmado = 0` com potencial conhecido | `0 confirmados` — **é zero de verdade**, pode mostrar | — |

⚠️ **Não unificar tudo em `—`.** Três vazios diferentes (tribunal não publicou / nós não lemos / não temos o processo) com o mesmo glifo é a causa raiz do print ruim. Cada um tem palavra própria. `—` só é aceitável dentro de uma linha muito apertada **se** a legenda do topo já explicou o glifo — e a legenda explica (seção 3.C).

---

## 3. Copy deck ANTES → DEPOIS

### A. Cabeçalho da página + nav

| Local | ANTES | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO |
|---|---|---|---|
| `{% block title %}` (aba) | `Mapa Comercial` | `Mapa de Precatórios` | — |
| nav (`base.html` ~438) | `Mapa Comercial` | `Mapa de Precatórios` | — |
| `<h1>` | `Mapa Comercial de Precatórios` | `Mapa de Precatórios` | — |
| subtítulo | `Onde estão os precatórios — por estado e por tribunal. Reflete o que já processamos, não a verdade do Brasil: potencial (sinal DJEN, amplo) ≠ confirmado (ML, preciso), e % validado diz o quanto ainda mal tocamos.` | `Em que estados atacar primeiro. Mostra o que já temos na nossa base — não o Brasil inteiro.` | (o subtítulo já É a frase curta; sem tooltip) |

⚠️ **A honestidade "possível ≠ confirmado" sai do subtítulo mas NÃO se perde** — ela passa a viver inteira na barra de legenda logo abaixo (bloco C), que fica a 1 cm de distância na tela. Isso mata a duplicação apontada no feedback e o símbolo `≠`, que ninguém lê. **Não remover o bloco C achando que o subtítulo cobre.**
Também sai o `<strong>`/`<span>` inline do subtítulo — vira texto puro (o `|safe` do `page_header` deixa de ser necessário aqui).

### B. Ordem dos blocos (1 mudança, resolve a duplicação)

`ANTES:` legenda → filtros → KPIs → mapa
`DEPOIS:` **legenda → KPIs → mapa → filtros no topo do card do mapa** *(ou mantém a ordem atual; a mudança obrigatória é só a de copy)*.
Marcado como **opcional** — se o implementador não quiser mexer em layout, o deck funciona na ordem atual.

### C. Barra de legenda do topo — 4 chips, 1 por regra de honestidade

Substitui o bloco de 2 itens + 1 linha cinza (linhas 490–514). Cada chip é um quadradinho de cor + rótulo + frase.

| Cor | RÓTULO | EXPLICAÇÃO (texto VISÍVEL, não só tooltip) |
|---|---|---|
| laranja `#f97316` | **Possível** | as publicações do processo citam precatório. Indício forte, ainda não é certeza. |
| verde `#10b981` | **Confirmado** | nossa IA leu o processo e confirmou que é precatório. Mais certeiro, por isso menor. |
| cinza `rgba(120,120,130,.5)` | **Ainda não analisado** | cinza no mapa é estado que não olhamos ainda. Não quer dizer "zero precatório". |
| — (usar `R$`) | **R$ não informado** | o tribunal não publicou o valor desses processos. Não é falta de dinheiro. |

| Local | ANTES | DEPOIS |
|---|---|---|
| timestamp (dir.) | `atualizado 12/08/2026 03:14:22` | `atualizado hoje às 03:14` / `atualizado 11/08 às 22:40` — sem segundos |

⚠️ Os 4 chips são **texto visível**, não tooltip. Foi o `title=` que falhou no print: tooltip nativo não abre em toque e ninguém passa o mouse antes de tirar a conclusão errada.

### D. Filtros

| ANTES (rótulo) | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO | Opções ANTES → DEPOIS |
|---|---|---|---|
| `Classificação` | **O que a IA classificou** | Filtra pelo veredito da nossa IA. Só vale para os processos que já analisamos a fundo. | `todas` → `todos os processos` · `Precatório` ✓ · `Pré-precatório` ✓ · `Direito creditório` ✓ · `Não lead` → `Não é lead` |
| `Tipo de sinal` | **Nível de certeza** | Possível = as publicações citam precatório. Confirmado = a nossa IA já leu e bateu o martelo. | `qualquer` → `todos` · `Só com sinal (potencial)` → `Só os possíveis` · `Só confirmados (ML)` → `Só os confirmados pela IA` |
| `Natureza / classe (código)` | **Código da classe processual** | Código de classe do CNJ. Deixe vazio para ver todas as classes. | placeholder `ex.: 1116` ✓ |
| `Ano CNJ (min / max)` | **Ano do processo (de / até)** | Ano que está no número do processo — em regra, quando ele começou. | placeholders `min`/`max` → `de`/`até` |
| `Valor R$ (min / max)` | **Valor da causa (de / até)** | ⚠️ Filtra pelo valor publicado pelo tribunal. Processo sem valor informado fica de fora do resultado. | placeholders `min`/`max` → `de`/`até` |
| `Limpar filtros` | ✓ mantém | — | — |

⚠️ O filtro de valor **exclui silenciosamente** todo processo com `valor_causa = 0` (= não informado), que é a maior parte da base. Sem essa frase o comercial conclui "não tem nada acima de R$ 1 mi nesse estado". A explicação é obrigatória, não decorativa.

### E. KPIs globais (4 cards)

| ANTES | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO (`title`) |
|---|---|---|
| `Volume` | **Processos na base** | Processos deste recorte que já trouxemos para a nossa base. Não é o acervo total dos tribunais. |
| `Valor (R$)` | **Valor informado** | Soma dos valores publicados pelos tribunais. Processo sem valor publicado não entra nesta soma. |
| `Potencial` | **Possíveis precatórios** | Processos cujas publicações citam precatório. Indício forte — ainda não confirmado por ninguém. |
| `Confirmado` | **Confirmados pela IA** | Processos que a nossa IA leu e confirmou serem precatório. Mais certeiro e, por isso, menor. |

Manter o glifo `ⓘ` ao lado do rótulo (afordância de "tem explicação"). Rótulo em 2 palavras cabe no card; se estourar, quebra em 2 linhas — **não** abreviar.

### F. Card do mapa: título, métrica ativa e toggles

| Local | ANTES | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO |
|---|---|---|---|
| `<h2>` | `Choropleth por estado` | **Brasil por estado** | Cor mais forte = mais do que está selecionado nos botões à direita. |
| chip da métrica | `· Potenciais (sinal DJEN) ⓘ` | `· mostrando: Possíveis precatórios` | (usa a explicação da métrica ativa, glossário) |
| `rotuloMetrica()` → valor | `Valor (R$)` | **Valor informado (R$)** | — |
| `rotuloMetrica()` → confirmado | `Confirmados (ML)` | **Confirmados pela IA** | — |
| `rotuloMetrica()` → potencial | `Potenciais (sinal DJEN)` | **Possíveis precatórios** | — |
| toggle 1 `aria-label` | `Métrica` | `O que o mapa mostra` | — |
| toggle 1 botões | `Volume` / `R$` | **`Quantidade`** / **`R$`** | Quantidade = nº de processos. R$ = soma dos valores informados. |
| toggle 2 `aria-label` | `Sinal` | `Nível de certeza` | — |
| toggle 2 botões | `Potencial` / `Confirmado` | **`Possíveis`** / **`Confirmados`** | (glossário `potencial` / `confirmado`) |
| toggle 2 **desabilitado** (modo R$) | *(só fica 40% opaco, sem explicação)* | **acrescentar** `title`: | No modo R$ não separamos possíveis de confirmados — o valor vem do processo inteiro. |
| `series.name` | `Potenciais` / `Confirmados` / `Valor (R$)` | `Possíveis` / `Confirmados` / `Valor informado` | — |
| `visualMap.text` | `['alto','baixo']` | **`['mais','menos']`** | — |
| `visualMap.formatter` (modo Quantidade) | `fmtCompact` → `120k` | `fmtQtdCompacta` → `120 mil` | — |

### G. Tooltip do mapa (hover no estado)

ANTES (7 linhas, mistura jargão e abreviação):
```
Rondônia (RO)
Volume: 1.234.567
Valor: R$ 0
Potencial: 119.586
Confirmado: 1.948
0.0% validado
score foco: 0.0142
[frase longa do glossário]
```

DEPOIS:
```
Rondônia (RO)
Processos na base: 1.234.567
Valor informado: não informado
Possíveis precatórios: 119.586
Confirmados pela IA: 1.948
Já analisado: 0%
Prioridade: 100 de 100
─────────────────────────
[explicação curta da métrica ativa — ≤140 car.]
```

Variantes de vazio, item por item:

| Campo | Condição | Texto |
|---|---|---|
| Valor informado | `valor = 0` | `não informado` |
| Possíveis precatórios | `potDesconhecido(b)` | `ainda não analisado` *(itálico, laranja translúcido — mantém o tratamento visual atual)* |
| Já analisado | `cobertura_pct = null` | `não sabemos ainda` |
| Prioridade | `potDesconhecido(b)` | `ainda não medida` |
| tooltip inteiro | estado sem bucket na resposta | `Rondônia (RO)` + `nenhum processo deste estado na nossa base` ⚠️ |

⚠️ O fallback atual é `sem dado`, que o leitor entende como "não tem precatório aqui". São coisas diferentes: **não temos o processo** (nada ingerido) vs **não lemos o processo** (ingerido, não analisado). O texto acima separa os dois.
Trocar também `Potencial: sinal ainda não processado` (linha ~222) → `Possíveis precatórios: ainda não analisado`.

### H. Painel "Ataque primeiro" (ranking de estados)

| Local | ANTES | DEPOIS |
|---|---|---|
| `<h2>` | `Ataque primeiro` | ✓ **mantém** (é a melhor linha da página) |
| descrição | `Ranking por score de foco ⓘ — precatório-rico, com dinheiro, ainda pouco tocado. Clique pra abrir os tribunais.` | **`Estados em ordem de prioridade: muito precatório, com valor, e que quase não olhamos ainda. Clique para ver os tribunais.`** |
| aviso ⚠ (parcial) | `sinal de precatório processado em 12 de 27 estados — ranking parcial; "pot —" = ainda não processado, não zero.` | **`Já lemos as publicações de 12 dos 27 estados. Onde diz "ainda não analisado", o estado nem entrou nesta comparação.`** |
| loading | `acquiring signal…` | **`carregando o ranking…`** |

**Linha do ranking.** ANTES (1 linha cifrada):
```
1. RO Rondônia                          0.0142
[▓▓▓▓▓▓▓▓░░░░░░░░]
pot 119.586   conf 1.948   R$ 0        val 0.0%
```
DEPOIS (**quebrar a linha de números em 2** — é a mudança de maior impacto do deck):
```
1. RO Rondônia                    prioridade 100
[▓▓▓▓▓▓▓▓░░░░░░░░]
119.586 possíveis · 1.948 confirmados
R$ não informado · 0% analisado
```

| Célula | ANTES | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO (`title`) |
|---|---|---|---|
| pontuação | `0.0142` | `prioridade 100` | Escala de 0 a 100 comparando só os estados desta lista, com os filtros atuais. 100 = o mais promissor. |
| pontuação sem sinal | `0.0000` | `prioridade —` | Ainda não medida: falta ler as publicações deste estado. |
| possíveis | `pot 119.586` | `119.586 possíveis` | (glossário `potencial`) |
| possíveis vazio | `pot —` | `possíveis: não analisado` | Ainda não lemos as publicações deste estado. É desconhecido, não zero. |
| confirmados | `conf 1.948` | `1.948 confirmados` | (glossário `confirmado`) |
| valor | `R$ 0` | `R$ não informado` | O tribunal ainda não publicou o valor destes processos. Não é falta de dinheiro. |
| analisado | `val 0.0%` | `0% analisado` | (glossário `cobertura`) |
| analisado vazio | `val desconhecida` | `não sabemos quanto foi analisado` | — |

Se o painel ficar estreito demais para `119.586 possíveis`, a ordem de degradação é: **(1)** manter o número e encurtar a palavra para `poss.`/`conf.`; **(2)** só então virar coluna. **Nunca** voltar para `pot`/`conf`/`val`.

⚠️ **`prioridade 0–100` troca o número exibido.** Vantagem: `0.0142` é ilegível e o comercial não tem âncora para julgar se é alto. Riscos e mitigação: (a) é **relativo à lista filtrada**, então muda quando os filtros mudam → dito na explicação; (b) pode ser lido como "%" → sempre a palavra `prioridade` na frente, e no tooltip a linha `cálculo interno: 0,0142` para quem precisa auditar. Fórmula: `Math.round(100 * score_foco / maxScore())`.

### I. Bloco "Justiça Federal"

| Local | ANTES | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO |
|---|---|---|---|
| título | `Camada Federal (multi-UF)` | **`Justiça Federal — fora do mapa`** | Processo federal corre em vários estados, então não cabe em um só. Fica contado aqui, separado. |
| `Volume:` | | `Processos na base:` | (glossário `volume`) |
| `Valor:` | | `Valor informado:` | (glossário `valor`) |
| `Potencial:` | | `Possíveis:` | (glossário `potencial`) |
| valor de potencial vazio | `não processado` | `ainda não analisado` | — |
| `Confirmado:` | | `Confirmados pela IA:` | (glossário `confirmado`) |
| botão | `ver tribunais →` | `Ver tribunais federais →` | — |

### J. Drill-down (painel do estado selecionado)

| Local | ANTES | DEPOIS — RÓTULO | DEPOIS — EXPLICAÇÃO |
|---|---|---|---|
| botão fechar `aria-label` | `Fechar` | `Fechar o painel deste estado` | — |
| mini-KPI 1 | `Volume` | `Processos na base` | (glossário `volume`) |
| mini-KPI 2 | `Valor` | `Valor informado` | (glossário `valor`) |
| loading | `acquiring signal…` | `carregando os tribunais…` | — |
| vazio | `Sem tribunais com dado nesta UF sob os filtros atuais.` | **`Nenhum tribunal deste estado tem dado com os filtros atuais. Tente limpar os filtros.`** | — |
| erro | *(mostra o erro cru, ex.: `HTTP 500`)* | **`Não conseguimos carregar os tribunais deste estado.`** + detalhe técnico em fonte menor | — |

**Linha do tribunal.** ANTES:
```
1. TJRO                                score 0.0142
vol 1.234   pot —   conf 12
R$ 0                            validado 4.0%
```
DEPOIS:
```
1. TJRO                            prioridade 100
1.234 processos · 12 confirmados
possíveis: não analisado
R$ não informado · 4% analisado
```

| Célula | ANTES | DEPOIS |
|---|---|---|
| pontuação | `score 0.0142` | `prioridade 100` (mesma regra da seção H) |
| volume | `vol 1.234` | `1.234 processos` |
| possíveis | `pot 119.586` / `pot —` | `119.586 possíveis` / `possíveis: não analisado` |
| confirmados | `conf 12` | `12 confirmados` |
| valor | `R$ 0` | `R$ não informado` |
| analisado | `validado 4.0%` | `4% analisado` |

### J.2 Frase de leitura automática (`insightUf()`)

É o texto mais lido do drill-down — sai no card laranja, montado dos números do estado. Copy quase boa; ajustes cirúrgicos.

| Caso | ANTES | DEPOIS |
|---|---|---|
| sinal não processado | `Ainda não processamos o indício de precatório neste estado — os 1.234 processos já estão na base, mas a leitura das publicações ainda não passou por aqui.` | **`Ainda não lemos as publicações de Rondônia. Os 1.234 processos já estão na nossa base, mas nenhum foi olhado a fundo aqui.`** ⚠️ (dizer o nome do estado; trocar "não processamos o indício" por "não lemos as publicações") |
| corpo | `Rondônia: 9,7% dos processos deste estado têm indício de precatório e apenas 0,4% foi analisado a fundo` | **`Rondônia: 9,7% dos processos daqui citam precatório e só 0,4% foi analisado a fundo`** |
| densidade < 0,1% | `0,0% dos processos…` | **`menos de 0,1% dos processos daqui citam precatório`** ⚠️ (`0,0%` é lido como "zero") |
| cobertura nula | `(ainda não sabemos quanto foi analisado a fundo)` | **`— e ainda não sabemos quanto foi analisado a fundo`** (sem parênteses) |
| tom: densa + pouco analisada | ` — território praticamente inexplorado, prioridade alta.` | ✓ **mantém** |
| tom: bem analisada | ` — já bem explorado por nós.` | ✓ mantém |
| tom: densa | ` — bom indício de precatório, ainda pouco explorado.` | **` — boa concentração de precatório, ainda pouco explorado.`** |
| tom: densidade baixa | ` — indício de precatório baixo neste estado.` | **` — pouca menção a precatório neste estado.`** |
| tom: neutro | ` — ainda há espaço pra explorar.` | ✓ mantém |

### K. Bloco "Como ler estes números"

| Local | ANTES | DEPOIS |
|---|---|---|
| botão | `Como ler estes números` | ✓ **mantém** |
| item 1 | `Volume — Quantos processos deste estado já trouxemos para a nossa base. Não é o total do tribunal.` | **`Processos na base` — Quantos processos deste estado já trouxemos para cá. O tribunal tem mais; estes são os que temos.** |
| item 2 | `Potencial (pot) — Processos cujas publicações mencionam precatório, ofício requisitório ou RPV. É um forte indício, não uma certeza — pode incluir menções como "carta precatória".` | **`Possíveis` — As publicações do processo citam precatório, ofício requisitório ou RPV. Indício forte, não certeza: um "carta precatória" entra aqui por engano.** |
| item 3 | `Confirmado (conf) — Processos que nossa inteligência artificial já classificou como precatório de verdade. Número menor porque só analisamos a fundo os processos já enriquecidos.` | **`Confirmados pela IA` — A nossa IA leu o processo e bateu o martelo: é precatório. O número é menor porque só analisamos a fundo parte da base.** |
| item 4 | `R$ (Valor) — Soma do valor da causa. "R$ 0" significa que o tribunal ainda não informou o valor — não que o processo não tenha dinheiro.` | **`Valor informado` — Soma dos valores publicados pelos tribunais. "Não informado" = o tribunal não publicou. Não é falta de dinheiro. ⚠️** |
| item 5 | `Validado (%) — Quanto deste estado já passou pela nossa análise detalhada. Perto de 0% = território inexplorado.` | **`Já analisado` — Quanto deste estado já passou pela nossa análise a fundo. Perto de 0% é território praticamente virgem.** |
| item 6 | `Score (pontuação) — Prioridade comercial: alto = muitos precatórios em relação ao total E pouco explorado por nós. É a resposta para "onde atacar primeiro".` | **`Prioridade` — 0 a 100: muito precatório em relação ao total E pouco explorado por nós. É a resposta para "onde atacar primeiro".** |

⚠️ Este bloco hoje vive **dentro do drill-down** — quem nunca clica num estado nunca lê a legenda. Recomendação: **mover para o card do topo, colapsado**, ao lado dos 4 chips (a preferência já persiste em `localStorage`). Mudança de posição, copy idêntica.

### L. Estados de carregamento e erro (mapa)

| Local | ANTES | DEPOIS |
|---|---|---|
| skeleton do mapa | `acquiring signal` | ✓ **mantém** (identidade da casa, `pulsar-mark`, só no gráfico) |
| erro — wordmark | `signal lost` | ✓ mantém |
| erro — linha humana | *(não existe: só o erro cru)* | **`Não conseguimos carregar os dados do mapa.`** ← nova linha, acima do detalhe técnico |
| erro — detalhe | `HTTP 500` | ✓ mantém, em `text-xs font-mono text-fg-subtle` |
| botão | `tentar de novo` | `Tentar de novo` |
| erro do GeoJSON | `Não consegui carregar o mapa do Brasil (HTTP 404).` | **`Não conseguimos desenhar o mapa do Brasil. Recarregue a página.`** + detalhe técnico à parte |
| **FALTA HOJE:** resposta OK com `ufs = []` | *(mapa todo cinza, sem explicação)* | **`Nenhum estado tem dado com os filtros atuais.`** + botão `Limpar filtros` ← estado vazio novo ⚠️ |

⚠️ Sem esse último estado, um filtro restritivo demais (ex.: `valor de: 1000000`) pinta o Brasil inteiro de cinza — e cinza, pela nossa própria legenda, significa "ainda não analisado". O mapa passa a **mentir** por acidente. É o item de honestidade mais barato de implementar do deck.

---

## 4. `window.GLOSSARIO` reescrito

**As chaves ficam as mesmas** de propósito — assim nenhuma chamada `glos('...')` do template precisa mudar. Só o texto muda. Duas chaves novas no fim.

```js
// Glossário canônico — fonte única de toda explicação numérica da página.
// Regras: ≤140 caracteres, 1 frase, ZERO parênteses aninhados, zero jargão interno.
window.GLOSSARIO = {
  volume:     'Processos deste estado que já estão na nossa base. O tribunal tem mais — estes são os que temos.',
  valor:      'Soma dos valores publicados pelos tribunais. "Não informado" quer dizer que o tribunal não publicou, não que falte dinheiro.',
  potencial:  'Processos cujas publicações citam precatório. Indício forte, não certeza. "Não analisado" = ainda não lemos este estado.',
  confirmado: 'A nossa IA leu o processo e confirmou que é precatório. Mais certeiro que os possíveis e, por isso, um número menor.',
  cobertura:  'Quanto deste estado já passou pela nossa análise a fundo. Perto de 0% é território praticamente virgem.',
  score:      'Prioridade de 0 a 100: muito precatório, com valor, e pouco explorado por nós. 100 = o estado mais promissor da lista.',
  // novas
  naoAnalisado: 'Ainda não lemos as publicações deste estado. O número é desconhecido, não é zero.',
  federal:      'Processo federal corre em vários estados, então não cabe em um só. Fica contado aqui, separado do mapa.',
};
```

Contagem de caracteres: 101 / 128 / 124 / 122 / 108 / 121 / 82 / 100. Todas ≤140. ✅

---

## 5. Micro-regras de estilo desta página

1. **Voz ativa, sujeito "nós":** "ainda não lemos", "a nossa IA confirmou". O produto assume o que fez e o que não fez.
2. **Rótulo sempre substantivo, nunca sigla.** Se não cabe, o card cresce ou quebra em 2 linhas.
3. **Número antes da palavra:** `119.586 possíveis`, não `possíveis: 119.586` — o olho bate no número primeiro no scan de ranking.
4. **Sentence case** em rótulos de campo e botões (`Tentar de novo`, não `TENTAR DE NOVO`). O `uppercase` do CSS nos rótulos de KPI/filtro pode ficar — é a identidade da casa.
5. **`ⓘ` marca "tem explicação"** — mas o que é crítico para não errar a leitura é **texto visível**, nunca só `title` (tooltip nativo não existe no toque).
6. **Nunca usar `≠`, `↔`, `×`, `⇒`** em texto para leigo. Escrever a palavra.
7. Reticências de carregamento: `carregando o ranking…` (uma reticência, caractere `…`).

---

## 6. ⚠️ Riscos: onde simplificar pode induzir erro de negócio

| # | Risco | Escolha do deck |
|---|---|---|
| 1 | Colapsar *possível* e *confirmado* num só número "precatórios" — inflaria o pipeline em ~50× | Mantidos **dois rótulos, duas cores, dois números**, sempre lado a lado, e a diferença dita em texto visível nos chips do topo. |
| 2 | Traduzir `R$ 0` para `—` | ⚠️ `—` viraria irmão do "não analisado". Escolhido **`não informado`** com a causa dita: *o tribunal não publicou*. |
| 3 | Traduzir "não processado" para `0` ou omitir | ⚠️ Escolhido **`ainda não analisado`** + `prioridade —` + o aviso "12 dos 27 estados" no ranking. O desconhecido continua explícito. |
| 4 | Trocar `score 0.0142` por `prioridade 100` | ⚠️ Muda o número na tela. Mitigado com a palavra `prioridade`, com "comparando só os estados desta lista, com os filtros atuais" e com `cálculo interno: 0,0142` no tooltip. |
| 5 | Mapa todo cinza por filtro restritivo | ⚠️ Cinza = "não analisado" na nossa legenda → estado vazio novo `Nenhum estado tem dado com os filtros atuais.` (seção L). |
| 6 | Filtro de valor esconde processos sem valor informado | ⚠️ Explicação obrigatória no campo (seção D). Sem ela o mapa parece dizer "não existe crédito grande aqui". |
| 7 | "Processos na base" ser lido como acervo do tribunal | ⚠️ A frase termina sempre em "O tribunal tem mais — estes são os que temos." |
| 8 | Tirar "Federal" do mapa sem dizer | ⚠️ O bloco vira **`Justiça Federal — fora do mapa`**, com o porquê em 1 frase. |
| 9 | Legenda só em `title=` | ⚠️ Foi exatamente o que falhou no print. Os 4 chips e o bloco "Como ler" são **texto visível**. |
| 10 | `R$ 3065,8 bi` | 🐛 Bug real do `fmtBRL`. Corrigido na seção 2.1 — `R$ 3,1 tri`. |

---

## 7. Checklist de aplicação (para o agente implementador)

- [ ] `fmtBRL` com `tri` + `= 0 → 'não informado'` (§2.1)
- [ ] `fmtQtdCompacta` local para o `visualMap` — **sem** tocar o `fmtCompact` global (§2.2)
- [ ] `cobLabel` → separador vírgula, `,0` derrubado, sufixo `analisado`, nulo → `não sabemos ainda` (§2.3)
- [ ] `prioridade(b)` = `Math.round(100 * score_foco / maxScore())`, `null` quando `potDesconhecido` (§H)
- [ ] `window.GLOSSARIO` substituído inteiro, chaves preservadas (§4)
- [ ] Título da aba, nav e `<h1>` (§A) — nav é `base.html` linha ~438
- [ ] Subtítulo curto, sem `≠`, sem HTML inline (§A)
- [ ] Barra de legenda de 4 chips, texto visível (§C)
- [ ] Rótulos dos filtros + opções + placeholders + explicação do filtro de valor (§D)
- [ ] 4 KPIs (§E)
- [ ] Título do card, chip de métrica, `rotuloMetrica`, `series.name`, `visualMap.text`, `aria-label` e rótulos dos 4 botões de toggle + `title` do toggle desabilitado (§F)
- [ ] Tooltip do mapa reescrito, com as 5 variantes de vazio (§G)
- [ ] Ranking: descrição, aviso parcial, linha em 2 níveis, `prioridade` (§H)
- [ ] Bloco Federal (§I)
- [ ] Drill-down: mini-KPIs, loading, vazio, erro humano, linha do tribunal (§J)
- [ ] `insightUf()`: nome do estado no caso "não lemos", `menos de 0,1%`, parênteses fora, 2 tons reescritos (§J.2)
- [ ] "Como ler estes números": 6 itens + **mover para o card do topo** (§K)
- [ ] Loading/erro do mapa + **estado vazio novo `ufs = []`** (§L)
- [ ] Varredura final: `grep -nE 'pot |conf |val |vol |DJEN|choropleth|Choropleth| ML|score |enriquec|cobertura|potencial\b' comercial_mapa.html` — só pode sobrar em comentário `{# #}`/`//` e em nome de variável/chave JS
