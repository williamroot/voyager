"""Minera candidatos a falso negativo (FN) do classificador v6.

Hipótese: parte dos Process classificados como NAO_LEAD pelo v6 são, na
verdade, leads. Este comando ranqueia esses candidatos combinando 6
estratégias, cada uma contribuindo com um delta para `suspeita_score`
∈ [0, 1]. O CSV de saída alimenta (a) UI de validação humana e (b)
métrica `recall@FN_candidatos` no retreino v7.

Estratégias e contribuição:

  E1  score_band [0.10, 0.20]                       → +0.20
  E2  novos regex sinalizando expedição/RPV         → +0.30 * min(n/3, 1)
  E3  F1 órfão  (Cumprimento com ≤ 5 movs)          → +0.15
  E4  similaridade com 1.327 recuperados (mini-LR)  → +0.20 se sim > 0.6
  E5  KMeans cluster density > 0.7                  → +0.10
  E6  cross-tribunal cosine sim > 0.7 ao centroid   → +0.05

Uso:

  python manage.py minerar_fn --tribunal TRF1 --limit 5000 --dry-run
  python manage.py minerar_fn --output data/fn_candidatos_20260511.csv
  python manage.py minerar_fn --upsert-lote
"""
from __future__ import annotations

import csv
import json
import math
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from tribunals.classificador import (
    ANO_MEAN,
    ANO_STD,
    CLASSES_CUMPRIMENTO,
    DIAS_ULT_MOV_MEAN,
    DIAS_ULT_MOV_STD,
    VERSAO,
    WEIGHTS,
    _ano_cnj,
    _is_anti_classe,
)
from tribunals.models import Process, Tribunal

# ── Constantes ───────────────────────────────────────────────────────────────

# Padrões "novos" — sinais de expedição/pagamento que o v6 não tem como
# feature dedicada. Contagem destes incrementa E2.
NOVOS_REGEX = [
    r'\bRPV\s+expedid',
    r'pagamento\s+administrativo',
    r'of[íi]cio\s+requisit[óo]rio\s+expedido',
    r'precat[óo]rio\s+(?:expedido|encaminhado|inscrito)',
    r'inscri[çc][ãa]o\s+(?:na\s+)?ordem\s+cronol[óo]gica',
    r'transitad[oa]\s+em\s+julgado',
    r'l[íi]quid[oa]\s+(?:e\s+)?cert[oa]',
]

E1_BAND_MIN = 0.10
E1_BAND_MAX = 0.20
E1_DELTA = 0.20
E2_MAX_DELTA = 0.30
E3_MAX_MOVS = 5
E3_DELTA = 0.15
E4_THRESHOLD = 0.60
E4_DELTA = 0.20
E5_DENSITY_THRESHOLD = 0.70
E5_DELTA = 0.10
E5_K = 20
E6_SIM_THRESHOLD = 0.70
E6_DELTA = 0.05

DEFAULT_LIMITE_UNIVERSO = 1_000_000
DEFAULT_TOP_N = 5_000

REPO_ROOT = Path(__file__).resolve().parents[3]
GROUND_TRUTH_DIR = REPO_ROOT / "data_ground_truth"
RECUPERADOS_CSV = GROUND_TRUTH_DIR / "leads_trf1_recuperados_1327.csv"
LEADS_TRF1_CSV = GROUND_TRUTH_DIR / "leads_trf1.csv"
MINING_REPORT_DIR = REPO_ROOT / '.ia'

FEATURE_NAMES = [
    'F1_cumprim', 'F10_juizado_ANTI', 'F2_precat_tc', 'F7_envTrib_tc',
    'F11_precat_text', 'F12_rpv_text', 'F13_reqPag_text', 'F14_oficio_text',
    'F15_logMovs', 'F16_logTipos', 'F17_logN1count', 'F18_anoZ',
    'F19_cancelado_ANTI', 'F20_exp_juriscope', 'F21_diasUltMovZ', 'F23_logPartes',
    'F1xF11', 'F1xF15', 'F1xF20',
]


# ── SQL agregado ─────────────────────────────────────────────────────────────

def _build_movs_agg_sql() -> str:
    """SQL agregado: contagens v6 + 7 novos regex (E2)."""
    base = """
        SELECT
            m.processo_id,
            COUNT(*) AS total_movs,
            COUNT(DISTINCT CASE WHEN m.tipo_comunicacao <> '' THEN m.tipo_comunicacao END) AS distinct_tipos,
            MAX(m.data_disponibilizacao) AS ult_mov_dt,
            COALESCE(SUM(CASE WHEN m.tipo_comunicacao IN
                ('Expedição de precatório/rpv','Precatório') THEN 1 ELSE 0 END), 0) AS f2_n,
            COALESCE(SUM(CASE WHEN m.tipo_comunicacao IN
                ('Enviada ao Tribunal','Preparada para Envio') THEN 1 ELSE 0 END), 0) AS f7_n,
            COALESCE(SUM(CASE WHEN m.texto ~* 'precat[óo]rio' THEN 1 ELSE 0 END), 0) AS f11_n,
            COALESCE(SUM(CASE WHEN m.texto ~* '\\mrpv\\M' THEN 1 ELSE 0 END), 0) AS f12_n,
            COALESCE(SUM(CASE WHEN m.texto ~* 'requisi[çc][ãa]o de pagamento' THEN 1 ELSE 0 END), 0) AS f13_n,
            COALESCE(SUM(CASE WHEN m.texto ~* 'of[íi]cio requisit[óo]rio' THEN 1 ELSE 0 END), 0) AS f14_n,
            COALESCE(SUM(CASE WHEN m.texto ~* 'cancelamento de precat[óo]rio|cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|revoga[çc][ãa]o de rpv' THEN 1 ELSE 0 END), 0) AS f19_n,
            COALESCE(SUM(CASE WHEN m.texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento' THEN 1 ELSE 0 END), 0) AS f20_n,
    """
    # 7 novos regex como NOVO1..NOVO7
    extras = []
    for i, pat in enumerate(NOVOS_REGEX, start=1):
        # Escapa aspas simples para SQL.
        safe = pat.replace("'", "''")
        extras.append(
            f"            COALESCE(SUM(CASE WHEN m.texto ~* '{safe}' THEN 1 ELSE 0 END), 0) AS novo{i}_n"
        )
    extras_sql = ',\n'.join(extras)
    return base + extras_sql + """
        FROM tribunals_movimentacao m
        WHERE m.processo_id = ANY(%s)
        GROUP BY m.processo_id
    """


_MOVS_AGG_BATCH_SQL = _build_movs_agg_sql()


# ── Helpers de feature extraction (vetorizado) ──────────────────────────────

def _features_from_row(row: tuple, cnj: str, classe_cod: str, classe_nome: str,
                       n_partes: int, now: datetime) -> dict:
    """Recria as 19 features v6 a partir do row agregado.

    `row` segue a ordem do SELECT em _MOVS_AGG_BATCH_SQL (sem o processo_id).
    """
    (total_movs, distinct_tipos, ult_mov_dt,
     f2_n, f7_n, f11_n, f12_n, f13_n, f14_n, f19_n, f20_n,
     *_novos) = row

    ano = _ano_cnj(cnj)
    f1 = int((classe_cod or '') in CLASSES_CUMPRIMENTO)
    f10 = _is_anti_classe(classe_nome or '')

    if not total_movs:
        f18 = (ano - ANO_MEAN) / ANO_STD if ano > 0 else 0.0
        f21 = (9999 - DIAS_ULT_MOV_MEAN) / DIAS_ULT_MOV_STD
        return {
            'F1_cumprim': f1, 'F10_juizado_ANTI': f10,
            'F2_precat_tc': 0, 'F7_envTrib_tc': 0,
            'F11_precat_text': 0, 'F12_rpv_text': 0,
            'F13_reqPag_text': 0, 'F14_oficio_text': 0,
            'F15_logMovs': 0.0, 'F16_logTipos': 0.0, 'F17_logN1count': 0.0,
            'F18_anoZ': f18, 'F19_cancelado_ANTI': 0, 'F20_exp_juriscope': 0,
            'F21_diasUltMovZ': f21, 'F23_logPartes': 0.0,
            'F1xF11': 0, 'F1xF15': 0.0, 'F1xF20': 0,
        }

    dias_ult = ((now - ult_mov_dt).total_seconds() / 86400) if ult_mov_dt else 9999.0
    f15 = math.log1p(total_movs) / math.log(500)
    f16 = math.log1p(distinct_tipos or 0) / math.log(50)
    f17 = math.log1p((f11_n or 0) + (f12_n or 0) + (f13_n or 0) + (f14_n or 0)) / math.log(20)
    f18 = (ano - ANO_MEAN) / ANO_STD if ano > 0 else 0.0
    f21 = (dias_ult - DIAS_ULT_MOV_MEAN) / DIAS_ULT_MOV_STD
    f23 = math.log1p(n_partes) / math.log(50)

    f2 = int((f2_n or 0) > 0)
    f7 = int((f7_n or 0) > 0)
    f11 = int((f11_n or 0) > 0)
    f12 = int((f12_n or 0) > 0)
    f13 = int((f13_n or 0) > 0)
    f14 = int((f14_n or 0) > 0)
    f19 = int((f19_n or 0) > 0)
    f20 = int((f20_n or 0) > 0)

    return {
        'F1_cumprim': f1, 'F10_juizado_ANTI': f10,
        'F2_precat_tc': f2, 'F7_envTrib_tc': f7,
        'F11_precat_text': f11, 'F12_rpv_text': f12,
        'F13_reqPag_text': f13, 'F14_oficio_text': f14,
        'F15_logMovs': f15, 'F16_logTipos': f16, 'F17_logN1count': f17,
        'F18_anoZ': f18, 'F19_cancelado_ANTI': f19, 'F20_exp_juriscope': f20,
        'F21_diasUltMovZ': f21, 'F23_logPartes': f23,
        'F1xF11': f1 * f11, 'F1xF15': f1 * f15, 'F1xF20': f1 * f20,
    }


def _features_to_vec(feats: dict) -> np.ndarray:
    """Converte dict de features pra vetor np ordenado por FEATURE_NAMES."""
    return np.array([feats.get(n, 0.0) for n in FEATURE_NAMES], dtype=np.float64)


def _sigmoid(z: np.ndarray | float) -> np.ndarray | float:
    if isinstance(z, np.ndarray):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
    z = max(min(z, 30.0), -30.0)
    return 1.0 / (1.0 + math.exp(-z))


def _top_negative_contributions(feats: dict, top_k: int = 5) -> list[dict]:
    """Top features que mais puxaram score *para baixo* (peso * valor < 0).

    Útil pra entender onde o v6 "errou" — features negativas dominantes
    sugerem que se essas mudarem o processo viraria lead.
    """
    contribs = []
    for name in FEATURE_NAMES:
        w = WEIGHTS.get(name, 0.0)
        v = feats.get(name, 0.0)
        c = w * v
        if c < 0:
            contribs.append({'feature': name, 'weight': round(w, 4),
                             'value': round(float(v), 4), 'contrib': round(c, 4)})
    contribs.sort(key=lambda d: d['contrib'])
    return contribs[:top_k]


# ── Carga de ground truth ────────────────────────────────────────────────────

def _load_cnjs_csv(path: Path) -> set[str]:
    """Lê CSV simples com 1 coluna 'numero_processo' (header opcional)."""
    cnjs: set[str] = set()
    if not path.exists():
        return cnjs
    with open(path, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            v = row[0].strip()
            if not v or v.lower() in ('numero_processo', 'cnj', 'numero_cnj'):
                continue
            cnjs.add(v)
    return cnjs


# ── Mini-LR (E4) ─────────────────────────────────────────────────────────────

def _train_mini_lr(X_pos: np.ndarray, X_neg: np.ndarray,
                   epochs: int = 200, lr: float = 0.3, l2: float = 0.001,
                   seed: int = 42) -> tuple[np.ndarray, float]:
    """Treina LR binária (numpy puro). Retorna (W, b)."""
    rng = np.random.default_rng(seed)
    X = np.vstack([X_pos, X_neg])
    y = np.concatenate([np.ones(len(X_pos)), np.zeros(len(X_neg))])
    perm = rng.permutation(len(y))
    X, y = X[perm], y[perm]
    n, d = X.shape
    W = np.zeros(d)
    b = 0.0
    for _ in range(epochs):
        z = X @ W + b
        pred = _sigmoid(z)
        err = pred - y
        grad_w = (X.T @ err) / n + l2 * W
        grad_b = err.mean()
        W -= lr * grad_w
        b -= lr * grad_b
    return W, b


def _mini_lr_score(X: np.ndarray, W: np.ndarray, b: float) -> np.ndarray:
    return _sigmoid(X @ W + b)


# ── KMeans (E5) — numpy puro ─────────────────────────────────────────────────

def _kmeans(X: np.ndarray, k: int, n_iter: int = 30, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """KMeans clássico, numpy puro. Retorna (centroids, labels).

    Inicialização: k pontos aleatórios distintos (sem k-means++ por
    simplicidade — suficiente pra E5 que só usa cluster ID).
    """
    rng = np.random.default_rng(seed)
    n = len(X)
    if n == 0:
        return np.zeros((k, X.shape[1] if X.ndim == 2 else 1)), np.zeros(0, dtype=int)
    if n <= k:
        # Não há pontos suficientes pra k clusters; usa cada ponto como
        # centroid e preenche com cópias.
        centroids = np.tile(X[0], (k, 1))
        centroids[:n] = X
        labels = np.arange(n) % k
        return centroids, labels.astype(int)

    idx0 = rng.choice(n, size=k, replace=False)
    centroids = X[idx0].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(n_iter):
        # Distância L2² ao quadrado: ||x||² + ||c||² - 2·x·c (broadcast)
        d2 = ((X[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        new_labels = d2.argmin(axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for ki in range(k):
            mask = labels == ki
            if mask.any():
                centroids[ki] = X[mask].mean(axis=0)
    return centroids, labels


def _assign_clusters(X: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    if len(X) == 0:
        return np.zeros(0, dtype=int)
    d2 = ((X[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
    return d2.argmin(axis=1)


# ── Cosine sim (E6) ──────────────────────────────────────────────────────────

def _cosine_sim_to_centroid(X: np.ndarray, c: np.ndarray) -> np.ndarray:
    if len(X) == 0:
        return np.zeros(0)
    cn = np.linalg.norm(c) + 1e-12
    xn = np.linalg.norm(X, axis=1) + 1e-12
    return (X @ c) / (xn * cn)


# ── Carga em chunks ──────────────────────────────────────────────────────────

def _iter_universe(tribunais: list[str], min_data, limite: int, chunk: int = 50_000):
    """Itera Process NAO_LEAD em chunks. Retorna list[(pid, cnj, sigla, classe_cod, classe_nome,
    score_modelo, total_movs)] por chunk."""
    qs = (Process.objects
          .filter(classificacao=Process.CLASSIF_NAO_LEAD)
          .filter(tribunal__sigla__in=tribunais))
    if min_data:
        qs = qs.filter(inserido_em__gte=min_data)
    qs = qs.order_by('id').values_list(
        'id', 'numero_cnj', 'tribunal__sigla',
        'classe_codigo', 'classe_nome',
        'classificacao_score', 'total_movimentacoes',
    )
    if limite:
        qs = qs[:limite]

    buf: list = []
    for tup in qs.iterator(chunk_size=chunk):
        buf.append(tup)
        if len(buf) >= chunk:
            yield buf
            buf = []
    if buf:
        yield buf


def _fetch_agg_for_pids(pids: list[int]) -> dict[int, tuple]:
    """Roda _MOVS_AGG_BATCH_SQL para um chunk de PIDs."""
    out: dict[int, tuple] = {}
    if not pids:
        return out
    with connection.cursor() as cur:
        cur.execute(_MOVS_AGG_BATCH_SQL, [pids])
        for row in cur.fetchall():
            pid = row[0]
            out[pid] = row[1:]
    return out


def _fetch_partes_for_pids(pids: list[int]) -> dict[int, int]:
    out: dict[int, int] = {}
    if not pids:
        return out
    with connection.cursor() as cur:
        cur.execute(
            "SELECT processo_id, COUNT(*) FROM tribunals_processoparte "
            "WHERE processo_id = ANY(%s) GROUP BY processo_id",
            [pids],
        )
        for pid, n in cur.fetchall():
            out[pid] = n
    return out


def _fetch_features_by_cnj(tribunal_sigla: str, cnjs: set[str], now) -> dict[str, np.ndarray]:
    """Carrega as 19 features pra um set de CNJs (qualquer classificação)."""
    if not cnjs:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, numero_cnj, COALESCE(classe_codigo,''), "
            "COALESCE(classe_nome,'') "
            "FROM tribunals_process "
            "WHERE tribunal_id = %s AND numero_cnj = ANY(%s)",
            [tribunal_sigla, list(cnjs)],
        )
        proc_rows = cur.fetchall()
    if not proc_rows:
        return {}
    pids = [r[0] for r in proc_rows]
    agg = _fetch_agg_for_pids(pids)
    partes = _fetch_partes_for_pids(pids)
    out: dict[str, np.ndarray] = {}
    for pid, cnj, cls_cod, cls_nome in proc_rows:
        row = agg.get(pid)
        if row is None:
            # processo sem mov — usa zeros
            row = (0, 0, None, 0, 0, 0, 0, 0, 0, 0, 0) + tuple([0] * len(NOVOS_REGEX))
        feats = _features_from_row(row, cnj, cls_cod, cls_nome, partes.get(pid, 0), now)
        out[cnj] = _features_to_vec(feats)
    return out


# ── ASCII histogram ──────────────────────────────────────────────────────────

def _ascii_hist(values: list[float], bins: int = 10, width: int = 40) -> str:
    if not values:
        return '(vazio)'
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        return f'{vmin:.3f}  | ' + '█' * width + f'  ({len(values)})'
    step = (vmax - vmin) / bins
    counts = [0] * bins
    for v in values:
        b = min(int((v - vmin) / step), bins - 1)
        counts[b] += 1
    maxc = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        lo = vmin + i * step
        hi = lo + step
        bar = '█' * int(width * c / maxc)
        lines.append(f'  {lo:5.3f}–{hi:5.3f} |{bar:<{width}}| {c}')
    return '\n'.join(lines)


# ── Command ──────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = 'Minera candidatos a falso negativo do classificador v6.'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default='',
                            help='Sigla do tribunal (default: todos ativos).')
        parser.add_argument('--limit', type=int, default=DEFAULT_TOP_N,
                            help='Top N candidatos no CSV final.')
        parser.add_argument('--output', default='',
                            help='Path do CSV (default: data/fn_candidatos_YYYYMMDD.csv).')
        parser.add_argument('--min-data', default='',
                            help='Filtra Process.inserido_em >= YYYY-MM-DD.')
        parser.add_argument('--upsert-lote', action='store_true',
                            help='Também cria AmostraValidacao via sampling.criar_lote.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Não escreve CSV nem relatório.')
        parser.add_argument('--limite-universo', type=int, default=DEFAULT_LIMITE_UNIVERSO,
                            help='Cap no universo de NAO_LEAD pra performance.')

    def handle(self, *args, **opts):
        t_start = time.time()
        sigla = (opts['tribunal'] or '').strip().upper()
        limit = max(1, int(opts['limit']))
        dry = bool(opts['dry_run'])
        upsert_lote = bool(opts['upsert_lote'])
        limite_universo = max(1, int(opts['limite_universo']))

        if sigla:
            tribunais = [sigla]
        else:
            tribunais = list(
                Tribunal.objects.filter(ativo=True).values_list('sigla', flat=True)
            )
            if not tribunais:
                raise CommandError('Nenhum tribunal ativo encontrado.')

        min_data = None
        if opts['min_data']:
            try:
                min_data = datetime.strptime(opts['min_data'], '%Y-%m-%d')
                if settings.USE_TZ:
                    min_data = timezone.make_aware(min_data)
            except ValueError:
                raise CommandError(f'--min-data inválido: {opts["min_data"]!r}; use YYYY-MM-DD.')

        output = opts['output'] or str(
            REPO_ROOT / 'data' / f'fn_candidatos_{datetime.now():%Y%m%d}.csv'
        )

        self.stdout.write(self.style.NOTICE(
            f'\nminerar_fn  tribunais={tribunais}  limit={limit:,}  '
            f'min_data={min_data!s}  dry_run={dry}  upsert_lote={upsert_lote}\n'
        ))

        # ── E4 / E5 / E6 setup (pré-loop) ────────────────────────────────────
        now = timezone.now() if settings.USE_TZ else datetime.now()
        e4_W, e4_b = self._setup_e4(tribunais, now)
        e5_centroids, e5_density = self._setup_e5(tribunais, now)
        e6_centroid = self._setup_e6(now)  # treina em TRF1 sempre

        # ── Loop principal ───────────────────────────────────────────────────
        self.stdout.write(self.style.NOTICE('Iterando universo NAO_LEAD em chunks...'))
        all_candidates: list[dict] = []
        n_total = 0
        n_ativados_por_estrategia: dict[str, int] = defaultdict(int)

        for chunk in _iter_universe(tribunais, min_data, limite_universo):
            pids = [t[0] for t in chunk]
            agg_map = _fetch_agg_for_pids(pids)
            partes_map = _fetch_partes_for_pids(pids)

            for pid, cnj, sigla_t, classe_cod, classe_nome, score_modelo, _total_movs in chunk:
                n_total += 1
                row = agg_map.get(pid)
                if row is None:
                    # Sem movs — pula (não dá pra avaliar E2; modelo já marcou NAO_LEAD).
                    continue
                feats = _features_from_row(row, cnj, classe_cod, classe_nome,
                                            partes_map.get(pid, 0), now)
                vec = _features_to_vec(feats)

                # Novos regex counts (E2): últimas 7 colunas do row.
                novo_counts = row[-len(NOVOS_REGEX):]
                novo_matches = sum(1 for c in novo_counts if (c or 0) > 0)

                total_movs = row[0] or 0

                motivos: list[str] = []
                suspeita = 0.0

                # E1 — score band [0.10, 0.20]
                if score_modelo is not None and E1_BAND_MIN <= score_modelo <= E1_BAND_MAX:
                    suspeita += E1_DELTA
                    motivos.append('E1')
                    n_ativados_por_estrategia['E1'] += 1

                # E2 — novos regex matches
                if novo_matches > 0:
                    delta = E2_MAX_DELTA * min(novo_matches / 3.0, 1.0)
                    suspeita += delta
                    motivos.append('E2')
                    n_ativados_por_estrategia['E2'] += 1

                # E3 — F1 órfão (Cumprimento mas com poucas movs)
                if (classe_cod or '') in CLASSES_CUMPRIMENTO and total_movs <= E3_MAX_MOVS:
                    suspeita += E3_DELTA
                    motivos.append('E3')
                    n_ativados_por_estrategia['E3'] += 1

                # E4 — similaridade com recuperados (mini-LR)
                if e4_W is not None:
                    sim = float(_mini_lr_score(vec.reshape(1, -1), e4_W, e4_b)[0])
                    if sim > E4_THRESHOLD:
                        suspeita += E4_DELTA
                        motivos.append('E4')
                        n_ativados_por_estrategia['E4'] += 1
                else:
                    sim = None  # noqa: F841 (mantido pra clareza futura)

                # E5 — KMeans cluster density
                if e5_centroids is not None and e5_density is not None:
                    label = int(_assign_clusters(vec.reshape(1, -1), e5_centroids)[0])
                    if e5_density[label] > E5_DENSITY_THRESHOLD:
                        suspeita += E5_DELTA
                        motivos.append('E5')
                        n_ativados_por_estrategia['E5'] += 1

                # E6 — cross-tribunal cosine sim
                if e6_centroid is not None and sigla_t != 'TRF1':
                    csim = float(_cosine_sim_to_centroid(vec.reshape(1, -1), e6_centroid)[0])
                    if csim > E6_SIM_THRESHOLD:
                        suspeita += E6_DELTA
                        motivos.append('E6')
                        n_ativados_por_estrategia['E6'] += 1

                if not motivos:
                    continue

                suspeita = min(1.0, suspeita)
                all_candidates.append({
                    'cnj': cnj,
                    'tribunal': sigla_t,
                    'score_modelo': float(score_modelo) if score_modelo is not None else 0.0,
                    'suspeita_score': suspeita,
                    'motivos': '|'.join(motivos),
                    'top_features': _top_negative_contributions(feats),
                })

        elapsed = time.time() - t_start
        self.stdout.write(
            f'Processados {n_total:,} NAO_LEAD em {elapsed:.0f}s; '
            f'candidatos (≥1 motivo): {len(all_candidates):,}'
        )

        # Sort por suspeita_score desc + corte top-N
        all_candidates.sort(key=lambda d: -d['suspeita_score'])
        top = all_candidates[:limit]

        # ── Logs / tabela ────────────────────────────────────────────────────
        self._log_estrategias(n_ativados_por_estrategia, len(top))

        if dry:
            self.stdout.write(self.style.WARNING(
                f'\n--dry-run: nenhum CSV escrito. Top-{limit} teriam '
                f'{len(top):,} linhas.'
            ))
            self._log_distribuicao(top)
            return

        # ── Escrita CSV ──────────────────────────────────────────────────────
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['cnj', 'tribunal', 'score_modelo', 'suspeita_score',
                             'motivos', 'top_features'])
            for c in top:
                writer.writerow([
                    c['cnj'], c['tribunal'],
                    f'{c["score_modelo"]:.6f}', f'{c["suspeita_score"]:.6f}',
                    c['motivos'], json.dumps(c['top_features'], ensure_ascii=False),
                ])
        self.stdout.write(self.style.SUCCESS(f'CSV escrito: {out_path}  ({len(top):,} linhas)'))

        # ── Relatório markdown ───────────────────────────────────────────────
        self._write_report(top, n_total, n_ativados_por_estrategia, tribunais,
                            min_data, limit, limite_universo)

        # ── Upsert lote (T7) ─────────────────────────────────────────────────
        if upsert_lote:
            self._tentar_upsert_lote(top, tribunais)

        self.stdout.write(f'\n⏱  tempo total: {time.time() - t_start:.0f}s')

    # ─────────────────────────────────────────────────────────────────────────

    def _setup_e4(self, tribunais: list[str], now):
        """Treina mini-LR pra E4. Retorna (W, b) ou (None, None)."""
        cnjs_recup = _load_cnjs_csv(RECUPERADOS_CSV)
        if not cnjs_recup:
            self.stdout.write(self.style.WARNING(
                f'E4: {RECUPERADOS_CSV.name} ausente/vazio — desabilitando.'
            ))
            return None, None
        self.stdout.write(f'E4: {len(cnjs_recup):,} CNJs recuperados carregados.')

        # Features dos positivos (TRF1)
        pos_vecs = list(_fetch_features_by_cnj('TRF1', cnjs_recup, now).values())
        if len(pos_vecs) < 50:
            self.stdout.write(self.style.WARNING(
                f'E4: só {len(pos_vecs)} positivos com features — desabilitando.'
            ))
            return None, None

        # Negativos: amostra random de NAO_LEAD TRF1
        rng = np.random.default_rng(42)
        neg_pids = list(
            Process.objects
            .filter(tribunal__sigla='TRF1',
                    classificacao=Process.CLASSIF_NAO_LEAD)
            .order_by('?')
            .values_list('id', flat=True)[:len(pos_vecs) * 2]
        )
        # Carrega features via batch
        neg_vecs = []
        if neg_pids:
            chunk_ids = neg_pids[:len(pos_vecs)]
            agg = _fetch_agg_for_pids(chunk_ids)
            partes = _fetch_partes_for_pids(chunk_ids)
            proc_rows = (
                Process.objects.filter(id__in=chunk_ids)
                .values_list('id', 'numero_cnj', 'classe_codigo', 'classe_nome')
            )
            for pid, cnj, cls_cod, cls_nome in proc_rows:
                row = agg.get(pid)
                if row is None:
                    row = (0, 0, None) + (0,) * (8 + len(NOVOS_REGEX))
                feats = _features_from_row(row, cnj, cls_cod or '', cls_nome or '',
                                            partes.get(pid, 0), now)
                neg_vecs.append(_features_to_vec(feats))
        if len(neg_vecs) < 50:
            self.stdout.write(self.style.WARNING(
                f'E4: só {len(neg_vecs)} negativos pra treino — desabilitando.'
            ))
            return None, None

        # Amostra balanceada
        m = min(len(pos_vecs), len(neg_vecs))
        rng.shuffle(pos_vecs)
        rng.shuffle(neg_vecs)
        X_pos = np.array(pos_vecs[:m])
        X_neg = np.array(neg_vecs[:m])
        W, b = _train_mini_lr(X_pos, X_neg)
        self.stdout.write(
            f'E4: mini-LR treinada com {m} pos + {m} neg ({len(FEATURE_NAMES)} features).'
        )
        return W, b

    def _setup_e5(self, tribunais: list[str], now):
        """KMeans em leads TRF1. Retorna (centroids, density_por_cluster)."""
        cnjs_leads = _load_cnjs_csv(LEADS_TRF1_CSV)
        if not cnjs_leads:
            self.stdout.write(self.style.WARNING(
                f'E5: {LEADS_TRF1_CSV.name} ausente/vazio — desabilitando.'
            ))
            return None, None

        # Carrega features de uma amostra (cap em 20k pra performance)
        amostra = list(cnjs_leads)[:20_000]
        feats_map = _fetch_features_by_cnj('TRF1', set(amostra), now)
        if len(feats_map) < E5_K * 5:
            self.stdout.write(self.style.WARNING(
                f'E5: só {len(feats_map)} leads com features — desabilitando.'
            ))
            return None, None

        X_leads = np.array(list(feats_map.values()))
        self.stdout.write(f'E5: rodando KMeans (k={E5_K}) em {len(X_leads):,} leads...')
        centroids, labels_leads = _kmeans(X_leads, k=E5_K)

        # Density = fração de leads no cluster.
        # Como aqui só temos leads (não NAO_LEAD), aproximamos density pela
        # *concentração relativa* — clusters com peso > média global (1/k)
        # × fator são considerados densos. Implementação prática:
        # density[i] = (n_leads_i / total_leads) / (1/k) clamp [0, 1].
        # Isso é heurística — funciona porque clusters muito povoados de
        # leads sinalizam padrão recorrente que NAO_LEADs alinhados podem ser FN.
        counts = np.bincount(labels_leads, minlength=E5_K).astype(float)
        rel = counts / counts.sum()  # soma=1
        density = np.clip(rel * E5_K, 0.0, 1.0)  # 1.0 quando cluster tem 1/k dos leads
        self.stdout.write(
            f'E5: density por cluster — max={density.max():.2f}, '
            f'≥0.7 em {int((density > E5_DENSITY_THRESHOLD).sum())}/{E5_K} clusters.'
        )
        return centroids, density

    def _setup_e6(self, now):
        """Centroid (média) de features de leads TRF1 confirmados."""
        cnjs_leads = _load_cnjs_csv(LEADS_TRF1_CSV)
        if not cnjs_leads:
            self.stdout.write(self.style.WARNING(
                f'E6: {LEADS_TRF1_CSV.name} ausente/vazio — desabilitando.'
            ))
            return None
        amostra = list(cnjs_leads)[:10_000]
        feats_map = _fetch_features_by_cnj('TRF1', set(amostra), now)
        if len(feats_map) < 100:
            self.stdout.write(self.style.WARNING(
                f'E6: só {len(feats_map)} leads pra centroid — desabilitando.'
            ))
            return None
        centroid = np.mean(np.array(list(feats_map.values())), axis=0)
        self.stdout.write(f'E6: centroid TRF1 calculado de {len(feats_map):,} leads.')
        return centroid

    def _log_estrategias(self, ativadas: dict[str, int], n_final: int):
        self.stdout.write(self.style.NOTICE('\nEstatística por estratégia:\n'))
        self.stdout.write('| Estratégia | Ativados |')
        self.stdout.write('|------------|---------:|')
        for k in ('E1', 'E2', 'E3', 'E4', 'E5', 'E6'):
            self.stdout.write(f'| {k}         | {ativadas.get(k, 0):>8,} |')
        self.stdout.write(f'\nTotal no top-N final: {n_final:,}')

    def _log_distribuicao(self, top: list[dict]):
        if not top:
            self.stdout.write('(nenhum candidato)')
            return
        scores = [c['suspeita_score'] for c in top]
        self.stdout.write(self.style.NOTICE('\nDistribuição suspeita_score (top):'))
        self.stdout.write(_ascii_hist(scores))
        by_trib: dict[str, int] = defaultdict(int)
        for c in top:
            by_trib[c['tribunal']] += 1
        self.stdout.write('\nPor tribunal:')
        for t, n in sorted(by_trib.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {t:<8} {n:>6,}')

    def _write_report(self, top, n_total, ativadas, tribunais, min_data, limit, limite_universo):
        if not top:
            return
        scores = [c['suspeita_score'] for c in top]
        date = datetime.now().strftime('%Y%m%d')
        path = MINING_REPORT_DIR / f'MINING_FN_v1_{date}.md'
        path.parent.mkdir(parents=True, exist_ok=True)

        by_trib: dict[str, int] = defaultdict(int)
        for c in top:
            by_trib[c['tribunal']] += 1

        lines = []
        lines.append(f'# Mineração FN — relatório {date}\n')
        lines.append('## Parâmetros\n')
        lines.append(f'- Tribunais: `{tribunais}`')
        lines.append(f'- min_data: `{min_data}`')
        lines.append(f'- limite_universo: `{limite_universo:,}`')
        lines.append(f'- top-N: `{limit:,}`')
        lines.append(f'- versao_modelo: `{VERSAO}`\n')

        lines.append('## Distribuição suspeita_score\n')
        lines.append('```')
        lines.append(_ascii_hist(scores))
        lines.append('```\n')

        lines.append('## Estatísticas por estratégia\n')
        lines.append('| Estratégia | Ativados | % do top |')
        lines.append('|------------|---------:|---------:|')
        for k in ('E1', 'E2', 'E3', 'E4', 'E5', 'E6'):
            n = ativadas.get(k, 0)
            pct = (100.0 * n / max(1, len(top)))
            lines.append(f'| {k} | {n:,} | {pct:.1f}% |')
        lines.append('')

        lines.append('## Distribuição por tribunal\n')
        lines.append('| Tribunal | Candidatos |')
        lines.append('|----------|-----------:|')
        for t, n in sorted(by_trib.items(), key=lambda x: -x[1]):
            lines.append(f'| {t} | {n:,} |')
        lines.append('')

        lines.append('## Top 20 candidatos\n')
        lines.append('| # | CNJ | Tribunal | score_modelo | suspeita_score | motivos |')
        lines.append('|---|-----|----------|-------------:|--------------:|---------|')
        for i, c in enumerate(top[:20], 1):
            lines.append(
                f'| {i} | `{c["cnj"]}` | {c["tribunal"]} | '
                f'{c["score_modelo"]:.4f} | {c["suspeita_score"]:.4f} | {c["motivos"]} |'
            )
        lines.append('')

        path.write_text('\n'.join(lines), encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Relatório escrito: {path}'))

    def _tentar_upsert_lote(self, top: list[dict], tribunais: list[str]):
        """T7 dependency. Se sampling.criar_lote não existir, no-op."""
        try:
            from tribunals.sampling import criar_lote  # type: ignore[attr-defined]
        except (ImportError, AttributeError):
            self.stdout.write(self.style.WARNING(
                '--upsert-lote: tribunals.sampling.criar_lote não disponível (T7 ainda não merged) — pulando.'
            ))
            return

        cnjs = [c['cnj'] for c in top]
        suspeita_map = {c['cnj']: c['suspeita_score'] for c in top}
        motivos_map = {c['cnj']: c['motivos'].split('|') for c in top}
        try:
            lote = criar_lote(
                estrategia='fn_candidatos',
                tribunal_sigla=tribunais[0] if len(tribunais) == 1 else None,
                cnjs=cnjs,
                suspeita_score_map=suspeita_map,
                motivos_map=motivos_map,
                versao_modelo=VERSAO,
            )
            self.stdout.write(self.style.SUCCESS(
                f'--upsert-lote: AmostraValidacao #{getattr(lote, "pk", "?")} criada com {len(cnjs)} itens.'
            ))
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f'--upsert-lote: falhou ({exc}) — seguindo sem bloquear.'
            ))
