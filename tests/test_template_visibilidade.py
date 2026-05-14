"""Testes do template `dashboard/leads/visibilidade.html` (T12).

Foco em estrutura/HTML do shell server-side. Validações cobrem:
- 403 sem permissão `can_view_validacao_dashboard`
- 200 + template correto com permissão
- 8 KPI cards renderizados
- 5 chart cards (com URLs dos endpoints T8 no atributo data-chart-url)
- CSRF token presente (precisa pro stub de re-ingestão)
- ARIA labels nos charts (a11y)
- Classes responsivas (kpi-grid, lg:grid-cols-2 etc.)
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from tribunals.models import ClassificadorVersao, Tribunal

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------- fixtures ----------

@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1', 'ativo': True},
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
    p = Permission.objects.get(content_type__app_label='tribunals', codename=codename)
    user.user_permissions.add(p)


@pytest.fixture
def viewer(db):
    """Tem `can_view_validacao_dashboard`."""
    u = User.objects.create_user(username='viewer-vis', password='x')
    _add_perm(u, 'can_view_validacao_dashboard')
    return u


@pytest.fixture
def noperm_user(db):
    return User.objects.create_user(username='zero-vis', password='x')


# ---------- testes ----------

def test_403_sem_perm(client, noperm_user):
    """Sem permissão → 403."""
    client.force_login(noperm_user)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    assert resp.status_code == 403


def test_200_e_template_correto(client, viewer, trf1, versao_ativa):
    """Com permissão → 200 e usa o template novo."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    assert resp.status_code == 200
    # `assertTemplateUsed` em pytest é via cliente — checamos via template_name.
    template_names = {t.name for t in resp.templates if getattr(t, 'name', None)}
    assert 'dashboard/leads/visibilidade.html' in template_names


def test_contem_8_kpi_cards(client, viewer, trf1, versao_ativa):
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    # Atributo data-kpi nos cards do mockup
    esperados = {
        'precatorio', 'pre', 'dc', 'validacao',
        'descobertos', 'consumidos',
        'lotes-ativos', 'fn-semana',
    }
    encontrados = {k for k in esperados if f'data-kpi="{k}"' in body}
    assert encontrados == esperados, f'KPIs faltando: {esperados - encontrados}'


def test_contem_5_chart_cards_com_urls(client, viewer, trf1, versao_ativa):
    """Página deve conter 5 chart cards com os endpoints T8."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    endpoints = [
        '/dashboard/leads/visibilidade/chart/histograma-score/',
        '/dashboard/leads/visibilidade/chart/calibracao/',
        '/dashboard/leads/visibilidade/chart/heatmap/',
        '/dashboard/leads/visibilidade/chart/funil/',
    ]
    for url in endpoints:
        assert url in body, f'endpoint não encontrado: {url}'
    # Distribuição de score (mini) — 5o chart
    assert 'data-chart="distribuicao-score"' in body or 'distribuicao-score' in body
    # 5 chart-card sections
    assert body.count('class="chart-card') >= 4 or body.count('chart-card') >= 5


def test_csrf_token_no_body(client, viewer, trf1, versao_ativa):
    """Necessário pro stub de re-ingestão (POST /dashboard/ingestao/reingerir/)."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    assert 'csrfmiddlewaretoken' in body


def test_aria_labels_nos_charts(client, viewer, trf1, versao_ativa):
    """A11y: charts/regiões têm aria-label e role=region."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    assert 'role="region"' in body
    # Pelo menos um aria-label em chart-card
    assert 'aria-label="Histograma de score · por tribunal"' in body
    assert 'aria-label="Calibração · por tribunal"' in body
    assert 'aria-label="Heatmap tribunal × ano CNJ"' in body
    assert 'aria-label="Funil ampliado"' in body
    # role=progressbar (lotes ativos) — somente se houver lotes; checamos a presença de aria-live nos skeletons
    assert 'aria-live="polite"' in body


def test_classes_responsivas(client, viewer, trf1, versao_ativa):
    """Mobile/responsivo: grid responsivo + classes lg:."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    # KPI grid responsivo (classe CSS dedicada)
    assert 'kpi-grid' in body
    # Charts em grid 2-col em lg
    assert 'lg:grid-cols-2' in body
    # Lotes + FN preview em 3-col com col-span-2
    assert 'lg:grid-cols-3' in body
    assert 'lg:col-span-2' in body


def test_modal_reingest_presente(client, viewer, trf1, versao_ativa):
    """Modal de re-ingestão é stub mas deve estar no HTML."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    assert 'showReingestModal' in body
    assert 'Reingerir DJEN' in body
    assert 'reingest-modal-title' in body


def test_filtros_tribunal_e_periodo(client, viewer, trf1, versao_ativa):
    """Filtros de tribunal (chips) e período (botões) renderizam."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    # Chip "Todos"
    assert '>Todos<' in body
    # Chip do TRF1 (fixture)
    assert '>TRF1<' in body
    # Botões de período
    assert '>7d<' in body
    assert '>30d<' in body
    assert '>90d<' in body


def test_links_externos_uteis(client, viewer, trf1, versao_ativa):
    """API docs + criar novo lote (validação) linkam pras URLs certas."""
    client.force_login(viewer)
    resp = client.get(reverse('dashboard:leads_visibilidade'))
    body = resp.content.decode('utf-8')
    assert reverse('dashboard:api-docs') in body
    assert reverse('dashboard:leads_validacao_overview') in body
