import pytest
from datetime import date, datetime, timezone
from django.db import connection
from tribunals.models import Tribunal, Process

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
