"""Classificador de ESTÁGIO DO CRÉDITO — lib de predição (estagio_v1).

Primo do classificador de leads (v6/v7), mas com alvo diferente: em vez de
"é lead?", responde **em que estágio o crédito está**, usando SÓ dado público
(cadastro + movimentações + partes):

  DC       — direito creditório (sentença/acórdão/trânsito, sem cumprimento maduro)
  PRE      — pré-precatório (cumprimento/homologação, sem requisitório)
  EMITIDO  — precatório/RPV requisitado
  MORTO    — crédito encerrado (pago integral OU improcedência/prescrição)

Treino: GBM LightGBM multi-classe com rótulos de supervisão cruzada
(autos/Falcon — ver `scripts/estagio/build_labels.py` e `.ia/ESTAGIO_CREDITO.md`).
As features daqui DEVEM permanecer em sincronia com
`scripts/estagio/build_features.py` (mesmo SQL, mesma ordem — o bundle carrega
a lista `features` e este módulo monta o vetor a partir dela).

Uso:
    from tribunals.estagio import predict_estagio
    predict_estagio('0001234-56.2020.4.01.3800')
    → {'classe': 'EMITIDO', 'proba': {...}, 'sinais': [...],
       'valor_homologado': 12345.67, 'partes': ['FULANO'], 'versao': 'estagio_v1'}

Modelo: bundle joblib em `settings.ESTAGIO_MODEL_PATH` (default
`<BASE_DIR>/models/estagio_gbm_v1.joblib`). Pesos NÃO são versionados no git —
md5 no registry `.ia/MODELOS.md`. Sem o artefato, `predict_estagio` levanta
`EstagioIndisponivel` (fail-closed; nenhum fallback de chute).
"""
from __future__ import annotations

import logging
import math
import re
import threading
from datetime import date, datetime

from django.conf import settings
from django.db import connection

logger = logging.getLogger('voyager.tribunals.estagio')

VERSAO = 'estagio_v1'
CLASSES = ['DC', 'PRE', 'EMITIDO', 'MORTO']

# --- catálogos públicos (mesmos do classificador v6/v7) ----------------------
CLASSES_CUMPRIMENTO = {'12078', '156', '15160', '15215', '12079'}
CLASSES_FAZENDA_PUBLICA = {'12078', '12079', '15215'}
RE_JUIZADO = re.compile(r'juizado especial|recurso inominado|procedimento comum', re.I)
RE_ENTE_PUBLICO = re.compile(
    r'\b(uniao|união|municipio|município|estado d|fazenda|inss|instituto nacional'
    r'|autarquia|prefeitura|df|distrito federal|funda[çc][ãa]o p[úu]blica)\b', re.I)
RE_VALOR_BRL = re.compile(r'R\$\s*([\d.]{1,15},\d{2})')

# Agregado de movimentações públicas — MANTER EM SINCRONIA com
# scripts/estagio/build_features.py::MOVS_AGG_SQL (é o mesmo SQL; qualquer
# mudança precisa regenerar dataset + retreinar).
MOVS_AGG_SQL = """
    SELECT
        COUNT(*) AS total_movs,
        COUNT(DISTINCT CASE WHEN tipo_comunicacao <> '' THEN tipo_comunicacao END) AS distinct_tipos,
        COALESCE(SUM(CASE WHEN tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                          THEN 1 ELSE 0 END), 0) AS exped_tc_n,
        COALESCE(SUM(CASE WHEN texto ~* 'precat[óo]rio'                 THEN 1 ELSE 0 END), 0) AS precat_text_n,
        COALESCE(SUM(CASE WHEN texto ~* '\\mrpv\\M|requisi[çc][ãa]o de pequeno valor'
                          THEN 1 ELSE 0 END), 0) AS rpv_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'requisi[çc][ãa]o de pagamento' THEN 1 ELSE 0 END), 0) AS reqpag_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'of[íi]cio requisit[óo]rio'     THEN 1 ELSE 0 END), 0) AS oficio_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                          THEN 1 ELSE 0 END), 0) AS exped_forte_n,
        COALESCE(SUM(CASE WHEN texto ~* 'cancelamento de precat[óo]rio|cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|revoga[çc][ãa]o de rpv'
                          THEN 1 ELSE 0 END), 0) AS cancel_n,
        COALESCE(SUM(CASE WHEN texto ~* 'tr[âa]nsito em julgado|transitad[oa] em julgado'
                          THEN 1 ELSE 0 END), 0) AS transito_n,
        COALESCE(SUM(CASE WHEN texto ~* 'homologo|homolga|homologa[çc][ãa]o' AND texto ~* 'c[áa]lculo|valor|conta'
                          THEN 1 ELSE 0 END), 0) AS homolog_n,
        COALESCE(SUM(CASE WHEN texto ~* 'cumprimento de senten[çc]a'    THEN 1 ELSE 0 END), 0) AS cumpr_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'alvar[áa]\\s+(judicial|de\\s+levantamento)|expe[çc]am?-se\\s+(o\\s+)?alvar[áa]|autorizo[^.]{0,150}sequestro|defiro[^.]{0,100}sequestro|sequestro\\s+do\\s+numer[áa]rio|mandado\\s+de\\s+levantamento'
                          THEN 1 ELSE 0 END), 0) AS pago_n,
        COALESCE(SUM(CASE WHEN texto ~* 'extin[çc][ãa]o pel[oa] pagamento|julgo\\s+extint[oa][^.]{0,160}(pagamento|satisfa[çc][ãa]o da obriga[çc][ãa]o|cumprimento da obriga[çc][ãa]o)|art[^0-9]{0,8}924[^.]{0,15}(inciso\\s+)?II'
                          THEN 1 ELSE 0 END), 0) AS ext_satisf_n,
        COALESCE(SUM(CASE WHEN texto ~* 'sem\\s+resolu[çc][ãa]o\\s+d[eo]\\s+m[ée]rito|art[^0-9]{0,8}485|indefer\\w+\\s+a\\s+(peti[çc][ãa]o|inicial|exordial)'
                          THEN 1 ELSE 0 END), 0) AS ext_semmerito_n,
        COALESCE(SUM(CASE WHEN texto ~* 'improceden|(decreto|reconhe[çc]o|pronuncio)\\s+a\\s+prescri|prescri[çc][ãa]o\\s+intercorrente|nego\\s+provimento'
                          THEN 1 ELSE 0 END), 0) AS improc_n,
        MIN(data_disponibilizacao) AS mov_min_dt,
        MAX(data_disponibilizacao) AS mov_max_dt,
        MIN(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                 OR tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                 THEN data_disponibilizacao END) AS exped_min_dt,
        MAX(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                 OR tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                 THEN data_disponibilizacao END) AS exped_max_dt,
        MIN(CASE WHEN texto ~* 'tr[âa]nsito em julgado|transitad[oa] em julgado'
                 THEN data_disponibilizacao END) AS transito_min_dt,
        MAX(CASE WHEN texto ~* 'homologo|homologa[çc][ãa]o' AND texto ~* 'c[áa]lculo|valor|conta'
                 THEN data_disponibilizacao END) AS homolog_max_dt,
        MAX(CASE WHEN texto ~* 'alvar[áa]\\s+(judicial|de\\s+levantamento)|expe[çc]am?-se\\s+(o\\s+)?alvar[áa]|autorizo[^.]{0,150}sequestro|defiro[^.]{0,100}sequestro|sequestro\\s+do\\s+numer[áa]rio|mandado\\s+de\\s+levantamento'
                 THEN data_disponibilizacao END) AS pago_max_dt,
        MAX(CASE WHEN texto ~* 'improceden|(decreto|reconhe[çc]o|pronuncio)\\s+a\\s+prescri|sem\\s+resolu[çc][ãa]o\\s+d[eo]\\s+m[ée]rito|julgo\\s+extint'
                 THEN data_disponibilizacao END) AS extneg_max_dt
    FROM tribunals_movimentacao
    WHERE processo_id = %s
"""

VALOR_HOMOLOG_SQL = """
    SELECT texto FROM tribunals_movimentacao
    WHERE processo_id = %s
      AND texto ~* 'homolog'
      AND texto ~* 'R\\$'
    ORDER BY data_disponibilizacao DESC
    LIMIT 1
"""

PARTES_SQL = """
    SELECT pa.nome, pp.polo, pp.papel
    FROM tribunals_processoparte pp
    JOIN tribunals_parte pa ON pa.id = pp.parte_id
    WHERE pp.processo_id = %s AND pp.representa_id IS NULL
    LIMIT 60
"""


class EstagioIndisponivel(RuntimeError):  # noqa: N818 — nome pt-BR idiomático
    """Modelo de estágio não carregável (artefato ausente/corrompido)."""


# --- bundle cache (lazy, thread-safe) ---------------------------------------
_BUNDLE_CACHE: dict = {'bundle': None, 'booster': None}
_BUNDLE_LOCK = threading.Lock()


def _model_path() -> str:
    import os  # noqa: PLC0415
    default = os.path.join(str(settings.BASE_DIR), 'models', 'estagio_gbm_v1.joblib')
    return getattr(settings, 'ESTAGIO_MODEL_PATH', default)


def _load_bundle():
    if _BUNDLE_CACHE['booster'] is not None:
        return _BUNDLE_CACHE['bundle'], _BUNDLE_CACHE['booster']
    with _BUNDLE_LOCK:
        if _BUNDLE_CACHE['booster'] is not None:
            return _BUNDLE_CACHE['bundle'], _BUNDLE_CACHE['booster']
        try:
            import joblib  # noqa: PLC0415
            import lightgbm as lgb  # noqa: PLC0415
        except ImportError as e:
            raise EstagioIndisponivel(f'dependência ausente: {e}') from e
        path = _model_path()
        try:
            bundle = joblib.load(path)
            booster = lgb.Booster(model_str=bundle['booster_str'])
        except Exception as e:
            raise EstagioIndisponivel(f'falha carregando {path}: {e}') from e
        _BUNDLE_CACHE.update(bundle=bundle, booster=booster)
        logger.info('estagio: bundle %s carregado de %s', bundle.get('versao'), path)
        return bundle, booster


def reset_bundle_cache() -> None:
    """Descarrega o modelo (testes / troca de artefato)."""
    with _BUNDLE_LOCK:
        _BUNDLE_CACHE.update(bundle=None, booster=None)


# --- feature extraction ------------------------------------------------------

def _dias(a, b) -> float | None:
    if a is None or b is None:
        return None
    if isinstance(a, datetime):
        a = a.date()
    if isinstance(b, datetime):
        b = b.date()
    return float((b - a).days)


def computar_features_publicas(processo, snapshot: date | None = None) -> tuple[dict, dict]:  # noqa: PLR0912
    """Extrai as features 100% públicas + extras informativos de um Process.

    Retorna (features, extras): `features` alimenta o GBM; `extras` carrega
    valor_homologado, partes beneficiárias e os sinais legíveis.
    """
    snapshot = snapshot or date.today()
    classe_cod = processo.classe_codigo or ''
    classe_nome = processo.classe_nome or ''

    with connection.cursor() as cur:
        cur.execute(MOVS_AGG_SQL, [processo.pk])
        m = cur.fetchone()
        (total_movs, distinct_tipos, exped_tc_n, precat_text_n, rpv_text_n,
         reqpag_text_n, oficio_text_n, exped_forte_n, cancel_n, transito_n,
         homolog_n, cumpr_text_n, pago_n, ext_satisf_n, ext_semmerito_n,
         improc_n, mov_min_dt, mov_max_dt, exped_min_dt, exped_max_dt,
         transito_min_dt, homolog_max_dt, pago_max_dt, extneg_max_dt) = m

        valor_homolog = None
        if total_movs and homolog_n:
            cur.execute(VALOR_HOMOLOG_SQL, [processo.pk])
            r = cur.fetchone()
            if r and r[0]:
                mv = RE_VALOR_BRL.search(r[0])
                if mv:
                    try:
                        valor_homolog = float(
                            mv.group(1).replace('.', '').replace(',', '.'))
                    except ValueError:
                        valor_homolog = None

        n_partes = 0
        tem_ente = 0
        beneficiarias: list[str] = []
        cur.execute(PARTES_SQL, [processo.pk])
        for nome, polo, papel in cur.fetchall():
            n_partes += 1
            if polo == 'passivo' and RE_ENTE_PUBLICO.search(nome or ''):
                tem_ente = 1
            if polo == 'ativo' and len(beneficiarias) < 5 \
                    and not re.search(r'advogad', (papel or ''), re.I):
                beneficiarias.append(nome)

    duracao = _dias(mov_min_dt, mov_max_dt)
    features = {
        'ano_cnj': processo.ano_cnj or 0,
        'is_cumprimento': int(classe_cod in CLASSES_CUMPRIMENTO),
        'is_fazenda': int(classe_cod in CLASSES_FAZENDA_PUBLICA),
        'is_juizado_anti': int(bool(RE_JUIZADO.search(classe_nome))),
        'dias_autuacao': _dias(processo.data_autuacao, snapshot),
        'total_movs': total_movs or 0,
        'distinct_tipos': distinct_tipos or 0,
        'exped_tc_n': exped_tc_n, 'precat_text_n': precat_text_n,
        'rpv_text_n': rpv_text_n, 'reqpag_text_n': reqpag_text_n,
        'oficio_text_n': oficio_text_n, 'exped_forte_n': exped_forte_n,
        'cancel_n': cancel_n, 'transito_n': transito_n, 'homolog_n': homolog_n,
        'cumpr_text_n': cumpr_text_n, 'pago_n': pago_n,
        'ext_satisf_n': ext_satisf_n, 'ext_semmerito_n': ext_semmerito_n,
        'improc_n': improc_n,
        'dias_ult_mov': _dias(mov_max_dt, snapshot),
        'duracao_dias': duracao,
        'movs_por_ano': round((total_movs or 0) / max((duracao or 0) / 365.25, 0.1), 3),
        'tem_exped': int(exped_max_dt is not None),
        'dias_desde_exped': _dias(exped_max_dt, snapshot),
        'dias_transito_a_exped': _dias(transito_min_dt, exped_min_dt),
        'tem_transito': int(transito_min_dt is not None),
        'dias_desde_transito': _dias(transito_min_dt, snapshot),
        'tem_homolog': int(homolog_max_dt is not None),
        'dias_desde_homolog': _dias(homolog_max_dt, snapshot),
        'tem_pago': int(pago_max_dt is not None),
        'dias_desde_pago': _dias(pago_max_dt, snapshot),
        'pago_pos_exped': int(bool(pago_max_dt)
                              and (exped_max_dt is None or pago_max_dt >= exped_max_dt)),
        'extneg_pos_exped': int(bool(extneg_max_dt)
                                and (exped_max_dt is None or extneg_max_dt >= exped_max_dt)),
        'n_partes': n_partes,
        'tem_ente_publico_passivo': tem_ente,
        'log_valor_homologado': (math.log1p(valor_homolog)
                                 if valor_homolog is not None else None),
        # categóricas (mapeadas pra código do bundle na predição)
        'tribunal': processo.tribunal_id,
        'classe_codigo': classe_cod,
        'assunto_codigo': processo.assunto_codigo or '',
    }

    sinais: list[str] = []
    if exped_tc_n or exped_forte_n:
        sinais.append(
            f'expedição de precatório/RPV nas movs públicas '
            f'(tipo_com={exped_tc_n}, texto={exped_forte_n}, '
            f'última={exped_max_dt.date() if exped_max_dt else "?"})')
    if rpv_text_n:
        sinais.append(f'menção a RPV/pequeno valor (n={rpv_text_n})')
    if transito_n:
        sinais.append(f'trânsito em julgado no texto (n={transito_n}, '
                      f'primeira={transito_min_dt.date() if transito_min_dt else "?"})')
    if homolog_n:
        sinais.append(f'homologação de cálculos (n={homolog_n})')
    if cumpr_text_n or classe_cod in CLASSES_CUMPRIMENTO:
        sinais.append(f'cumprimento de sentença (classe={classe_cod or "—"}, '
                      f'texto n={cumpr_text_n})')
    if pago_n:
        sinais.append(f'pagamento/alvará/sequestro (n={pago_n}, '
                      f'último={pago_max_dt.date() if pago_max_dt else "?"})')
    if ext_satisf_n:
        sinais.append(f'extinção pelo pagamento no texto (n={ext_satisf_n})')
    if ext_semmerito_n:
        sinais.append(f'extinção sem mérito no texto (n={ext_semmerito_n})')
    if improc_n:
        sinais.append(f'improcedência/prescrição no texto (n={improc_n})')
    if cancel_n:
        sinais.append(f'cancelamento/revogação de requisitório (n={cancel_n})')

    extras = {
        'sinais': sinais,
        'valor_homologado': valor_homolog,
        'partes': beneficiarias or None,
        'total_movs': total_movs or 0,
    }
    return features, extras


def _vetor(features: dict, bundle: dict) -> list[float]:
    """Monta o vetor na ordem do bundle; categóricas → código (unseen = -1)."""
    nan = float('nan')
    row: list[float] = []
    cat_maps = bundle['cat_maps']
    for f in bundle['features']:
        v = features.get(f)
        if f in cat_maps:
            try:
                row.append(float(cat_maps[f].index('' if v is None else str(v))))
            except ValueError:
                row.append(-1.0)
        else:
            row.append(nan if v is None else float(v))
    return row


def predict_estagio(numero_cnj: str, tribunal: str | None = None) -> dict:
    """Prediz o estágio do crédito de um processo pelo CNJ.

    Retorna {numero_cnj, tribunal, classe, proba{classe: p}, sinais[],
    valor_homologado, partes, total_movs, versao}. Levanta `Process.DoesNotExist`
    se o CNJ não existe no Voyager e `EstagioIndisponivel` sem o artefato.
    """
    from .models import Process  # noqa: PLC0415

    qs = Process.objects.filter(numero_cnj=numero_cnj)
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    processo = qs.first()
    if processo is None:
        raise Process.DoesNotExist(f'CNJ {numero_cnj} não encontrado no Voyager')
    return predict_estagio_processo(processo)


def predict_estagio_processo(processo) -> dict:
    """Variante que recebe a instância `Process` (evita query extra)."""
    import numpy as np  # noqa: PLC0415

    bundle, booster = _load_bundle()
    features, extras = computar_features_publicas(processo)
    x = np.asarray([_vetor(features, bundle)])
    proba = booster.predict(x)[0]
    classes = bundle['classes']
    idx = int(proba.argmax())
    # Guarda de precisão (calibrada no treino, espelha o gate): MORTO só acima
    # do threshold do bundle; abaixo, rebaixa pro melhor não-MORTO (demote-only).
    thr_morto = (bundle.get('thresholds') or {}).get('MORTO')
    if thr_morto is not None and classes[idx] == 'MORTO' \
            and float(proba[idx]) < float(thr_morto):
        resto = proba.copy()
        resto[idx] = -1.0
        idx = int(resto.argmax())
    return {
        'numero_cnj': processo.numero_cnj,
        'tribunal': processo.tribunal_id,
        'classe': classes[idx],
        'confianca': round(float(proba[idx]), 4),
        'proba': {c: round(float(p), 4) for c, p in zip(classes, proba, strict=True)},
        'sinais': extras['sinais'],
        'valor_homologado': extras['valor_homologado'],
        'partes': extras['partes'],
        'total_movs': extras['total_movs'],
        'versao': bundle.get('versao', VERSAO),
    }
