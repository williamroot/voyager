import pytest
from datetime import date, datetime, timezone
from django.db import connection
from django.contrib.auth import get_user_model
from tribunals.models import Tribunal, Process, IngestionRun
from dashboard.queries import _classificar_celula, pipeline_saude_grid, pipeline_volume_temporal, pipeline_kpis

@pytest.mark.django_db(transaction=True)
def test_mv_pipeline_diario_popula_tres_fontes():
    t = Tribunal.objects.create(sigla='TST', sigla_djen='TST', nome='Teste', ativo=True)
    dt = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    Process.objects.create(tribunal=t, numero_cnj='1', data_enriquecimento_datajud=dt)
    Process.objects.create(tribunal=t, numero_cnj='2', enriquecido_em=dt)
    Process.objects.create(tribunal=t, numero_cnj='3', classificacao_em=dt)
    with connection.cursor() as c:
        c.execute('REFRESH MATERIALIZED VIEW mv_pipeline_diario')
        c.execute("SELECT fonte, processos FROM mv_pipeline_diario "
                  "WHERE tribunal_id=%s AND dia=%s ORDER BY fonte", [t.pk, date(2026,5,15)])
        rows = dict(c.fetchall())
    assert rows == {'classif': 1, 'datajud': 1, 'pje': 1}


def test_classificar_celula():
    base = [100, 100, 100, 100]
    assert _classificar_celula(90, base, dia_util=True) == 'verde'
    assert _classificar_celula(40, base, dia_util=True) == 'amarelo'
    assert _classificar_celula(5, base, dia_util=True) == 'vermelho'
    assert _classificar_celula(0, base, dia_util=False) == 'cinza'
    assert _classificar_celula(0, [], dia_util=True) == 'cinza'


@pytest.mark.django_db
def test_pipeline_saude_grid_djen_usa_max_nao_sum():
    t = Tribunal.objects.create(sigla='TSU', sigla_djen='TSU', nome='T', ativo=True)
    d = date(2026, 5, 15)
    for novas in (10, 12):
        IngestionRun.objects.create(
            tribunal=t, status=IngestionRun.STATUS_SUCCESS,
            janela_inicio=d, janela_fim=d, movimentacoes_novas=novas,
            movimentacoes_duplicadas=3, paginas_lidas=2,
            finished_at=datetime(2026, 5, 15, 3, tzinfo=timezone.utc))
    grid = pipeline_saude_grid(dias=3650, tribunais=[t.pk])
    djen = [c for c in grid
            if c['tribunal_id'] == t.pk and c['fonte'] == 'djen' and c['dia'] == d][0]
    assert djen['novas'] == 12          # MAX(10,12), nao 22
    assert djen['encontradas'] == 15    # MAX(novas+dup) = 12+3


@pytest.mark.django_db
def test_pipeline_volume_temporal_e_kpis():
    t = Tribunal.objects.create(sigla='TVK', sigla_djen='TVK', nome='T', ativo=True)
    d = date(2026, 5, 15)
    IngestionRun.objects.create(
        tribunal=t, status=IngestionRun.STATUS_SUCCESS, janela_inicio=d,
        janela_fim=d, movimentacoes_novas=50, movimentacoes_duplicadas=0,
        paginas_lidas=5, finished_at=datetime(2026, 5, 15, 3, tzinfo=timezone.utc))
    serie = pipeline_volume_temporal(dias=3650, tribunais=[t.pk])
    pontos = [p for p in serie if p['fonte'] == 'djen' and p['dia'] == d]
    assert pontos and pontos[0]['volume'] == 50
    k = pipeline_kpis(tribunais=[t.pk])
    assert 'ultima_ingestao_djen' in k and 'anomalias_24h' in k


@pytest.mark.django_db
def test_ingestao_saude_view_200(client, settings):
    settings.STORAGES = {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    }
    u = get_user_model().objects.create_user('w', password='x')
    client.force_login(u)
    resp = client.get('/dashboard/ingestao/saude/')
    assert resp.status_code == 200
