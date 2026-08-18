"""O teto do sync não pode ser um corte mudo.

CONTEXTO (medido em 18/08/2026, amostrando os DOIS lados por faixa de id).

`LIMITE_MOVS_NOVAS` era 50.000 e o tick roda a cada 10 minutos: teto de 300
mil/h. Sob a recuperação do acervo a ingestão escreve muito mais, e o watermark
não acompanha. Resultado:

    abaixo do watermark ..... 9 de 9 janelas de id IDÊNTICAS ao Postgres
    acima do watermark ...... 190.320.885 no PG, 10.830.272 no ES
                              → 179.490.613 publicações fora do índice

E o sintoma visível era uma fila `es_index` **zerada**. Run verde, log limpo,
número redondo — os três sinais da tabela do CLAUDE.md ao mesmo tempo, e nenhum
deles mentindo tecnicamente: a fila estava vazia mesmo, porque nada enfileirava.

O que estes testes protegem:
  1. bater o teto vira ERRO com o tamanho do atraso — nunca `return` discreto;
  2. o tick freia pela FILA, não por um número fixo (empurrar pra fila cheia não
     aumenta vazão, só esconde o atraso num número maior);
  3. o watermark nunca pula id: ele avança só até onde de fato enfileirou.
"""
from unittest.mock import MagicMock, patch

import pytest

from search import sync_incremental as S


@pytest.fixture
def sem_cache():
    valores = {}
    with patch.object(S, 'cache') as c:
        c.get.side_effect = lambda k, *a: valores.get(k)
        c.set.side_effect = lambda k, v, *a, **kw: valores.__setitem__(k, v)
        yield valores


def _mock_movs(ids, topo=None):
    """Faz `Movimentacao.objects.filter(id__gt=...)` devolver `ids`."""
    qs = MagicMock()
    qs.order_by.return_value.values_list.return_value.__getitem__ = lambda _s, _k: ids
    topo_qs = MagicMock()
    topo_qs.values_list.return_value.first.return_value = topo if topo is not None else (
        ids[-1] if ids else 0)
    m = MagicMock()
    m.objects.filter.return_value = qs
    m.objects.order_by.return_value = topo_qs
    return m


@pytest.mark.django_db
def test_bater_o_teto_vira_erro_com_o_atraso(sem_cache):
    """Espiona o logger direto: o projeto configura logging com handler próprio,
    e o `caplog` do pytest não vê o registro."""
    sem_cache[S._WM_MOV_ID] = 1_000
    ids = list(range(1_001, 1_001 + S.LIMITE_MOVS_NOVAS))
    topo = ids[-1] + 179_000_000

    with patch.object(S, 'Movimentacao', _mock_movs(ids, topo=topo)), \
         patch.object(S, '_enfileirar_movs', lambda pks: len(pks)), \
         patch.object(S, '_fila_es', lambda: 0), \
         patch.object(S.logger, 'error') as erro:
        r = S.sync_movimentacoes_novas()

    assert r['atraso_ids'] == 179_000_000
    assert erro.called, 'teto silencioso — foi assim que 179 milhões ficaram fora'
    # o tamanho do atraso tem que estar NO registro; "bateu o teto" sozinho não
    # diz se faltam mil ou duzentos milhões
    assert 179_000_000 in erro.call_args.args


@pytest.mark.django_db
def test_sem_bater_o_teto_nao_alarma(sem_cache):
    sem_cache[S._WM_MOV_ID] = 1_000
    ids = list(range(1_001, 1_101))

    with patch.object(S, 'Movimentacao', _mock_movs(ids)), \
         patch.object(S, '_enfileirar_movs', lambda pks: len(pks)), \
         patch.object(S, '_fila_es', lambda: 0), \
         patch.object(S.logger, 'error') as erro:
        r = S.sync_movimentacoes_novas()

    assert 'atraso_ids' not in r
    assert not erro.called, 'alarmou sem motivo — alerta que grita sempre vira ruído'


@pytest.mark.django_db
def test_freia_quando_a_fila_esta_cheia(sem_cache):
    """Empurrar pra fila cheia não aumenta vazão: o gargalo é o dreno."""
    sem_cache[S._WM_MOV_ID] = 1_000
    enfileirou = []

    with patch.object(S, 'Movimentacao', _mock_movs(list(range(1_001, 2_000)))), \
         patch.object(S, '_enfileirar_movs', lambda pks: enfileirou.extend(pks)), \
         patch.object(S, '_fila_es', lambda: S.FILA_ES_ALTA + 1):
        r = S.sync_movimentacoes_novas()

    assert r['pulou'] is True
    assert enfileirou == []
    assert sem_cache[S._WM_MOV_ID] == 1_000, 'avançou o watermark sem enfileirar — pularia ids'


@pytest.mark.django_db
def test_watermark_avanca_so_ate_onde_enfileirou(sem_cache):
    """A garantia de completude do sync: nada entre o watermark antigo e o novo
    pode ficar sem passar pela fila."""
    sem_cache[S._WM_MOV_ID] = 500
    ids = [501, 502, 777, 900]

    with patch.object(S, 'Movimentacao', _mock_movs(ids, topo=5_000)), \
         patch.object(S, '_enfileirar_movs', lambda pks: len(pks)), \
         patch.object(S, '_fila_es', lambda: 0):
        S.sync_movimentacoes_novas()

    assert sem_cache[S._WM_MOV_ID] == 900


def test_o_teto_cabe_na_janela_do_tick():
    """Contrato numérico: com tick de 10 min, o teto tem que dar conta do que a
    ingestão escreve na recuperação (~1,5M/h medidos no pico)."""
    por_hora = S.LIMITE_MOVS_NOVAS * 6
    assert por_hora >= 2_000_000, f'teto de {por_hora:,}/h volta a ser corte mudo'
