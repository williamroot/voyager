"""Treina o modelo de sobrevivência DIREITO_CREDITÓRIO→precatório.

Roda no container (pandas/lifelines instalados). Lê /tmp/surv_dataset.csv.
- Baseline SERVABLE: Kaplan-Meier estratificado por {ente_tipo × natureza} →
  chance@12/24m + tempo mediano por estrato → JSON (o dossiê consome sem lib ML).
- Modelo Cox (lifelines): C-index com SPLIT TEMPORAL (coorte antiga treino / recente
  teste) — valida se as features ajudam além do baseline.
Saídas: /tmp/surv_strata.json (servable) + métricas no stdout.
"""
import json, math
import pandas as pd
from lifelines import KaplanMeierFitter, CoxPHFitter
from lifelines.utils import concordance_index

df = pd.read_csv('/tmp/surv_dataset.csv')
n0 = len(df)
# saneamento
df = df[(df['duracao_dias'] > 0) & (df['duracao_dias'] < 366*25)].copy()
df['natureza'] = df['natureza'].fillna('DESCONHECIDA').replace('', 'DESCONHECIDA')
df['ente_tipo'] = df['ente_tipo'].fillna('outro').replace('', 'outro')
print(f'linhas: {n0:,} -> {len(df):,} | eventos: {df.evento.sum():,} ({100*df.evento.mean():.1f}%) '
      f'| censura: {(1-df.evento).sum():,} | is_extinto: {df.is_extinto.sum():,}')
print('duração (dias) evento=1: P25/P50/P75 =',
      [int(x) for x in df[df.evento==1]['duracao_dias'].quantile([.25,.5,.75])])
print('coortes (t0 ano):', dict(df['coorte_ano'].value_counts().sort_index().tail(8)))

HORIZ = {'12m': 365, '24m': 730, '36m': 1095}

def km_stats(sub):
    kmf = KaplanMeierFitter()
    kmf.fit(sub['duracao_dias'], sub['evento'])
    out = {'n': int(len(sub)), 'eventos': int(sub.evento.sum())}
    for k, d in HORIZ.items():
        try:
            s = float(kmf.predict(d))  # S(d) = ainda NÃO virou
            out[f'chance_{k}'] = round(100*(1-s), 1)
        except Exception:
            out[f'chance_{k}'] = None
    med = kmf.median_survival_time_
    out['tempo_mediano_dias'] = None if (med is None or math.isinf(med)) else int(med)
    return out

# --- Baseline servable: KM por estrato ---
strata = {'_overall': km_stats(df)}
for (ente, nat), sub in df.groupby(['ente_tipo', 'natureza']):
    if len(sub) < 200:   # estrato raso → cai no overall
        continue
    strata[f'{ente}|{nat}'] = km_stats(sub)
# também por ente_tipo só (fallback intermediário)
for ente, sub in df.groupby('ente_tipo'):
    if len(sub) >= 200:
        strata[f'{ente}|*'] = km_stats(sub)

with open('/tmp/surv_strata.json', 'w') as f:
    json.dump(strata, f, ensure_ascii=False, indent=1)
print(f'\nestratos servable: {len(strata)}')
for k in list(strata)[:8]:
    s = strata[k]; print(f'  {k}: n={s["n"]:,} chance12m={s.get("chance_12m")}% chance24m={s.get("chance_24m")}% mediana={s.get("tempo_mediano_dias")}d')

# --- Cox com split temporal (C-index) ---
try:
    cut = int(df['coorte_ano'].quantile(0.7))
    tr, te = df[df.coorte_ano <= cut], df[df.coorte_ano > cut]
    if len(te) > 500 and te.evento.sum() > 50:
        feats = ['ente_tipo', 'natureza']
        X = pd.get_dummies(df[feats + ['duracao_dias', 'evento', 'coorte_ano']], columns=feats, drop_first=True)
        cols = [c for c in X.columns if c not in ('duracao_dias', 'evento', 'coorte_ano')]
        cph = CoxPHFitter(penalizer=0.1)
        cph.fit(X[X.coorte_ano <= cut][cols + ['duracao_dias', 'evento']], 'duracao_dias', 'evento')
        Xte = X[X.coorte_ano > cut]
        risk = -cph.predict_partial_hazard(Xte[cols])
        cidx = concordance_index(Xte['duracao_dias'], risk, Xte['evento'])
        print(f'\nCox split-temporal (treino≤{cut}/teste>{cut}): C-index={cidx:.3f} | n_teste={len(te):,}')
    else:
        print('\nCox: teste insuficiente p/ split temporal.')
except Exception as e:
    print('\nCox falhou:', str(e)[:150])
print('\nOK — /tmp/surv_strata.json')
