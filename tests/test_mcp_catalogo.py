"""Catálogo das telas do MCP: sai do servidor, nada some."""
import pytest

from mcp_server import catalogo
from mcp_server.server import TOOLS


def test_toda_tool_do_servidor_esta_num_grupo_com_titulo():
    """Tool nova cai em "Outras" (a tela mostra), mas o catálogo tem de ser
    atualizado: este teste é o lembrete."""
    gs = catalogo.grupos()
    assert 'outras' not in [g.slug for g in gs]
    nomes = [t.nome for g in gs for t in g.tools]
    assert sorted(nomes) == sorted(t['name'] for t in TOOLS)
    assert len(nomes) == len(set(nomes))
    assert set(catalogo.TITULOS) == set(nomes)


def test_grupo_nao_cita_tool_inexistente():
    existentes = {t['name'] for t in TOOLS}
    for _, _, _, _, nomes in catalogo.GRUPOS:
        assert set(nomes) <= existentes


def test_tool_sem_grupo_aparece_em_outras(monkeypatch):
    monkeypatch.setattr(catalogo, 'GRUPOS', catalogo.GRUPOS[:1])
    gs = catalogo.grupos()
    assert gs[-1].slug == 'outras'
    assert sum(len(g.tools) for g in gs) == len(TOOLS)


def test_escrita_e_externa_vem_das_annotations():
    ctx = catalogo.contexto()
    assert sorted(ctx['tools_escrita']) == ['monitorar_processo', 'monitorar_termo']
    assert ctx['total_externas'] > 0
    assert ctx['total_tools'] == len(TOOLS)


@pytest.mark.django_db
def test_landing_no_navegador_e_405_pro_cliente_mcp(client, settings):
    settings.MCP_PUBLIC_BASE_URL = 'https://voyager.test'
    resp = client.get('/mcp', HTTP_ACCEPT='text/html,application/xhtml+xml')
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'https://voyager.test/mcp' in html
    assert f'Catálogo · {len(TOOLS)} tools' in html
    assert "script-src 'self'" in resp['Content-Security-Policy']
    assert client.get('/mcp/', HTTP_ACCEPT='text/html').status_code == 200
    assert client.get('/mcp', HTTP_ACCEPT='text/event-stream').status_code == 405
    assert client.get('/mcp').status_code == 405


@pytest.mark.django_db
def test_pagina_do_dashboard_mostra_status_e_catalogo(client, settings):
    from django.contrib.auth import get_user_model

    from mcp_server.models import MCPAcesso
    settings.MCP_PUBLIC_BASE_URL = 'https://voyager.test'
    U = get_user_model()
    adm = U.objects.create_superuser('adm@x.com', 'adm@x.com', 'senha-forte-123')
    U.objects.create_user('semacesso@x.com', 'semacesso@x.com', 'senha-forte-123')
    MCPAcesso.objects.create(usuario=adm)
    client.force_login(adm)
    resp = client.get('/dashboard/mcp/')
    assert resp.status_code == 200
    assert resp.context['tem_acesso'] is True
    assert resp.context['admin_acessos']['sem_acesso'] == 1
    html = resp.content.decode()
    assert 'Liberada' in html and 'visao_geral' in html and 'https://voyager.test/mcp' in html
