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

## Portão do #92 — o que precisa de decisão humana

Não é engenharia, é licença:

1. o DataJud veda "vender ou explorar comercialmente qualquer informação
   derivada dela" — e ele alimenta o denominador da nossa métrica pública;
2. `robots.txt` diverge por tribunal (STM e TST publicam `Disallow: /`).

Até isso ser respondido, o R92 entrega a puxada **pronta e parada**.
