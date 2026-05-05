"""Classifier de leads — aplica modelo Logistic Regression v5.

Pesos hardcoded da primeira versão (treinada em 2026-04-30, AUC=0.9523,
precision@5000=0.939). Quando subir nova versão, persistimos em
ClassificadorVersao e carregamos do banco.

Hierarquia de classificação:
  PRECATORIO (N1)         — score > 0.7 + tem expedição explícita (F2 ou F11)
  PRE_PRECATORIO (N2)     — Cumprimento + Trânsito julgado/Mudança Classe, sem expedição
  DIREITO_CREDITORIO (N3) — score > 0.2 + classe de Cumprimento ou similar
  NAO_LEAD                — resto
"""
from __future__ import annotations

import math
import re
from typing import Optional

from django.utils import timezone as djtz

# === v5 — pesos treinados em 2026-04-30 (TRF1, 887k procs) ==================
VERSAO = 'v5'

WEIGHTS = {
    '_intercept_':            -3.196,
    'F1_cumprim':              1.922,
    'F10_juizado_ANTI':       -1.129,
    'F2_precat_tc':            0.079,
    'F7_envTrib_tc':           0.085,
    'F11_precat_text':         0.894,
    'F12_rpv_text':            0.527,
    'F13_reqPag_text':        -0.560,
    'F14_oficio_text':        -0.186,
    'F15_logMovs':             2.311,
    'F16_logTipos':           -1.738,
    'F17_logN1count':          0.181,
    'F18_anoZ':                0.438,
    'F19_cancelado_ANTI':     -0.000,
    'F20_exp_juriscope':      -0.025,
    'F21_diasUltMovZ':         0.570,
    'F23_logPartes':          -0.401,
    'F1xF11':                 -0.134,
    'F1xF15':                  1.612,
    'F1xF20':                 -0.021,
}

METRICAS = {
    'auc': 0.9523,
    'precision_at_500': 0.978,
    'precision_at_1000': 0.969,
    'precision_at_5000': 0.939,
    'precision_at_10000': 0.919,
    'train_size': 710028,
    'test_size': 177506,
    'n_features': 19,
}

# Estatísticas pra normalização (mesmas do treino)
ANO_MEAN = 2018.9
ANO_STD = 6.6
DIAS_ULT_MOV_MEAN = 687.0
DIAS_ULT_MOV_STD = 570.0

CLASSES_CUMPRIMENTO = {
    '12078',  # Cumprimento de Sentença contra a Fazenda Pública (federal e estadual)
    '156',    # Cumprimento de Sentença
    '15160',  # Cumprimento de Sentença de Ações Coletivas
    '15215',  # Cumprimento contra Fazenda Mediante Execução Invertida
    '12079',  # Execução de Título Extrajudicial contra a Fazenda Pública (estadual)
}

CNJ_ANO_RE = re.compile(r'^\d{7}-\d{2}\.(\d{4})\.')

# Thresholds (revisáveis com base em produção)
THRESHOLD_PRECATORIO = 0.70
THRESHOLD_PRE_PRECATORIO = 0.40
THRESHOLD_DIREITO_CREDITORIO = 0.20


def _ano_cnj(numero: str) -> int:
    if not numero:
        return 0
    m = CNJ_ANO_RE.match(numero)
    return int(m.group(1)) if m else 0


def _is_anti_classe(classe_nome: str) -> int:
    if not classe_nome:
        return 0
    n = classe_nome.lower()
    return int('juizado especial' in n or 'recurso inominado' in n
               or 'procedimento comum' in n)


def _sigmoid(z: float) -> float:
    z = max(min(z, 30.0), -30.0)
    return 1.0 / (1.0 + math.exp(-z))


def compute_features(processo) -> dict:
    """Computa as 19 features do v5 a partir de um Process.

    Retorna dict {feature_name: value}. Valores numéricos contínuos
    já normalizados (z-score / log).
    """
    from .models import Movimentacao, ProcessoParte

    classe_cod = (processo.classe_codigo or '')
    classe_nome = processo.classe_nome or ''
    ano = _ano_cnj(processo.numero_cnj)
    f1 = int(classe_cod in CLASSES_CUMPRIMENTO)
    f10 = _is_anti_classe(classe_nome)

    # Aggregations sobre Movimentacao do processo
    movs = Movimentacao.objects.filter(processo_id=processo.pk)
    total_movs = movs.count()

    if total_movs == 0:
        return _empty_features(ano, f1, f10)

    # Counts por tipo_comunicacao + texto
    f2_n = movs.filter(tipo_comunicacao__in=['Expedição de precatório/rpv', 'Precatório']).count()
    f7_n = movs.filter(tipo_comunicacao__in=['Enviada ao Tribunal', 'Preparada para Envio']).count()
    distinct_tipos = movs.exclude(tipo_comunicacao='').values('tipo_comunicacao').distinct().count()

    f11_n = movs.filter(texto__iregex=r'precat[óo]rio').count()
    f12_n = movs.filter(texto__iregex=r'\mrpv\M').count()
    f13_n = movs.filter(texto__iregex=r'requisi[çc][ãa]o de pagamento').count()
    f14_n = movs.filter(texto__iregex=r'of[íi]cio requisit[óo]rio').count()
    f19_n = movs.filter(
        texto__iregex=r'cancelamento de precat[óo]rio|cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|revoga[çc][ãa]o de rpv'
    ).count()
    f20_n = movs.filter(
        texto__iregex=r'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
    ).count()

    # Recência
    ult_mov_dt = movs.values_list('data_disponibilizacao', flat=True).order_by('-data_disponibilizacao').first()
    if ult_mov_dt:
        dias_ult = (djtz.now() - ult_mov_dt).total_seconds() / 86400
    else:
        dias_ult = 9999

    # Partes
    n_partes = ProcessoParte.objects.filter(processo_id=processo.pk).count()

    f15 = math.log1p(total_movs) / math.log(500)
    f16 = math.log1p(distinct_tipos) / math.log(50)
    f17 = math.log1p(f11_n + f12_n + f13_n + f14_n) / math.log(20)
    f18 = (ano - ANO_MEAN) / ANO_STD if ano > 0 else 0.0
    f21 = (dias_ult - DIAS_ULT_MOV_MEAN) / DIAS_ULT_MOV_STD
    f23 = math.log1p(n_partes) / math.log(50)

    f2 = int(f2_n > 0); f7 = int(f7_n > 0)
    f11 = int(f11_n > 0); f12 = int(f12_n > 0); f13 = int(f13_n > 0); f14 = int(f14_n > 0)
    f19 = int(f19_n > 0); f20 = int(f20_n > 0)

    return {
        'F1_cumprim':         f1,
        'F10_juizado_ANTI':   f10,
        'F2_precat_tc':       f2,
        'F7_envTrib_tc':      f7,
        'F11_precat_text':    f11,
        'F12_rpv_text':       f12,
        'F13_reqPag_text':    f13,
        'F14_oficio_text':    f14,
        'F15_logMovs':        f15,
        'F16_logTipos':       f16,
        'F17_logN1count':     f17,
        'F18_anoZ':           f18,
        'F19_cancelado_ANTI': f19,
        'F20_exp_juriscope':  f20,
        'F21_diasUltMovZ':    f21,
        'F23_logPartes':      f23,
        'F1xF11':             f1 * f11,
        'F1xF15':             f1 * f15,
        'F1xF20':             f1 * f20,
    }


def _empty_features(ano: int, f1: int, f10: int) -> dict:
    """Quando processo não tem mov, retorna features zeradas (mas com classe)."""
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


def predict_score(features: dict) -> float:
    """Aplica pesos LR e retorna probabilidade [0, 1]."""
    z = WEIGHTS['_intercept_']
    for fname, value in features.items():
        w = WEIGHTS.get(fname, 0.0)
        z += w * value
    return _sigmoid(z)


def classificar(processo, features: Optional[dict] = None) -> tuple[str, float, dict]:
    """Classifica um processo. Retorna (categoria, score, features).

    Hierarquia:
      score > 0.7 AND (F2 OR F11)             → PRECATORIO
      score > 0.4 AND F1 (Cumprimento)        → PRE_PRECATORIO
      score > 0.2 AND F1                      → DIREITO_CREDITORIO
      else                                    → NAO_LEAD
    """
    from .models import Process

    if features is None:
        features = compute_features(processo)
    score = predict_score(features)

    is_cumprim = features.get('F1_cumprim', 0) == 1
    has_precat_explicit = (features.get('F2_precat_tc', 0) == 1
                           or features.get('F11_precat_text', 0) == 1)

    if score >= THRESHOLD_PRECATORIO and has_precat_explicit:
        cat = Process.CLASSIF_PRECATORIO
    elif score >= THRESHOLD_PRE_PRECATORIO and is_cumprim:
        cat = Process.CLASSIF_PRE_PRECATORIO
    elif score >= THRESHOLD_DIREITO_CREDITORIO and is_cumprim:
        cat = Process.CLASSIF_DIREITO_CREDITORIO
        # F13 (requisição de pagamento) tem peso negativo no modelo porque sem
        # "precatório" era falso positivo no treino — mas combinado com Cumprimento
        # e sem cancelamento é sinal claro de expedição em andamento.
        if (features.get('F13_reqPag_text', 0) == 1
                and features.get('F19_cancelado_ANTI', 0) == 0):
            cat = Process.CLASSIF_PRE_PRECATORIO
    else:
        cat = Process.CLASSIF_NAO_LEAD

    return cat, score, features


def classificar_e_persistir(processo, registrar_log: bool = True) -> tuple[str, float]:
    """Classifica + atualiza Process. Cria ClassificacaoLog apenas quando
    a categoria mudou (evita inflar tabela de log).
    """
    from .models import ClassificacaoLog, Process

    cat, score, features = classificar(processo)
    now = djtz.now()

    classif_anterior = processo.classificacao
    Process.objects.filter(pk=processo.pk).update(
        classificacao=cat,
        classificacao_score=score,
        classificacao_versao=VERSAO,
        classificacao_em=now,
    )
    if registrar_log and classif_anterior != cat:
        ClassificacaoLog.objects.create(
            processo=processo, classificacao=cat, score=score, versao=VERSAO,
            features_snapshot={k: round(v, 4) for k, v in features.items()},
        )
    return cat, score
