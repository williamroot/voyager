"""Cache pré-calculado da listagem de leads (`api/leads_cache.py`).

Contexto (13/10/2026): o pull das 08:00 do Juriscope falhava com 503 em 10/13
dias no TRF3 e 12/13 no TJSP, porque a consulta ao vivo passava dos 20 s frio.
O job `precomputar_leads_cache` roda a MESMA consulta fora da requisição e a API
serve do Redis enquanto o cálculo é recente.

Contratos cobertos aqui:
1. o pré-cálculo grava exatamente os ids que a consulta ao vivo devolveria, na
   mesma ordem, e uma combinação que falha não derruba as outras;
2. o cache serve e TIRA o que foi consumido depois do cálculo, re-confere a
   linha atual (nível/score/devedor) e respeita `limit` e ordem;
3. cache velho ou ausente cai na consulta ao vivo (`fonte=ao_vivo`), com o 503
   de sempre;
4. esgotar o cache abaixo do `limit` é DITO na resposta — e quando o cache
   estava no teto, sai em WARNING (teto é alerta, nunca corte mudo).
"""
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from django.db import OperationalError
from django.test import Client

from api import leads_cache
from tribunals.models import ApiClient, LeadConsumption, Process, Tribunal

URL = '/api/v1/leads/'


@pytest.fixture(autouse=True)
def redis_limpo():
    """O Redis de teste é compartilhado entre execuções; ids de cliente
    recomeçam a cada banco de teste novo. Cache que sobra de uma execução
    seria servido na próxima."""
    conn = leads_cache._conn()

    def _limpar():
        for k in list(conn.scan_iter('leads:*')):
            conn.delete(k)
        from django.core.cache import cache
        cache.delete(leads_cache._LOCK_KEY)

    _limpar()
    yield
    _limpar()


@pytest.fixture
def cliente(db):
    return ApiClient.objects.create(nome='falcon-cache', api_key='k-cache', ativo=True)


@pytest.fixture
def trf3(db):
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    return t


@pytest.fixture
def tjsp(db):
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    return t


def _mk(t, n, dia, score=0.9, nivel=Process.CLASSIF_PRE_PRECATORIO, **kw):
    return Process.objects.create(
        tribunal=t, numero_cnj=f'{n:07d}-00.2024.4.03.6100',
        classificacao=nivel, classificacao_score=score,
        ultima_movimentacao_em=datetime(2026, 10, dia, tzinfo=UTC), **kw)


def _get(qs: str):
    return Client().get(URL + '?' + qs, HTTP_X_API_KEY='k-cache')


COMBO_TRF3_N2 = leads_cache.chave_combo('PRE_PRECATORIO', 'TRF3', 0.2, False)
QS_TRF3_N2 = 'nivel=PRE_PRECATORIO&tribunal=TRF3&min_score=0.2'


# ── 1. pré-cálculo ──────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_precomputo_grava_os_ids_da_consulta_ao_vivo_na_mesma_ordem(cliente, trf3, tjsp):
    p_novo = _mk(trf3, 1, 10)
    p_meio = _mk(trf3, 2, 5)
    p_velho = _mk(trf3, 3, 1)
    consumido = _mk(trf3, 4, 12)               # mais recente, mas já consumido
    _mk(trf3, 5, 11, score=0.1)               # abaixo do min_score
    _mk(trf3, 6, 11, nivel=Process.CLASSIF_PRECATORIO)   # outro nível
    _mk(tjsp, 7, 11)                           # outro tribunal
    LeadConsumption.objects.create(cliente=cliente, processo=consumido)

    r = leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)

    dado = leads_cache.ler_cache(cliente.id, COMBO_TRF3_N2)
    assert dado['ids'] == [p_novo.pk, p_meio.pk, p_velho.pk]
    assert dado['completo'] is True
    assert r == {'ids': 3, 'completo': True, 'levou_s': r['levou_s']}
    # paridade com a consulta ao vivo (mesmo recorte, mesma ordem)
    vivo = [r['cnj'] for r in _get(QS_TRF3_N2 + '&limit=50').json()['results']]
    assert vivo == [p.numero_cnj for p in (p_novo, p_meio, p_velho)]


@pytest.mark.django_db
def test_precomputo_respeita_devedor_publico(cliente, tjsp):
    fazenda = _mk(tjsp, 1, 10, nivel=Process.CLASSIF_DIREITO_CREDITORIO,
                  classe_codigo='12078')
    pelo_nome = _mk(tjsp, 2, 9, nivel=Process.CLASSIF_DIREITO_CREDITORIO,
                    classe_codigo='999', classe_nome='Execução contra a Fazenda Pública')
    _mk(tjsp, 3, 11, nivel=Process.CLASSIF_DIREITO_CREDITORIO, classe_codigo='156',
        classe_nome='Cumprimento de sentença')
    combo = leads_cache.chave_combo('DIREITO_CREDITORIO', 'TJSP', 0.25, True)

    leads_cache.precomputar_combo(cliente.id, combo)

    assert leads_cache.ler_cache(cliente.id, combo)['ids'] == [fazenda.pk, pelo_nome.pk]


@pytest.mark.django_db
def test_precomputo_marca_incompleto_quando_bate_o_teto(cliente, trf3, monkeypatch):
    monkeypatch.setattr(leads_cache, 'TETO_IDS', 2)
    for i in range(4):
        _mk(trf3, i + 1, 10 - i)

    r = leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)

    assert r['ids'] == 2 and r['completo'] is False


@pytest.mark.django_db
def test_job_combinacao_que_falha_nao_derruba_as_outras(cliente, trf3, tjsp, settings,
                                                         monkeypatch):
    settings.LEADS_CACHE_SEMENTES_TRIBUNAIS = ['TRF3', 'TJSP']
    settings.LEADS_CACHE_SEMENTES_DC_TRIBUNAIS = []
    p = _mk(trf3, 1, 10)
    LeadConsumption.objects.create(cliente=cliente, processo=_mk(trf3, 99, 1))  # elegível
    original = leads_cache.precomputar_combo

    def _as_vezes_estoura(cid, combo):
        if '|TJSP|' in combo and combo.startswith('PRECATORIO'):
            raise OperationalError('canceling statement due to statement timeout')
        return original(cid, combo)

    monkeypatch.setattr(leads_cache, 'precomputar_combo', _as_vezes_estoura)

    rel = leads_cache.precomputar_leads_cache()

    assert rel['falhas'] == 1
    assert rel['ok'] == 3                  # TRF3 N1/N2 + TJSP N2
    assert leads_cache.ler_cache(cliente.id, COMBO_TRF3_N2)['ids'] == [p.pk]
    assert leads_cache.ultima_execucao()['falhas'] == 1


@pytest.mark.django_db
def test_clientes_elegiveis_exige_ativo_e_consumo_recente(cliente, trf3):
    inativo = ApiClient.objects.create(nome='velho', api_key='k-velho', ativo=False)
    parado = ApiClient.objects.create(nome='parado', api_key='k-parado', ativo=True)
    proc = _mk(trf3, 1, 10)
    LeadConsumption.objects.create(cliente=cliente, processo=proc)
    LeadConsumption.objects.create(cliente=inativo, processo=proc)
    antigo = LeadConsumption.objects.create(cliente=parado, processo=proc)
    LeadConsumption.objects.filter(pk=antigo.pk).update(
        consumido_em=datetime.now(UTC) - timedelta(days=45))

    assert leads_cache.clientes_elegiveis() == [cliente.pk]


@pytest.mark.django_db
def test_api_registra_combinacao_e_precomputo_a_usa(cliente, trf3, settings):
    settings.LEADS_CACHE_SEMENTES_TRIBUNAIS = []
    settings.LEADS_CACHE_SEMENTES_DC_TRIBUNAIS = []
    _get('nivel=PRECATORIO&tribunal=TRF1&min_score=0.70&limit=10')

    combos = leads_cache.combos_do_cliente(cliente.id)

    # 0.70 e 0.7 são a mesma combinação; o `limit` não entra na chave
    assert combos == [leads_cache.chave_combo('PRECATORIO', 'TRF1', 0.7, False)]


def test_sementes_cobrem_o_pull_do_juriscope(settings):
    settings.LEADS_CACHE_SEMENTES_TRIBUNAIS = ['TRF3', 'TJSP']
    settings.LEADS_CACHE_SEMENTES_DC_TRIBUNAIS = ['TJSP']
    sementes = leads_cache._sementes()
    assert 'PRECATORIO|TRF3|0.7000|0' in sementes
    assert 'PRE_PRECATORIO|TJSP|0.2000|0' in sementes
    assert 'DIREITO_CREDITORIO|TJSP|0.2500|1' in sementes


# ── 2. servir do cache ──────────────────────────────────────────────────────

@pytest.mark.django_db
def test_cache_serve_e_tira_o_consumido_depois_do_calculo(cliente, trf3):
    a, b, c = _mk(trf3, 1, 10), _mk(trf3, 2, 9), _mk(trf3, 3, 8)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    LeadConsumption.objects.create(cliente=cliente, processo=b)   # consumido DEPOIS

    corpo = _get(QS_TRF3_N2 + '&limit=10').json()

    assert corpo['fonte'] == 'cache'
    assert [r['cnj'] for r in corpo['results']] == [a.numero_cnj, c.numero_cnj]
    assert corpo['cache']['descartados_consumidos'] == 1
    assert corpo['cache_idade_s'] is not None


@pytest.mark.django_db
def test_cache_reconfere_a_linha_atual(cliente, trf3):
    a, b, c = _mk(trf3, 1, 10), _mk(trf3, 2, 9), _mk(trf3, 3, 8)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    Process.objects.filter(pk=a.pk).update(classificacao=Process.CLASSIF_NAO_LEAD)
    Process.objects.filter(pk=c.pk).update(classificacao_score=0.05)

    corpo = _get(QS_TRF3_N2 + '&limit=10').json()

    assert [r['cnj'] for r in corpo['results']] == [b.numero_cnj]
    assert corpo['cache']['descartados_reclassificados'] == 2


@pytest.mark.django_db
def test_cache_respeita_limit_e_ordem(cliente, trf3):
    procs = [_mk(trf3, i + 1, 28 - i) for i in range(6)]
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)

    corpo = _get(QS_TRF3_N2 + '&limit=4').json()

    assert corpo['fonte'] == 'cache'
    assert [r['cnj'] for r in corpo['results']] == [p.numero_cnj for p in procs[:4]]
    assert corpo['count'] == 4
    assert corpo['cache']['esgotado'] is False


@pytest.mark.django_db
def test_cache_esgotado_completo_diz_que_acabou(cliente, trf3):
    _mk(trf3, 1, 10)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)

    corpo = _get(QS_TRF3_N2 + '&limit=50').json()

    assert corpo['count'] == 1
    assert corpo['cache']['esgotado'] is True
    assert corpo['cache']['completo'] is True


@pytest.mark.django_db
def test_cache_esgotado_no_teto_sai_em_warning(cliente, trf3, monkeypatch, caplog):
    monkeypatch.setattr(leads_cache, 'TETO_IDS', 2)
    a, b, _c = _mk(trf3, 1, 10), _mk(trf3, 2, 9), _mk(trf3, 3, 8)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    LeadConsumption.objects.create(cliente=cliente, processo=a)

    # o logger `voyager` não propaga para a raiz: pendura o handler do caplog
    leads_cache.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger='voyager.api.leads_cache'):
            corpo = _get(QS_TRF3_N2 + '&limit=5').json()
    finally:
        leads_cache.logger.removeHandler(caplog.handler)

    assert [r['cnj'] for r in corpo['results']] == [b.numero_cnj]
    assert corpo['cache']['esgotado'] is True
    assert corpo['cache']['completo'] is False
    assert any('esgotou no teto' in r.getMessage() for r in caplog.records)


# ── 3. sem cache recente → ao vivo ──────────────────────────────────────────

@pytest.mark.django_db
def test_sem_cache_cai_na_consulta_ao_vivo(cliente, trf3):
    a = _mk(trf3, 1, 10)

    corpo = _get(QS_TRF3_N2 + '&limit=10').json()

    assert corpo['fonte'] == 'ao_vivo'
    assert corpo['cache'] is None
    assert [r['cnj'] for r in corpo['results']] == [a.numero_cnj]


@pytest.mark.django_db
def test_cache_velho_cai_na_consulta_ao_vivo(cliente, trf3, settings):
    a = _mk(trf3, 1, 10)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    b = _mk(trf3, 2, 11)               # lead novo, depois do cálculo
    conn = leads_cache._conn()
    chave = leads_cache._CACHE_KEY % (cliente.id, COMBO_TRF3_N2)
    dado = json.loads(conn.get(chave))
    dado['epoch'] = time.time() - settings.LEADS_CACHE_MAX_IDADE_S - 60
    conn.set(chave, json.dumps(dado))

    corpo = _get(QS_TRF3_N2 + '&limit=10').json()

    assert corpo['fonte'] == 'ao_vivo'
    assert [r['cnj'] for r in corpo['results']] == [b.numero_cnj, a.numero_cnj]


@pytest.mark.django_db
def test_cache_velho_e_ao_vivo_estourado_responde_503(cliente, trf3, monkeypatch):
    from api import leads as leads_view

    def estoura(*_a, **_kw):
        raise OperationalError('canceling statement due to statement timeout')

    monkeypatch.setattr(leads_view, '_linhas_com_teto', estoura)
    resp = _get(QS_TRF3_N2 + '&limit=10')
    assert resp.status_code == 503


@pytest.mark.django_db
def test_incluir_consumidos_nunca_usa_o_cache(cliente, trf3):
    a = _mk(trf3, 1, 10)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    LeadConsumption.objects.create(cliente=cliente, processo=a)

    corpo = _get(QS_TRF3_N2 + '&limit=10&incluir_consumidos=1').json()

    assert corpo['fonte'] == 'ao_vivo'
    assert [r['cnj'] for r in corpo['results']] == [a.numero_cnj]


@pytest.mark.django_db
def test_cache_desligado_por_setting(cliente, trf3, settings):
    _mk(trf3, 1, 10)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    settings.LEADS_CACHE_ENABLED = False

    assert _get(QS_TRF3_N2 + '&limit=10').json()['fonte'] == 'ao_vivo'


# ── 4. agendamento ──────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_scheduler_agenda_intervalo_e_passada_das_0740(trf3):
    from djen.scheduler import create_scheduler
    sched = create_scheduler()
    assert sched.get_job('leads_cache_precomputo') is not None
    fixo = sched.get_job('leads_cache_precomputo_0740')
    assert fixo is not None
    campos = {f.name: str(f) for f in fixo.trigger.fields}
    assert campos['hour'] == '7' and campos['minute'] == '40'


@pytest.mark.django_db
def test_kill_switch_no_redis_sem_deploy(cliente, trf3):
    _mk(trf3, 1, 10)
    leads_cache.precomputar_combo(cliente.id, COMBO_TRF3_N2)
    leads_cache.desligar_cache()
    assert _get(QS_TRF3_N2 + '&limit=10').json()['fonte'] == 'ao_vivo'
    leads_cache.desligar_cache(False)
    assert _get(QS_TRF3_N2 + '&limit=10').json()['fonte'] == 'cache'
