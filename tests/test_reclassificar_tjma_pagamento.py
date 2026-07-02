from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from tribunals.models import Process, Tribunal


@pytest.mark.django_db
def test_dry_run_conta_so_leads_tjma():
    tj_ma, _ = Tribunal.objects.get_or_create(sigla='TJMA')
    tj_mg, _ = Tribunal.objects.get_or_create(sigla='TJMG')
    # leads TJMA (alvo: N1 e N2)
    Process.objects.create(tribunal=tj_ma, classe_codigo='12078',
                           numero_cnj='0000001-00.2026.8.10.0001',
                           classificacao=Process.CLASSIF_PRECATORIO)
    Process.objects.create(tribunal=tj_ma, classe_codigo='156',
                           numero_cnj='0000002-00.2026.8.10.0001',
                           classificacao=Process.CLASSIF_PRE_PRECATORIO)
    # NAO_LEAD do TJMA (não-alvo: regra só rebaixa quem é lead)
    Process.objects.create(tribunal=tj_ma, classe_codigo='156',
                           numero_cnj='0000003-00.2026.8.10.0001',
                           classificacao=Process.CLASSIF_NAO_LEAD)
    # lead de outro tribunal (não-alvo)
    Process.objects.create(tribunal=tj_mg, classe_codigo='156',
                           numero_cnj='0000004-00.2026.8.13.0001',
                           classificacao=Process.CLASSIF_PRECATORIO)

    out = StringIO()
    call_command('reclassificar_tjma_pagamento', '--dry-run', stdout=out)
    assert 'alvo=2' in out.getvalue()
