"""A porta do Datajud grava e ENTREGA ao índice — e há régua para provar.

O que estes testes travam, medido em produção em 24/08/2026 com amostra
aleatória e `_mget` por id (resposta exata por documento, não estimativa):

    MOVIMENTAÇÕES — recorte por janela de `inserido_em`, filtro
    `external_id LIKE 'datajud:%'`, ancorado em `mov_inserido_tribunal_idx`:

      idade da escrita   linhas na janela   amostra   fora do índice
      0-5 min                     3.088       3.000   3.000 (100,00%)
      5-15 min                    3.927       3.000   1.268 ( 42,27%)
      15-30 min                   4.133       3.000       0
      30-60 min                  15.362       3.000       0

    PROCESSOS — critério exato "o doc está em dia com esta porta se
    `doc.enriquecido_em >= PG.data_enriquecimento_datajud`":

      janela de `data_enriquecimento_datajud`   amostra   doc em dia
      30-15 min                                     500   0
      2h-30min                                      500   0
      1d-2h                                         500   8   (1,6%)
      3d-1d                                         500   98  (19,6%)
      30d-7d                                        500   0

Eram DUAS doenças com a mesma assinatura:

  1. `Movimentacao.objects.bulk_create(...)` não dispara `post_save`, logo o
     write-through de `search/signals.py` nunca rodava. A única coisa que
     levava as linhas ao índice era o poller de 10 minutos
     (`search/sync_incremental.py`) — e a medição acima foi feita com ele
     SAUDÁVEL (atraso de 122.604 ids ≈ 1 tick). Freado (`FILA_ES_ALTA`),
     desligado (`sync_es:off`) ou com a chave do watermark perdida do cache
     (ele RE-ANCORA NO TOPO), a perda deixa de ser espera e vira definitiva.
  2. `Process.objects.filter(pk=...).update(...)` não dispara `post_save` E
     não mexe em `atualizado_em` — `auto_now` só roda em `Model.save()`. Como
     `sync_processos_atualizados` é keyset por `atualizado_em`, esse segundo
     buraco não tinha poller NENHUM. População: 22.475.738 processos com
     `data_enriquecimento_datajud`, 1.703.782 nos últimos 30 dias, ~5.000/h.
"""

import datetime as dt
from unittest import mock

import pytest
from django.utils import timezone


def _tribunal():
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF4', defaults={'nome': 'TRF4', 'sigla_djen': 'TRF4'})
    return t


def _source(cnj, n_movs=3):
    return {
        'numeroProcesso': cnj.replace('-', '').replace('.', ''),
        'classe': {'codigo': 12078, 'nome': 'Cumprimento de Sentença contra a Fazenda'},
        'movimentos': [
            {'codigo': 100 + i, 'nome': f'Movimento {i}',
             'dataHora': f'2025-0{i + 1}-10T10:00:00.000Z'}
            for i in range(n_movs)
        ],
    }


# ── 1. a entrega acontece na GRAVAÇÃO, não no próximo poller ─────────────────
@pytest.mark.django_db(transaction=True)
def test_sync_entrega_movs_e_processo_ao_indice_no_commit():
    """A regressão principal: gravar sem entregar.

    Antes, `sync_processo` terminava sem falar com a fila `es_index`. Medido:
    100% das movimentações escritas nos últimos 5 minutos estavam fora do
    índice, e 0 de 500 processos tocados nas últimas 2 horas tinham o doc em
    dia com a escrita.
    """
    from datajud.ingestion import sync_processo
    from tribunals.models import Movimentacao, Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5001111-11.2025.4.04.7100')
    cli = mock.MagicMock()
    cli.fetch_processo.return_value = _source(p.numero_cnj, n_movs=3)

    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila):
        out = sync_processo(p, client=cli)

    assert out['novos'] == 3
    chamadas = {c[0][0]: c[0][1] for c in fila.enqueue.call_args_list}
    assert 'search.jobs.indexar_movimentacoes_bulk' in chamadas, \
        'as movimentações novas TÊM que ir para o índice na gravação'
    assert 'search.jobs.indexar_processos_bulk' in chamadas, \
        'o doc do processo muda em toda sincronização (enriquecido_em)'
    assert sorted(chamadas['search.jobs.indexar_movimentacoes_bulk']) == sorted(
        Movimentacao.objects.filter(processo=p).values_list('id', flat=True))
    assert chamadas['search.jobs.indexar_processos_bulk'] == [p.pk]


@pytest.mark.django_db(transaction=True)
def test_nada_e_enfileirado_antes_do_commit():
    """Entregar pks dentro da transação é enfileirar fantasma: um rollback
    deixaria a fila `es_index` perseguindo linha que não existe."""
    from django.db import transaction

    from datajud.ingestion import sync_processo
    from tribunals.models import Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5002222-11.2025.4.04.7100')
    cli = mock.MagicMock()
    cli.fetch_processo.return_value = _source(p.numero_cnj, n_movs=2)

    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila):
        with transaction.atomic():
            sync_processo(p, client=cli)
            assert fila.enqueue.call_count == 0, 'nada antes do commit'
        assert fila.enqueue.call_count >= 1, 'e tudo no commit'


@pytest.mark.django_db(transaction=True)
def test_processo_sem_hit_no_datajud_tambem_atualiza_o_doc():
    """"Não encontrado" também é escrita: `data_enriquecimento_datajud` muda, e
    com ela o `enriquecido_em` do documento. Sem entregar, o doc fica dizendo
    que o processo nunca passou pelo Datajud — e o gate acusaria isso para
    sempre, porque o Postgres diz que passou."""
    from datajud.ingestion import sync_processo
    from tribunals.models import Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5003333-11.2025.4.04.7100')
    cli = mock.MagicMock()
    cli.fetch_processo.return_value = None

    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila):
        out = sync_processo(p, client=cli)

    assert out['encontrado'] is False
    assert [c[0][0] for c in fila.enqueue.call_args_list] == [
        'search.jobs.indexar_processos_bulk']


@pytest.mark.django_db(transaction=True)
def test_update_carimba_atualizado_em_para_o_poller_enxergar():
    """`auto_now` só roda em `Model.save()` — `.update()` o IGNORA.

    Este é o buraco que NÃO tinha poller: `sync_processos_atualizados` é keyset
    por `atualizado_em`, então uma linha alterada por `.update()` era invisível
    para ele. Medido: 0 de 500 processos tocados nas últimas 2 horas tinham o
    doc em dia com a escrita.
    """
    from datajud.ingestion import sync_processo
    from tribunals.models import Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5004444-11.2025.4.04.7100')
    Process.objects.filter(pk=p.pk).update(
        atualizado_em=timezone.now() - dt.timedelta(days=30))
    antes = Process.objects.values_list('atualizado_em', flat=True).get(pk=p.pk)

    cli = mock.MagicMock()
    cli.fetch_processo.return_value = _source(p.numero_cnj, n_movs=1)
    with mock.patch('django_rq.get_queue', return_value=mock.MagicMock()):
        sync_processo(p, client=cli)

    depois = Process.objects.values_list('atualizado_em', flat=True).get(pk=p.pk)
    assert depois > antes, 'sem isto o poller de processos nunca vê a mudança'


@pytest.mark.django_db(transaction=True)
def test_fila_fora_do_ar_derruba_a_sincronizacao_em_vez_de_calar():
    """Fila fora ⇒ a sincronização NÃO chegou ao índice ⇒ o job falha alto e o
    RQ retenta (`DATAJUD_RETRY`). Engolir aqui produziria o estado que este
    arquivo existe para impedir: `novos=N` no retorno e nada no índice."""
    from datajud.ingestion import sync_processo
    from tribunals.models import Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5005555-11.2025.4.04.7100')
    cli = mock.MagicMock()
    cli.fetch_processo.return_value = _source(p.numero_cnj, n_movs=2)

    with mock.patch('django_rq.get_queue', side_effect=RuntimeError('redis fora')), \
         pytest.raises(RuntimeError):
        sync_processo(p, client=cli)


# ── 2. o gate mede OS DOIS LADOS, com o MESMO critério ───────────────────────
@pytest.mark.django_db
def test_gate_acha_exatamente_o_que_falta_e_reenfileira_so_isso():
    """O reparo é caro. Ele não pode re-enfileirar a janela inteira por causa
    de duas linhas ausentes — a fila `es_index` já chegou a 1,68 milhão."""
    from datajud import indice
    from tribunals.models import Movimentacao, Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5006666-11.2025.4.04.7100')
    ids = []
    for i in range(5):
        m = Movimentacao.objects.create(
            processo=p, tribunal=t, external_id=f'datajud:{i:024d}',
            data_disponibilizacao=timezone.now(), texto='x')
        ids.append(m.id)

    ini = timezone.now() - dt.timedelta(hours=1)
    fim = timezone.now() + dt.timedelta(hours=1)
    ausentes = [ids[1], ids[3]]
    with mock.patch.object(indice.gate, 'ausentes_no_bloco', return_value=ausentes), \
         mock.patch.object(indice.gate, 'enfileirar_movs',
                           side_effect=lambda p, *a: len(p)) as enf:
        r = indice.conferir_movs(ini, fim)
    assert r['pg'] == 5 and r['faltando'] == 2 and r['enfileiradas'] == 2
    assert enf.call_args[0][0] == ausentes


@pytest.mark.django_db
def test_gate_ignora_o_que_nao_e_desta_porta():
    """O recorte é a janela de ESCRITA da PORTA. Uma linha do DJEN na mesma
    janela não é dívida do Datajud — e o DJEN, sob recuperação nacional,
    escreve 109.796 linhas/h contra as 27.468/h desta porta (medido em
    24/08/2026). Sem o filtro, o gate acusaria o país inteiro."""
    from datajud import indice
    from tribunals.models import Movimentacao, Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5007777-11.2025.4.04.7100')
    Movimentacao.objects.create(processo=p, tribunal=t, external_id='datajud:abc',
                                data_disponibilizacao=timezone.now(), texto='x')
    Movimentacao.objects.create(processo=p, tribunal=t, external_id='987654321',
                                data_disponibilizacao=timezone.now(), texto='x')

    ini = timezone.now() - dt.timedelta(hours=1)
    fim = timezone.now() + dt.timedelta(hours=1)
    with mock.patch.object(indice.gate, 'ausentes_no_bloco', return_value=[]):
        r = indice.conferir_movs(ini, fim)
    assert r['pg'] == 1, 'só a linha com external_id datajud: entra no recorte'


@pytest.mark.django_db
def test_es_mudo_vira_abstencao_e_nunca_zero():
    """Abster > chutar (regra nº 6). Zero faria a régua dizer "está tudo no
    índice" sem ter olhado — e, do outro lado do sinal, faria o reparo
    re-enfileirar a janela inteira por nada."""
    from datajud import indice
    from tribunals.models import Movimentacao, Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5008888-11.2025.4.04.7100')
    Movimentacao.objects.create(processo=p, tribunal=t, external_id='datajud:zzz',
                                data_disponibilizacao=timezone.now(), texto='x')

    ini = timezone.now() - dt.timedelta(hours=1)
    fim = timezone.now() + dt.timedelta(hours=1)
    with mock.patch.object(indice.gate, 'ausentes_no_bloco',
                           side_effect=RuntimeError('es fora')):
        r = indice.conferir_movs(ini, fim)
    assert r['faltando'] is None and r['faltando'] != 0
    assert r['abstido'] is True


@pytest.mark.django_db
def test_doc_anterior_a_escrita_conta_como_atrasado():
    """O critério exato do lado dos processos: `doc.enriquecido_em` menor que o
    `data_enriquecimento_datajud` do Postgres significa que o documento é
    ANTERIOR à escrita desta porta. Medido: 0 de 500 em dia na janela de 2h."""
    from datajud import indice

    agora = timezone.now()
    pares = [(1, agora), (2, agora), (3, agora)]

    class ESFalso:
        def mget(self, index, ids, source, request_timeout):
            resp = {
                '1': {'_id': '1', 'found': True,
                      'enriquecido_em': (agora - dt.timedelta(days=1)).isoformat()},
                '2': {'_id': '2', 'found': True,
                      'enriquecido_em': (agora + dt.timedelta(seconds=1)).isoformat()},
                '3': {'_id': '3', 'found': False},
            }
            docs = []
            for i in ids:
                d = resp[i]
                docs.append({'_id': d['_id'], 'found': d['found'],
                             '_source': ({'enriquecido_em': d['enriquecido_em']}
                                         if d['found'] else None)})
            return {'docs': docs}

    with mock.patch.object(indice.gate, '_es', return_value=ESFalso()):
        atrasados = indice._atrasados_no_bloco(pares)
    assert atrasados == [1, 3], 'doc velho e doc ausente contam; doc novo, não'


# ── 3. teto é ALERTA e o watermark não passa por cima do que não foi medido ──
@pytest.mark.django_db
def test_abstencao_no_meio_da_faixa_para_o_watermark_no_passo_anterior():
    """Regra nº 2 + regra nº 6: o watermark é o instante até onde a conferência
    FECHOU. Avançá-lo por cima de um passo abstido daria por conferido o que
    ninguém olhou — e o keyset só anda pra frente."""
    from datajud import indice

    ini = timezone.now().replace(microsecond=0) - dt.timedelta(hours=1)
    fim = ini + dt.timedelta(minutes=45)
    ok = {'pg': 10, 'faltando': 0, 'enfileiradas': 0, 'teto_atingido': False,
          'abstido': False}
    mudo = {'pg': None, 'faltando': None, 'enfileiradas': 0, 'teto_atingido': False,
            'abstido': True}
    procs_ok = {'pg': 1, 'atrasados': 0, 'enfileirados': 0, 'teto_atingido': False,
                'abstido': False}
    with mock.patch.object(indice, 'conferir_movs', side_effect=[ok, mudo]), \
         mock.patch.object(indice, 'conferir_processos', return_value=procs_ok):
        r = indice.conferir_janela(ini, fim, passo_min=15)
    assert r['abstidos'] == 1
    assert r['ate'] == (ini + dt.timedelta(minutes=15)).isoformat(), \
        'parou no fim do último passo que fechou'


@pytest.mark.django_db
def test_teto_de_leitura_e_erro_e_nao_corte_mudo():
    """Bater o teto não pode virar `return` discreto: a janela continua em
    dívida e a próxima passada tem que recomeçar dela."""
    from datajud import indice
    from tribunals.models import Movimentacao, Process

    t = _tribunal()
    p = Process.objects.create(tribunal=t, numero_cnj='5009999-11.2025.4.04.7100')
    for i in range(4):
        Movimentacao.objects.create(
            processo=p, tribunal=t, external_id=f'datajud:teto{i}',
            data_disponibilizacao=timezone.now(), texto='x')

    ini = timezone.now() - dt.timedelta(hours=1)
    fim = timezone.now() + dt.timedelta(hours=1)
    with mock.patch.object(indice, 'TETO_IDS_MOVS', 2), \
         mock.patch.object(indice.gate, 'ausentes_no_bloco', return_value=[]):
        r = indice.conferir_movs(ini, fim)
    assert r['teto_atingido'] is True and r['pg'] == 2


@pytest.mark.django_db
def test_watermark_ausente_reancora_para_tras_e_nunca_no_topo():
    """Quando a chave do watermark some, o poller de movimentações RE-ANCORA NO
    TOPO e o que ficou abaixo nunca mais é lido — foi assim que 27.619 linhas
    do diário ficaram fora do índice. O gate faz o contrário: volta
    `RE_ANCORA_HORAS` e GRITA."""
    from django.core.cache import cache

    from datajud import indice

    cache.delete(indice.WM)
    agora = timezone.now()
    try:
        with mock.patch.object(indice, 'conferir_janela',
                               return_value={'passos': 0, 'movs_pg': 0, 'movs_fora': 0,
                                             'movs_enfileiradas': 0, 'procs_pg': 0,
                                             'procs_atrasados': 0, 'procs_enfileirados': 0,
                                             'abstidos': 0, 'teto': False,
                                             'ate': agora.isoformat()}) as cj:
            out = indice.tick(agora=agora)
        assert out['reancorou'] is True
        de = dt.datetime.fromisoformat(out['de'])
        assert de < agora - dt.timedelta(hours=indice.RE_ANCORA_HORAS - 0.01), \
            're-ancorar no topo é o defeito, não a cura'
        assert cj.call_count == 1
    finally:
        cache.delete(indice.WM)


@pytest.mark.django_db
def test_gate_desligado_e_no_op_sem_deploy():
    """Kill switch: `DATAJUD_GATE_INDICE_ENABLED=0` vira no-op."""
    from django.test import override_settings

    from datajud import indice

    with override_settings(DATAJUD_GATE_INDICE_ENABLED=False):
        assert indice.gate_ativo() is False
        assert 'skip' in indice.tick()
