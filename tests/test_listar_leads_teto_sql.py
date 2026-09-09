"""A listagem de leads falha ALTO quando o banco demora — nunca em silêncio.

Incidente de 09/09/2026: `?nivel=PRE_PRECATORIO&tribunal=TJSP` entrava num plano
que caminhava 31 M de linhas, o worker do gunicorn era morto pelo arbiter aos
30 s e o Juriscope fechava o dia com `n2: 0` no TJSP. Sem `statement_timeout`, a
consulta ainda continuava rodando no Postgres depois que o cliente sumiu.

Dois contratos aqui:

1. o SQL da listagem roda sob `statement_timeout` de verdade (não adianta
   chamar `SET` fora da transação: o pgbouncer roda em transaction-mode);
2. estouro vira **503**, não 500 — para o Juriscope 503 é transitório, ele pula
   a rodada e volta na próxima, em vez de tratar como bug permanente.

O índice que conserta o PLANO é o `proc_leads_api_idx` (migration 0061); estes
testes cobrem o comportamento quando, apesar dele, alguma consulta passar do
teto.
"""
import pytest
from django.db import OperationalError, connection
from django.test import Client, override_settings

from api import leads as leads_view
from tribunals.models import ApiClient, Process, Tribunal


@pytest.fixture
def cliente(db):
    return ApiClient.objects.create(nome='falcon-teto', api_key='k-teto',
                                    ativo=True)


@pytest.fixture
def tribunal(db):
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    return t


@pytest.mark.django_db
@override_settings(LEADS_SQL_TIMEOUT_SECONDS=7)
def test_statement_timeout_vale_na_mesma_transacao(tribunal):
    """O `SET LOCAL` tem que alcançar a query — é isso que o pgbouncer quebra
    quando o `SET` sai solto, fora da transação."""
    visto = {}

    class QsEspiao:
        """Finge ser o queryset: ao ser fatiado, lê o timeout vigente."""

        def __getitem__(self, _fatia):
            with connection.cursor() as cur:
                cur.execute('SHOW statement_timeout')
                visto['timeout'] = cur.fetchone()[0]
            return []

    leads_view._linhas_com_teto(QsEspiao(), limit=3, segundos=7)
    assert visto['timeout'] == '7s'


@pytest.mark.django_db
def test_teto_volta_a_zero_depois_da_transacao(tribunal):
    """A conexão é reaproveitada (pgbouncer + CONN_MAX_AGE): o teto não pode
    vazar para a próxima requisição, que pode ser um relatório legítimo."""

    class QsVazio:
        def __getitem__(self, _fatia):
            return []

    leads_view._linhas_com_teto(QsVazio(), limit=3, segundos=7)
    with connection.cursor() as cur:
        cur.execute('SHOW statement_timeout')
        assert cur.fetchone()[0] != '7s'


@pytest.mark.django_db
@override_settings(LEADS_SQL_TIMEOUT_SECONDS=13)
def test_consulta_estourada_responde_503(cliente, tribunal, monkeypatch):
    """503 e não 500: o cliente trata como transitório e tenta de novo."""
    def estoura(*_a, **_kw):
        raise OperationalError('canceling statement due to statement timeout')

    monkeypatch.setattr(leads_view, '_linhas_com_teto', estoura)

    resp = Client().get('/api/v1/leads/?nivel=PRE_PRECATORIO&tribunal=TJSP'
                        '&limit=3&min_score=0.5', HTTP_X_API_KEY='k-teto')

    assert resp.status_code == 503
    corpo = resp.json()
    assert '13s' in corpo['erro']
    assert corpo['tribunal'] == 'TJSP'


@pytest.mark.django_db
def test_caminho_normal_segue_200(cliente, tribunal):
    """O teto não pode custar nada a quem responde rápido."""
    Process.objects.create(
        tribunal=tribunal, numero_cnj='1-1.2024.8.26.0100',
        classificacao=Process.CLASSIF_PRE_PRECATORIO,
        classificacao_score=0.9)

    resp = Client().get('/api/v1/leads/?nivel=PRE_PRECATORIO&tribunal=TJSP'
                        '&limit=3&min_score=0.5', HTTP_X_API_KEY='k-teto')

    assert resp.status_code == 200
    assert [r['cnj'] for r in resp.json()['results']] == ['1-1.2024.8.26.0100']
