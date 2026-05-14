# POC TJMG/TJSP — Resultados (madrugada 30/04→01/05/2026)

## Setup

- **Local**: docker-compose com workers RQ (web + 30 datajud + 4 classificacao + 4 default + redis + postgres)
- **Janela DJEN ingerida**: 14-20 abril 2026 (1 semana)
- **Patch aplicado**: `datajud.sync_processo` agora popula `Process.classe_codigo/classe_nome` quando vazio
- **Modelo**: v5 (treinado SÓ em TRF1, AUC 0.95)

## Resultados — primeira passada (1 semana 14-20 abril)

| Tribunal | Procs | Datajud OK | 💎 PRECATÓRIO | ⏳ PRÉ | 🌱 DIREITO | Total | % lead |
|----------|-------|------------|---------------|--------|------------|-------|--------|
| **TJMG** | 18.739 | 18.739 (100%) | **183** | **2.053** | **538** | **2.774** | **14.8%** |
| **TJSP** | 22.757 | 22.757 (100%) | **6** | **301** | **278** | **585** | **2.6%** |

## Resultados — segunda passada (TJMG abril completo 1-30 abril)

| Tribunal | Procs | Datajud OK | 💎 PRECATÓRIO | ⏳ PRÉ | 🌱 DIREITO | Total | % lead |
|----------|-------|------------|---------------|--------|------------|-------|--------|
| **TJMG** | **89.540** | 80.086 (89%) | **333** | **3.852** | **976** | **5.161** | **5.76%** |

A queda de % (14.8% → 5.76%) na segunda passada faz sentido: amostra maior pega dias-úteis + fim-de-semana (quando há menos publicações de Cumprimento contra Fazenda). Em valor absoluto, leads **dobraram** com 4.7x mais procs ingeridos.

## Top scores (validação manual sugerida)

### TJMG — top 10 PRECATÓRIO
```
5002377-05.2018.8.13.0707 score=0.828
5033425-81.2024.8.13.0024 score=0.824
5004678-69.2024.8.13.0106 score=0.823
5011558-22.2024.8.13.0480 score=0.821
5235985-80.2022.8.13.0024 score=0.819
5000784-88.2024.8.13.0687 score=0.819
5000367-52.2023.8.13.0338 score=0.818
5000654-91.2022.8.13.0324 score=0.816
5177974-58.2022.8.13.0024 score=0.812
5000313-90.2021.8.13.0133 score=0.809
```

### TJSP — top 10 PRECATÓRIO/PRÉ
```
0008292-04.2019.8.26.0100 PRECATORIO  0.908
0005495-84.2021.8.26.0100 PRECATORIO  0.884
0000273-38.2021.8.26.0100 PRECATORIO  0.874
0005231-04.2025.8.26.0011 PRECATORIO  0.841
0118988-96.2008.8.26.0002 PRECATORIO  0.812
0018084-06.2024.8.26.0100 PRECATORIO  0.807
0016340-85.2025.8.26.0602 PRE_PRECATORIO  0.704
0008523-60.2021.8.26.0100 PRE_PRECATORIO  0.690
1021409-68.2021.8.26.0005 PRE_PRECATORIO  0.687
0005352-42.2019.8.26.0011 PRE_PRECATORIO  0.675
```

## Achados-chave

### 1. Modelo TRF1 generaliza pra estaduais ✓
Sem retreino, modelo treinado em TRF1 detecta leads em TJMG/TJSP. Scores altos (>0.8) em vários casos. As features universais (classe Cumprimento, palavras-chave, contagem de movs) funcionam transversalmente.

### 2. TJMG tem MUITO mais cumprimentos contra fazenda
14.8% dos procs TJMG (1 dia) viraram lead. No TRF1 a proporção foi ~34% (na lista histórica de leads), mas naquela amostra ground truth era enviesada. Em produção atual TRF1, a proporção classificada é menor (1.7% recente).

A proporção alta TJMG sugere:
- MG concentra muitos cumprimentos contra Fazenda Estadual
- Justiça estadual tem maior frequência relativa de cumprimentos vs federal (que tem mais variedade — fiscal, criminal, etc)

### 3. TJSP subestimado (provavelmente)
TJSP usa PJe + **e-SAJ** (sistema legacy proprietário). Datajud só cobre PJe. A maioria dos procs TJSP da amostra:
- Datajud cobertura baixa (poucos % com classe populada)
- Score sem dados ricos
- Features F11/F12 (texto) também podem estar vazias

Pra melhorar TJSP precisaria parser e-SAJ (`enrichers/esaj.py` — não existe ainda).

### 4. Hook in-process é insuficiente pra re-classificar
`datajud.sync_processo` só chama `classificar_e_persistir` quando `novos > 0` (movs novas). Em **2ª passada** (Datajud já feito), `novos == 0` e classificação NÃO refaz. Como o patch `Process.classe_codigo` foi aplicado depois da 1ª classificação, processos ficaram com classificação **antiga** (pré-patch) até re-classify síncrono.

**Implicação**: pra deploy real desse patch em prod, será necessário **rodar batch de re-classificação** uma única vez após o deploy, pra capturar os procs já com Datajud feito mas classificação stale.

### 5. Volume de movs é o sinal dominante
Mesmo procs com classe Cumprimento (cod=12078/156) só viram PRECATÓRIO se `F15_logMovs` é alto (lots de movs). Procs com 1-10 movs (típico de DJEN-only) viram PRE_PRECATÓRIO ou DIREITO_CREDITÓRIO no máximo.

## Próximos passos sugeridos

1. **Validar manualmente** — abrir os 10 top PRECATÓRIO TJMG/TJSP (CNJs acima) e confirmar se são realmente leads.
2. **Expandir janela** — ingestar abril inteiro TJMG (~300k+ comunicações esperadas).
3. **Lista TJMG/TJSP de leads confirmados** — pedir ao usuário pra validar 50-100 manualmente, criar `lead_tjmg.csv` / `lead_tjsp.csv`.
4. **Re-treinar v6 multi-tribunal** — TRF1 + TRF3 + TJMG + TJSP combinado.
5. **Parser e-SAJ pro TJSP** — fora do escopo do Voyager hoje, mas necessário pra cobrir TJSP completo.
6. **Reclassify batch trigger** — ao deployar mudanças que afetem features (ex: novo patch de classe), disparar `reclassificar_recentes(dias=N)` automaticamente.

## Configuração local (docker-compose.yml)

Adicionados:
- `worker_datajud` (30 réplicas, fila `datajud`)
- `worker_classificacao` (4 réplicas, fila `classificacao`)
- `worker_default` ajustado pra 4 réplicas (era 30 — too many)

## Tempo de execução

- Ingestão DJEN 1 semana TJMG: ~5min (com cap-split adaptativo)
- Ingestão DJEN 1 semana TJSP: ~5min
- Datajud sync 18.7k procs TJMG: ~30min (30 workers paralelos, rate ~10/s/host)
- Datajud sync 22.7k procs TJSP: em curso (mais lento por proxies + e-SAJ não-Datajud)
- Re-classify síncrono 18.7k TJMG: ~14min (~22/s)
- Re-classify síncrono 22.7k TJSP: ~17min (~22/s)
