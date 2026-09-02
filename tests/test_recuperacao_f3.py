"""A Fase 3 tem que andar sozinha — e falar quando parar.

CONTEXTO (02/09/2026). A Fase 2 da recuperação nacional fechou por mutirão
manual. O mutirão terminou, ninguém religou, e de 27/08 a 02/09 o país inteiro
fechou ~59 runs de janela-1-dia por dia — a coleta diária dos 59 tribunais e
mais nada, contra um pico medido de 1.641/dia. Ficaram 7.167 dias nunca
refeitos em 19 tribunais, e **nenhum instrumento acusou**.

O que estes testes protegem, na ordem em que doeria:

  1. `recoletar_dia` NÃO pula dia coberto. Todo dia da Fase 3 já tem run
     `success` — usar `backfill_dia` pularia os 7.167 sem ler uma linha;
  2. a trava por (tribunal, dia) continua de pé. Sem ela, 12 runs concorrentes
     e 28× as páginas do dia (medido 27/08/2026);
  3. circuito aberto ADIA, não falha — nada de FailedRegistry;
  4. a régua é a MESMA da tela: dia com `success` pós-corte sai da conta, dia
     com razão baixa e só run antigo fica;
  5. o dia já em voo (por qualquer rota) não ganha uma segunda coleta;
  6. o TJPR está fora por padrão — é decisão comercial, não bug;
  7. todo caminho de parada é ERRO/WARNING com o número real, nunca `return`
     discreto (regra nº 2 do CLAUDE.md);
  8. dia teimoso (re-coletado 3× e ainda na conta) sai da fila e vira achado —
     insistir nele queimaria banda do CNJ para sempre.
"""
import datetime
import logging
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from dashboard import completude_medicoes as M
from djen import recuperacao as R

DIA = datetime.date(2025, 5, 5)
ANTES = M.CORTE_FLAT - datetime.timedelta(days=1)
DEPOIS = M.CORTE_FLAT + datetime.timedelta(days=1)


def _aware(naive):
    return naive.replace(tzinfo=datetime.UTC)


@pytest.fixture
def tribs(db):
    from tribunals.models import Tribunal
    feitos = {}
    for sig in ('TRT2', 'TRT3', 'TJPR'):
        feitos[sig], _ = Tribunal.objects.get_or_create(
            sigla=sig, defaults={'nome': sig, 'sigla_djen': sig})
    return feitos


def _run(trib, dia, *, quando, itens=20_000, paginas=40, status='success', novas=None):
    """Cria um IngestionRun de 1 dia com razão itens/página controlada."""
    from tribunals.models import IngestionRun
    novas = itens if novas is None else novas
    r = IngestionRun.objects.create(
        tribunal=trib, fonte='djen', status=status,
        janela_inicio=dia, janela_fim=dia, paginas_lidas=paginas,
        movimentacoes_novas=novas, movimentacoes_duplicadas=itens - novas,
    )
    IngestionRun.objects.filter(pk=r.pk).update(started_at=_aware(quando))
    return r


@pytest.fixture(autouse=True)
def _deixar_o_log_chegar_no_caplog():
    """`core.settings.LOGGING` põe `propagate: False` no logger `voyager`.

    Sem religar a propagação, o `caplog` fica VAZIO e todo teste de alerta
    passaria sem alerta nenhum — que é exatamente o defeito que estes testes
    existem para proibir.
    """
    lg = logging.getLogger('voyager')
    antes = lg.propagate
    lg.propagate = True
    yield
    lg.propagate = antes


@pytest.fixture(autouse=True)
def _limpar_chaves():
    for k in (R.CHAVE_OFF, R.CHAVE_TJPR, R.CHAVE_ESTADO, R.CHAVE_LIGADO_EM):
        cache.delete(k)
    yield
    for k in (R.CHAVE_OFF, R.CHAVE_TJPR, R.CHAVE_ESTADO, R.CHAVE_LIGADO_EM):
        cache.delete(k)


# ── 1. a régua ────────────────────────────────────────────────────────────

def test_dia_capado_e_antigo_entra_na_conta(tribs):
    """Razão < 700 e só run PRÉ-corte: é exatamente o dia que a Fase 3 existe
    para refazer. Razão 500 = 20.000 itens em 40 páginas (o caminho por UF)."""
    _run(tribs['TRT2'], DIA, quando=ANTES, itens=20_000, paginas=40)
    assert R.dias_pendentes(['TRT2']) == [('TRT2', DIA.isoformat(), 0)]


def test_dia_refeito_pos_corte_sai_da_conta(tribs):
    """MUTAÇÃO: é este teste que quebra se alguém trocar o `CORTE_FLAT` ou
    esquecer o `status='success'` — e um teto que não sai da conta faz o tique
    reprocessar o país inteiro para sempre."""
    _run(tribs['TRT2'], DIA, quando=ANTES, itens=20_000, paginas=40)
    _run(tribs['TRT2'], DIA, quando=DEPOIS, itens=25_000, paginas=40)
    assert R.dias_pendentes(['TRT2']) == []


def test_run_pos_corte_que_FALHOU_nao_tira_o_dia_da_conta(tribs):
    _run(tribs['TRT2'], DIA, quando=DEPOIS, itens=20_000, paginas=40,
         status='failed')
    assert R.dias_pendentes(['TRT2']) == [('TRT2', DIA.isoformat(), 1)]


def test_dia_flat_nao_e_alvo(tribs):
    """Razão 1.000 = caminho flat. Não é buraco, não entra na fila."""
    _run(tribs['TRT2'], DIA, quando=ANTES, itens=20_000, paginas=20)
    assert R.dias_pendentes(['TRT2']) == []


def test_dia_pequeno_nao_e_alvo(tribs):
    """Abaixo de MIN_ITENS_DIA_GRANDE o cap de 10k nunca foi atingido — refazer
    esse dia é gastar banda do CNJ à toa."""
    _run(tribs['TRT2'], DIA, quando=ANTES, itens=1_000, paginas=10)
    assert R.dias_pendentes(['TRT2']) == []


def test_janela_de_varios_dias_nao_e_alvo(tribs):
    from tribunals.models import IngestionRun
    r = IngestionRun.objects.create(
        tribunal=tribs['TRT2'], fonte='djen', status='success',
        janela_inicio=DIA, janela_fim=DIA + datetime.timedelta(days=3),
        paginas_lidas=40, movimentacoes_novas=20_000)
    IngestionRun.objects.filter(pk=r.pk).update(started_at=_aware(ANTES))
    assert R.dias_pendentes(['TRT2']) == []


def test_run_de_outra_fonte_nao_conta(tribs):
    """Desde 08/2026 o mesmo tribunal tem run do diário próprio. Contá-lo aqui
    faria a Fase 3 escolher trabalho por um número que não é dela."""
    from tribunals.models import IngestionRun
    r = IngestionRun.objects.create(
        tribunal=tribs['TRT2'], fonte='tjsp-dje', status='success',
        janela_inicio=DIA, janela_fim=DIA, paginas_lidas=40,
        movimentacoes_novas=20_000)
    IngestionRun.objects.filter(pk=r.pk).update(started_at=_aware(ANTES))
    assert R.dias_pendentes(['TRT2']) == []


# ── 2. a unidade de trabalho ──────────────────────────────────────────────

def test_recoletar_dia_NAO_pula_dia_coberto(tribs):
    """O teste mais importante do arquivo.

    Todo dia da Fase 3 tem run `success` — foi assim que o teto por UF
    decapitou 43,6% do TJSP todo dia por 17 meses com run verde. Se alguém
    trocar `recoletar_dia` por `backfill_dia`, ou colar um `_dia_coberto` aqui,
    este teste quebra e os 7.167 dias voltam a ser pulados em silêncio.
    """
    _run(tribs['TRT2'], DIA, quando=ANTES)
    run = MagicMock(pk=1, movimentacoes_novas=9, movimentacoes_duplicadas=0,
                    paginas_lidas=3)
    with patch('djen.ingestion.ingest_window', return_value=run) as iw:
        out = R.recoletar_dia('TRT2', DIA.isoformat())
    assert iw.called, 'dia coberto foi PULADO — a Fase 3 inteira seria pulada'
    assert out['run_id'] == 1


def test_trava_impede_segunda_coleta_do_mesmo_dia(tribs, caplog):
    """Sem a trava: 12 runs concorrentes e 7.437 páginas contra as 264 do dia
    (TJSP, 27/08/2026). Não perde dado — queima a banda que abre o circuito."""
    from djen.jobs import _chave_coleta
    chave = _chave_coleta('TRT2', DIA.isoformat())
    cache.set(chave, 1, 60)
    try:
        with patch('djen.ingestion.ingest_window') as iw, \
             caplog.at_level(logging.WARNING, logger='voyager.djen.recuperacao'):
            out = R.recoletar_dia('TRT2', DIA.isoformat())
        assert not iw.called
        assert out['skip'] == 'ja_em_coleta'
        # regra nº 2: não é corte mudo — sai com o nome do dia
        assert any(DIA.isoformat() in m for m in caplog.messages)
    finally:
        cache.delete(chave)


def test_circuito_aberto_adia_e_solta_a_trava(tribs):
    """`DjenBusyError` não pode virar falha: em 19/08/2026 foram 4.153 runs
    marcados como falha que eram jobs adiados, e isso esconde a falha real."""
    from djen.client import DjenBusyError
    from djen.jobs import _chave_coleta
    with patch('djen.ingestion.ingest_window', side_effect=DjenBusyError('x')):
        out = R.recoletar_dia('TRT2', DIA.isoformat())
    assert out['skip'] == 'circuito_aberto'
    assert cache.get(_chave_coleta('TRT2', DIA.isoformat())) is None, \
        'a trava ficou presa e o dia não voltaria até o TTL de 4h'


# ── 3. o tique ────────────────────────────────────────────────────────────

@pytest.fixture
def fila():
    f = MagicMock()
    f.get_job_ids.return_value = []
    f.__len__.return_value = 0
    with patch('django_rq.get_queue', return_value=f), \
         patch('djen.recuperacao._ocupados', return_value=set()), \
         patch('djen.recuperacao._travados', return_value=set()), \
         patch('djen.client.circuit_is_open', return_value=False):
        yield f


def _enfileirados(fila):
    return [c.kwargs['job_id'] for c in fila.enqueue.call_args_list]


def test_tique_enfileira_o_dia_pendente(tribs, fila):
    _run(tribs['TRT2'], DIA, quando=ANTES)
    r = R.tick_recuperacao_fase3()
    assert r['pendentes'] == 1 and r['enfileirados'] == 1
    assert _enfileirados(fila) == [f'f3:TRT2:{DIA.isoformat()}']


def test_tique_nao_duplica_dia_ja_em_voo(tribs, fila):
    """Vale para QUALQUER rota: `f3`, o `f2` do mutirão, o `bfd` do tique
    retroativo e o `adiado` do circuito. Duplicar não traz publicação."""
    _run(tribs['TRT2'], DIA, quando=ANTES)
    for prefixo in ('f3', 'f2', 'bfd', 'adiado'):
        fila.enqueue.reset_mock()
        with patch('djen.recuperacao._ocupados',
                   return_value={f'{prefixo}:TRT2:{DIA.isoformat()}'}):
            R.tick_recuperacao_fase3()
        assert _enfileirados(fila) == [], f'duplicou apesar do id {prefixo}:'


def test_tique_nao_duplica_dia_com_trava_viva(tribs, fila):
    """A trava é o único sinal entre o worker PEGAR o job e ele terminar: o id
    já saiu da lista da fila. Sem ler a trava, o tique de 5 min re-enfileiraria
    justamente o dia mais longo (o TJSP de 27/08 levou 136 min)."""
    _run(tribs['TRT2'], DIA, quando=ANTES)
    with patch('djen.recuperacao._travados',
               return_value={('TRT2', DIA.isoformat())}):
        R.tick_recuperacao_fase3()
    assert _enfileirados(fila) == []


def test_tjpr_fica_de_fora_ate_a_escotilha(tribs, fila):
    """1.152 dias e 38,8M — 43% do que resta. Está fora por decisão comercial;
    entrar sozinho aqui seria o código decidindo no lugar do dono do produto."""
    _run(tribs['TJPR'], DIA, quando=ANTES)
    r = R.tick_recuperacao_fase3()
    assert r['pendentes'] == 0 and _enfileirados(fila) == []

    cache.set(R.CHAVE_TJPR, 1, 60)
    fila.enqueue.reset_mock()
    r = R.tick_recuperacao_fase3()
    assert r['tjpr_ligado'] and r['pendentes'] == 1
    assert _enfileirados(fila) == [f'f3:TJPR:{DIA.isoformat()}']


def test_kill_switch_para_de_enfileirar_e_grita(tribs, fila, caplog):
    _run(tribs['TRT2'], DIA, quando=ANTES)
    cache.set(R.CHAVE_OFF, 1, 60)
    with caplog.at_level(logging.WARNING, logger='voyager.djen.recuperacao'):
        r = R.tick_recuperacao_fase3()
    assert _enfileirados(fila) == []
    assert r['motivo_parada'] == 'pausado'
    assert any('PAUSADO' in m for m in caplog.messages)


def test_circuito_aberto_no_tique_e_alerta_com_o_numero(tribs, fila, caplog):
    _run(tribs['TRT2'], DIA, quando=ANTES)
    with patch('djen.client.circuit_is_open', return_value=True), \
         caplog.at_level(logging.WARNING, logger='voyager.djen.recuperacao'):
        r = R.tick_recuperacao_fase3()
    assert _enfileirados(fila) == []
    assert r['motivo_parada'] == 'circuito_aberto'
    assert any('1 dias pendentes' in m for m in caplog.messages), \
        'parou sem dizer quantos ficaram esperando — é o corte mudo de novo'


def test_teto_do_tique_e_ERRO_com_o_pendente_real(tribs, fila, caplog):
    """Regra nº 2: teto atingido é alerta com o número real. `enfileirei 2`
    sem dizer que havia 5 é a mesma mentira por omissão de sempre."""
    for i in range(5):
        _run(tribs['TRT2'], DIA + datetime.timedelta(days=i), quando=ANTES)
    with patch.object(R, 'POR_TIQUE', 2), \
         caplog.at_level(logging.INFO, logger='voyager.djen.recuperacao'):
        r = R.tick_recuperacao_fase3()
    assert r['enfileirados'] == 2 and r['pendentes'] == 5
    assert any('5 pendentes' in m for m in caplog.messages)


def test_maquina_parada_e_ERRO(tribs, fila, caplog):
    """Pendente > 0, nada em voo, nada enfileirado: foi este estado exato que
    durou de 27/08 a 02/09/2026 sem um único alarme."""
    _run(tribs['TRT2'], DIA, quando=ANTES)
    with patch.object(R, 'POR_TIQUE', 0), \
         caplog.at_level(logging.ERROR, logger='voyager.djen.recuperacao'):
        r = R.tick_recuperacao_fase3()
    assert r['motivo_parada'] == 'nada_enfileirado'
    assert any('PARADA' in m for m in caplog.messages)


def test_dia_teimoso_sai_da_fila_e_vira_achado(tribs, fila, caplog):
    """Re-coletado TENTATIVAS_MAX vezes pelo caminho novo e ainda na conta: não
    é trabalho, é sintoma. Insistir queimaria banda do CNJ para sempre."""
    for i in range(R.TENTATIVAS_MAX):
        _run(tribs['TRT2'], DIA, quando=DEPOIS + datetime.timedelta(minutes=i),
             status='failed')
    with caplog.at_level(logging.ERROR, logger='voyager.djen.recuperacao'):
        r = R.tick_recuperacao_fase3()
    assert r['pendentes'] == 1 and r['teimosos'] == 1
    assert _enfileirados(fila) == [], 'insistiu num dia já provado teimoso'
    assert any('achado, não fila' in m for m in caplog.messages)


def test_tique_reparte_entre_tribunais(tribs, fila):
    """Sem round-robin o primeiro tribunal da ordem monopoliza a fila e os
    outros 18 ficam parados — foi o que o teto global sozinho fez com os TRTs
    em 29/07/2026."""
    for i in range(5):
        _run(tribs['TRT2'], DIA + datetime.timedelta(days=i), quando=ANTES)
        _run(tribs['TRT3'], DIA + datetime.timedelta(days=i), quando=ANTES)
    with patch.object(R, 'POR_TIQUE', 4):
        R.tick_recuperacao_fase3()
    ids = _enfileirados(fila)
    assert len(ids) == 4
    assert sum(1 for i in ids if ':TRT2:' in i) == 2
    assert sum(1 for i in ids if ':TRT3:' in i) == 2


def test_o_retrato_fica_no_cache_mesmo_parado(tribs, fila):
    """A tela precisa mostrar o tamanho do buraco mesmo com o processo parado.
    Card vazio some da vista; número alto ao lado de `PAUSADO` não."""
    _run(tribs['TRT2'], DIA, quando=ANTES)
    cache.set(R.CHAVE_OFF, 1, 60)
    R.tick_recuperacao_fase3()
    assert (R.estado() or {}).get('motivo_parada') == 'pausado'


# ── 4. a tela ─────────────────────────────────────────────────────────────

@pytest.fixture
def logado(db, client):
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.create_user('r116', password='x')
    client.force_login(u)
    return client


def test_a_tela_publica_o_numero_e_o_estado(logado):
    """O card em `/dashboard/ingestao/saude/`. Sem ele, "parou" continua sendo
    uma informação que só existe dentro do log — que foi como seis dias de
    máquina parada passaram sem ninguém ver."""
    cache.set(R.CHAVE_ESTADO, {
        'medido_em': timezone.now(), 'pendentes': 7167, 'vazao_24h': 0,
        'em_voo': 3, 'enfileirados': 40, 'tjpr_ligado': False, 'teimosos': 2,
        'por_tribunal': [('TRT2', 479)], 'motivo_parada': None,
    }, 60)
    corpo = logado.get('/dashboard/ingestao/saude/').content.decode()
    assert '7167' in corpo and 'TRT2' in corpo
    assert 'Fase 3' in corpo


def test_a_tela_diz_quando_NAO_HA_retrato(logado):
    """Retrato vencido é, sozinho, o alarme de que o tique parou. Card vazio
    some da vista; card que grita não."""
    cache.delete(R.CHAVE_ESTADO)
    corpo = logado.get('/dashboard/ingestao/saude/').content.decode()
    assert 'sem retrato' in corpo


def test_a_tela_mostra_o_motivo_da_parada(logado):
    cache.set(R.CHAVE_ESTADO, {'medido_em': timezone.now(), 'pendentes': 7167,
                               'motivo_parada': 'circuito_aberto'}, 60)
    corpo = logado.get('/dashboard/ingestao/saude/').content.decode()
    assert 'circuito_aberto' in corpo


def test_vazao_conta_dia_refeito_e_nao_job(tribs):
    """Vazão é MEDIÇÃO do que saiu da conta. Contar jobs contaria a mesma
    página duas vezes e transformaria retrabalho em progresso."""
    _run(tribs['TRT2'], DIA, quando=DEPOIS, itens=25_000, paginas=40)
    _run(tribs['TRT2'], DIA, quando=DEPOIS, itens=25_000, paginas=40)
    assert R.vazao(['TRT2'], horas=24 * 3650) == 1
