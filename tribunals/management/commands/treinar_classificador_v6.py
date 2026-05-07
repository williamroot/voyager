"""Treina classificador v6 com ground truth TRF1 + TRF3.

Melhorias sobre v5:
  - Incorpora ground truth TRF3 (lead_trf3)
  - Incorpora novos leads TRF1 confirmados desde 2026-04-30
  - Recomputa constantes de normalização do universo atual
  - Mesmo algoritmo: Logistic Regression batch GD + L2 (numpy puro)
  - Feature extraction via query única no movimentacoes (não por processo)

Garantias de não-regressão:
  - AUC: tolerância 0.01 (v5=0.9523 → mínimo 0.9423)
  - precision@5000: tolerância 0.02 (v5=0.939 → mínimo 0.919)

Se --deploy: atualiza WEIGHTS/VERSAO/normas em tribunals/classificador.py
diretamente. Reiniciar workers ativa o novo modelo.

Uso:
  python manage.py treinar_classificador_v6
  python manage.py treinar_classificador_v6 --deploy
  python manage.py treinar_classificador_v6 --epochs 600 --lr 0.3
"""
from __future__ import annotations

import math
import re
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

# ── constantes v5 (baseline para comparação) ─────────────────────────────────
FEATURE_NAMES = [
    'F1_cumprim', 'F10_juizado_ANTI', 'F2_precat_tc', 'F7_envTrib_tc',
    'F11_precat_text', 'F12_rpv_text', 'F13_reqPag_text', 'F14_oficio_text',
    'F15_logMovs', 'F16_logTipos', 'F17_logN1count', 'F18_anoZ',
    'F19_cancelado_ANTI', 'F20_exp_juriscope', 'F21_diasUltMovZ', 'F23_logPartes',
    'F1xF11', 'F1xF15', 'F1xF20',
]

V5 = {
    'versao': 'v5',
    'intercept': -3.196,
    'weights': {
        'F1_cumprim': 1.922, 'F10_juizado_ANTI': -1.129,
        'F2_precat_tc': 0.079, 'F7_envTrib_tc': 0.085,
        'F11_precat_text': 0.894, 'F12_rpv_text': 0.527,
        'F13_reqPag_text': -0.560, 'F14_oficio_text': -0.186,
        'F15_logMovs': 2.311, 'F16_logTipos': -1.738,
        'F17_logN1count': 0.181, 'F18_anoZ': 0.438,
        'F19_cancelado_ANTI': -0.000, 'F20_exp_juriscope': -0.025,
        'F21_diasUltMovZ': 0.570, 'F23_logPartes': -0.401,
        'F1xF11': -0.134, 'F1xF15': 1.612, 'F1xF20': -0.021,
    },
    'metricas': {
        'auc': 0.9523,
        'precision_at_500': 0.978, 'precision_at_1000': 0.969,
        'precision_at_2500': 0.962, 'precision_at_5000': 0.939,
        'precision_at_10000': 0.919,
    },
    'ano_mean': 2018.9, 'ano_std': 6.6,
    'dias_mean': 687.0, 'dias_std': 570.0,
}

CLASSES_CUMPRIMENTO = {'12078', '156', '15160', '15215', '12079'}
_CNJ_ANO_RE = re.compile(r'^\d{7}-\d{2}\.(\d{4})\.')


def _ano_cnj(numero: str) -> int:
    m = _CNJ_ANO_RE.match(numero or '')
    return int(m.group(1)) if m else 0


def _is_anti_classe(nome: str) -> int:
    n = (nome or '').lower()
    return int('juizado especial' in n or 'recurso inominado' in n or 'procedimento comum' in n)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _auc(y, scores):
    order = np.argsort(-scores)
    ys = y[order]
    n_pos, n_neg = y.sum(), (1 - y).sum()
    if n_pos == 0 or n_neg == 0:
        return 0.0
    tpr = np.cumsum(ys) / n_pos
    fpr = np.cumsum(1 - ys) / n_neg
    return float(np.trapz(tpr, fpr))


def _prec_at_k(y, scores, k):
    k = min(k, len(y))
    top = np.argsort(-scores)[:k]
    return float(y[top].sum() / k)


# ── SQL de extração de features ───────────────────────────────────────────────
_SQL_MOVS = """
    SELECT
        m.processo_id,
        COUNT(*)                                                                              AS total_movs,
        COUNT(DISTINCT CASE WHEN m.tipo_comunicacao <> '' THEN m.tipo_comunicacao END)        AS distinct_tipos,
        MAX(m.data_disponibilizacao)                                                          AS ult_mov_dt,
        SUM(CASE WHEN m.tipo_comunicacao IN
            ('Expedição de precatório/rpv','Precatório')                THEN 1 ELSE 0 END)   AS f2_n,
        SUM(CASE WHEN m.tipo_comunicacao IN
            ('Enviada ao Tribunal','Preparada para Envio')              THEN 1 ELSE 0 END)   AS f7_n,
        SUM(CASE WHEN m.texto ~* 'precat[óo]rio'                       THEN 1 ELSE 0 END)   AS f11_n,
        SUM(CASE WHEN m.texto ~* '\\mrpv\\M'                           THEN 1 ELSE 0 END)   AS f12_n,
        SUM(CASE WHEN m.texto ~* 'requisi[çc][ãa]o de pagamento'       THEN 1 ELSE 0 END)   AS f13_n,
        SUM(CASE WHEN m.texto ~* 'of[íi]cio requisit[óo]rio'          THEN 1 ELSE 0 END)   AS f14_n,
        SUM(CASE WHEN m.texto ~* 'cancelamento de precat[óo]rio|cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|revoga[çc][ãa]o de rpv'
                                                                       THEN 1 ELSE 0 END)   AS f19_n,
        SUM(CASE WHEN m.texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                                                                       THEN 1 ELSE 0 END)   AS f20_n
    FROM tribunals_movimentacao m
    WHERE m.tribunal_id IN ({placeholders})
    GROUP BY m.processo_id
"""

_SQL_PARTES = """
    SELECT pp.processo_id, COUNT(*) AS n_partes
    FROM tribunals_processoparte pp
    JOIN tribunals_process p ON p.id = pp.processo_id
    WHERE p.tribunal_id IN ({placeholders})
    GROUP BY pp.processo_id
"""

_SQL_PROC = """
    SELECT id, numero_cnj, COALESCE(classe_codigo,''), COALESCE(classe_nome,'')
    FROM tribunals_process
    WHERE tribunal_id IN ({placeholders}) AND total_movimentacoes > 0
"""


class Command(BaseCommand):
    help = 'Treina classificador v6 (TRF1+TRF3) e compara com v5.'

    def add_arguments(self, parser):
        parser.add_argument('--deploy', action='store_true',
                            help='Atualiza classificador.py e ClassificadorVersao se sem regressão.')
        parser.add_argument('--epochs', type=int, default=400)
        parser.add_argument('--lr', type=float, default=0.5)
        parser.add_argument('--l2', type=float, default=0.0005)
        parser.add_argument('--seed', type=int, default=42)
        parser.add_argument('--tribunais', default='TRF1,TRF3',
                            help='Tribunais separados por vírgula.')

    def handle(self, *args, **opts):
        tribunais = [t.strip().upper() for t in opts['tribunais'].split(',')]
        t_start = time.time()
        self.stdout.write(self.style.NOTICE(
            f'\n🏋  Treinamento v6  |  tribunais={",".join(tribunais)}'
            f'  |  epochs={opts["epochs"]}  lr={opts["lr"]}  l2={opts["l2"]}\n'
        ))

        # ── 1. ground truth ──────────────────────────────────────────────────
        self._hdr('1/5  Ground truth')
        gt = self._load_gt(tribunais)

        # ── 2. feature extraction ────────────────────────────────────────────
        self._hdr('2/5  Extração de features')
        mov_map, parte_map, proc_map = self._extract(tribunais)

        # ── 3. matriz X / vetor y ────────────────────────────────────────────
        self._hdr('3/5  Montando matriz X e labels y')
        X, y, norm = self._build(mov_map, parte_map, proc_map, gt)

        # ── 4. split + treino ────────────────────────────────────────────────
        self._hdr(f'4/5  Split 80/20 estratificado + {opts["epochs"]} épocas GD')
        res = self._train(X, y, opts)

        # ── 5. avaliação ─────────────────────────────────────────────────────
        self._hdr('5/5  Avaliação e comparação com v5')
        regressao = self._report(res, norm)

        # ── deploy ───────────────────────────────────────────────────────────
        if opts['deploy']:
            if regressao:
                self.stdout.write(self.style.ERROR(
                    '\n❌  Regressão detectada — deploy abortado.\n'
                    '    Ajuste hiperparâmetros ou investigue as features.\n'
                ))
            else:
                self._deploy(res, norm, opts, tribunais)
        else:
            self.stdout.write(
                '\nPasse --deploy para atualizar classificador.py e subir v6.'
            )

        elapsed = timedelta(seconds=int(time.time() - t_start))
        self.stdout.write(f'\n⏱  Tempo total: {elapsed}\n')

    # ─────────────────────────────────────────────────────────────────────────

    def _hdr(self, msg):
        self.stdout.write(self.style.NOTICE(f'\n[{time.strftime("%H:%M:%S")}] {msg}'))

    def _load_gt(self, tribunais):
        gt = set()
        with connection.cursor() as c:
            for trib in tribunais:
                table = f'lead_{trib.lower()}'
                try:
                    c.execute(f'SELECT numero_cnj FROM {table}')  # noqa: S608
                    rows = c.fetchall()
                    gt.update(r[0] for r in rows)
                    self.stdout.write(f'  {table}: {len(rows):,} CNJs')
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f'  {table}: ignorada ({exc})'))
        self.stdout.write(f'  Total ground truth: {len(gt):,} CNJs únicos')
        return gt

    def _extract(self, tribunais):
        ph = ', '.join(['%s'] * len(tribunais))

        self.stdout.write('  movimentacoes (query única — pode levar minutos)...')
        t = time.time()
        with connection.cursor() as c:
            c.execute('SET statement_timeout = 0')
            c.execute(_SQL_MOVS.format(placeholders=ph), tribunais)
            rows_mov = c.fetchall()
        mov_map = {r[0]: r[1:] for r in rows_mov}
        self.stdout.write(f'  → {len(mov_map):,} processos c/ movs  ({time.time()-t:.0f}s)')

        self.stdout.write('  partes...')
        t = time.time()
        with connection.cursor() as c:
            c.execute(_SQL_PARTES.format(placeholders=ph), tribunais)
            parte_map = {r[0]: r[1] for r in c.fetchall()}
        self.stdout.write(f'  → {len(parte_map):,} processos c/ partes  ({time.time()-t:.0f}s)')

        self.stdout.write('  process (classe, CNJ)...')
        t = time.time()
        with connection.cursor() as c:
            c.execute(_SQL_PROC.format(placeholders=ph), tribunais)
            proc_map = {r[0]: (r[1], r[2], r[3]) for r in c.fetchall()}
        self.stdout.write(f'  → {len(proc_map):,} processos  ({time.time()-t:.0f}s)')

        return mov_map, parte_map, proc_map

    def _build(self, mov_map, parte_map, proc_map, gt):
        now = timezone.now()
        pids, X_rows, y_vals = [], [], []
        anos_raw, dias_raw_list = [], []

        for pid, mov_data in mov_map.items():
            proc = proc_map.get(pid)
            if not proc:
                continue
            cnj, cls_cod, cls_nome = proc
            (total_movs, distinct_tipos, ult_mov_dt,
             f2_n, f7_n, f11_n, f12_n, f13_n, f14_n, f19_n, f20_n) = mov_data

            ano = _ano_cnj(cnj)
            f1 = int(cls_cod in CLASSES_CUMPRIMENTO)
            f10 = _is_anti_classe(cls_nome)
            dias = ((now - ult_mov_dt).total_seconds() / 86400) if ult_mov_dt else 9999.0
            n_partes = parte_map.get(pid, 0)

            anos_raw.append(ano if ano > 0 else None)
            dias_raw_list.append(dias)
            pids.append(pid)
            X_rows.append([
                f1, f10, f2_n, f7_n, f11_n, f12_n, f13_n, f14_n,
                total_movs, distinct_tipos,
                f11_n + f12_n + f13_n + f14_n,  # n1count (F17 raw)
                ano, f19_n, f20_n, dias, n_partes,
                f1 * f11_n, f1 * total_movs, f1 * f20_n,
            ])
            y_vals.append(1 if cnj in gt else 0)

        # Constantes de normalização (recomputadas do universo atual)
        valid_anos = [a for a in anos_raw if a is not None]
        ano_mean = float(np.mean(valid_anos)) if valid_anos else V5['ano_mean']
        ano_std = max(float(np.std(valid_anos)), 1e-6) if valid_anos else V5['ano_std']
        dias_arr = np.array(dias_raw_list)
        dias_mean = float(dias_arr.mean())
        dias_std = max(float(dias_arr.std()), 1e-6)
        norm = {'ano_mean': round(ano_mean, 2), 'ano_std': round(ano_std, 2),
                'dias_mean': round(dias_mean, 2), 'dias_std': round(dias_std, 2)}
        self.stdout.write(
            f'  Normalização  ano={ano_mean:.1f}±{ano_std:.1f}'
            f'  dias={dias_mean:.0f}±{dias_std:.0f}'
        )

        # Constrói matrix de features na ordem de FEATURE_NAMES
        raw = np.array(X_rows, dtype=np.float64)
        f1_col = raw[:, 0]
        total_movs_col = raw[:, 8]
        f11_col = raw[:, 4]
        f20_col = raw[:, 13]
        ano_col = raw[:, 11]

        Xf = np.zeros((len(raw), len(FEATURE_NAMES)), dtype=np.float64)
        idx = {n: i for i, n in enumerate(FEATURE_NAMES)}

        Xf[:, idx['F1_cumprim']]       = raw[:, 0]
        Xf[:, idx['F10_juizado_ANTI']] = raw[:, 1]
        Xf[:, idx['F2_precat_tc']]     = (raw[:, 2] > 0).astype(float)
        Xf[:, idx['F7_envTrib_tc']]    = (raw[:, 3] > 0).astype(float)
        Xf[:, idx['F11_precat_text']]  = (raw[:, 4] > 0).astype(float)
        Xf[:, idx['F12_rpv_text']]     = (raw[:, 5] > 0).astype(float)
        Xf[:, idx['F13_reqPag_text']]  = (raw[:, 6] > 0).astype(float)
        Xf[:, idx['F14_oficio_text']]  = (raw[:, 7] > 0).astype(float)
        Xf[:, idx['F15_logMovs']]      = np.log1p(total_movs_col) / math.log(500)
        Xf[:, idx['F16_logTipos']]     = np.log1p(raw[:, 9]) / math.log(50)
        Xf[:, idx['F17_logN1count']]   = np.log1p(raw[:, 10]) / math.log(20)
        Xf[:, idx['F18_anoZ']]         = np.where(ano_col > 0, (ano_col - ano_mean) / ano_std, 0.0)
        Xf[:, idx['F19_cancelado_ANTI']] = (raw[:, 12] > 0).astype(float)
        Xf[:, idx['F20_exp_juriscope']]  = (raw[:, 13] > 0).astype(float)
        Xf[:, idx['F21_diasUltMovZ']]  = (raw[:, 14] - dias_mean) / dias_std
        Xf[:, idx['F23_logPartes']]    = np.log1p(raw[:, 15]) / math.log(50)
        Xf[:, idx['F1xF11']]           = f1_col * (f11_col > 0).astype(float)
        Xf[:, idx['F1xF15']]           = f1_col * np.log1p(total_movs_col) / math.log(500)
        Xf[:, idx['F1xF20']]           = f1_col * (f20_col > 0).astype(float)

        y = np.array(y_vals, dtype=np.float64)
        n_pos = int(y.sum())
        n_neg = int(len(y) - n_pos)
        self.stdout.write(
            f'  Universo: {len(y):,} processos  |  leads={n_pos:,} ({100*n_pos/len(y):.2f}%)'
            f'  |  não-leads={n_neg:,}'
        )
        return Xf, y, norm

    def _train(self, X, y, opts):
        rng = np.random.default_rng(opts['seed'])
        pos_idx = np.where(y == 1)[0]
        neg_idx = np.where(y == 0)[0]
        rng.shuffle(pos_idx); rng.shuffle(neg_idx)

        n_tp = int(len(pos_idx) * 0.2)
        n_tn = int(len(neg_idx) * 0.2)
        test_idx = np.concatenate([pos_idx[:n_tp], neg_idx[:n_tn]])
        train_idx = np.concatenate([pos_idx[n_tp:], neg_idx[n_tn:]])

        Xtr, Xte = X[train_idx], X[test_idx]
        ytr, yte = y[train_idx], y[test_idx]

        self.stdout.write(
            f'  Train: {len(Xtr):,} ({int(ytr.sum()):,} leads)'
            f'  |  Test: {len(Xte):,} ({int(yte.sum()):,} leads)'
        )

        epochs = opts['epochs']
        lr = opts['lr']
        l2 = opts['l2']
        n = len(ytr)
        W = np.zeros(X.shape[1])
        b = 0.0
        t0 = time.time()

        for ep in range(1, epochs + 1):
            z = Xtr @ W + b
            pred = _sigmoid(z)
            err = pred - ytr
            grad_w = (Xtr.T @ err) / n + l2 * W
            grad_b = err.mean()
            W -= lr * grad_w
            b -= lr * grad_b

            if ep % 50 == 0 or ep == 1 or ep == epochs:
                loss = float(-np.mean(ytr * np.log(pred + 1e-15)
                                      + (1 - ytr) * np.log(1 - pred + 1e-15)))
                elapsed = time.time() - t0
                eta = (elapsed / ep) * (epochs - ep)
                self.stdout.write(
                    f'  época {ep:3d}/{epochs}  loss={loss:.4f}'
                    f'  elapsed={elapsed:.0f}s  ETA={eta:.0f}s'
                )

        return {'W': W, 'b': b, 'Xte': Xte, 'yte': yte,
                'train_size': len(Xtr), 'test_size': len(Xte)}

    def _report(self, res, norm):
        W, b, Xte, yte = res['W'], res['b'], res['Xte'], res['yte']
        scores = _sigmoid(Xte @ W + b)

        auc = _auc(yte, scores)
        ks = [500, 1000, 2500, 5000, 10000]
        precs = {k: _prec_at_k(yte, scores, k) for k in ks}

        SEP = '─' * 62
        self.stdout.write(f'\n{SEP}')
        self.stdout.write('  MÉTRICAS')
        self.stdout.write(f'  {"Métrica":<22} {"v5":>8}  {"v6":>8}  {"Δ":>8}  Status')
        self.stdout.write(f'  {SEP}')

        regressao = False

        def _ln(nome, v5v, v6v, tol):
            nonlocal regressao
            d = v6v - v5v
            ok = d >= -tol
            if not ok:
                regressao = True
            s = '✅' if ok else '❌'
            self.stdout.write(f'  {nome:<22} {v5v:>8.4f}  {v6v:>8.4f}  {d:>+8.4f}  {s}')

        _ln('AUC', V5['metricas']['auc'], auc, 0.01)
        for k in ks:
            v5k = V5['metricas'].get(f'precision_at_{k}')
            if v5k is not None:
                _ln(f'precision@{k}', v5k, precs[k], 0.02)

        self.stdout.write(f'\n{SEP}')
        self.stdout.write('  PESOS v5 → v6  (◄ = delta > 0.1)')
        self.stdout.write(f'  {"Feature":<22} {"v5":>8}  {"v6":>8}  {"Δ":>8}')
        self.stdout.write(f'  {SEP}')
        self.stdout.write(
            f'  {"_intercept_":<22} {V5["intercept"]:>8.3f}  {b:>8.3f}  {b-V5["intercept"]:>+8.3f}'
            + (' ◄' if abs(b - V5['intercept']) > 0.1 else '')
        )
        for i, name in enumerate(FEATURE_NAMES):
            v5w = V5['weights'].get(name, 0.0)
            v6w = float(W[i])
            d = v6w - v5w
            mark = ' ◄' if abs(d) > 0.1 else ''
            self.stdout.write(f'  {name:<22} {v5w:>8.3f}  {v6w:>8.3f}  {d:>+8.3f}{mark}')

        self.stdout.write(f'\n  Normalização')
        self.stdout.write(f'  v5  ano={V5["ano_mean"]}±{V5["ano_std"]}'
                          f'  dias={V5["dias_mean"]}±{V5["dias_std"]}')
        self.stdout.write(f'  v6  ano={norm["ano_mean"]}±{norm["ano_std"]}'
                          f'  dias={norm["dias_mean"]}±{norm["dias_std"]}')
        self.stdout.write(SEP)

        if regressao:
            self.stdout.write(self.style.ERROR('  ❌  REGRESSÃO detectada em uma ou mais métricas.'))
        else:
            self.stdout.write(self.style.SUCCESS('  ✅  Sem regressão — v6 é candidato a deploy.'))

        return regressao

    def _deploy(self, res, norm, opts, tribunais):
        W, b = res['W'], res['b']

        # 1) Persiste em ClassificadorVersao
        from tribunals.models import ClassificadorVersao
        pesos = {name: round(float(W[i]), 6) for i, name in enumerate(FEATURE_NAMES)}
        pesos['_intercept_'] = round(float(b), 6)
        metricas = {
            'train_size': res['train_size'], 'test_size': res['test_size'],
            'tribunais': tribunais,
            'epochs': opts['epochs'], 'lr': opts['lr'], 'l2': opts['l2'],
            'ano_mean': norm['ano_mean'], 'ano_std': norm['ano_std'],
            'dias_mean': norm['dias_mean'], 'dias_std': norm['dias_std'],
        }
        cv, created = ClassificadorVersao.objects.update_or_create(
            versao='v6', defaults={'pesos': pesos, 'metricas': metricas, 'ativa': True},
        )
        self.stdout.write(f'\nClassificadorVersao v6 {"criada" if created else "atualizada"} (id={cv.pk})')

        # 2) Atualiza classificador.py diretamente
        clf_path = Path(__file__).parents[3] / 'classificador.py'
        src = clf_path.read_text()

        def _fmt_weights(weights_dict, indent=4):
            lines = []
            sp = ' ' * indent
            for k, v in weights_dict.items():
                lines.append(f"{sp}'{k}':{' ' * max(1, 26-len(k))}{v:.6f},")
            return '\n'.join(lines)

        w_dict = {name: round(float(W[i]), 6) for i, name in enumerate(FEATURE_NAMES)}
        w_dict['_intercept_'] = round(float(b), 6)

        # Substitui bloco VERSAO
        src = re.sub(r"VERSAO = '[^']*'", "VERSAO = 'v6'", src)

        # Substitui bloco WEIGHTS
        new_weights_body = _fmt_weights(
            {'_intercept_': w_dict.pop('_intercept_'), **w_dict}
        )
        src = re.sub(
            r'(WEIGHTS = \{)[^}]*(})',
            lambda m: m.group(1) + '\n' + new_weights_body + '\n}',
            src, flags=re.DOTALL,
        )

        # Substitui constantes de normalização
        src = re.sub(r'ANO_MEAN\s*=\s*[\d.]+', f'ANO_MEAN = {norm["ano_mean"]}', src)
        src = re.sub(r'ANO_STD\s*=\s*[\d.]+', f'ANO_STD = {norm["ano_std"]}', src)
        src = re.sub(r'DIAS_ULT_MOV_MEAN\s*=\s*[\d.]+', f'DIAS_ULT_MOV_MEAN = {norm["dias_mean"]}', src)
        src = re.sub(r'DIAS_ULT_MOV_STD\s*=\s*[\d.]+', f'DIAS_ULT_MOV_STD = {norm["dias_std"]}', src)

        clf_path.write_text(src)
        self.stdout.write(self.style.SUCCESS(
            f'\n✅  classificador.py atualizado para v6.\n'
            f'    Reinicie os workers para ativar o novo modelo.\n'
            f'    Depois rode: python manage.py reclassificar_trf1_bulk --apply\n'
        ))
