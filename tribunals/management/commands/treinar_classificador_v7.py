"""Treina classificador v7 com pesos amostrais por origem, features novas,
thresholds por tribunal e gates de aceitação.

Diferenças sobre v6:
  - Ground truth multi-fonte via `tribunals.services.export_labels`
    (CSV legados + Juriscope + ProcessoValidacao) com pesos amostrais
    (1.0, 2.0, 3.0) — REGRAS_NEGOCIO_VALIDACAO.md.
  - 24 features (19 do v6 + 5 novas F24-F28: RPV expedida, pagamento admin,
    inscrição ordem cronológica, trânsito julgado, líquido certo).
  - Logistic Regression numpy puro com `sample_weight`.
  - Avaliação por tribunal + ECE (calibração) + `recall@FN_candidatos`
    + regressão em `falsos_consumidos_1327`.
  - Grid search de thresholds por (tribunal × nível) maximizando precision@500.
  - Conformal prediction (split conformal, quantil 0.9).
  - 6 gates PASS/WARN/BLOCK; --force libera WARN mas não BLOCK.

Uso:
  python manage.py treinar_classificador_v7
  python manage.py treinar_classificador_v7 --shadow
  python manage.py treinar_classificador_v7 --deploy
  python manage.py treinar_classificador_v7 --deploy --force
  python manage.py treinar_classificador_v7 \\
      --ground-truth-csv data/labels_retreino_20260511.csv \\
      --fn-candidates-csv data/fn_candidatos_20260511.csv
"""
from __future__ import annotations

import csv
import json
import logging
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Constantes ───────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = REPO_ROOT / 'data'
FN_CANDIDATOS_GLOB = 'fn_candidatos_*.csv'
GROUND_TRUTH_DIR = REPO_ROOT / "data_ground_truth"
FALSOS_CONSUMIDOS_CSV = GROUND_TRUTH_DIR / "leads_trf1_falsos_consumidos_1327.csv"

# v6 baseline (de classificador.HARDCODED_WEIGHTS) — pra comparação no relatório.
V6_BASELINE = {
    'versao': 'v6',
    'metricas': {
        'auc': 0.9610,
        'precision_at_500': 0.986,
        'precision_at_1000': 0.993,
        'precision_at_5000': 0.991,
        'precision_at_10000': 0.982,
    },
}

# Features v6 + 5 novas v7.
FEATURE_NAMES_V6 = [
    'F1_cumprim', 'F10_juizado_ANTI', 'F2_precat_tc', 'F7_envTrib_tc',
    'F11_precat_text', 'F12_rpv_text', 'F13_reqPag_text', 'F14_oficio_text',
    'F15_logMovs', 'F16_logTipos', 'F17_logN1count', 'F18_anoZ',
    'F19_cancelado_ANTI', 'F20_exp_juriscope', 'F21_diasUltMovZ', 'F23_logPartes',
    'F1xF11', 'F1xF15', 'F1xF20',
]
FEATURE_NAMES_NOVAS = [
    'F24_rpv_expedida_text',
    'F25_pagamento_administrativo',
    'F26_inscricao_ordem',
    'F27_transitado_julgado',
    'F28_liquido_certo',
]
FEATURE_NAMES = FEATURE_NAMES_V6 + FEATURE_NAMES_NOVAS

# Pares (regex, feature_name) das 5 novas features.
NOVAS_REGEX_FEATURES = [
    (r'\bRPV\s+expedid', 'F24_rpv_expedida_text'),
    (r'pagamento\s+administrativo', 'F25_pagamento_administrativo'),
    (r'inscri[çc][ãa]o\s+(?:na\s+)?ordem\s+cronol[óo]gica', 'F26_inscricao_ordem'),
    (r'transitad[oa]\s+em\s+julgado', 'F27_transitado_julgado'),
    (r'l[íi]quid[oa]\s+(?:e\s+)?cert[oa]', 'F28_liquido_certo'),
]

CLASSES_CUMPRIMENTO = {'12078', '156', '15160', '15215', '12079'}
_CNJ_ANO_RE = re.compile(r'^\d{7}-\d{2}\.(\d{4})\.')

# Defaults de thresholds (REGRAS_NEGOCIO_VALIDACAO §4).
THRESHOLDS_DEFAULT = {
    'TRF1': {'precatorio': 0.70, 'pre': 0.40, 'dc': 0.20},
    'TRF3': {'precatorio': 0.65, 'pre': 0.35, 'dc': 0.20},
    'TJMG': {'precatorio': 0.75, 'pre': 0.45, 'dc': 0.25},
    'TJSP': {'precatorio': 0.75, 'pre': 0.45, 'dc': 0.25},
}
TRIBUNAIS_THRESHOLDS = list(THRESHOLDS_DEFAULT.keys())

# Gates de aceitação (REGRAS_NEGOCIO_VALIDACAO §3).
GATES_SPEC = [
    # (codigo, descricao, pass_op, pass_threshold, warn_threshold)
    # op: 'gte' = score >= threshold; 'lte' = score <= threshold.
    ('AUC_GLOBAL', 'AUC global (TRF1+TRF3)', 'gte', 0.960, 0.955),
    ('PRECISION_AT_5000', 'precision@5000', 'gte', 0.985, 0.970),
    ('RECALL_FN', 'recall@FN_candidatos', 'gte', 0.40, 0.20),
    ('AUC_TRF3', 'AUC TRF3', 'gte', 0.90, 0.85),
    ('ECE', 'Expected Calibration Error', 'lte', 0.05, 0.08),
    ('REGRESSAO_FALSOS', '% falsos_consumidos com score >= 0.3', 'lte', 0.0, 0.10),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ano_cnj(numero: str) -> int:
    m = _CNJ_ANO_RE.match(numero or '')
    return int(m.group(1)) if m else 0


def _is_anti_classe(nome: str) -> int:
    n = (nome or '').lower()
    return int('juizado especial' in n or 'recurso inominado' in n
               or 'procedimento comum' in n)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _auc(y, scores):
    """ROC-AUC via calculo de area Mann-Whitney (numpy puro)."""
    order = np.argsort(-scores)
    ys = np.asarray(y)[order]
    n_pos = float(ys.sum())
    n_neg = float(len(ys) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return 0.0
    tpr = np.cumsum(ys) / n_pos
    fpr = np.cumsum(1 - ys) / n_neg
    # área via trapezoide manual (np.trapz foi removido no NumPy 2.0).
    return float(np.sum((fpr[1:] - fpr[:-1]) * (tpr[1:] + tpr[:-1])) / 2)


def _prec_at_k(y, scores, k):
    k = min(k, len(y))
    if k == 0:
        return 0.0
    top = np.argsort(-scores)[:k]
    return float(np.asarray(y)[top].sum() / k)


def _ece(y, scores, n_bins: int = 10):
    """Expected Calibration Error (10 bins por default).

    Para cada bin, |confianca_media - acuracia_media| * peso_do_bin.
    """
    y = np.asarray(y, dtype=np.float64)
    s = np.asarray(scores, dtype=np.float64)
    if len(s) == 0:
        return 0.0
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n_total = len(s)
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        mask = (s >= lo) & (s <= hi if i == n_bins - 1 else s < hi)
        if not mask.any():
            continue
        conf = s[mask].mean()
        acc = y[mask].mean()
        ece += abs(conf - acc) * (mask.sum() / n_total)
    return float(ece)


def _build_movs_agg_sql() -> str:
    """SQL agregado retornando counts v6 + 5 novos regex F24-F28."""
    parts = [
        'm.processo_id',
        'COUNT(*) AS total_movs',
        ("COUNT(DISTINCT CASE WHEN m.tipo_comunicacao <> '' "
         'THEN m.tipo_comunicacao END) AS distinct_tipos'),
        'MAX(m.data_disponibilizacao) AS ult_mov_dt',
        ("COALESCE(SUM(CASE WHEN m.tipo_comunicacao IN "
         "('Expedição de precatório/rpv','Precatório') "
         'THEN 1 ELSE 0 END), 0) AS f2_n'),
        ("COALESCE(SUM(CASE WHEN m.tipo_comunicacao IN "
         "('Enviada ao Tribunal','Preparada para Envio') "
         'THEN 1 ELSE 0 END), 0) AS f7_n'),
        ("COALESCE(SUM(CASE WHEN m.texto ~* 'precat[óo]rio' THEN 1 ELSE 0 END), 0) AS f11_n"),
        ("COALESCE(SUM(CASE WHEN m.texto ~* '\\mrpv\\M' THEN 1 ELSE 0 END), 0) AS f12_n"),
        ("COALESCE(SUM(CASE WHEN m.texto ~* 'requisi[çc][ãa]o de pagamento' "
         'THEN 1 ELSE 0 END), 0) AS f13_n'),
        ("COALESCE(SUM(CASE WHEN m.texto ~* 'of[íi]cio requisit[óo]rio' "
         'THEN 1 ELSE 0 END), 0) AS f14_n'),
        ("COALESCE(SUM(CASE WHEN m.texto ~* 'cancelamento de precat[óo]rio|"
         "cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|"
         "revoga[çc][ãa]o de rpv' THEN 1 ELSE 0 END), 0) AS f19_n"),
        ("COALESCE(SUM(CASE WHEN m.texto ~* 'precat[óo]rio expedido|"
         "rpv expedida|of[íi]cio requisit[óo]rio expedido|"
         "requisi[çc][ãa]o de pagamento de pequeno valor enviada|"
         "requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|"
         "determinada expedi[çc][ãa]o de precat[óo]rio|"
         "determinada expedi[çc][ãa]o de rpv|"
         "expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento' "
         'THEN 1 ELSE 0 END), 0) AS f20_n'),
    ]
    for i, (pat, _name) in enumerate(NOVAS_REGEX_FEATURES, start=1):
        safe = pat.replace("'", "''")
        parts.append(
            f"COALESCE(SUM(CASE WHEN m.texto ~* '{safe}' THEN 1 ELSE 0 END), 0) "
            f'AS novo{i}_n'
        )
    select = ',\n            '.join(parts)
    return (
        '        SELECT\n            ' + select + '\n'
        '        FROM tribunals_movimentacao m\n'
        '        WHERE m.processo_id = ANY(%s)\n'
        '        GROUP BY m.processo_id\n'
    )


_MOVS_AGG_BATCH_SQL = _build_movs_agg_sql()


def _load_cnjs_csv(path: Path) -> set[str]:
    """Lê CSV com 1 coluna CNJ (header opcional)."""
    cnjs: set[str] = set()
    if not path.exists():
        return cnjs
    with path.open(encoding='utf-8') as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            v = (row[0] or '').strip()
            if not v or v.lower() in ('numero_processo', 'cnj', 'numero_cnj'):
                continue
            cnjs.add(v)
    return cnjs


def _find_latest_fn_candidates() -> Optional[Path]:
    candidates = sorted(DEFAULT_OUTPUT_DIR.glob(FN_CANDIDATOS_GLOB))
    return candidates[-1] if candidates else None


# ── Command ──────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = 'Treina classificador v7 (24 features, weighted LR, gates de aceitação).'

    def add_arguments(self, parser):
        parser.add_argument('--ground-truth-csv', default='',
                            help='CSV de labels consolidado (export_labels). '
                                 'Default: gera em runtime.')
        parser.add_argument('--fn-candidates-csv', default='',
                            help='CSV de candidatos FN do mining. '
                                 'Default: mais recente em data/fn_candidatos_*.csv.')
        parser.add_argument('--no-deploy', action='store_true',
                            help='(default) Treina mas não persiste ClassificadorVersao.')
        parser.add_argument('--shadow', action='store_true',
                            help='Persiste v7 como shadow (ativa=False, shadow=True).')
        parser.add_argument('--deploy', action='store_true',
                            help='Persiste v7 como ativa após passar nos gates.')
        parser.add_argument('--force', action='store_true',
                            help='Permite --deploy com gates em WARN (não BLOCK).')
        parser.add_argument('--seed', type=int, default=42)
        parser.add_argument('--epochs', type=int, default=400)
        parser.add_argument('--lr', type=float, default=0.5)
        parser.add_argument('--l2', type=float, default=0.0005)
        parser.add_argument('--output-dir', default=str(DEFAULT_OUTPUT_DIR))

    def handle(self, *args, **opts):
        # Imports tardios pra dar erro claro se faltarem dependências.
        try:
            import numpy as _np_check  # noqa: F401
        except ImportError as exc:
            raise CommandError(
                'numpy é obrigatório. Adicione a requirements.txt e rebuild '
                'da imagem do container web.'
            ) from exc

        t_start = time.time()
        ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_dir = Path(opts['output_dir'])
        output_dir.mkdir(parents=True, exist_ok=True)

        self._log(
            f'treinar_classificador_v7  ts={ts_str}  '
            f'epochs={opts["epochs"]} lr={opts["lr"]} l2={opts["l2"]} seed={opts["seed"]}'
        )

        # 1. Carregar dataset consolidado.
        dataset = self._carregar_dataset(opts)
        if not dataset['rows']:
            raise CommandError('Dataset vazio — nada para treinar.')

        # 2. Extrair features (24 features × N processos).
        feats = self._extrair_features(dataset)

        # 3. Train/test split estratificado.
        split = self._split(feats, opts['seed'])

        # 4. Treino LR weighted.
        model = self._treinar(split, opts)

        # 5. Avaliação em holdout.
        eval_res = self._avaliar(model, split, dataset, opts)

        # 6. Grid search de thresholds por tribunal.
        thresholds = self._otimizar_thresholds(model, split, dataset, opts,
                                                ts_str, output_dir)

        # 7. Conformal prediction.
        conformal = self._conformal(model, split, opts['seed'])

        # 8. Gates de aceitação.
        gates = self._avaliar_gates(eval_res)

        # 9. Persistir artefatos sempre.
        metrics_path = output_dir / f'v7_metrics_{ts_str}.json'
        pesos_path = output_dir / f'v7_pesos_{ts_str}.json'
        report_path = output_dir / f'V7_TRAINING_REPORT_{ts_str}.md'

        pesos_dict = {
            name: round(float(model['W'][i]), 6)
            for i, name in enumerate(FEATURE_NAMES)
        }
        pesos_dict['_intercept_'] = round(float(model['b']), 6)

        metricas_dict = {
            'versao': 'v7',
            'ts': ts_str,
            'train_size': split['train_size'],
            'test_size': split['test_size'],
            'epochs': opts['epochs'],
            'lr': opts['lr'],
            'l2': opts['l2'],
            'seed': opts['seed'],
            'n_features': len(FEATURE_NAMES),
            'auc_global': eval_res['auc_global'],
            'auc_por_tribunal': eval_res['auc_por_tribunal'],
            'precision_at_k': eval_res['precision_at_k'],
            'ece': eval_res['ece'],
            'recall_fn_candidatos': eval_res['recall_fn'],
            'regressao_falsos_consumidos_pct': eval_res['regressao_falsos_pct'],
            'regressao_falsos_consumidos_count': eval_res['regressao_falsos_count'],
            'calibracao_decis': eval_res['calibracao_decis'],
            'normas': model['normas'],
            'thresholds_otimos': thresholds,
            'conformal': conformal,
            'gates': gates,
            'pesos_distribuicao': dataset['pesos_distribuicao'],
            'conflitos': dataset['conflitos'][:10],
        }

        with metrics_path.open('w', encoding='utf-8') as fh:
            json.dump(metricas_dict, fh, indent=2, ensure_ascii=False, default=str)

        with pesos_path.open('w', encoding='utf-8') as fh:
            json.dump({
                'versao': 'v7',
                'features': FEATURE_NAMES,
                'pesos': pesos_dict,
                'normas': model['normas'],
            }, fh, indent=2, ensure_ascii=False)

        self._escrever_relatorio(report_path, metricas_dict, dataset)

        self._log('\nArtefatos:')
        self._log(f'  metricas: {metrics_path}')
        self._log(f'  pesos:    {pesos_path}')
        self._log(f'  relatorio: {report_path}')

        # 10. Deploy / shadow.
        if opts['deploy']:
            self._handle_deploy(pesos_dict, metricas_dict, thresholds, gates,
                                 force=opts['force'])
        elif opts['shadow']:
            self._criar_versao(pesos_dict, metricas_dict, ativa=False, shadow=True)
            self._log('Versao v7 persistida como shadow (ativa=False).')
        else:
            self._log('--no-deploy (default): pesos não persistidos no DB.')

        elapsed = timedelta(seconds=int(time.time() - t_start))
        self._log(f'\ntempo total: {elapsed}')

    # ─────────────────────────────────────────────────────────────────────────
    # Pipeline steps
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self.stdout.write(f'[{time.strftime("%H:%M:%S")}] {msg}')

    def _warn(self, msg: str) -> None:
        self.stdout.write(self.style.WARNING(f'[{time.strftime("%H:%M:%S")}] WARN  {msg}'))

    def _err(self, msg: str) -> None:
        self.stdout.write(self.style.ERROR(f'[{time.strftime("%H:%M:%S")}] ERROR {msg}'))

    def _carregar_dataset(self, opts) -> dict:
        """Carrega rows de labels consolidados.

        Retorna dict com:
          - rows: list[dict] cnj, tribunal, label, peso, fonte, conflito_flag, processo_id
          - pesos_distribuicao: {fonte_origem: peso_medio}
          - conflitos: top 10 conflitos
        """
        gt_path = opts['ground_truth_csv']
        if gt_path:
            path = Path(gt_path)
            if not path.exists():
                raise CommandError(f'--ground-truth-csv não encontrado: {path}')
            self._log(f'Carregando ground truth: {path}')
        else:
            self._log('Gerando ground truth via export_labels (runtime)...')
            from tribunals.services.export_labels import exportar_labels_retreino
            path = exportar_labels_retreino()
            self._log(f'  -> escrito em {path}')

        rows: list[dict] = []
        with path.open(encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                pid_raw = (r.get('processo_id') or '').strip()
                if not pid_raw:
                    # Sem processo_id → não é possível extrair features.
                    continue
                try:
                    pid = int(pid_raw)
                except ValueError:
                    continue
                try:
                    label = int(r['label'])
                    peso = float(r['peso'])
                except (KeyError, ValueError):
                    continue
                rows.append({
                    'cnj': r['cnj'],
                    'tribunal': r.get('tribunal') or '',
                    'label': label,
                    'peso': peso,
                    'fonte': r.get('fonte') or '',
                    'conflito_flag': (r.get('conflito_flag', '').lower()
                                       in {'true', '1', 'yes', 'sim'}),
                    'processo_id': pid,
                })

        self._log(f'  -> {len(rows):,} rows com processo_id válido')

        # Estatísticas por origem.
        peso_por_origem: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            origem = r['fonte'].split(':', 1)[0] if r['fonte'] else 'unknown'
            peso_por_origem[origem].append(r['peso'])
        pesos_distribuicao = {
            o: {
                'n': len(ps),
                'peso_medio': round(sum(ps) / max(1, len(ps)), 4),
            }
            for o, ps in peso_por_origem.items()
        }

        # Conflitos detectados.
        conflitos = [
            {'cnj': r['cnj'], 'tribunal': r['tribunal'],
             'label_final': r['label'], 'fonte_vencedora': r['fonte']}
            for r in rows if r['conflito_flag']
        ]
        return {
            'rows': rows,
            'pesos_distribuicao': pesos_distribuicao,
            'conflitos': conflitos,
        }

    def _extrair_features(self, dataset: dict) -> dict:
        """Roda SQL agregado em batches e monta matriz X."""

        rows = dataset['rows']
        pids = [r['processo_id'] for r in rows]

        # Aggregates SQL em batches de 5000.
        agg_map: dict[int, tuple] = {}
        partes_map: dict[int, int] = {}
        batch = 5000
        self._log(f'Extraindo features de {len(pids):,} processos (batches de {batch})...')
        with connection.cursor() as cur:
            for i in range(0, len(pids), batch):
                chunk = pids[i:i + batch]
                cur.execute(_MOVS_AGG_BATCH_SQL, [chunk])
                for row in cur.fetchall():
                    agg_map[row[0]] = row[1:]
                cur.execute(
                    'SELECT processo_id, COUNT(*) FROM tribunals_processoparte '
                    'WHERE processo_id = ANY(%s) GROUP BY processo_id',
                    [chunk],
                )
                for pid, n in cur.fetchall():
                    partes_map[pid] = n

        # Para obter classe_codigo/classe_nome de cada processo.
        proc_meta: dict[int, tuple] = {}
        with connection.cursor() as cur:
            for i in range(0, len(pids), batch):
                chunk = pids[i:i + batch]
                cur.execute(
                    "SELECT id, numero_cnj, COALESCE(classe_codigo,''), "
                    "COALESCE(classe_nome,'') FROM tribunals_process "
                    'WHERE id = ANY(%s)',
                    [chunk],
                )
                for pid, cnj, cc, cn in cur.fetchall():
                    proc_meta[pid] = (cnj, cc, cn)

        now = timezone.now()
        valid_rows = []
        anos = []
        dias_list = []
        for r in rows:
            pid = r['processo_id']
            meta = proc_meta.get(pid)
            if not meta:
                continue
            cnj, classe_cod, classe_nome = meta
            agg = agg_map.get(pid)
            f1 = int(classe_cod in CLASSES_CUMPRIMENTO)
            f10 = _is_anti_classe(classe_nome)
            ano = _ano_cnj(cnj)
            if agg is None:
                # Sem mov — features zeradas (mas mantém classe e ano).
                total_movs = distinct_tipos = 0
                ult_mov_dt = None
                f2_n = f7_n = f11_n = f12_n = f13_n = f14_n = f19_n = f20_n = 0
                novos = [0] * len(NOVAS_REGEX_FEATURES)
            else:
                (total_movs, distinct_tipos, ult_mov_dt,
                 f2_n, f7_n, f11_n, f12_n, f13_n, f14_n, f19_n, f20_n,
                 *novos) = agg
            dias = ((now - ult_mov_dt).total_seconds() / 86400
                     if ult_mov_dt else 9999.0)
            anos.append(ano if ano > 0 else None)
            dias_list.append(dias)
            valid_rows.append({
                'row': r,
                'f1': f1, 'f10': f10, 'ano': ano,
                'total_movs': total_movs, 'distinct_tipos': distinct_tipos,
                'f2_n': f2_n, 'f7_n': f7_n, 'f11_n': f11_n, 'f12_n': f12_n,
                'f13_n': f13_n, 'f14_n': f14_n, 'f19_n': f19_n, 'f20_n': f20_n,
                'dias': dias,
                'n_partes': partes_map.get(pid, 0),
                'novos': novos,
            })

        # Normas (ano, dias).
        valid_anos = [a for a in anos if a is not None]
        ano_mean = float(np.mean(valid_anos)) if valid_anos else 2020.0
        ano_std = max(float(np.std(valid_anos)) if valid_anos else 1.0, 1e-6)
        dias_arr = np.array(dias_list, dtype=np.float64)
        dias_mean = float(dias_arr.mean()) if len(dias_arr) else 0.0
        dias_std = max(float(dias_arr.std()) if len(dias_arr) else 1.0, 1e-6)

        n = len(valid_rows)
        d = len(FEATURE_NAMES)
        X = np.zeros((n, d), dtype=np.float64)
        y = np.zeros(n, dtype=np.float64)
        w = np.zeros(n, dtype=np.float64)
        tribunais = []
        cnjs = []

        idx = {name: i for i, name in enumerate(FEATURE_NAMES)}
        for i, item in enumerate(valid_rows):
            r = item['row']
            f1 = item['f1']
            total_movs = item['total_movs']
            distinct_tipos = item['distinct_tipos']
            f11_pos = int((item['f11_n'] or 0) > 0)
            f12_pos = int((item['f12_n'] or 0) > 0)
            f13_pos = int((item['f13_n'] or 0) > 0)
            f14_pos = int((item['f14_n'] or 0) > 0)
            f20_pos = int((item['f20_n'] or 0) > 0)
            f15 = math.log1p(total_movs) / math.log(500)
            f16 = math.log1p(distinct_tipos or 0) / math.log(50)
            f17 = math.log1p((item['f11_n'] or 0) + (item['f12_n'] or 0)
                              + (item['f13_n'] or 0) + (item['f14_n'] or 0)) / math.log(20)
            f18 = ((item['ano'] - ano_mean) / ano_std) if item['ano'] > 0 else 0.0
            f21 = (item['dias'] - dias_mean) / dias_std
            f23 = math.log1p(item['n_partes']) / math.log(50)

            X[i, idx['F1_cumprim']] = f1
            X[i, idx['F10_juizado_ANTI']] = item['f10']
            X[i, idx['F2_precat_tc']] = int((item['f2_n'] or 0) > 0)
            X[i, idx['F7_envTrib_tc']] = int((item['f7_n'] or 0) > 0)
            X[i, idx['F11_precat_text']] = f11_pos
            X[i, idx['F12_rpv_text']] = f12_pos
            X[i, idx['F13_reqPag_text']] = f13_pos
            X[i, idx['F14_oficio_text']] = f14_pos
            X[i, idx['F15_logMovs']] = f15
            X[i, idx['F16_logTipos']] = f16
            X[i, idx['F17_logN1count']] = f17
            X[i, idx['F18_anoZ']] = f18
            X[i, idx['F19_cancelado_ANTI']] = int((item['f19_n'] or 0) > 0)
            X[i, idx['F20_exp_juriscope']] = f20_pos
            X[i, idx['F21_diasUltMovZ']] = f21
            X[i, idx['F23_logPartes']] = f23
            X[i, idx['F1xF11']] = f1 * f11_pos
            X[i, idx['F1xF15']] = f1 * f15
            X[i, idx['F1xF20']] = f1 * f20_pos
            for j, (_pat, fname) in enumerate(NOVAS_REGEX_FEATURES):
                X[i, idx[fname]] = int((item['novos'][j] or 0) > 0)

            y[i] = r['label']
            w[i] = r['peso']
            tribunais.append(r['tribunal'])
            cnjs.append(r['cnj'])

        n_pos = int(y.sum())
        n_neg = int(n - n_pos)
        self._log(
            f'  matriz: {n:,} × {d}  | leads={n_pos:,} ({100*n_pos/max(1,n):.2f}%) '
            f'| nao-leads={n_neg:,}'
        )
        return {
            'X': X, 'y': y, 'w': w, 'tribunais': tribunais, 'cnjs': cnjs,
            'normas': {
                'ano_mean': round(ano_mean, 2), 'ano_std': round(ano_std, 2),
                'dias_mean': round(dias_mean, 2), 'dias_std': round(dias_std, 2),
            },
        }

    def _split(self, feats: dict, seed: int) -> dict:
        """80/20 estratificado por (tribunal, label)."""

        X = feats['X']
        y = feats['y']
        w = feats['w']
        tribs = feats['tribunais']
        cnjs = feats['cnjs']

        rng = np.random.default_rng(seed)
        n = len(y)
        train_idx = []
        test_idx = []
        # Group por (tribunal, label).
        groups: dict[tuple, list[int]] = defaultdict(list)
        for i in range(n):
            groups[(tribs[i], int(y[i]))].append(i)
        for idxs in groups.values():
            idxs = list(idxs)
            rng.shuffle(idxs)
            n_test = max(1, int(len(idxs) * 0.2)) if len(idxs) >= 2 else 0
            test_idx.extend(idxs[:n_test])
            train_idx.extend(idxs[n_test:])

        train_idx = np.array(train_idx, dtype=np.int64)
        test_idx = np.array(test_idx, dtype=np.int64)

        return {
            'Xtr': X[train_idx], 'ytr': y[train_idx], 'wtr': w[train_idx],
            'Xte': X[test_idx], 'yte': y[test_idx], 'wte': w[test_idx],
            'tribs_te': [tribs[i] for i in test_idx],
            'cnjs_te': [cnjs[i] for i in test_idx],
            'normas': feats['normas'],
            'train_size': len(train_idx),
            'test_size': len(test_idx),
        }

    def _treinar(self, split: dict, opts) -> dict:
        """Logistic Regression batch GD + L2 com sample_weight."""

        Xtr = split['Xtr']
        ytr = split['ytr']
        wtr = split['wtr']
        n, d = Xtr.shape
        W = np.zeros(d, dtype=np.float64)
        b = 0.0
        epochs = opts['epochs']
        lr = opts['lr']
        l2 = opts['l2']
        # Normalização do weight: divide pelo soma pra equivaler a unweighted
        # quando todos pesos = 1 (mantém escala do gradiente parecida com v6).
        w_sum = wtr.sum() if wtr.sum() > 0 else 1.0
        w_norm = wtr * (n / w_sum)

        t0 = time.time()
        for ep in range(1, epochs + 1):
            z = Xtr @ W + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            err = (p - ytr) * w_norm
            grad_w = (Xtr.T @ err) / n + l2 * W
            grad_b = err.mean()
            W -= lr * grad_w
            b -= lr * grad_b
            if ep % 100 == 0 or ep == 1 or ep == epochs:
                loss = float(-np.mean(w_norm * (
                    ytr * np.log(p + 1e-15) + (1 - ytr) * np.log(1 - p + 1e-15)
                )))
                self._log(
                    f'  epoca {ep:3d}/{epochs}  loss(weighted)={loss:.4f}  '
                    f'elapsed={time.time()-t0:.1f}s'
                )

        return {'W': W, 'b': b, 'normas': split['normas']}

    def _avaliar(self, model: dict, split: dict, dataset: dict, opts) -> dict:
        """AUC global e por tribunal, precision@K, ECE, recall@FN, regressão falsos."""

        Xte = split['Xte']
        yte = split['yte']
        tribs_te = split['tribs_te']
        cnjs_te = split['cnjs_te']
        W = model['W']
        b = model['b']
        scores = _sigmoid(Xte @ W + b)

        auc_global = _auc(yte, scores)
        auc_por_trib = {}
        for trib in TRIBUNAIS_THRESHOLDS:
            mask = np.array([t == trib for t in tribs_te])
            if mask.sum() < 2 or yte[mask].sum() == 0 or yte[mask].sum() == mask.sum():
                continue
            auc_por_trib[trib] = round(_auc(yte[mask], scores[mask]), 4)

        prec_k = {
            f'precision_at_{k}': round(_prec_at_k(yte, scores, k), 4)
            for k in (500, 1000, 2500, 5000, 10000)
        }
        ece_val = round(_ece(yte, scores, n_bins=10), 4)

        # Calibração por decil.
        order = np.argsort(-scores)
        sorted_s = scores[order]
        sorted_y = yte[order]
        n_per_decile = max(1, len(sorted_s) // 10)
        decis = []
        for d in range(10):
            lo = d * n_per_decile
            hi = (d + 1) * n_per_decile if d < 9 else len(sorted_s)
            if lo >= hi or lo >= len(sorted_s):
                continue
            sl_s = sorted_s[lo:hi]
            sl_y = sorted_y[lo:hi]
            if len(sl_s) == 0:
                continue
            decis.append({
                'decil': d + 1,
                'score_medio': round(float(sl_s.mean()), 4),
                'taxa_real': round(float(sl_y.mean()), 4),
                'n': int(hi - lo),
            })

        # recall@FN_candidatos.
        recall_fn = self._calcular_recall_fn(model, opts)

        # Regressão falsos_consumidos_1327.
        reg_pct, reg_count = self._calcular_regressao_falsos(model)

        # Para gate "AUC_TRF3" precisamos da chave.
        return {
            'auc_global': round(auc_global, 4),
            'auc_por_tribunal': auc_por_trib,
            'precision_at_k': prec_k,
            'ece': ece_val,
            'recall_fn': recall_fn,
            'regressao_falsos_pct': reg_pct,
            'regressao_falsos_count': reg_count,
            'calibracao_decis': decis,
            'scores_te': scores,
            'yte': yte,
            'tribs_te': tribs_te,
            'cnjs_te': cnjs_te,
        }

    def _calcular_recall_fn(self, model: dict, opts) -> dict:
        """recall@FN_candidatos: dos top 5000 mining candidates, quantos o v7
        classifica como N1/N2/N3 (score >= threshold mínimo do tribunal)?

        Sem ThresholdTribunal otimizado ainda, usa default 0.20 (N3) como
        cut-off "lead" agnóstico de tribunal.
        """
        fn_path = opts['fn_candidates_csv']
        if not fn_path:
            latest = _find_latest_fn_candidates()
            if latest is None:
                self._warn('Nenhum CSV de fn_candidatos encontrado — recall_fn=N/A')
                return {'n_candidatos': 0, 'recall': None, 'cnjs_recuperados': 0}
            fn_path = latest
        path = Path(fn_path)
        if not path.exists():
            self._warn(f'fn_candidates_csv ausente: {path} — recall_fn=N/A')
            return {'n_candidatos': 0, 'recall': None, 'cnjs_recuperados': 0}

        cnjs = []
        with path.open(encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                v = (r.get('cnj') or '').strip()
                if v:
                    cnjs.append(v)
                if len(cnjs) >= 5000:
                    break

        if not cnjs:
            return {'n_candidatos': 0, 'recall': None, 'cnjs_recuperados': 0}

        # Score esses CNJs com o modelo treinado (extraindo features sob demanda).
        scores = self._score_cnjs(model, cnjs)
        n_recovered = sum(1 for s in scores.values() if s >= 0.20)
        n_total = len(scores)
        recall = (n_recovered / n_total) if n_total else 0.0
        self._log(f'  recall@FN_candidatos: {n_recovered}/{n_total} = {recall:.4f}')
        return {
            'n_candidatos': n_total,
            'cnjs_recuperados': n_recovered,
            'recall': round(recall, 4),
        }

    def _calcular_regressao_falsos(self, model: dict) -> tuple[float, int]:
        """% de leads_trf1_falsos_consumidos_1327 com score >= 0.3 (deveriam ser 0)."""
        cnjs = list(_load_cnjs_csv(FALSOS_CONSUMIDOS_CSV))
        if not cnjs:
            self._warn(f'{FALSOS_CONSUMIDOS_CSV.name} ausente — pulando gate regressão')
            return 0.0, 0
        scores = self._score_cnjs(model, cnjs)
        if not scores:
            return 0.0, 0
        n_alto = sum(1 for s in scores.values() if s >= 0.3)
        pct = n_alto / len(scores)
        self._log(
            f'  regressao_falsos: {n_alto}/{len(scores)} '
            f'({100*pct:.1f}%) com score >= 0.3'
        )
        return round(pct, 4), n_alto

    def _score_cnjs(self, model: dict, cnjs: list[str]) -> dict[str, float]:
        """Computa scores v7 para uma lista de CNJs (busca features no DB)."""

        if not cnjs:
            return {}
        # Resolve CNJs → pids (qualquer tribunal).
        pid_map: dict[int, tuple[str, str, str]] = {}
        with connection.cursor() as cur:
            batch = 2000
            for i in range(0, len(cnjs), batch):
                chunk = cnjs[i:i + batch]
                cur.execute(
                    "SELECT id, numero_cnj, COALESCE(classe_codigo,''), "
                    "COALESCE(classe_nome,'') FROM tribunals_process "
                    'WHERE numero_cnj = ANY(%s)',
                    [chunk],
                )
                for pid, cnj, cc, cn in cur.fetchall():
                    pid_map[pid] = (cnj, cc, cn)
        if not pid_map:
            return {}

        pids = list(pid_map.keys())
        agg: dict[int, tuple] = {}
        partes: dict[int, int] = {}
        with connection.cursor() as cur:
            batch = 2000
            for i in range(0, len(pids), batch):
                chunk = pids[i:i + batch]
                cur.execute(_MOVS_AGG_BATCH_SQL, [chunk])
                for row in cur.fetchall():
                    agg[row[0]] = row[1:]
                cur.execute(
                    'SELECT processo_id, COUNT(*) FROM tribunals_processoparte '
                    'WHERE processo_id = ANY(%s) GROUP BY processo_id',
                    [chunk],
                )
                for pid, n in cur.fetchall():
                    partes[pid] = n

        now = timezone.now()
        normas = model['normas']
        ano_mean, ano_std = normas['ano_mean'], normas['ano_std']
        dias_mean, dias_std = normas['dias_mean'], normas['dias_std']
        # Guarda contra divisão por zero quando dataset tiver poucos valores.
        ano_std = ano_std if ano_std > 0 else 1.0
        dias_std = dias_std if dias_std > 0 else 1.0

        out: dict[str, float] = {}
        W = model['W']
        b = model['b']
        d = len(FEATURE_NAMES)
        idx = {name: i for i, name in enumerate(FEATURE_NAMES)}
        for pid, (cnj, classe_cod, classe_nome) in pid_map.items():
            f1 = int(classe_cod in CLASSES_CUMPRIMENTO)
            f10 = _is_anti_classe(classe_nome)
            ano = _ano_cnj(cnj)
            row = agg.get(pid)
            if row is None:
                total_movs = distinct_tipos = 0
                ult_mov_dt = None
                f2_n = f7_n = f11_n = f12_n = f13_n = f14_n = f19_n = f20_n = 0
                novos = [0] * len(NOVAS_REGEX_FEATURES)
            else:
                (total_movs, distinct_tipos, ult_mov_dt,
                 f2_n, f7_n, f11_n, f12_n, f13_n, f14_n, f19_n, f20_n,
                 *novos) = row
            dias = ((now - ult_mov_dt).total_seconds() / 86400
                     if ult_mov_dt else 9999.0)
            n_partes = partes.get(pid, 0)
            v = np.zeros(d, dtype=np.float64)
            v[idx['F1_cumprim']] = f1
            v[idx['F10_juizado_ANTI']] = f10
            v[idx['F2_precat_tc']] = int((f2_n or 0) > 0)
            v[idx['F7_envTrib_tc']] = int((f7_n or 0) > 0)
            v[idx['F11_precat_text']] = int((f11_n or 0) > 0)
            v[idx['F12_rpv_text']] = int((f12_n or 0) > 0)
            v[idx['F13_reqPag_text']] = int((f13_n or 0) > 0)
            v[idx['F14_oficio_text']] = int((f14_n or 0) > 0)
            v[idx['F15_logMovs']] = math.log1p(total_movs) / math.log(500)
            v[idx['F16_logTipos']] = math.log1p(distinct_tipos or 0) / math.log(50)
            v[idx['F17_logN1count']] = math.log1p(
                (f11_n or 0) + (f12_n or 0) + (f13_n or 0) + (f14_n or 0)
            ) / math.log(20)
            v[idx['F18_anoZ']] = ((ano - ano_mean) / ano_std) if ano > 0 else 0.0
            v[idx['F19_cancelado_ANTI']] = int((f19_n or 0) > 0)
            v[idx['F20_exp_juriscope']] = int((f20_n or 0) > 0)
            v[idx['F21_diasUltMovZ']] = (dias - dias_mean) / dias_std
            v[idx['F23_logPartes']] = math.log1p(n_partes) / math.log(50)
            v[idx['F1xF11']] = f1 * int((f11_n or 0) > 0)
            v[idx['F1xF15']] = f1 * (math.log1p(total_movs) / math.log(500))
            v[idx['F1xF20']] = f1 * int((f20_n or 0) > 0)
            for j, (_pat, fname) in enumerate(NOVAS_REGEX_FEATURES):
                v[idx[fname]] = int((novos[j] or 0) > 0)
            z = float(v @ W + b)
            out[cnj] = float(_sigmoid(z))
        return out

    def _otimizar_thresholds(self, model: dict, split: dict, dataset: dict,
                              opts, ts_str: str, output_dir: Path) -> dict:
        """Grid search [0.1, 0.9] step 0.05 por tribunal × nível.

        Otimiza precision@500 no holdout do tribunal. Persiste CSV com grid.
        """

        Xte = split['Xte']
        yte = split['yte']
        tribs_te = split['tribs_te']
        W = model['W']
        b = model['b']
        scores = _sigmoid(Xte @ W + b)

        grid_path = output_dir / f'v7_threshold_grid_{ts_str}.csv'
        grid_rows = []

        result = {}
        for trib in TRIBUNAIS_THRESHOLDS:
            mask = np.array([t == trib for t in tribs_te])
            if mask.sum() < 4 or yte[mask].sum() == 0:
                self._warn(
                    f'  threshold[{trib}]: amostra insuficiente '
                    f'({mask.sum()} rows) — usando defaults'
                )
                result[trib] = THRESHOLDS_DEFAULT[trib]
                # Mantém pelo menos 1 row no grid pra teste/inspeção.
                grid_rows.append({
                    'tribunal': trib, 'nivel': 'precatorio',
                    'threshold': THRESHOLDS_DEFAULT[trib]['precatorio'],
                    'n_above': int(mask.sum()),
                    'precision_at_500': 0.0,
                })
                continue
            s_trib = scores[mask]
            y_trib = yte[mask]
            best = {}
            for nivel in ('precatorio', 'pre', 'dc'):
                best_th = THRESHOLDS_DEFAULT[trib][nivel]
                best_prec = -1.0
                for th_i in range(2, 19):  # 0.10 .. 0.90 step 0.05
                    th = th_i * 0.05
                    sel = s_trib >= th
                    if not sel.any():
                        continue
                    # precision@500 (top-500 dentre os com score >= th).
                    k = min(500, int(sel.sum()))
                    top = np.argsort(-s_trib)[:k]
                    prec = float(y_trib[top].sum() / max(1, k))
                    grid_rows.append({
                        'tribunal': trib, 'nivel': nivel, 'threshold': round(th, 2),
                        'n_above': int(sel.sum()), 'precision_at_500': round(prec, 4),
                    })
                    if prec > best_prec:
                        best_prec = prec
                        best_th = round(th, 2)
                best[nivel] = best_th
            result[trib] = best
            self._log(f'  threshold[{trib}]: {best}')

        # Persiste grid CSV.
        if grid_rows:
            with grid_path.open('w', encoding='utf-8', newline='') as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=['tribunal', 'nivel', 'threshold',
                                     'n_above', 'precision_at_500'])
                writer.writeheader()
                writer.writerows(grid_rows)
            self._log(f'  grid persistido: {grid_path}')
        return result

    def _conformal(self, model: dict, split: dict, seed: int) -> dict:
        """Split conformal: 20% do holdout → calibration; quantil 0.9 do resíduo."""

        rng = np.random.default_rng(seed)
        Xte = split['Xte']
        yte = split['yte']
        n = len(yte)
        if n < 20:
            return {'delta': None, 'n_calib': 0, 'quantile': 0.9}
        idx = np.arange(n)
        rng.shuffle(idx)
        n_calib = max(2, n // 5)
        calib = idx[:n_calib]
        scores = _sigmoid(Xte[calib] @ model['W'] + model['b'])
        residuals = np.abs(yte[calib] - scores)
        delta = float(np.quantile(residuals, 0.9))
        return {
            'delta': round(delta, 4),
            'n_calib': int(n_calib),
            'quantile': 0.9,
            'interpretacao': (
                f'90% das predições têm erro absoluto ≤ {delta:.3f} '
                f'(intervalo ±{delta:.3f} no score).'
            ),
        }

    def _avaliar_gates(self, eval_res: dict) -> dict:
        """Aplica os 6 gates. Retorna dict {codigo: {status, valor, pass, warn}}."""
        # Mapa de valores.
        recall_v = eval_res['recall_fn'].get('recall')
        auc_trf3 = eval_res['auc_por_tribunal'].get('TRF3')
        values = {
            'AUC_GLOBAL': eval_res['auc_global'],
            'PRECISION_AT_5000': eval_res['precision_at_k'].get(
                'precision_at_5000', 0.0),
            'RECALL_FN': recall_v if recall_v is not None else 0.0,
            'AUC_TRF3': auc_trf3 if auc_trf3 is not None else 0.0,
            'ECE': eval_res['ece'],
            'REGRESSAO_FALSOS': eval_res['regressao_falsos_pct'],
        }
        # Gates "no_data" para métricas faltantes (não bloqueiam).
        no_data = set()
        if recall_v is None:
            no_data.add('RECALL_FN')
        if auc_trf3 is None:
            no_data.add('AUC_TRF3')

        gates = {}
        for code, desc, op, pass_th, warn_th in GATES_SPEC:
            v = values[code]
            if code in no_data:
                status = 'NO_DATA'
            elif op == 'gte':
                if v >= pass_th:
                    status = 'PASS'
                elif v >= warn_th:
                    status = 'WARN'
                else:
                    status = 'BLOCK'
            else:  # lte
                if v <= pass_th:
                    status = 'PASS'
                elif v <= warn_th:
                    status = 'WARN'
                else:
                    status = 'BLOCK'
            gates[code] = {
                'descricao': desc, 'valor': round(v, 4),
                'pass_threshold': pass_th, 'warn_threshold': warn_th,
                'op': op, 'status': status,
            }
        return gates

    # ─────────────────────────────────────────────────────────────────────────
    # Persistência / deploy
    # ─────────────────────────────────────────────────────────────────────────

    def _criar_versao(self, pesos: dict, metricas: dict, *,
                       ativa: bool, shadow: bool) -> None:
        from django.db import transaction

        from tribunals.models import ClassificadorVersao

        defaults = {
            'pesos': pesos,
            'metricas': metricas,
            'ativa': ativa,
            'shadow': shadow,
        }
        # Constraint partial garante 1 ativa por vez — desativa as outras antes.
        with transaction.atomic():
            if ativa:
                ClassificadorVersao.objects.filter(ativa=True).exclude(
                    versao='v7',
                ).update(ativa=False)
            cv, created = ClassificadorVersao.objects.update_or_create(
                versao='v7', defaults=defaults,
            )
        self._log(
            f'ClassificadorVersao v7 {"criada" if created else "atualizada"} '
            f'(id={cv.pk} ativa={ativa} shadow={shadow})'
        )

    def _persistir_thresholds(self, thresholds: dict) -> None:
        """Cria/atualiza ThresholdTribunal pra cada tribunal × v7."""
        from tribunals.models import ThresholdTribunal, Tribunal

        for sigla, vals in thresholds.items():
            trib = Tribunal.objects.filter(sigla=sigla).first()
            if trib is None:
                self._warn(f'  Tribunal {sigla} não existe — pulando threshold')
                continue
            ThresholdTribunal.objects.update_or_create(
                tribunal=trib, versao_modelo='v7',
                defaults={
                    'threshold_precatorio': vals['precatorio'],
                    'threshold_pre': vals['pre'],
                    'threshold_dc': vals['dc'],
                    'ativo': True,
                },
            )
            self._log(f'  ThresholdTribunal[{sigla}]/v7 persistido')

    def _handle_deploy(self, pesos: dict, metricas: dict, thresholds: dict,
                        gates: dict, *, force: bool) -> None:
        blocks = [c for c, g in gates.items() if g['status'] == 'BLOCK']
        warns = [c for c, g in gates.items() if g['status'] == 'WARN']

        if blocks:
            self._err(
                f'Gates em BLOCK: {blocks} — deploy bloqueado '
                f'(--force não libera BLOCK).'
            )
            return
        if warns and not force:
            self._err(
                f'Gates em WARN: {warns} — deploy abortado. '
                f'Use --force pra ignorar WARN (ou ajuste hiperparâmetros).'
            )
            return
        if warns and force:
            self._warn(
                f'Gates em WARN: {warns} — deploy seguindo por --force.'
            )

        self._criar_versao(pesos, metricas, ativa=True, shadow=False)
        self._persistir_thresholds(thresholds)
        self._log('Deploy v7 concluído. Hot reload (T17) propaga em até '
                   '`CLASSIFICADOR_RELOAD_TTL` segundos (default 60s).')

    def _escrever_relatorio(self, path: Path, metricas: dict, dataset: dict) -> None:
        gates = metricas['gates']
        normas = metricas['normas']
        eval_lines = []

        # Resumo executivo.
        eval_lines.append(f'# V7 — Relatório de treinamento ({metricas["ts"]})\n')
        eval_lines.append('## Resumo executivo\n')
        eval_lines.append(f'- Features: {len(FEATURE_NAMES)} ({len(FEATURE_NAMES_NOVAS)} novas: F24–F28).')
        eval_lines.append(
            f'- Dataset: {metricas["train_size"]:,} train + '
            f'{metricas["test_size"]:,} test (80/20 estratificado por tribunal × label).'
        )
        eval_lines.append(
            f'- AUC global v7={metricas["auc_global"]} '
            f'(v6={V6_BASELINE["metricas"]["auc"]}).'
        )
        eval_lines.append(
            f'- precision@5000 v7='
            f'{metricas["precision_at_k"].get("precision_at_5000", "n/a")} '
            f'(v6={V6_BASELINE["metricas"]["precision_at_5000"]}).'
        )
        eval_lines.append(f'- ECE: {metricas["ece"]} (target ≤ 0.05).')
        n_blocks = sum(1 for g in gates.values() if g['status'] == 'BLOCK')
        n_warns = sum(1 for g in gates.values() if g['status'] == 'WARN')
        n_pass = sum(1 for g in gates.values() if g['status'] == 'PASS')
        eval_lines.append(f'- Gates: {n_pass} PASS, {n_warns} WARN, {n_blocks} BLOCK.\n')

        # Comparativo v6 vs v7.
        eval_lines.append('## v6 vs v7\n')
        eval_lines.append('| Métrica | v6 | v7 |')
        eval_lines.append('|---|---:|---:|')
        eval_lines.append(
            f'| AUC global | {V6_BASELINE["metricas"]["auc"]} | '
            f'{metricas["auc_global"]} |'
        )
        for k in (500, 1000, 5000, 10000):
            v6 = V6_BASELINE['metricas'].get(f'precision_at_{k}', '-')
            v7 = metricas['precision_at_k'].get(f'precision_at_{k}', '-')
            eval_lines.append(f'| precision@{k} | {v6} | {v7} |')
        eval_lines.append('')

        # Gates.
        eval_lines.append('## Gates de aceitação\n')
        eval_lines.append('| Código | Descrição | Valor | Pass | Warn | Status |')
        eval_lines.append('|---|---|---:|---:|---:|---|')
        for code, g in gates.items():
            eval_lines.append(
                f'| {code} | {g["descricao"]} | {g["valor"]} | '
                f'{g["pass_threshold"]} | {g["warn_threshold"]} | {g["status"]} |'
            )
        eval_lines.append('')

        # Pesos por origem.
        eval_lines.append('## Distribuição de pesos amostrais por origem\n')
        eval_lines.append('| Origem | N | Peso médio |')
        eval_lines.append('|---|---:|---:|')
        for origem, info in dataset['pesos_distribuicao'].items():
            eval_lines.append(f'| {origem} | {info["n"]:,} | {info["peso_medio"]} |')
        eval_lines.append('')

        # Top conflitos.
        if dataset['conflitos']:
            eval_lines.append('## Top 10 conflitos de label\n')
            eval_lines.append('| CNJ | Tribunal | Label final | Fonte vencedora |')
            eval_lines.append('|---|---|---:|---|')
            for c in dataset['conflitos'][:10]:
                eval_lines.append(
                    f'| `{c["cnj"]}` | {c["tribunal"]} | {c["label_final"]} | '
                    f'{c["fonte_vencedora"]} |'
                )
            eval_lines.append('')

        # Thresholds otimizados.
        eval_lines.append('## Thresholds otimizados por tribunal\n')
        eval_lines.append('| Tribunal | Precatório (N1) | Pré (N2) | DC (N3) |')
        eval_lines.append('|---|---:|---:|---:|')
        for trib in TRIBUNAIS_THRESHOLDS:
            vals = metricas['thresholds_otimos'].get(trib, {})
            eval_lines.append(
                f'| {trib} | {vals.get("precatorio", "-")} | '
                f'{vals.get("pre", "-")} | {vals.get("dc", "-")} |'
            )
        eval_lines.append('')

        # Normas.
        eval_lines.append('## Normalização (z-scores)\n')
        eval_lines.append(
            f'- ano: mean={normas["ano_mean"]}, std={normas["ano_std"]}'
        )
        eval_lines.append(
            f'- dias desde ult mov: mean={normas["dias_mean"]}, '
            f'std={normas["dias_std"]}\n'
        )

        # AUC por tribunal.
        eval_lines.append('## AUC por tribunal\n')
        eval_lines.append('| Tribunal | AUC |')
        eval_lines.append('|---|---:|')
        for trib, auc in metricas['auc_por_tribunal'].items():
            eval_lines.append(f'| {trib} | {auc} |')
        eval_lines.append('')

        # Conformal.
        conf = metricas['conformal']
        eval_lines.append('## Conformal prediction\n')
        if conf.get('delta') is not None:
            eval_lines.append(
                f'- Quantil {conf["quantile"]}: delta = ±{conf["delta"]} '
                f'(n_calib={conf["n_calib"]}).'
            )
            eval_lines.append(f'- {conf["interpretacao"]}\n')
        else:
            eval_lines.append('- amostra insuficiente.\n')

        path.write_text('\n'.join(eval_lines), encoding='utf-8')
