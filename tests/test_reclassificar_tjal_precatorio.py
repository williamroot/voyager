from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from tribunals.models import Process, Tribunal


@pytest.mark.django_db
def test_dry_run_conta_so_cumprimentos_tjal():
    tj_al, _ = Tribunal.objects.get_or_create(sigla='TJAL')
    tj_sp, _ = Tribunal.objects.get_or_create(sigla='TJSP')
    # cumprimento TJAL (alvo)
    Process.objects.create(tribunal=tj_al, classe_codigo='156',
                           numero_cnj='0000001-00.2026.8.02.0001')
    Process.objects.create(tribunal=tj_al, classe_codigo='12078',
                           numero_cnj='0000002-00.2026.8.02.0001')
    # classe fora do conjunto de cumprimento (não-alvo)
    Process.objects.create(tribunal=tj_al, classe_codigo='1265',
                           numero_cnj='0000003-00.2026.8.02.9003')
    # cumprimento de outro tribunal (não-alvo)
    Process.objects.create(tribunal=tj_sp, classe_codigo='156',
                           numero_cnj='0000004-00.2026.8.26.0001')

    out = StringIO()
    call_command('reclassificar_tjal_precatorio', '--dry-run', stdout=out)
    assert 'alvo=2' in out.getvalue()
