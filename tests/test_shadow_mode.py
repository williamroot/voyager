"""Testes do shadow mode (T19) — infra de A/B test entre versão ativa e shadow.

Cobre:
- classificar_shadow com 0/1/N versões shadow.
- _categorizar usa ThresholdTribunal ativo quando informado, default caso contrário.
- classificar_e_persistir com SHADOW_SAMPLE_RATE=0 não enfileira shadow.
- classificar_e_persistir com SHADOW_SAMPLE_RATE=1 enfileira shadow.
- comparar_shadow com N logs produz relatório markdown válido.
- comparar_shadow com 0 logs retorna estatísticas vazias.
- shadow_status sem versão shadow retorna None.
- shadow_status com versão shadow retorna dict completo.
- chart_shadow_status endpoint responde 200 JSON.
- _ks_2samp produz estatística esperada.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from tribunals import classificador as clf
from tribunals.classificador import (
    HARDCODED_WEIGHTS,
    _categorizar,
    classificar_e_persistir,
    classificar_shadow,
    force_reload_weights,
)
from tribunals.jobs import (
    _confusion_matrix,
    _ks_2samp,
    classificar_shadow_async,
    comparar_shadow,
)
from tribunals.models import (
    ClassificacaoShadowLog,
    ClassificadorVersao,
    Process,
    ThresholdTribunal,
    Tribunal,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------- fixtures ----------

def _reset_cache():
    with clf._WEIGHTS_LOCK:
        clf._WEIGHTS_CACHE.update(
            versao=None, pesos=None, thresholds=None, normas=None, loaded_at=0.0,
        )


@pytest.fixture(autouse=True)
def _isolate_cache(settings):
    """Cada teste começa com cache limpo e classificador ativo conhecido."""
    settings.SHADOW_SAMPLE_RATE = 0.0  # default conservador
    _reset_cache()
    ClassificadorVersao.objects.all().delete()
    ClassificacaoShadowLog.objects.all().delete()
    yield
    _reset_cache()
    ClassificadorVersao.objects.all().delete()
    ClassificacaoShadowLog.objects.all().delete()


@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True},
    )
    return t


@pytest.fixture
def proc(trf1):
    return Process.objects.create(
        tribunal=trf1,
        numero_cnj='0001234-56.2023.4.01.0000',
        classe_codigo='12078',
        classe_nome='Cumprimento contra Fazenda',
    )


@pytest.fixture
def proc2(trf1):
    return Process.objects.create(
        tribunal=trf1,
        numero_cnj='0005678-90.2024.4.01.0000',
        classe_codigo='12078',
        classe_nome='Cumprimento contra Fazenda',
    )


def _versao_ativa(versao='v6', pesos=None):
    return ClassificadorVersao.objects.create(
        versao=versao,
        pesos=pesos if pesos is not None else dict(HARDCODED_WEIGHTS),
        ativa=True,
    )


def _versao_shadow(versao, pesos=None):
    return ClassificadorVersao.objects.create(
        versao=versao,
        pesos=pesos if pesos is not None else dict(HARDCODED_WEIGHTS),
        ativa=False,
        shadow=True,
    )


# ---------- classificar_shadow ----------

def test_classificar_shadow_zero_versoes_retorna_zero(proc):
    """Sem ClassificadorVersao(shadow=True), classificar_shadow retorna 0 e
    não toca em ClassificacaoShadowLog."""
    _versao_ativa('v6')
    n = classificar_shadow(proc)
    assert n == 0
    assert ClassificacaoShadowLog.objects.count() == 0


def test_classificar_shadow_uma_versao_cria_um_log(proc):
    _versao_ativa('v6')
    _versao_shadow('v7')
    n = classificar_shadow(proc)
    assert n == 1
    logs = list(ClassificacaoShadowLog.objects.all())
    assert len(logs) == 1
    log = logs[0]
    assert log.processo_id == proc.pk
    assert log.versao_shadow == 'v7'
    assert 0.0 <= log.score <= 1.0
    assert log.categoria in {
        Process.CLASSIF_PRECATORIO,
        Process.CLASSIF_PRE_PRECATORIO,
        Process.CLASSIF_DIREITO_CREDITORIO,
        Process.CLASSIF_NAO_LEAD,
    }


def test_classificar_shadow_duas_versoes_cria_dois_logs(proc):
    _versao_ativa('v6')
    _versao_shadow('v7-a')
    _versao_shadow('v7-b')
    n = classificar_shadow(proc)
    assert n == 2
    versoes = set(
        ClassificacaoShadowLog.objects.values_list('versao_shadow', flat=True),
    )
    assert versoes == {'v7-a', 'v7-b'}


def test_classificar_shadow_pula_pesos_corrompidos(proc, caplog):
    """Versão shadow com pesos faltando features não cria log (log warning)."""
    import logging
    _versao_ativa('v6')
    # Pesos só com uma feature — viola _validate_pesos.
    _versao_shadow('v7-broken', pesos={'F1_cumprim': 1.0})
    _versao_shadow('v7-ok')

    logger = logging.getLogger('voyager')
    propagate = logger.propagate
    logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger='voyager.tribunals.classificador'):
            n = classificar_shadow(proc)
    finally:
        logger.propagate = propagate

    assert n == 1
    versoes = set(
        ClassificacaoShadowLog.objects.values_list('versao_shadow', flat=True),
    )
    assert versoes == {'v7-ok'}


# ---------- _categorizar ----------

def test_categorizar_usa_thresholds_default_quando_sem_tribunal():
    features = {'F1_cumprim': 1, 'F2_precat_tc': 1}
    # score muito alto e tem F2 → PRECATORIO (threshold default 0.70).
    cat = _categorizar(0.95, features, tribunal_id=None)
    assert cat == Process.CLASSIF_PRECATORIO


def test_categorizar_le_threshold_do_db_quando_existe(trf1):
    """ThresholdTribunal ativo é usado quando informado o tribunal_id."""
    ThresholdTribunal.objects.create(
        tribunal=trf1, versao_modelo='v6',
        threshold_precatorio=0.99,  # quase impossível atingir
        threshold_pre=0.95,
        threshold_dc=0.90,
        ativo=True,
    )
    features = {'F1_cumprim': 1, 'F2_precat_tc': 1}
    # Score 0.80: sem threshold custom seria PRECATORIO (default 0.70);
    # com threshold 0.99, vira DIREITO_CREDITORIO? Não — pre/dc também subiram.
    # 0.80 < 0.90 (dc) → NAO_LEAD.
    # versao_modelo='v6' explícito: review T20 exige filtro por versão
    # pra prevenir mix entre v6/v7 no momento da transição.
    cat = _categorizar(0.80, features, tribunal_id=trf1.pk, versao_modelo='v6')
    assert cat == Process.CLASSIF_NAO_LEAD


# ---------- hook em classificar_e_persistir ----------

def test_sample_rate_zero_nao_enfileira(proc, settings):
    settings.SHADOW_SAMPLE_RATE = 0.0
    _versao_ativa('v6')
    _versao_shadow('v7')
    force_reload_weights()

    with patch('tribunals.jobs.classificar_shadow_async.delay') as mock_delay:
        classificar_e_persistir(proc, registrar_log=False)
    mock_delay.assert_not_called()


def test_sample_rate_um_sempre_enfileira(proc, settings):
    settings.SHADOW_SAMPLE_RATE = 1.0
    _versao_ativa('v6')
    _versao_shadow('v7')
    force_reload_weights()

    with patch('tribunals.jobs.classificar_shadow_async.delay') as mock_delay:
        classificar_e_persistir(proc, registrar_log=False)
    mock_delay.assert_called_once_with(proc.pk)


def test_sample_rate_falha_enqueue_nao_propaga(proc, settings):
    """Falha de Redis/RQ no enqueue não deve afetar classificação principal."""
    settings.SHADOW_SAMPLE_RATE = 1.0
    _versao_ativa('v6')
    force_reload_weights()

    with patch('tribunals.jobs.classificar_shadow_async.delay',
               side_effect=RuntimeError('redis down')):
        # Não pode levantar.
        cat, score = classificar_e_persistir(proc, registrar_log=False)
    assert 0.0 <= score <= 1.0


# ---------- classificar_shadow_async ----------

def test_classificar_shadow_async_processo_inexistente_retorna_zero():
    """ID inexistente retorna 0 (sem crash)."""
    # Chama a função wrapped diretamente (sem ir pra fila).
    n = classificar_shadow_async(999_999_999)
    assert n == 0


def test_classificar_shadow_async_processo_valido(proc):
    _versao_ativa('v6')
    _versao_shadow('v7')
    n = classificar_shadow_async(proc.pk)
    assert n == 1
    assert ClassificacaoShadowLog.objects.filter(processo=proc).count() == 1


# ---------- comparar_shadow ----------

def test_comparar_shadow_com_logs_produz_relatorio(proc, proc2, tmp_path):
    _versao_ativa('v6')
    Process.objects.filter(pk=proc.pk).update(
        classificacao=Process.CLASSIF_NAO_LEAD,
        classificacao_score=0.10, classificacao_versao='v6',
        classificacao_em=timezone.now(),
    )
    Process.objects.filter(pk=proc2.pk).update(
        classificacao=Process.CLASSIF_PRE_PRECATORIO,
        classificacao_score=0.60, classificacao_versao='v6',
        classificacao_em=timezone.now(),
    )

    # Logs shadow: proc concorda, proc2 discorda.
    ClassificacaoShadowLog.objects.create(
        processo=proc, versao_shadow='v7',
        score=0.12, categoria=Process.CLASSIF_NAO_LEAD,
    )
    ClassificacaoShadowLog.objects.create(
        processo=proc2, versao_shadow='v7',
        score=0.85, categoria=Process.CLASSIF_PRECATORIO,
    )

    out = tmp_path / 'shadow.md'
    result = comparar_shadow(
        versao_a='v6', versao_b='v7', dias=7, output_path=str(out),
    )

    assert result['total'] == 2
    assert result['total_disagreements'] == 1
    assert 0.0 <= result['agreement_rate'] <= 1.0
    assert result['agreement_rate'] == 0.5
    assert result['ks_statistic'] >= 0.0
    assert result['report_path'] == str(out)
    assert out.exists()
    content = out.read_text(encoding='utf-8')
    assert '# Shadow comparison' in content
    assert 'v6' in content and 'v7' in content
    assert 'Top disagreements' in content


def test_comparar_shadow_sem_logs_retorna_vazio(tmp_path):
    """Sem logs no período, retorna estatísticas zeradas sem crash."""
    out = tmp_path / 'empty.md'
    result = comparar_shadow(
        versao_a='v6', versao_b='v7', dias=1, output_path=str(out),
    )
    assert result['total'] == 0
    assert result['total_disagreements'] == 0
    assert result['agreement_rate'] == 0.0
    assert result['ks_statistic'] == 0.0
    assert out.exists()


def test_comparar_shadow_dedup_mais_recente_por_processo(proc, tmp_path):
    """Quando processo tem >1 shadow log, comparar_shadow usa o mais recente."""
    _versao_ativa('v6')
    Process.objects.filter(pk=proc.pk).update(
        classificacao=Process.CLASSIF_NAO_LEAD, classificacao_score=0.1,
        classificacao_em=timezone.now(),
    )
    now = timezone.now()
    ClassificacaoShadowLog.objects.create(
        processo=proc, versao_shadow='v7',
        score=0.1, categoria=Process.CLASSIF_NAO_LEAD,
    )
    novo = ClassificacaoShadowLog.objects.create(
        processo=proc, versao_shadow='v7',
        score=0.9, categoria=Process.CLASSIF_PRECATORIO,
    )
    # Força criada_em mais recente no segundo.
    ClassificacaoShadowLog.objects.filter(pk=novo.pk).update(
        criada_em=now + timedelta(seconds=10),
    )

    out = tmp_path / 'dedup.md'
    result = comparar_shadow(
        versao_a='v6', versao_b='v7', dias=7, output_path=str(out),
    )
    # Apenas 1 par considerado (o mais recente — divergente).
    assert result['total'] == 1
    assert result['total_disagreements'] == 1


# ---------- helpers internos ----------

def test_ks_2samp_distribuicoes_iguais():
    a = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert _ks_2samp(a, list(a)) == 0.0


def test_ks_2samp_distribuicoes_distintas():
    a = [0.0, 0.0, 0.0, 0.0]
    b = [1.0, 1.0, 1.0, 1.0]
    # CDF(a) sobe pra 1.0 em 0.0; CDF(b) só sobe em 1.0 → diff máxima = 1.0
    assert _ks_2samp(a, b) == 1.0


def test_ks_2samp_lista_vazia():
    assert _ks_2samp([], [0.1, 0.2]) == 0.0
    assert _ks_2samp([0.1], []) == 0.0


def test_confusion_matrix_basico():
    pairs = [
        ('PRECATORIO', 'PRECATORIO'),
        ('NAO_LEAD', 'PRECATORIO'),
        ('PRE_PRECATORIO', 'PRE_PRECATORIO'),
    ]
    cm = _confusion_matrix(pairs)
    assert cm['total'] == 3
    assert cm['concordantes'] == 2
    assert cm['agreement_rate'] == pytest.approx(2 / 3)
    assert cm['matriz']['PRECATORIO']['PRECATORIO'] == 1
    assert cm['matriz']['NAO_LEAD']['PRECATORIO'] == 1


# ---------- shadow_status (queries) ----------

def test_shadow_status_sem_versao_retorna_none():
    from dashboard.queries import shadow_status
    assert shadow_status() is None


def test_shadow_status_com_versao_retorna_dict(trf1, proc, settings, tmp_path):
    """Versão shadow ativa + logs no período → dict completo."""
    from dashboard.queries import shadow_status

    _versao_shadow('v7')
    ClassificacaoShadowLog.objects.create(
        processo=proc, versao_shadow='v7',
        score=0.5, categoria=Process.CLASSIF_NAO_LEAD,
    )

    # Aponta BASE_DIR pra tmp pra controlar lookup de relatórios.
    settings.SHADOW_SAMPLE_RATE = 0.1
    settings.BASE_DIR = str(tmp_path)
    ia_dir = tmp_path / '.ia'
    ia_dir.mkdir()
    report_file = ia_dir / 'SHADOW_COMPARISON_20260512.md'
    report_file.write_text('# stub', encoding='utf-8')

    data = shadow_status()
    assert data is not None
    assert data['versao_shadow'] == 'v7'
    assert data['total_logs_7d'] == 1
    assert data['last_log_at'] is not None
    assert data['last_report'] == 'SHADOW_COMPARISON_20260512.md'
    assert data['sample_rate'] == pytest.approx(0.1)


# ---------- chart_shadow_status (view) ----------

def _user_com_permissao():
    user = User.objects.create_user(username='viz', password='x')
    perm = Permission.objects.get(codename='can_view_validacao_dashboard')
    group, _ = Group.objects.get_or_create(name='auditores_leads')
    group.permissions.add(perm)
    user.groups.add(group)
    return user


def test_chart_shadow_status_endpoint_retorna_json():
    user = _user_com_permissao()
    client = Client()
    client.force_login(user)
    url = reverse('dashboard:chart_shadow_status')
    resp = client.get(url)
    assert resp.status_code == 200
    payload = resp.json()
    assert 'data' in payload
    # Sem ClassificadorVersao(shadow=True), data deve ser None.
    assert payload['data'] is None


def test_chart_shadow_status_endpoint_com_shadow(trf1, proc):
    _versao_shadow('v7')
    ClassificacaoShadowLog.objects.create(
        processo=proc, versao_shadow='v7',
        score=0.5, categoria=Process.CLASSIF_NAO_LEAD,
    )
    user = _user_com_permissao()
    client = Client()
    client.force_login(user)
    url = reverse('dashboard:chart_shadow_status')
    resp = client.get(url)
    assert resp.status_code == 200
    data = resp.json().get('data')
    assert data is not None
    assert data['versao_shadow'] == 'v7'
    assert data['total_logs_7d'] == 1
