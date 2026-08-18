"""Indexar publicação um-a-um não acompanha a coleta.

CONTEXTO (medido em 18/08/2026). `indexar_movimentacao` é UM job por publicação
— um SELECT no Postgres e um `es.index()` por item. O tick de sincronismo
enfileirava até 50.000 desses a cada 10 minutos (300 mil/h) contra um dreno
medido de ~200 mil/h. A fila só podia crescer, e cresceu:

    fila `es_index` ........ 1.825.123 e subindo
    Postgres ............... 1,344 bilhão de movimentações
    Elasticsearch .......... 1,166 bilhão de documentos
                             → o índice já corre ~178 milhões de docs atrás

Isso é a terceira perda da tabela do CLAUDE.md se repetindo: dado coletado que a
tela não enxerga vale zero. Com 212 milhões de publicações pra recuperar,
coletar sem indexar em lote é enterrar 212 milhões.

O que estes testes protegem: que o caminho de volume seja UM `_bulk` por lote, e
que o tick enfileire lotes em vez de itens.
"""
from unittest.mock import MagicMock, patch

import pytest

from search import jobs, sync_incremental


class EsFalso:
    def __init__(self, erros=False):
        self.bulks = []
        self.indexes = []
        self._erros = erros

    def bulk(self, operations=None, **kw):
        self.bulks.append(operations)
        if self._erros:
            return {'errors': True, 'items': [{'index': {'status': 400}}]}
        return {'errors': False, 'items': []}

    def index(self, **kw):
        self.indexes.append(kw)


@pytest.fixture
def movs(db):
    import datetime

    from tribunals.models import Movimentacao, Process, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJXX', defaults={'nome': 'TJ Teste', 'sigla_djen': 'TJXX'})
    p = Process.objects.create(tribunal=t, numero_cnj='0000001-11.2020.8.26.0100')
    return [Movimentacao.objects.create(
        tribunal=t, processo=p, external_id=f'e{i}', texto=f'publicação {i}',
        data_disponibilizacao=datetime.date(2026, 8, 13),
    ) for i in range(5)]


@pytest.mark.django_db
def test_lote_vira_um_unico_bulk(movs):
    es = EsFalso()
    with patch.object(jobs, 'get_es', lambda: es), \
         patch.object(jobs, 'indices_espelho', lambda _: ['voyager-movimentacoes']):
        jobs.indexar_movimentacoes_bulk([m.pk for m in movs])

    assert len(es.bulks) == 1, 'quebrou o lote em várias chamadas'
    assert es.indexes == [], 'ainda indexa item a item'
    # cada doc ocupa 2 entradas no corpo do _bulk (ação + documento)
    assert len(es.bulks[0]) == 2 * len(movs)


@pytest.mark.django_db
def test_indice_espelho_recebe_os_dois(movs):
    """Durante migração de índice a escrita vai nos dois — se o bulk mandasse
    só pro primeiro, o índice novo nasceria incompleto e ninguém veria."""
    es = EsFalso()
    with patch.object(jobs, 'get_es', lambda: es), \
         patch.object(jobs, 'indices_espelho', lambda _: ['v1', 'v2']):
        jobs.indexar_movimentacoes_bulk([m.pk for m in movs])
    destinos = {op['index']['_index'] for op in es.bulks[0] if isinstance(op, dict)
                and 'index' in op and isinstance(op['index'], dict)
                and '_index' in op['index']}
    assert destinos == {'v1', 'v2'}


@pytest.mark.django_db
def test_falha_do_bulk_propaga(movs):
    """Engolir a exceção deixaria o job verde com o dado fora do índice — que é
    exatamente o modo de falha que este projeto persegue."""
    es = MagicMock()
    es.bulk.side_effect = RuntimeError('ES fora')
    with patch.object(jobs, 'get_es', lambda: es), \
         patch.object(jobs, 'indices_espelho', lambda _: ['x']), \
         pytest.raises(RuntimeError):
        jobs.indexar_movimentacoes_bulk([m.pk for m in movs])


@pytest.mark.django_db
def test_lote_vazio_nao_chama_o_es(db):
    es = EsFalso()
    with patch.object(jobs, 'get_es', lambda: es):
        jobs.indexar_movimentacoes_bulk([])
    assert es.bulks == []


def test_tick_enfileira_lotes_e_nao_itens():
    """50.000 movs por tick viravam 50.000 jobs. Em lote de 500, viram 100."""
    fila = MagicMock()
    with patch('django_rq.get_queue', lambda _: fila):
        n = sync_incremental._enfileirar_movs(list(range(50_000)))

    assert n == 50_000
    chamadas = fila.enqueue.call_args_list
    assert len(chamadas) == 50_000 // sync_incremental.CHUNK == 100
    assert all(c.args[0] == 'search.jobs.indexar_movimentacoes_bulk' for c in chamadas)
    # e nenhum pk pode se perder no fatiamento
    enviados = [pk for c in chamadas for pk in c.args[1]]
    assert enviados == list(range(50_000))
