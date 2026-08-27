"""O portão tem que pegar o "success verde com um terço do dia".

CONTEXTO (medido em 25-27/08/2026). A ingestão do dia 25/08 morreu inteira:
10.410 `IngestionRun` em `failed`, zero `success`. Ninguém viu por 21 horas — o
que denunciou foi um KPI da tela, não um portão.

E quando o dia voltou, voltou **incompleto**: 1.180.554 publicações contra
1.529.530 do dia vizinho. O run fechou `success`, o log ficou limpo, o número
parecia plausível. É o padrão que o CLAUDE.md chama de **run verde, log limpo,
número redondo**, e ele já custou 47.141 publicações uma vez.

O que estes testes protegem:
  1. dia sem `success` é problema, mesmo com publicação no banco;
  2. dia COM `success` mas com um terço do volume do próprio tribunal é
     problema — é o caso que motivou o comando existir;
  3. a comparação é POR TRIBUNAL: um TJSP inteiro sumido não pode se dissolver
     no agregado nacional;
  4. fim de semana e tribunal de baixo volume NÃO viram alarme falso — portão
     que grita sempre é portão que ninguém lê;
  5. `failed` sem `success` posterior é problema; com `success` posterior, não;
  6. quando há buraco, o comando SAI COM ERRO — para servir a cron e CI sem
     depender de alguém ler a saída.
"""
import datetime

import pytest
from django.core.management import call_command

from tribunals.management.commands import conferir_dia as CD


class _Cmd(CD.Command):
    """Command com as três leituras de banco trocadas por dados de teste."""

    def __init__(self, cont, runs, tribs):
        super().__init__()
        self._cont, self._runs_, self._tribs = cont, runs, tribs

    def _contagens(self, ini, fim):
        return self._cont

    def _runs(self, dia):
        return self._runs_

    def _tribunais(self, dia):
        return self._tribs


DIA = datetime.date(2026, 8, 25)          # uma terça-feira
OPC = {'piso': CD.PISO_MEDIANA, 'fracao': CD.FRACAO_MINIMA}


def _uteis_ao_redor(t, valor, dia=DIA):
    """Contagens `valor` nos dias úteis vizinhos de `dia`, para o tribunal `t`."""
    fora = {}
    for k in range(-(CD.VIZINHOS + 2), CD.VIZINHOS + 3):
        d = dia + datetime.timedelta(days=k)
        if d.weekday() < 5 and d != dia:
            fora[(t, d)] = valor
    return fora


def test_dia_com_um_terco_do_volume_e_PROBLEMA():
    """O caso real: TJSP com 1,18 M onde a mediana dele é 1,53 M."""
    cont = _uteis_ao_redor('TJSP', 1_529_530)
    cont[('TJSP', DIA)] = 500_000            # 32,7% do normal
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert len(r['problemas']) == 1
    p = r['problemas'][0]
    assert p['t'] == 'TJSP'
    assert '33% do normal' in ' '.join(p['motivos']) or '% do normal' in ' '.join(p['motivos'])
    assert p['falta'] > 1_000_000, 'não disse QUANTO falta — número sem tamanho não age'


def test_dia_completo_com_success_FECHA():
    cont = _uteis_ao_redor('TJSP', 1_500_000)
    cont[('TJSP', DIA)] = 1_480_000          # variação normal
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert r['problemas'] == []
    assert r['fechados'] == 1


def test_sem_run_success_e_PROBLEMA_mesmo_com_publicacao():
    """Publicação no banco sem run de sucesso = alguém escreveu por fora."""
    cont = _uteis_ao_redor('TJRS', 60_000)
    cont[('TJRS', DIA)] = 60_000
    r = _Cmd(cont, {}, ['TJRS'])._conferir(DIA, OPC)
    assert 'sem run success' in ' '.join(r['problemas'][0]['motivos'])


def test_failed_com_success_POSTERIOR_nao_e_problema():
    """Quebrou e o watchdog refez: isso é o sistema funcionando."""
    cont = _uteis_ao_redor('TJPR', 40_000)
    cont[('TJPR', DIA)] = 39_000
    r = _Cmd(cont, {'TJPR': {'failed': 1, 'success': 2}}, ['TJPR'])._conferir(DIA, OPC)
    assert r['problemas'] == []


def test_failed_SEM_success_posterior_e_problema():
    cont = _uteis_ao_redor('TJPR', 40_000)
    cont[('TJPR', DIA)] = 39_000
    r = _Cmd(cont, {'TJPR': {'success': 1, 'failed': 5}}, ['TJPR'])._conferir(DIA, OPC)
    assert 'failed sem success posterior' in ' '.join(r['problemas'][0]['motivos'])


def test_tribunal_de_baixo_volume_NAO_vira_alarme_falso():
    """Portão que grita sempre é portão que ninguém lê."""
    cont = _uteis_ao_redor('TJRR', 12)
    cont[('TJRR', DIA)] = 3
    r = _Cmd(cont, {}, ['TJRR'])._conferir(DIA, OPC)
    assert r['problemas'] == []
    assert r['fechados'] == 1


def test_a_mediana_ignora_fim_de_semana():
    """Sábado com 0 não pode puxar a mediana para baixo e esconder buraco."""
    cont = _uteis_ao_redor('TJSP', 1_000_000)
    for k in range(-9, 10):                       # zera todos os fins de semana
        d = DIA + datetime.timedelta(days=k)
        if d.weekday() >= 5:
            cont[('TJSP', d)] = 0
    cont[('TJSP', DIA)] = 100_000                 # 10% do normal
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert r['problemas'], 'o fim de semana derrubou a mediana e escondeu o buraco'


def test_a_comparacao_e_POR_TRIBUNAL_e_nao_no_agregado():
    """Um TJSP inteiro sumido não pode se dissolver nos outros 58."""
    cont = {}
    for t in ('TJMG', 'TJRJ', 'TJRS', 'TJPR', 'TJSC'):
        cont.update(_uteis_ao_redor(t, 300_000))
        cont[(t, DIA)] = 300_000
    cont.update(_uteis_ao_redor('TJSP', 1_500_000))
    cont[('TJSP', DIA)] = 0                       # sumiu inteiro
    tribs = ['TJMG', 'TJPR', 'TJRJ', 'TJRS', 'TJSC', 'TJSP']
    runs = {t: {'success': 1} for t in tribs}
    r = _Cmd(cont, runs, tribs)._conferir(DIA, OPC)
    assert [p['t'] for p in r['problemas']] == ['TJSP']
    assert r['fechados'] == 5


@pytest.mark.django_db
def test_o_comando_SAI_COM_ERRO_quando_ha_buraco(monkeypatch):
    """Cron e CI não podem depender de alguém LER a saída."""
    cont = _uteis_ao_redor('TJSP', 1_000_000)
    cont[('TJSP', DIA)] = 10_000
    monkeypatch.setattr(CD.Command, '_contagens', lambda s, i, f: cont)
    monkeypatch.setattr(CD.Command, '_runs', lambda s, d: {'TJSP': {'success': 1}})
    monkeypatch.setattr(CD.Command, '_tribunais', lambda s, d: ['TJSP'])
    with pytest.raises(SystemExit) as e:
        call_command('conferir_dia', '2026-08-25')
    assert e.value.code == 1


@pytest.mark.django_db
def test_o_comando_sai_ZERO_quando_o_dia_fecha(monkeypatch):
    cont = _uteis_ao_redor('TJSP', 1_000_000)
    cont[('TJSP', DIA)] = 990_000
    monkeypatch.setattr(CD.Command, '_contagens', lambda s, i, f: cont)
    monkeypatch.setattr(CD.Command, '_runs', lambda s, d: {'TJSP': {'success': 1}})
    monkeypatch.setattr(CD.Command, '_tribunais', lambda s, d: ['TJSP'])
    call_command('conferir_dia', '2026-08-25')     # não levanta
