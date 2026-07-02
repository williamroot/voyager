from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tribunals.models import Process, Tribunal


@pytest.mark.django_db
def test_dry_run_conta_so_cumprimentos_tjma():
    tj_ma, _ = Tribunal.objects.get_or_create(sigla='TJMA')
    tj_al, _ = Tribunal.objects.get_or_create(sigla='TJAL')
    # Cumprimentos TJMA de QUALQUER classificação são alvo (o expedido pode
    # estar diluído em NAO_LEAD/DC/sem classificação).
    Process.objects.create(tribunal=tj_ma, classe_codigo='12078',
                           numero_cnj='0000001-00.2026.8.10.0001',
                           classificacao=Process.CLASSIF_NAO_LEAD)
    Process.objects.create(tribunal=tj_ma, classe_codigo='156',
                           numero_cnj='0000002-00.2026.8.10.0001',
                           classificacao=Process.CLASSIF_PRE_PRECATORIO)
    # Não-Cumprimento TJMA (fora)
    Process.objects.create(tribunal=tj_ma, classe_codigo='7',
                           numero_cnj='0000003-00.2026.8.10.0001')
    # Cumprimento de outro tribunal (fora)
    Process.objects.create(tribunal=tj_al, classe_codigo='12078',
                           numero_cnj='0000004-00.2026.8.02.0001')

    out = StringIO()
    call_command('reclassificar_tjma_expedicao', '--dry-run', stdout=out)
    assert 'alvo=2' in out.getvalue()


@pytest.mark.django_db
def test_apply_enfileira_batches():
    tj_ma, _ = Tribunal.objects.get_or_create(sigla='TJMA')
    for i in range(3):
        Process.objects.create(tribunal=tj_ma, classe_codigo='12078',
                               numero_cnj=f'000000{i}-01.2026.8.10.0001')
    fake_q = type('Q', (), {'enqueued': []})()
    fake_q.enqueue = lambda fn, pids: fake_q.enqueued.append(pids)
    out = StringIO()
    with patch('django_rq.get_queue', return_value=fake_q):
        call_command('reclassificar_tjma_expedicao', '--apply',
                     '--batch-size', '2', stdout=out)
    assert len(fake_q.enqueued) == 2
    assert sum(len(b) for b in fake_q.enqueued) == 3


@pytest.mark.django_db
def test_exige_dry_run_ou_apply():
    with pytest.raises(CommandError):
        call_command('reclassificar_tjma_expedicao')
