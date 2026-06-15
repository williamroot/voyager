from __future__ import annotations

import pytest
from django.utils import timezone

from tribunals.classificador import classificar_e_persistir
from tribunals.models import Movimentacao, Process, Tribunal


@pytest.mark.django_db
def test_cumprimento_com_oficio_persiste_n1_e_entra_no_filtro():
    tj = Tribunal.objects.get_or_create(sigla='TJAL')[0]
    p = Process.objects.create(tribunal=tj, classe_codigo='156',
                               numero_cnj='0000009-00.2026.8.02.0001')
    # Movimento que dispara F14 (texto ~ 'ofício requisitório').
    Movimentacao.objects.create(
        processo=p, tribunal=tj, external_id='m1',
        data_disponibilizacao=timezone.now(),
        tipo_comunicacao='Expedição de documento',
        texto='Expedido Ofício Requisitório de Precatório ao ente público.')

    cat, score = classificar_e_persistir(p, registrar_log=False)
    assert cat == Process.CLASSIF_PRECATORIO
    assert score == 1.0

    p.refresh_from_db()
    assert p.classificacao == 'PRECATORIO'
    assert p.classificacao_score == 1.0

    # Espelha o filtro de api/leads.py::listar_leads (nivel + min_score).
    leads = Process.objects.filter(classificacao='PRECATORIO',
                                   classificacao_score__gte=0.70,
                                   tribunal_id='TJAL')
    assert p in leads
