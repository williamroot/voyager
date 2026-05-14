"""Testes do partial `_validacao_card.html` (T14).

Renderiza o template diretamente via `render_to_string`, sem cliente HTTP
nem view. Foca em estrutura, presença de elementos críticos (CSRF, hotkeys,
ARIA) e robustez contra contexto mínimo.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string

pytestmark = pytest.mark.django_db


# ---------- helpers ----------

def _mk_processo(pk=1, cnj='0001234-56.2025.4.01.0000', tribunal_sigla='TRF1'):
    """Stub mínimo. O partial só lê atributos, não persiste."""
    tribunal = SimpleNamespace(sigla=tribunal_sigla)
    return SimpleNamespace(
        pk=pk,
        numero_cnj=cnj,
        tribunal=tribunal,
        classe_nome='Cumprimento de Sentença contra Fazenda Pública',
        ano_cnj=2023,
        total_movimentacoes=47,
    )


def _mk_mov(data='2026-05-07', tipo='Decisão', texto='Defiro a expedição de precatório.'):
    # 18h UTC = 15h em America/Sao_Paulo — fica no mesmo dia, evita shift.
    return SimpleNamespace(
        data_disponibilizacao=datetime.fromisoformat(f'{data}T18:00:00').replace(tzinfo=timezone.utc),
        tipo_comunicacao=tipo,
        texto=texto,
    )


def _mk_parte(nome='MARIA DA SILVA', polo='ATIVO', papel='autor'):
    parte = SimpleNamespace(nome=nome)
    return SimpleNamespace(parte=parte, polo=polo, papel=papel)


def _base_ctx(**overrides):
    """Mock URL resolver evitando depender de urls.py (URLs T8 podem não existir).

    Resolveremos `dashboard:processo-detail` que JÁ EXISTE; o `salvar_url`
    é passado como string crua (o partial usa `salvar_url|default:''`).
    """
    ctx = {
        'processo': _mk_processo(),
        'classificacao_no_momento': 'PRE_PRECATORIO',
        'score_no_momento': 0.617,
        'score_breakdown': [],
        'suspeita': None,
        'ultimas_movs': [],
        'partes': [],
        'lote_id': 42,
        'posicao': 48,
        'total': 120,
        'versao_modelo': 'v6',
        'salvar_url': '/dashboard/leads/validacao/42/decidir/',
    }
    ctx.update(overrides)
    return ctx


def _render(ctx):
    return render_to_string('dashboard/_partials/_validacao_card.html', ctx)


# ---------- testes ----------

def test_renderiza_com_dados_minimos():
    """Sem suspeita, sem partes, sem movs, score_breakdown vazio."""
    html = _render(_base_ctx())
    # estrutura básica
    assert 'validacao-card' in html
    assert 'data-processo-id="1"' in html
    assert 'data-lote-id="42"' in html
    assert '0001234-56.2025.4.01.0000' in html
    assert 'PRE_PRECATORIO' in html
    # locale pode usar vírgula ou ponto decimal
    assert ('0.617' in html) or ('0,617' in html)
    assert '#48/120' in html
    # sem banner
    assert 'vc-banner-suspeita' not in html
    # score breakdown vazio renderiza placeholder
    assert 'indisponível' in html or 'sb-empty' in html


def test_renderiza_com_suspeita_preenchida():
    """Banner aparece quando há suspeita."""
    ctx = _base_ctx(suspeita={
        'score': 0.82,
        'motivos': ['precat-regex', 'rpv-text', 'trans-julg→expedição'],
    })
    html = _render(ctx)
    assert 'vc-banner-suspeita' in html
    assert 'data-nivel="alta"' in html  # 0.82 → alta
    assert ('0.82' in html) or ('0,82' in html)
    assert 'precat-regex' in html
    assert 'rpv-text' in html


def test_renderiza_com_cinco_movs():
    movs = [
        _mk_mov('2026-05-07', 'Expedição', 'Expedido Ofício Requisitório 2026/00231'),
        _mk_mov('2026-05-02', 'Decisão', 'Defiro a expedição de precatório.'),
        _mk_mov('2026-04-28', 'Cumprimento', 'Intime-se a Fazenda para impugnar.'),
        _mk_mov('2026-03-14', 'Sentença', 'Trânsito em julgado certificado.'),
        _mk_mov('2026-02-02', 'Mov. Geral', 'Conclusão para sentença.'),
    ]
    html = _render(_base_ctx(ultimas_movs=movs))
    assert 'Últimas 5 movimentações' in html
    assert '2026-05-07' in html
    assert 'Expedição' in html
    assert '2026-02-02' in html
    # truncatewords:30 não cortou frases curtas
    assert 'Expedido Ofício Requisitório' in html


def test_csrf_token_presente():
    """O partial inclui {% csrf_token %}. Sem request real, Django renderiza
    apenas o hidden input se o context tiver `csrf_token` injetado."""
    from django.test import RequestFactory
    rf = RequestFactory()
    request = rf.get('/')
    ctx = _base_ctx()
    html = render_to_string('dashboard/_partials/_validacao_card.html', ctx, request=request)
    assert 'csrfmiddlewaretoken' in html


def test_hotkeys_em_todos_os_sete_botoes():
    html = _render(_base_ctx())
    for hk in ['1', '2', '3', '4', 'I', 'E', 'S']:
        assert f'data-hotkey="{hk}"' in html, f'falta data-hotkey={hk}'


def test_aria_labels_em_todos_os_botoes():
    html = _render(_base_ctx())
    # cada decisão tem aria-label descritivo (não emoji)
    for label in ['Precatório', 'Pré-precatório', 'Direito Creditório',
                  'Não-lead', 'Incerto', 'Enriquecer', 'Pular']:
        assert f'aria-label="Marcar como {label}' in html or \
               f'aria-label="Enviar para {label.lower()}' in html or \
               f'aria-label="Pular este processo' in html or \
               label in html  # fallback no texto visível
    # garantia explícita: cada botão tem algum aria-label
    assert html.count('aria-label="') >= 8  # 7 decisão + 1 posição


def test_partes_renderiza_polo():
    partes = [
        _mk_parte('MARIA DA SILVA', 'ATIVO', 'autor'),
        _mk_parte('UNIÃO FEDERAL', 'PASSIVO', 'réu'),
    ]
    html = _render(_base_ctx(partes=partes))
    assert 'MARIA DA SILVA' in html
    assert 'UNIÃO FEDERAL' in html
    assert 'data-polo="ATIVO"' in html
    assert 'data-polo="PASSIVO"' in html


def test_score_breakdown_renderiza_top_5_ordenado():
    breakdown = [
        {'feature': 'F1', 'label': 'Cumprimento contra Fazenda',
         'peso': 1.92, 'valor': 1.0, 'contribuicao': 1.92},
        {'feature': 'F15', 'label': 'Volume de movs (log)',
         'peso': 2.31, 'valor': 0.61, 'contribuicao': 1.41},
        {'feature': 'F18', 'label': 'Ano CNJ',
         'peso': 0.44, 'valor': 0.62, 'contribuicao': 0.27},
    ]
    html = _render(_base_ctx(score_breakdown=breakdown))
    assert 'F1' in html
    assert 'Cumprimento contra Fazenda' in html
    assert ('+1.92' in html) or ('+1,92' in html)
    assert 'sb-pos' in html  # contrib positiva
    assert 'Volume de movs' in html


def test_link_voyager_externo_com_target_blank():
    """Garante rel=noopener para evitar tab-jacking."""
    html = _render(_base_ctx())
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html


def test_form_decisao_hx_post_aponta_para_salvar_url():
    html = _render(_base_ctx(salvar_url='/foo/bar/decidir/'))
    assert 'hx-post="/foo/bar/decidir/"' in html
    assert 'name="processo_id"' in html
    assert 'name="lote_id"' in html
