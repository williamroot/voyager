"""Integração da regra de sinal NEGATIVA do TJMA: Movimentacao real com
alvará de levantamento posterior à expedição rebaixa o lead e o tira do
filtro de leads (api/leads.py).

Usa uma ClassificadorVersao de teste com intercept alto (score ~0.99) para
garantir que, SEM a regra, o processo seria PRECATORIO — o teste exercita
exatamente o caminho N1 → rebaixamento.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

import tribunals.classificador as clf
from tribunals.classificador import HARDCODED_WEIGHTS, classificar_e_persistir
from tribunals.models import (
    ClassificadorVersao,
    Movimentacao,
    Process,
    Tribunal,
)

pytestmark = pytest.mark.django_db


def _reset_cache():
    with clf._WEIGHTS_LOCK:
        clf._WEIGHTS_CACHE.update(
            versao=None, pesos=None, thresholds=None, normas=None,
            loaded_at=0.0,
        )


@pytest.fixture(autouse=True)
def _versao_score_alto(settings):
    """Score ~0.99 p/ qualquer processo: isola o efeito da regra de sinal."""
    settings.SHADOW_SAMPLE_RATE = 0.0
    _reset_cache()
    ClassificadorVersao.objects.all().delete()
    # _validate_pesos exige TODAS as features do extrator (rejeita subset):
    # zera os pesos e sobe só o intercept → score ~0.99 p/ qualquer processo.
    pesos = {k: 0.0 for k in HARDCODED_WEIGHTS}
    pesos['_intercept_'] = 5.0
    ClassificadorVersao.objects.create(versao='vtest', pesos=pesos, ativa=True)
    yield
    _reset_cache()
    ClassificadorVersao.objects.all().delete()


def _processo_tjma(cnj):
    tj = Tribunal.objects.get_or_create(sigla='TJMA')[0]
    return tj, Process.objects.create(
        tribunal=tj, classe_codigo='12078', numero_cnj=cnj)


def _mov(p, tj, ext, quando, tipo, texto):
    Movimentacao.objects.create(
        processo=p, tribunal=tj, external_id=ext,
        data_disponibilizacao=quando, tipo_comunicacao=tipo, texto=texto)


def test_precatorio_tjma_com_alvara_posterior_sai_do_filtro():
    tj, p = _processo_tjma('0000009-00.2026.8.10.0001')
    agora = timezone.now()
    # Expedição publicada há 60 dias...
    _mov(p, tj, 'm1', agora - timedelta(days=60),
         'Expedição de precatório/rpv',
         'Determinada expedição de precatório em favor do exequente. '
         'Precatório expedido ao presidente do tribunal.')
    # ...e alvará de levantamento publicado DEPOIS (crédito em pagamento).
    _mov(p, tj, 'm2', agora - timedelta(days=5),
         'Intimação',
         'Expeça-se alvará judicial de levantamento em favor da parte '
         'exequente da quantia depositada.')

    cat, score = classificar_e_persistir(p, registrar_log=False)
    assert cat == Process.CLASSIF_NAO_LEAD
    assert score < 0.70

    p.refresh_from_db()
    assert p.classificacao == 'NAO_LEAD'

    # Espelha o filtro de api/leads.py::listar_leads (nivel + min_score).
    leads = Process.objects.filter(classificacao='PRECATORIO',
                                   classificacao_score__gte=0.70,
                                   tribunal_id='TJMA')
    assert p not in leads


def test_precatorio_tjma_alvara_antigo_expedicao_nova_mantem_lead():
    # Alvará antigo (verba acessória) + expedição NOVA → lead vivo.
    tj, p = _processo_tjma('0000010-00.2026.8.10.0001')
    agora = timezone.now()
    _mov(p, tj, 'm1', agora - timedelta(days=400),
         'Intimação',
         'Expeça-se alvará judicial de levantamento dos honorários '
         'periciais depositados.')
    _mov(p, tj, 'm2', agora - timedelta(days=3),
         'Expedição de precatório/rpv',
         'Determinada expedição de precatório em favor do exequente. '
         'Precatório expedido.')

    cat, score = classificar_e_persistir(p, registrar_log=False)
    assert cat == Process.CLASSIF_PRECATORIO
    assert score >= 0.70
