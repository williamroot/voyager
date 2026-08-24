"""O dia que fecha `success` fechou ÍNTEGRO? — gate de completude (24/08/2026).

CONTEXTO. As três perdas do CLAUDE.md tinham a MESMA assinatura: run verde, log
limpo, número redondo. `for pagina in range(1, 11)` serviu 43,6% do TJSP como
`success` por 17 meses. Até 24/08/2026 nada no sistema comparava sozinho o que
o dia trouxe com o que a fonte declara — a conferência existia só como comando
manual, e comando manual é coisa que ninguém roda.

O que estes testes protegem:

  1. dia que bate com a fonte fecha calado (falso alarme a cada 5 min gastaria
     o alerta em uma tarde);
  2. dia que NÃO bate vira ERRO no `run.erros` **e** linha de log com os dois
     números — nunca exceção, nunca `return` discreto;
  3. `count` no teto de 10.000 é PISO, não total (regra nº 3): sem paginar, o
     gate se ABSTÉM em vez de declarar "10.000 = 10.000" (regra nº 6);
  4. o dia grande vai pro caminho caro (paginação), racionado por tique;
  5. circuito aberto não é veredito: não marca nada, pra voltar no próximo tique.

E o zumbi: "running há >1h ⇒ worker crashou" é falso justamente no dia grande.
MEDIDO em 24/08/2026: de 170 runs de 1 dia que fecharam `success` em 12 h, 11
(6,5%) passaram de 60 min, o pior um TJSP de 357 min — todos marcados "worker
crashou" com o worker trabalhando.
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from djen import jobs as J


@pytest.fixture
def trib(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJDFT', defaults={'nome': 'TJ do DF', 'sigla_djen': 'TJDFT'})
    return t


def _run_ok(trib, dia, novas=0, dup=0, status='success'):
    from tribunals.models import IngestionRun
    r = IngestionRun.objects.create(
        tribunal=trib, fonte='djen', status=status,
        janela_inicio=dia, janela_fim=dia,
        movimentacoes_novas=novas, movimentacoes_duplicadas=dup,
    )
    IngestionRun.objects.filter(pk=r.pk).update(finished_at=timezone.now())
    r.refresh_from_db()
    return r


@pytest.fixture(autouse=True)
def _cache_limpo():
    """Marca de conferência vive no Redis; teste não pode herdar a do vizinho.

    Apaga só as CHAVES deste teste — `cache.clear()` em produção é proibido
    (OPS §"NUNCA rodar cache.clear()") e o hábito começa no teste.
    """
    from django.core.cache import cache
    yield
    for sigla in ('TJDFT',):
        for d in ('2026-08-21', '2026-08-20'):
            cache.delete(J._marca_gate(sigla, d))


# ────────────────────────────────────────────────────────── veredito por dia

@pytest.mark.django_db
def test_dia_que_bate_com_a_fonte_fecha_calado(trib):
    dia = datetime.date(2026, 8, 20)
    _run_ok(trib, dia, novas=900, dup=100)
    cliente = MagicMock()
    cliente.count_window.return_value = 1000
    with patch.object(J, 'DJENClient', return_value=cliente), \
         patch.object(J.logger, 'error') as erro:
        r = J.conferir_dia_contra_fonte('TJDFT', dia.isoformat())

    assert r['veredito'] == 'integro'
    assert r['gap'] == 0
    assert not erro.called, 'alarme em dia íntegro gasta o alerta'


@pytest.mark.django_db
def test_divergencia_vira_erro_no_run_e_no_log(trib):
    """O dia trouxe 6.000, a fonte declara 9.500 — 3.500 publicações sumidas."""
    dia = datetime.date(2026, 8, 20)
    run = _run_ok(trib, dia, novas=5_000, dup=1_000)
    cliente = MagicMock()
    cliente.count_window.return_value = 9_500
    with patch.object(J, 'DJENClient', return_value=cliente), \
         patch.object(J.logger, 'error') as erro:
        r = J.conferir_dia_contra_fonte('TJDFT', dia.isoformat())

    assert r['veredito'] == 'divergente'
    assert r['gap'] == 3_500
    run.refresh_from_db()
    achados = [e for e in run.erros
               if isinstance(e, dict) and e.get('erro') == 'cobertura_divergente']
    assert len(achados) == 1, 'achado tem que ficar no run, não só no log'
    assert achados[0]['fonte'] == 9_500 and achados[0]['run'] == 6_000
    assert erro.called, 'divergência tem que gritar'

    # rodar de novo não duplica o achado (o watchdog roda a cada 5 min)
    with patch.object(J, 'DJENClient', return_value=cliente), \
         patch.object(J.logger, 'error'):
        J.conferir_dia_contra_fonte('TJDFT', dia.isoformat())
    run.refresh_from_db()
    assert sum(1 for e in run.erros
               if isinstance(e, dict) and e.get('erro') == 'cobertura_divergente') == 1


@pytest.mark.django_db
def test_count_no_teto_de_10k_faz_o_gate_se_abster(trib):
    """10.000 é PISO disfarçado. Declarar 'bateu' aqui seria inventar prova."""
    dia = datetime.date(2026, 8, 21)
    _run_ok(trib, dia, novas=10_000, dup=0)
    cliente = MagicMock()
    cliente.count_window.return_value = 10_000
    with patch.object(J, 'DJENClient', return_value=cliente), \
         patch.object(J.logger, 'error') as erro:
        r = J.conferir_dia_contra_fonte('TJDFT', dia.isoformat(), paginar=False)

    assert r['veredito'] == 'indecidivel_no_teto_de_10k'
    assert 'gap' not in r
    assert not erro.called


@pytest.mark.django_db
def test_dia_grande_paginado_confronta_a_fonte_de_verdade(trib):
    """O molde real: TJDFT 2026-08-21, 14.651 de um lado e 14.651 do outro."""
    dia = datetime.date(2026, 8, 21)
    _run_ok(trib, dia, novas=14_000, dup=651)
    cliente = MagicMock()
    cliente.count_window.return_value = 10_000            # teto
    ids = {str(i) for i in range(14_651)}
    with patch.object(J, 'DJENClient', return_value=cliente), \
         patch('djen.management.commands.djen_provar_dias.paginar_forca_bruta',
               return_value={'ids': ids, 'requisicoes': 59}), \
         patch.object(J.logger, 'error') as erro:
        r = J.conferir_dia_contra_fonte('TJDFT', dia.isoformat(), paginar=True)

    assert r['paginado'] is True
    assert r['fonte_paginada'] == 14_651
    assert r['veredito'] == 'integro' and r['gap'] == 0
    assert not erro.called


@pytest.mark.django_db
def test_circuito_aberto_nao_e_veredito(trib):
    """Sem marca no Redis: o dia volta pro gate no próximo tique."""
    from django.core.cache import cache

    from djen.client import DjenBusyError
    dia = datetime.date(2026, 8, 20)
    _run_ok(trib, dia, novas=10, dup=0)
    cliente = MagicMock()
    cliente.count_window.side_effect = DjenBusyError('circuito aberto')
    with patch.object(J, 'DJENClient', return_value=cliente):
        r = J.conferir_dia_contra_fonte('TJDFT', dia.isoformat())

    assert r['skip'] == 'djen_circuito_aberto'
    assert cache.get(J._marca_gate('TJDFT', dia.isoformat())) is None


# ─────────────────────────────────────────────────── seleção por tique

@pytest.mark.django_db
def test_tique_raciona_o_caro_e_nao_repete_o_ja_conferido(trib):
    """20 baratos e 1 caro por tique — e dia marcado no Redis não volta."""
    from django.core.cache import cache
    fila = MagicMock()
    _run_ok(trib, datetime.date(2026, 8, 20), novas=500)            # barato
    _run_ok(trib, datetime.date(2026, 8, 21), novas=14_651)         # caro (>=10k)
    with patch('django_rq.get_queue', return_value=fila):
        resumo = {}
        n = J.conferir_dias_fechados(resumo=resumo)

    assert n == 2 and resumo['baratos'] == 1 and resumo['caros'] == 1
    chamadas = {c.args[1]: c.args[3] for c in fila.enqueue.call_args_list}
    assert chamadas['TJDFT'] in (True, False)      # sigla é args[1]
    assert fila.enqueue.call_count == 2

    # dia já EM CURSO na fila não volta: a marca no Redis só nasce no fim, e a
    # paginada de um dia grande passa dos 5 min do tique — sem isto, o mesmo
    # `job_id` seria reenfileirado por cima do job em execução.
    fila.reset_mock()
    fila.job_ids = ['gate:TJDFT:2026-08-20', 'gate:TJDFT:2026-08-21']
    with patch('django_rq.get_queue', return_value=fila):
        assert J.conferir_dias_fechados() == 0
    assert not fila.enqueue.called
    fila.job_ids = []

    # marcado = não volta
    cache.set(J._marca_gate('TJDFT', '2026-08-20'), 'ok', 60)
    cache.set(J._marca_gate('TJDFT', '2026-08-21'), 'cap', 60)
    fila.reset_mock()
    with patch('django_rq.get_queue', return_value=fila):
        assert J.conferir_dias_fechados() == 0
    assert not fila.enqueue.called


# ──────────────────────────────────────────────────────────── zumbi falso

@pytest.mark.django_db
def test_run_longo_com_job_vivo_nao_e_zumbi(trib):
    """357 min é duração real de dia do TJSP — não é worker crashado."""
    from tribunals.models import IngestionRun
    dia = datetime.date(2026, 8, 20)
    r = IngestionRun.objects.create(tribunal=trib, fonte='djen', status='running',
                                    janela_inicio=dia, janela_fim=dia)
    IngestionRun.objects.filter(pk=r.pk).update(
        started_at=timezone.now() - datetime.timedelta(hours=3))
    with patch.object(J, '_dias_com_job_vivo',
                      return_value={('TJDFT', dia.isoformat())}):
        matados, poupados = J._matar_zumbis(timezone.now())

    assert (matados, poupados) == (0, 1)
    r.refresh_from_db()
    assert r.status == 'running'


@pytest.mark.django_db
def test_zumbi_de_verdade_morre_sem_apagar_a_evidencia(trib):
    """O `erros` do run é APPEND: o teto de bytes daquele dia tem que sobreviver."""
    from tribunals.models import IngestionRun
    dia = datetime.date(2026, 8, 20)
    r = IngestionRun.objects.create(
        tribunal=trib, fonte='djen', status='running',
        janela_inicio=dia, janela_fim=dia,
        erros=[{'erro': 'resposta_acima_do_teto_de_bytes', 'bytes': 50_462_021}])
    IngestionRun.objects.filter(pk=r.pk).update(
        started_at=timezone.now() - datetime.timedelta(hours=3))
    with patch.object(J, '_dias_com_job_vivo', return_value=set()):
        matados, poupados = J._matar_zumbis(timezone.now())

    assert (matados, poupados) == (1, 0)
    r.refresh_from_db()
    assert r.status == 'failed'
    assert r.erros[0]['erro'] == 'resposta_acima_do_teto_de_bytes', 'evidência apagada'
    assert r.erros[-1] == J.MSG_ZUMBI


@pytest.mark.django_db
def test_job_vivo_nao_protege_para_sempre(trib):
    """Passado o `job_timeout` do RQ, nem o RQ acredita mais no job."""
    from tribunals.models import IngestionRun
    dia = datetime.date(2026, 8, 20)
    r = IngestionRun.objects.create(tribunal=trib, fonte='djen', status='running',
                                    janela_inicio=dia, janela_fim=dia)
    IngestionRun.objects.filter(pk=r.pk).update(
        started_at=timezone.now() - datetime.timedelta(hours=9))
    with patch.object(J, '_dias_com_job_vivo',
                      return_value={('TJDFT', dia.isoformat())}):
        matados, poupados = J._matar_zumbis(timezone.now())

    assert (matados, poupados) == (1, 0)
