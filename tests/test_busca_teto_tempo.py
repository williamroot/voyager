"""Estouro do tempo do job no meio da paginação não é "erro".

Medido em 16/10/2026: busca por OAB no TJRS (janelas de 2.000 linhas, ~45 s
cada) colheu 17.926 processos, a última requisição cruzou o `JOB_TIMEOUT` e o
RQ interrompeu o job. O tribunal saía como ERRO e a tela dizia "falhou" com o
resultado inteiro salvo.
"""
from unittest.mock import patch

import pytest
from rq.timeouts import JobTimeoutException

from enrichers.busca import jobs
from enrichers.busca.base import ItemEncontrado, PaginaResultado
from tribunals.models import BuscaTribunalRun


class _Motor:
    def __init__(self, paginas_antes_do_estouro):
        self.n = paginas_antes_do_estouro

    def paginar(self, criterio, valor, teto_paginas):
        for i in range(self.n):
            yield PaginaResultado(
                itens=[ItemEncontrado(numero_cnj=f'500000{i}-00.2026.8.21.0001', tribunal='TJRS')],
                pagina=i + 1, tem_proxima=True)
        raise JobTimeoutException('Task exceeded maximum timeout value (420 seconds)')


def _run():
    return BuscaTribunalRun.objects.create(
        criterio='oab', valor='RS000000', valor_normalizado='RS000000', tribunais=['TJRS'],
        por_tribunal={'TJRS': {'status': 'na_fila', 'encontrados': 0}})


@pytest.mark.django_db
def test_estouro_com_resultado_vira_ok_truncado():
    run = _run()
    with patch.object(jobs, 'buscador', return_value=_Motor(3)), \
            patch.object(jobs, '_ingerir'):
        out = jobs.buscar_no_tribunal(str(run.pk), 'TJRS')
    assert out['status'] == jobs.OK
    assert out['truncado'] is True
    assert out['encontrados'] == 3
    assert 'teto' in out['motivo_truncagem']
    run.refresh_from_db()
    assert run.por_tribunal['TJRS']['status'] == jobs.OK


@pytest.mark.django_db
def test_estouro_sem_resultado_e_fonte_indisponivel_e_nao_erro():
    run = _run()
    with patch.object(jobs, 'buscador', return_value=_Motor(0)), \
            patch.object(jobs, '_ingerir'):
        out = jobs.buscar_no_tribunal(str(run.pk), 'TJRS')
    assert out['status'] == jobs.INDISPONIVEL
