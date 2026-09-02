"""Chave de cache dos widgets da /dashboard/leads/ e o teto do caminho da requisição.

Contexto medido em 01/09/2026 (#64). A página respondia 0,9 s porque o
`warm_leads_charts` cobre exatamente 24 combinações (6 keys × 4 períodos do
picker, sem tribunal, sem nível). Fora dessas, a view COMPUTAVA de forma
síncrona no caminho da requisição, contra o `proxy_read_timeout 90s` do nginx:

    recorte                       pior widget
    default d=30 (warm)           by-tribunal   41,56 s
    d=365 (warm)                  timeseries    80,46 s
    ?nivel=PRECATORIO             by-tribunal   63,82 s
    ?tribunal=TJSP&dias=365       by-tribunal   57,01 s
    ?dias=31                      calibration   90,75 s  <- ACIMA dos 90 s

Os dois primeiros casos são HIT na prática. Os três últimos eram MISS **por
construção da chave**, não porque o dado fosse diferente: `nivel` não é usado
por widget nenhum, e `dias` só muda `timeseries` e `funnel`.
"""
import pytest

from dashboard.views import (LEADS_CHART_KEYS, LEADS_KEYS_COM_DIAS,
                             leads_cache_key)


def test_nivel_nao_entra_na_chave():
    """`nivel` fragmentava a chave sem mudar o payload.

    A UI põe `?nivel=PRECATORIO` nos links de lista/export; com `nivel` na
    chave, isso era MISS garantido contra o warm (medido: 63,82 s em
    `by-tribunal`) pra devolver o MESMO payload já aquecido.
    """
    for key in LEADS_CHART_KEYS:
        assert (leads_cache_key(key, None, 'PRECATORIO', 30, 'juriscope')
                == leads_cache_key(key, None, None, 30, 'juriscope'))
        assert (leads_cache_key(key, 'TJSP', 'DIREITO_CREDITORIO', 30, 'x')
                == leads_cache_key(key, 'TJSP', None, 30, 'x'))


def test_dias_so_entra_na_chave_de_quem_usa_dias():
    """Recorte que estourou os 90 s do nginx: `?dias=31` em `calibration`.

    `calibration` ignora `dias` (varre todo o LeadConsumption do cliente),
    então qualquer valor fora do picker tem que cair na MESMA chave que o
    warm popula.
    """
    for key in set(LEADS_CHART_KEYS) - LEADS_KEYS_COM_DIAS:
        assert (leads_cache_key(key, None, None, 31, 'juriscope')
                == leads_cache_key(key, None, None, 30, 'juriscope')), key
        assert (leads_cache_key(key, None, None, 365, 'juriscope')
                == leads_cache_key(key, None, None, 7, 'juriscope')), key

    # E o contrário: quem USA dias precisa continuar separando por período,
    # senão o gráfico de 7 dias serviria o payload de 1 ano.
    for key in LEADS_KEYS_COM_DIAS:
        assert (leads_cache_key(key, None, None, 7, 'juriscope')
                != leads_cache_key(key, None, None, 365, 'juriscope')), key


def test_tribunal_e_cliente_continuam_separando():
    """Controle: a normalização não pode colapsar o que É diferente."""
    assert (leads_cache_key('kpis', 'TJSP', None, 30, 'juriscope')
            != leads_cache_key('kpis', None, None, 30, 'juriscope'))
    assert (leads_cache_key('kpis', None, None, 30, 'juriscope')
            != leads_cache_key('kpis', None, None, 30, 'outro'))
    # E keys diferentes nunca colidem entre si.
    chaves = {leads_cache_key(k, None, None, 30, 'juriscope')
              for k in LEADS_CHART_KEYS}
    assert len(chaves) == len(LEADS_CHART_KEYS)


def test_lista_de_keys_com_dias_bate_com_a_assinatura_de_compute():
    """Régua contra o código apodrecer.

    Se algum dia `calibration` passar a usar `dias`, este teste não pega
    sozinho — mas pega o inverso, que é o erro barato: alguém tirar uma key
    de LEADS_CHART_KEYS e esquecer de LEADS_KEYS_COM_DIAS.
    """
    assert LEADS_KEYS_COM_DIAS <= set(LEADS_CHART_KEYS)
    assert LEADS_KEYS_COM_DIAS == {'timeseries', 'funnel'}


def test_key_invalida_e_barrada_antes_de_abrir_transacao():
    """A view valida a key contra LEADS_CHART_KEYS ANTES de tocar no banco.

    Antes ela chamava `compute_leads_chart`, que consulta `ApiClient` e só
    então levanta ValueError: uma key inválida abria conexão e transação pra
    devolver 400. A régua aqui é a lista que a view consulta.
    """
    assert 'nao-existe' not in LEADS_CHART_KEYS
    assert set(LEADS_CHART_KEYS) == {
        'kpis', 'timeseries', 'calibration', 'funnel',
        'by-tribunal', 'distribuicao-score',
    }


def test_warm_cobre_exatamente_as_chaves_que_a_view_le():
    """O warm e a view TÊM que produzir o mesmo conjunto de chaves.

    Este é o teste que pegaria um "warm órfão" — gravar numa chave que
    ninguém lê. Reproduz o alvo do warm sem tocar no banco.
    """
    from dashboard.tasks import _LEADS_PERIODOS, leads_warm_alvos

    alvos = leads_warm_alvos()
    escritas = {leads_cache_key(ck, None, None, d, 'juriscope')
                for ck, d in alvos}

    # Tudo o que a tela pede no recorte default (qualquer período do picker,
    # com ou sem `nivel` na URL) tem que estar coberto.
    lidas = {leads_cache_key(ck, None, niv, d, 'juriscope')
             for ck in LEADS_CHART_KEYS
             for d in _LEADS_PERIODOS
             for niv in (None, 'PRECATORIO')}
    assert lidas <= escritas, lidas - escritas

    # E o warm não pode computar mais do que precisa: 2 keys × 4 períodos +
    # 4 keys × 1 = 12 chaves; sem duplicata na lista de alvos.
    assert len(alvos) == 12
    assert len(escritas) == 12


# ── teto de RELÓGIO (02/09/2026) ─────────────────────────────────────────────
#
# O `SET LOCAL statement_timeout` sozinho não é teto de requisição: ele corta
# UMA consulta. Medido em produção no recorte `?tribunal=TJSP&dias=365`, com o
# banco sob quatro backfills:
#
#     kpis 87,69 s · timeseries 72,84 s  → HTTP 200 e SEM `pending`
#     funnel 60,16 s · by-tribunal 60,14 s → `pending: True` (uma consulta
#                                            sozinha estourava)
#
# 87,69 s é 2 s abaixo do corte de 90 s do nginx. O 504 que a #64 dizia ter
# matado seguia vivo no recorte por tribunal — e agora sem acender o `pending`.

def test_prazo_de_relogio_recusa_quando_o_orcamento_acaba():
    """Muitas consultas RÁPIDAS somando mais que o teto têm que abster."""
    from django.db import DatabaseError

    from dashboard.views import PrazoDeRelogio

    p = PrazoDeRelogio(0)          # orçamento já esgotado
    with pytest.raises(DatabaseError) as exc:
        p(lambda *a: None, 'SELECT 1', None, False, {})
    # regra nº 2: teto é ALERTA com número, nunca corte mudo
    assert 'teto de relógio' in str(exc.value)
    assert 'SELECT 1' in str(exc.value)


def test_prazo_de_relogio_deixa_passar_dentro_do_orcamento():
    """Controle: com orçamento sobrando ele NÃO pode atrapalhar."""
    from dashboard.views import PrazoDeRelogio

    p = PrazoDeRelogio(60)
    chamou = []
    r = p(lambda *a: chamou.append(1) or 'ok', 'SELECT 1', None, False, {})
    assert r == 'ok' and chamou == [1]
    assert p.consultas == 1


def test_prazo_conta_as_consultas_para_o_log():
    """O log precisa dizer QUANTAS consultas passaram antes de estourar —
    'estourou' sem número não diz se foi uma lenta ou cem rápidas."""
    from dashboard.views import PrazoDeRelogio

    p = PrazoDeRelogio(60)
    for _ in range(3):
        p(lambda *a: None, 'SELECT 1', None, False, {})
    assert p.consultas == 3


def test_prazo_aperta_o_teto_do_proximo_statement():
    """Conferir entre statements não basta: o estouro cabe DENTRO de um.

    Medido em produção depois da 1ª versão deste teto: `timeseries` levou
    68,51 s com orçamento de 60 s — a última consulta começou dentro do prazo
    e rodou até o fim. O prazo tem que reapertar o `statement_timeout` de cada
    consulta para o que RESTA, senão a soma escapa.
    """
    from dashboard.views import PrazoDeRelogio

    emitidos = []

    class _Cur:
        def execute(self, sql, params=None):
            emitidos.append((sql, params))

    p = PrazoDeRelogio(60)
    p(lambda *a: None, 'SELECT 1', None, False, {'cursor': _Cur()})
    assert emitidos, 'não reapertou o teto do statement'
    sql, params = emitidos[0]
    assert 'statement_timeout' in sql
    # o valor tem que ser o RESTANTE (~60 s em ms), não o teto cheio de sempre
    assert 0 < params[0] <= 60_000
