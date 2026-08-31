# Plano: fechar a fila do acervo (#105, #104, #97, #96, #92)

Aberto em **31/08/2026**. General: a sessão principal. Quatro especialistas.

## A ordem não é por tamanho, é por dependência

O maior número da fila é o #92 (187,7 M que faltam). Ele **não** é o primeiro.

```
   #105  classe vs fase          ─┬─→  #104  classe_id NULL (8,09 M)
   (a régua do nicho)            │
                                 └─→  #92   puxada nacional  ── PORTÃO ──→ execução
   #97   sinal TJSP (1,51 M)     ─── independente
   #96   dedup por OAB           ─── independente
```

**Por que o #105 vem antes.** Ele descobriu que 37,1% dos nossos rótulos de
classe 12078 batem em outra classe no CNJ (TRF3 98%, TJSP 4%). Enquanto esse
campo misturar **fase** (o que o DJEN publica) com **classe** (o que o CNJ
cadastra), qualquer contagem do nicho é medida com régua torta — inclusive a
que julgaria se o #92 valeu a pena. Backfillar o #104 antes disso propagaria o
erro para 8,09 M linhas.

**Por que o #92 tem portão.** A licença do DataJud é declaradamente **não
comercial**. Ele é a origem dos 344,6 M do `voyager-acervo`, que é o
denominador da métrica de cobertura que está na tela do Acompanhamento. A
preparação (telemetria, kill switch, medição a seco) segue; **a puxada em
escala não roda sem decisão do dono do produto**. Ver `.ia/ESTUDO_JUIT.md` §5.

## Quem faz o quê

| agente | pendência | entrega que fecha |
|---|---|---|
| **R105** | #105 | veredito medido: rótulo errado **ou** dois campos colididos; se colisão, `classe_cnj` × `fase_detectada` separados, com migration e reindex |
| **R104** | #104 | 8,09 M `classe_id` ligados ao catálogo — **só depois** do veredito do R105 |
| **R97** | #97 | 1,51 M `tem_sinal_precatorio` computados no TJSP, sem o recorte medir o próprio buraco |
| **R96** | #96 | dedup de Parte por OAB com zero à esquerda, teto 13.045 conferido dos dois lados |
| **R92** | #92 | puxada pronta para rodar: telemetria, kill switch, dry-run medido — **execução travada no portão** |

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

### A condição que não é negociável

A cobertura vai saltar de **35,55% para perto de 100%** — e isso é mentira se
ninguém separar **esqueleto** de **acervo rico**. O `_source` do Datajud não tem
parte, advogado nem valor. Antes de a puxada terminar, o card de Cobertura tem
que mostrar as duas coisas separadas: *temos o CNJ* × *temos o processo*.
Terminar a puxada com o card somando os dois é entregar confiança falsa — o
oposto do que o produto existe para fazer.

### Pré-voo medido em 31/08/2026

| | |
|---|---|
| ES `voyager-es-01` | 994 GB livres de 2991 (67% usado) — os ~77 GB cabem |
| cluster | `yellow`, 3 shards não atribuídos (esperado em nó único com réplica) |
| `voyager-acervo` | 344.603.487 docs — este é o "antes" |

**Abortar se**: cluster virar `red`, disco livre cair abaixo de 200 GB, ou a
busca do site degradar. O banco já está sob carga (enriquecimento a 1,74 M/dia)
e a busca trava sob contenção de I/O. Site em pé vale mais que terminar 3h antes.
