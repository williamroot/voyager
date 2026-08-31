"""A watermark tem que sobreviver ao Redis — e saber a diferença entre nascer e perder.

CONTEXTO (medido em 31/08/2026, no Redis de produção `192.168.30.100`, lido de
dentro do `voyager-web-1`):

    save ...................... ''            (RDB DESLIGADO)
    appendonly ................ no            (AOF DESLIGADO)
    maxmemory-policy .......... noeviction
    evicted_keys .............. 0
    uptime_in_seconds ......... 469.336   ⇒ restart em 2026-08-26 06:59:02 UTC
    ttl de sync_es:wm:* ....... -1  (as três chaves)

Eviction e TTL estão DESCARTADOS por medição. Sobra o restart — e restart neste
Redis apaga o keyspace inteiro, porque não há RDB nem AOF. O código antigo lia
chave ausente como "primeiro tique da vida do sistema" e ancorava em
`agora`/`max(id)`: como o keyset só anda para frente, tudo que foi escrito antes
nunca mais era revisitado. Perda silenciosa e definitiva, por desenho.

O que estes testes protegem:
  1. perder a chave do Redis NÃO abandona o passado (a watermark volta do banco);
  2. o primeiro tique de verdade — e SÓ ele — ancora no topo;
  3. re-ancoragem inevitável ancora no MENOR não-sincronizado e vira ERRO com o
     intervalo órfão, nunca um pulo mudo para `agora`;
  4. se nem a fronteira der para medir, o tique NÃO ancora — repetir é barato,
     amputar não tem volta.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from search import sync_incremental as sync
from search import watermarks as wm_store
from search.models import Watermark


@pytest.fixture(autouse=True)
def _limpa_cache():
    cache.clear()
    yield
    cache.clear()


def _espia_logger():
    return patch.object(sync.logger, 'error')


# --------------------------------------------------------------------------
# 1. A perda da chave do Redis
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_perder_a_chave_do_redis_nao_abandona_o_passado():
    """O TESTE DO #109. Simula o restart de 26/08/2026 e prova que o passado fica.

    Mutação: troque `wm_store.obter` por `cache.get` em `_ancora_por_id` e este
    teste passa a ver a watermark saltar para o topo (999_999) — que é
    exatamente a amputação medida.
    """
    wm_store.gravar(sync._WM_PROC_ID, 100)

    cache.clear()                      # ← o restart do Redis, sem RDB e sem AOF

    with patch.object(sync, '_extremos', return_value=(1, 999_999)), \
         _espia_logger() as erro:
        wm, relatorio = sync._ancora_por_id(
            sync._WM_PROC_ID, 'tribunals_process', 'idx', 'proc_novos')

    assert wm == 100, 'a watermark tem que voltar do banco, não pular pro topo'
    assert relatorio['restaurada'] is True
    assert relatorio.get('ancorou') is None
    # ...e o cache volta a ter a chave, para o próximo tique não pagar o banco.
    assert cache.get(sync._WM_PROC_ID) == 100
    # perder o Redis é INCIDENTE, mesmo quando nada se perde: sai como ERRO.
    assert erro.called


@pytest.mark.django_db
def test_watermark_do_ts_tambem_volta_do_banco():
    """Os TRÊS lados do módulo tinham o mesmo defeito, não só o `proc_ts`."""
    quando = timezone.now() - timedelta(days=7)
    wm_store.gravar(sync._WM_PROC_TS, (quando, 4242))
    cache.clear()

    valor, estado = wm_store.obter(sync._WM_PROC_TS)

    assert estado == 'restaurada'
    ts, ident = sync._wm_par(valor)
    assert ident == 4242
    assert abs((ts - quando).total_seconds()) < 1


# --------------------------------------------------------------------------
# 2. Primeiro tique — e só ele — ancora no topo
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_primeiro_tique_ancora_no_topo_e_deixa_linha_duravel():
    assert not Watermark.objects.filter(chave=sync._WM_PROC_ID).exists()

    with patch.object(sync, '_extremos', return_value=(1, 12_345)):
        wm, relatorio = sync._ancora_por_id(
            sync._WM_PROC_ID, 'tribunals_process', 'idx', 'proc_novos')

    assert wm is None                     # ancorar não é trabalhar
    assert relatorio == {'wm': 12_345, 'ancorou': True}
    # a linha durável passa a existir: este ramo NUNCA MAIS pode ser confundido
    # com perda de chave, que era a raiz do #109.
    assert Watermark.objects.get(chave=sync._WM_PROC_ID).valor == {'t': 'int',
                                                                   'v': 12_345}


@pytest.mark.django_db
def test_primeiro_tique_sem_topo_medido_nao_ancora():
    with patch.object(sync, '_extremos', return_value=None):
        wm, relatorio = sync._ancora_por_id(
            sync._WM_PROC_ID, 'tribunals_process', 'idx', 'proc_novos')

    assert wm is None
    assert relatorio['sem_ancora'] == 'topo não medido'
    assert not Watermark.objects.filter(chave=sync._WM_PROC_ID).exists()


# --------------------------------------------------------------------------
# 3. Re-ancoragem inevitável: menor não-sincronizado + ERRO com o intervalo
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_reancoragem_inevitavel_vai_pro_menor_nao_sincronizado_e_vira_erro():
    """Linha durável existe, valor ilegível. NÃO pode ancorar no topo.

    Mutação: faça o ramo 'perdida' ancorar em `topo` e o `assert` do intervalo
    órfão quebra — que é o comportamento antigo, o que apagou o passado.
    """
    Watermark.objects.create(chave=sync._WM_PROC_ID, valor={'t': 'lixo'})

    with patch.object(sync, '_extremos', return_value=(1, 999_999)), \
         patch.object(sync, 'fronteira_do_indice', return_value=700_000), \
         _espia_logger() as erro:
        wm, relatorio = sync._ancora_por_id(
            sync._WM_PROC_ID, 'tribunals_process', 'idx', 'proc_novos')

    assert wm is None
    assert relatorio['reancorou'] is True
    assert relatorio['wm'] == 700_000, 'ancorou no topo — o passado foi embora'
    assert relatorio['orfao_ids'] == 999_999 - 700_000
    # teto/perda é ERRO REGISTRADO com o número real, nunca `return` discreto.
    assert erro.called
    msg = erro.call_args[0][0] % erro.call_args[0][1:]
    assert 'ÓRFÃO' in msg and '299999' in msg.replace('.', '').replace(',', '')
    assert wm_store.obter(sync._WM_PROC_ID) == (700_000, 'ok')


@pytest.mark.django_db
def test_sem_fronteira_medida_o_tique_nao_ancora_nada():
    """Abster > chutar: sem medida, a watermark NÃO se move para `agora`."""
    Watermark.objects.create(chave=sync._WM_PROC_ID, valor=None)

    with patch.object(sync, '_extremos', return_value=(1, 999_999)), \
         patch.object(sync, 'fronteira_do_indice', return_value=None), \
         _espia_logger():
        wm, relatorio = sync._ancora_por_id(
            sync._WM_PROC_ID, 'tribunals_process', 'idx', 'proc_novos')

    assert wm is None
    assert relatorio['sem_ancora'] == 'fronteira não medida'
    assert cache.get(sync._WM_PROC_ID) is None
    assert Watermark.objects.get(chave=sync._WM_PROC_ID).valor is None


@pytest.mark.django_db
def test_banco_mudo_nao_autoriza_reancorar():
    """Sem o lado durável não há como saber se é nascimento ou perda: não ancora."""
    with patch.object(Watermark.objects, 'filter',
                      side_effect=RuntimeError('banco fora')), \
         patch.object(wm_store.logger, 'error'), _espia_logger():
        wm, relatorio = sync._ancora_por_id(
            sync._WM_PROC_ID, 'tribunals_process', 'idx', 'proc_novos')

    assert wm is None
    assert relatorio['sem_ancora'] == 'banco mudo'


@pytest.mark.django_db
def test_proc_ts_perdida_ancora_no_menor_atualizado_em_e_nao_em_agora():
    Watermark.objects.create(chave=sync._WM_PROC_TS, valor={'t': 'ts_id',
                                                            'ts': 'não-é-data'})
    antigo = timezone.now() - timedelta(days=400)

    with patch.object(sync, '_menor_atualizado_em', return_value=antigo), \
         _espia_logger() as erro:
        saida = sync.sync_processos_atualizados()

    assert saida['reancorou'] is True
    assert saida['wm'] == antigo.isoformat()
    assert saida['orfao'][0] == antigo.isoformat()
    assert erro.called
    ts, _ = sync._wm_par(cache.get(sync._WM_PROC_TS))
    assert abs((ts - antigo).total_seconds()) < 1, 'ancorou em agora'


# --------------------------------------------------------------------------
# 4. A sonda da fronteira — "o menor não-sincronizado" é MEDIDO, não chutado
# --------------------------------------------------------------------------

def _tabela_falsa(base: int, topo: int, indexados_ate: int):
    """PG devolve ids consecutivos; o ES tem tudo até `indexados_ate`."""
    def ids_a_partir(_tabela, ini, n):
        return [i for i in range(max(ini, base), min(topo, ini + n - 1) + 1)]

    def ausentes(ids, _indice=None):
        return [i for i in ids if i > indexados_ate]

    return ids_a_partir, ausentes


def test_fronteira_do_indice_acha_o_menor_ausente():
    """A busca binária tem que cair no PRIMEIRO id fora do índice, não perto."""
    ids_a_partir, ausentes = _tabela_falsa(1, 1_000_000, indexados_ate=734_512)

    with patch.object(sync, '_extremos', return_value=(1, 1_000_000)), \
         patch.object(sync, '_ids_a_partir', side_effect=ids_a_partir), \
         patch('search.gate.ausentes_no_bloco', side_effect=ausentes):
        assert sync.fronteira_do_indice('tribunals_process', 'idx') == 734_512


def test_fronteira_com_indice_em_dia_devolve_o_topo():
    ids_a_partir, ausentes = _tabela_falsa(1, 500_000, indexados_ate=500_000)
    with patch.object(sync, '_extremos', return_value=(1, 500_000)), \
         patch.object(sync, '_ids_a_partir', side_effect=ids_a_partir), \
         patch('search.gate.ausentes_no_bloco', side_effect=ausentes):
        assert sync.fronteira_do_indice('tribunals_process', 'idx') == 500_000


def test_fronteira_abstem_quando_o_es_esta_mudo():
    ids_a_partir, _ = _tabela_falsa(1, 500_000, indexados_ate=1)
    with patch.object(sync, '_extremos', return_value=(1, 500_000)), \
         patch.object(sync, '_ids_a_partir', side_effect=ids_a_partir), \
         patch('search.gate.ausentes_no_bloco', side_effect=RuntimeError('ES fora')):
        assert sync.fronteira_do_indice('tribunals_process', 'idx') is None


def test_fronteira_abstem_quando_o_postgres_esta_mudo():
    with patch.object(sync, '_extremos', return_value=(1, 500_000)), \
         patch.object(sync, '_ids_a_partir', return_value=None):
        assert sync.fronteira_do_indice('tribunals_process', 'idx') is None
