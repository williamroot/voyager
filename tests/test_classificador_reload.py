"""Testes do hot reload de pesos do classificador (T17).

Cobre:
- Reload básico: DB atualiza pesos → próxima classificação reflete (após TTL).
- Fallback quando DB lança exceção.
- Validação rejeita pesos com features faltando → fallback hardcoded.
- TTL: 2 classificações dentro do TTL fazem só 1 query.
- force_reload_weights() pula TTL e recarrega imediatamente.
- Logging: troca de versão emite INFO 'classifier reloaded'.
- Concorrência: N threads classificando enquanto outro thread troca pesos —
  sem crash, sem deadlock, todas as classificações terminam.
"""
from __future__ import annotations

import logging
import threading
import time
from unittest.mock import patch

import pytest
from django.db import DatabaseError

from tribunals import classificador as clf
from tribunals.classificador import (
    _WEIGHTS_CACHE,
    _WEIGHTS_LOCK,
    HARDCODED_WEIGHTS,
    _maybe_reload_weights,
    classificar,
    force_reload_weights,
    get_versao_ativa,
    predict_score,
)
from tribunals.models import ClassificadorVersao, Process, Tribunal

pytestmark = pytest.mark.django_db


# ---------- helpers ----------

def _reset_cache():
    """Reseta o cache do classificador pra estado de boot (vazio + vencido)."""
    with _WEIGHTS_LOCK:
        _WEIGHTS_CACHE.update(
            versao=None,
            pesos=None,
            thresholds=None,
            normas=None,
            loaded_at=0.0,
        )


@pytest.fixture(autouse=True)
def cache_clean():
    """Garante isolamento entre testes — cada teste começa com cache vazio."""
    _reset_cache()
    # Limpa registros prévios de versão pra cada teste — testes anteriores
    # podem deixar versões ativas residuais.
    ClassificadorVersao.objects.all().delete()
    # Habilita propagação do logger 'voyager' pra caplog conseguir capturar.
    # Em produção o logger tem propagate=False (configurado em settings.LOGGING)
    # pra evitar duplicate output; mas em pytest precisamos do caplog, que
    # consome via root logger.
    voyager_logger = logging.getLogger('voyager')
    propagate_original = voyager_logger.propagate
    voyager_logger.propagate = True
    yield
    voyager_logger.propagate = propagate_original
    _reset_cache()
    ClassificadorVersao.objects.all().delete()


@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True},
    )
    return t


@pytest.fixture
def proc_precatorio(trf1):
    """Processo que com pesos hardcoded vira PRECATORIO ou PRE_PRECATORIO.

    Estratégia: classe Cumprimento (F1=1) + ano 2023 (próximo da média).
    Features computadas serão baixas exceto F1 + F18 — score depende
    inteiramente do peso de F1 e F1xF15. Vamos validar manualmente via
    `classificar()` sem precisar de movimentações reais.
    """
    return Process.objects.create(
        tribunal=trf1,
        numero_cnj='0000001-23.2023.4.01.0000',
        classe_codigo='12078',  # Cumprimento contra Fazenda
        classe_nome='Cumprimento de Sentença contra a Fazenda Pública',
    )


def _make_versao(versao: str, pesos: dict, ativa: bool = True, **kw) -> ClassificadorVersao:
    if ativa:
        ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    return ClassificadorVersao.objects.create(
        versao=versao, pesos=pesos, ativa=ativa, **kw,
    )


# ---------- 1. Reload básico ----------

def test_reload_pega_pesos_do_db(proc_precatorio):
    """Quando há ClassificadorVersao ativa, classificar usa esses pesos.

    Comparativo: pesos zerados (todos=0, intercept=-20) devem produzir score
    bem menor que pesos hardcoded normais pro mesmo processo.
    """
    # Baseline: pesos hardcoded normais.
    _make_versao('v6-norm', dict(HARDCODED_WEIGHTS), ativa=True)
    force_reload_weights()
    _cat_baseline, score_baseline, _ = classificar(proc_precatorio)

    # Troca pra pesos quase nulos.
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    pesos_zerados = dict.fromkeys(HARDCODED_WEIGHTS, 0.0)
    pesos_zerados['_intercept_'] = -20.0  # sigmoide(-20) ~ 2e-9
    _make_versao('v6-zero', pesos_zerados, ativa=True)
    force_reload_weights()
    assert get_versao_ativa() == 'v6-zero'

    cat, score, _ = classificar(proc_precatorio)
    # Score deve ser drasticamente menor (com intercept=-20, todos pesos=0
    # → z = -20 → sigmoid ≈ 0).
    assert score < 1e-6
    assert score < score_baseline
    assert cat == Process.CLASSIF_NAO_LEAD


def test_troca_versao_no_db_reflete_apos_force_reload(proc_precatorio):
    """Troca dinâmica: muda pesos no DB, força reload, próxima classif usa novos."""
    # Versão 1 — pesos hardcoded.
    _make_versao('v6-base', dict(HARDCODED_WEIGHTS), ativa=True)
    force_reload_weights()
    _cat1, score1, _feats1 = classificar(proc_precatorio)

    # Troca versão ativa.
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    pesos_v7 = dict(HARDCODED_WEIGHTS)
    pesos_v7['_intercept_'] = 10.0  # vira tudo PRECATORIO
    ClassificadorVersao.objects.create(
        versao='v7-hi', pesos=pesos_v7, ativa=True,
    )

    force_reload_weights()
    assert get_versao_ativa() == 'v7-hi'

    cat2, score2, _feats2 = classificar(proc_precatorio)
    # Features iguais (mesmo processo), mas score deve subir drasticamente.
    assert score2 > score1
    # Não precisa virar PRECATORIO necessariamente (depende de F2/F11) — mas
    # ao menos PRE_PRECATORIO (cumprim + score > 0.4).
    assert cat2 in (Process.CLASSIF_PRECATORIO,
                    Process.CLASSIF_PRE_PRECATORIO,
                    Process.CLASSIF_DIREITO_CREDITORIO)


# ---------- 2. Fallback DB down ----------

def test_fallback_quando_db_lanca_database_error(proc_precatorio):
    """Se ClassificadorVersao.objects.filter lança DatabaseError, cai pra
    hardcoded e classifica normalmente."""
    _reset_cache()

    with patch(
        'tribunals.models.ClassificadorVersao.objects',
    ) as mock_objs:
        mock_objs.filter.side_effect = DatabaseError('connection refused')
        # Não deve levantar — classificador absorve.
        _maybe_reload_weights()

    # Cache foi populado com hardcoded.
    assert _WEIGHTS_CACHE['versao'] == 'hardcoded'
    assert _WEIGHTS_CACHE['pesos'] == HARDCODED_WEIGHTS

    # E classificar funciona.
    cat, score, _ = classificar(proc_precatorio)
    assert cat in {
        Process.CLASSIF_NAO_LEAD, Process.CLASSIF_DIREITO_CREDITORIO,
        Process.CLASSIF_PRE_PRECATORIO, Process.CLASSIF_PRECATORIO,
    }
    assert 0.0 <= score <= 1.0


def test_fallback_preserva_pesos_anteriores_em_falha_apos_carga_ok(proc_precatorio):
    """Se já tinha pesos bons em cache e DB cai, mantém os bons (não rebaixa pra
    hardcoded perdendo o ajuste fino)."""
    pesos_custom = dict(HARDCODED_WEIGHTS)
    pesos_custom['_intercept_'] = 5.0
    _make_versao('v6-cust', pesos_custom, ativa=True)
    force_reload_weights()
    assert get_versao_ativa() == 'v6-cust'

    # Expira o cache e força reload com DB quebrado.
    with _WEIGHTS_LOCK:
        _WEIGHTS_CACHE['loaded_at'] = 0.0

    with patch(
        'tribunals.models.ClassificadorVersao.objects',
    ) as mock_objs:
        mock_objs.filter.side_effect = DatabaseError('boom')
        _maybe_reload_weights()

    # Manteve a versão anterior em cache (não regrediu pra hardcoded).
    assert _WEIGHTS_CACHE['versao'] == 'v6-cust'
    assert _WEIGHTS_CACHE['pesos']['_intercept_'] == 5.0


# ---------- 3. Validação de pesos corrompidos ----------

def test_pesos_corrompidos_caem_para_hardcoded(caplog):
    """Versão ativa com pesos faltando features → fallback + warning."""
    _make_versao('v6-brk', {'F1_cumprim': 1.0}, ativa=True)

    with caplog.at_level(logging.WARNING, logger='voyager.tribunals.classificador'):
        force_reload_weights()

    assert _WEIGHTS_CACHE['versao'] == 'hardcoded'
    assert _WEIGHTS_CACHE['pesos'] == HARDCODED_WEIGHTS
    assert any('corrompidos' in rec.message for rec in caplog.records)


def test_pesos_superset_aceito_com_features_extras(caplog):
    """v7 com F24/F25 além das v6: validação passa, predict ignora as extras,
    warning informa."""
    pesos_v7 = dict(HARDCODED_WEIGHTS)
    pesos_v7['F24_nova'] = 0.5
    pesos_v7['F25_outra'] = -0.3
    _make_versao('v7-x', pesos_v7, ativa=True)

    with caplog.at_level(logging.WARNING, logger='voyager.tribunals.classificador'):
        force_reload_weights()

    assert _WEIGHTS_CACHE['versao'] == 'v7-x'
    assert _WEIGHTS_CACHE['pesos']['F24_nova'] == 0.5
    assert any('features extras' in rec.message for rec in caplog.records)

    # Predict não quebra ao encontrar pesos sem feature correspondente
    # (multiplicação por 0 implícita — só as features presentes contribuem).
    feats = {'F1_cumprim': 1, 'F15_logMovs': 0.5}
    score = predict_score(feats)
    assert 0.0 <= score <= 1.0


# ---------- 4. TTL: cache evita re-query dentro da janela ----------

def test_ttl_evita_query_dentro_da_janela(proc_precatorio, settings):
    """Duas classificações em sequência fazem 1 SELECT em ClassificadorVersao,
    não 2."""
    settings.CLASSIFICADOR_RELOAD_TTL = 60
    _make_versao('v6-ttl', dict(HARDCODED_WEIGHTS), ativa=True)

    # Primeira chamada popula cache.
    force_reload_weights()
    call_count = {'n': 0}
    real_first = ClassificadorVersao.objects.filter

    def counted_filter(*a, **kw):
        call_count['n'] += 1
        return real_first(*a, **kw)

    with patch(
        'tribunals.models.ClassificadorVersao.objects.filter',
        side_effect=counted_filter,
    ):
        # Várias chamadas dentro do TTL — não deve consultar DB.
        for _ in range(5):
            _maybe_reload_weights()

    assert call_count['n'] == 0, (
        f'Esperava 0 queries dentro do TTL, fez {call_count["n"]}'
    )


def test_ttl_expirado_recarrega(proc_precatorio, settings):
    """Quando passamos do TTL, próxima chamada recarrega."""
    settings.CLASSIFICADOR_RELOAD_TTL = 1
    _make_versao('v6-exp', dict(HARDCODED_WEIGHTS), ativa=True)

    force_reload_weights()
    loaded_at_inicial = _WEIGHTS_CACHE['loaded_at']

    # Empurra o relógio do cache pra trás (TTL=1s, manipulamos o registro)
    # — equivalente a "passou >1s sem chamar".
    with _WEIGHTS_LOCK:
        _WEIGHTS_CACHE['loaded_at'] = loaded_at_inicial - 2.0

    _maybe_reload_weights()
    # Reload aconteceu → loaded_at virou tempo atual.
    assert _WEIGHTS_CACHE['loaded_at'] > loaded_at_inicial - 2.0


# ---------- 5. force_reload_weights pula TTL ----------

def test_force_reload_pula_ttl():
    """force_reload_weights() pega versão nova imediatamente, mesmo dentro do TTL."""
    _make_versao('vA', dict(HARDCODED_WEIGHTS), ativa=True)
    force_reload_weights()
    assert get_versao_ativa() == 'vA'

    # Troca versão (dentro do TTL — não recarregaria sozinho).
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    _make_versao('vB', dict(HARDCODED_WEIGHTS), ativa=True)

    # Sem force, get_versao_ativa retornaria 'vA' (cache fresco).
    # Com force, pega 'vB' agora.
    force_reload_weights()
    assert get_versao_ativa() == 'vB'


# ---------- 6. Logging ----------

def test_log_info_quando_troca_versao(caplog):
    """Trocar versão emite INFO 'classifier reloaded: X -> Y'."""
    _make_versao('v6-old', dict(HARDCODED_WEIGHTS), ativa=True)

    with caplog.at_level(logging.INFO, logger='voyager.tribunals.classificador'):
        force_reload_weights()
        # Troca pra outra.
        ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
        _make_versao('v6-new', dict(HARDCODED_WEIGHTS), ativa=True)
        force_reload_weights()

    msgs = [rec.message for rec in caplog.records]
    assert any('classifier reloaded' in m and 'v6-old' in m and 'v6-new' in m
               for m in msgs), f'Logs capturados: {msgs}'


def test_log_warning_sem_versao_ativa(caplog):
    """Sem nenhuma ClassificadorVersao ativa, emite warning na primeira carga."""
    assert not ClassificadorVersao.objects.filter(ativa=True).exists()

    with caplog.at_level(logging.WARNING, logger='voyager.tribunals.classificador'):
        force_reload_weights()

    assert _WEIGHTS_CACHE['versao'] == 'hardcoded'
    assert any('Nenhuma ClassificadorVersao' in rec.message for rec in caplog.records)


# ---------- 7. Concorrência ----------

def test_concorrencia_multiplas_threads_sem_crash():
    """N threads chamando `_current_weights()` enquanto outro thread força
    reloads a cada poucos ms. Verifica que o lock interno não deadlocka e
    que `predict_score` nunca lê um cache em estado intermediário.

    DB é mockado pra evitar problemas de visibilidade transacional de
    pytest-django entre threads (cada thread tem sua connection isolada
    com snapshot diferente).
    """
    # Lista de "versões disponíveis no DB" que o mock vai alternar.
    versoes_simuladas = []
    for i in range(10):
        pesos_v = dict(HARDCODED_WEIGHTS)
        pesos_v['_intercept_'] = float(i) - 5.0  # varia de -5 a +4

        class FakeAtiva:
            versao = f'v-c-{i}'
            pesos = pesos_v
            metricas: dict = {}
        FakeAtiva.versao = f'v-c-{i}'
        versoes_simuladas.append(FakeAtiva)

    cursor = {'i': 0}
    cursor_lock = threading.Lock()

    def fake_filter(*a, **kw):
        # Retorna um manager fake cuja `.only(...).first()` devolve a próxima
        # versão da lista.
        idx = cursor['i'] % len(versoes_simuladas)

        class FakeQS:
            @staticmethod
            def only(*a, **kw):
                return FakeQS

            @staticmethod
            def first():
                return versoes_simuladas[idx]

        return FakeQS

    errors: list[Exception] = []
    stop_flag = threading.Event()

    feats_fixos = {k: 0.5 for k in HARDCODED_WEIGHTS if k != '_intercept_'}

    def worker_predizer():
        try:
            for _ in range(200):
                if stop_flag.is_set():
                    return
                score = predict_score(feats_fixos)
                assert 0.0 <= score <= 1.0
        except Exception as e:
            errors.append(e)

    def worker_trocar():
        try:
            for i in range(20):
                if stop_flag.is_set():
                    return
                with cursor_lock:
                    cursor['i'] = i
                force_reload_weights()
                time.sleep(0.005)
        except Exception as e:
            errors.append(e)

    with patch(
        'tribunals.models.ClassificadorVersao.objects.filter',
        side_effect=fake_filter,
    ):
        threads = [threading.Thread(target=worker_predizer) for _ in range(10)]
        threads.append(threading.Thread(target=worker_trocar))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), 'Thread travou (possível deadlock no lock)'

    stop_flag.set()
    assert not errors, f'Threads tiveram exceções: {errors}'

    # Estado final consistente — versão atual está no conjunto simulado.
    versao_final = _WEIGHTS_CACHE['versao']
    assert versao_final.startswith('v-c-')


# ---------- 8. predict_score aceita pesos custom (compat de testes) ----------

def test_predict_score_aceita_pesos_explicitos():
    """API legacy: `predict_score(features)` sem pesos usa cache; com pesos
    explícitos usa eles. Garante que ferramentas internas (notebooks, scripts
    de re-treino) podem injetar pesos sem precisar mexer no cache global."""
    feats = {'F1_cumprim': 1, 'F15_logMovs': 0.5}
    pesos = {'_intercept_': 0.0, 'F1_cumprim': 10.0, 'F15_logMovs': 0.0}
    score = predict_score(feats, pesos=pesos)
    # z = 0 + 10*1 + 0 = 10 → sigmoid(10) ≈ 0.9999
    assert score > 0.999


# ---------- 9. Migration data backfill ----------

def test_migration_seed_v6_e_idempotente():
    """Re-rodar a função `seed_v6` da migration 0026 não cria duplicata nem
    quebra. Documenta a invariante de idempotência exigida no plano."""
    from importlib import import_module  # noqa: PLC0415

    from django.apps import apps  # noqa: PLC0415
    mod = import_module(
        'tribunals.migrations.0026_seed_classificador_versao_v6',
    )

    # Primeira execução: cria v6 ativa.
    mod.seed_v6(apps, schema_editor=None)
    v6 = ClassificadorVersao.objects.filter(versao='v6', ativa=True).first()
    assert v6 is not None
    assert v6.pesos == clf.HARDCODED_WEIGHTS
    assert v6.metricas['auc'] == 0.9610

    # Segunda execução: idempotente — count permanece 1.
    mod.seed_v6(apps, schema_editor=None)
    assert ClassificadorVersao.objects.filter(versao='v6').count() == 1
