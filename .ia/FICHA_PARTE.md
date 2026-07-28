# Ficha da Parte — extração entity-centric (v2 do partes)

> Meta: a **ficha COMPLETA de cada parte** — papel, documentos, valor a receber,
> quanto já recebeu — com a maior perfeição possível. E o **contexto processual
> completo p/ jurimetria**: vara, comarca, grau, juízes, relator, datas-chave.
> **Falcon é permitido SÓ no treino** (professor/silver); em produção o modelo
> opera 100% sobre os autos.

## O alvo: um ledger por parte (não uma lista de nomes)

```
PROCESSO (numero_cnj)
└── PARTE (entidade resolvida)
    ├── nome_canonico            "MARIA DA SILVA"        ← merge de grafias
    ├── cpf_cnpj                 "123.456.789-00"        ← chave forte do merge
    ├── papel                    HERDEIRO                ← enum fechado
    ├── docs_origem[]            [PROCURACAO#42, HABILITACAO#77]
    ├── valor_a_receber          R$ 231.450,10           ← ofício/planilha (parcela DELA)
    ├── recebido[]               [{R$ 50.000, 2024-03-01, ALVARA#91}]
    ├── saldo                    R$ 181.450,10           ← derivado (nunca extraído)
    └── eventos[]                [CESSAO→FUNDO X (30%), OBITO 2022-05]
```

Cada campo = `MetaField` (valor + proveniência doc/span + confiança + abstido).
`saldo` é **sempre derivado** — extração nunca grava aritmética própria.

## Contexto processual (camada jurimetria)

Além das partes, extrair de cada processo **tudo que alimenta jurimetria**:

```
CONTEXTO
├── vara / comarca / foro          "3ª Vara da Fazenda Pública de Santos"
├── grau / instância               1º grau, 2º grau, STJ/STF (por fase)
├── juízes[]                       {nome, cargo, decisão que assinou, data}
├── relator (2º grau+)             {nome, órgão julgador, câmara/turma}
├── orgao_julgador                 câmara/turma/seção
├── datas-chave                    distribuição, sentença, trânsito, homologação
│                                  de cálculos, expedição do ofício, pagamento
├── desfecho por grau              procedente/improcedente/parcial + recurso
└── advogados[]                    {nome, OAB, parte que representa}
```

Fonte por classe de documento: SENTENCA/DECISAO/DESPACHO → juiz+data+vara (o
cabeçalho e a assinatura são padronizados — alta precisão); ACORDAO → relator+
órgão+desfecho; OFICIO_REQUISITORIO → vara expedidora+datas. Juiz é capturado
**por decisão** (quem assinou o quê, quando) — jurimetria de magistrado depende
disso, não de "um juiz por processo". Tudo com a mesma proveniência MetaField.

## Por que o partes deu 54% (e como isso conserta)

Diagnóstico medido (jul/2026): erro é de **CONTEÚDO/cobertura**, não formato
(grammar não moveu: 0,533→0,529; só 2,5% JSON quebrado). O modelo gerava a lista
inteira one-shot vendo ~6 chunks → nunca viu metade dos herdeiros. Modelo maior
(120B) só melhorava por achar mais docs — ou seja, o lever é **cobertura
documental**, não capacidade.

**Reformulação:** extração **POR DOCUMENTO** (input = 1 doc inteiro, output =
registros) + **merge determinístico**. Completude vem do pipeline (lê TODOS os
docs relevantes), não da memória do modelo.

## Pipeline

```
 autos (PDFs já classificados: Documento.doc_classe, 644k docs)
   │
   ├─ roteamento por classe ──────────────── o que cada doc rende ───────────┐
   │   PROCURACAO / DOC_PESSOAL / CERT_CASAMENTO → {nome, cpf, papel_hint}   │
   │   OFICIO_REQUISITORIO → {beneficiário, valor individual, natureza, data}│
   │   PLANILHA_CALCULO    → {parte, valor_calculado, data_base}             │
   │   CESSAO_CREDITO      → {cedente, cessionário, %/valor cedido}         │
   │   HABILITACAO_HERDEIROS + CERTIDAO_OBITO → {falecido, herdeiros[]}      │
   │   ALVARA / PAGAMENTO_COMPROVANTE → {beneficiário, valor_pago, data}     │
   │   CONTRATO_HONORARIOS → {advogado, %, valor}                            │
   │   SENTENCA / DECISAO / DESPACHO → {juiz, vara, data, desfecho}          │
   │   ACORDAO → {relator, órgão julgador, desfecho, data}                   │
   │                                                                         │
   ├─ [1] extrator 7B por-documento (fine-tune, prompt condicionado à classe)
   ├─ [2] resolução de entidade (determinística):
   │       chave forte = CPF/CNPJ → senão nome canônico (NFKD, fuzzy alto)
   │       "ESPÓLIO DE X" ↔ "X" viram entidade ligada (espolio_de)
   ├─ [3] montagem do ledger + saldo derivado + eventos ordenados por data
   └─ [4] abstenção por campo (conflito entre docs → abstém, nunca chuta)
```

## Decisões

1. **Extração por-documento, não por-processo.** Doc individual cabe fácil nos
   4096 tokens; labels por documento já existem (regex e-SAJ: ofício↔parte+valor,
   cessão cedente↔parte); treino fica mais denso em sinal; merge é código, não LLM.
2. **Falcon-free em produção.** Valor sai da PLANILHA/OFÍCIO dos autos. Falcon
   entra só como silver-label e régua de gate no treino.
3. **Especialistas por tier, não por campo.** Tier 0 = regex (cessão já dá 100).
   Tier 1 = classificador pequeno p/ natureza (99 no talo, não gastar 7B). Tier 2 =
   7B fine-tunado por-documento (partes/valores/eventos). Um modelo 7B único
   condicionado à doc_classe serve o Tier 2 inteiro.
4. **Base swap NÃO é o lever.** 120B não consertou valor/natureza. Encoder-NER
   (BERTikal) fica como otimização de custo futura, gateada contra o 7B.
5. **Métrica nova**: F1 de entidade **pós-canonicalização** (mata ruído de grafia
   que punia o 54%), exatidão de valores por parte, cobertura de pagamentos.

## Gaps a fechar

- [ ] Classe **ALVARA** explícita no doc_classificador (hoje cai em OUTROS/DECISAO;
      keywords: "alvará", "levantamento", "mandado de levantamento") — é a fonte
      do "já recebeu".
- [ ] Dataset v2 por-documento (gerador novo em `~/zordon/eval/`), labels:
      regex determinística por classe + gold v1 (10.850) reaproveitado + Falcon
      silver p/ valores. Disputas → fila κ.
- [ ] Retreino QLoRA (mesma receita validada: Qwen2.5-7B nf4 r=32, llmsv2) com
      mix v1 (campos maxados, pouco peso) + v2 por-documento (peso alto).
- [ ] Merger + ledger em `~/zordon/acervo/` (persistir em MetadadoExtraido).
- [ ] Gate: TEST held-out por CNJ + κ humano em amostra de fichas completas.

## Fases

| fase | entrega | onde | custo |
|---|---|---|---|
| F0 | ALVARA no classificador + reclassificar OUTROS | zordon (CPU) | horas |
| F1 | gerador dataset v2 por-documento + relatório yield | zordon (CPU/DB) | ~1 dia |
| F2 | retreino QLoRA + GGUF | llmsv2 (3090) | ~10h GPU |
| F3 | merger/ledger + persistência + gate | zordon | ~1 dia |
| F4 | demote natureza→clf pequeno; encoder-NER experimental | depois | opcional |

Referências: `.ia/CLASSIFICACAO.md`, plano extração roteada (extract_routed.py),
memória `finetune-extrator-qwen-qlora` (receita e números v1).
