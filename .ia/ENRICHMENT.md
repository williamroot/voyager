# Enriquecimento via consulta pública

DJEN dá só metadata da movimentação (texto, tipo, órgão). Pra **partes** (autores, réus, advogados com OAB) e classe/assunto/valor estruturados, precisamos consultar o sistema do tribunal direto.

## Estado atual

> ⚠️ **A coluna "Sistema" abaixo diz UM sistema por tribunal. Isso é falso em
> 15 dos 59 tribunais com acervo** — a varredura de 29/08/2026 está em
> §"Um tribunal, mais de um sistema". Cinco deles (TJSP, TJMG, TJRJ, TJAC,
> TJAL) rodam **eproc** em paralelo, e a fatia do eproc **não tem consulta
> pública** aqui: quem estiver nela é recusado antes da requisição
> (`enrichers/faixas.py`). Antes de citar esta tabela, leia aquela seção.

| Tribunal | Sistema | Implementado | Notas |
|---|---|---|---|
| TRF1 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf1.py` (subclasse) |
| TRF3 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf3.py` (subclasse) |
| TRF5 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf5.py` (subclasse) — path `/pjeconsulta/` |
| TJMA | PJe consulta pública (sem login) | **Sim** | `enrichers/tjma.py` (subclasse) — `pje.tjma.jus.br/pje/...`, sem WAF; script de pesquisa é `executarPesquisa` (sem `ReCaptcha`) — capturado via fallback `A4J.AJAX.Submit` na base; CPF/CNPJ sem máscara |
| TRF2 | E-PROC | **Não (só DJEN+Datajud ativos)** | Subdomain público `eproc-consulta.trf2.jus.br` existe mas exige captcha (`#divInfraCaptcha`) e tem IDs randomizados por sessão. Sistema interno tem login + 2FA. Parser autenticado de referência em `~/projetos/JURISCOPE/falcon/datamodel/processors/trf2.py` (965 linhas). |
| TRF4 | E-PROC | **Não (só DJEN+Datajud ativos desde 2026-05-24)** | Mesmo cenário do TRF2. |
| TRF6 | E-PROC | **Não (só DJEN+Datajud ativos desde 2026-05-24)** | Mesmo cenário do TRF2. |
| TJSP | e-SAJ **+ eproc** | **Sim** (2026-05-24) | `enrichers/esaj.py::TjspEnricher` (subclasse de `BaseEsajEnricher`, não herda BasePjeEnricher). HTTP puro (sem Selenium): `open.do` → `search.do?NUMPROC` (302) → `show.do` → parse. Selectors portados de `ESAJSPProcessDataProcessor` do JURISCOPE. Limitação: e-SAJ público mascara CPF/CNPJ, então `documento` fica vazio (OAB e nome são preservados). |
| TJAL | e-SAJ **+ eproc (2026→)** | **Sim / ativo** (2026-05-30; ativado 2026-05-31) | `enrichers/esaj.py::TjalEnricher` (subclasse de `BaseEsajEnricher`). Mesmo software/fluxo do TJSP, host `www2.tjal.jus.br`. Teste e2e em `tests/test_enricher_tjal.py`. **Roteia pelo pool ProxyScrape** (`PREFER_CORTEX=False` desde 2026-06-17 — o pool responde ~37% dos IPs; antes era Cortex-only por premissa equivocada). `worker_tjal` em 24 réplicas. Ver `.ia/DECISIONS.md` ADR-021. |
| TJMG | PJe **+ eproc + portal `www4`** | **Sim** (só o PJe) | `enrichers/tjmg.py` (subclasse) — `pje-consulta-publica.tjmg.jus.br/pje/...`. **Sem `valor_causa`** — a fonte não publica (ver §"Valor da causa"). |
| TJDFT | PJe SPA Angular + REST API (sem login) | **Sim** (2026-05-26) | `enrichers/tjdft.py` (classe própria, não herda BasePjeEnricher). API REST Spring Boot em `pje-consultapublica-api.tjdft.jus.br/v1/`. CPF/CNPJ sem máscara. Limitação: rota `/dados` não expõe `valor_causa`. |
| TJCE | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjce.py` (subclasse) — host `pje-consulta.tjce.jus.br`, path `/pje1grau/`. reCaptcha `if(false)`. |
| TJAP | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjap.py` (subclasse) — `pje.tjap.jus.br`, path `/1g/`. |
| TJPE | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjpe.py` (subclasse) — `pje.cloud.tjpe.jus.br`, path `/1g/`. |
| TJRJ | PJe clássico **+ eproc + portal `www3`** | **Sim** (só o PJe) (2026-06-29) | `enrichers/tjrj.py` (subclasse) — `tjrj.pje.jus.br`, path `/pje/`. |
| TJRO | PJe clássico (sem login) | **Sim** (2026-06-29) | `enrichers/tjro.py` (subclasse) — `pjepg-consulta.tjro.jus.br`, path `/consulta/`. Atenção: host pode devolver 403 a IPs datacenter — validar pelo pool/Cortex em prod. |
| TJAC | e-SAJ clássico **+ eproc (65,7% do que publica)** | **Sim** (só o e-SAJ) (2026-06-29) | `enrichers/esaj.py::TjacEnricher` (subclasse) — `esaj.tjac.jus.br`, 2º grau `cposg5`. |
| TJMT | PJe SPA Angular + REST (sem login) | **Sim** (2026-06-29) | `enrichers/tjmt.py` (classe própria). API gateway `hellsgate.tjmt.jus.br/consultaprocessual/ProcessosJudiciais/v2?numeroUnico=`. Exige header `X-Fingerprint` = base64(HMAC-SHA256("UA-resolução-lang-timestamp_ms", chave `A_mesma_mao_que_aplaude_e_a_que_vaia!`)) gerado fresco por request. Sem login/captcha. OAB sem UF (assume MT). |
| TJPA | Portal próprio "Consulta Unificada" (SPA + REST) | **Sim** (2026-06-29) | `enrichers/tjpa.py` (classe própria). `GET consulta-processual-unificada-prd.tjpa.jus.br/consilium-rest/processobycnj/{cnj}` (UA de browser, throttle p/ 429). reCAPTCHA só no front, não enforced. `cpfcnpj` vazio na consulta pública (tipo pf/pj por `tppessoa`). |
| **Bloqueados (recon 2026-06-29)** | vários | **Não — captcha/login/anti-bot** | Consulta pública gated, inviável headless sem captcha-solver ou credenciais: **captcha** (hCaptcha/reCaptcha/Tencent) — TJBA, TJPB, TJRR, TJSE, TJMS (e-SAJ virou SPA Next.js c/ captcha), TJGO+TJPR (PROJUDI), TJAM (PROJUDI atrás de F5 anti-bot); **login obrigatório** — TJES, TJPI, TJTO; **indeterminado** (sem amostra/host estável) — TJRN. eproc (TRF2/4/6, TJRS, TJSC) segue exigindo login+2FA (ver linhas TRF acima). Desbloqueio exige decisão: serviço de captcha-solving (2captcha etc.) ou credenciais/OTP. Veredictos completos do recon no histórico do commit. |

## Papel processual: está no TEXTO da publicação, e o JSONB não tem (27/08/2026)

O `destinatarios` do DJEN dá `polo: "A"/"P"` — **posição**. O corpo da
publicação do eproc dá a **função**, numa tabela no cabeçalho:

```html
<b>PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL Nº 5012262-48.2025.4.02.5101/RJ</b>
<table>
  <tr><td>AUTOR</td>       <td>: CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA</td></tr>
  <tr><td>ADVOGADO(A)</td> <td>: JOAO PAULO SARDINHA DOS SANTOS (OAB RJ250427)</td></tr>
  <tr><td>RÉU</td>         <td>: CAIXA ECONÔMICA FEDERAL - CEF</td></tr>
</table>
```

Naquela publicação o JSONB lista **três** advogados sem dizer de quem é qual; o
texto amarra o `RJ250427` ao autor. E `EXEQUENTE` num polo A não é a mesma coisa
que `AUTOR` — é a função que diz se o crédito já está em execução.

**Cobertura medida** (60 âncoras espalhadas, 2.400 publicações, e a leitura do
JSONB feita com `_como_lista` — `bool('[]')` é `True` e já mentiu aqui antes):

| | |
|---|---:|
| publicações **do eproc** com a tabela | **79 de 79 = 100%** |
| **todas** as publicações com a tabela | **3,3%** |
| destinatários do JSONB que ganham papel | **8,6%** |

Os 3,3% são o tamanho do eproc no fluxo nacional de hoje — e ele cresce sozinho:
a fatia eproc do TJSP foi de ~3% até 2024 para 23,4% em 2025 e 46,6% em 2026.
**Não leia os 100% como número nacional.**

**Custo:** trazer `left(texto, 4000)` junto (a tabela vive sempre no topo, antes
do "SENTENÇA") encarece a LEITURA em **+261%** — 0,065 s → 0,234 s por 2.000
processos. Em absoluto são 0,17 s, contra 24-52 s de escrita do mesmo lote:
**0,3% a 0,7% do tempo total**. Percentual assustador, grandeza irrelevante.

**Regras da implementação** (`tribunals/services/partes_djen.py`):
- o texto **NUNCA cria parte** — só rotula quem o JSONB já trouxe (e que já
  passou pelo filtro de segredo). Cabeçalho estranho no máximo deixa de rotular.
- `RELATOR`/`RELATORA` (75 ocorrências na amostra), juiz, procurador e perito
  **não são parte** e são recusados por lista.
- rótulo desconhecido ⇒ abstém. Mesmo nome com **dois** papéis ⇒ abstém, não
  escolhe.
- o papel entra na constraint `(processo, parte, polo, papel)`, então linha já
  gravada com papel vazio **não** é substituída — o retrospecto é um UPDATE
  próprio, ainda não escrito.

## eproc/TJSP: a consulta pública EXISTE e o muro é Turnstile — não login+2FA (26/08/2026)

A linha "eproc (TRF2/4/6, TJRS, TJSC) segue exigindo login+2FA" **não vale para o
eproc do TJSP**. Medido ao vivo:

```
https://eproc-consulta.tjsp.jus.br/consulta_1g/externo_controlador.php
   ?acao=tjsp@consulta_publica_eproc/exibir_processo
   &num_processo=<CNJ sem pontuação>&hash=<32 hex>
```

Num navegador com sessão validada, essa URL entrega a ficha COMPLETA, **sem
login**: capa (autuação, situação, órgão, juiz, classe), **assunto com código
TPU**, `Partes e Representantes` com papel (EXEQUENTE/EXECUTADO) e OAB do
advogado, **Valor da Causa**, e a lista de eventos. É mais do que o e-SAJ dá.

**Três coisas medidas que definem o que dá e o que não dá:**

1. **NÃO bloqueia IP de datacenter — o site é que oscila.** Eu havia escrito o
   contrário aqui, a partir de dois *connection timed out*. **Errado.** Repetido
   depois, sem proxy nenhum, de máquina comum: **HTTP 200 em ~0,09 s, 5 de 5**.
   Timeout isolado num site instável não é bloqueio; concluir "bloqueia" a
   partir de duas falhas é o mesmo erro de amostra pequena que este documento
   passa a vida denunciando.
2. **O muro é Cloudflare Turnstile, e o `hash` NÃO é atalho — com o mecanismo à
   vista.** A URL de detalhe, em sessão que não passou o desafio, responde
   **302** para

   ```
   Location: externo_controlador.php?acao=tjsp@consulta_unificada_publica/consultar
             &num_processo=40024317520258260223
   ```

   ou seja, **devolve o visitante ao formulário de busca com o número já
   preenchido**, e esse formulário traz
   `<div class="cf-turnstile" data-sitekey="0x4AAAAAABC6galUOytMs1K-">` com
   `hdnInfraCaptcha=0`. Testado também abrindo o formulário antes para criar
   sessão e reenviando o detalhe com o mesmo cookie jar e `Referer`: **mesmo
   302**. O `hash` só vale dentro de sessão que já limpou o desafio.
3. **O `hash` não é derivável.** 32 hex = MD5, mas nenhuma das 8 variantes
   óbvias bate (`md5(num_processo)`, `md5(cnj)`, com/sem pontuação, com sufixo
   `SP`/`eproc`, maiúsculas). Há sal ou componente de sessão.

**MEDIDO em 26/08/2026 — navegador headless NÃO passa, e o modo é interativo.**
Playwright + Chromium do sistema, quatro configurações:

| tentativa | resultado |
|---|---|
| headless clássico, 36 s | sem token, `hdnInfraCaptcha=0` |
| `--headless=new` + `navigator.webdriver` mascarado, 36 s | sem token |
| **headed de verdade** (Xvfb, viewport 1366×900), 48 s | sem token |
| headed + **clique na caixa**, 40 s | **caixa continua desmarcada**, sem token |

A instrumentação de rede prova que **o desafio carrega e roda** — `api.js` 200,
iframe `challenges.cloudflare.com/.../turnstile/f/av0/...` presente, chamadas
`challenge-platform` respondendo 200 (uma 401 no `/pat/`, esperado fora do
Safari). Ou seja: não é ambiente quebrado, **é recusa**.

E o screenshot mostra o ponto decisivo: o widget está em **modo interativo**
("Confirme que é humano", caixa para clicar), não no modo *managed* que passa
sozinho. A hipótese de "navegador legítimo passa nativamente" está **refutada
para este site**.

Por que o clique não marca: Playwright/Puppeteer dirigem por CDP, e o Turnstile
detecta CDP. Passar daqui exige **navegador com patch anti-detecção** ou
**serviço de captcha-solving** — as duas coisas são evasão deliberada de um
controle anti-bot, não "usar a página pública". **Isso não se implementa aqui.**
Se o dado do eproc virar necessidade de negócio, o caminho é **autorização**
(pedir acesso ao TJSP/CNJ), não evasão.

**Decisão em aberto, e é do dono do produto:** um serviço de captcha-solving tem
implicações de ToS e **não se liga sem ordem explícita**. O que é
tecnicamente diferente — e não foi testado ainda — é um **navegador headless**
(Playwright) carregando a página pública: o Turnstile em modo *managed* costuma
passar sozinho para navegador legítimo, sem solver de terceiro. Vale medir antes
de decidir qualquer coisa paga.

**Antes de gastar isso, note o que já temos de graça:** partes com papel saem em
**79,2%** das publicações eproc do próprio DJEN (§ acima). O que a consulta
pública acrescenta é **valor da causa** e o **assunto com código**, mais os
eventos. Dimensione a decisão por esses campos, não por "partes".

## Incidentes vinculados no e-SAJ: o precatório NÃO tem CNJ (01-02/09/2026)

**Veredito: o crédito do TJSP se espalha, sim — mas 95,7% dos degraus não têm
número CNJ nenhum. Eles não estão faltando no acervo: eles não podem entrar
por CNJ, nem pelo DJEN nem pelo Datajud. A página do incidente é a única
porta.**

### O que foi medido

Sonda ao vivo em 100 processos do TJSP (páginas aleatórias do heap, sementes
20260901/20260902), dois estratos: geral (`enriquecimento_status='ok'`) e o do
crédito (`tem_sinal_precatorio`, **816.017** processos). Controle: 100% dos CNJ
vêm do nosso banco e 4 de 4 processos-pai foram achados lá — se o controle
cair, a sonda mediu a fonte fora do ar, não o fenômeno.

| medida | estrato do crédito |
|---|---|
| respostas conclusivas | 53 (`detalhe`), 3 `segredo`, 24 inconclusivas por proxy |
| processos **com** incidente vinculado | **31 = 58,5%** |
| incidentes lidos | **210** (média 4,0 por processo; máximos **59** e **87**) |
| `Precatório` / `Requisição de Pequeno Valor` | **201 = 95,7%**, e **nenhum tem CNJ** |
| incidentes COM CNJ próprio | 9 (cumprimento de sentença, IDPJ) |
| desses, **fora** do nosso acervo | 6 de 9 |

O e-SAJ identifica o precatório por `<classe> (<CNJ do principal>) (<seq>)` —
"Precatório - 00012" — e por um `processo.codigo` interno. É um processo sem
número: o DJEN, que é indexado por CNJ, nunca o publica.

E é lá que está a ficha do crédito **por beneficiário**: `Reqte` (quem recebe),
`Ent. Devedora` (quem deve) e `#valorAcaoProcesso` (o valor requisitado
daquele beneficiário, publicado em 1 das 5 páginas da sonda).

Um caso real, a cadeia inteira, na fixture
`tests/fixtures/tjsp/esaj_cpopg_detalhe_com_incidentes.html`:
`1012705-02.2021.8.26.0576` (conhecimento) → `0018347-36.2022.8.26.0576`
(cumprimento provisório, **tem** CNJ) → **7 precatórios/RPV** (não têm).
Conferido no banco em 02/09/2026: temos **só o degrau do meio** — o
conhecimento não está no acervo e os 7 precatórios não podem estar. **1 de 9
nós** de um crédito, e o processo que temos está `ok`, com 14 movimentações e
`tem_sinal_precatorio = true`. É esse o "pedaço" sobre o qual o Estágio do
Crédito decide hoje.

### Três premissas que a sonda derrubou

1. **"O incidente exige captcha"** (comentário de `ESAJ_SEGUIR_INCIDENTES` em
   `core/settings.py`) — **falso**: 5 de 5 páginas abriram pelo `show.do` sem
   `uuidCaptcha`, com 60 a 84 KB e `#tablePartesPrincipais`. O mesmo comentário
   dizia "default OFF" enquanto o default no código era `True`.
2. **A promoção de classe/valor do incidente para o pai era código morto** —
   dependia de `#classeProcesso`, que **não existe** na página do incidente
   (0 de 5). Nunca disparou. E, se disparasse, escreveria `Precatório` na classe
   de um processo que é o cumprimento — e o valor de UM beneficiário no
   `valor_causa` do processo.
3. **O teto `MAX_INCIDENTES = 12` cortava calado** — com 59 e 87 incidentes
   medidos, o processo saía `ok` mostrando 12. Agora é ERRO com o número real
   (regra nº 2).

### Cobertura hoje: zero no acervo

`seguir_incidentes` só é passado por `enqueue_enriquecimento_manual` (o clique
na UI); o enriquecimento em massa nunca passa a flag. Prova por dado, não por
leitura de código: **0 participações com papel `ENT. DEVEDORA` em 217.964
amostradas** (`TABLESAMPLE SYSTEM (0.2) REPEATABLE (20260902)`), com os
controles `ADVOGADO` (93.130), `REQTE` (1.608) e `EXEQTE` (1.183) todos
presentes. O incidente-following existe em código e não produziu acervo.

### Ligado em produção (02/09/2026) — só TJSP, e por quê

O seguimento em massa é decidido **por tribunal**, dentro do
`enriquecer_processo` (o refill enfileira sem kwargs, e é ele que move o
acervo): `ESAJ_INCIDENTES_TRIBUNAIS` (default `['TJSP']`) × master switch
`ESAJ_SEGUIR_INCIDENTES` × kill switch no Redis
(`enrichers.jobs.set_incidentes_desligados({'TJSP'})` — desligar o custo de
REQUISIÇÃO num pool COMPARTILHADO não pode depender de deploy).

Começa pelo TJSP porque é onde precatório vira lead (`TRIBUNAIS_JURISCOPE`) e
onde a sonda mediu 58,5% de processos com incidente. Abrir para o resto é
decisão de orçamento, depois de medir o retorno desta fatia.

**Teto = 100 incidentes por processo, e o número tem prova.** Distribuição
medida (53 processos conclusivos, 210 incidentes): `0×22 · 1×11 · 2×10 · 3×3 ·
4×3 · 5 · 7 · 59 · 87`. O teto antigo de 12 truncava **2 processos de 53** e
colhia **41,9% dos incidentes** — os dois maiores guardam 146 dos 210, porque
um processo com 87 precatórios é um processo com 87 BENEFICIÁRIOS. Teto que
parece inofensivo pela contagem de processos e come dois terços do dado é o
`for pagina in range(1, 11)` outra vez. Passar de 100 continua sendo ERRO com o
número real.

**Custo por incidente caiu pela metade**: `_fetch_incidente` reusa a sessão do
pai (o e-SAJ atrela o `JSESSIONID` ao IP, e o incidente é o mesmo site) — 1
requisição por incidente em vez de 2. Conferido no caminho real: 8 requisições
para 1 processo + 7 incidentes.

### O que já está entregue (02/09/2026)

- `BaseEsajEnricher.parsear_incidente(html)` — ficha determinística do
  incidente: `tipo` (PRECATORIO/RPV/None), `classe`, `sequencial`,
  `cnj_principal`, `cnj_proprio`, `situacao`, `assunto`/`foro`/`vara`/
  `controle`/`recebido_em`, `valor`, **`requerentes[]`** e
  **`ent_devedoras[]`** (o #78, sem LLM). Abstém: página que não é incidente
  devolve `None`; campo que a fonte não publica sai vazio.
- `Ent. Devedora` passou a mapear para o **polo passivo** (caía em `outros`, e é
  o passivo que alimenta o "quem deve" do Overview, `search/agg_estado.py`).
- `_agregar_incidentes` devolve censo (`total`, `lidos`, `falhas`, `truncado`)
  e o resultado do job carrega `incidentes[]` com as fichas.
- 22 testes sobre HTML REAL (`tests/test_esaj_incidente.py`), conferidos por
  mutação.

### O que falta, com tamanho

| falta | tamanho medido |
|---|---|
| ~~onde guardar um incidente sem CNJ~~ | **FEITO**: `tribunals_incidente` (migration 0058), chave natural `codigo_esaj` do próprio e-SAJ |
| ligar para o resto de `TRIBUNAIS_JURISCOPE` | TJSP ligado em 02/09; ~477 k processos do estrato têm incidente, **≈ 3,2 M incidentes** no total |
| ~~custo de requisição~~ | **FEITO**: sessão do pai reusada ⇒ 1 req por incidente (≈ 3,2 M em vez de 6,5 M) |
| ~~teto real por processo~~ | **FEITO**: 100 (cobre 100% do medido; 12 colhia 41,9%) |
| CNJ novos que o seguimento descobre | ≈ 0,094 incidente-com-CNJ por processo do estrato ⇒ **≈ 77 k CNJ**, dos quais 4 de 5 não estão no acervo (**≈ 61 k**) |

## Um tribunal, mais de um sistema — a varredura dos 60 (29/08/2026)

**Veredito: 15 dos 59 tribunais com acervo rodam mais de um sistema, e em
CINCO deles isso custa requisição hoje — TJSP (já cortado em 25/08), TJMG,
TJRJ, TJAC e TJAL.** Os outros dez não têm enricher, então a fatia extra é
achado de mapa, não desperdício.

A varredura anterior (25/08) tinha coberto **24** dos 60 tribunais e concluído
"três casos". Ela não estava errada — estava incompleta, e a diferença tem
nome: método de amostragem.

### O método — e por que o anterior tinha que ser trocado

O sinal continua sendo o mesmo: **o host do `link` da publicação DJEN denuncia
o sistema**. O que mudou é como se sorteia a amostra.

Âncora por **data + OFFSET** (o desenho anterior) tem um viés que este arquivo
já denunciava em outro contexto e não tinha visto aqui: dentro de um mesmo
`data_disponibilizacao` a ordem do índice é a **ordem física**, ou seja a ordem
de inserção. Uma âncora de dia amostra **um momento de ingestão**. Medido no
mesmo TJSP, no mesmo dia:

| leitura | cobertura de `link` |
|---|---:|
| primeiras 500 linhas do dia (OFFSET 0) | **30%** |
| 500 linhas a 20 mil de OFFSET | **0,0%** |
| página aleatória do heap | **43,8%** |

As duas primeiras estão erradas, e cada uma engana para um lado.

O desenho novo é `TABLESAMPLE SYSTEM (p) REPEATABLE (semente)` em N passadas
independentes: o Postgres sorteia **páginas do heap inteiro** (126,9 M páginas
/ 969 GB em `tribunals_movimentacao`), sem depender de índice e sem privilegiar
momento de ingestão nenhum. É "âncora espalhada" feita direito — e barata:

```sql
-- sistema por tribunal: 260 passadas = 3,21 M publicações em 9,8 min
SELECT tribunal_id, split_part(split_part(link,'//',2),'/',1) AS host, count(*)
FROM tribunals_movimentacao TABLESAMPLE SYSTEM (0.0008) REPEATABLE (0.0031)
WHERE link <> '' GROUP BY 1,2;

-- estado por (tribunal, prefixo do CNJ, ano): 200 passadas = 10,36 M processos em 2,1 min
SELECT tribunal_id, left(numero_cnj,1), ano_cnj, enriquecimento_status, count(*)
FROM tribunals_process TABLESAMPLE SYSTEM (0.05) REPEATABLE (0.1001)
GROUP BY 1,2,3,4;
```

Amostra final: **3.211.149 publicações** (260 passadas, 0 estouro de teto) +
**10.359.868 processos** (200 passadas, 0 estouro) + **1,24 M publicações**
cruzadas com o processo para juntar host × prefixo × ano.

### O mapa completo — 59 tribunais com acervo, 1 sem amostra

STF está cadastrado mas tem **zero** movimentação, então não entra. Os outros
59 apareceram todos na amostra. **Um deles não pôde ser medido**: TJRR, com
`link` vazio em 100% de 4.939 publicações — ali a resposta é *não medido*,
nunca "sistema único".

| tribunal | publicações amostradas | `link` presente | sistemas vistos (share do que tem link) | enricher |
|---|---:|---:|---|---|
| TJMG | 652.781 | 11.3% | pje 51.6% · portal proprio 35.9% · eproc 12.5% · projudi 0.0% | sim |
| TJSP | 317.469 | 43.8% | e-SAJ/DJE 81.0% · eproc 19.0% | sim |
| TRF3 | 310.833 | 13.1% | pje 100.0% · portal proprio 0.0% | sim |
| TJDFT | 186.405 | 8.6% | pje 99.4% · portal proprio 0.4% | sim |
| TJGO | 116.757 | 34.6% | projudi 100.0% | — |
| TRF1 | 107.634 | 7.5% | pje 98.3% · portal proprio 1.7% | sim |
| TJMA | 105.999 | 21.5% | pje 100.0% | sim |
| TJMT | 98.842 | 24.3% | pje 99.7% · portal proprio 0.3% | sim |
| TJRS | 82.408 | 42.1% | eproc 100.0% | — |
| TRF4 | 81.202 | 26.0% | eproc 100.0% | — |
| TJPR | 76.458 | 75.3% | projudi 99.8% · eproc 0.2% | — |
| TJAM | 71.816 | 0.7% | projudi 58.8% · e-SAJ/DJE 41.2% | — |
| TJCE | 66.391 | 18.5% | pje 96.3% · e-SAJ/DJE 3.7% · portal proprio 0.0% | sim |
| TJAL | 64.994 | 7.5% | e-SAJ/DJE 100.0% · eproc 0.0% | sim |
| TRF5 | 60.687 | 22.0% | pje 100.0% | sim |
| TJBA | 52.682 | 28.6% | projudi 52.7% · pje 47.3% | — |
| TJMS | 52.174 | 14.4% | e-SAJ/DJE 98.2% · eproc 1.8% | — |
| TJRJ | 48.473 | 60.2% | pje 65.4% · portal proprio 22.1% · eproc 12.5% | sim |
| TRF6 | 40.790 | 17.7% | eproc 95.7% · pje 4.3% · portal proprio 0.1% | — |
| TJPA | 39.826 | 34.0% | pje 99.5% · portal proprio 0.5% | sim |
| TJPB | 33.657 | 24.4% | pje 100.0% | — |
| TRT2 | 32.539 | 60.9% | pje 100.0% | — |
| TJPE | 27.432 | 36.5% | pje 100.0% · portal proprio 0.0% | sim |
| TRT3 | 27.313 | 57.9% | pje 100.0% | — |
| TRT9 | 27.267 | 45.0% | pje 100.0% | — |
| TRT15 | 26.394 | 50.5% | pje 100.0% | — |
| TJSC | 25.679 | 58.0% | eproc 100.0% | — |
| TJRN | 24.029 | 81.7% | pje 100.0% | — |
| TRT1 | 22.798 | 66.5% | pje 100.0% | — |
| TJSE | 22.761 | 63.9% | portal proprio 99.5% · eproc 0.5% | — |
| TRT4 | 21.871 | 57.8% | pje 100.0% | — |
| TJAP | 20.708 | 11.7% | portal proprio 51.0% · pje 49.0% | sim |
| TRT5 | 19.218 | 54.8% | pje 100.0% | — |
| TJES | 17.830 | 30.6% | pje 100.0% | — |
| TRT6 | 17.709 | 53.4% | pje 100.0% | — |
| TRF2 | 17.690 | 52.8% | eproc 100.0% | — |
| TST | 17.045 | 74.4% | pje 82.3% · portal proprio 17.7% | — |
| TRT17 | 16.654 | 26.6% | pje 100.0% | — |
| TJPI | 15.096 | 43.8% | pje 95.4% · portal proprio 4.6% | — |
| TRT12 | 14.881 | 66.6% | pje 100.0% | — |
| TJRO | 13.914 | 90.4% | pje 98.8% · portal proprio 1.2% | sim |
| TJAC | 12.767 | 3.0% | eproc 65.7% · e-SAJ/DJE 34.3% | sim |
| TJTO | 11.406 | 29.8% | eproc 100.0% | — |
| TRT18 | 10.749 | 59.4% | pje 100.0% | — |
| TRT7 | 10.270 | 43.9% | pje 100.0% | — |
| STJ | 9.125 | 92.0% | portal proprio 100.0% | — |
| TRT10 | 8.724 | 71.5% | pje 100.0% | — |
| TRT8 | 7.407 | 63.4% | pje 100.0% | — |
| TRT23 | 5.671 | 59.5% | pje 100.0% | — |
| TRT13 | 4.996 | 69.8% | pje 100.0% | — |
| TJRR | 4.939 | 0.0% | **NÃO MEDIDO** (link vazio em 100%) | — |
| TRT14 | 4.829 | 45.7% | pje 100.0% | — |
| TRT16 | 4.250 | 55.5% | pje 100.0% | — |
| TRT24 | 4.019 | 63.4% | pje 100.0% | — |
| TRT19 | 3.522 | 48.5% | pje 99.9% · portal proprio 0.1% | — |
| TRT20 | 3.306 | 52.4% | pje 100.0% | — |
| TRT11 | 3.167 | 89.1% | pje 100.0% | — |
| TRT21 | 3.037 | 77.4% | pje 100.0% | — |
| TRT22 | 1.859 | 79.5% | pje 100.0% | — |

⚠️ **`link` presente varia de 0,0% a 92,0%.** Onde ele é baixo (TJAM 0,7%,
TJAC 3,0%, TJAL 7,5%, TRF1 7,5%, TJDFT 8,6%) a leitura é do que dá pra ver, não
do acervo. "portal proprio" agrupa o legado de cada casa: `www4.tjmg.jus.br`,
`www3/www4.tjrj.jus.br`, `www.tjse.jus.br`, `processo.stj.jus.br`,
`consultaprocessual.tst.jus.br`, `services.tjap.jus.br`/`tucujuris`.

**Onde há eproc:** TJRS, TJSC, TJTO, TRF2, TRF4 e TRF6 são eproc **puro** (não
é migração, é o sistema da casa); TJSP 19,0%, TJMG 12,5%, TJRJ 12,5%, TJAC
65,7%, TJMS 1,8%, TJSE 0,5%, TJPR 0,2%, TJAL 0,02% são **migração em curso**.

### O dano, medido dentro × fora da fatia

`enriquecimento_status` dos processos, amostra de página aleatória
(10,36 M processos), recorte `prefixo do sequencial do CNJ + ano`:

| tribunal | faixa | tamanho | `ok` DENTRO | `ok` FORA | sonda ao vivo (dentro / controle) |
|---|---|---:|---:|---:|---|
| TJSP | prefixo 4, ano ≥ 2025 | 15,9% ≈ **2,65 M** | **0,17%** | 12-18% | 16 de 16 "não existe" / 33 de 33 `ok` em 2013 |
| TJMG | prefixo 1, ano ≥ 2025 | 13,6% ≈ **1,13 M** | **0,00%** | 43-51% | **16 de 16** / 15 de 16 achou (pref. 5) |
| TJRJ | prefixo 3, ano ≥ 2024 | 13,2% ≈ **809 mil** | **0,00%** | 31-40% | **16 de 16** (2025-26) e **16 de 16** (2024) / 13 de 16 achou (pref. 0) |
| TJAC | prefixo 5, ano ≥ 2025 | 32,0% ≈ **49 mil** | **0,43%** | 92-94% | **16 de 16** / 16 de 16 com cadastro (pref. 0) |
| TJAL | prefixo 5, ano ≥ 2026 | 0,1% ≈ **490** | **0,00%** | 70-79% | **14 de 14** / — |

E o que isso custa **por hora, agora** (janela de 2 h em prod, `Process`
distintos por `enriquecido_em`, índice `proc_enriquecido_em_idx`):

| tribunal | desfechos na janela | **dentro da faixa** | `ok` dentro | `ok` fora |
|---|---:|---:|---:|---:|
| TJMG | 11.994 | **1.900 (15,8%)** | 0,0% | 74,0% |
| TJRJ | 24.499 | **1.894 (7,7%)** | 0,0% | 26,3% |
| TJAC | 166 | **154 (92,8%)** | 0,0% | 100,0% |
| TJAL | 4.344 | 17 (0,4%) | 0,0% | 83,5% |
| TJSP (já cortado) | 31.597 | 10.920 — recusa em LOTE, **zero requisição** | — | 92,7% |

⇒ **≈ 1.980 requisições por hora** em TJMG+TJRJ+TJAC+TJAL para ouvir "não
existe" garantido, cada uma podendo queimar até `MAX_PROXY_ROTATIONS` IPs do
pool **compartilhado com todos os tribunais**. No TJAC, **92,8% de todo o
trabalho de enriquecimento** cai na fatia morta.

### O que foi cortado (e a prova exigida)

A tabela de faixas vive em `enrichers/faixas.py` e cada linha exigiu **três**
provas — a terceira é a que manda:

1. **sistema**: host do `link` (eproc contra pje/e-SAJ/portal);
2. **estado**: `ok` dentro × fora, em amostra de página aleatória;
3. **sonda ao vivo**: 16 CNJ da faixa perguntados à fonte real **+ controle
   negativo na mesma janela**. Sem o controle, "não existe" pode ser a fonte
   fora do ar.

```python
TjmgEnricher.FORA_DA_FONTE_FAIXAS = (('1', 2025, 'eproc'),)
TjrjEnricher.FORA_DA_FONTE_FAIXAS = (('3', 2024, 'eproc'),)
TjacEnricher.FORA_DA_FONTE_FAIXAS = (('5', 2025, 'eproc'),)
TjalEnricher.FORA_DA_FONTE_FAIXAS = (('5', 2026, 'eproc'),)
TjspEnricher.FORA_DA_FONTE_FAIXAS = (('4', 2025, 'eproc'),)
```

⚠️ **O corte é `prefixo` E `ano ≥ N`, nunca o prefixo sozinho, e o ano é
diferente em cada tribunal.** O controle negativo do TJMG é a razão: prefixo 1
de 2015-2021 **está** no PJe (7 de 16 achou ao vivo) contra 0 de 16 em
2025-2026. Cortar por prefixo apagaria acervo bom. O mesmo já valia no TJSP
(prefixo 4 de 2013, 33 de 33 `ok`).

O mecanismo é o de 25/08, agora genérico: o hook virou `fora_da_fonte`
(`fora_do_esaj` continua resolvendo), o guard por job existe também no
`BasePjeEnricher`, e o fechamento em LOTE (`_separar_fora_da_fonte`, teto de
10.000/passada/tribunal) já era agnóstico de tribunal. **A recusa é contada,
nunca muda** — `manage.py enrich_fora_do_esaj` e ERROR no refill.

### O que NÃO foi cortado, e por quê (abster > chutar)

- **TJRR — não medido.** `link` vazio em 100% de 4.939 publicações. Nenhuma
  afirmação sobre sistema. Pendência aberta: medir por outra via (o texto da
  publicação, ou o Datajud).
- **TJCE (e-SAJ 3,7%) e TJAP (portal próprio 51,0%)** têm segundo sistema mas
  **não têm separação limpa por CNJ**: no TJCE o prefixo 0 aparece nos dois
  sistemas, no TJAP o `link` majoritário é relativo (`processo`, sem host). Sem
  predicado, recusar erraria — e `ok` dentro não é zero (TJCE 9,5%). Fica sem
  corte, com a perda documentada.
- **TJMG prefixo 1 de 2022-2024** também dá 16 de 16 "não existe", mas ali a
  amostra é toda de foro `0000` (2º grau, que o enricher não cobre — o TJMG tem
  `pjerecursal.tjmg.jus.br` sem configuração) e o estoque já está 99% consumido
  como `nao_encontrado`: recusar não pouparia requisição. Fenômeno diferente.
- **TJMS, TJPR, TJSE, TJRS, TJSC, TJTO, TRF2, TRF4, TRF6, TJBA, TJAM** têm mais
  de um sistema (ou eproc puro) e **nenhum enricher** — não há requisição a
  poupar. Entram no mapa, não no corte.
- **Nenhuma das fatias eproc ganhou porta.** Continua valendo o §"eproc/TJSP" acima:
  a consulta pública do eproc existe, é sem login, e está atrás de **Cloudflare
  Turnstile em modo interativo**. Quatro configurações de navegador testadas,
  nenhuma passa. **Solver de captcha e evasão anti-bot não estão autorizados** e
  não se ligam por conta própria; o caminho legítimo é **autorização do
  tribunal/CNJ**. Hoje esses ≈ 4,6 M de processos (TJSP+TJMG+TJRJ+TJAC+TJAL)
  **não têm porta nenhuma**: o enricher não alcança e o
  `reabastecer_fila_datajud` **exclui** tribunais com enricher
  (`TRIBUNAIS_COM_ENRICHER`). Abrir o Datajud para a fatia recusada é o próximo
  passo óbvio — e é decisão de produto, porque compartilha a cota do CNJ.

### Achado de infraestrutura no caminho

`proc_tribunal_id_idx` está declarado no model como `(tribunal, -id)` e **no
banco é só `(tribunal_id)`** — índice de uma coluna. Conferido por
`pg_get_indexdef` em 29/08/2026. É o padrão da casa que a memória já registra
("índices declarados e ausentes"): `makemigrations` compara com o ESTADO, não
com o banco. Efeito prático medido: `WHERE tribunal_id=X AND id >= N ORDER BY
id LIMIT 50` **estoura 20 s** em TJRR, TJSP e TJRJ. Quem escrever amostragem
por `id` dentro de tribunal vai bater nisso.

## Roteamento de proxy: Cortex quando datacenter morre (2026-07-06)
O pool datacenter (ProxyScrape) degrada em ondas: WAF dos tribunais bloqueia os IPs
(403) e a API do proxyscrape rate-limita a conta (429 → não repõe a lista). Isso
trava backfill DJEN + enriquecimento. Roteamento (config por env):
- **DJEN** (`djen/client.py`): pool degradado → `DJEN_CORTEX_RATIO_DEGRADED` (default
  **1.0** = 100% Cortex; era 0.9 hardcoded). Normal: `DJEN_CORTEX_RATIO` (0.5).
- **Enrichment** (`enrichers/jobs.py::enriquecer_processo`): `prefer_cortex` resolve de
  `ENRICH_PREFER_CORTEX` (default **True** = Cortex-first em TODOS os enqueues, inclusive
  o reabastecer em massa). Residencial (Cortex) passa o WAF; datacenter vira fallback.
- Cortex tem cooldown próprio (`cortex_is_bad`) se rate-limitar. O 429 do proxyscrape
  costuma resetar sozinho (janela da conta).

## Correções e-SAJ / classe (2026-07-06)

Dois bugs que faziam leads de precatório do TJSP sumirem — achados a partir de um
Cumprimento contra Fazenda com "expedição de precatório" marcado `nao_encontrado`+NAO_LEAD:

1. **`nao_encontrado` de falha transitória** (`enrichers/esaj.py`, `8bfd9f7`): o e-SAJ
   às vezes devolve **HTTP 200 com página que não é detalhe nem not-found** (soft-error/
   throttle). O código tratava qualquer 200 sem campos do detalhe como `nao_encontrado`
   **terminal** (nunca re-tenta) → **3,25M TJSP presos** como falso-negativo. Agora:
   detalhe = `classeProcesso`/redirect; not-found = **só** com marcador "Não existem
   informações"; **200 ambíguo → rotaciona/`erro` (re-tentável)**. Recuperação dos presos:
   tick agendado `enrichers.jobs.tick_reenrich_esaj_legacy` (scheduler, id `reenrich_esaj_legacy`,
   cada 5min) devolve `nao_encontrado` com `enriquecido_em < 2026-07-06` a `pendente`,
   **auto-limitante** (só quando `pendente < 20k`, 50k/tribunal/tick) — sobrevive a restart,
   sem loop (re-enriquecidos pós-fix ganham timestamp recente e saem do alvo). Cobre TJSP/TJAL/TJAC.
2. **Blanking de classe no drainer** (`enrichers/drainer.py::normalize_dados`, `bcf809e`):
   gravava `classe_codigo=''` quando o e-SAJ traz a classe só com **nome** (sem código TPU),
   apagando o código herdado do DJEN → F1=0 → lead revertia a NAO_LEAD **a cada
   re-enriquecimento** (afetava 87% dos TJSP enriquecidos). Agora só sobrescreve o código
   se vier código; senão atualiza só o nome e **preserva** o código. Idem `assunto`.

Fonte de classe pra e-SAJ (nome-only) = **backfill DJEN** (`preencher_classe_via_djen`,
mov mais recente). Ver `.ia/CLASSIFICACAO.md` §2 (regra de sinal TJSP + esses fixes).

## Arquitetura

`enrichers/pje.py::BasePjeEnricher` concentra **toda** a lógica de PJe consulta pública (form JSF, parsing do detalhe, polos, partes). Subclasses configuram só:

```python
class Trf1Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g-consultapublica.trf1.jus.br'
    LIST_URL = f'{BASE_URL}/consultapublica/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF1'
    LOG_NAME = 'voyager.enrichers.trf1'

class Trf3Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g.trf3.jus.br'
    LIST_URL = f'{BASE_URL}/pje/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF3'
    LOG_NAME = 'voyager.enrichers.trf3'
```

## Fluxo (`BasePjeEnricher.enriquecer`)

```
GET LIST_URL ────────────► HTML inicial
   │                         │
   │                         ├─ extrai javax.faces.ViewState
   │                         ├─ todos <input>/<select> do form fPP
   │                         └─ encontra script id dinâmico (executarPesquisaReCaptcha)
   │
POST LIST_URL ───────────► resposta AJAX
   {form_fields, CNJ}        │
                             ├─ regex DETALHE_PATH/[^"']+  (path varia: trf1=/consultapublica/, trf3=/pje/)
                             └─ ou idProcessoTrf:NNN → constrói URL fallback
   │
GET detalhe ──────────────► HTML completo
                             │
                             ├─ div.propertyView .name>label + .value
                             │   → classe, assunto, autuação, valor
                             ├─ <b>Órgão Julgador</b><br/>NOME → orgao_julgador
                             └─ div#poloAtivo / div#poloPassivo / div#outrosInteressados
                                 → tabelas com partes
   │
WITH transaction.atomic:
  Process.objects.select_for_update().get(pk=)    ◀── serializa workers concorrentes
  _aplicar_dados(processo, dados)                       no mesmo Process
  _aplicar_partes(processo, partes)
  processo.enriquecimento_status = OK
  processo.save(update_fields=[...])
```

**Particularidades PJe:**
- O botão `fPP:searchProcessos` é só trigger visual — o **script real** com `executarPesquisaReCaptcha` tem id `fPP:j_idXXX` dinâmico. `_find_search_script_id` localiza.
- hCaptcha presente no JS mas com flag `if (false)` — desabilitado.
- jsessionid é mantido pelo `requests.Session` (cookie automático).
- Path varia por TRF: TRF1 usa `/consultapublica/`, TRF3 usa `/pje/`. `DETALHE_PATH` parametriza.

## Valor da causa — quem publica e quem não (probe 2026-08-13)

`Process.valor_causa` **só se preenche onde a fonte manda o campo**. Medição
por sistema (amostra dos 2.000 `ok` mais recentes por tribunal, em prod):

| Sistema | Tribunais | Campo na fonte | Cobertura medida |
|---|---|---|---|
| Portal REST próprio | TJPA | `valorCausaFormatado` | 2000/2000 (17% são `0,00`) |
| SPA + REST | TJMT | `valorCausa` | 1903/2000 |
| e-SAJ | TJSP, TJAL, TJAC | `#valorAcaoProcesso` | TJSP 991/2000 · TJAL 1557/2000 |
| **PJe consulta pública** | **TJMG, TRF1, TRF3, TRF5, TJMA, TJCE, TJAP, TJPE, TJRJ, TJRO** | **não existe** | **0/2000 (TJMG e TRF3 conferidos)** |
| PJe SPA/REST | TJDFT | não existe em `/dados` | 0 |

**O PJe consulta pública não publica valor da causa — em nenhum tribunal.**
Conferido no HTML cru do detalhe de 6 processos TJMG (JEC, comum, inventário e
3 execuções fiscais) + 2 do TRF3: a string `valor` **não aparece nenhuma vez**
na página. O `propertyView` do PJe entrega só Número, Data da Distribuição,
Classe Judicial, Assunto, Jurisdição, Órgão Julgador (e às vezes Processo
referência). O ramo `'valor' in chave and 'causa' in chave` de
`BasePjeEnricher._extrair_dados` é, na prática, **código morto** — fica pra
quando algum PJe passar a expor.

Não adianta trocar de fonte pra MG:
- **Datajud** (`api_publica_tjmg`): `valorCausa` **não existe no schema** —
  0/20 docs aleatórios e 0/9 dirigidos (exec. fiscal, cumprimento, comum). O
  `_meta_updates_from_source` de `datajud/ingestion.py` já lê o campo; nunca
  acha. Idem TRF3/TJSP/TJMT/TJPA no Datajud.
- **Portal legado `www4.tjmg.jus.br/juridico/sf/proc_resultado.jsp`**: HTTP 401
  + captcha numérico (DWR `ValidacaoCaptchaAction`, input `captcha_text`) —
  exigiria captcha-solver por processo.

⇒ MG aparecer como "valor não informado" no mapa está **correto**: o tribunal
não publica. Reprocessar o que já baixamos preenche **0** processos. Fixtures da
evidência e teste-canário (falha se o TJMG passar a publicar):
`tests/test_enricher_tjmg.py` + `tests/fixtures/tjmg/`.

> **`0,00` ≠ ausente.** Onde a fonte manda o campo, `parse_valor_brl('R$ 0,00')`
> devolve `Decimal('0')` e o drainer grava 0 (TJPA: 336/2000; TJMT: 74/2000).
> Filtro/mapa que trata `valor_causa=0` como "informado" mostra "R$ 0,00" na
> ficha. Ausência real é `NULL`.

## Documentos mascarados

TRF3 PJe consulta pública mascara CPF/CNPJ por privacidade:
```
TRF1: GRACILENE ROSA LIMA - CPF: 123.456.789-00     ← real
TRF3: GRACILENE ROSA LIMA   639.XXX.XXX-XX          ← mascarado
```

`enrichers/parsers.py`:
- `CPF_RE` / `CNPJ_RE` aceitam `[\dX*]` em posições privadas
- `parse_documento(text)` devolve (string, tipo) — preserva máscara
- `is_documento_mascarado(doc)` — testa por X/* na string
- `real_casa_com_mascara(real, mascara)` — testa compatibilidade posição-a-posição (`29.979.036/0001-40` casa com `29.9XX.XXX/XXXX-XX`)

## Dedupe de partes (`_upsert_parte`)

3 caminhos em ordem de confiança:

1. **OAB** (advogados) — chave estável, precedência total.
2. **Documento real** — PK natural global (CPF/CNPJ unique constraint).
3. **Documento mascarado**:
   - Antes de criar Parte mascarada, busca Parte com mesmo nome e doc REAL que case com a máscara → reusa (TRF1 viu CNPJ completo, TRF3 vê mascarado, é a MESMA PJ).
   - Senão, dedupe por `(nome, documento)` — homônimos com máscaras distintas ficam separados.
4. **Sem doc nem OAB**: `get_or_create((nome, tipo))` — evita explosão de "Procuradoria Regional Federal" replicada em N processos.

Constraints partial em `Parte.Meta`:
```python
UniqueConstraint(documento) WHERE doc != '' AND doc NOT LIKE '%X%' AND NOT LIKE '%*%'
   → uniq_parte_documento_real
UniqueConstraint(nome, documento) WHERE doc LIKE '%X%' OR LIKE '%*%'
   → uniq_parte_documento_mascarado
UniqueConstraint(oab) WHERE oab != ''
```

### Armadilha: CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS

Os 3 índices únicos parciais de `Parte` ficaram **inválidos** em 2026
(migration 0017): `CREATE UNIQUE INDEX CONCURRENTLY` falha na validação se
a tabela já tem duplicatas, deixando o índice `indisvalid=false`; o
`IF NOT EXISTS` fez re-execuções pularem o husk morto. Índice inválido não
enforça unicidade — o `bulk_create(ignore_conflicts)` do drainer parou de
deduplicar e a tabela inflou de ~4M pra ~84M linhas.

Corrigido pelo command `dedup_partes` (colapso por chave exata: oab /
documento real / `(nome,documento)` mascarado — anti-homônimo — mais
absorção masked→real com trava de candidato único) seguido da migration
`0030_recriar_indices_unicos_parte`, que **dropa** o husk e **verifica
`indisvalid`** após recriar. Monitorar com `manage.py check_parte_indexes`
(exit 1 se algum índice único estiver inválido).

## Catálogo de classes/assuntos

`tribunals.ClasseJudicial` e `tribunals.Assunto` (PK = código TPU/CNJ). FKs em `Process.classe`, `Process.assunto`, `Movimentacao.classe`. Habilita filtros de dropdown sem `DISTINCT` em milhões de linhas e resolve discrepância de capitalização entre PJe (UPPERCASE) e DJEN (CamelCase quebrado).

`_upsert_catalogo` é race-safe via `bulk_create(ignore_conflicts=True) + get(codigo=)` — não levanta `IntegrityError` quando 2 workers veem o mesmo código pela 1ª vez.

## `classe_codigo` — o `\d{2,5}` que nunca casou a classe mais comum do país

**Medido em 29/08/2026.** O regex que parte `"NOME (CÓDIGO)"` na porta do
enricher (`enrichers/drainer.py::normalize_dados`) era

```python
CLASSE_COM_CODIGO_RE = re.compile(r'^(.+?)\s*\((\d{2,5})\)?\s*$')   # ← DOIS dígitos
```

e a classe mais comum do Brasil tem **um**: `PROCEDIMENTO COMUM CÍVEL (7)`.
Resultado: `classe_codigo` ia para `''` e o `(7)` ficava pendurado dentro do
`classe_nome`. Corrigido em `08d306e`; o regex hoje é `(\d{1,5})` com o fecho
`)` **obrigatório** (1 dígito é permissivo demais para aceitar fecho opcional).

> ⚠️ **A assimetria com o ASSUNTO é medida, não descuido.** O assunto continua
> `{2,5}` com fecho OPCIONAL: dos 193 assuntos com cauda de 1 dígito, os 193
> eram código de 2+ dígitos **cortado no teto de 255** (`DIREITO PREVIDENCIÁRIO
> (1` é `(195`). Abrir 1 dígito ali gravaria `1` onde o código é `195` — trocar
> campo vazio por campo ERRADO é pior. **Não mexa no regex do assunto.**

### O tamanho do buraco (amostra uniforme por pk, semente `20260829`)

200.000 pks sorteados em `[3.520, 105.980.219]`; 195.741 existiam:

| | amostra | % | ≈ acervo (103,7 M) |
|---|---:|---:|---:|
| com `classe_nome` | 67.569 | 34,5% | ~35,8 M |
| … e `classe_codigo` **vazio** (o buraco) | **4.440** | 2,27% | **~2,35 M** |
| … **recuperável** pelo regex novo | **3.477** | 1,78% | **~1,84 M** |
| … que o regex ANTIGO também pegaria | **0** | 0% | **0** |
| … abstenções (nome sem código nenhum) | 963 | — | ~510 k |

**100% do recuperável era o `{2,5}`** — os 3.477 têm código de UM dígito, e o
texto é `PROCEDIMENTO COMUM CÍVEL (7)` (2.604), `[CÍVEL] PROCEDIMENTO COMUM
CÍVEL (7)` (753, TJMG) ou `Procedimento Comum Cível (7)` (120). Por tribunal:
TJMG 753 · TJRJ 611 · TRF3 563 · TJMA 426 · TJMT 287 · TRF1 220 · TJCE 174 ·
TJPA 119 · TJPE 113 · TRF5 108 · TJDFT 102.

### 🔴 O regex estava certo no disco e ERRADO na memória por 4 dias

O commit é de **25/08 02:07 UTC**. Os 5 drainers da `.103` tinham subido às
**25/08 01:42 UTC** — 25 minutos ANTES. Bind mount entrega o arquivo, **Python
não recarrega**: até 29/08 eles seguiam aplicando `{2,5}` com o `{1,5}` no
disco, dentro do mesmo container.

Provado **dentro do processo**, lendo `/proc/<pid>/mem` (o disco não serve de
prova aqui) — os 5 drainers tinham 0 ocorrências de `(\d{1,5})\)\s*$` e um
processo novo, criado no mesmo container, tinha 2. O sintoma nos DADOS batia:
em 27, 28 e 29/08, amostras de 30.000 processos recém-enriquecidos ainda tinham
1.264 / 184 / 444 linhas com `classe_nome` terminando em `(d)` e
`classe_codigo` vazio. Depois do `restart` dos 5: o padrão novo aparece na
memória dos 5 e o antigo some.

Receita (roda no host da `.103`, precisa de `sudo`):

```bash
# qual regex está VIVO no processo — não no arquivo
PID=$(docker inspect -f '{{.State.Pid}}' voyager-enrichment_drainer-1)
sudo python3 - "$PID" <<'PY'
import sys
alvo = rb'(\d{1,5})\)\s*$'
pid = sys.argv[1]
regioes = []
for ln in open(f'/proc/{pid}/maps'):
    f = ln.split()
    if 'r' not in f[1]:
        continue
    lo, hi = (int(x, 16) for x in f[0].split('-'))
    if hi - lo < 512 * 1024 * 1024:
        regioes.append((lo, hi))
n = 0
with open(f'/proc/{pid}/mem', 'rb', 0) as m:
    for lo, hi in regioes:
        try:
            m.seek(lo); n += m.read(hi - lo).count(alvo)
        except Exception:
            pass
print('ocorrencias do regex NOVO:', n)   # 0 = processo com código velho
PY
```

**O controle positivo é obrigatório**: rode a mesma sonda num processo recém-
criado que importe o módulo. Sem ele, "0 ocorrências" tanto pode ser código
velho quanto sonda quebrada.

### O reparo: `backfill_classe`

`enrichers/management/commands/backfill_classe.py` — **zero requisição de
rede**, o dado está no nosso banco. Retomável (checkpoint em Redis), com kill
switch, teto de linhas, teto de tempo, freio por ms/linha, freio pela fila do
`es_index` e freio por sessões esperando `Lock`.

⚠️ **NÃO use `docker exec -d` no `web` para a corrida.** As sessões de `exec`
morrem quando alguém mexe no compose, e foi assim que os 4 primeiros shards
sumiram em 29/08 **sem deixar erro nenhum no log** — o mesmo modo de falha já
documentado em `OPS.md`. Suba um container PRÓPRIO (`docker run -d` com a
mesma imagem, `.env` e bind mount); a receita está em
[`OPS.md`](OPS.md#backfill-de-classe_codigo--operar).

```bash
# medir sem escrever NADA
manage.py backfill_classe --de 42809753 --ate 42849753 --dry-run --json

# a corrida (sem --de continua do checkpoint)
manage.py backfill_classe --sleep 0.1

# parar de qualquer lugar, sem deploy
manage.py shell -c "from django.core.cache import cache; \
    cache.set('enrichers:backfill_classe:off', True)"
```

Ele **toca a campainha** (`atualizado_em = now()` no mesmo UPDATE): `auto_now`
não roda em SQL cru e `sync_processos_atualizados` é keyset por
`atualizado_em`, então sem isso a classe ficaria certa no banco e velha na
busca. Ver `tests/test_backfill_classe.py` e `tests/test_campainha_sync.py`.

**Leitura estreita por default.** `WHERE classe_nome <> '' AND classe_codigo =
''` em vez de ler toda linha com nome: os vereditos `nada`/`conflito`/`fk_orfa`
exigem `classe_codigo <> ''` por construção, então a leitura larga só serve
para contar denominador — e denominador de proporção se mede por **amostra
uniforme de pk**, não varrendo tudo. `--com-denominador` liga a leitura larga.

### Antes × depois — a corrida inteira (29/08/2026)

**Mesmos 200.000 pks, mesma semente `20260829`, mesmas 195.741 linhas
existentes** — não é amostra nova, é a MESMA régua medida duas vezes:

| | antes | depois |
|---|---:|---:|
| com `classe_nome` | 67.569 | 67.844 |
| com `classe_codigo` | 63.136 | **66.864** (+3.728) |
| **buraco** (`classe_nome` cheio, `classe_codigo` vazio) | **4.440** | **987** (−77,8%) |
| … **recuperável pelo regex** | **3.477** | **0** |
| abstenções (nome sem código nenhum) | 963 | 987 |
| `classe_codigo` cheio e `classe_id` NULL (fora de escopo) | 15.499 | 15.485 |

**1.852.508 processos ganharam `classe_codigo`** — contra a projeção de ≈1,84 M
feita pela amostra ANTES de escrever a primeira linha. O que sobra do buraco
(≈523 k projetados) é abstenção: nome sem código nenhum no texto, onde inventar
seria pior que deixar vazio.

Dez shards, 4-6 simultâneos, ~1 h 40 min de relógio. **0 deadlock retentado, 0
espera de freio, 0 sessão do banco em `wait_event_type='Lock'` em 15 amostras
ao longo da corrida, e a fila do `es_index` em 0 o tempo todo** (o tique de 10
min drena mais rápido do que a campainha enche: ~370 k linhas por tique contra
um teto de 600 k).

| shard | faixa de pk | escritos | segundos |
|---|---|---:|---:|
| a + a1 + a2 + a3 | 0 – 26.500.000 | 1.008.149 | ~5.400 |
| b + b1 + b2 + b3 | 26.500.000 – 53.000.000 | 589.483 | ~4.700 |
| c | 53.000.000 – 79.500.000 | 89.594 | 2.210 |
| d | 79.500.000 – 105.980.219 | 108.219 | 3.380 |

A faixa de pk baixo concentra o dano (54% dos reparos em 25% dos pks): é a era
TRF1/TJMG, quando o enricher era quase toda a ingestão.

Custo medido em produção (fatia `id ∈ (42809753, 42849753]`, 40.000 pks):

| | |
|---|---:|
| buraco na fatia, antes | 3.719 |
| buraco na fatia, depois | **0** |
| escritos | 3.719 em 34,3 s = **9,2 ms/linha** |
| linhas com `atualizado_em` nos 20 min seguintes | **3.719** (a campainha, conferida) |
| deadlocks | 0 |
| sessões do banco esperando `Lock` durante a corrida | **0** (12 amostras) |

O plano é `Index Scan` na pkey com filtro no heap — ~18.000 buffers por bloco
de 20.000 pks, metade lidos do disco. É I/O, não CPU. **Sharding escala quase
linear** (medido: 3 blocos frios em 5,9 s em série contra 2,4 s em 3 threads;
6 blocos em 2,9 s) — a storage tem profundidade de fila. Cada shard tem
checkpoint próprio (`--shard <nome>`); as faixas TÊM que ser disjuntas, o
comando não coordena shards.

### O freio mede varredura, não densidade — e a 1ª versão errou nisso

Medido em 59 blocos reais de produção:

| métrica | p50 | p90 | máx | amplitude |
|---|---:|---:|---:|---:|
| **ms por 1.000 pks varridos** | 315 | 567 | 1.093 | **3,5×** |
| ms por linha escrita | 8,4 | 16,7 | 95,9 | **25×** |

O custo aqui é a LEITURA, e a densidade de linhas quebradas muda por faixa —
então ms/linha escrita mede DENSIDADE, não custo. Com ele, o shard `d` parou
sozinho a 37,56 ms/linha numa faixa que tinha 1.319 recuperáveis em 2.011
lidas: nada estava caro, só havia pouco a consertar. É o mesmo erro que a 1ª
versão do backfill de assunto cometeu ao medir duração de bloco. Hoje o freio é
`--freio-ms-kpk` (1.500) / `--parar-ms-kpk` (3.000) e roda em **todo** bloco —
inclusive nos que não escrevem nada, que são justamente os que escapavam do
`if n_escritos:`.

### O que o backfill NÃO conserta (medido, e de propósito)

- **FK órfã de quem já tem código.** 15.499 de 63.136 processos com
  `classe_codigo` preenchido (24,5%) têm `classe_id` **NULL** — ≈ **8,2 M
  linhas**, 4,5× este trabalho. É o território de `repop_classe_assunto`;
  emendar os dois dobraria a campainha (8,2 M reindexações) sem ter medido o
  impacto. **Contado, não consertado.**
- **`(7)` pendurado no nome de quem já tem o código certo.** Na fatia de 40.000
  pks: 5.515 nomes terminavam em `(7)` mas só 3.719 estavam sem código — os
  outros ~1.800 têm `classe_codigo = '7'` e o `(7)` no texto. É duplicação
  cosmética, não perda de dado; reescrevê-los somaria ~39% de escrita e de
  campainha por zero informação nova.
- **Conflito** (o código guardado difere do código no texto): 335 na mesma
  fatia. Ali dois escritores discordam sobre QUAL é a classe, e escolher no
  chute seria pior que deixar como está (regra nº 6).

## Filas per-tribunal

```python
RQ_QUEUES = {
    'default':         ...,       # fallback
    'enrich_trf1':     ...,       # 4 workers dedicados
    'enrich_trf3':     ...,       # 4 workers dedicados
    'djen_ingestion':  ...,
    'djen_backfill':   ...,
}
```

Helper `enrichers.jobs.enqueue_enriquecimento(pid, sigla)` roteia pra `enrich_<sigla.lower()>`. Auto-enqueue em `djen/ingestion.py` ao bulk_create de novos `Process` usa esse helper. Filas separadas evitam que TRF3 (volume alto, mais lento) sufoque TRF1.

## Comandos

```bash
# Foreground (debug, roda inline)
docker compose exec web python manage.py enriquecer_processo <CNJ_ou_ID>

# Async (vai pra fila do tribunal)
docker compose exec web python manage.py enriquecer_processo <CNJ> --async

# Bulk: todos pendentes do TRF3
docker compose exec web python manage.py enriquecer_pendentes --tribunal TRF3 --limit 0

# Reprocessar erros (proxy ruim, rate limit) do TRF1
docker compose exec web python manage.py enriquecer_pendentes --tribunal TRF1 --status erro --limit 0

# Funde Partes mascaradas em reais (one-shot, idempotente)
docker compose exec web python manage.py consolidar_partes_mascaradas --dry-run --limit 100
docker compose exec web python manage.py consolidar_partes_mascaradas

# Via dashboard: botão "↻ Atualizar dados públicos" no detalhe do processo
```

## PJe novo (SPA + REST) — caso TJDFT

Alguns tribunais já migraram do PJe clássico (JSF/Seam, form `fPP`,
`javax.faces.ViewState`) pra uma SPA Angular consumindo REST API Spring
Boot. **`BasePjeEnricher` não funciona nesse caso** — não há HTML form
pra parsear. Detecção: GET na URL `listView.seam` redireciona pra um
domínio `pje-consultapublica*.tjxxx.jus.br` cujo body é um shell Angular
(`<title>Consulta pública · Processo Judicial Eletrônico</title>`,
bundles `main-*.js`).

Padrão das rotas (caso TJDFT, validado 2026-05-26):

```
GET /v1/processos?page=0&numeroProcesso=<CNJ_FORMATADO>
    → result[0].idProcesso (token opaco URL-safe ~70 chars)

GET /v1/processos/{idProcesso}/dados
    → { classeJudicial, assunto (hierárquico), orgaoJulgador,
        dataDistribuicao (ISO8601), jurisdicao, endereco, ... }

GET /v1/processos/{idProcesso}/poloAtivo?page=N
GET /v1/processos/{idProcesso}/poloPassivo?page=N
GET /v1/processos/{idProcesso}/outrosInteressados?page=N
    → result: [ { participante (texto), nome, tipo, procuradoria, ... } ]
      pageInfo: { current, last, size, count }
```

Wrapper de resposta é uniforme:
`{ "status":"ok", "code":"200", "messages":[...], "result":..., "pageInfo":? }`.
Quando `status != "ok"`, levante erro — sem fallback.

Implementação fica em `enrichers/tjdft.py` (referência). Pontos-chave:
- Headers `Referer` + `Origin` apontando pra SPA oficial — sem isso o ALB
  pode devolver 403 em alguns endpoints.
- `participante` é o texto cru `"NOME - OAB UF<num> - CPF: ... (TIPO)"`.
  Reutilize `parse_documento` / `parse_oab` dos parsers compartilhados.
- O `tipo` da API (AUTOR/REU/ADVOGADO/FISCAL DA LEI/INTERESSADO/...)
  determina se a entrada é principal ou advogado. Agrupe advogados
  subsequentes como `representantes` do principal anterior — mesmo
  contrato que `BasePjeEnricher._parse_polo`.
- Assunto vem hierárquico (`"RAIZ (cod) - FILHO (cod) - ... - FOLHA (codFolha)"`);
  pegar **só o último segmento** pra entrar no catálogo `Assunto`.
- `dataDistribuicao` é ISO; converter pra `DD/MM/YYYY` antes de publicar
  (drainer usa `parse_data_br`).
- `valor_causa` **não** vem na API pública do TJDFT (limitação aceita).

Como mapear endpoints de um TJ novo nesse formato:
1. Abra a SPA, faça uma busca real (Playwright ou DevTools).
2. Capture os XHRs com `browser_network_requests` filtrando pelo
   subdomínio `*-api.*`.
3. Compare os campos do JSON com a tabela do `Process` (classe,
   assunto, orgao_julgador, data_autuacao) — costuma ser 1:1 com o caso
   TJDFT mas mudam nomes de campos.

## Justiça do Trabalho (TST + 24 TRTs) — `consultaprocessual` (recon 2026-07-04)

Registrados (migration `0040_seed_trts`) e **ativos** (ingestão DJEN roda; DJEN
carrega trabalhista via `sigla_djen='TRTn'/'TST'`). **Enrichment ainda NÃO ligado.**

Recon da consulta pública (SPA Angular `pje.trt<N>.jus.br/consultaprocessual/`,
API `/pje-consulta-api/api`):

| Tier | Endpoint | Dados | Captcha |
|---|---|---|---|
| **A** | `GET /processos/dadosbasicos/{cnj20dig}` + header `X-Grau-Instancia: 1\|2` | `[{id, numero, classe(abbrev), codigoOrgaoJulgador, segredoJustica}]` | ❌ aberto |
| **B** | `GET /processos/{id}` | partes/valor/movimentos | ✅ **captcha de imagem** (`tokenDesafio`+`imagem` JPEG) |

- **Grau**: OOOO do CNJ = `0000` → 2º grau (TRT), senão 1º grau (vara). Empty (204) → tentar o outro.
- **Alcance direto** (probe dummy-CNJ): 🟢 400/aberto: TRT 2,4,6,8,10,11,14,16,17,18,19,20,21,24 · 🟡 403 datacenter (precisa pool/Cortex): TRT 1,5,7,9,12,13,15,22,23 · ⚠️ TRT3=405 (checar path).
- **Tier B exige CapSolver** (ImageToText). Estratégia econômica: enrich rico só nos **leads classificados** (não nos ~40M). Sem saldo CapSolver → adiado.
- **classe vem do DJEN** também (`preencher_classe_via_djen`) — Tier A é redundante p/ classe; só agrega `codigoOrgaoJulgador`.
- ⚠️ **Scheduler**: ativar muitos tribunais estourava `hour = 4 + idx//2` (>23). Corrigido p/ janela madrugada com módulo (`djen/scheduler.py`, fix 2026-07-04).

## Como adicionar enricher pra outro PJe

1. Criar `enrichers/<sigla>.py` (15 linhas):
   ```python
   from .pje import BasePjeEnricher
   class TrfNEnricher(BasePjeEnricher):
       BASE_URL = '...'
       LIST_URL = f'{BASE_URL}/...'
       DETALHE_PATH = '/...'
       TRIBUNAL_SIGLA = 'TRFN'
       LOG_NAME = 'voyager.enrichers.trfN'
   ```
2. Adicionar em `enrichers/jobs.py::_ENRICHERS`.
3. Adicionar em `djen/ingestion.py::TRIBUNAIS_COM_ENRICHER` pra ativar auto-enqueue.
4. Adicionar fila `enrich_trfN` em `core/settings.py::RQ_QUEUES`.
5. Adicionar serviço `worker_trfN` em `docker-compose-prod.yml` com `replicas: 4`.
6. Restart scheduler pra registrar daily cron.

## Como adicionar enricher pra outro e-SAJ (TJSP, TJAL, ...)

e-SAJ é idêntico entre tribunais — só muda o host. `BaseEsajEnricher`
(`enrichers/esaj.py`) tem toda a lógica; o split do CNJ é por segmento
(independente de tribunal). Caso de referência: **TJAL** (2026-05-30).

1. Subclasse em `enrichers/esaj.py` (3 linhas):
   ```python
   class TjalEnricher(BaseEsajEnricher):
       BASE_URL = 'https://www2.tjal.jus.br'   # host do e-SAJ do TJ
       TRIBUNAL_SIGLA = 'TJAL'
       LOG_NAME = 'voyager.enrichers.tjal'
   ```
2. `enrichers/jobs.py::_ENRICHERS` + import.
3. `djen/ingestion.py::TRIBUNAIS_COM_ENRICHER` (auto-enqueue). Dimensione a fila
   antes pra tribunal de volume alto — TJSP entrou aqui em 2026-05-30 já com
   `worker_tjsp` em 60 réplicas (auto-enqueue só pega Process novos por janela
   diária; o backlog drena via `reabastecer_filas_enriquecimento`, capado em
   `QUEUE_HIGH_WATER`).
4. Fila `enrich_<sigla>` em `core/settings.py::RQ_QUEUES`.
5. Serviço `worker_<sigla>` em `docker-compose-workers.yml` (ramp 10 réplicas).
6. Seed migration `update_or_create` o `Tribunal` com `ativo=False`.
7. Ativação em prod: `djen_descobrir_inicio <sigla>` → flip `ativo=True` →
   backfill (ver .ia/OPS.md). Botão de enrich manual: condição no template
   `dashboard/templates/dashboard/processo_detail.html`.

Limitação herdada do TJSP: e-SAJ público mascara CPF/CNPJ → `documento` vazio
(OAB e nome preservados).

> **Gotcha `tipo` vs `papel` (corrigido 2026-06-10):** a tabela e-SAJ
> `#tablePartesPrincipais` traz o **papel** processual (Exeqte/Reqdo/Agravante/
> Apelado/...). Esse valor vai pra `ProcessoParte.papel` (uppercased) — **nunca**
> pra `Parte.tipo`. `Parte.tipo` é a categoria canônica
> (`pf`/`pj`/`advogado`/`desconhecido`), derivada de doc/oab via
> `classificar_tipo_parte`. Bug original: `esaj._extrair_partes` gravava o papel
> cru em `tipo`, poluindo o donut "Distribuição por tipo" da `/dashboard/partes/`
> com centenas de papéis e fragmentando Partes sem-doc por papel (o lookup de
> dedupe usa `tipo` na chave). Como o e-SAJ mascara doc, quase toda pessoa vira
> `desconhecido` (sem doc não dá pra distinguir pf/pj) — correto. Limpeza dos
> dados históricos: `manage.py recategorizar_tipo_partes` (ver .ia/OPS.md).

### 1º vs 2º grau (cpopg / cposg)

`BaseEsajEnricher` roteia por grau automaticamente: **foro de origem `OOOO == '0000'`
⇒ 2º grau** (processo originário do tribunal — agravos, recursos na Presidência,
competência originária). 1º grau usa `/cpopg/`; 2º grau usa `/{CPOSG_PATH}/`
(**TJSP = `cposg`**, **TJAL = `cposg5`** — varia por tribunal, override na subclasse).

Sem isso, todo processo de 2º grau caía em falso "não encontrado" (o cpopg só
tem 1º grau). Os dois grais são e-SAJ clássico (mesmo HTML); diferem só em:
- search param do CNJ: 1g `dadosConsulta.valorConsultaNuUnificado`, 2g `dePesquisaNuUnificado`;
- selectors do detalhe: 1g `foroProcesso`/`varaProcesso`/`dataHoraDistribuicao`/`valorAcao`;
  2g `secaoProcesso`/`orgaoJulgadorProcesso`/`relatorProcesso` (sem data/valor).
Partes (`#tablePartesPrincipais`) são idênticas nos dois.

## Stream sharded (drainer × N)

### Por que shard

O drainer original era **single-replica** porque múltiplas instâncias deadlocavam:
o XREADGROUP do Redis distribui entries aleatoriamente entre consumers, e dois
drainers podiam pegar events do **mesmo `process_id`** (uma re-publicação após retry,
por exemplo) e competir em `DELETE FROM tribunals_processoparte WHERE processo_id=…`
seguido de `INSERT`. Resultado: PG deadlock detector mata um dos lados.

A consequência operacional era throughput hard-cap em ~1k entries/min — pra cada
~100k events publicados, o drainer ficava 1.5h atrás. Sob carga pesada (re-fix do
backfill TRF3, deploy do TJMG) a lag chegou a 460k entries.

### Como funciona

Cada `process_id` é hashado (`process_id % STREAM_PARTITIONS`) pra escolher uma
das N partições. Workers publicam direto na partição certa via
`stream.publish(payload)` — a função olha `payload['process_id']` e escolhe o
stream físico:

```
voyager:enrichment:results:0
voyager:enrichment:results:1
voyager:enrichment:results:2
voyager:enrichment:results:3
```

Cada drainer roda com `--partition I` e consome **apenas** seu stream físico.
**O mesmo `process_id` SEMPRE cai no mesmo drainer**, então as serializações de
`DELETE+INSERT` por proc continuam sequenciais (sem deadlock entre drainers).
Entre `process_id`s diferentes, os 4 drainers paralelizam.

`STREAM_PARTITIONS=4` (vide `enrichers/stream.py`). Mudar este valor exige
quiescer o pipeline (parar workers + drenar streams existentes) — senão events
publicados sob N antigo ficam órfãos em partições que ninguém lê.

### Stream legado

`voyager:enrichment:results` (sem suffix) é o **stream legado** — usado antes
do shard. O serviço `enrichment_drainer` (sem suffix) continua processando-o
até que `XLEN` chegue a zero, momento em que pode ser desligado:

```bash
ssh ubuntu@192.168.30.100 redis-cli XLEN voyager:enrichment:results
# quando = 0:
ssh ubuntu@192.168.30.103 docker compose -f docker-compose-prod.yml stop enrichment_drainer
```

### Operação

```bash
# Ver lag por partição
for p in 0 1 2 3; do
  redis-cli -h 192.168.30.100 XLEN voyager:enrichment:results:$p
done

# Stats do consumer group de uma partição
redis-cli -h 192.168.30.100 XINFO GROUPS voyager:enrichment:results:0
```

### Capacity model

Drainer único → 1k entries/min. Com 4 partições + drainer dedicado por shard,
throughput nominal = 4k/min. Limite real é PG write-throughput em
`tribunals_processoparte` (DELETE+INSERT em batch + UPSERT em catálogos
`Parte`/`ClasseJudicial`/`Assunto`).

Sob 4× a carga, observar `pg_stat_activity` filtrado por
`wait_event_type='Lock'` — se contention crescer, considerar:
1. Aumentar `STREAM_PARTITIONS` (hot redeploy: drenar + reconfigurar).
2. Trocar `wipe + reinsert` de partes por `INSERT … ON CONFLICT DO UPDATE`.
3. Particionar `tribunals_processoparte` por hash(processo_id).

### Rollback emergencial (modo `--partition all`)

Se algum shard tiver bug grave em produção (ex: `apply_event` falhando só
pra partição N), pode-se voltar imediatamente pra topologia 1-drainer
sem refactor:

```bash
# Para 4 dos 5 services sharded
docker compose -f docker-compose-prod.yml stop \
  enrichment_drainer_p0 enrichment_drainer_p1 \
  enrichment_drainer_p2 enrichment_drainer_p3

# Reconfigura o legacy pra processar TUDO (round-robin entre legado + 4 shards)
docker compose -f docker-compose-prod.yml \
  run -d --name voyager-enrichment_drainer-rollback \
  enrichment_drainer python manage.py enrichers_drain --partition all \
  --batch-size 1000 --block-ms 500
```

Modo `all` faz round-robin entre todos os streams num único drainer.
Reintroduz a possibilidade de deadlock (mesmo problema do drainer
pré-shard) — usar **só pra rescue de curto prazo** enquanto se diagnostica
o bug do shard. Tempo: ~2min pra ativar. Sem perda de dados.

### Monitoramento (TODO follow-up)

- **Alerta por partition**: cron a cada 5min checa `XLEN voyager:enrichment:results:I`
  e se `> 5_000` por 3 ciclos consecutivos, envia alerta. Sem isso, lag
  numa partição é invisível pro usuário do dashboard.
- **Auto-stop legacy**: cron checa se `XLEN voyager:enrichment:results == 0`
  por 30min consecutivos, então `docker stop voyager-enrichment_drainer-1`.
  Manual hoje — risco de zombie consumindo entries antigas indefinidamente.

### Out-of-order safety

`apply_batch` (drainer.py:680) tem guard: se `proc.enriquecido_em >=
event.scraped_at`, o event é descartado (contado em `skipped`). Isso protege
contra:
- Re-publicação tardia do legacy stream sobrescrevendo dados frescos
  do shard
- Click manual ("Atualizar dados públicos") chegando antes de um batch
  agendado mais antigo

Dentro do mesmo batch, dedupe por `process_id` mantém o event de
`scraped_at` mais recente (drainer.py:662).

## Kill-switch por tribunal (pausa)

Quando um tribunal bloqueia o método de acesso (ex.: **TJRO/TJAP** devolvendo
`403` pra todos os proxies em 2026-07 — WAF/bloqueio total), continuar tentando
só queima proxy/CPU e enche `enriquecimento_erro`. Pausa via cache Redis
(`enrich:pausados`, sem TTL — sobrevive a restart), honrada em 3 pontos:
refill (`_reabastecer_impl` loga `pausado`), `enqueue_enriquecimento` (no-op) e
o job `enriquecer_processo` (retorna `{skip: pausado}` SEM tocar no status → a
fila residual drena sem bater na fonte; o processo segue `pendente`).

```bash
python manage.py enrich_pausa TJRO TJAP    # pausa
python manage.py enrich_pausa --off TJRO   # despausa (refill repõe sozinho)
python manage.py enrich_pausa --list       # mostra pausados
```

## Watermark no auto-enqueue (não inflar fila)

O `reabastecer_filas_enriquecimento` respeita `QUEUE_HIGH_WATER=100_000`, mas o
**auto-enqueue da ingestão** (`djen/ingestion.py`, quando cria processo novo)
NÃO respeitava — sob backfill isso inflou filas a MILHÕES (TJRO 3,6M, TJRJ 2,6M
em 2026-07-11). Agora o auto-enqueue checa `len(queue) < QUEUE_HIGH_WATER`
(gate cacheado 30s por sigla); acima do teto deixa pro refill (o processo já
fica `pendente`, nada se perde).

## Runbook: fila de enrichment inflada

1. `enrich_pausa <SIGLA>` se o tribunal está bloqueado (403/WAF total).
2. Esvaziar a fila (não perde nada — pendentes seguem no DB, refill repõe):
   ```python
   # manage.py shell
   import django_rq; django_rq.get_queue('enrich_tjro').empty()
   ```
3. Re-tentar erros após corrigir a causa:
   `manage.py enriquecer_pendentes --tribunal <SIGLA> --status erro --max-tentativas 3 --limit 0`

## AWS WAF challenge (PJe) — o desafio NÃO é do IP (corrigido 29/08/2026)

Alguns PJe (o caso medido é o **TJPE**, `pje.cloud.tjpe.jus.br/1g/`) ficam atrás
de AWS WAF cuja ação é `challenge`: devolvem `HTTP 202` com o header
`x-amzn-waf-action: challenge` e a página `awsWafCookie`/`challenge.js` no lugar
do form JSF.

**A leitura antiga era "bloqueio POR PROXY, intermitente — rotaciona até achar
um IP que passa". Ela está errada, e custava caro.** Sonda de 29/08/2026, mesmo
path, mesmo User-Agent:

| caminho | desafiadas |
|---|---|
| Cortex residencial (IP novo a cada request) | **29 de 30** |
| IP direto do host de workers `.102`, sem proxy | **16 de 20** |
| IP direto do host web `.103`, sem proxy | 0 de 2 |
| pool datacenter ProxyScrape | não chega (ProxyError/403) |

Trinta IPs residenciais distintos levaram 29 desafios: **trocar de IP não sai do
desafio**. E o User-Agent também não discrimina — `voyager-ops/0.1` e um UA de
Firefox foram desafiados na mesma proporção (o teste que parecia mostrar
diferença estava medindo a loteria do IP de saída do Cortex, não o UA).

O código tratava o desafio como 403: rotacionava `MAX_PROXY_ROTATIONS` (10)
vezes e chamava `pool.mark_bad()` em cada uma. O pool é **COMPARTILHADO** com a
ingestão DJEN e os outros 15 enrichers, então cada job do TJPE tirava até 10 IPs
de circulação por 120 s — para gravar `erro` do mesmo jeito. Censo de 10 min na
`.102` (29/08/2026, todas as réplicas):

| tribunal | requisições/10 min | `mark_bad`/10 min | erro | ok | nao_enc |
|---|---:|---:|---:|---:|---:|
| **TJPE** | 2.791 | **1.379** | 129 | **7** | 1 |
| TJRJ | 2.972 | 1.231 | 22 | 40 | 193 |
| TJCE | 1.978 | 822 | 18 | 94 | 19 |
| TJMA | 1.426 | 583 | 11 | 82 | 0 |
| TJRO | 938 | 469 | 48 | **0** | 0 |

O TJRJ requisita mais e é o **maior consumidor legítimo** (233 desfechos úteis
de 255). O TJPE gastava quase o mesmo para 7 `ok` — 95% de refugo.

**Comportamento atual** (`enrichers/pje.py`):
- `_detectar_desafio_waf(resp)` — header `x-amzn-waf-action` ∈ {challenge,
  captcha}, **ou** corpo com `awswaf`/`challenge.js` num status que o PJe nunca
  usa para conteúdo (202/405/403/429). Um 200 legítimo que só *cita* o WAF não
  conta (o detector antigo, "`awswaf` no corpo", confundia os dois).
- Desafio tem **teto próprio** (`WAF_MAX_TENTATIVAS = 3`, sobrescritível por
  subclasse), **não gasta as 10 rotações** e **nunca chama `mark_bad`** — o IP
  não é ruim, o site é que está desafiando todo mundo.
- Bater o teto é `PjeWafChallenge`, que vira `erro` **com o número real** no log
  (`ERROR: AWS WAF: 3 de 3 respostas foram desafio em TJPE`) e aciona
  `WAF_CHALLENGE_SLEEP_SECONDS = 30` de freio antes do próximo job. Sem esse
  freio o job falha em ~0,6 s e o worker pega o seguinte na hora — foi assim que
  14 réplicas fizeram 2.791 requisições em 10 min.
- 403/429 **sem** assinatura de WAF segue como antes: é bloqueio do IP mesmo,
  rotaciona e marca ruim.

**Resolver o desafio (rodar o `challenge.js`, montar o cookie do WAF) é evasão
anti-bot e NÃO está autorizado** (decisão de produto, 25/08/2026 — a mesma que
barra solver de captcha no eproc/TJSP). Então o teto acima é o fim da linha: o
que passar da fatia que o WAF deixa passar, passa; o resto fica `erro`.

Bloqueio TOTAL de verdade (TJRO/TJAP: **0** proxies passam, `ok = 0` em 1,09 M
de processos) → aí sim `manage.py enrich_pausa <SIGLA>`.

Regressão: `tests/test_waf_challenge_nao_queima_pool.py`.

---

## Plano de cobertura por VALOR (Juriscope) — 2026-08-06

**Contexto.** Auditoria da cobertura de enriquecimento do acervo (75,6M): ~50M sem
enriquecimento completo. Brute-force é inviável — o Datajud é **1 APIKey pública
compartilhada** (teto ~100 rpm, ~15s/job na API do CNJ → ~94/min → 278 dias só o
Track A). **Decisão: enriquecer por VALOR, não por contagem** — só os tribunais que o
Juriscope de fato lê (precatório vira lead lá).

**Alvo = 10 tribunais do Falcon/Juriscope + TJPR** (add do usuário). Corta 50M → **~14M**.
Falcon (`datamodel_process` @ 10.10.0.51): TJSP, TRF1, TRF3, TRF4, TJMG, TRF6, TRF5,
TJAL, TRF2, TJMA (2,43M precatórios conhecidos).

### O buraco no alvo
```
Track A — datajud (sem enricher): TJPR 5,83M · TRF4 2,28M · TRF6 1,02M · TRF2 0,86M   → ~10,0M
Track B — enricher (PJe/e-SAJ):   {TJSP,TRF1,TRF3,TRF5,TJMG,TJAL,TJMA}
                                   20,5M tot · 54% ok · pendente 2,78M · erro 1,14M    → gap ~3,9M
TOTAL ALVO ≈ 13,9M  (vs ~50M do acervo inteiro)
```
Nota: isso DESprioriza os maiores backlogs de enricher (TJRJ 1,3M/386k-erro, TJPE, TJPA,
TJMT, TJCE) — Juriscope não cobre esses.

### Fases
- **Fase 0 — alvo fino (pré-filtro DJEN).** Dentro do alvo, enriquecer PRIMEIRO os CNJ
  cujo texto DJEN (já ingerido, sem custo) menciona precatório/requisitório/RPV/ofício.
  Troca "74 dias pro Track A" por "dias pro que importa".
- **Fase 1 — Track A priorizado.** Refill do datajud puxa por (alvo-Juriscope → sinal-DJEN
  → newest), não FIFO/spread cego. Teto de 100 rpm é permanente (APIKey única) → foco em
  gastar a quota no que vale. TJPR é o grosso (5,8M).
- **Fase 2 — Track B higiene.** (a) requeue dos **1,14M em erro** (muito é transitório de
  proxy/WAF; parte é parser — ver fix ViewState TJPE/TRF1); (b) escalar scrapers dos
  maiores do alvo sob o kill-switch existente. Não mexer nos não-alvo.
- **Fase 3 — instrumentação.** Página de **cobertura por tribunal** (%ok, pendente, erro,
  datajud-null) — hoje só há freshness (lag), não cobertura. Responde "quais faltam"
  continuamente. Barato; fazer primeiro.

### Restrições firmes
- Datajud = 1 APIKey compartilhada → **sem lever de rate-limit** (não existe key dedicada).
  Escala do Track A = priorização, não throughput.
- Enricher = proxy/captcha/WAF por tribunal; kill-switch + watermark já existem.

### Execução (2026-08-09) — o que saiu
- **Fase 1 ✅**: refill datajud prioriza o alvo (fila 92% TJPR/TRF4/TRF6/TRF2). Bug
  pego: `order_by('-inserido_em')` por-tribunal = 35s → timeout 300s; fix = sem sort.
- **Fase 3 ✅**: `/dashboard/tribunais/cobertura/` (warm 6h). Gap do alvo = 13,9M.
- **Fase 2 ✅**: `tick_requeue_erros_enricher` (10min) devolve erro→pendente nos 7
  tribunais Juriscope∩enricher (SEM floor — senão pulava os grandes). Erro caindo
  ~50k/batch (1,14M→…). reabastecer só pega 'pendente', por isso os erros ficavam presos.
- **Fase 0 ⛔ (não é quick-win)**: pré-filtro por `classificacao` NÃO serve nos
  tribunais datajud — TJPR tem 4,2M classificados mas **lead=0** (sem datajud o
  classificador não acha precatório: chicken-egg). Pré-filtro real = flag DJEN-texto
  denormalizado + backfill (migration + scan de movimentações) → item próprio.

---

## Valor da causa: onde existe e onde NÃO existe (triagem 2026-08-13)

**Pergunta que motivou.** Só 5 UFs têm `valor_causa` no ES; TJRJ (3,46M), TJMA
(2,70M), TJPE, TJCE e TJAP estão em 0,0%. Hipótese inicial: "falta parser nesses
enrichers" (os arquivos `enrichers/tjrj.py` etc. não mencionam "valor").

**Veredicto: a hipótese é FALSA — e escrever parser não preencheria nada.**

`BasePjeEnricher._extrair_dados` (`enrichers/pje.py`, ramo
`elif 'valor' in chave and 'causa' in chave`) **já** extrai o valor; os 5
enrichers são subclasses de 13 linhas e herdam isso. O ramo nunca dispara porque
**a consulta pública do PJe clássico não publica o valor da causa**.

### Quem publica o valor é o SISTEMA, não o tribunal

| Sistema | Campo na fonte | Valor? | Tribunais |
|---|---|---|---|
| e-SAJ | `#valorAcaoProcesso` (só 1º grau/cpopg) | **Sim** | TJSP, TJAL, TJAC |
| REST próprio (TJPA) | `valorCausaFormatado` | **Sim** | TJPA |
| REST próprio (TJMT) | `valorCausa` | **Sim** | TJMT |
| **PJe consulta pública** (clássico e SPA) | — não existe — | **Não** | TJRJ, TJMA, TJPE, TJCE, TJAP, TJMG, TJRO, TRF1, TRF3, TRF5, TJDFT |
| **Datajud** (API CNJ) | `valorCausa` **ausente do `_source`** | **Não** | todos os acima |

O detalhe do PJe consulta pública expõe exatamente 6 campos em `div.propertyView`:
Número Processo · Data da Distribuição · Classe Judicial · Assunto · Jurisdição ·
Órgão Julgador. Fim. A string "valor" **não aparece uma única vez** no HTML
inteiro (70–140 KB por página).

### Evidência (probes reais, 2026-08-13)

- **16 processos reais** buscados ao vivo pelo próprio enricher (search + detalhe,
  via Cortex), 5 tribunais: `ocorrencias_de_"valor"_no_HTML_CRU = 0` em **todos**.
  Fixtures de detalhe reais no repo pros 5 (TJPE/TJAP capturadas nesta triagem).
- **Datajud**: 14 CNJ dos 5 tribunais → `valorCausa=None` e a chave sequer existe
  no `_source` (`['@timestamp','assuntos','classe','dataAjuizamento',...]`).
- **Prod, amostra por `(tribunal, -id)` com LIMIT** (nunca `valor_causa__isnull`
  solto — vira seq scan no tabelão): 0/300 com valor em TJRJ, TJMA, TJPE, TJCE,
  TJAP **e também em TRF1, TRF3, TJMG** (mesma base PJe). Contraprova no mesmo
  corte: TJPA 300/300, TJMT 288/300, TJSP 183/300.

⇒ O ramo de valor em `pje.py` é **código morto em todos os PJe**. Mantido de
propósito: se um tribunal passar a publicar, ele volta a funcionar sozinho.

### Quanto custaria obter mesmo assim

Não sai de graça: exigiria **uma requisição extra por processo** num sistema
*diferente* (portal legado do tribunal), cada um com layout próprio e captcha.
Para TJRJ+TJMA isso é ~6,2M requisições adicionais sob o mesmo orçamento de
proxy/WAF que hoje já limita o enriquecimento básico — e reprocessar o que já
temos **não preencheria um único registro**, porque o dado nunca esteve no
payload. Só faz sentido com decisão explícita de custo (captcha-solver + scraper
novo por tribunal), não como conserto de parser.

### Onde vale investir (se o objetivo é cobertura de valor)

Nos tribunais **e-SAJ e REST próprio** — é onde o dado existe. TJSP está em
183/300 (~61%): fechar essa lacuna (2º grau não traz valor; ver §cpopg/cposg)
rende mais que qualquer coisa nos 5 PJe.

### Regressão

`tests/test_valor_causa_pje.py` trava a constatação com as fixtures reais dos 5:
se algum tribunal passar a publicar o valor, o teste de ausência quebra e avisa
que dá pra ligar a extração. Cobre também o formato BR do `parse_valor_brl`
(milhar `.` vs decimal `,` — inverter erra por 1000×) e o contrato
**sem valor → `None`, nunca `Decimal('0')`** (0 afirmaria "a causa vale R$ 0,00").

---

## Auditoria de completude do DADO — matriz tribunal × campo (24–25/08/2026)

Pergunta diferente da do acervo. Não é "temos o processo?" (ver
[`ACERVO_CNJ.md`](ACERVO_CNJ.md)) — é **"dos processos que já temos, extraímos
tudo que a fonte dá?"**. A resposta curta: **não, e o maior buraco não é de
scraping — é de dado já baixado que ninguém promove**.

### Como foi medido (amostra, semente, custo)

Nada aqui usa `exists` do ES (regra nº 4 do CLAUDE.md: string vazia conta como
presente) nem `reltuples`. Todo número sai de **conteúdo**, por amostra.

| # | amostra | tamanho | como | custo |
|---|---|---:|---|---|
| A | matriz de campos (PG) | **120.000 processos** | 3.000 âncoras uniformes em `id ∈ [3.520, 104.602.261]`, `random.Random(20260824)`, bloco de 40 pks consecutivos por âncora (`WHERE id >= a ORDER BY id LIMIT 40`) | 38,7 s |
| B | partes | 11.160 (200/tribunal) + **todos os 17.376 `ok`** da amostra A | `processoparte ⋈ parte` por `processo_id = ANY(...)` | 5 s + 47 s |
| C | destinatários DJEN | 3 movimentações mais recentes de cada um dos 11.160 | `LATERAL … ORDER BY data_disponibilizacao DESC LIMIT 3` | 18,5 s |
| D | portas | 20 processos reais, 1 por tribunal, priorizando lead | probe ao vivo no Datajud, 1 req/2 s | 40 s |
| E | ES × PG | `_mget` dos mesmos 11.160 em `voyager-processos` | resposta exata por id, nunca `exists` | 6 s |
| F | e-SAJ ao vivo | 11 processos TJSP `ok` **sem partes** | `open.do` + `search.do` diretos, 3 s entre requisições | ~1 min |

**Controle positivo da sonda** (verificação com input vazio reporta sucesso):
a função de "preenchido" foi rodada contra 2 linhas sintéticas — uma cheia, uma
com `-` / `não informado` / `0` / `N/A` — e reportou 50% em todos os campos,
classificando cada placeholder pelo valor. Sem esse controle, "0,0%" não
distingue "campo vazio" de "sonda quebrada".

**Aferição da amostra A contra a contagem exata do acervo** (102.296.406):

| status | contagem exata | amostra A | erro |
|---|---:|---:|---:|
| pendente | 76,2% | 77,27% | +1,1 pp |
| ok | 15,2% | 14,48% | −0,7 pp |
| nao_encontrado | 7,0% | 6,74% | −0,3 pp |
| erro | 1,6% | 1,52% | −0,1 pp |

⇒ o desenho por conglomerados (40 pks vizinhos) tem efeito de desenho ≈ **1 pp**.
Números abaixo de ~2 pp de diferença entre tribunais **não são achado**.

### A matriz (% dos processos com o campo REALMENTE preenchido)

Vazio = `NULL`, string vazia **ou** placeholder (`-`, `não informado`, `0`,
`N/A`, …). Nenhum placeholder foi encontrado nos 120.000 — os campos deste
banco são cheios ou vazios de verdade, nunca "cheios de nada".

| tribunal | acervo est. | n | ok% | classe cód | assunto cód | assunto nome | autuação | valor | órgão cód | órgão nome | juízo | ≥1 parte | dos `ok`, com parte |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **TJSP** | 17.254.846 | 20241 | 10.0 | 24.8 | 4.9 | 13.4 | 13.0 | 7.7 | 6.2 | 13.5 | 10.4 | 6 | 82 |
| **TJMG** | 7.815.445 | 9168 | 32.1 | 55.2 | 17.4 | 41.4 | 41.5 | 0.0 | 18.2 | 18.2 | 0.0 | 36 | 99 |
| **TJPR** | 6.943.369 | 8145 | 0.0 | 1.2 | 1.2 | 1.2 | 1.2 | 0.0 | 1.2 | 1.2 | 0.0 | 0 | · |
| **TJRJ** | 5.624.597 | 6598 | 15.7 | 36.9 | 0.3 | 18.1 | 18.1 | 0.0 | 0.4 | 0.4 | 0.0 | 21 | 100 |
| **TJRS** | 5.082.426 | 5962 | 0.0 | 3.6 | 3.6 | 3.6 | 3.4 | 0.0 | 3.6 | 3.6 | 0.0 | 0 | · |
| **TRF3** | 4.941.769 | 5797 | 63.4 | 70.5 | 3.6 | 67.9 | 68.0 | 0.0 | 4.7 | 68.0 | 0.0 | 69 | 100 |
| **TRF4** | 3.352.765 | 3933 | 0.0 | 21.2 | 21.2 | 20.8 | 21.2 | 0.0 | 21.2 | 21.2 | 0.0 | 0 | · |
| **TJSC** | 3.314.404 | 3888 | 0.0 | 3.6 | 3.6 | 3.6 | 3.6 | 0.0 | 3.6 | 3.6 | 0.0 | 0 | · |
| **TJMA** | 2.909.480 | 3413 | 17.1 | 38.1 | 24.7 | 41.5 | 41.2 | 0.0 | 30.1 | 41.6 | 0.0 | 17 | 100 |
| **TJMT** | 2.653.739 | 3113 | 29.9 | 43.3 | 21.2 | 21.1 | 43.9 | 29.7 | 21.2 | 43.9 | 0.0 | 32 | 100 |
| **TRF1** | 2.457.671 | 2883 | 74.1 | 82.0 | 15.5 | 74.8 | 74.8 | 0.0 | 13.0 | 74.8 | 0.0 | 76 | 98 |
| **TJBA** | 2.365.604 | 2775 | 0.0 | 8.3 | 8.3 | 8.3 | 8.3 | 0.0 | 8.3 | 8.3 | 0.0 | 0 | · |
| **TJGO** | 2.090.257 | 2452 | 0.0 | 24.1 | 24.1 | 23.9 | 24.1 | 0.0 | 24.1 | 24.1 | 0.0 | 0 | · |
| **TJPA** | 1.954.714 | 2293 | 18.8 | 33.5 | 28.3 | 28.3 | 28.4 | 20.5 | 10.1 | 28.4 | 20.5 | 21 | 100 |
| **TJCE** | 1.931.697 | 2266 | 11.5 | 37.0 | 28.1 | 36.7 | 36.7 | 0.0 | 29.7 | 36.7 | 0.0 | 12 | 100 |
| **TJRR** | 1.705.793 | 2001 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0 | · |
| **TRF5** | 1.695.563 | 1989 | 86.4 | 91.3 | 23.8 | 90.8 | 88.4 | 0.0 | 44.2 | 59.3 | 0.0 | 85 | 100 |
| **TRF6** | 1.630.775 | 1913 | 0.0 | 20.8 | 20.8 | 20.8 | 20.6 | 0.0 | 20.8 | 20.8 | 0.0 | 0 | · |
| **TJPE** | 1.421.920 | 1668 | 12.1 | 41.6 | 6.8 | 20.7 | 20.4 | 0.0 | 7.8 | 20.8 | 0.0 | 18 | 100 |
| **TRT2** | 1.413.395 | 1658 | 0.0 | 3.4 | 3.4 | 3.4 | 3.4 | 0.0 | 3.4 | 3.4 | 0.0 | 0 | · |
| **TRT15** | 1.335.821 | 1567 | 0.0 | 6.5 | 6.5 | 6.5 | 6.5 | 0.0 | 6.5 | 6.5 | 0.0 | 0 | · |
| **TJMS** | 1.291.492 | 1515 | 0.0 | 19.1 | 19.1 | 19.1 | 19.1 | 0.0 | 19.1 | 19.1 | 0.0 | 0 | · |
| **TJAM** | 1.279.558 | 1501 | 0.0 | 88.2 | 33.4 | 33.4 | 33.4 | 0.0 | 33.4 | 33.4 | 0.0 | 0 | · |
| **TST** | 1.278.705 | 1500 | 0.0 | 17.2 | 17.2 | 17.2 | 17.2 | 0.0 | 17.2 | 17.2 | 0.0 | 0 | · |
| **TJSE** | 1.227.557 | 1440 | 0.0 | 7.8 | 7.8 | 7.8 | 7.8 | 0.0 | 7.8 | 7.8 | 0.0 | 0 | · |
| **TJPI** | 1.177.261 | 1381 | 0.0 | 10.3 | 10.3 | 10.3 | 10.3 | 0.0 | 10.3 | 10.3 | 0.0 | 0 | · |
| **TJDFT** | 1.099.686 | 1290 | 72.6 | 95.4 | 82.7 | 82.7 | 82.7 | 0.0 | 70.9 | 82.7 | 0.0 | 70 | 100 |
| **STJ** | 1.098.834 | 1289 | 0.0 | 54.0 | 1.4 | 1.4 | 1.4 | 0.0 | 1.4 | 1.4 | 0.0 | 0 | · |
| **TRT1** | 1.088.604 | 1277 | 0.0 | 2.4 | 2.4 | 2.4 | 2.4 | 0.0 | 2.4 | 2.4 | 0.0 | 0 | · |
| **TJPB** | 1.069.850 | 1255 | 0.0 | 23.8 | 23.8 | 23.8 | 23.8 | 0.0 | 23.8 | 23.8 | 0.0 | 0 | · |
| **TJRN** | 1.064.735 | 1249 | 0.0 | 2.3 | 2.3 | 2.3 | 2.3 | 0.0 | 2.3 | 2.3 | 0.0 | 0 | · |
| **TRF2** | 1.043.423 | 1224 | 0.0 | 23.2 | 23.2 | 23.0 | 22.5 | 0.0 | 23.2 | 23.2 | 0.0 | 0 | · |
| **TJRO** | 1.008.472 | 1183 | 0.0 | 38.5 | 1.9 | 1.9 | 1.9 | 0.0 | 1.9 | 1.9 | 0.0 | 0 | · |
| **TJAL** | 715.222 | 839 | 46.4 | 93.3 | 42.6 | 84.1 | 85.6 | 45.1 | 78.5 | 84.4 | 49.5 | 47 | 88 |
| **TJAP** | 316.266 | 371 | 7.0 | 67.7 | 31.0 | 37.5 | 37.5 | 0.0 | 34.5 | 37.5 | 0.0 | 16 | 100 |
| **TJAC** | 136.395 | 160 | 42.5 | 100.0 | 61.3 | 81.9 | 78.1 | 20.6 | 77.5 | 81.2 | 36.9 | 37 | 78 |

---

## Vazão: onde o tempo vai e o que estava comendo a frota (medição 25/08/2026)

Pergunta que abriu: "por que o enriquecimento está tão lento?". A resposta não
era escala — era **desperdício**. Todos os números abaixo foram medidos em
produção, não estimados.

### O tempo de um job é REDE. Parse é ruído; banco é zero.

Cronômetro por fase dentro de um container da `.102` (mesma rota de rede dos
workers), 5–6 processos `pendente` por tribunal, amostra aleatória semente
`20260824`, `stream.publish` no-op:

| tribunal | mediana/job | rede busca (soma) | rede detalhe | parse | % rede |
|---|---|---|---|---|---|
| TRF1 | 1,29 s | 4,18 s | 3,69 s | 0,08 s | 98,4% |
| TRF3 | 1,49 s | 4,18 s | 4,85 s | 0,07 s | 99,2% |
| TRF5 | 2,19 s | 9,77 s | 8,62 s | 0,09 s | 99,4% |
| TJMG | 7,90 s | 51,91 s | 5,31 s | 0,07 s | 99,9% |
| TJMA | 2,63 s | 13,09 s | 5,36 s | 0,06 s | 99,7% |
| TJCE | 2,03 s | 6,10 s | 5,67 s | 0,07 s | 99,7% |
| TJPE | 2,72 s | 25,70 s | 4,26 s | 0,06 s | 99,9% |
| TJPA | 1,09 s | 10,28 s | — | 0,00 s | 99,8% |
| TJMT | 0,63 s | 12,80 s | — | 0,00 s | 100% |
| TJRJ | 0,65 s | 2,53 s | 1,87 s | 0,03 s | 100% |
| TJDFT | **0,095 s** | 0,18 s | 0,07 s | 0,00 s | 62,5% |
| TJRO | 8,95 s | 49,42 s | — | 0,00 s | 100% (tudo `erro`) |

**Banco não aparece porque o worker não toca o Postgres**: ele publica o
resultado no stream Redis e o drainer escreve. Otimizar parser ou banco não
compra vazão nenhuma aqui — o que compra é *menos requisição desperdiçada* e
*IP que a fonte aceita*.

O TJDFT é a régua do que dá pra ser: API REST, 0,095 s por processo, **35,9 mil
jobs/h com 2 réplicas**. PJe clássico custa 3 viagens HTTP (GET listView → POST
pesquisa → GET detalhe) e nunca desce de ~1 s.

### O gargalo real: 77,1% das raspagens repetiam trabalho já enfileirado

`_reabastecer_impl` enche cada fila até `QUEUE_HIGH_WATER` (100.000). Com a
frota atual — **146 workers de enrich, 146 ocupados, 0 ocioso** — 100k jobos
significam **20 a 74 horas de espera na fila** (medido pelo `enqueued_at` do job
na frente). E o `Process` só sai de `pendente` quando o drainer aplica o
resultado, lá no fim dessa espera. Resultado: a cada 2 minutos o refill
re-seleciona a MESMA cabeça do índice e enfileira de novo.

Censo COMPLETO das 16 filas (todos os ids, não amostra), 00:20 UTC:

| fila | ids | process_id distintos | cópias | máx. de 1 pid | desperdício |
|---|---|---|---|---|---|
| enrich_tjmt | 99.818 | 1.418 | 70,4 | 136 | 98,6% |
| enrich_tjac | 99.976 | 1.929 | 51,8 | 607 | 98,1% |
| enrich_tjap | 99.968 | 3.426 | 29,2 | 515 | 96,6% |
| enrich_tjal | 99.993 | 6.433 | 15,5 | 849 | 93,6% |
| enrich_tjro | 99.979 | 8.486 | 11,8 | **1.926** | 91,5% |
| enrich_tjpa | 99.952 | 10.130 | 9,9 | 832 | 89,9% |
| enrich_tjma | 99.927 | 10.711 | 9,3 | 564 | 89,3% |
| enrich_tjce | 99.954 | 12.008 | 8,3 | 336 | 88,0% |
| enrich_tjpe | 99.942 | 12.895 | 7,8 | 499 | 87,1% |
| enrich_trf5 | 99.948 | 31.546 | 3,2 | 535 | 68,4% |
| enrich_trf3 | 99.939 | 35.265 | 2,8 | 672 | 64,7% |
| enrich_trf1 | 99.935 | 35.675 | 2,8 | 484 | 64,3% |
| enrich_tjdft | 100.767 | 54.277 | 1,9 | 32 | 46,1% |
| enrich_tjmg | 99.947 | 74.931 | 1,3 | 192 | 25,0% |
| enrich_tjrj | 251.534 | 251.534 | 1,0 | 1 | **0,0%** |
| enrich_tjsp | 227.678 | 227.503 | 1,0 | 2 | **0,1%** |

**1.400.007 jobos para 321.130 processos distintos nas 14 filas que o refill
administra = 77,1% de cópia.** As duas filas limpas são exatamente as que estão
ACIMA do teto — o refill as PULA, e a duplicação desaparece. O teto redondo de
100k não era o total da fila: era a fábrica de cópia.

O drainer não é gargalo e prova isso: aplica **126.264 events/h** (4 partições,
5 min de log) com `skipped_idempotent` ≈ 0 e `XLEN` das partições entre 0 e 44.
O trabalho chega — é que dois terços dele já tinham sido feitos.

### O efeito colateral: a cópia queima o pool, que é compartilhado

Cada job que falha gasta até 10 IPs (`MAX_PROXY_ROTATIONS`), e cada 403 chama
`pool.mark_bad` com TTL de 120 s. Estado do pool medido: **2.500 IPs na lista,
289 saudáveis (11,6%), 2.211 no `bad_zset`, `degraded=1`, `fail_streak=11.011`**.
Os três tribunais de rendimento ~zero (TJRO, TJAP, TJAL) sozinhos queimavam
~58 mil IP/h; a TTL de 120 s sustenta ~1.930 IPs marcados a qualquer instante —
praticamente o `bad_zset` inteiro. Ou seja: o desperdício de um tribunal degrada
a coleta de **todos** os outros.

### Bug: o escalonamento pro Cortex nunca escalou

`BasePjeEnricher._request_with_rotation` declarava `bloqueios_dc = 0` e
consultava `force_cortex=bloqueios_dc >= 3` — **e nunca incrementava a
variável**. O ramo residencial era inalcançável: com 2.500 IPs no pool, as 10
rotações jamais o esgotam, então o fallback do fim de `_next_proxy` também
nunca é atingido. Tribunal que bloqueia datacenter terminava 100% em `erro` sem
nunca ter tentado o residencial. O `BaseEsajEnricher` não tinha o escalonamento
de forma alguma.

Prova A/B (mesmos 5 pids, semente `20260824`, um container da `.102`):

| tribunal | rota | mediana | desfechos |
|---|---|---|---|
| TJAP | pool datacenter | 7,91 s | **erro 5/5** |
| TJAP | Cortex residencial | **0,95 s** | ok 1 + nao_encontrado 4, **0 erro** |
| TJAL | pool datacenter | 3,42 s (73,1 s no total) | ok 5/5 |
| TJAL | Cortex residencial | **1,22 s** (17,0 s no total) | ok 5/5 |
| TJRO | pool datacenter | 13,91 s | erro 5/5 |
| TJRO | Cortex residencial | 8,28 s | **erro 5/5** |

TJAP não estava bloqueado na fonte — `curl` direto e via Cortex devolvem HTTP
200. Estava bloqueado **no caminho que o código insistia em usar**.

### TJRO: bloqueio total, e zero `ok` em 1,09 milhão de processos

`curl` ao `pjepg-consulta.tjro.jus.br` devolve **403 pelo IP direto E pelo
Cortex residencial**. O balanço do tribunal: `pendente` 720.344 · `erro`
367.176 · **`ok` 0** · `nao_encontrado` 1. O enricher do TJRO nunca entregou um
único processo. É o caso exato do kill switch — que estava com a lista vazia.

### Desfecho por tribunal (15 min de log dos 146 workers, ×4 = por hora)

| tribunal | jobs/h | ok/h | nao_encontrado/h | erro/h | % ok |
|---|---|---|---|---|---|
| TJDFT | 34.184 | 25.892 | 8.148 | 0 | 75,7% |
| TJRJ | 21.892 | 4.956 | 16.860 | 0 | 22,6% |
| TJMT | 16.864 | 16.152 | 668 | 0 | 95,8% |
| TJSP | 10.580 | 64 | 10.480 | 0 | **0,6%** |
| TJCE | 5.380 | 3.972 | 1.396 | 0 | 73,8% |
| TJMA | 5.988 | 5.100 | 860 | 0 | 85,2% |
| TJPE | 4.352 | 3.168 | 1.040 | 128 | 72,8% |
| TJAP | 3.648 | 0 | 0 | 3.648 | **0%** |
| TJMG | 3.844 | 3.092 | 748 | 0 | 80,4% |
| TRF1 | 3.352 | 3.228 | 116 | 0 | 96,3% |
| TRF3 | 3.072 | 2.988 | 64 | 0 | 97,3% |
| TJPA | 3.060 | 2.592 | 0 | 456 | 84,7% |
| TRF5 | 2.496 | 2.432 | 52 | 0 | 97,4% |
| TJRO | 1.388 | 0 | 0 | 1.388 | **0%** |
| TJAL | 944 | 44 | 0 | 900 | **4,7%** |

> **TJSP com 0,6% de `ok` é achado aberto e NÃO é problema de vazão.** 10.480
> processos por hora sendo marcados `nao_encontrado` (que é TERMINAL pós
> 2026-07-06) enquanto o mesmo enricher, na mesma máquina, acerta 6 de 6 numa
> amostra da cabeça do índice. A população que está na fila do TJSP é outra
> (CNJ sequenciais `4xxxxxx-…2026.8.26.…`). Isso é qualidade/parser — passado
> pra auditoria de qualidade, não corrigido aqui.

### O que mudou no código

1. **`enrichers/pje.py`** — `bloqueios_dc += 1` no ramo de 403/WAF de proxy do
   pool. O escalonamento pro Cortex passa a existir de fato.
2. **`enrichers/esaj.py`** — `_next_proxy` ganha `force_cortex` e os dois laços
   de rotação (`_buscar_em_grau`, `_fetch_incidente`) contam bloqueios de
   datacenter e escalam após 3. Antes o e-SAJ não tinha escalonamento nenhum.
3. **`enrichers/jobs.py::_reabastecer_impl`** — duas travas:
   - `_profundidade_alvo()`: o teto da fila deixa de ser 100k fixo e passa a ser
     `ENRICH_QUEUE_ALVO_S` (1.800 s) de trabalho, calculado pela vazão REAL da
     fila (`len(FinishedJobRegistry)/result_ttl`, dado que o RQ já mantém).
     `QUEUE_HIGH_WATER` continua como teto duro; `ENRICH_QUEUE_MIN` (2.000) como
     piso pra fila lenta não secar.
   - `_filtrar_em_voo()`: ZSET `enr:inflight:<sigla>` com os `process_id` já
     enfileirados, podado por score a cada passada. Um pid só volta pra fila
     depois de `ENRICH_INFLIGHT_TTL_S` (3.600 s). O refill agora lê MAIS
     candidatos que a capacidade (`capacidade + em_voo + 1.000`, teto 60k) —
     senão, com a cabeça do índice inteira em voo, ele não avançaria.
4. **`enrichers/jobs.py::tick_requeue_erros_enricher`** — teto de
   `REENRICH_MAX_TENTATIVAS` (12) raspagens por processo. Sem ele o tick era
   esteira infinita: o processo 24179591 (TJAL) estava em
   `enriquecimento_tentativas=360`. Conforme a regra nº 2 do CLAUDE.md, o teto
   é **alerta** — loga em ERROR quantos processos ficaram estacionados por
   tribunal, nunca corta em silêncio.

### O que NÃO é o gargalo (conferido, pra não voltar a ser investigado)

- **Workers ociosos**: `Worker.all()` → 146 de 146 workers de enrich em `busy`.
  Os "145 jobs rodando" do relato eram a frota inteira trabalhando.
- **Drainer / Postgres**: 126.264 events/h aplicados, `XLEN` das 4 partições
  entre 0 e 44, `skipped_idempotent` ≈ 0.
- **Kill switch preso**: `enrich:pausados` estava **vazio** — nada pausado.
- **Código velho**: `FailedJobRegistry` em **0 nas 16 filas**; frota reciclada
  em 24/08 (ver OPS.md).
- **Parser**: 0,06–0,09 s por 6 jobs, entre 0,1% e 1,6% do tempo.

### Backlog real por tribunal (contagem por índice, 25/08/2026)

| sigla | pendente | erro | ok | nao_encontrado |
|---|---|---|---|---|
| TJSP | 12.101.245 | 0 | 1.634.135 | 2.591.568 |
| TJMG | 3.556.408 | 0 | 2.479.816 | 2.255.297 |
| TJRJ | 3.394.492 | 594.533 | 1.051.748 | 1.040.036 |
| TJMA | 2.252.663 | 0 | 440.799 | 69.661 |
| TJPA | 1.688.451 | 157.920 | 329.324 | 1 |
| TJMT | 1.547.426 | 14.797 | 638.987 | 77.903 |
| TRF3 | 1.517.936 | 0 | 3.450.446 | 344.925 |
| TJCE | 1.243.006 | 23.027 | 252.843 | 70.116 |
| TJPE | 869.550 | 361.591 | 213.551 | 130.296 |
| TJRO | 720.344 | 367.176 | **0** | 1 |
| TRF1 | 450.672 | 0 | 1.917.351 | 89.754 |
| TJAL | 247.478 | 61 | 362.899 | 54.053 |
| TJAP | 175.063 | 105.959 | 20.533 | 7.515 |
| TRF5 | 70.629 | 0 | 1.697.025 | 112.705 |
| TJAC | 64.030 | 9.556 | 49.589 | 32.768 |
| TJDFT | 58.387 | 5.483 | 997.955 | 329.738 |
| **soma** | **29.957.780** | **1.640.103** | **15.537.001** | **7.206.337** |

> `erro = 0` nos 7 tribunais Juriscope não quer dizer que não há erro: o
> `tick_requeue_erros_enricher` devolve todos a `pendente` a cada 10 min.

### A conta do horizonte

- Pendentes no país: **77.923.013**. Em tribunais **com** enricher:
  **29.957.780** (38,4%). Os outros **47.965.233 (61,6%)** estão em tribunais
  sem enricher (TJPR, TRF2/4/6, TRTs, …) e **o enricher nunca vai alcançá-los**
  — a porta deles é o Datajud, com APIKey pública a ~100 rpm ⇒ 144 mil/dia ⇒
  **≈ 333 dias**. Esse é o número desconfortável, e nenhum ajuste de worker
  muda: o limite é por chave, não por IP.
- Com enricher, somando `pendente + erro` = **31,6 milhões**. À taxa medida de
  processos distintos concluídos (41.571/h na melhor hora), **≈ 32 dias**; à
  média de 24 h (25.277/h), **≈ 52 dias**.
- Eliminando a cópia (o desperdício medido é 77,1% nas filas administradas pelo
  refill), a mesma frota, com a MESMA carga sobre as fontes, tende à vazão bruta
  de jobos já medida (~125 mil/h) ⇒ **≈ 11 dias**. É esse o tamanho da
  correção: uma ordem de grandeza, sem um worker a mais e sem uma requisição a
  mais no tribunal.

Colunas: `ok%` = `enriquecimento_status='ok'`; `≥1 parte` = % dos processos com
ao menos uma `ProcessoParte`; `dos ok, com parte` = entre os `ok`, quantos têm
parte (`·` = menos de 20 `ok` na amostra, não medido). Tribunais sem enricher
(TJPR, TJRS, TJSC, TJBA, TJGO, TJMS, TJPI, TJSE, TJRN, TJES, TJRR, TRF2/4/6,
TST, STJ, TRTs) têm `ok%`=0 por construção — o que preenchem vem do Datajud, e
as 6 colunas de campo mostram o **mesmo** percentual porque **o Datajud escreve
tudo de uma vez ou nada**. Onde `classe cód` descola das outras (TJAM 88,2 vs
33,4; STJ 54,0 vs 1,4; TJRO 38,5 vs 1,9; TJSP 24,8 vs 4,9), a diferença é a
classe herdada da movimentação DJEN.

Os 40 tribunais restantes (todos < 900 k, somando ~4,4 M) estão em
`scripts/auditoria_campos.py` — nenhum passa de 30% em campo nenhum.

### As três causas, separadas e medidas

Campo vazio exige ações **opostas** conforme a causa. Sobre a amostra A
(120.000, escalado para 102.296.406):

| causa | o que é | medida | ação |
|---|---|---:|---|
| **3 — nunca fomos buscar** | `pendente`, nenhuma porta consultada | **77,27%** ≈ 79,0 M · e **68,59%** ≈ 70,2 M não têm classe **nem** assunto **nem** autuação — 99,7% desses são `pendente` | volume/priorização (é o plano de cobertura por valor acima) |
| **2 — a fonte tem, nós não pegamos** | parser/mapa/política | ver os 7 achados abaixo — o maior sozinho vale **84,8%** do acervo | conserto nosso, sem gastar requisição |
| **1 — a fonte não tem** | dado inexistente no payload público | `valor_causa` em todo PJe (já provado em §"Valor da causa"); partes em processo sob segredo (10 de 11 sondas ao vivo no TJSP) | a tela precisa **dizer que não há** — hoje diz `ok` e mostra vazio |

O `ok` totalmente vazio (sem classe, sem assunto, sem autuação) é **0,17%**
≈ 174 k processos: pequeno, mas é "run verde com log limpo".

### Achado 1 — 84,8% do acervo tem partes no DJEN que nunca viram `Parte`

**O maior buraco desta auditoria, e ele não precisa de uma única requisição.**

`Movimentacao.destinatarios` e `Movimentacao.destinatario_advogados` (JSONB,
vindos da DJEN em toda comunicação) guardam parte + polo + advogado + OAB. O
grafo de entidades (`Parte`/`ProcessoParte`), que alimenta a ficha, a tela de
partes, o "quem deve" e os campos `partes`/`advs` do ES, é populado **apenas**
pelos enrichers — `grep -rn destinatario` não acha nenhum caminho de promoção.

Medido (amostra B+C, 11.160 processos):

```
sem nenhuma ProcessoParte ............... 10.046 / 11.160 = 90,0%
   … mas com destinatário DJEN gravado ..  9.467 / 10.046 = 94,2%
⇒ processos que ganhariam parte de graça  9.467 / 11.160 = 84,8%  ≈ 86,7 M
```

Qualidade do que está lá (23.575 destinatários + 21.930 advogados, 1 mov/processo):

| atributo | medida |
|---|---:|
| destinatário com **polo** (`A`/`P`) | 23.575 / 23.575 = **100%** |
| destinatário com nome real (não iniciais de segredo) | 22.590 / 23.575 = **95,8%** |
| advogado com **OAB** (número + UF) | 21.901 / 21.930 = **99,87%** |
| OABs distintas na amostra | 17.677 |

Exemplo cru (TRT8, `id=1324979864`): `[{"nome":"WANDERSON AMANCIO VIEIRA","polo":"A"},
{"nome":"BRASIL NORTE COM. REPRESENTACOES COMERCIAIS LTDA","polo":"P"}]` +
3 advogados com `numero_oab`/`uf_oab`.

Duas notas honestas:

- **Não substitui o enricher.** O destinatário é quem foi *intimado naquela
  comunicação*, não o cadastro completo de partes, e não traz CPF/CNPJ. É
  cobertura ampla e rasa; o enricher é estreita e profunda.
- `.ia/ACERVO_CNJ.md` já afirmava que "partes e advogados continuam vindo do
  DJEN (destinatários) e dos enrichers" — a primeira metade da frase **nunca
  foi verdade no código**. `SEARCH_SCHEMA.md` §Lacunas registra a decisão de não
  indexá-los "(ruído/duplicação vs ProcessoParte)"; a premissa dessa decisão era
  que `ProcessoParte` cobria o caso, e ela cobre **10,0%**.

#### O conserto: `backfill_partes_djen` (25/08/2026)

`tribunals/services/partes_djen.py` + `manage.py backfill_partes_djen`. Lê as 3
movimentações mais recentes de cada processo **sem nenhuma `ProcessoParte`**,
extrai destinatário e advogado dos dois JSONB e grava `Parte`/`ProcessoParte`
com `fonte='djen'`. Zero requisição a tribunal.

**Confirmação independente do achado** (semente 20260825, 6 âncoras × 600 pks
uniformes em `id ∈ [1, 104.602.261]`, contagem pelo lado do SQL): 2.311 de
3.584 processos sem `ProcessoParte`, e **2.172 = 94,0%** deles com destinatário
gravado — bate com os 94,2% da auditoria, por outro desenho de amostra.

**Custo medido, ponta a ponta: 1,26 s por 1.000 processos** (janela de pk +
`NOT EXISTS` em `tribunals_processoparte` + `LATERAL LIMIT 3` em
`tribunals_movimentacao`). Só leitura; a escrita entra no piloto.

Decisões que a medição forçou, e o motivo de cada uma:

| decisão | por quê |
|---|---|
| driver por **faixa fechada de `Process.id`**, não por tribunal | `proc_tribunal_id_idx` é `(tribunal_id)` no banco, não `(tribunal, -id)` como o model declara — ver `DATA_MODEL.md`. Sem esse índice, filtrar por tribunal e ordenar por `id` varre a PK inteira (um `LIMIT 1` ficou 1.318 s em produção). Faixa contígua também é o que torna o reindex do ES viável. |
| idempotência pela **constraint**, nunca por `DELETE` | tudo sai com `representa=NULL`, então `uniq_processo_parte_polo_papel_principal` cobre 100% das linhas e `bulk_create(ignore_conflicts=True)` é race-safe. O caminho do `apply_batch` (`DELETE … WHERE processo_id = ANY`) apagaria a linha do enricher, que tem CPF/CNPJ e papel. |
| só processos com **zero** `ProcessoParte` | quem já tem parte tem a de fonte melhor. Complementar criaria duplicata semântica: o enricher grava `papel='AUTOR'`, nós gravaríamos `papel=''` para a mesma pessoa. |
| `papel=''`, `documento=''`, `representa=NULL` | o DJEN não dá nenhum dos três. `papel` aparece em **10 de 23.771** destinatários (0,04%) e só nos que vêm dos coletores de `diarios/`. Rotular de `'DESTINATARIO'` inventaria um papel processual **e** tiraria a linha do escopo da constraint que o enricher usa. |
| reusa `enrichers.drainer._bulk_upsert_partes` | inclusive a armadilha do caminho `sem_id`, que casa o nome com uma `Parte` que já tenha CNPJ antes de criar — é o que evita duplicar "ESTADO DO AMAPÁ" 40 mil vezes. Medido: **6.398 de 6.398** `Parte` sem doc e sem OAB estão em `tipo='desconhecido'`, então a chave `(nome, tipo)` casa com o que já existe. |

**O formato real, medido — não presumido** (24 âncoras × 400 pks, semente
20260825; 23.771 destinatários e 26.569 advogados):

```
destinatarios[]           polo 23.771 · nome 23.771 · comunicacao_id 23.761 · papel 10
destinatario_advogados[]  advogado · id · advogado_id · comunicacao_id · created_at · updated_at
…[].advogado              nome · numero_oab · uf_oab (100%) · id
```

`polo` tem **quatro** valores, não dois: `A` 13.003 · `P` 10.519 · `T` 228 ·
`D` 21. `T`/`D` são terceiro/custos legis (Ministério Público, administradora
judicial) e vão para `outros`, junto com os advogados.

**OAB** — `Parte.oab` tem unique constraint parcial: formatar diferente do
resto do sistema duplicaria partes em 17,7 M de linhas. `formatar_oab` produz
exatamente o que `enrichers.parsers.parse_oab` produz (`UF` + número, sem
separador, sufixo de uma letra colado — a forma real em `tribunals_parte`:
`GO26464`, `RJ209212A`, `CE14458S`). Formatos encontrados nos 26.569:

| forma | n | tratamento |
|---|---:|---|
| só dígitos (`'6094'`) | 23.832 (89,7%) | `AP6094` |
| dígitos + letra (`'46601A'`) | 2.469 (9,3%) | `BA46601A` |
| **UF repetida** (`'MT4960'`, `'4960MT'`) | 103 (0,4%) | UF removida antes de concatenar — senão viraria `MTMT4960`, uma `Parte` nova para advogado que já existe |
| lixo (`'R'`, `'D'`, `'A1608'`, `'27467/O'`) | ~138 | abstém: o advogado entra pelo nome, sem OAB inventada |

Resultado: **99,48%** dos 26.569 saem com OAB formatada, 0 fora do padrão
`^[A-Z]{2}\d…`.

#### Guarda de segredo de justiça — o achado 3 mordendo o achado 1

**Parte inventada em processo sob segredo é pior que processo sem parte.** As
famílias abaixo saíram de medição em 12.592 destinatários distintos por
`(processo, nome, polo)`, não de suposição:

| família | n | % | exemplos REAIS |
|---|---:|---:|---|
| marcador textual | 598 | 4,75% | `SIGILO`, `SIGILO1`, `SIGILO 2`, `SIGILOSO`, `EM SEGREDO DE JUSTIÇA`, `SEGREDO DE JUSTIÃ§A` (mojibake), `PROCESSO ESTÁ EM SEGREDO DE JUSTIÇA - 1`, `PARTE/PROCESSO SIGILOSO OU …JUSTIçA.4` |
| só iniciais | 840 | 6,67% | `E.O.`, `W.V.D.`, `E. S. D. J.`, `A. H. DOS S.`, `F. S. O. DO B. L.`, e nomes de UMA letra: `I`, `M`, `N.` |
| **total descartado** | **1.588** | **12,61%** | |

Os 12,61% são **3× a estimativa da auditoria** (4,2%): ela mediu uma família
só. `SIGILO` sozinho aparece 358 vezes — sem a guarda, viraria **uma** entidade
colando 358 processos sem relação nenhuma.

**Regra que foi REJEITADA por medição:** `[Xx]{3,}` ("parece máscara") parecia
óbvia e teria descartado `MRV MRL XXXVIII INCORPORACOES SPE LTDA` — o único
nome dos 12.592 com 3 X seguidos é uma incorporadora com algarismo romano.
Máscara por `*` ou `X` **não existe** neste campo: 0 ocorrências em 12.592. Os
nomes de segredo aqui são texto explícito ou iniciais, nada mais.

Marcador textual ⇒ a fonte **afirmou** o segredo, e o contador
`com_marcador_segredo` registra. Iniciais ⇒ **não** afirmamos: iniciais também
aparecem em vara de família sem segredo decretado, e chutar é pior que vazio
(regra nº 6).

**Resíduo conhecido, não resolvido** (≈ 0,4% dos nomes mantidos): placeholders
que não são segredo nem parte — `INTEGRAçãO DJEN-TRF3` (23), `DIVóRCIO-IBGE`
(14), `PUBLICAÇÃO`, `OS MESMOS`, `VíTIMA`. Não entraram numa lista de bloqueio
porque uma lista de bloqueio inventada a partir de 5 exemplos erra mais do que
acerta. `fonte='djen'` torna a remoção posterior barata.

#### O piloto em produção (25/08/2026) — antes/depois, idempotência, dano

Três faixas contíguas de `Process.id`, escolhidas em tribunais **órfãos** (0%
de parte hoje, sem enricher e sem previsão de ter). Todos os números abaixo são
**contagem independente no banco**, nunca o log do job.

| faixa de `Process.id` | tribunal | processos | com parte ANTES | com parte DEPOIS | linhas criadas | linhas/processo |
|---|---|---:|---:|---:|---:|---:|
| `[57.000.000, 57.020.000)` | TJRS | 19.996 | **0** | **19.902 (99,53%)** | 83.316 | 4,2 |
| `[85.000.000, 85.002.000)` | TJSP | 2.000 | 7 | **2.000 (100%)** | 7.218 | 3,6 |
| `[60.000.000, 60.002.000)` | TRT4/TRT9 | 2.000 | **0** | **1.944 (97,20%)** | 14.482 | 7,2 |

Os que **não** ganharam parte fecham a conta item a item: 94 no TJRS e 56 no
TRT são exatamente os processos com movimentação e `destinatarios = []`. Nada
some no resto.

**Idempotência** — 4.983 processos já gravados, `promover_lote` chamado
DIRETO neles (não pelo comando, senão o filtro D2 pularia tudo e a prova seria
sobre o filtro, não sobre a constraint): 21.783 linhas oferecidas, **0 linhas
novas**.

**Zero dano** — `fonte IS NULL` intacto (as 39 linhas de enricher da faixa do
TJSP continuam lá); **0** linhas nossas com `papel`, **0** com `representa`;
**0** nome de segredo virou `Parte`. E **18.374** linhas apontaram para uma
`Parte` que já tinha CPF/CNPJ — o caminho `sem_id` do drainer reusando entidade
em vez de duplicar.

**Custo** (freio `--carga 0.5`, ou seja metade do tempo de parede é descanso):

| | leitura seca | com escrita, parede | de banco |
|---|---:|---:|---:|
| TJRS | 1,26 s/1.000 | 12,59 s/1.000 | ≈ 6,3 s/1.000 |
| TJSP | — | 15,71 s/1.000 | ≈ 7,9 s/1.000 |
| TRT | — | 22,8–25,9 s/1.000 | ≈ 11,4–13,0 s/1.000 |

O custo acompanha as **linhas por processo**, não os processos: o TRT custa 2×
o TJRS porque tem 7,2 linhas/processo contra 4,2.

**O que o piloto achou e a bancada não podia achar** — ver o commit
`55264d3`: o DJEN publica a MESMA OAB com e sem zero à esquerda, às vezes no
mesmo processo, e isso dobrava 21,5% dos advogados. Na amostra nacional só 0,4%
dos `numero_oab` vinham com a UF prefixada; no TJRS são 85,9%. **O formato
varia por tribunal** — é por isso que o piloto tem que ser em produção e num
tribunal de verdade.

**Kill switch, medido:** `manage.py backfill_partes_djen --pausar` (chave
`bf:partes_djen:pausado` no Redis, honrada a cada lote). Acionado às 02:08:57
com 4 shards rodando: **0 escritas nos 60 s seguintes**, e os 4 cursores
salvos (`bf:partes_djen:<shard>:cursor`) — retomável sem reler.

**Rollback por procedência, medido:** `DELETE FROM tribunals_processoparte
WHERE processo_id >= X AND processo_id < Y AND fonte='djen'`, em pedaços de
2.000 pks — **112.377 linhas em 3 s**, sem tocar em nenhuma linha de enricher.
É para isso que a coluna existe.

#### A régua de qualidade, antes de ligar o volume (02/09/2026, #121)

O backfill existia desde 25/08 e **nunca tinha sido ligado**. Promover 86,7 M
com parser errado é escrever lixo em escala, então a largada foi precedida de
uma medição contra a única fonte independente que temos: os 16 tribunais onde
o **enricher** já escreveu parte (PJe/eSAJ, com CPF/CNPJ e papel).

Desenho: semente 20260902, 24 âncoras × 3.000 pks, **900 processos que JÁ TÊM
`ProcessoParte` de `fonte IS NULL`**. Para cada um, roda-se `specs_do_processo`
exatamente como o backfill rodaria (`JANELA_MOVS=3`) e compara-se nome, polo e
OAB com o que o enricher gravou. 698 destinatários e 1.024 advogados.

| régua | resultado |
|---|---|
| nome bate no enricher | **654/698 = 93,7%** |
| polo idêntico (sobre os que batem por nome) | **632/654 = 96,6%** |
| OAB idêntica (sobre os pares COMPARÁVEIS) | **579/613 = 94,5%** |
| … casos em que o enricher NÃO tinha OAB | 256 — nós **acrescentamos** |
| papel preenchido | 13/698 = 1,9% (a fonte não rotula) |

**As 22 divergências de polo não são erro de parser — são FASE.** O JSONB diz
o polo da COMUNICAÇÃO; o enricher, o da AÇÃO. Conferido lendo o cabeçalho:

```
Process.id=81821397 (TJSP)
  enricher  Luis Artur Piloto REQTE/ativo · Banco do Brasil REQDO/passivo
  DJEN      "Recorrido: Luis Artur Piloto - Recorrente: Banco do Brasil S/A"
            → polo P para o autor e A para o banco — CERTO, para o recurso
```

O que **estava** errado é o que o dedupe por `(nome, polo)` fazia com isso. Em
`Process.id=2740674`, AGNALDO ISMAEL aparece como RECORRIDO numa movimentação
e como AUTOR na outra, e saíam **duas `ProcessoParte`**: a mesma pessoa no polo
ativo E no passivo do mesmo processo — um fato impossível, e uma delas
necessariamente errada. Desde 02/09, quando a janela se contradiz sobre alguém
o polo é **ABSTIDO** e a pessoa vai para `outros` (regra nº 6, e é onde os
advogados já moram, pelo mesmo motivo). Custa **1,16% dos nomes** e elimina 8
das 22 divergências. Conferido na faixa do piloto depois de escrever:

```
pares (processo, parte) com ativo E passivo, fonte='djen' .... 0
```

⚠️ **O 1,16% é da amostra, não da população que o backfill escreve.** A régua
foi medida onde HÁ enricher — e enricher existe justamente nos processos mais
litigiosos e multifásicos (566 dos 654 casos são TRF3, cheios de recurso
inominado). Remedido em 02/09 na população real do backfill (4.000 processos
com ZERO `ProcessoParte`, 16.338 specs): a abstenção cai para **0,11% dos
nomes e 0,35% dos processos**. O custo da regra é uma ordem de grandeza menor
do que a amostra sugeria — o que também quer dizer que a divergência de polo
que ela conserta é mais rara ali.

As outras 14 (2,1% dos 654) são janelas SÓ com publicação de recurso, onde o
DJEN é coerente consigo mesmo e não há contradição a detectar. Não são
corrigíveis com o dado que temos — e como o backfill só escreve onde NÃO há
outra fonte, o polo da comunicação é o melhor sinal disponível ali. Fica
registrado como caveat medido, não como defeito aberto.

**Sobra medida e NÃO consertada:** 34 advogados (3,9% dos que batem por nome)
têm OAB divergente, e a maioria é sufixo — `SP211735` (DJEN) vs `SP211735E`
(enricher, lido do texto "OAB SP 211735E"). Isso cria uma `Parte` paralela para
o mesmo advogado. Normalizar o sufixo foi descartado: `A`, `E`, `S`, `O` são
seccional/estagiário/suplementar de verdade em parte dos casos, e fundi-los
colaria advogados distintos. Trocaria um erro visível por um invisível.

#### Ligado em 02/09/2026 — o estado antes

Medido pelo vigia (`vigia_backfills --medir`), amostra de páginas de
`tribunals_process`:

```
cobertura de ProcessoParte ........ 18,66% ± 2,66 pp
… destas, com fonte='djen' ........  0,45%
faltam (estimado) ................. 87.458.787 processos
teto alcançável ................... 92,5% dos SEM parte têm destinatário
                                     nas 3 movs mais recentes
```

⇒ o teto do backfill é ≈ **93,9%** de cobertura (18,66 + 81,34 × 0,925), não
100%: processo que só entrou pela porta do Datajud não tem
`Movimentacao.destinatarios` e não ganha parte sem requisição externa. Este é
o número que o card publica ao lado da cobertura — sem ele a tela mostraria
"faltam 81%" para sempre.

### Achado 2 — classe e órgão da própria movimentação, não promovidos

Mesmo caminho, outro campo. `fallback_classe_via_djen` existe, mas só roda
dentro do drainer de enrichment — ou seja, nunca para os 77% `pendente`.

| | medida (11.160) | concordância com enricher/Datajud onde ambos existem |
|---|---:|---|
| processos com mov. que tem `codigo_classe` | 11.136 = **99,8%** | — |
| sem `classe_codigo` no `Process`, **com** código na mov. | 8.309 = **74,5%** ≈ 76,2 M | **91,0%** exato (2.572/2.827) |
| sem `orgao_julgador_nome`, **com** `nome_orgao` na mov. | 9.032 = **80,9%** ≈ 82,8 M | 58,7% exato, 69,4% incluindo contido |

⇒ **classe pode ser preenchida** (com carimbo de fonte: é a classe *da
comunicação*, e os 9% de divergência são exatamente incidente/fase distinta —
ex. `12078` no cadastro vs `436` na publicação). **Órgão NÃO deve**: o
`nome_orgao` do DJEN é quem publicou (`19ª - Salvador` vs
`19ª Vara Federal de Execução Fiscal da SJBA`; `Gab. 22 - JUÍZA FEDERAL
CONVOCADA…`). 30,6% de discordância é chute, e chute é pior que vazio.

### Achado 3 — o PJe corta o `)` do assunto e nós jogamos o código fora

O detalhe do PJe entrega o assunto hierárquico com o código da folha **sem o
parêntese de fecho**:

```
DIREITO PREVIDENCIÁRIO (195) - Benefícios em Espécie (6094) - Auxílio-Acidente (Art. 86) (6107
                                                                                          ↑ sem ')'
```

`drainer.CLASSE_COM_CODIGO_RE = ^(.+?)\s*\((\d{2,5})\)\s*$` exige o fecho ⇒
`assunto_codigo=''` e `assunto_nome` recebe **a hierarquia inteira**, truncada
em 255. A classe, que o PJe fecha direito (`PROCEDIMENTO COMUM CÍVEL (7)`),
passa — daí a assimetria `classe cód ≈ classe nome` mas `assunto cód ≪ assunto
nome` na matriz (TRF3: 70,5 vs 3,6).

| | amostra A | ≈ acervo |
|---|---:|---:|
| processos com `assunto_nome` | 26.841 (22,4%) | 22,9 M |
| … **sem** `assunto_codigo` | 13.299 (49,5% deles) | 11,3 M |
| … com o código da folha **recuperável do texto já gravado** | **9.667 (72,7%)** | **8,2 M** |
| … só com código de ancestral (nome truncado em 255) | 1.515 | 1,3 M |
| `assunto_nome` truncado em 255 | 1.920 | 1,6 M |

Prova de que o código está íntegro (só falta o `)`): dos 9.667 recuperados,
**9.503 (98,3%) já existem em `tribunals_assunto`**. Efeito colateral do mesmo
bug: **1.690 de 2.673 (63,2%)** dos nomes do catálogo `Assunto` são a hierarquia
inteira em vez do nome da folha — o dropdown de filtro mostra
`DIREITO ADMINISTRATIVO… (9985) - Atos Administrativos (9997) - Licenças (9998)`
como se fosse o nome. `ClasseJudicial` está limpo (0/658).

**Este conserto não precisa de rede**: o dado está no nosso banco.

### Achado 4 — `Jurisdição` do PJe: publicada, lida, descartada

`juizo` = 0,0% em **todos** os 10 tribunais PJe (TRF1, TRF3, TRF5, TJMG, TJMA,
TJCE, TJAP, TJPE, TJRJ, TJRO) e no TJDFT. Não é a fonte: o `div.propertyView`
tem o campo, conferido nas fixtures reais de 5 tribunais —
TJMG "Patrocínio" · TJRJ "Comarca da Capital" · TJPE "Recife - Varas" ·
TJMA "Fórum da Comarca de Riachão" · TJAP "Comarca de Laranjal do Jari - Interior".
`BasePjeEnricher._extrair_dados` ramifica em classe/assunto/autuação/valor/
segredo e **não tem ramo para jurisdição**; `tjdft.py` idem (a rota `/dados`
devolve `jurisdicao`). Só e-SAJ (TJSP/TJAL/TJAC) e TJPA escrevem `juizo`.

⚠️ **Não é um `elif` óbvio**: no e-SAJ `juizo` é a *vara* (1º grau) ou o
*relator* (2º grau); no PJe "Jurisdição" é a *comarca/foro*. Enfiar um no outro
mistura semânticas no mesmo campo. O conserto certo é decidir antes: campo novo
`jurisdicao` ou `juizo` com semântica documentada.

### Achado 5 — `segredo_justica` nunca foi escrito, e o e-SAJ nos avisa

- `segredo_justica = true` em **0 de 91.638.494** documentos do índice
  (`_count`, não `exists`); `false` em 27,2 M; **ausente do doc** em 64,5 M
  (indexados antes do campo entrar no builder). Na amostra A: **0 de 120.000**.
- Ou seja: para 102 M processos afirmamos "não corre em segredo" — sem ter
  perguntado uma vez. É o oposto de "abster > chutar": o default `False` do
  `BooleanField` virou afirmação.
- O `nivelSigilo` do Datajud existe em **20/20** dos `_source` sondados (valia 0
  nos 20 — então preencheria pouco **nesta** amostra) e não é lido por
  `_meta_updates_from_source`.
- **O e-SAJ diz explicitamente.** Das 11 sondas ao vivo em processos TJSP
  marcados `ok` **sem nenhuma parte**, **10** devolvem HTTP 200 com a página
  *"é necessário informar uma senha para acessar processo em segredo de
  justiça"* (33 KB, sem `tablePartesPrincipais`, sem "Não existem informações").
  Entre elas classes que ninguém suspeitaria: `PROCEDIMENTO COMUM CÍVEL` (3),
  `CUMPRIMENTO DE SENTENÇA` (2), além de Curatela/Divórcio/Ação Penal.

Tamanho: **17,5% dos `ok` do TJSP não têm nenhuma parte** (356 de 2.034 `ok`
na amostra) ≈ **302 k processos** rotulados "enriquecido" com o cadastro vazio.
Idem TJAC 22,1% e TJAL 12,3%. Causa é (1) — a fonte não publica —, mas o
registro diz (2). Conserto: reconhecer a página de senha ⇒ `segredo_justica=True`
+ status que **diga** "sem dados públicos", em vez de `ok` mudo.

### Achado 6 — e-SAJ entrega partes sem OAB e sem documento

O §"Como adicionar enricher pra outro e-SAJ" diz "documento fica vazio (OAB e
nome são preservados)". A primeira metade está certa; a segunda **não se
confirma em produção**:

| tribunal | linhas de parte | advogados | com OAB | com documento |
|---|---:|---:|---:|---:|
| TJSP | 59 | 29 | **0** | **0** |
| TJAL | 441 | 211 | **0** | **0** |
| TJAC | 268 | 107 | **0** | 3 |
| TJPA | 195 | 84 | **0** | 10 |
| TRF1 | 840 | 370 | 196 | 590 |
| TJDFT | 467 | 153 | 153 | 435 |

(entre os `ok`, por processo: TJSP 100% sem OAB e 95,8% sem documento;
TJAL 100%/99,1%; TJAC 100%/96,2%; TJPA 100%/76,3%.)

O parser sabe extrair (`parse_oab` é chamado quando `is_advogado`); a fixture do
TJAL que "prova" a OAB é **sintética**. Consequência: no maior tribunal do
acervo, a parte guardada é **nome solto** — sem chave estável, o vínculo entre
processos e a ficha da parte não fecha. E o DJEN do mesmo TJSP tem OAB em
**98,0%** dos processos (achado 1).

Também medido: `papel` vazio em 171/441 linhas do TJAL, 52/422 do TJMG,
47/176 do TJPE, 14/59 do TJSP.

### Achado 7 — o que cada porta traz, para os MESMOS 20 processos

20 processos reais (1 por tribunal, priorizando `Cumprimento de Sentença contra
a Fazenda` — o caminho do precatório), sondados ao vivo no Datajud:

- **Datajud achou 20/20** — inclusive os 3 que o enricher marcou
  `nao_encontrado`/`erro` (TJMG, TJCE, TJPE).
- `_source` sempre com as mesmas 14 chaves: `assuntos, classe, dataAjuizamento,
  dataHoraUltimaAtualizacao, formato, grau, id, movimentos, nivelSigilo,
  numeroProcesso, orgaoJulgador, sistema, tribunal, @timestamp`.
- `valorCausa` **ausente em 20/20** (confirma §"Valor da causa").
- `orgaoJulgador.codigo` presente em 20/20; **nosso** `orgao_julgador_codigo`
  vazio em 8/20. `assuntos` presente em 20/20; nosso `assunto_codigo` vazio em 8/20.

**O que a porta tem e nós descartamos:**

| campo da porta | onde | o que fazemos | valor |
|---|---|---|---|
| `destinatarios` / `destinatario_advogados` | DJEN, toda comunicação | grava no JSONB e para aí | **84,8% do acervo** ganharia parte+polo+OAB |
| `grau` (`G1`/`G2`/`JE`/`SUP`) | Datajud, 20/20 | não existe coluna | **JE ⇒ RPV, não precatório** — 5 dos 20 são `JE`. Separa dois produtos |
| `assuntos[1..]` | Datajud | só `assuntos[0]` | 5/20 têm 2+ assuntos (TST tem 7) |
| `nivelSigilo` | Datajud, 20/20 | não lido | achado 5 |
| `formato`, `sistema` | Datajud, 20/20 | não lidos | eletrônico/físico e PJe/SAJ/Eproc/Projudi: diz qual enricher tentar |
| `movimentos[].codigo` (TPU) | Datajud | entra num **sha1** dentro do `external_id` — irrecuperável | o código do movimento é o Estágio do Crédito determinístico; sobra só o `nome` no texto |
| `Jurisdição` | PJe, todos | sem ramo no parser | achado 4 |
| página de senha | e-SAJ | tratada como detalhe vazio ⇒ `ok` | achado 5 |
| `codigo_classe` / `nome_orgao` da movimentação | DJEN, 99,8% | só no drainer do enricher | achado 2 |

E uma lacuna de **política**, não de parser: `reabastecer_fila_datajud`
exclui os 16 tribunais com enricher (`.exclude(sigla__in=TRIBUNAIS_COM_ENRICHER)`).
Então quem o enricher não achou não tem segunda porta: **4,54% do acervo
≈ 4,6 M processos** estão em `nao_encontrado`/`erro` **e** com
`data_enriquecimento_datajud IS NULL` — e o Datajud tinha 3/3 dos que sondei.

### O que a tela vê (ES × PG, mesmos 11.160 processos, `_mget`)

| | medida |
|---|---:|
| processos **fora** do índice | 1.045 / 11.160 = **9,4%** (TRT8 62%, TJRS 38%, TRT5 35,5%, TJSE 30%, TJSP 26,5%) |
| doc no índice **sem** `partes` | 9.031 / 10.115 = **89,3%** |
| PG tem `data_autuacao`, doc não | 1.900 / 10.115 = 18,8% (TRF5 83,5%, TRF1 72,9%) |
| PG tem `juizo`, doc não | 165 · `classe_nome` 168 · `assunto` 157 · `orgao_julgador` 163 · `valor_causa` 19 |

A perda de `data_autuacao` **não é defeito**: o campo entrou no builder em
`7d03bab` (11/08/2026) e o reindex de 71 M ainda corre (SEARCH_SCHEMA §Fases).
Vale como régua de progresso, não como bug. Os 9,4% fora do índice e os 89,3%
sem partes, sim, são buraco de produto.

### Buracos ordenados por IMPACTO no produto (direito creditório)

Ordem por valor, não por tamanho. `valor_causa`, partes e assunto valem mais que
`orgao_julgador_nome`.

| # | buraco | tamanho | custo do conserto | por que primeiro |
|---|---|---:|---|---|
| 1 | **destinatários DJEN → `Parte`/`ProcessoParte`** | ≈ 86,7 M processos | backfill sobre dado local + write-through; **zero requisição** | é o produto: quem é a parte, quem é o advogado. Cobre TJPR/TJRS/TJSC/TRTs, que hoje têm **0%** de parte e nunca terão enricher |
| 2 | **assunto: código da folha + nome da folha** | 8,2 M com código recuperável, 1,7 M de nomes de catálogo a limpar | regex + backfill local; **zero requisição** | assunto é feature do classificador de lead e filtro de tela; hoje 63,2% do catálogo mostra hierarquia truncada |
| 3 | **e-SAJ: segredo vira `ok` vazio** | ≈ 302 k só no TJSP `ok` | detectar a página de senha; muda status e `segredo_justica` | "confiança falsa" no tribunal nº 1 do Juriscope — a tela precisa dizer que **não há** |
| 4 | **`grau` do Datajud** | 20/20 têm; 5/20 são `JE` | coluna + mapeamento (o `_source` já é lido) | **JE = RPV, não precatório**. Sem isso o funil mistura dois produtos com prazos e preços diferentes |
| 5 | **2ª porta para `nao_encontrado`/`erro`** | 4,6 M | política de refill, não código | 3/3 achados no Datajud; hoje ficam órfãos entre as duas portas |
| 6 | **classe via movimentação para os `pendente`** | 76,2 M (91,0% de acerto) | backfill local, com carimbo de fonte | destrava o classificador onde não há enricher (é o chicken-egg da Fase 0) |
| 7 | **e-SAJ sem OAB** | TJSP/TJAL/TJAC/TJPA: 0 de 347 advogados | investigar HTML real do `show.do` | resolvido de graça pelo #1 (DJEN traz OAB em 98% do TJSP) |
| 8 | **`assuntos[1..]`, `formato`, `sistema`, `movimentos[].codigo`** | 5/20 multi-assunto | colunas novas | ganho real, mas depois dos 7 acima |
| 9 | `juizo`/`Jurisdição` do PJe | 0,0% em 11 tribunais | decidir semântica antes | campo de contexto; não move lead |
| 10 | `valor_causa` no PJe | 0% e continuará | — | **a fonte não tem.** Ver §"Valor da causa". Não é buraco: é ausência |

### O que NÃO foi medido (e por quê)

- **Se o `ok` sem partes do TJAL/TJAC também é segredo**: as 11 sondas ao vivo
  foram só no TJSP. TJAL/TJAC roteiam por pool/Cortex e sondar direto daqui
  arrisca queimar IP — fica para quando houver janela de proxy.
- **Cobertura de partes por polo/papel contra a fonte**: comparei *existência*,
  não *completude* da lista (o e-SAJ pode mostrar 5 partes e nós gravarmos 2).
  Exigiria buscar o detalhe ao vivo de centenas de processos.
- **`nivelSigilo` em escala**: 20 processos é amostra para *presença da chave*,
  não para estimar a taxa de sigilo nacional.
- **Contagem exata por tribunal**: o volume da matriz vem de `most_common_freqs`
  de `pg_stats` × 102.296.406 (**estimativa**, ±1 pp de efeito de desenho), com
  o `_count` do ES por tribunal como segunda medida. Um `GROUP BY tribunal_id`
  exato em 102 M num banco disk-I/O-bound não cabia no orçamento desta auditoria.
- **TRTs/TST/STJ campo a campo pela fonte**: enrichment trabalhista não está
  ligado (§Justiça do Trabalho); o Tier B exige CapSolver.

### Reprodução

`scripts/auditoria_campos.py` — mesma semente (`20260824`), mesmas consultas,
sem escrita em produção. Gera `matriz.tsv` e imprime as agregações acima.

### O efeito medido em produção (25/08/2026, 00:10–01:05 UTC)

Sequência aplicada: deploy do refill → `enrich_pausa TJRO` → `enrich_cortex
TJAP` → purga das 14 filas envenenadas (1.395.317 jobos removidos por
`LPOP` em lote + `UNLINK`, nunca `q.empty()`) → reciclagem dos 146 workers de
enrich em 4 ondas com trava de memória.

**Duplicação — censo completo das filas, antes e depois:**

| | jobos | process_id distintos | cópia |
|---|---|---|---|
| antes (00:20) | 1.879.219 | 800.167 | 57,4% global · **77,1%** nas 14 filas do refill |
| depois (00:35) | 508.405 | 508.244 | **0,0%** — todas as 16 filas em 1,0 cópia |

**Vazão, na métrica do usuário (`Process` distintos com `enriquecido_em` na
janela — `proc_enriquecido_em_idx`, range scan):**

| janela | processos | taxa |
|---|---|---|
| antes — última 1 h | 41.571 | 41.571/h |
| antes — últimas 24 h | 606.654 | 25.277/h |
| **depois — últimos 10 min** | **21.474** | **128.844/h** |
| depois — últimos 20 min | 35.029 | 105.087/h |
| depois — últimos 30 min | 46.476 | 92.952/h |

**3,1× a melhor hora medida antes; 5,1× a média de 24 h.** O gradiente entre as
janelas é a própria assinatura da mudança: quanto mais longa a janela, mais ela
mistura o regime antigo. E o essencial: **os drainers continuam aplicando o
mesmo volume** (126.264 → 121.692 events/h) — a frota não ficou maior nem bate
mais na fonte; o que mudou é que os events deixaram de ser repetição.

**Pool de proxies — a prova de que a cópia envenenava o recurso compartilhado:**

| | saudáveis | `bad_zset` | `fail_streak` |
|---|---|---|---|
| antes | 289 de 2.500 (11,6%) | 2.211 | 11.011 |
| depois | **1.926 de 2.500 (77,0%)** | **574** | **423** |

**Custo em trabalho perdido: zero.** `FailedJobRegistry` em 0 nas 16 filas antes
e depois (warm shutdown com `--timeout 90`). Purga não perde nada — o backlog é
o status no Postgres, e o refill repôs cada fila ao seu alvo em 2 minutos.

Horizonte recalculado: 31,6 M (pendente+erro com enricher) ÷ 128.844/h =
**≈ 10 dias**, contra os ≈ 32 dias da taxa anterior. Os 47,9 M em tribunais sem
enricher continuam a ≈ 333 dias pelo Datajud — esse número não mudou e não muda
com worker.

### Estado estável (01:55 UTC, ~1 h depois, instrumento independente)

Sonda que não usa log — delta de job ids no `FinishedJobRegistry` em 180 s, com
o `process_id` de cada um lido do hash do job:

| fila | jobs/h | pids distintos | útil |
|---|---|---|---|
| enrich_tjdft | 26.268 | 1.314 | 100% |
| enrich_tjrj | 23.969 | 1.199 | 100% |
| **enrich_tjap** | **21.251** | 1.063 | 100% |
| enrich_tjmt | 17.372 | 869 | 100% |
| enrich_tjsp | 10.056 | 503 | 100% |
| enrich_tjpe | 6.777 | 339 | 100% |
| enrich_tjma | 5.877 | 294 | 100% |
| enrich_tjce | 5.558 | 278 | 100% |
| enrich_tjmg | 4.018 | 201 | 100% |
| enrich_tjpa | 3.898 | 195 | 100% |
| enrich_trf1 | 3.398 | 170 | 100% |
| enrich_tjac | 3.279 | 164 | 100% |
| enrich_trf3 | 3.199 | 160 | 100% |
| enrich_tjal | 3.059 | 153 | 100% |
| enrich_trf5 | 2.479 | 124 | 100% |
| enrich_tjro | 0 | 0 | (pausado) |
| **TOTAL** | **140.457** | **7.026 em 180 s** | **100,0%** |

**TJAP foi de 3.648 jobos/h com 0% de `ok` para 21.251 jobos/h com ~58% de
`ok`** (111 `ok` contra 81 `nao_encontrado` numa janela de 1 min) só por sair do
datacenter — nenhum worker novo. É a terceira maior fonte de enriquecimento do
sistema hoje, e ontem era zero.

A profundidade-alvo se auto-ajustou como projetado: `enrich_tjdft` (a fila mais
rápida) ficou em 24.757, `enrich_tjap` em 10.267, `enrich_tjmt` em 8.426, e as
filas lentas no piso de 2.000. `FailedJobRegistry` em **0 nas 16 filas** depois
de ~290 recriações de container.

> **Vigiar:** o alvo é calculado com a vazão instantânea, então uma rajada pode
> deixar a fila mais funda do que os 1.800 s pretendidos — o `enrich_tjdft`
> ficou em ~38 min de trabalho. Ainda cabe com folga dentro de
> `ENRICH_INFLIGHT_TTL_S` (60 min), que é a garantia de não-duplicação. Se a
> margem encostar, baixe `ENRICH_QUEUE_ALVO_S` — não suba a TTL.

### Pendências deixadas em aberto

- **TJSP com 0,6% de `ok`** (10.480 `nao_encontrado`/h, terminal): entregue à
  auditoria de qualidade. Não é vazão — a fila do TJSP é a mais saudável que
  existe (1,0 cópia).
- **TJAP ainda erra muito** mesmo em `cortex_only` (1.415 de 1.756 numa janela
  de 15 min). O `cortex_only` do `BasePjeEnricher` dava **uma** tentativa e só
  (o gateway entrava em `tentados` e a 2ª rotação devolvia `None`); corrigido
  logo em seguida. Re-medir depois do próximo ciclo.
- **TJRO pausado** com `ok = 0` em 1,09 M de processos. Despausar exige que
  `curl` direto E via Cortex parem de devolver 403 — hoje devolvem os dois.
- **`.103` não recebeu este deploy**: o `git pull` aborta por arquivos não
  rastreados de outra frente (`search/backfill_processos.py`,
  `search/management/commands/es_backfill_processos.py`). Não é bloqueante — o
  refill e os enrichers rodam todos na `.102`; o `web`/`scheduler` só
  enfileiram. Reconciliar quando a outra frente fechar.
- **`tests/test_enricher_tjal.py::test_enriquecer_nao_encontrado_emite_payload`
  falha no HEAD**, não por esta mudança: a fixture `search_form.html` deixou de
  ser lida como "não encontrado" e vira `ok`. É da mesma família do falso
  `nao_encontrado` do e-SAJ — vale para a auditoria de qualidade.
- **`docker logs --since` mente em janela longa**: medido no mesmo dia, um
  `worker_tjdft` devolve 324 linhas em `--since 1m`, 1.395 em `--since 5m` e só
  1.933 em `--since 15m` e `--since 30m` — o buffer do container guarda ~7 min
  nesse ritmo. Janela maior que isso **subconta em silêncio** e o resultado se
  lê como "caiu a vazão". Meça throughput pelo `FinishedJobRegistry` (o RQ
  guarda `result_ttl` = 500 s) ou pelo `enriquecido_em`, nunca por log longo.

## Vigia das fontes (pausa e despausa sozinho)

`enrichers/jobs.py::tick_vigia_fontes` — scheduler, a cada 10 min, ~22 s por tick.

Um tribunal fora do ar queima o pool **compartilhado** de IPs até alguém
reparar. Em 30/08/2026 o TJMG passou a servir uma página de erro estática
(912 bytes, `Last-Modified` de 2016) com **HTTP 200**: não sendo 5xx, passava
pelo `raise_for_status`, quebrava no parser e virava `erro` opaco — 1.184
erro/h por réplica, 0 ok, ~7.100 req/h em 6 réplicas, sem nenhum alerta.

Regras do vigia:

| situação | o que faz |
|---|---|
| marcador de página de erro (`_PJE_ERROR_MARKERS`) | **pausa** |
| HTTP 5xx | **pausa** |
| 2xx com menos de `VIGIA_BYTES_MINIMOS` (4.000) | **pausa** — é casca |
| HTTP 4xx | **abstém** (`duvida`) — pode ser a NOSSA `LIST_URL` |
| desafio de WAF (202 + `x-amzn-waf-action`) | **abstém** — é anti-bot, não fonte fora |
| erro de rede | **abstém** — a sonda mede o proxy tanto quanto a fonte |
| fonte voltou E a pausa é dele | **despausa** |
| pausa é de humano | não toca, nem sonda |

Decisão por **maioria de 3 sondas** (`VIGIA_SONDAS`/`VIGIA_MAIORIA`), em
paralelo, com `VIGIA_TIMEOUT` por sonda. Pausar e despausar saem em `ERROR`.

⚠️ A chave `enrich:pausa_automatica` guarda **de quem é a pausa**. Ela existe
para uma coisa só: o vigia nunca desfazer decisão de gente. Pausa humana tem
motivo que ele não conhece.

⚠️ `sondar_fonte` devolve **três** estados (`viva`/`morta`/`duvida`), nunca um
booleano. Só `morta` pausa, só `viva` despausa, `duvida` não mexe em nada. Com
dois estados o inconclusivo tinha que cair de algum lado — caiu no `True`, e o
log de produção registrou `vigia DESPAUSOU TJPA — fonte voltou: inconclusivo
(0 viva/1 morta/0 duvida/2 rede)`. O TJPA passou a oscilar a cada tick.

⚠️ Desafio de WAF não é fonte fora. O 202 do `awselb` vem com 0 bytes e caía em
"2xx pequeno demais": o TJPE foi pausado enquanto respondia 61% dos jobs.

⚠️ O 4xx não pausar não é frouxidão: um GET seco na `LIST_URL` devolve 404 no
TJDFT, 401 no TJMT e 403 no TJAP/TJRO, e não dá pra separar "tribunal fora" de
"sonda errada" só com o código. Pausar por isso seria o vigia produzindo a
confiança falsa que ele existe para evitar.


## Busca POR PARTE na consulta pública — matriz medida (04/09/2026)

Todo enricher deste documento sabe uma coisa só: achar processo **por número
CNJ**. A pergunta "quais processos esta pessoa tem" é outro formulário, e nunca
tinha sido medida. A tabela abaixo é o resultado do recon
(`scripts/recon_busca_parte.py`), com fixture real de cada desfecho em
`tests/fixtures/<sigla>/busca_*.html|json`.

Por que isto importa: no índice do Voyager, `participacoes.documento` cobre
**0,14%** e `participacoes.oab` **0,067%** dos 71,3 M de processos (`.ia/API.md`).
Buscar CPF no ES e achar zero é ausência de DADO, não de processo. A consulta
pública do tribunal é a única fonte que responde de verdade.

| Tribunal | Motor | documento | nome da parte | OAB | nome do advogado | total declarado | paginação | teto da fonte |
|---|---|---|---|---|---|---|---|---|
| TJSP | e-SAJ | ✅ | ✅ | ✅ | ✅ | `#contadorDeProcessos` | `trocarPagina.do` | **1.000** |
| TJAL | e-SAJ | ✅ | ✅ | ✅ | ✅ | idem | idem | idem (não exercido) |
| TJMG | PJe fPP | ✅ | ✅ | ✅ (sem UF) | ✅ | rodapé "N resultados" | **não tem** | **30** |
| TJMA | PJe fPP | ✅ | ✅ | ✅ (sem UF) | ✅ | idem | não tem | 30 |
| TRF1 | PJe fPP | ✅ | ✅ | ✅ (sem UF) | ✅ | idem | não tem | 30 |
| TRF5 | PJe fPP | ✅\* | ✅\* | ✅\* | ✅\* | idem | não tem | **1 linha** |
| TRF3 | PJe fPP | \*\* | \*\* | \*\* | \*\* | — | — | — |
| TJPA | REST | ✅ `processobycpf` / `processobycnpj` | ✅ `processobynomeparte` (desambiguação) + `processobynomeparteexato` | ✅ `processobyoab` | — | `qtdRegistrosTotal` | `/{pagina}/{tamanho}` | sem teto observado |
| TJMT | REST | ✅ `parteCpfCnpj` | ✅ `parteNome` | ✅ `advogadoOAB` | ✅ `NomeOab`, `advogadoCPF` | `totalRegistros` | `Skip`/`Take` | sem teto observado |

\*\* TRF3 **não foi medido**: o host inteiro (`pje1g.trf3.jus.br` e até
`www.trf3.jus.br`) dá `ReadTimeout` a partir de IP residencial — bloqueio de
borda, não da rota da busca. O recon dele tem de rodar do container, com
`PROXY=cortex`, como os enrichers. Célula vazia é "não medido", nunca "não tem".

\* No TRF5 os quatro critérios respondem, mas a fonte **renderiza apenas o
primeiro resultado** — ver §"O TRF5 conta certo e mostra uma linha".

Um traço vazio (—) é critério ainda não exercitado naquele tribunal, não
ausência provada. Depois da rodada de 04/09/2026 (tarde) só restam duas
lacunas: o TRF3 inteiro e `advogado` no TJPA, que a fonte não oferece.

### e-SAJ: cinco desfechos, e três deles parecem "zero"

O `classificar_resposta` de hoje conhece detalhe, segredo, lista e
"não existem informações". A busca por parte acrescenta dois, e **os dois se
leem como lista vazia** se ninguém olhar:

| desfecho | marcador | o que é |
|---|---|---|
| lista | `a.linkProcesso` + `#contadorDeProcessos` | achou |
| vazio | `#mensagemRetorno` = "Não existem informações disponíveis…" | não achou (terminal) |
| **muitos** | `#mensagemRetorno` = "Foram encontrados **muitos processos** … refine sua busca" | a fonte se recusa a responder — pedir refino, nunca dizer "não tem" |
| **simultâneas** | `#mensagemRetorno` = "Foram identificadas **multiplas consultas simultâneas**" | TRANSITÓRIO: você pediu rápido demais (ver pacing) |
| redireciona | URL final vira `show.do` | 1 resultado só: não há lista, é o detalhe |

Fixtures: `tests/fixtures/tjsp/busca_{nome,oab,documento,documento_vazio,muitos,consultas_simultaneas,nome_pagina2}.html`.

**Pacing entre páginas é obrigatório.** Medido no TJSP, mesma sessão, busca de
823 processos:

| pausa antes do `trocarPagina.do` | resultado |
|---|---|
| 0 s | "multiplas consultas simultâneas", 0 links — **3 tentativas de 3** |
| 1,5 s | página completa, 25 links novos — **3 de 3** |

Sem a pausa, o coletor lê a página 1 e conclui que acabou. A resposta em si é
rápida (0,1–0,3 s por página); o custo é só a espera.

**O teto é 1.000 e ele se disfarça de total.** O `#contadorDeProcessos` do CNPJ
do Bradesco (`60.746.948/0001-12`) diz exatamente `1000 Processos encontrados`,
e a primeira página levou **71 s**. Número redondo é piso disfarçado (regra nº 3
do CLAUDE.md): quem tem mais de mil processos não é alcançável por este
critério, e a resposta da API tem de dizer isso em vez de entregar 1.000 como se
fosse tudo.

### PJe: 30 resultados, sem paginação, e a UF da OAB que zera a busca

**O teto de 30 é da CONTAGEM da fonte, e foi provado com um caso intermediário**
(no TJMG, TJMA e TRF1 o rodapé bate com o número de linhas; o TRF5 é a exceção
tratada abaixo):

| busca (TJMG) | rodapé |
|---|---|
| "ADALGISA PEREIRA DE MENEZES" (raro) | `resultados encontrados` (sem número) = 0 |
| "JOAQUIM PEREIRA DE ALMEIDA" | **12** resultados |
| "MARIA DAS GRAÇAS SILVA" | **30** |
| "MARIA APARECIDA" (muito comum) | **30** |
| "SEBASTIAO RODRIGUES DA COSTA" | **30** |

O rodapé é `<tfoot>` da `fPP:processosTable`, e **não existe scroller nem link
de próxima página** — procurados um a um no HTML (`próxima`, `scroller`,
`datascroller`, `page`: zero ocorrências). Acima de 30 o dado não é alcançável
por este critério: a saída honesta é dizer o teto e sugerir refinar pelos
filtros que o mesmo form oferece (classe judicial, data de autuação).

**Três armadilhas do PJe que devolvem ZERO em vez de erro:**

1. **Host errado.** `pje1g.trf1.jus.br/consultapublica` responde 200, serve um
   form `fPP` idêntico e devolve tabela vazia para QUALQUER busca — inclusive
   "INSTITUTO NACIONAL DO SEGURO SOCIAL". O host da consulta pública é
   `pje1g-consultapublica.trf1.jus.br`. Mesma história no TRF5: `pje.trf5` (sem
   o `1g`) é o PJe com login e serve uma consulta pública antiga, com captcha de
   imagem e sem o form `fPP`.
2. **Botão errado.** O `<input id="fPP:searchProcessos">` visível é
   `type=button`; quem submete é o `A4J.AJAX.Submit` do `executarPesquisa`, com
   um id gerado (`fPP:j_id248` no TRF1). Postar com o `searchProcessos` devolve
   200 e uma resposta AJAX que atualiza só a div de mensagens — sem tabela e sem
   erro. É a heurística que `_find_search_script_id` já usa para o CNJ.
3. **UF da OAB preenchida.** Medido no TJMG com a OAB `MG65417`, colhida de um
   processo real: com a UF selecionada (o combo guarda ÍNDICE — "0"=AC, "1"=AL,
   …, "12"=MG) a busca devolve **0**; com a UF **sem seleção**, devolve **6**.
   Mandar a sigla crua ("MG") é pior ainda: o Seam redireciona para
   `errorUnexpected.seam`. Regra: só o número, UF em branco.

**Ids de campo são por instalação.** `nomeAdv` é `fPP:j_id186:nomeAdv` no TJMA,
`fPP:j_id184:nomeAdv` no TRF1 e `fPP:j_id180:nomeAdv` no TRF5. Procurar o `name`
literal faz o critério "não existir" no tribunal errado. Casar por **sufixo**
(`:nomeAdv`, `:documentoParte`, `:nomeParte`, `:numeroOAB`) — estável nos cinco.

O TRF5 serviu, numa tentativa, uma página **diferente** na mesma URL (consulta
pública antiga, com captcha de imagem em `consultaPublicaForm`). Dez tentativas
seguidas depois vieram todas com o `fPP`. Intermitente: o coletor precisa
detectar "não é o form que eu conheço" e re-tentar em sessão nova, não concluir
"tribunal sem busca".

### O TRF5 conta certo e mostra UMA linha

Primeiro eu li o rodapé do TRF5 ("30 resultados encontrados" numa resposta com
uma linha) como *tamanho de página*. **Errado, e a correção veio de medir mais.**
Seis buscas, 04/09/2026:

| busca | linhas na tabela | rodapé |
|---|---:|---:|
| nome "MARIA JOSE DOS SANTOS" | 1 | 30 |
| nome "JOAO BATISTA DE OLIVEIRA" | 1 | 30 |
| OAB 18191 | 1 | **16** |
| advogado "KLEBER TABOSA BRASILEIRO" | 1 | **13** |
| nome raro | 0 | (sem número) |
| CNPJ do INSS | 1 | 30 |

O rodapé **varia com a busca** — logo é contagem, não tamanho de página. Quem
está truncada é a TABELA: o `<tbody>` traz um `<tr>` e o slot
`<div class="pull-left" title="Paginação">` vem **vazio**; não há scroller no
HTML, e um GET na `listView.seam` depois do POST devolve o formulário limpo. O
navegador de um humano receberia exatamente isso.

Consequência prática: **no TRF5 a busca por parte alcança 1 processo por
consulta**, e a resposta da API sai sempre `truncado`, com "a fonte contou 16 e
devolveu 1". Aceitar o total e avisar o que falta é melhor do que descartá-lo:
o número é a única informação confiável daquela resposta.

### O filtro do PJe FILTRA — prova por interseção

Um teste barato que vale para qualquer fonte nova, e que teria pego a armadilha
do TJMT: buscar critérios diferentes e comparar os conjuntos. No TRF1, três
buscas de 30 resultados cada:

| par | processos em comum |
|---|---:|
| nome × documento | **0** |
| nome × OAB | 1 |
| OAB × nome do advogado (a MESMA pessoa: OAB 83437 = EVERTON RICARDO DA SILVA) | **21** |

Conjuntos disjuntos entre critérios diferentes e quase iguais entre dois
critérios que apontam para a mesma pessoa: o filtro está sendo aplicado. Se
tudo voltasse igual, seria o `documento=` do TJMT de novo.

### REST: os dois melhores, e a armadilha do parâmetro ignorado

**TJMT** — `GET hellsgate.tjmt.jus.br/consultaprocessual/ProcessosJudiciais/v2`
com `Skip`/`Take` e `X-Fingerprint` (o mesmo do enricher). Os nomes dos
parâmetros saem do bundle da SPA (`chunk-VDMA6QP5.js`, `getProcessosJudiciais`):
`parteCpfCnpj` (só dígitos), `parteNome`, `advogadoOAB`, `advogadoCPF`,
`NomeOab`, `numeroUnico`, `comarca`, `AnoInicio`/`AnoFim`, `ExibirArquivados`.

⚠️ **Parâmetro que ele não conhece é IGNORADO, e a resposta é 200 com a base
inteira.** `documento=`, `nomeCpfCnpj=` e `cpfCnpj=` devolveram os mesmos
`totalRegistros: 11.672.774` do baseline sem filtro
(`tests/fixtures/tjmt/busca_parametro_ignorado.json`). Um coletor que confiasse
no 200 traria processos aleatórios como se fossem "os processos deste CPF". O
nome certo filtra de verdade: `parteNome=MARIA JOSE DOS SANTOS` → 1.159;
`advogadoOAB=20688` → 112; `parteCpfCnpj=<cpf fictício>` → 0.

**Teste de sanidade obrigatório** para esta fonte: uma busca com filtro nunca
pode devolver o mesmo total da busca sem filtro.

**TJPA** — `consilium-rest`, rotas lidas do bundle (`main.*.js`):
`processobycpf/{cpf}/{pagina}/{tamanho}`, `processobycnpj/…`,
`processobyoab/{numero}/{orgao}/{pagina}/{tamanho}`,
`processobynomeparteexato/{nome}/{pagina}/{tamanho}` e
`processobynomeparte/{nome}` — esta última é um **desambiguador**: devolve
`[{nome, quantidade, sistema}]`, os nomes reais que casam com o que foi digitado,
para então buscar o exato. Chutar nomes de rota dá **405** em todas
(foi o primeiro resultado do recon); os nomes certos vieram do bundle.

Os dois REST dão **total real e paginação de verdade** — são as únicas fontes
sem teto observado.
