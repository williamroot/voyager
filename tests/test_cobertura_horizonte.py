import pytest
from datetime import date, timedelta, datetime, timezone
from tribunals.models import Tribunal, IngestionRun
from djen.jobs import _dia_coberto, _dias_cobertos

def _run(t, d, **kw):
    return IngestionRun.objects.create(
        tribunal=t, status=IngestionRun.STATUS_SUCCESS,
        janela_inicio=d, janela_fim=d,
        movimentacoes_novas=kw.get('novas', 0),
        movimentacoes_duplicadas=kw.get('dup', 0),
        paginas_lidas=kw.get('pag', 0),
        finished_at=datetime(d.year, d.month, d.day, 3, tzinfo=timezone.utc))

@pytest.mark.django_db
def test_horizonte_recente_empty_nao_cobre_mas_antigo_cobre():
    t = Tribunal.objects.create(sigla='TCH', sigla_djen='TCH', nome='T',
                                ativo=True, overlap_dias=3)
    hoje = date.today()
    recente_vazio = hoje - timedelta(days=1)
    recente_cheio = hoje - timedelta(days=2)
    antigo_vazio = hoje - timedelta(days=400)
    _run(t, recente_vazio)
    _run(t, recente_cheio, novas=10)
    _run(t, antigo_vazio)
    assert _dia_coberto(t, recente_vazio) is False
    assert _dia_coberto(t, recente_cheio) is True
    assert _dia_coberto(t, antigo_vazio) is True
    cob = _dias_cobertos(t, antigo_vazio, hoje)
    assert recente_vazio not in cob
    assert recente_cheio in cob
    assert antigo_vazio in cob

@pytest.mark.django_db
def test_recente_empty_mais_cheio_cobre():
    t = Tribunal.objects.create(sigla='TC2', sigla_djen='TC2', nome='T',
                                ativo=True, overlap_dias=3)
    d = date.today() - timedelta(days=1)
    _run(t, d)
    _run(t, d, dup=5)
    assert _dia_coberto(t, d) is True
