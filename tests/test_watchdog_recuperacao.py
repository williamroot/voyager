"""Dia de recuperação que falhou tem que voltar sozinho.

CONTEXTO (19/08/2026). O watchdog já cuidava do fluxo diário e do backfill por
tribunal, mas NÃO dos dias avulsos da recuperação do acervo — os
`reprocessar_janela` que vêm da varredura dos 59 tribunais.

Um deles que falhasse por deadlock, 403 ou timeout ficava `failed` para sempre.
Ninguém reenfileirava, e ninguém via: são 4 linhas no meio de milhares de runs.
Medido no dia: 4 dias do TJSP parados assim, cada um valendo ~176 mil
publicações. Não é o dado que se perde — é o trabalho.

O que estes testes protegem:
  1. dia falho e órfão volta pra fila;
  2. dia que JÁ foi refeito depois da falha não volta (senão o watchdog
     reprocessa a base inteira toda hora);
  3. dia já na fila, agendado ou em execução não é duplicado;
  4. o teto por tique existe e, quando batido, é ERRO com o número real —
     "devolvi 200" sem dizer que havia 5.000 é o corte mudo de novo.
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

from djen import jobs as J


@pytest.fixture
def trib(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJ São Paulo', 'sigla_djen': 'TJSP'})
    return t


def _run(trib, dia, status, quando=None):
    from django.utils import timezone

    from tribunals.models import IngestionRun
    r = IngestionRun.objects.create(
        tribunal=trib, fonte='djen', status=status,
        janela_inicio=dia, janela_fim=dia,
    )
    if quando:
        IngestionRun.objects.filter(pk=r.pk).update(started_at=quando)
    else:
        IngestionRun.objects.filter(pk=r.pk).update(started_at=timezone.now())
    return r


@pytest.fixture
def fila():
    f = MagicMock()
    f.job_ids = []
    with patch('django_rq.get_queue', return_value=f), \
         patch('rq.registry.ScheduledJobRegistry') as sched, \
         patch('rq.registry.StartedJobRegistry') as start:
        sched.return_value.get_job_ids.return_value = []
        start.return_value.get_job_ids.return_value = []
        yield f


@pytest.mark.django_db
def test_dia_falho_orfao_volta(trib, fila):
    dia = datetime.date(2025, 8, 7)
    _run(trib, dia, 'failed')
    n = J.ressuscitar_dias_de_recuperacao()

    assert n == 1
    assert fila.enqueue.call_count == 1
    args = fila.enqueue.call_args
    assert args.args[1:4] == ('TJSP', '2025-08-07', '2025-08-07')
    assert args.kwargs['job_id'] == 'f2:TJSP:2025-08-07'


@pytest.mark.django_db
def test_dia_ja_refeito_nao_volta(trib, fila):
    """Sem isto o watchdog reprocessaria a base inteira a cada tique."""
    from django.utils import timezone
    dia = datetime.date(2025, 8, 7)
    agora = timezone.now()
    _run(trib, dia, 'failed', quando=agora - datetime.timedelta(hours=3))
    _run(trib, dia, 'success', quando=agora - datetime.timedelta(hours=1))

    assert J.ressuscitar_dias_de_recuperacao() == 0
    assert not fila.enqueue.called


@pytest.mark.django_db
def test_sucesso_ANTES_da_falha_nao_conta(trib, fila):
    """Sucesso velho seguido de falha nova = o dia quebrou de novo."""
    from django.utils import timezone
    dia = datetime.date(2025, 8, 7)
    agora = timezone.now()
    _run(trib, dia, 'success', quando=agora - datetime.timedelta(hours=5))
    _run(trib, dia, 'failed', quando=agora - datetime.timedelta(hours=1))

    assert J.ressuscitar_dias_de_recuperacao() == 1


@pytest.mark.django_db
def test_dia_ja_na_fila_nao_duplica(trib, fila):
    dia = datetime.date(2025, 8, 7)
    _run(trib, dia, 'failed')
    fila.job_ids = ['f2:TJSP:2025-08-07']

    assert J.ressuscitar_dias_de_recuperacao() == 0
    assert not fila.enqueue.called


@pytest.mark.django_db
def test_dia_adiado_nao_duplica(trib, fila):
    """O adiado do circuito volta sozinho — o watchdog não pode empurrar outro."""
    dia = datetime.date(2025, 8, 7)
    _run(trib, dia, 'failed')
    fila.job_ids = ['adiado:TJSP:2025-08-07']

    assert J.ressuscitar_dias_de_recuperacao() == 0


@pytest.mark.django_db
def test_janela_de_varios_dias_e_ignorada(trib, fila):
    """Chunk de 30 dias é do backfill antigo, tem watchdog próprio."""
    from tribunals.models import IngestionRun
    IngestionRun.objects.create(
        tribunal=trib, fonte='djen', status='failed',
        janela_inicio=datetime.date(2025, 8, 1), janela_fim=datetime.date(2025, 8, 30))

    assert J.ressuscitar_dias_de_recuperacao() == 0


@pytest.mark.django_db
def test_teto_por_tique_grita_com_o_numero_real(trib, fila):
    """Devolver 200 sem dizer que havia 5.000 é o corte mudo de novo."""
    for i in range(J.RECUP_POR_TIQUE + 12):
        _run(trib, datetime.date(2025, 1, 1) + datetime.timedelta(days=i), 'failed')

    with patch.object(J.logger, 'error') as erro:
        n = J.ressuscitar_dias_de_recuperacao()

    assert n == J.RECUP_POR_TIQUE
    assert fila.enqueue.call_count == J.RECUP_POR_TIQUE
    assert erro.called, 'bateu o teto em silêncio'
    assert J.RECUP_POR_TIQUE + 12 in erro.call_args.args, 'não disse quantos havia'
