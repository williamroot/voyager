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
                                ativo=True, overlap_dias=14)
    hoje = date.today()
    # Dia útil recente vazio: recua até achar um weekday dentro do horizonte.
    # Garante determinismo independente de qual dia da semana cai hoje.
    recente_vazio = hoje - timedelta(days=1)
    while recente_vazio.weekday() >= 5:
        recente_vazio -= timedelta(days=1)
    # Dia útil recente cheio: um weekday antes do recente_vazio
    recente_cheio = recente_vazio - timedelta(days=1)
    while recente_cheio.weekday() >= 5:
        recente_cheio -= timedelta(days=1)
    antigo_vazio = hoje - timedelta(days=400)
    _run(t, recente_vazio)
    _run(t, recente_cheio, novas=10)
    _run(t, antigo_vazio)
    assert _dia_coberto(t, recente_vazio) is False   # dia útil recente vazio: não coberto
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
    # força dia útil recente para evitar falso positivo quando hoje é fds
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    _run(t, d)
    _run(t, d, dup=5)
    assert _dia_coberto(t, d) is True


@pytest.mark.django_db
def test_fim_de_semana_recente_vazio_continua_coberto():
    t = Tribunal.objects.create(sigla='TWE', sigla_djen='TWE', nome='T',
                                ativo=True, overlap_dias=14)
    hoje = date.today()
    # Saturday within horizon, deterministic: large overlap, pick last Saturday
    d = hoje - timedelta(days=1)
    while d.weekday() != 5:           # 5 = Saturday
        d -= timedelta(days=1)
    fds = d                          # a Saturday, <= 7 days ago, within 14-day horizon
    _run(t, fds)  # success vazio num fim de semana recente
    assert _dia_coberto(t, fds) is True            # fds vazio = coberto (DJEN nao publica)
    cob = _dias_cobertos(t, hoje - timedelta(days=10), hoje)
    assert fds in cob
