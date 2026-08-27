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


# ─────────────────────────── os DOIS lados de PROCESSO (medido em 26/08/2026)

def _mock_process(ids, topo=None):
    """`Process.objects.filter(...)` devolve `ids`; `order_by('-id').first()` o topo."""
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
def test_proc_novos_freia_pela_fila(sem_cache):
    """O lado de PROCESSO não tinha o freio da fila — só as movimentações.

    Sem o freio, subir o teto (que era 20.000 e batia em 6 de 6 ticks) só trocaria
    "atraso no watermark" por "atraso na fila": o mesmo dado invisível, num número
    maior. Medido em 26/08: 7,72 M de pks atrás COM a fila `es_index` em ZERO —
    prova de que o gargalo era o teto, não o dreno; mas o inverso pode acontecer
    a qualquer momento e aí o freio é que segura.
    """
    sem_cache[S._WM_PROC_ID] = 1_000
    enfileirou = []

    with patch.object(S, 'Process', _mock_process(list(range(1_001, 2_000)))), \
         patch.object(S, '_enfileirar_processos', lambda pks: enfileirou.extend(pks)), \
         patch.object(S, 'computar_sinal', lambda pks: len(pks)), \
         patch.object(S, '_fila_es', lambda: S.FILA_ES_ALTA + 1):
        r = S.sync_processos_novos()

    assert r['pulou'] is True
    assert enfileirou == []
    assert sem_cache[S._WM_PROC_ID] == 1_000, 'avançou o watermark sem enfileirar'


@pytest.mark.django_db
def test_proc_atualizados_freia_pela_fila(sem_cache):
    import datetime
    from django.utils import timezone
    sem_cache[S._WM_PROC_TS] = timezone.now() - datetime.timedelta(hours=1)
    antes = sem_cache[S._WM_PROC_TS]
    enfileirou = []

    with patch.object(S, 'Process', _mock_process(list(range(1, 500)))), \
         patch.object(S, '_enfileirar_processos', lambda pks: enfileirou.extend(pks)), \
         patch.object(S, '_fila_es', lambda: S.FILA_ES_ALTA + 1):
        r = S.sync_processos_atualizados()

    assert r['pulou'] is True
    assert enfileirou == []
    assert sem_cache[S._WM_PROC_TS] == antes


def test_teto_de_atualizados_acompanha_o_relogio():
    """Contrato numérico do lado que DIVERGIA.

    `proc_atualizados` tem watermark de TEMPO, então o teto certo não se mede em
    ids e sim em quanto de relógio ele consegue cobrir por tick. Com 10.000/tick
    a watermark andava **45 s por tick de 600 s** — perdia 13:1, e a idade subia
    (127,92 → 128,08 → 128,26 h em três tiques). Um teto que não converge não é
    teto, é vazamento.

    A escrita em lote medida no pior momento (recuperação nacional + 4 backfills)
    ficou em ~1,4 M de processos tocados por hora. O teto tem que passar disso
    com folga, senão a idade volta a crescer sozinha.
    """
    por_hora = S.LIMITE_PROC_ATUALIZADOS * 6
    assert por_hora >= 1_000_000, (
        f'teto de {por_hora:,}/h — abaixo da escrita em lote medida, volta a divergir')


def test_teto_de_novos_cabe_na_janela_do_tick():
    """`proc_novos` é limitado pelo `computar_sinal` INLINE, não pelo enqueue.

    Medido: ~7 ms por processo. Com tick de 10 min, o teto não pode passar de
    ~85.000 sem estourar a janela — e estourar a janela atrasa os outros dois
    syncs, que rodam DEPOIS dele no mesmo tique.
    """
    segundos = S.LIMITE_PROC_NOVOS * 0.007
    assert segundos <= 540, (
        f'{S.LIMITE_PROC_NOVOS:,} × 7 ms = {segundos:.0f}s — passa da janela de 10 min')
    assert S.LIMITE_PROC_NOVOS >= 40_000, 'teto baixo demais: o atraso não drena'


# ───────────────────── computar_sinal: teto de TEMPO, e a invariante do sinal

@pytest.mark.django_db
def test_sinal_que_estoura_o_teto_nao_deixa_o_watermark_passar(sem_cache):
    """A invariante deste sync: nenhum processo passa a fronteira sem sinal.

    INCIDENTE de 27/08/2026: o mesmo `EXISTS` com regex sobre
    `movimentacao.texto` rodou **12,7 h** num lote de 20.000 do TJSP e travou
    três jobs. O custo é por MOVIMENTAÇÃO, não por processo, e varia ~100×
    entre tribunais — por isso o teto aqui é de TEMPO, em bloco pequeno.

    Quando um bloco estoura, o tick NÃO pode enfileirar o resto nem empurrar a
    watermark por cima: os pks não cobertos voltam no próximo tick. Sem isto,
    subir o teto de `proc_novos` (que eu subi de 20.000 para 60.000) troca um
    vazamento por outro — processo indexado com `tem_sinal_precatorio` NULL,
    que é o mapa comercial mentindo por omissão.
    """
    sem_cache[S._WM_PROC_ID] = 1_000
    ids = list(range(1_001, 1_001 + 6_000))     # 3 blocos de 2.000
    enfileirados = []

    chamadas = {'n': 0}

    def sinal_falso(pks):
        # cobre só o primeiro bloco e "estoura" no segundo
        chamadas['n'] += 1
        return len(pks[:S.BLOCO_SINAL]), pks[:S.BLOCO_SINAL]

    with patch.object(S, 'Process', _mock_process(ids, topo=ids[-1] + 500)), \
         patch.object(S, 'computar_sinal', sinal_falso), \
         patch.object(S, '_enfileirar_processos',
                      lambda pks: (enfileirados.extend(pks), len(pks))[1]), \
         patch.object(S, '_fila_es', lambda: 0):
        r = S.sync_processos_novos()

    assert r['novos'] == S.BLOCO_SINAL, 'enfileirou pk sem sinal computado'
    assert enfileirados == ids[:S.BLOCO_SINAL]
    assert sem_cache[S._WM_PROC_ID] == ids[S.BLOCO_SINAL - 1], (
        'a watermark passou por cima do que não teve sinal — o resto some')


@pytest.mark.django_db
def test_sinal_que_nao_fecha_nem_o_primeiro_bloco_nao_anda(sem_cache):
    sem_cache[S._WM_PROC_ID] = 1_000
    enfileirados = []
    with patch.object(S, 'Process', _mock_process(list(range(1_001, 3_000)))), \
         patch.object(S, 'computar_sinal', lambda pks: (0, [])), \
         patch.object(S, '_enfileirar_processos', lambda pks: enfileirados.extend(pks)), \
         patch.object(S, '_fila_es', lambda: 0):
        r = S.sync_processos_novos()

    assert r.get('sinal_travou') is True
    assert enfileirados == []
    assert sem_cache[S._WM_PROC_ID] == 1_000


def test_o_bloco_do_sinal_e_pequeno_e_tem_teto_de_tempo():
    """Contrato numérico do que causou as 12,7 h."""
    assert S.BLOCO_SINAL <= 5_000, (
        f'bloco de {S.BLOCO_SINAL:,} — 20.000 no TJSP virou meio dia de bloqueio')
    assert S.TIMEOUT_SINAL, 'sem teto de tempo, um bloco lento trava o tick inteiro'


def test_watermark_de_atualizados_guarda_o_PAR_e_nao_so_o_instante():
    """Empate de instante no limite do teto some SEM ERRO — o pior formato.

    `sync_processos_atualizados` lê `atualizado_em > wm`. Escrita em lote grava
    milhares de linhas com o MESMO `now()`: reclassificação, backfill de
    `assunto_id`, backfill de `grau`, a própria campainha. Se o teto cai no meio
    de um desses grupos, o watermark vira aquele instante e o `>` do tique
    seguinte exclui **todas** as linhas do grupo — inclusive as que nunca foram
    lidas.

    A consulta continua válida, o log continua limpo e o número continua
    redondo: os três sinais da tabela do CLAUDE.md, outra vez.

    Por isso o keyset é `(atualizado_em, id)` e o watermark guarda o PAR.
    """
    fonte = open('search/sync_incremental.py').read()
    i = fonte.find('def sync_processos_atualizados')
    corpo = fonte[i:i + 2500]
    assert '(atualizado_em, id) > (%s, %s)' in corpo, 'keyset sem desempate por id'
    assert 'ORDER BY atualizado_em, id' in corpo
    assert 'cache.set(_WM_PROC_TS, (ultimo, pks[-1])' in corpo, (
        'guardou só o instante — o resto do grupo empatado some')


def test_watermark_antigo_do_cache_nao_quebra():
    """Quem já está com o formato velho (só o instante) tem que continuar."""
    import datetime
    from django.utils import timezone
    agora = timezone.now()
    assert S._wm_par(agora) == (agora, 0)
    assert S._wm_par((agora, 42)) == (agora, 42)
    assert S._wm_par([agora, '7']) == (agora, 7)
    assert S._wm_par(None) == (None, 0)
    del datetime
