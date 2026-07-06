"""Classifier de leads — aplica modelo Logistic Regression v6.

v6 treinada em 2026-05-08 (TRF1, 1,050,791 procs, AUC=0.9610,
precision@5000=0.991). Retreinamento com universo atualizado e
normalização recalculada.

Hierarquia de classificação:
  PRECATORIO (N1)         — score > 0.7 + tem expedição explícita (F2 ou F11)
  PRE_PRECATORIO (N2)     — Cumprimento + Trânsito julgado/Mudança Classe, sem expedição
  DIREITO_CREDITORIO (N3) — score > 0.2 + classe de Cumprimento ou similar
  NAO_LEAD                — resto

Hot reload (T17):
  Pesos vivem em ClassificadorVersao(ativa=True). O classificador mantém
  cache em memória com TTL `settings.CLASSIFICADOR_RELOAD_TTL` (default 60s).
  A cada chamada de `classificar()`, se o TTL expirou, recarrega do DB.
  Em qualquer erro (DB down, pesos corrompidos, sem versão ativa) cai pro
  HARDCODED_WEIGHTS deste arquivo.

  Limitação conhecida: se a versão DB declarar features novas (ex: F24+ em v7)
  que `compute_features` ainda não extrai, os pesos dessas features ficam
  desperdiçados — o predict ignora silenciosamente (warning no log no primeiro
  reload). Pra ativar features novas é necessário deploy de código que atualize
  `compute_features`. Hot reload cobre só ajuste de pesos das features já
  conhecidas (F1-F23) — caminho típico de re-treino mantendo schema.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from typing import Optional

from django.conf import settings
from django.db import connection
from django.utils import timezone as djtz

from .models import ProcessoParte

logger = logging.getLogger('voyager.tribunals.classificador')

# === v6 — pesos treinados em 2026-05-08 (TRF1, 1.05M procs) =================
# Estes valores são o FALLBACK hardcoded usado quando:
#   - Não há ClassificadorVersao(ativa=True) no DB (boot, dev fresco)
#   - DB indisponível durante reload
#   - Pesos da versão ativa não passam na validação (features faltando)
# O caminho normal é carregar do DB. Constante mantida pra ser self-contained
# (testes, scripts ad-hoc) e pra garantir que o worker nunca fica "sem pesos".
VERSAO = 'v6'

HARDCODED_WEIGHTS = {
    '_intercept_':            -2.635,
    'F1_cumprim':              1.801,
    'F10_juizado_ANTI':       -1.119,
    'F2_precat_tc':            0.129,
    'F7_envTrib_tc':           0.292,
    'F11_precat_text':         0.746,
    'F12_rpv_text':            0.357,
    'F13_reqPag_text':        -0.659,
    'F14_oficio_text':        -0.174,
    'F15_logMovs':             1.546,
    'F16_logTipos':           -3.184,
    'F17_logN1count':          0.143,
    'F18_anoZ':                0.334,
    'F19_cancelado_ANTI':      0.000,
    'F20_exp_juriscope':      -0.033,
    'F21_diasUltMovZ':         0.497,
    'F23_logPartes':          -0.606,
    'F1xF11':                 -0.131,
    'F1xF15':                  1.630,
    'F1xF20':                 -0.027,
}

# Compat: import legado `from tribunals.classificador import WEIGHTS`
# continua funcionando, mas agora aponta pro snapshot hardcoded — o caminho
# vivo passa por `_current_weights()`.
WEIGHTS = HARDCODED_WEIGHTS

METRICAS = {
    'auc': 0.9610,
    'precision_at_500': 0.986,
    'precision_at_1000': 0.993,
    'precision_at_5000': 0.991,
    'precision_at_10000': 0.982,
    'train_size': 840634,
    'test_size': 210157,
    'n_features': 19,
}

# Estatísticas pra normalização (mesmas do treino)
ANO_MEAN = 2019.66
ANO_STD = 6.49
DIAS_ULT_MOV_MEAN = 532.24
DIAS_ULT_MOV_STD = 574.57

CLASSES_CUMPRIMENTO = {
    '12078',  # Cumprimento de Sentença contra a Fazenda Pública (federal e estadual)
    '156',    # Cumprimento de Sentença
    '15160',  # Cumprimento de Sentença de Ações Coletivas
    '15215',  # Cumprimento contra Fazenda Mediante Execução Invertida
    '12079',  # Execução de Título Extrajudicial contra a Fazenda Pública (estadual)
}

# Subconjunto que indica DEVEDOR PÚBLICO (Fazenda) — onde precatório/RPV nascem.
# A classe 156 ("Cumprimento de Sentença" genérico) é execução PRIVADA e domina o
# DIREITO_CREDITORIO do TJSP (~94%), nunca virando precatório. Filtrar por estas
# classes na origem evita gastar o cap de pull/scrape do Falcon com lixo privado.
# A 15160 (Ações Coletivas) fica de fora do código fixo por ser ambígua — é
# capturada pelo match textual 'fazenda públ' quando o réu é ente público.
CLASSES_FAZENDA_PUBLICA = {
    '12078',  # Cumprimento de Sentença contra a Fazenda Pública (federal/estadual)
    '12079',  # Execução de Título Extrajudicial contra a Fazenda Pública
    '15215',  # Cumprimento contra Fazenda Mediante Execução Invertida
}

# Tribunais onde a "regra de sinal" promove Cumprimento→PRECATORIO direto
# (rollout controlado; começa só no TJAL). O LR v6 trava o score do eSAJ em
# ≤0,582 (sinais de expedição do eSAJ não batem com o treino PJe/DataJud), então
# um Cumprimento com ofício requisitório/expedição NOS MOVIMENTOS — que é
# precatório de fato e o Falcon baixa+parseia (1º grau) — nunca chegaria a N1.
# TJMA (2026-07-02): mesmo problema com outro sintoma — os CumSenFaz com
# "Expedição de precatório/RPV" publicada ficam diluídos como N2/NAO_LEAD e o
# pull diário (80/dia) baixa pré-precatório genérico no lugar deles. Nos
# tribunais que TAMBÉM têm a regra negativa (PAGAMENTO_SINAL_TRIBUNAIS), o F24
# veta a promoção: expedido e já pago não sobe.
PRECATORIO_SINAL_TRIBUNAIS = {'TJAL', 'TJMA', 'TJSP'}

# Score gravado na promoção por regra de sinal. Precisa passar o
# VOYAGER_MIN_SCORE_N1=0.70 do Falcon; é certeza de regra, não probabilidade LR.
SCORE_PROMOCAO_SINAL = 1.0

# Regra de sinal NEGATIVA (rollout controlado; começa no TJMA): comunicação
# DJEN com marcador de PAGAMENTO (alvará de levantamento, sequestro deferido,
# extinção) POSTERIOR ao último sinal de expedição = crédito em levantamento —
# não é lead comprável (decisão de negócio 2026-07-01; auditoria dos autos
# reais do 1º lote TJMA: N1 0.70-0.77 dominado por RPV municipal já paga via
# BacenJud/alvará). Rebaixa N1/N2 → NAO_LEAD e derruba o score abaixo de
# todos os cortes.
PAGAMENTO_SINAL_TRIBUNAIS = {'TJMA', 'TJSP'}
SCORE_REBAIXAMENTO_SINAL = 0.10

CNJ_ANO_RE = re.compile(r'^\d{7}-\d{2}\.(\d{4})\.')

# Agrega todos os counts de movimentações em uma única passagem pela tabela,
# eliminando os ~10 round-trips separados do código anterior.
_MOVS_AGG_SQL = """
    SELECT
        COUNT(*) AS total_movs,
        COUNT(DISTINCT CASE WHEN tipo_comunicacao <> '' THEN tipo_comunicacao END) AS distinct_tipos,
        COALESCE(SUM(CASE WHEN tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                          THEN 1 ELSE 0 END), 0) AS f2_n,
        COALESCE(SUM(CASE WHEN tipo_comunicacao IN ('Enviada ao Tribunal','Preparada para Envio')
                          THEN 1 ELSE 0 END), 0) AS f7_n,
        COALESCE(SUM(CASE WHEN texto ~* 'precat[óo]rio'                THEN 1 ELSE 0 END), 0) AS f11_n,
        COALESCE(SUM(CASE WHEN texto ~* '\\mrpv\\M'                    THEN 1 ELSE 0 END), 0) AS f12_n,
        COALESCE(SUM(CASE WHEN texto ~* 'requisi[çc][ãa]o de pagamento' THEN 1 ELSE 0 END), 0) AS f13_n,
        COALESCE(SUM(CASE WHEN texto ~* 'of[íi]cio requisit[óo]rio'    THEN 1 ELSE 0 END), 0) AS f14_n,
        COALESCE(SUM(CASE WHEN texto ~* 'cancelamento de precat[óo]rio|cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|revoga[çc][ãa]o de rpv'
                          THEN 1 ELSE 0 END), 0) AS f19_n,
        COALESCE(SUM(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                          THEN 1 ELSE 0 END), 0) AS f20_n,
        MAX(CASE WHEN texto ~* 'alvar[áa]\\s+(judicial|de\\s+levantamento)|expe[çc]am?-se\\s+(o\\s+)?alvar[áa]|autorizo[^.]{0,150}sequestro|defiro[^.]{0,100}sequestro|sequestro\\s+do\\s+numer[áa]rio|julgo\\s+extint[oa]|mandado\\s+de\\s+levantamento'
                 THEN data_disponibilizacao END) AS pago_max_dt,
        MAX(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                 THEN data_disponibilizacao END) AS exped_max_dt,
        MAX(data_disponibilizacao) AS ult_mov_dt
    FROM tribunals_movimentacao
    WHERE processo_id = %s
"""

# Thresholds (revisáveis com base em produção)
THRESHOLD_PRECATORIO = 0.70
THRESHOLD_PRE_PRECATORIO = 0.40
THRESHOLD_DIREITO_CREDITORIO = 0.20


# ============================================================================
# Hot reload de pesos do DB (ClassificadorVersao.ativa=True)
# ============================================================================

# Cache thread-safe. Inicializado como "vencido" (loaded_at=0.0) — primeira
# chamada de `classificar()` força carga inicial. Mantém o valor em memória
# até TTL vencer.
_WEIGHTS_CACHE: dict = {
    'versao': None,        # str — versão atualmente em memória ('v6', 'hardcoded', ...)
    'pesos': None,         # dict[str, float] — mapeamento feature → peso
    'thresholds': None,    # dict — reservado pra T futuras (override de thresholds via DB)
    'normas': None,        # dict — reservado (ano_mean, dias_mean, etc — vivem hardcoded por enquanto)
    'loaded_at': 0.0,      # epoch da última carga bem-sucedida (ou tentativa+fallback)
}
_WEIGHTS_LOCK = threading.Lock()


def _reload_ttl_seconds() -> int:
    """TTL configurável via settings.CLASSIFICADOR_RELOAD_TTL (default 60s)."""
    return int(getattr(settings, 'CLASSIFICADOR_RELOAD_TTL', 60))


def _validate_pesos(pesos) -> bool:
    """Retorna True se o dict tem TODAS as features esperadas pelo extrator.

    Aceita superset (v7 com F24-F28 OK — features extras são ignoradas pelo
    predict_score). Rejeita subset (faltar uma feature do v6 corrompe scores).
    """
    if not isinstance(pesos, dict):
        return False
    expected = set(HARDCODED_WEIGHTS.keys())
    return expected.issubset(set(pesos.keys()))


def _carregar_hardcoded(now: float) -> None:
    """Popula o cache com os pesos hardcoded. Caller já segura o lock."""
    _WEIGHTS_CACHE.update(
        versao='hardcoded',
        pesos=dict(HARDCODED_WEIGHTS),
        thresholds=None,
        normas=None,
        loaded_at=now,
    )


def _maybe_reload_weights() -> None:
    """Recarrega pesos do DB se cache vencido. Thread-safe.

    Fast-path sem lock quando dentro do TTL — leitura de epoch é atômica em
    CPython e o pior caso é uma chamada extra dentro do lock. Fallback para
    HARDCODED_WEIGHTS em qualquer erro (DB down, pesos corrompidos, exceção
    inesperada). Sempre marca `loaded_at` no fim pra evitar storm de retries
    quando o DB está fora.
    """
    ttl = _reload_ttl_seconds()
    now = time.time()
    if now - _WEIGHTS_CACHE['loaded_at'] < ttl:
        return

    with _WEIGHTS_LOCK:
        # Double-check: outro thread pode ter recarregado enquanto esperávamos.
        now = time.time()
        if now - _WEIGHTS_CACHE['loaded_at'] < ttl:
            return

        try:
            # Import local: evita ciclo no boot e respeita lazy app loading.
            from .models import ClassificadorVersao
            ativa = (
                ClassificadorVersao.objects
                .filter(ativa=True)
                .only('versao', 'pesos', 'metricas')
                .first()
            )
            if ativa is None:
                if _WEIGHTS_CACHE['versao'] != 'hardcoded':
                    logger.warning(
                        'Nenhuma ClassificadorVersao(ativa=True); usando hardcoded fallback',
                    )
                _carregar_hardcoded(now)
            elif _validate_pesos(ativa.pesos):
                versao_anterior = _WEIGHTS_CACHE['versao']
                if ativa.versao != versao_anterior:
                    logger.info(
                        'classifier reloaded: %s -> %s',
                        versao_anterior or 'hardcoded',
                        ativa.versao,
                    )
                    # Warn sobre features extras desperdiçadas (v7+ com F24+).
                    extras = set(ativa.pesos.keys()) - set(HARDCODED_WEIGHTS.keys())
                    if extras:
                        logger.warning(
                            'Versão %s tem features extras que o extrator não conhece e serão '
                            'ignoradas: %s',
                            ativa.versao,
                            sorted(extras),
                        )
                _WEIGHTS_CACHE.update(
                    versao=ativa.versao,
                    pesos=dict(ativa.pesos),
                    thresholds=None,
                    normas=None,
                    loaded_at=now,
                )
            else:
                logger.warning(
                    'Pesos da versão %s corrompidos (faltam features esperadas); usando hardcoded',
                    ativa.versao,
                )
                _carregar_hardcoded(now)
        except Exception as e:  # fallback é estratégia explícita: nunca propagar
            logger.exception('Erro recarregando pesos: %s; usando hardcoded', e)
            # Só sobrescreve cache se não havia nada (boot). Senão preserva o
            # último valor bom e só atualiza o timestamp pra evitar retry-storm.
            if _WEIGHTS_CACHE['pesos'] is None:
                _carregar_hardcoded(now)
            else:
                _WEIGHTS_CACHE['loaded_at'] = now


def force_reload_weights() -> None:
    """Pula o TTL e força reload imediato.

    Útil em testes, management commands e cenários de "trocar modelo agora".
    """
    with _WEIGHTS_LOCK:
        _WEIGHTS_CACHE['loaded_at'] = 0.0
    _maybe_reload_weights()


def _current_weights() -> dict:
    """Retorna os pesos vigentes (após reload se necessário)."""
    _maybe_reload_weights()
    pesos = _WEIGHTS_CACHE['pesos']
    # Boot edge-case: cache ainda vazio e reload falhou silenciosamente.
    # Não deve acontecer porque `_maybe_reload_weights` sempre popula no fim,
    # mas defendemos contra `pesos=None` por segurança.
    if pesos is None:
        return HARDCODED_WEIGHTS
    return pesos


def get_versao_ativa() -> str:
    """Versão atualmente em memória (após reload se necessário)."""
    _maybe_reload_weights()
    return _WEIGHTS_CACHE['versao'] or 'hardcoded'


# ============================================================================
# Feature extraction (inalterado — escopo v6)
# ============================================================================

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
    from .models import ProcessoParte

    classe_cod = (processo.classe_codigo or '')
    classe_nome = processo.classe_nome or ''
    ano = _ano_cnj(processo.numero_cnj)
    f1 = int(classe_cod in CLASSES_CUMPRIMENTO)
    f10 = _is_anti_classe(classe_nome)

    with connection.cursor() as cur:
        cur.execute(_MOVS_AGG_SQL, [processo.pk])
        row = cur.fetchone()

    (total_movs, distinct_tipos, f2_n, f7_n, f11_n, f12_n,
     f13_n, f14_n, f19_n, f20_n, pago_max_dt, exped_max_dt, ult_mov_dt) = row

    if not total_movs:
        return _empty_features(ano, f1, f10)

    dias_ult = ((djtz.now() - ult_mov_dt).total_seconds() / 86400
                if ult_mov_dt else 9999)

    n_partes = ProcessoParte.objects.filter(processo_id=processo.pk).count()

    f15 = math.log1p(total_movs) / math.log(500)
    f16 = math.log1p(distinct_tipos) / math.log(50)
    f17 = math.log1p(f11_n + f12_n + f13_n + f14_n) / math.log(20)
    f18 = (ano - ANO_MEAN) / ANO_STD if ano > 0 else 0.0
    f21 = (dias_ult - DIAS_ULT_MOV_MEAN) / DIAS_ULT_MOV_STD
    f23 = math.log1p(n_partes) / math.log(50)

    f24 = int(bool(pago_max_dt)
              and (exped_max_dt is None or pago_max_dt >= exped_max_dt))

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
        # Sinal-anti de pagamento (peso LR 0: atua só como regra, como F19).
        'F24_pago_pos_exped_ANTI': f24,
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
        'F24_pago_pos_exped_ANTI': 0,
    }


def predict_score(features: dict, pesos: dict | None = None) -> float:
    """Aplica pesos LR e retorna probabilidade [0, 1].

    Quando `pesos` é None, usa o snapshot vigente em cache. Features que
    aparecem em `pesos` mas não em `features` não contribuem (peso multiplicado
    por 0). Features em `features` mas não em `pesos` também não contribuem.
    """
    if pesos is None:
        pesos = _current_weights()
    z = pesos.get('_intercept_', 0.0)
    for fname, value in features.items():
        w = pesos.get(fname, 0.0)
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

    # Regra de sinal (eSAJ): Cumprimento (F1) com ofício requisitório (F14) ou
    # expedição (F20) nos movimentos é precatório de fato. Promove a N1 com
    # score alto, bypassando o LR (que trava o eSAJ <0.70). Escopado por tribunal.
    if processo.tribunal_id in PRECATORIO_SINAL_TRIBUNAIS:
        if features is None:
            features = compute_features(processo)
        # Guard F24 (só onde a regra negativa está ativa, ex.: TJMA): a
        # promoção NÃO pode passar na frente do rebaixamento por pagamento —
        # expedido mas já pago (alvará/sequestro/extinção posterior) cai pro
        # fluxo normal, onde a regra negativa abaixo rebaixa a NAO_LEAD.
        pago_pos_exped = (
            processo.tribunal_id in PAGAMENTO_SINAL_TRIBUNAIS
            and features.get('F24_pago_pos_exped_ANTI') == 1)
        if (not pago_pos_exped
                and features.get('F1_cumprim') == 1
                and (features.get('F14_oficio_text') == 1
                     or features.get('F20_exp_juriscope') == 1)):
            return Process.CLASSIF_PRECATORIO, SCORE_PROMOCAO_SINAL, features

    # Garante que estamos usando os pesos mais recentes (com TTL).
    pesos = _current_weights()

    if features is None:
        features = compute_features(processo)
    score = predict_score(features, pesos=pesos)

    # Categorização DB-driven (review T20): mesma lógica do path shadow,
    # garante que A/B compara só o score — não a política de threshold.
    cat = _categorizar(score, features, tribunal_id=processo.tribunal_id)

    # Regra de sinal NEGATIVA (TJMA): pagamento publicado no DJEN posterior ao
    # último sinal de expedição → crédito em levantamento; não vira lead.
    # Espelha (inverte) a regra de sinal do TJAL; JURISCOPE tem a checagem
    # fina equivalente nos autos (datas dos documentos).
    if (processo.tribunal_id in PAGAMENTO_SINAL_TRIBUNAIS
            and features.get('F24_pago_pos_exped_ANTI') == 1
            and cat in (Process.CLASSIF_PRECATORIO,
                        Process.CLASSIF_PRE_PRECATORIO)):
        return (Process.CLASSIF_NAO_LEAD,
                min(score, SCORE_REBAIXAMENTO_SINAL), features)

    return cat, score, features


def classificar_e_persistir(processo, registrar_log: bool = True) -> tuple[str, float]:
    """Classifica + atualiza Process. Cria ClassificacaoLog apenas quando
    a categoria mudou (evita inflar tabela de log).
    """
    from .models import ClassificacaoLog, Process

    cat, score, features = classificar(processo)
    now = djtz.now()
    versao_em_uso = get_versao_ativa()

    classif_anterior = processo.classificacao
    Process.objects.filter(pk=processo.pk).update(
        classificacao=cat,
        classificacao_score=score,
        classificacao_versao=versao_em_uso,
        classificacao_em=now,
    )
    if registrar_log and classif_anterior != cat:
        ClassificacaoLog.objects.create(
            processo=processo, classificacao=cat, score=score, versao=versao_em_uso,
            features_snapshot={k: round(v, 4) for k, v in features.items()},
        )

    # Shadow mode (T19): aplica versões shadow em job async com sample rate
    # configurado. Fora do hot path — falha de enqueue é ignorável (debug log).
    _maybe_enfileirar_shadow(processo.pk)

    return cat, score


# ============================================================================
# Shadow mode (T19) — aplica versões shadow=True em paralelo, sem afetar
# Process.classificacao oficial. Resultados em ClassificacaoShadowLog.
# ============================================================================

def _categorizar(score: float, features: dict, tribunal_id: int | None = None,
                 versao_modelo: str | None = None) -> str:
    """Aplica a hierarquia de categorização N1/N2/N3/NAO_LEAD em um score.

    Mesma lógica de `classificar()` (pra que a comparação shadow x ativa seja
    legítima — só o score muda entre versões). Se `tribunal_id` informado e
    existir `ThresholdTribunal` ativo, usa thresholds do DB; senão defaults.
    """
    from .models import Process, ThresholdTribunal

    t_prec = THRESHOLD_PRECATORIO
    t_pre = THRESHOLD_PRE_PRECATORIO
    t_dc = THRESHOLD_DIREITO_CREDITORIO

    if tribunal_id is not None:
        try:
            # Filtra por versao_modelo quando informado — protege durante a
            # transição v6→v7 onde podem coexistir thresholds de 2 versões.
            qs = ThresholdTribunal.objects.filter(
                tribunal_id=tribunal_id, ativo=True,
            )
            if versao_modelo is None:
                versao_modelo = get_versao_ativa()
            if versao_modelo:
                qs = qs.filter(versao_modelo=versao_modelo)
            row = qs.only(
                'threshold_precatorio', 'threshold_pre', 'threshold_dc',
            ).first()
            if row is not None:
                t_prec = row.threshold_precatorio
                t_pre = row.threshold_pre
                t_dc = row.threshold_dc
        except Exception:
            # Qualquer erro lendo thresholds: fallback silencioso pros defaults.
            pass

    is_cumprim = features.get('F1_cumprim', 0) == 1
    has_precat_explicit = (features.get('F2_precat_tc', 0) == 1
                           or features.get('F11_precat_text', 0) == 1)

    if score >= t_prec and has_precat_explicit:
        return Process.CLASSIF_PRECATORIO
    if score >= t_pre and is_cumprim:
        return Process.CLASSIF_PRE_PRECATORIO
    if score >= t_dc and is_cumprim:
        cat = Process.CLASSIF_DIREITO_CREDITORIO
        if (features.get('F13_reqPag_text', 0) == 1
                and features.get('F19_cancelado_ANTI', 0) == 0):
            cat = Process.CLASSIF_PRE_PRECATORIO
        return cat
    return Process.CLASSIF_NAO_LEAD


def classificar_shadow(processo) -> int:
    """Aplica TODAS ClassificadorVersao(shadow=True) e grava ClassificacaoShadowLog.

    Não toca em Process.classificacao nem em ClassificacaoLog. Idempotente
    no sentido de "rodar 2x cria 2 conjuntos de rows" — não dedup. O job de
    comparação trabalha com a versão mais recente por processo.

    Retorna o número de rows criadas.
    """
    from .models import ClassificacaoShadowLog, ClassificadorVersao

    try:
        shadow_versoes = list(
            ClassificadorVersao.objects.filter(shadow=True).only('versao', 'pesos')
        )
    except Exception as e:
        logger.warning('shadow: erro lendo ClassificadorVersao: %s', e)
        return 0

    if not shadow_versoes:
        return 0

    try:
        features = compute_features(processo)
    except Exception as e:
        logger.warning(
            'shadow: erro extraindo features pra processo %s: %s', processo.pk, e,
        )
        return 0

    tribunal_id = getattr(processo, 'tribunal_id', None)
    logs = []
    for sv in shadow_versoes:
        try:
            if not _validate_pesos(sv.pesos):
                logger.warning(
                    'shadow: pesos da versão %s corrompidos; pulando', sv.versao,
                )
                continue
            score = predict_score(features, pesos=sv.pesos)
            cat = _categorizar(score, features, tribunal_id=tribunal_id)
            logs.append(ClassificacaoShadowLog(
                processo=processo,
                versao_shadow=sv.versao,
                score=score,
                categoria=cat,
            ))
        except Exception:
            logger.exception(
                'shadow: erro classificando processo %s com versão %s',
                processo.pk, sv.versao,
            )

    if logs:
        try:
            ClassificacaoShadowLog.objects.bulk_create(logs)
        except Exception as e:
            logger.warning('shadow: bulk_create falhou: %s', e)
            return 0
    return len(logs)


def _maybe_enfileirar_shadow(processo_id: int) -> None:
    """Hook de sample-rate chamado dentro de `classificar_e_persistir`.

    Fora do hot path — falha de enqueue (Redis fora, RQ não montado, etc) é
    ignorável e logada em debug. Import local pra evitar ciclo (jobs.py
    importa de classificador.py).
    """
    import random  # noqa: PLC0415
    sample_rate = float(getattr(settings, 'SHADOW_SAMPLE_RATE', 0.0) or 0.0)
    if sample_rate <= 0.0:
        return
    if sample_rate < 1.0 and random.random() >= sample_rate:
        return
    try:
        from .jobs import classificar_shadow_async  # noqa: PLC0415
        classificar_shadow_async.delay(processo_id)
    except Exception as e:
        logger.debug('shadow: enqueue assíncrono falhou (ignorável): %s', e)
