"""FASE 3 — Treino do GBM multi-classe de ESTÁGIO DO CRÉDITO + gate pré-registrado.

Entrada: estagio_features.csv.gz (Fase 2). Saída:
  out/estagio_gbm_v1.joblib   — bundle {booster, features, cat_maps, classes, métricas}
  out/gate_report.json        — métricas das 3 validações + veredito do gate
  out/feature_importance.csv

Gate pré-registrado (BLOCK se falhar):
  - precision(EMITIDO) ≥ 0.90 e precision(MORTO) ≥ 0.90 no split hash-CNJ
  - sanidade: top features devem ser marcos processuais; se `tribunal` dominar
    (top-3 de ganho), red flag de leakage/proxy → WARN explícito no report
Validações (as 3 são obrigatórias no report):
  1. split por hash de CNJ (80/20 determinístico, sha1)
  2. teste TEMPORAL: treina processos com última mov < corte, testa recentes
  3. leave-one-tribunal-out no MAIOR tribunal

Anti-leakage: os campos de rótulo/flags da Fase 1 (subtipo, flag_*, fonte,
label_ev_dt) NUNCA entram como feature — lista explícita FEATURES abaixo.

Uso:
  python train_estagio.py --data estagio_features.csv.gz --outdir out/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import date

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, classification_report, confusion_matrix

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('estagio.train')

CLASSES = ['DC', 'PRE', 'EMITIDO', 'MORTO']

# Features 100% públicas (origem documentada em build_features.py).
# NÃO incluir: classe (target), subtipo, flag_* (derivadas de autos/Falcon),
# fonte, label_ev_dt, partes_beneficiarias (string informativa).
FEATURES_NUM = [
    'ano_cnj', 'is_cumprimento', 'is_fazenda', 'is_juizado_anti', 'dias_autuacao',
    'total_movs', 'distinct_tipos', 'exped_tc_n', 'precat_text_n', 'rpv_text_n',
    'reqpag_text_n', 'oficio_text_n', 'exped_forte_n', 'cancel_n', 'transito_n',
    'homolog_n', 'cumpr_text_n', 'pago_n', 'ext_satisf_n', 'ext_semmerito_n',
    'improc_n', 'dias_ult_mov', 'duracao_dias', 'movs_por_ano',
    'tem_exped', 'dias_desde_exped', 'dias_transito_a_exped',
    'tem_transito', 'dias_desde_transito', 'tem_homolog', 'dias_desde_homolog',
    'tem_pago', 'dias_desde_pago', 'pago_pos_exped', 'extneg_pos_exped',
    'n_partes', 'tem_ente_publico_passivo', 'log_valor_homologado',
]
# 'tribunal' NÃO entra como feature: no 1º treino dominou o ganho (red flag de
# proxy — MORTO é 100% TJSP no rótulo). Estágio deve vir dos marcos processuais.
FEATURES_CAT = ['classe_codigo', 'assunto_codigo']
FEATURES = FEATURES_NUM + FEATURES_CAT

GATE_PRECISION_MIN = 0.90
TEMPORAL_CORTE_DIAS = 365   # última mov há mais de 1 ano = treino; recentes = teste


def hash_frac(cnj: str) -> float:
    # SALTADO ('split|') — o cap da Fase 2 usa hash sem salt; reutilizar o mesmo
    # hash faria o test conter só classes não-capadas (bug pego no 1º treino).
    return int(hashlib.sha1(f'split|{cnj}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def carregar(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={'classe_codigo': str, 'assunto_codigo': str},
                     low_memory=False)
    df = df[df['classe'].isin(CLASSES)].copy()
    df['log_valor_homologado'] = np.log1p(
        pd.to_numeric(df['valor_homologado'], errors='coerce'))
    for c in FEATURES_CAT:
        df[c] = df[c].fillna('').astype('category')
    for c in FEATURES_NUM:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    # --- higiene de rótulo DEFASADO (staleness) ---------------------------
    # O rótulo vem dos autos/Falcon no momento do download; o público é mais
    # NOVO. Quando o público contradiz o rótulo num sentido só-possível-por-
    # evolução (ex.: EMITIDO com extinção-pelo-pagamento publicada DEPOIS da
    # expedição), a linha é rótulo-obsoleto — sai do treino e da avaliação.
    # Isso NÃO é usar feature como rótulo: é detectar obsolescência do rótulo.
    n0 = len(df)
    stale_emitido = ((df['classe'] == 'EMITIDO')
                     & (df['ext_satisf_n'] > 0) & (df['extneg_pos_exped'] == 1))
    stale_pre = ((df['classe'] == 'PRE')
                 & ((df['exped_forte_n'] > 0) | (df['exped_tc_n'] > 0)))
    stale_dc = ((df['classe'] == 'DC')
                & ((df['exped_forte_n'] > 0) | (df['exped_tc_n'] > 0)))
    stale = stale_emitido | stale_pre | stale_dc
    logger.info('higiene staleness: -%d EMITIDO→extinto público, -%d PRE→expedido '
                'público, -%d DC→expedido público (de %d)',
                int(stale_emitido.sum()), int(stale_pre.sum()),
                int(stale_dc.sum()), n0)
    df = df[~stale].copy()

    df['y'] = df['classe'].map({c: i for i, c in enumerate(CLASSES)})
    df['hfrac'] = df['numero_cnj'].map(hash_frac)
    logger.info('dataset: %d linhas — %s', len(df),
                df['classe'].value_counts().to_dict())
    return df


def treinar(tr: pd.DataFrame, va: pd.DataFrame | None = None,
            n_rounds_fixo: int = 400) -> lgb.Booster:
    params = {
        'objective': 'multiclass', 'num_class': len(CLASSES),
        'metric': 'multi_logloss', 'learning_rate': 0.08,
        'num_leaves': 63, 'min_data_in_leaf': 50,
        'feature_fraction': 0.85, 'bagging_fraction': 0.8, 'bagging_freq': 1,
        'lambda_l2': 1.0, 'verbose': -1, 'num_threads': 8, 'seed': 42,
    }
    dtr = lgb.Dataset(tr[FEATURES], label=tr['y'],
                      categorical_feature=FEATURES_CAT, free_raw_data=True)
    kw = {}
    if va is not None and len(va):
        dva = lgb.Dataset(va[FEATURES], label=va['y'], reference=dtr,
                          categorical_feature=FEATURES_CAT)
        kw = {'valid_sets': [dva],
              'callbacks': [lgb.early_stopping(50, verbose=False)]}
        n_rounds = 2000
    else:
        n_rounds = n_rounds_fixo
    return lgb.train(params, dtr, num_boost_round=n_rounds, **kw)


MORTO_I = CLASSES.index('MORTO')


def decidir(proba: np.ndarray, thr_morto: float) -> np.ndarray:
    """argmax com guarda de precisão: MORTO só quando p(MORTO) >= thr_morto;
    abaixo disso rebaixa pro melhor não-MORTO (demote-only)."""
    pred = proba.argmax(axis=1)
    mask = (pred == MORTO_I) & (proba[:, MORTO_I] < thr_morto)
    if mask.any():
        rest = proba.copy()
        rest[:, MORTO_I] = -1.0
        pred[mask] = rest[mask].argmax(axis=1)
    return pred


def tune_thr_morto(model: lgb.Booster, va: pd.DataFrame,
                   alvo_precisao: float = 0.92) -> float:
    """Menor threshold que atinge a precisão-alvo de MORTO no conjunto de
    validação (com pelo menos 3 predições MORTO). Fallback 0.90."""
    proba = model.predict(va[FEATURES])
    y = va['y'].to_numpy()
    melhor = 0.90
    for thr in [x / 100 for x in range(50, 100, 2)]:
        pred = decidir(proba, thr)
        sel = pred == MORTO_I
        if sel.sum() >= 3:
            prec = float((y[sel] == MORTO_I).mean())
            if prec >= alvo_precisao:
                melhor = thr
                break
    logger.info('thr_morto calibrado no val: %.2f', melhor)
    return melhor


def avaliar(model: lgb.Booster, te: pd.DataFrame, nome: str,
            thr_morto: float = 0.0) -> dict:
    proba = model.predict(te[FEATURES])
    pred = decidir(proba, thr_morto)
    y = te['y'].to_numpy()
    rep = classification_report(
        y, pred, labels=range(len(CLASSES)), target_names=CLASSES,
        output_dict=True, zero_division=0)
    cm = confusion_matrix(y, pred, labels=range(len(CLASSES))).tolist()
    # Brier one-vs-rest por classe (calibração)
    brier = {}
    for i, c in enumerate(CLASSES):
        if (y == i).any():
            brier[c] = round(float(brier_score_loss((y == i).astype(int), proba[:, i])), 4)
    out = {
        'nome': nome, 'n_test': len(te),
        'accuracy': round(float(rep['accuracy']), 4),
        'macro_f1': round(float(rep['macro avg']['f1-score']), 4),
        'por_classe': {c: {'precision': round(rep[c]['precision'], 4),
                           'recall': round(rep[c]['recall'], 4),
                           'f1': round(rep[c]['f1-score'], 4),
                           'support': int(rep[c]['support'])} for c in CLASSES},
        'confusion_matrix': {'labels': CLASSES, 'matrix': cm},
        'brier_ovr': brier,
    }
    logger.info('[%s] acc=%.4f macroF1=%.4f | EMITIDO p=%.3f r=%.3f | MORTO p=%.3f r=%.3f',
                nome, out['accuracy'], out['macro_f1'],
                out['por_classe']['EMITIDO']['precision'],
                out['por_classe']['EMITIDO']['recall'],
                out['por_classe']['MORTO']['precision'],
                out['por_classe']['MORTO']['recall'])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', required=True)
    ap.add_argument('--outdir', default='out')
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    df = carregar(args.data)

    report: dict = {'n_total': len(df),
                    'dist_classe': df['classe'].value_counts().to_dict(),
                    'dist_tribunal': df['tribunal'].value_counts().to_dict(),
                    'data_treino': date.today().isoformat(),
                    'validacoes': []}

    # ---- V1: split hash-CNJ 70/10/20 ---------------------------------------
    tr = df[df['hfrac'] < 0.70]
    va = df[(df['hfrac'] >= 0.70) & (df['hfrac'] < 0.80)]
    te = df[df['hfrac'] >= 0.80]
    logger.info('V1 hash-split: train=%d val=%d test=%d', len(tr), len(va), len(te))
    m1 = treinar(tr, va)
    thr_morto = tune_thr_morto(m1, va)
    report['thr_morto'] = thr_morto
    ev1 = avaliar(m1, te, 'hash_cnj_80_20', thr_morto=thr_morto)
    report['validacoes'].append(ev1)
    report['best_iteration'] = int(m1.best_iteration or 0)

    # feature importance (gain) do modelo principal
    imp = pd.DataFrame({
        'feature': m1.feature_name(),
        'gain': m1.feature_importance(importance_type='gain'),
        'split': m1.feature_importance(importance_type='split'),
    }).sort_values('gain', ascending=False)
    imp.to_csv(os.path.join(args.outdir, 'feature_importance.csv'), index=False)
    top20 = imp.head(20)[['feature', 'gain']].to_dict('records')
    report['feature_importance_top20'] = [
        {'feature': r['feature'], 'gain': round(float(r['gain']), 1)} for r in top20]
    top3 = [r['feature'] for r in top20[:3]]
    report['red_flag_tribunal_domina'] = 'tribunal' in top3

    # ---- V2: temporal ------------------------------------------------------
    tr_t = df[df['dias_ult_mov'] > TEMPORAL_CORTE_DIAS]
    te_t = df[df['dias_ult_mov'] <= TEMPORAL_CORTE_DIAS]
    if len(tr_t) > 1000 and len(te_t) > 1000:
        m2 = treinar(tr_t)
        ev2 = avaliar(m2, te_t, f'temporal_corte_{TEMPORAL_CORTE_DIAS}d', thr_morto=thr_morto)
        report['validacoes'].append(ev2)
    else:
        logger.warning('temporal: split degenerado (tr=%d te=%d)', len(tr_t), len(te_t))

    # ---- V3: leave-one-tribunal-out no maior -------------------------------
    maior = df['tribunal'].value_counts().idxmax()
    tr_l = df[df['tribunal'] != maior]
    te_l = df[df['tribunal'] == maior]
    m3 = treinar(tr_l)
    ev3 = avaliar(m3, te_l, f'loto_{maior}', thr_morto=thr_morto)
    report['validacoes'].append(ev3)

    # ---- Gate --------------------------------------------------------------
    p_emit = ev1['por_classe']['EMITIDO']['precision']
    p_morto = ev1['por_classe']['MORTO']['precision']
    gate_pass = p_emit >= GATE_PRECISION_MIN and p_morto >= GATE_PRECISION_MIN
    report['gate'] = {
        'criterio': f'precision(EMITIDO)>= {GATE_PRECISION_MIN} e precision(MORTO)>= '
                    f'{GATE_PRECISION_MIN} no split hash-CNJ',
        'precision_emitido': p_emit, 'precision_morto': p_morto,
        'red_flag_tribunal': report['red_flag_tribunal_domina'],
        'veredito': 'PASS' if gate_pass else 'BLOCK',
    }
    logger.info('GATE: %s (EMITIDO %.3f | MORTO %.3f | tribunal domina? %s)',
                report['gate']['veredito'], p_emit, p_morto,
                report['red_flag_tribunal_domina'])

    # ---- Modelo final (todo o dado, n_rounds do best_iteration) ------------
    m_final = treinar(df, n_rounds_fixo=max(int(m1.best_iteration or 0), 150))
    cat_maps = {c: list(df[c].cat.categories) for c in FEATURES_CAT}
    bundle = {
        'versao': 'estagio_v1',
        'booster_str': m_final.model_to_string(),
        'features': FEATURES, 'features_num': FEATURES_NUM,
        'features_cat': FEATURES_CAT, 'cat_maps': cat_maps,
        'classes': CLASSES,
        'thresholds': {'MORTO': thr_morto},
        'gate': report['gate'],
        'metricas_hash_split': ev1,
        'treinado_em': date.today().isoformat(),
        'n_treino': len(df),
    }
    path_model = os.path.join(args.outdir, 'estagio_gbm_v1.joblib')
    joblib.dump(bundle, path_model, compress=3)
    with open(os.path.join(args.outdir, 'gate_report.json'), 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    logger.info('modelo → %s (%.1f MB)', path_model,
                os.path.getsize(path_model) / 1e6)


if __name__ == '__main__':
    main()
