"""Tests do shell HTML das páginas de validação humana (T13).

Cobertura:
- overview GET com/sem permissão
- overview lista lotes ativos do usuário
- lote GET com/sem permissão, container HTMX presente
- lote concluído GET render
- CSRF token presente em forms
- JS hotkeys referenciado no template do lote
- Modal hotkeys presente no template do lote
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from tribunals.models import (
    AmostraProcesso,
    AmostraValidacao,
    ClassificadorVersao,
    Process,
    ProcessoValidacao,
    Tribunal,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------- fixtures ----------

@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1',
        defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1', 'ativo': True},
    )
    return t


@pytest.fixture
def versao_ativa(db):
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    versao, _ = ClassificadorVersao.objects.update_or_create(
        versao='v6', defaults={'pesos': {'_intercept_': 0.0}, 'ativa': True},
    )
    return versao


def _add_perm(user, codename: str):
    p = Permission.objects.get(
        content_type__app_label='tribunals', codename=codename,
    )
    user.user_permissions.add(p)


@pytest.fixture
def validador(db):
    u = User.objects.create_user(username='validador-t13', password='x')
    _add_perm(u, 'can_validate_lead')
    _add_perm(u, 'can_view_validacao_dashboard')
    return u


@pytest.fixture
def viewer(db):
    u = User.objects.create_user(username='viewer-t13', password='x')
    _add_perm(u, 'can_view_validacao_dashboard')
    return u


@pytest.fixture
def noperm(db):
    return User.objects.create_user(username='zero-t13', password='x')


@pytest.fixture
def processos(trf1):
    objs = [
        Process(
            tribunal=trf1,
            numero_cnj=f'{i:07d}-67.2023.4.01.3400',
            classificacao=Process.CLASSIF_PRECATORIO,
            classificacao_score=0.90 + i * 0.005,
        )
        for i in range(5)
    ]
    Process.objects.bulk_create(objs)
    return list(Process.objects.filter(tribunal=trf1).order_by('id'))


@pytest.fixture
def lote(trf1, processos, validador, versao_ativa):
    amostra = AmostraValidacao.objects.create(
        estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
        tribunal=trf1,
        versao_modelo='v6',
        criada_por=validador,
        tamanho_alvo=5,
        seed=42,
    )
    for ordem, p in enumerate(processos, start=1):
        AmostraProcesso.objects.create(
            amostra=amostra, processo=p, ordem=ordem,
            score_no_sorteio=p.classificacao_score,
            classificacao_no_sorteio=p.classificacao,
        )
    return amostra


# ---------- overview ----------

def test_overview_sem_perm_403(client, noperm):
    client.force_login(noperm)
    resp = client.get(reverse('dashboard:leads_validacao_overview'))
    assert resp.status_code == 403


def test_overview_com_perm_200(client, viewer):
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_validacao_overview'))
    assert resp.status_code == 200
    assert b'VALIDA' in resp.content.upper()


def test_overview_lista_lotes_ativos(client, validador, lote):
    """Lote criado pelo usuário aparece na seção de lotes ativos."""
    client.force_login(validador)
    resp = client.get(reverse('dashboard:leads_validacao_overview'))
    assert resp.status_code == 200
    body = resp.content.decode()
    # LOTE-#### gerado a partir do pk.
    assert f'LOTE-{lote.pk:04d}' in body
    # CTA "Continuar" / link pra fila
    assert reverse('dashboard:leads_validacao_lote', kwargs={'lote_id': lote.pk}) in body


def test_overview_renderiza_modal_novo_lote(client, viewer):
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_validacao_overview'))
    body = resp.content.decode()
    # Form HTMX que cria o lote
    assert reverse('dashboard:leads_validacao_criar_lote') in body
    # CSRF token presente
    assert 'csrfmiddlewaretoken' in body
    # Lista de estratégias renderizada como <option>
    assert 'value="borderline"' in body
    assert 'value="top_score"' in body


# ---------- lote (fila de anotação) ----------

def test_lote_sem_perm_403(client, viewer, lote):
    """Viewer (só view perm) não anota → 403."""
    client.force_login(viewer)
    resp = client.get(
        reverse('dashboard:leads_validacao_lote', kwargs={'lote_id': lote.pk}),
    )
    assert resp.status_code == 403


def test_lote_com_perm_200_e_card_container(client, validador, lote):
    client.force_login(validador)
    resp = client.get(
        reverse('dashboard:leads_validacao_lote', kwargs={'lote_id': lote.pk}),
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    # Container HTMX presente, com URL do item 1.
    assert 'id="card-container"' in body
    assert 'hx-get="' in body
    assert reverse(
        'dashboard:leads_validacao_item',
        kwargs={'lote_id': lote.pk, 'posicao': 1},
    ) in body
    # Modal de hotkeys e JS de atalhos referenciados.
    assert 'hotkeys-overlay' in body
    assert 'validacao_hotkeys.js' in body
    # data-page presente pra escopo do JS.
    assert 'data-page="leads-validacao-lote"' in body


def test_lote_progress_bar_presente(client, validador, lote):
    client.force_login(validador)
    resp = client.get(
        reverse('dashboard:leads_validacao_lote', kwargs={'lote_id': lote.pk}),
    )
    body = resp.content.decode()
    assert 'lote-progress-bar' in body
    assert 'role="progressbar"' in body


# ---------- concluído ----------

def test_lote_concluido_render(client, validador, lote):
    client.force_login(validador)
    resp = client.get(
        reverse(
            'dashboard:leads_validacao_lote_concluido',
            kwargs={'lote_id': lote.pk},
        ),
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'MISSION COMPLETE' in body
    # Link de criar novo lote
    assert reverse('dashboard:leads_validacao_overview') in body


def test_lote_concluido_com_decisoes_mostra_distribuicao(
    client, validador, lote,
):
    """Decisões registradas devem aparecer na distribuição."""
    item = lote.itens.order_by('ordem').first()
    ProcessoValidacao.objects.create(
        processo=item.processo,
        amostra=lote,
        usuario=validador,
        usuario_hash='hashed-test',
        resultado=ProcessoValidacao.RESULTADO_EH_PRECATORIO,
        confianca=ProcessoValidacao.CONFIANCA_ALTA,
        motivo='',
        versao_modelo='v6',
        classificacao_no_momento=Process.CLASSIF_PRECATORIO,
        score_no_momento=0.9,
    )
    client.force_login(validador)
    resp = client.get(
        reverse(
            'dashboard:leads_validacao_lote_concluido',
            kwargs={'lote_id': lote.pk},
        ),
    )
    body = resp.content.decode()
    assert 'eh_precatorio' in body


# ---------- redirect quando posicao > total ----------

def test_lote_redireciona_pra_concluido_quando_terminado(client, validador, lote):
    """posicao 9999 → redirect 302 pra concluido."""
    client.force_login(validador)
    resp = client.get(
        reverse(
            'dashboard:leads_validacao_item',
            kwargs={'lote_id': lote.pk, 'posicao': 9999},
        ),
    )
    assert resp.status_code == 302
    assert f'/leads/validacao/{lote.pk}/concluido/' in resp['Location']
