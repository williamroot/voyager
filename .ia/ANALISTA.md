# Analista — substituir o GPT do JuriscopeIA por modelo próprio

> Missão nova (jul/2026): o **precatorio-ai-analyzer** ("JuriscopeIA", repo de
> terceiros) usa GPT da OpenAI pra análise de precatórios. Objetivo: substituir
> por modelo próprio (Analista-7B), destilado do nosso acervo + dos dados de
> produção deles, gateado contra respostas HUMANAS.

## ⚠️ REGRA DURA — repo deles é READ-ONLY ABSOLUTO

O repo `precatorio-ai-analyzer` é de terceiros. **Nunca commitar/push lá.**
Já aconteceu 1 push indevido — revertido com force pra `0baafbe`
(`.ia/LABLOG.md` 30/07). Regra derivada, sem exceção:

- repo deles: **só leitura** (clone pra estudar, nunca escrever);
- **todo** o nosso trabalho vive em `~/projetos/analista-lab` (repo próprio).

## O que o JuriscopeIA faz (mapa do alvo)

```
sessão de análise
 ├── validação cadastral (6 etapas)          ← E1..E6, respostas humanas em E6
 ├── análise IA (4 "especialistas")           ← prompts GPT por perfil
 ├── parecer DD (due diligence)               ← 16 perguntas ANCORADAS em
 │                                              mov_id / página (âncora é parte
 │                                              da resposta válida)
 └── certidões                                ← 22 prompts de certidão (gpt-4o)
```

## A1 — Colheita (DB prod deles, 10.10.0.108, acesso read-only)

O que existe de dado real pra destilar e pra régua:

| dado | volume | uso |
|---|--:|---|
| sessões de análise | 114 (só **5** com `analise_ia`) | pouco sinal do fluxo IA |
| correções humanas | 11 | preferência (estilo DPO) |
| **certidões com análise gpt-4o** | **1.377** (1.202 c/ PDF) | **maior corpus de destilação** |
| respostas E6 **HUMANAS** | **970** (34 sessões) | **a régua** dos gates/bake-off |
| divergências | 174 (91 manuais) | casos difíceis / eval |

## A2 — Shadow (30 ofícios): 💣 o caminho GPT do ofício NUNCA rodou

Achado da shadow-run: o caminho **gpt-4o-mini de extração de ofício NUNCA
executou em produção** — 0/114 sessões; um **fallback regex silencioso**
respondia no lugar. Ou seja: o "concorrente" real do nosso modelo no ofício
é regex, não GPT.

Nosso GGUF v2 vs a regex deles (30 ofícios, adjudicação):

| métrica | resultado |
|---|---|
| adjudicado (quem acertou quando divergem) | **28 × 0** pro GGUF (9 ambíguos) |
| concordância beneficiario | 73% |
| concordância valor | 79% |

O gap de concordância não é erro nosso: o **schema deles é mais rico**
(pss/irrf/rra, nº DEPRE, partes com OAB) e o v2 não extrai esses campos →
tarefa **"ofício ampliado"** entra no próximo ciclo de dataset. Mesmo assim,
**swap parcial (campos que já cobrimos) já é defensável** com o 28×0.

## A3 — Bake-off de professores (EM CURSO)

Escolher o professor da destilação por **dado, não por fama**:

- Via **Ollama Cloud** (base `https://ollama.com/v1`, chave do prod).
- Candidatos: `gpt-oss:120b` vs `glm-5.2` vs `kimi-k3` vs `deepseek-v4-pro`.
- Bancada: **10 sessões**; régua = as **respostas humanas E6**.
- Julgamento: **concordância + âncora válida** (resposta certa com mov_id/
  página errado = errada — a âncora é contrato do parecer DD).
- Vencedor **professora 1-2k processos do acervo** (rotulagem de destilação;
  custo = assinatura, não por-token).

## A4/A5 — Plano

| fase | entrega |
|---|---|
| A4 | treinar **Analista-7B** com os rótulos do professor vencedor + corpus A1 — possivelmente **sobre o checkpoint DAPT** (`.ia/EXPERIMENTOS_MODELO.md` §1), que já fala o dialeto dos autos |
| A5 | expandir pra **pré-precatório / direito creditório** — greenfield: question-set NOVO (não existe equivalente no JuriscopeIA) + **jurimetria como feature** (F30 / modelo-T do classificador) |

## Onde vive cada coisa

| o quê | onde |
|---|---|
| Todo o trabalho (colheita, shadow, bake-off, treino) | `~/projetos/analista-lab` (repo próprio) |
| Repo deles (referência) | `precatorio-ai-analyzer` — **READ-ONLY** |
| DB prod deles | `10.10.0.108` — acesso **read-only** |
| Régua / gates | respostas humanas E6 (970) + protocolo κ (`.ia/MODELOS.md`) |
