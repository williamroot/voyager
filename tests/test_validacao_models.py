"""Testes dos modelos de validação humana de leads (tarefa T4/T5).

Cobre:
- CRUD de AmostraValidacao + AmostraProcesso (through M2M).
- ProcessoValidacao válida.
- UniqueConstraint(processo, usuario) — re-anotação proibida.
- SET_NULL na FK usuario preserva label (LGPD).
- Choices validados via full_clean().
- ClassificadorVersao: N shadow=True OK; 2 ativa=True viola constraint.
- ThresholdTribunal: 1 ativo por (tribunal, versao_modelo).
- Comando setup_validacao_groups idempotente.
"""
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from tribunals.models import (
    AmostraProcesso,
    AmostraValidacao,
    ClassificadorVersao,
    Process,
    ProcessoValidacao,
    ThresholdTribunal,
    Tribunal,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------- fixtures ----------

@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True},
    )
    return t


@pytest.fixture
def proc(trf1):
    return Process.objects.create(tribunal=trf1, numero_cnj='0001234-56.2025.4.01.0000')


@pytest.fixture
def proc2(trf1):
    return Process.objects.create(tribunal=trf1, numero_cnj='0009999-99.2025.4.01.0000')


@pytest.fixture
def user_alice(db):
    return User.objects.create_user(username='alice', password='x')


@pytest.fixture
def user_bob(db):
    return User.objects.create_user(username='bob', password='x')


# ---------- AmostraValidacao + AmostraProcesso ----------

def test_amostra_validacao_criar_e_adicionar_processos(trf1, proc, proc2, user_alice):
    amostra = AmostraValidacao.objects.create(
        estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
        tribunal=trf1,
        versao_modelo='v6',
        criada_por=user_alice,
        parametros={'min_score': 0.7},
        tamanho_alvo=50,
        seed=12345,
    )
    AmostraProcesso.objects.create(
        amostra=amostra, processo=proc, ordem=0,
        score_no_sorteio=0.92, classificacao_no_sorteio='PRECATORIO',
    )
    AmostraProcesso.objects.create(
        amostra=amostra, processo=proc2, ordem=1,
        score_no_sorteio=0.88, classificacao_no_sorteio='PRE_PRECATORIO',
        suspeita_score=0.3, motivos_suspeita=['shadow_disagree'],
    )

    assert amostra.processos.count() == 2
    assert list(amostra.itens.order_by('ordem').values_list('ordem', flat=True)) == [0, 1]


def test_amostra_processo_unique_amostra_processo(trf1, proc, user_alice):
    amostra = AmostraValidacao.objects.create(
        estrategia=AmostraValidacao.ESTRATEGIA_BORDERLINE,
        tribunal=trf1, versao_modelo='v6', criada_por=user_alice,
        tamanho_alvo=10, seed=1,
    )
    AmostraProcesso.objects.create(
        amostra=amostra, processo=proc, ordem=0,
        score_no_sorteio=0.5, classificacao_no_sorteio='PRE_PRECATORIO',
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AmostraProcesso.objects.create(
            amostra=amostra, processo=proc, ordem=1,
            score_no_sorteio=0.5, classificacao_no_sorteio='PRE_PRECATORIO',
        )


# ---------- ProcessoValidacao ----------

def _criar_validacao(processo, usuario, **overrides):
    defaults = {
        'resultado': ProcessoValidacao.RESULTADO_EH_LEAD,
        'versao_modelo': 'v6',
        'classificacao_no_momento': 'PRECATORIO',
        'score_no_momento': 0.85,
        'features_snapshot': {'F1': 1.0},
    }
    defaults.update(overrides)
    return ProcessoValidacao.objects.create(
        processo=processo, usuario=usuario, **defaults,
    )


def test_processo_validacao_basica(proc, user_alice):
    v = _criar_validacao(proc, user_alice)
    assert v.pk is not None
    assert v.confianca == ProcessoValidacao.CONFIANCA_ALTA  # default
    assert v.resultado == ProcessoValidacao.RESULTADO_EH_LEAD


def test_processo_validacao_unique_processo_usuario(proc, user_alice):
    """Imutabilidade: mesmo (processo, usuario) NÃO pode anotar 2x."""
    _criar_validacao(proc, user_alice)
    with pytest.raises(IntegrityError), transaction.atomic():
        _criar_validacao(proc, user_alice, resultado=ProcessoValidacao.RESULTADO_NAO_LEAD)


def test_processo_validacao_dois_usuarios_distintos_ok(proc, user_alice, user_bob):
    """Dupla anotação por usuários diferentes é permitida (10% do lote)."""
    _criar_validacao(proc, user_alice, resultado=ProcessoValidacao.RESULTADO_EH_LEAD)
    _criar_validacao(proc, user_bob, resultado=ProcessoValidacao.RESULTADO_NAO_LEAD)
    assert ProcessoValidacao.objects.filter(processo=proc).count() == 2


def test_processo_validacao_set_null_preserva_label(proc, user_alice):
    """LGPD: deletar User → usuario=NULL mas label permanece."""
    v = _criar_validacao(proc, user_alice,
                         resultado=ProcessoValidacao.RESULTADO_EH_PRECATORIO)
    v_id = v.pk
    user_alice.delete()

    v.refresh_from_db()
    assert v.usuario_id is None
    assert v.resultado == ProcessoValidacao.RESULTADO_EH_PRECATORIO
    assert v.score_no_momento == 0.85
    assert ProcessoValidacao.objects.filter(pk=v_id).exists()


def test_processo_validacao_choices_invalido(proc, user_alice):
    """full_clean valida choices → ValidationError em valor fora da lista."""
    v = ProcessoValidacao(
        processo=proc, usuario=user_alice,
        resultado='ARBITRARIO',  # fora dos choices
        versao_modelo='v6',
        classificacao_no_momento='PRECATORIO',
        score_no_momento=0.5,
    )
    with pytest.raises(ValidationError):
        v.full_clean()


# ---------- ClassificadorVersao: shadow vs ativa ----------

def test_classificador_versao_multipla_shadow_ok():
    ClassificadorVersao.objects.create(
        versao='v7a', pesos={'_intercept_': 0.0}, ativa=False, shadow=True,
    )
    ClassificadorVersao.objects.create(
        versao='v7b', pesos={'_intercept_': 0.0}, ativa=False, shadow=True,
    )
    ClassificadorVersao.objects.create(
        versao='v7c', pesos={'_intercept_': 0.0}, ativa=False, shadow=True,
    )
    assert ClassificadorVersao.objects.filter(shadow=True).count() == 3


def test_classificador_versao_duas_ativas_viola_constraint():
    # Migration 0026 já criou v6 ativa; nosso teste usa versões distintas.
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    ClassificadorVersao.objects.create(
        versao='v_test_a', pesos={'_intercept_': 0.0}, ativa=True,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ClassificadorVersao.objects.create(
            versao='v_test_b', pesos={'_intercept_': 0.0}, ativa=True,
        )


# ---------- ThresholdTribunal ----------

def test_threshold_tribunal_unique_tribunal_versao(trf1):
    ThresholdTribunal.objects.create(
        tribunal=trf1, versao_modelo='v6',
        threshold_precatorio=0.7, threshold_pre=0.4, threshold_dc=0.2,
        ativo=True,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ThresholdTribunal.objects.create(
            tribunal=trf1, versao_modelo='v6',
            threshold_precatorio=0.8, threshold_pre=0.5, threshold_dc=0.3,
            ativo=True,
        )


def test_threshold_tribunal_versao_diferente_ok(trf1):
    ThresholdTribunal.objects.create(
        tribunal=trf1, versao_modelo='v6',
        threshold_precatorio=0.7, threshold_pre=0.4, threshold_dc=0.2,
    )
    ThresholdTribunal.objects.create(
        tribunal=trf1, versao_modelo='v7',
        threshold_precatorio=0.65, threshold_pre=0.35, threshold_dc=0.2,
    )
    assert ThresholdTribunal.objects.filter(tribunal=trf1).count() == 2


# ---------- management command setup_validacao_groups ----------

def test_setup_validacao_groups_idempotente():
    out = StringIO()
    call_command('setup_validacao_groups', stdout=out)
    call_command('setup_validacao_groups', stdout=out)  # 2ª vez

    nomes = set(Group.objects.values_list('name', flat=True))
    assert {'validadores_leads', 'revisores_seniores', 'model_admins'} <= nomes
    # Sem duplicações: cada grupo apenas 1 vez.
    assert Group.objects.filter(name='validadores_leads').count() == 1
    assert Group.objects.filter(name='revisores_seniores').count() == 1
    assert Group.objects.filter(name='model_admins').count() == 1

    validadores = Group.objects.get(name='validadores_leads')
    perm_codes = set(validadores.permissions.values_list('codename', flat=True))
    assert {'can_validate_lead', 'can_view_validacao_dashboard'} <= perm_codes

    revisores = Group.objects.get(name='revisores_seniores')
    perm_codes_r = set(revisores.permissions.values_list('codename', flat=True))
    assert 'can_resolve_disagreement' in perm_codes_r
    assert 'can_validate_lead' in perm_codes_r

    model_admins = Group.objects.get(name='model_admins')
    perm_codes_m = set(model_admins.permissions.values_list('codename', flat=True))
    assert 'can_publish_model' in perm_codes_m
