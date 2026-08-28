"""As facetas de /movimentacoes/ não podem varrer 1,96 bilhão de linhas.

MEDIDO EM 28/08/2026, em produção. `compute_filtros_movimentacoes` rodava
`SELECT ... FROM tribunals_movimentacao` **sem recorte de data**, num warm
agendado a cada **30 minutos**:

    pid=587950  active  wait=IO  q=1965s  x=1965s
    WITH ativos AS (...), movs AS (SELECT tipo_comunicacao, meio_completo,
    nome_classe FROM tribunals_movimentacao WHERE tribunal_id IN (...)) ...

**32,8 minutos e ainda não tinha terminado.** O docstring dizia "seq scan em
~30M rows"; a tabela tem 1,96 bilhão — 65x aquilo. O único teto era
`_with_timeout(3600)`, ou seja: permissão para queimar uma hora de disco e
cancelar sem entregar nada. Teto que não impede o dano não é teto (regra nº 7).

E o dano é sobre a INGESTÃO: o banco é I/O-bound, e essa varredura disputa o
mesmo disco da coleta que traz 1,4 M de publicações entre 03h e 06h UTC.

O resultado da consulta são **20 rótulos** (top 8 tipos, top 6 meios, top 6
classes) usados como opções de filtro na tela. O ranking deles não muda entre
30 dias e 17 meses de acervo. O custo muda.
"""
import re

import pytest

from dashboard import queries


def test_a_consulta_das_facetas_tem_recorte_de_data():
    """Sem `data_disponibilizacao >=`, o plano é seq scan em 1,96 bilhão."""
    import inspect
    src = inspect.getsource(queries.compute_filtros_movimentacoes)
    assert 'data_disponibilizacao >=' in src, (
        'a consulta das facetas voltou a varrer a tabela inteira'
    )
    assert "interval '%(dias)s days'" in src


def test_janela_e_configuravel_e_curta_por_padrao():
    assert queries.FILTROS_MOVIMENTACOES_JANELA_DIAS <= 14, (
        'janela longa devolve o custo que o recorte veio tirar'
    )
    assert queries.FILTROS_MOVIMENTACOES_JANELA_DIAS >= 7, (
        'janela curta demais pode não conter todos os rótulos de faceta'
    )


def test_tem_teto_de_tempo_e_dentro_de_transacao():
    """`SET LOCAL` fora de transação não vale nada — em autocommit cada
    `execute` é a sua própria transação, e o pgbouncer em transaction-mode
    descarta `SET` solto. O teto só existe dentro do `atomic()`."""
    import inspect
    src = inspect.getsource(queries.compute_filtros_movimentacoes)
    assert 'SET LOCAL statement_timeout' in src
    assert 'transaction.atomic' in src
    # o SET LOCAL tem que vir DEPOIS da abertura do atomic
    assert src.index('transaction.atomic') < src.index('SET LOCAL statement_timeout')


@pytest.mark.django_db
def test_ainda_devolve_as_tres_facetas(monkeypatch):
    """Contrato de saída intacto: as três chaves, e o cache é gravado."""
    from django.core.cache import cache
    cache.delete(queries.FILTROS_MOVIMENTACOES_CACHE_KEY)
    r = queries.compute_filtros_movimentacoes()
    assert set(r) == {'tipos', 'meios', 'classes'}
    assert cache.get(queries.FILTROS_MOVIMENTACOES_CACHE_KEY) == r


@pytest.mark.django_db
def test_le_as_publicacoes_da_janela_e_ignora_as_antigas():
    """Prova com dado: um rótulo só existente fora da janela não entra."""
    import datetime as dt

    from django.utils import timezone

    from tribunals.models import Movimentacao, Process, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJZZ', defaults={'nome': 'TJ Teste', 'sigla_djen': 'TJZZ', 'ativo': True})
    Tribunal.objects.filter(pk=t.pk).update(ativo=True)
    p = Process.objects.create(tribunal=t, numero_cnj='0000001-11.2026.8.26.0100')
    agora = timezone.now()
    Movimentacao.objects.create(
        tribunal=t, processo=p, external_id='dentro-1',
        data_disponibilizacao=agora - dt.timedelta(days=1),
        tipo_comunicacao='DENTRO_DA_JANELA', texto='x')
    Movimentacao.objects.create(
        tribunal=t, processo=p, external_id='fora-1',
        data_disponibilizacao=agora - dt.timedelta(
            days=queries.FILTROS_MOVIMENTACOES_JANELA_DIAS + 30),
        tipo_comunicacao='FORA_DA_JANELA', texto='x')

    r = queries.compute_filtros_movimentacoes()
    assert 'DENTRO_DA_JANELA' in r['tipos']
    assert 'FORA_DA_JANELA' not in r['tipos'], (
        'a janela não está sendo aplicada — a consulta voltou a olhar o acervo inteiro'
    )
