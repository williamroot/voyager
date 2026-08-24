"""Testes do orçamento da terceira porta (`diarios/orcamento.py` + `tick`).

O que estes testes travam é a diferença entre "teto que avisa" e "corte mudo",
que é a regra nº 2 do `CLAUDE.md`, e a decisão de FALHAR FECHADO quando não dá
para medir o disco do Elasticsearch. Nenhum deles depende de rede: o ES é
substituído por dublê, porque o que está sob teste é a DECISÃO, não o cliente.
"""

import datetime as dt

import pytest
from django.utils import timezone

from diarios import orcamento


class _EsFalso:
    """Dublê do cliente ES: só precisa responder `cat.allocation`."""

    def __init__(self, linhas=None, explode=False):
        self._linhas = linhas or []
        self._explode = explode
        self.cat = self

    def allocation(self, **kw):
        if self._explode:
            raise RuntimeError('ES fora do ar')
        return self._linhas


def _com_es(monkeypatch, es):
    from search import gate
    monkeypatch.setattr(gate, '_es', lambda: es)


# ── guarda de disco ─────────────────────────────────────────────────────────

def test_disco_abaixo_do_teto_libera(monkeypatch, settings):
    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    _com_es(monkeypatch, _EsFalso([{'node': 'es-01', 'disk.percent': '63',
                                    'disk.total': '2.9tb', 'disk.avail': '1tb'}]))
    pode, motivo = orcamento.guarda_de_disco()
    assert pode is True
    assert '63%' in motivo


def test_disco_no_teto_bloqueia_com_o_numero_no_motivo(monkeypatch, settings):
    """Atingir o teto tem que dizer QUANTO, senão vira 'parou e não sei por quê'."""
    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    _com_es(monkeypatch, _EsFalso([{'node': 'es-01', 'disk.percent': '85',
                                    'disk.total': '2.9tb', 'disk.avail': '430gb'}]))
    pode, motivo = orcamento.guarda_de_disco()
    assert pode is False
    assert '85%' in motivo and '430gb' in motivo


def test_es_mudo_fecha_a_guarda(monkeypatch, settings):
    """A decisão declarada: sem medição, não enfileira.

    O contrário (liberar quando não sei) transformaria um blip do ES em
    autorização para escrever ~980 mil linhas/h contra um disco que ninguém
    está medindo. Parada falsa custa 10 minutos; 'vai' falso custa o índice em
    `read_only_allow_delete`, que derruba as TRÊS portas.
    """
    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    _com_es(monkeypatch, _EsFalso(explode=True))
    pode, motivo = orcamento.guarda_de_disco()
    assert pode is False
    assert 'FECHA' in motivo


def test_linha_unassigned_do_cat_allocation_nao_e_lida_como_disco(monkeypatch, settings):
    """`_cat/allocation` devolve uma linha UNASSIGNED sem disco nenhum.

    Lê-la como 0% seria liberar a guarda por engano — e é exatamente a linha
    que aparece primeiro num cluster de um nó só com réplicas não alocadas
    (o caso desta casa: `active_shards_percent = 87.5`).
    """
    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    _com_es(monkeypatch, _EsFalso([
        {'node': 'UNASSIGNED', 'disk.percent': None},
        {'node': 'es-01', 'disk.percent': '91', 'disk.total': '2.9tb', 'disk.avail': '260gb'},
    ]))
    pode, motivo = orcamento.guarda_de_disco()
    assert pode is False, motivo
    assert '91%' in motivo


def test_guarda_desligada_por_env_libera_sem_perguntar_ao_es(monkeypatch, settings):
    settings.DIARIOS_GUARDA_DISCO_ENABLED = False
    _com_es(monkeypatch, _EsFalso(explode=True))
    pode, _ = orcamento.guarda_de_disco()
    assert pode is True


# ── guarda de fila do índice ────────────────────────────────────────────────

class _FilaFalsaCount:
    def __init__(self, n):
        self.count = n


def _com_fila_es(monkeypatch, n):
    import django_rq
    monkeypatch.setattr(django_rq, 'get_queue', lambda *a, **kw: _FilaFalsaCount(n))


def test_fila_do_indice_rasa_libera(monkeypatch, settings):
    settings.DIARIOS_FILA_ES_MAX = 5000
    _com_fila_es(monkeypatch, 84)
    pode, motivo = orcamento.guarda_do_indice()
    assert pode is True
    assert '84 jobs' in motivo


def test_fila_do_indice_funda_bloqueia_porque_coletado_nao_seria_buscavel(monkeypatch, settings):
    """O motivo tem que dizer POR QUE, não só que parou.

    Foi exatamente esta a lição de 21/08/2026 (§12): 27.619 linhas gravadas e
    fora do índice, com a edição marcada `ok`.
    """
    settings.DIARIOS_FILA_ES_MAX = 5000
    _com_fila_es(monkeypatch, 5000)
    pode, motivo = orcamento.guarda_do_indice()
    assert pode is False
    assert '5000 jobs' in motivo and 'buscável' in motivo


def test_fila_do_indice_ilegivel_fecha(monkeypatch, settings):
    settings.DIARIOS_FILA_ES_MAX = 5000
    monkeypatch.setattr(orcamento, 'fila_do_indice', lambda: None)
    pode, motivo = orcamento.guarda_do_indice()
    assert pode is False
    assert 'FECHA' in motivo


def test_guarda_de_recursos_reprova_no_disco_antes_de_olhar_a_fila(monkeypatch, settings):
    """Ordem importa: o disco é teto estrutural (não drena), a fila é conjuntural."""
    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    settings.DIARIOS_FILA_ES_MAX = 5000
    _com_es(monkeypatch, _EsFalso([{'node': 'es-01', 'disk.percent': '95',
                                    'disk.total': '2.9tb', 'disk.avail': '140gb'}]))

    def _explode():
        raise AssertionError('não podia ter chegado a medir a fila')

    monkeypatch.setattr(orcamento, 'fila_do_indice', _explode)
    pode, motivo = orcamento.guarda_de_recursos()
    assert pode is False
    assert '95%' in motivo


# ── orçamento diário ────────────────────────────────────────────────────────

def test_sem_teto_configurado_a_folga_e_none(settings):
    settings.DIARIOS_TETO_UNIDADES_DIA = 0
    settings.DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE = 0
    assert orcamento.folga_do_orcamento('tjsp-dje') is None


def test_teto_por_fonte_vence_o_global(settings):
    settings.DIARIOS_TETO_UNIDADES_DIA = 10
    settings.DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE = 3
    assert orcamento.teto_diario('tjsp-dje') == 3
    assert orcamento.teto_diario('stf') == 10 or orcamento.teto_diario('stf') == getattr(
        settings, 'DIARIOS_TETO_UNIDADES_DIA_STF', 10)


@pytest.mark.django_db
def test_orcamento_conta_so_o_que_fechou_nas_ultimas_24h(settings):
    """Unidade fechada há 25 h não consome orçamento de hoje; falha não consome nunca.

    Cobrar a falha faria uma fonte quebrada travar as próprias tentativas de
    conserto — e o custo dela para o banco foi zero.
    """
    from diarios.models import EdicaoDiario

    settings.DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE = 5
    agora = timezone.now()
    EdicaoDiario.objects.create(fonte='tjsp-dje', chave='a', data=dt.date(2015, 7, 15),
                                status=EdicaoDiario.OK, coletado_em=agora)
    EdicaoDiario.objects.create(fonte='tjsp-dje', chave='b', data=dt.date(2015, 7, 15),
                                status=EdicaoDiario.OK,
                                coletado_em=agora - dt.timedelta(hours=25))
    EdicaoDiario.objects.create(fonte='tjsp-dje', chave='c', data=dt.date(2015, 7, 15),
                                status=EdicaoDiario.FALHA)
    assert orcamento.coletadas_24h('tjsp-dje') == 1
    assert orcamento.folga_do_orcamento('tjsp-dje') == 4


@pytest.mark.django_db
def test_orcamento_estourado_nao_devolve_folga_negativa(settings):
    from diarios.models import EdicaoDiario

    settings.DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE = 1
    agora = timezone.now()
    for chave in ('a', 'b', 'c'):
        EdicaoDiario.objects.create(fonte='tjsp-dje', chave=chave, data=dt.date(2015, 7, 15),
                                    status=EdicaoDiario.OK, coletado_em=agora)
    assert orcamento.folga_do_orcamento('tjsp-dje') == 0


# ── o tick, que é onde as duas guardas encostam na fila ─────────────────────

@pytest.mark.django_db
def test_tick_bloqueado_por_disco_nao_enfileira_e_nao_perde_pendente(monkeypatch, settings):
    """Bloqueio é adiamento, nunca descarte: a unidade continua `pendente`."""
    from diarios import jobs
    from diarios.models import EdicaoDiario

    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    _com_es(monkeypatch, _EsFalso([{'node': 'es-01', 'disk.percent': '92',
                                    'disk.total': '2.9tb', 'disk.avail': '230gb'}]))
    ed = EdicaoDiario.objects.create(fonte='tjsp-dje', chave='4000-12',
                                     data=dt.date(2015, 7, 15), status=EdicaoDiario.PENDENTE)
    saida = jobs.tick('tjsp-dje')
    assert saida['enfileiradas'] == 0
    assert 'bloqueado' in saida and '92%' in saida['bloqueado']
    ed.refresh_from_db()
    assert ed.status == EdicaoDiario.PENDENTE
    assert ed.tentativas == 0


@pytest.mark.django_db
def test_tick_respeita_o_orcamento_e_enfileira_so_a_folga(monkeypatch, settings):
    from diarios import jobs
    from diarios.models import EdicaoDiario

    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    settings.DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE = 3
    _com_es(monkeypatch, _EsFalso([{'node': 'es-01', 'disk.percent': '10',
                                    'disk.total': '2.9tb', 'disk.avail': '2tb'}]))
    agora = timezone.now()
    # 2 já fechadas nas últimas 24 h ⇒ folga de 1
    for chave in ('ja-1', 'ja-2'):
        EdicaoDiario.objects.create(fonte='tjsp-dje', chave=chave, data=dt.date(2015, 7, 15),
                                    status=EdicaoDiario.OK, coletado_em=agora)
    for chave in ('p-1', 'p-2', 'p-3'):
        EdicaoDiario.objects.create(fonte='tjsp-dje', chave=chave, data=dt.date(2015, 7, 15),
                                    status=EdicaoDiario.PENDENTE)

    enfileirados = []

    class _FilaFalsa:
        count = 0          # a guarda de fila do índice lê isto na `es_index`

        def get_job_ids(self):
            return []

        def enqueue(self, *a, **kw):
            enfileirados.append(kw.get('job_id'))

    import django_rq
    monkeypatch.setattr(django_rq, 'get_queue', lambda *a, **kw: _FilaFalsa())

    saida = jobs.tick('tjsp-dje')
    assert saida['enfileiradas'] == 1, saida
    assert saida['folga_24h'] == 1
    assert len(enfileirados) == 1
    assert EdicaoDiario.objects.filter(fonte='tjsp-dje',
                                       status=EdicaoDiario.PENDENTE).count() == 3


@pytest.mark.django_db
def test_tick_com_orcamento_esgotado_avisa_com_o_numero(monkeypatch, settings):
    """Esgotar o orçamento não é silêncio: o retorno diz o teto e o consumo."""
    from diarios import jobs
    from diarios.models import EdicaoDiario

    settings.DIARIOS_ES_DISCO_MAX_PCT = 85.0
    settings.DIARIOS_TETO_UNIDADES_DIA_TJSP_DJE = 1
    _com_es(monkeypatch, _EsFalso([{'node': 'es-01', 'disk.percent': '10',
                                    'disk.total': '2.9tb', 'disk.avail': '2tb'}]))
    EdicaoDiario.objects.create(fonte='tjsp-dje', chave='ja', data=dt.date(2015, 7, 15),
                                status=EdicaoDiario.OK, coletado_em=timezone.now())
    EdicaoDiario.objects.create(fonte='tjsp-dje', chave='p', data=dt.date(2015, 7, 15),
                                status=EdicaoDiario.PENDENTE)

    class _FilaFalsa:
        count = 0          # a guarda de fila do índice lê isto na `es_index`

        def get_job_ids(self):
            return []

        def enqueue(self, *a, **kw):
            raise AssertionError('não podia enfileirar com o orçamento esgotado')

    import django_rq
    monkeypatch.setattr(django_rq, 'get_queue', lambda *a, **kw: _FilaFalsa())

    saida = jobs.tick('tjsp-dje')
    assert saida['orcamento_esgotado'] is True
    assert saida['teto_dia'] == 1
    assert saida['enfileiradas'] == 0
