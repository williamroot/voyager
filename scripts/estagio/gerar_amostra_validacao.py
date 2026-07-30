"""Amostra de validação humana do modelo de ESTÁGIO (entrega de aceite).

Monta o pacote pedido pelo usuário:
  A) 10 processos CONHECIDOS (rótulo de autos) onde o modelo ACERTA — com o
     rótulo, a evidência dos autos (âncora da verdade) e os sinais públicos;
  B) 5 EMITIDO NOVOS (sem rótulo — predição cega de alta confiança);
  C) 5 DC NOVOS (idem).
Foco TJSP. Âncoras legíveis: evidência do rótulo + sinais públicos por extenso.

Uso (máquina de treino):
  python gerar_amostra_validacao.py \
      --features out/estagio_features.csv.gz --labels out/estagio_labels.jsonl.gz \
      --unlabeled out/unlabeled_tjsp_features.csv.gz \
      --model out/estagio_gbm_v1.joblib --outdir out/
"""
# ruff: noqa: RUF001 — pt-BR usa sinal de multiplicacao
from __future__ import annotations

import argparse
import gzip
import json
import os

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

CLASSES = ['DC', 'PRE', 'EMITIDO', 'MORTO']


def hash_frac(cnj: str) -> float:
    """MESMO hash saltado do train_estagio.py (split, não o cap)."""
    import hashlib  # noqa: PLC0415
    return int(hashlib.sha1(f'split|{cnj}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _prep(df: pd.DataFrame, bundle: dict) -> np.ndarray:
    x = np.empty((len(df), len(bundle['features'])), dtype=float)
    cat_maps = bundle['cat_maps']
    for j, f in enumerate(bundle['features']):
        if f in cat_maps:
            idx = {v: i for i, v in enumerate(cat_maps[f])}
            x[:, j] = [idx.get('' if pd.isna(v) else str(v), -1)
                       for v in df[f].tolist()]
        elif f == 'log_valor_homologado':
            x[:, j] = np.log1p(pd.to_numeric(df['valor_homologado'], errors='coerce'))
        else:
            x[:, j] = pd.to_numeric(df[f], errors='coerce')
    return x


def _decidir(proba: np.ndarray, bundle: dict) -> np.ndarray:
    """Mesma regra de decisão da lib: MORTO só acima do threshold calibrado."""
    pred = proba.argmax(axis=1)
    thr = (bundle.get('thresholds') or {}).get('MORTO')
    if thr is not None:
        mi = CLASSES.index('MORTO')
        mask = (pred == mi) & (proba[:, mi] < float(thr))
        if mask.any():
            resto = proba.copy()
            resto[:, mi] = -1.0
            pred[mask] = resto[mask].argmax(axis=1)
    return pred


def sinais_de_linha(r: pd.Series) -> list[str]:  # noqa: PLR0912
    """Âncoras públicas legíveis a partir da linha de features."""
    def n(campo):
        v = r.get(campo)
        return 0 if pd.isna(v) else int(float(v))

    s = []
    if n('exped_tc_n') or n('exped_forte_n'):
        s.append(f"expedição de precatório/RPV publicada (tipo_comunicação n={n('exped_tc_n')}, "
                 f"texto forte n={n('exped_forte_n')})")
    if n('oficio_text_n'):
        s.append(f"'ofício requisitório' no texto público (n={n('oficio_text_n')})")
    if n('precat_text_n'):
        s.append(f"'precatório' no texto público (n={n('precat_text_n')})")
    if n('rpv_text_n'):
        s.append(f"RPV/pequeno valor no texto (n={n('rpv_text_n')})")
    if n('transito_n'):
        s.append(f"trânsito em julgado publicado (n={n('transito_n')})")
    if n('homolog_n'):
        s.append(f"homologação de cálculos (n={n('homolog_n')})")
    if n('cumpr_text_n'):
        s.append(f"'cumprimento de sentença' no texto (n={n('cumpr_text_n')})")
    if n('pago_n'):
        s.append(f"alvará/levantamento/sequestro publicado (n={n('pago_n')})")
    if n('ext_satisf_n'):
        s.append(f"extinção pelo pagamento no texto (n={n('ext_satisf_n')})")
    if n('improc_n'):
        s.append(f"improcedência/prescrição no texto (n={n('improc_n')})")
    if not pd.isna(r.get('dias_ult_mov')):
        s.append(f"última mov pública há {int(float(r['dias_ult_mov']))}d")
    if r.get('classe_codigo') and not pd.isna(r.get('classe_codigo')):
        s.append(f"classe TPU {r['classe_codigo']}")
    v = r.get('valor_homologado')
    if v not in (None, '') and not pd.isna(v):
        s.append(f"valor homologado detectado: R$ {float(v):,.2f}")
    p = r.get('partes_beneficiarias')
    if isinstance(p, str) and p:
        s.append(f"partes (polo ativo): {p[:120]}")
    return s


def main() -> None:  # noqa: PLR0912
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--features', required=True)
    ap.add_argument('--labels', required=True)
    ap.add_argument('--unlabeled', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--tribunal', default='TJSP')
    ap.add_argument('--outdir', default='out')
    args = ap.parse_args()

    bundle = joblib.load(args.model)
    booster = lgb.Booster(model_str=bundle['booster_str'])

    evid = {}
    with gzip.open(args.labels, 'rt') as f:
        for line in f:
            r = json.loads(line)
            if r['tribunal'] == args.tribunal:
                evid[r['numero_cnj']] = r

    df = pd.read_csv(args.features, dtype={'classe_codigo': str, 'assunto_codigo': str},
                     low_memory=False)
    df = df[(df['tribunal'] == args.tribunal) & df['classe'].isin(CLASSES)].copy()
    df['hfrac'] = df['numero_cnj'].map(hash_frac)
    te = df[df['hfrac'] >= 0.80].copy()          # só o teste (nunca visto no treino)
    proba = booster.predict(_prep(te, bundle))
    te['pred'] = [CLASSES[i] for i in _decidir(proba, bundle)]
    te['pred_proba'] = proba.max(axis=1)

    certos = te[te['pred'] == te['classe']].sort_values('pred_proba', ascending=False)
    conhecidos = []
    # mistura de classes: round-robin até 10
    por_classe = {c: certos[certos['classe'] == c] for c in CLASSES}
    idxs = dict.fromkeys(CLASSES, 0)
    while len(conhecidos) < 10:
        progresso = False
        for c in ['EMITIDO', 'MORTO', 'DC', 'PRE']:
            sub = por_classe[c]
            if idxs[c] < len(sub) and len(conhecidos) < 10:
                conhecidos.append(sub.iloc[idxs[c]])
                idxs[c] += 1
                progresso = True
        if not progresso:
            break

    un = pd.read_csv(args.unlabeled, dtype={'classe_codigo': str, 'assunto_codigo': str},
                     low_memory=False)
    un = un[un['tribunal'] == args.tribunal].copy()
    pu = booster.predict(_prep(un, bundle))
    un['pred'] = [CLASSES[i] for i in _decidir(pu, bundle)]
    un['pred_proba'] = pu.max(axis=1)
    novos_emitido = un[un['pred'] == 'EMITIDO'].sort_values('pred_proba', ascending=False).head(5)
    novos_dc = un[un['pred'] == 'DC'].sort_values('pred_proba', ascending=False).head(5)

    os.makedirs(args.outdir, exist_ok=True)
    md = [f'# Amostra de validação — estágio do crédito ({args.tribunal})',
          '',
          'Modelo: `estagio_v1` (LightGBM multi-classe). Seções: (A) conhecidos',
          'em que o modelo acerta o rótulo dos autos — no split de TESTE, nunca',
          'vistos no treino; (B/C) predições cegas em processos SEM rótulo.',
          '']
    jl = []

    md.append('## A) 10 conhecidos (rótulo dos autos × modelo) — modelo ACERTOU')
    for r in conhecidos:
        lab = evid.get(r['numero_cnj'], {})
        md.append(f"\n### {r['numero_cnj']} — rótulo **{r['classe']}**"
                  + (f" ({lab.get('subtipo')})" if lab.get('subtipo') else '')
                  + f" · predito {r['pred']} (p={r['pred_proba']:.2f})")
        md.append('- Verdade (autos/Falcon):')
        for e in lab.get('evidencias', [])[:6]:
            md.append(f'  - `{e[:220]}`')
        md.append('- Sinais públicos (features):')
        for s in sinais_de_linha(r):
            md.append(f'  - {s}')
        jl.append({'secao': 'conhecido', 'numero_cnj': r['numero_cnj'],
                   'rotulo': r['classe'], 'subtipo': lab.get('subtipo'),
                   'pred': r['pred'], 'proba': round(float(r['pred_proba']), 4),
                   'evidencias': lab.get('evidencias', []),
                   'sinais': sinais_de_linha(r)})

    for titulo, sub, secao in (('B) 5 EMITIDO novos (sem rótulo — validar)', novos_emitido, 'novo_emitido'),
                               ('C) 5 DC novos (sem rótulo — validar)', novos_dc, 'novo_dc')):
        md.append(f'\n## {titulo}')
        for _, r in sub.iterrows():
            md.append(f"\n### {r['numero_cnj']} — predito **{r['pred']}** (p={r['pred_proba']:.2f})")
            for s in sinais_de_linha(r):
                md.append(f'- {s}')
            jl.append({'secao': secao, 'numero_cnj': r['numero_cnj'],
                       'pred': r['pred'], 'proba': round(float(r['pred_proba']), 4),
                       'sinais': sinais_de_linha(r)})

    p_md = os.path.join(args.outdir, f'amostra_validacao_{args.tribunal.lower()}.md')
    p_jl = os.path.join(args.outdir, f'amostra_validacao_{args.tribunal.lower()}.jsonl')
    with open(p_md, 'w') as f:
        f.write('\n'.join(md) + '\n')
    with open(p_jl, 'w') as f:
        for r in jl:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f'{p_md} ({len(conhecidos)} conhecidos, {len(novos_emitido)} EMITIDO novos, '
          f'{len(novos_dc)} DC novos)')


if __name__ == '__main__':
    main()
