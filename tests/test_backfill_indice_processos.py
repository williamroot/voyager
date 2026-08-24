"""Backfill do índice de PROCESSOS — o passivo de 14,2 milhões e as três armadilhas.

## O incidente (24/08/2026)

Medido em produção com amostra ALEATÓRIA de 4.000 pks por faixa (seed 20260824)
e `_mget` por id — resposta exata por documento, não estimativa:

    faixa de pk                   existem   fora do índice
    3.520-13.042.773                3.987     0 (  0,00%)
    13.042.774-26.082.028           3.984     0 (  0,00%)
    26.082.029-39.121.283           3.988     0 (  0,00%)
    39.121.284-52.160.538           3.993     0 (  0,00%)
    52.160.539-65.199.793           3.989     0 (  0,00%)
    65.199.794-78.239.048           3.390 1.559 ( 45,99%)
    78.239.049-91.278.303           3.992   377 (  9,44%)
    91.278.304-104.317.558          3.978 2.444 ( 61,44%)
    -----------------------------------------------------
    TOTAL                          31.301 4.380 ( 13,99%)

O acervo antigo estava inteiro no índice; faltavam os processos NOVOS — os que
entraram por `bulk_create`/`.update()`, que não disparam `post_save` e portanto
nunca acionaram o write-through de `search/signals.py`.

## As três armadilhas que estes testes travam

1. **`_cat/indices` conta documentos LUCENE.** `voyager-processos` tem
   `participacoes` como `nested`, e cada objeto aninhado é um doc Lucene. O
   `_cat` marcava **104.594.795** contra **87.709.209** do `_count` — 16,9
   milhões de diferença. Quem contasse pelo `_cat` concluiria que o índice tem
   MAIS processos do que o Postgres tem linhas, e que não falta nada.
2. **`_bulk` fechado por DOCUMENTOS.** Do lado das movimentações isso já custou
   83 jobs mortos com `ApiError(413)` e 45.313 publicações fora do índice
   (21/08/2026). `indexar_processos_bulk` tinha o mesmo defeito latente: o doc
   do processo carrega `partes`/`advs`, a concatenação de TODAS as
   participações.
3. **Teto/ES mudo virando "não falta nada".** Postgres que não responde tem de
   ABSTER (regra nº 6) e parar o checkpoint; se ele devolvesse lista vazia, o
   checkpoint pularia o resto do acervo e o buraco viraria permanente.
"""
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.db import OperationalError

from search import backfill_processos as bp


@pytest.fixture(autouse=True)
def _limpa_cache():
    cache.delete(bp.WM)
    cache.delete(bp.OFF)
    yield
    cache.delete(bp.WM)
    cache.delete(bp.OFF)


# --------------------------------------------------------------------------- #
# 1. O censo: keyset, checkpoint e o botão de parar
# --------------------------------------------------------------------------- #
@patch('search.jobs.indexar_processos_bulk')
@patch('search.backfill_processos.gate.ausentes_no_bloco')
@patch('search.backfill_processos._ids_do_bloco')
@patch('search.backfill_processos._topo', return_value=30)
@patch('search.backfill_processos.baseline',
       return_value={'processos_ms': 100.0, 'conteudo_ms': 200.0, 'n': 9,
                     'erros': 0, 'processos_amostras': [], 'conteudo_amostras': [],
                     'processos_max': 0, 'conteudo_max': 0})
def test_censo_conta_os_dois_lados_e_indexa_so_o_que_falta(
        _base, _topo, mock_ids, mock_ausentes, mock_idx):
    # 2 blocos de 10 pks; no 1º faltam 3 docs, no 2º não falta nada.
    mock_ids.side_effect = [list(range(1, 11)), list(range(11, 21)), []]
    mock_ausentes.side_effect = [[2, 5, 7], []]
    mock_idx.return_value = 3

    r = bp.rodar(de=0, ate=30, sleep=0, bloco=10, usar_checkpoint=False)

    assert r['lidos'] == 20
    assert r['fora'] == 3
    assert r['indexados'] == 3
    # o caro (ler linha + participações + montar doc) só roda pros AUSENTES —
    # é a diferença entre reindexar 102 milhões e consertar 14
    mock_idx.assert_called_once_with([2, 5, 7])


@patch('search.jobs.indexar_processos_bulk', return_value=0)
@patch('search.backfill_processos.gate.ausentes_no_bloco', return_value=[])
@patch('search.backfill_processos._ids_do_bloco')
@patch('search.backfill_processos.baseline',
       return_value={'processos_ms': 1.0, 'conteudo_ms': 1.0, 'n': 9, 'erros': 0,
                     'processos_amostras': [], 'conteudo_amostras': [],
                     'processos_max': 0, 'conteudo_max': 0})
def test_checkpoint_retoma_de_onde_parou(_base, mock_ids, _aus, _idx):
    mock_ids.side_effect = [[1, 2, 3], []]
    bp.rodar(de=0, ate=100, sleep=0, bloco=3)
    assert cache.get(bp.WM) == 3

    # a corrida seguinte NÃO recomeça do zero: pede ids a partir do checkpoint
    mock_ids.reset_mock()
    mock_ids.side_effect = [[4, 5, 6], []]
    bp.rodar(de=0, ate=100, sleep=0, bloco=3)
    assert mock_ids.call_args_list[0].args[0] == 3
    assert cache.get(bp.WM) == 6


@patch('search.jobs.indexar_processos_bulk', return_value=0)
@patch('search.backfill_processos.gate.ausentes_no_bloco', return_value=[])
@patch('search.backfill_processos._ids_do_bloco')
@patch('search.backfill_processos.baseline',
       return_value={'processos_ms': 1.0, 'conteudo_ms': 1.0, 'n': 9, 'erros': 0,
                     'processos_amostras': [], 'conteudo_amostras': [],
                     'processos_max': 0, 'conteudo_max': 0})
def test_kill_switch_para_com_checkpoint_salvo(_base, mock_ids, _aus, _idx):
    """Botão de parar: a corrida encerra na virada do bloco, sem perder o lugar."""
    mock_ids.side_effect = [[1, 2, 3]]
    cache.set(bp.OFF, True)
    r = bp.rodar(de=0, ate=100, sleep=0, bloco=3)
    assert r['parou_por'] == 'kill-switch'
    assert r['lidos'] == 0


# --------------------------------------------------------------------------- #
# 2. Abster > chutar — o defeito que tornaria o buraco PERMANENTE
# --------------------------------------------------------------------------- #
@patch('search.backfill_processos.connection')
@patch('search.backfill_processos.transaction')
def test_postgres_mudo_abstem_em_vez_de_devolver_lista_vazia(_tx, mock_conn):
    """PG que estoura o timeout devolve None, NUNCA [].

    `[]` significa "acabou a tabela" e faria a corrida encerrar com o
    checkpoint carimbado no meio do acervo: o resto ficaria fora da busca para
    sempre, com o comando dizendo "faixa concluída" (regra nº 6).
    """
    mock_conn.cursor.return_value.__enter__.return_value.execute.side_effect = (
        OperationalError('canceling statement due to statement timeout'))
    assert bp._ids_do_bloco(0, 100, 10) is None


@patch('search.jobs.indexar_processos_bulk', return_value=0)
@patch('search.backfill_processos.gate.ausentes_no_bloco',
       side_effect=RuntimeError('ES fora do ar'))
@patch('search.backfill_processos._ids_do_bloco', return_value=[1, 2, 3])
@patch('search.backfill_processos.baseline',
       return_value={'processos_ms': 1.0, 'conteudo_ms': 1.0, 'n': 9, 'erros': 0,
                     'processos_amostras': [], 'conteudo_amostras': [],
                     'processos_max': 0, 'conteudo_max': 0})
def test_es_mudo_nao_vira_bloco_conferido(_base, _ids, _aus, _idx):
    """ES sem resposta é "não sei" — o checkpoint NÃO passa por cima do bloco."""
    r = bp.rodar(de=0, ate=100, sleep=0, bloco=3, usar_checkpoint=False)
    assert r['parou_por'] == 'es-mudo'
    assert r['abstidos'] == 1
    assert r['fora'] == 0 and r['lidos'] == 0     # nada foi DADO POR CONFERIDO


@patch('search.jobs.indexar_processos_bulk', return_value=0)
@patch('search.backfill_processos._ids_do_bloco', return_value=None)
@patch('search.backfill_processos.baseline',
       return_value={'processos_ms': 1.0, 'conteudo_ms': 1.0, 'n': 9, 'erros': 0,
                     'processos_amostras': [], 'conteudo_amostras': [],
                     'processos_max': 0, 'conteudo_max': 0})
def test_pg_mudo_nao_avanca_o_checkpoint(_base, _ids, _idx):
    cache.set(bp.WM, 7, None)
    r = bp.rodar(de=0, ate=100, sleep=0, bloco=3)
    assert r['parou_por'] == 'pg-mudo'
    assert cache.get(bp.WM) == 7      # continua onde estava


# --------------------------------------------------------------------------- #
# 3. Reparo incompleto é ERRO REGISTRADO, nunca silêncio (regra nº 2)
# --------------------------------------------------------------------------- #
def test_reparo_que_indexou_menos_do_que_faltava_grita():
    """`_bulk` que aceita 400 de 500 é exatamente a perda que isto fecha.

    Espiona o logger direto: o projeto configura `voyager` com handler próprio
    e `propagate: False`, então o `caplog` do pytest não vê o registro (mesmo
    padrão de tests/test_sync_es_teto.py).
    """
    with patch.object(bp.logger, 'error') as erro:
        n = bp._reparar([1, 2, 3, 4], 1, 4, lambda pks: 2)
    assert n == 2
    assert erro.called, 'reparo incompleto silencioso'
    # o TAMANHO da perda tem de estar no registro, não só "deu ruim"
    assert 2 in erro.call_args.args


# --------------------------------------------------------------------------- #
# 4. O `_bulk` de PROCESSOS fecha por BYTES (armadilha nº 2)
# --------------------------------------------------------------------------- #
@patch('search.jobs._enviar_bulk')
@patch('search.jobs.processo_to_doc')
@patch('search.jobs.Process')
def test_bulk_de_processos_fecha_por_bytes_e_nao_por_documentos(
        mock_proc, mock_doc, mock_env):
    """500 documentos podem passar de 100 MB — o teto do ES é de BYTES.

    Do lado das movimentações este mesmo defeito matou 83 jobs com
    `ApiError(413)` e deixou 45.313 publicações fora do índice (21/08/2026),
    porque ninguém reprocessa o `FailedJobRegistry`. O doc do processo tem o
    mesmo formato de risco: `partes`/`advs` concatenam TODAS as participações.
    """
    from search import jobs

    # 4 processos, cada um com um `partes` de 8 MB ⇒ 2 por lote (teto 20 MB)
    procs = [MagicMock(id=i) for i in range(1, 5)]
    (mock_proc.objects.filter.return_value.select_related.return_value
     .prefetch_related.return_value) = procs
    mock_doc.side_effect = lambda p: {'id': p.id, 'partes': 'x' * (8 * 1024 * 1024),
                                      'advs': ''}
    mock_env.return_value = 2

    jobs.indexar_processos_bulk([1, 2, 3, 4])

    assert mock_env.call_count == 2, 'devia ter fechado o lote por BYTES'
    for chamada in mock_env.call_args_list:
        ops = chamada.args[0]
        assert len(ops) == 4      # 2 documentos = 2 pares (ação, doc)


@patch('search.jobs._enviar_bulk', return_value=3)
@patch('search.jobs.processo_to_doc', side_effect=lambda p: {'id': p.id})
@patch('search.jobs.Process')
def test_bulk_de_processos_devolve_quantos_o_es_aceitou(mock_proc, _doc, _env):
    """Devolver `None` escondia a diferença entre "mandei 500" e "entraram 400"."""
    from search import jobs

    (mock_proc.objects.filter.return_value.select_related.return_value
     .prefetch_related.return_value) = [MagicMock(id=i) for i in (1, 2, 3)]
    assert jobs.indexar_processos_bulk([1, 2, 3]) == 3


# --------------------------------------------------------------------------- #
# 5. O freio — a busca do site tem prioridade
# --------------------------------------------------------------------------- #
def _base(proc=340.0, cont=7109.0):
    """A baseline REAL medida em produção em 24/08/2026 (mediana de 9 sondas)."""
    return {'processos_ms': proc, 'conteudo_ms': cont, 'n': 9, 'erros': 0,
            'processos_amostras': [], 'conteudo_amostras': [],
            'processos_max': 0, 'conteudo_max': 0}


def test_limiar_de_conteudo_e_capado_para_nao_virar_o_timeout():
    """4x de 7.109 ms daria 28.435 ms — praticamente o `ES_TIMEOUT` do cliente.

    Um freio calibrado ali só agiria depois que a busca já morreu, que é o
    mesmo que não ter freio.
    """
    f = bp.Freio(_base(), sleep_inicial=0.1)
    assert f.parada_cont == bp.TETO_PARADA_MS == 25_000.0
    assert f.freio_cont <= bp.TETO_FREIO_MS


def test_limiar_de_processos_tem_piso_para_nao_disparar_com_ruido():
    """2x de 340 ms daria 680 ms — dentro do ruído da JVM deste nó."""
    f = bp.Freio(_base(), sleep_inicial=0.1)
    assert f.freio_proc == bp.PISO_FREIO_MS == 1_000.0
    assert f.parada_proc == bp.PISO_PARADA_MS == 3_000.0


@patch('search.backfill_processos.sondar')
def test_freio_dobra_o_sleep_quando_a_busca_de_processos_piora(mock_sondar):
    """O índice ESCRITO é o sinal mais sensível — merge/refresh batem nele antes."""
    mock_sondar.return_value = {'processos_ms': 2_000.0, 'conteudo_ms': 50.0,
                                'erros': 0, 'i': 0}
    f = bp.Freio(_base(), sleep_inicial=0.1)
    assert f.avaliar() is True
    assert f.sleep == 0.2 and f.freadas == 1


@patch('search.backfill_processos.time.sleep')
@patch('search.backfill_processos.sondar')
def test_freio_aborta_a_corrida_se_a_busca_nao_voltar(mock_sondar, _sleep):
    """Desistir é ERRO REGISTRADO + checkpoint salvo — dívida VISÍVEL."""
    mock_sondar.return_value = {'processos_ms': 30_000.0, 'conteudo_ms': 30_000.0,
                                'erros': 1, 'i': 0}
    f = bp.Freio(_base(), sleep_inicial=0.1)
    assert f.avaliar() is False
    assert f.paradas == bp.PAUSA_TENTATIVAS + 1


@patch('search.backfill_processos.sondar')
def test_uma_leitura_fria_isolada_nao_para_o_backfill(mock_sondar):
    """Neste cluster um 10 s solto é NORMAL: está na própria baseline medida
    (83,9 · 88,7 · 136,5 · 289,7 · 346,4 · 2.545,2 · 10.116,5 ms na MESMA busca).

    A decisão é pela MEDIANA de 3 sondas com termos diferentes; um freio que
    dispara por acaso é desligado pelo primeiro operador que o vê.
    """
    mock_sondar.side_effect = [
        {'processos_ms': 100.0, 'conteudo_ms': 30_000.0, 'erros': 0, 'i': 0},
        {'processos_ms': 100.0, 'conteudo_ms': 90.0, 'erros': 0, 'i': 1},
        {'processos_ms': 100.0, 'conteudo_ms': 120.0, 'erros': 0, 'i': 2},
    ]
    f = bp.Freio(_base(), sleep_inicial=0.1)
    assert f.avaliar() is True
    assert f.paradas == 0


def test_sonda_usa_termos_rotacionados():
    """Repetir a MESMA busca mede "o disco já tem isto em cache", não a latência.

    Medido: 7 rodadas do mesmo termo deram 83,9 ms na melhor e 10.116,5 ms na
    pior — dois regimes, não ruído.
    """
    assert len(set(bp.TERMOS_CONTEUDO)) >= 9
    assert len(set(bp.TERMOS_PARTE)) >= 9


@patch('search.backfill_processos.time.monotonic', side_effect=[0.0, 3.0, 3.0, 8.0])
def test_busca_que_estoura_o_timeout_conta_como_a_pior_latencia(_mono):
    """Timeout não é "medição indisponível": para quem está na tela, a busca não
    voltou. Contar o tempo gasto é a leitura honesta E a conservadora.

    Na primeira execução em produção o timeout do cliente derrubou o comando
    inteiro antes do primeiro bloco.
    """
    with patch('search.busca_api.buscar_processos', side_effect=TimeoutError), \
         patch('search.busca_api.buscar_movimentacoes', side_effect=TimeoutError):
        s = bp.sondar(0)
    assert s['erros'] == 2
    assert s['processos_ms'] == 3_000.0 and s['conteudo_ms'] == 5_000.0


# --------------------------------------------------------------------------- #
# 6. A sentinela — detecta se o buraco reabrir
# --------------------------------------------------------------------------- #
@patch('search.backfill_processos.amostrar')
def test_sentinela_grita_quando_o_buraco_reabre(mock_am):
    mock_am.return_value = {'faixas': [{'faixa': 7, 'lo': 9, 'hi': 10, 'pct': 61.44,
                                        'existem': 3978, 'fora': 2444}],
                            'seed': 1, 'existem': 3978, 'fora': 2444, 'pct': 61.44,
                            'abstidos': 0, 'min_pk': 1, 'max_pk': 10}
    with patch.object(bp.logger, 'error') as erro:
        bp.conferir_indice_processos()
    assert erro.called
    assert 'reabriu' in erro.call_args.args[0]


@patch('search.backfill_processos.amostrar')
def test_sentinela_cala_quando_o_indice_esta_fechado(mock_am):
    mock_am.return_value = {'faixas': [], 'seed': 1, 'existem': 31301, 'fora': 0,
                            'pct': 0.0, 'abstidos': 0, 'min_pk': 1, 'max_pk': 10}
    with patch.object(bp.logger, 'error') as erro:
        bp.conferir_indice_processos()
    assert not erro.called


@patch('search.backfill_processos.amostrar')
def test_sentinela_com_es_mudo_avisa_que_a_medicao_ficou_incompleta(mock_am):
    """Faixa sem resposta do ES não pode passar por "0 fora" (regra nº 6)."""
    mock_am.return_value = {'faixas': [{'faixa': 0, 'lo': 1, 'hi': 5, 'pct': None,
                                        'existem': 100, 'fora': None}],
                            'seed': 1, 'existem': 0, 'fora': 0, 'pct': None,
                            'abstidos': 1, 'min_pk': 1, 'max_pk': 10}
    with patch.object(bp.logger, 'warning') as aviso:
        bp.conferir_indice_processos()
    assert aviso.called
    assert 'INCOMPLETA' in aviso.call_args.args[0]
