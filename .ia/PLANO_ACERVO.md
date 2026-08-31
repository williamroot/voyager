# Plano: fechar a fila do acervo (#105, #104, #97, #96, #92)

Aberto em **31/08/2026**. General: a sessão principal. Quatro especialistas.

## A ordem não é por tamanho, é por dependência

O maior número da fila é o #92 (187,7 M que faltam). Ele **não** é o primeiro.

```
   #105  classe vs fase          ─┬─→  #104  classe_id NULL (8,09 M)
   (a régua do nicho)            │
                                 └─→  #92   puxada nacional  ── PORTÃO ABERTO 31/08 ──→ execução
   #97   sinal TJSP (1,51 M)     ─── independente
   #96   dedup por OAB           ─── independente
```

**Por que o #105 vem antes.** Ele descobriu que 37,1% dos nossos rótulos de
classe 12078 batem em outra classe no CNJ (TRF3 98%, TJSP 4%). Enquanto esse
campo misturar **fase** (o que o DJEN publica) com **classe** (o que o CNJ
cadastra), qualquer contagem do nicho é medida com régua torta — inclusive a
que julgaria se o #92 valeu a pena. Backfillar o #104 antes disso propagaria o
erro para 8,09 M linhas.

**Por que o #92 tinha portão.** A licença do DataJud é declaradamente **não
comercial**, e ele é a origem dos 344,6 M do `voyager-acervo` — o denominador da
métrica de cobertura publicada na tela. A questão foi apresentada ao dono do
produto, que **autorizou a execução em 31/08/2026**. Ver a seção "Portão do
#92" no fim deste arquivo e `.ia/ESTUDO_JUIT.md` §5.

⚠️ Autorizado ≠ solto: a ordem de execução (kill switch com retomada testada →
dry-run → um tribunal no gate → nacional) e a separação esqueleto × acervo rico
no card continuam obrigatórias.

## Quem faz o quê

| agente | pendência | entrega que fecha |
|---|---|---|
| **R105** | #105 | veredito medido: rótulo errado **ou** dois campos colididos; se colisão, `classe_cnj` × `fase_detectada` separados, com migration e reindex |
| **R104** | #104 | 8,09 M `classe_id` ligados ao catálogo — **só depois** do veredito do R105 |
| **R97** | #97 | 1,51 M `tem_sinal_precatorio` computados no TJSP, sem o recorte medir o próprio buraco |
| **R96** | #96 | dedup de Parte por OAB com zero à esquerda, teto 13.045 conferido dos dois lados |
| **R92** | #92 | puxada executada: telemetria, kill switch com retomada testada, gate por tribunal — e o card separando esqueleto de acervo rico |

## O que vale para todos (e já custou caro quando foi ignorado)

1. **Meça dos dois lados.** Contagem própria não prova nada. Compare com a
   fonte, e conte a divergência nas **duas** direções.
2. **Campo de controle obrigatório.** Toda régua carrega um campo que *tem* que
   dar 100% (`proc`). Se ele não der, a medição inteira é lixo e não se publica.
   Foi um controle em 0,0% que pegou a régua torta em 30/08.
3. **Os nomes dos campos diferem entre os índices.** `voyager-acervo` usa
   `classe_codigo`; `voyager-processos` usa `codigo_classe` — **invertido**. E
   `numero_cnj` vira `proc`, `assunto_nome` vira `assunto`.
4. **`exists` do ES mente** (string vazia conta como valor). Campo `text` só se
   mede por amostra. **`count` trava em 10.000** sem `track_total_hits`.
5. **Nada sem teto de espera.** `statement_timeout` no Postgres,
   `request_timeout` no ES. Uma medição de rodapé sem teto já derrubou o site.
6. **O bind mount entrega o arquivo; o Python não recarrega.** Valide **dentro
   do processo** que está rodando — `docker exec python -c "import ..."` sobe um
   processo NOVO e prova o disco, não o worker. Prova boa é `StartedAt` posterior
   ao `git pull` **mais** comportamento observável.
7. **Restart de worker exige `-f docker-compose-workers.yml`.** Sem o `-f`,
   `worker_tjmg` reinicia o container homônimo do compose default e sai verde.
8. **Abster > chutar.** Campo que não dá pra provar fica vazio, e a tela diz que
   está vazio.
9. **Teto é alerta, nunca corte mudo.** Limite atingido é ERRO registrado com o
   número real.
10. **Teste que passaria sem o fix não prova nada.** Confira por mutação:
    desligue o fix e veja o teste quebrar.

## ⚠️ Os agentes compartilham a MESMA árvore de trabalho

Quatro agentes editando `/home/ubuntu/projetos/voyager` ao mesmo tempo. Um
`git add -A` de qualquer um varre o trabalho em andamento dos outros.

**Aconteceu em 31/08/2026:** o commit `36634c0`, cujo título é do R96 sobre
dedup de OAB, arrastou `datajud/client.py`, `datajud/varredura.py`,
`core/settings.py` e um teste de 381 linhas do R92. Nada se perdeu, mas a
história atribui o trabalho de um ao outro — e quem for ler o commit não acha o
porquê das mudanças de datajud, que é justamente a disciplina que a casa cobra.

O caso perigoso não é esse: é uma **migration pela metade** ser commitada por
outro agente. `0054_classe_cnj_e_fase.py` esteve nessa situação.

```bash
# ERRADO — varre a árvore inteira, inclusive o que não é seu
git add -A

# CERTO — só os seus arquivos, conferidos antes
git status --porcelain
git add tribunals/models.py tribunals/migrations/0054_*.py
```

História já publicada não se reescreve; o custo de reescrever é maior que o da
bagunça. A regra vale daqui pra frente.

## Definição de pronto (a mesma para os cinco)

Uma pendência só fecha com **as seis**:

- [ ] número **antes** e **depois**, medidos, com a consulta registrada
- [ ] medição dos dois lados, com campo de controle em 100%
- [ ] teste novo, conferido por mutação
- [ ] suíte relacionada rodada, com o **baseline** medido antes (falha anterior
      ao diff não conta como regressão, e tem que ser dita)
- [ ] deployado e verificado **dentro do processo**, não pelo disco
- [ ] seção "o que eu não consegui medir, e por quê" — meia régua é pior que
      régua nenhuma

## Portão do #92 — ABERTO em 31/08/2026

O portão existia por licença, não por engenharia: o DataJud veda "vender ou
explorar comercialmente qualquer informação derivada dela", e ele alimenta o
denominador da métrica de cobertura publicada na tela.

**A questão foi apresentada ao dono do produto (William) e ele autorizou a
execução em 31/08/2026.** Decisão dele, registrada aqui para quem ler depois
saber que não foi omissão nem descuido.

⚠️ O alerta do `robots.txt` (STM e TST publicam `Disallow: /`) **não era sobre
o #92** e continua de pé: ele vale para a coleta de jurisprudência nos portais
dos tribunais, discutida em `.ia/ESTUDO_JUIT.md`. O DataJud é API oficial com
chave; são portas diferentes e não devem ser confundidas.

### O que a autorização NÃO dispensa

Liberar a execução não substitui a engenharia. Ordem obrigatória:

1. telemetria + kill switch com **retomada testada** (parar no meio e provar que
   retoma do cursor sem repetir nem pular — testar só o `stop` não vale);
2. dry-run que mede sem escrever: delta por tribunal e ETA real;
3. **um** tribunal inteiro fechando o gate de completude (o padrão do TRT20:
   235.758 contra 235.754 declarados);
4. só então o nacional.

### ❌ A "condição não negociável" que eu escrevi estava ERRADA

Eu (o general) escrevi aqui que a cobertura saltaria de 35,55% para perto de
100% e que o card precisaria separar esqueleto de acervo rico. **Está errado, e
o R92 me corrigiu com o código na mão.**

`dashboard/cobertura_nacional.py:258` — `'faltam': max(cnjs - total_pg, 0)`:

| | |
|---|---|
| numerador | `tribunals_process` — o acervo **RICO** |
| denominador | CNJs distintos do `voyager-acervo` — o **esqueleto** |

A varredura só mexe no **denominador**. Puxar mais esqueleto faria a cobertura
**CAIR**, não subir. E o card já separa as duas coisas na tela: "processos
nossos" × "CNJs no país" × "ainda faltam" × "já na busca", mais o funil
banco → busca → com parte → com advogado.

Eu li esse arquivo, escrevi o card de Integridade ao lado dele, e ainda assim
inverti numerador com denominador. Fica registrado porque a lição é a mesma que
a casa cobra dos dados: **afirmação sem medição é chute, inclusive a minha.**

### E o 187,7 M não é varredura — é HIDRATAÇÃO

`faltam = cnjs − total_pg` é literalmente "CNJs que o esqueleto conhece e que
ainda não viraram processo". Isso se resolve com **1 requisição por CNJ**, não
com páginas de 10.000. É outro job, com outro custo, e não é o #92.

### O dry-run derrubou a premissa do #92 (31/08/2026)

59 requisições `size:0` ao CNJ, uma por tribunal, contra `_count` no acervo:

    declarado ao CNJ ....... 350.430.801
    voyager-acervo ......... 344.603.487
    delta bruto ............   5.800.259   (98,34% já coberto)

**95,1% desse delta não é processo.** São linhas com `numeroProcesso: null`,
`classe: {codigo: "-1", nome: "Inválido"}`, `grau: null`. Conferido por mim, de
forma independente, em dois tribunais:

| | CNJ declara | nosso | delta | sem `numeroProcesso` | resíduo REAL |
|---|---:|---:|---:|---:|---:|
| TJSP | 74.686.714 | 69.078.849 | 5.607.865 | 5.337.680 | **270.185** |
| TJMG | 36.698.417 | 36.678.104 | 20.313 | 20.313 | **0** |

E o fecho: o conjunto "sem `numeroProcesso`" é **exatamente** o conjunto "sem
`@timestamp`" (5.337.680 = 5.337.680 no TJSP; idem TJMG). A varredura pagina por
`range @timestamp` — sem chave de ordenação, esses documentos são inalcançáveis
**por construção**. Não é buraco nosso.

**Resíduo nacional real: 283.987 docs (0,082%) — ~29 requisições, minutos.**

Não são 187,7 M, não são 34,3 mil requisições, não são 77 GB, não são 16–20h.

### Pré-voo medido em 31/08/2026

| | |
|---|---|
| ES `voyager-es-01` | 994 GB livres de 2991 (67% usado) — os ~77 GB cabem |
| cluster | `yellow`, 3 shards não atribuídos (esperado em nó único com réplica) |
| `voyager-acervo` | 344.603.487 docs — este é o "antes" |

**Abortar se**: cluster virar `red`, disco livre cair abaixo de 200 GB, ou a
busca do site degradar. O banco já está sob carga (enriquecimento a 1,74 M/dia)
e a busca trava sob contenção de I/O. Site em pé vale mais que terminar 3h antes.
